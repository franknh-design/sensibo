"""
2GM Varme - Backend server
Snakker med Sensibo API, met.no og serverer index.html
Kjøres på Windows server med: python server.py
"""

import os
import json
import time
import math
import logging
import threading
import requests
import csv
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CONFIG
# ============================================================
SENSIBO_API_KEY = os.getenv("SENSIBO_API_KEY", "")
SENSIBO_DEVICE_NAME = os.getenv("SENSIBO_DEVICE_NAME", "Frank's device AC")
SENSIBO_DEVICE_UID = os.getenv("SENSIBO_DEVICE_UID", "YRqRonRH")  # Frank stue; Vigdis-instansen setter MTBb7tSM
ROOM_NAME = os.getenv("ROOM_NAME", "Frank stue")
SENSIBO_POLL_SECONDS = int(os.getenv("SENSIBO_POLL_SECONDS", "90"))
# Stuene i appen. Hver stue er en egen server-instans med egne innstillinger; hovedinstansen
# (den uten port) videresender /r/<id>/api/... til de andre på localhost.
# "emails" på en stue = de som skal havne der når appen åpnes (fra Cloudflare Access).
ROOMS = json.loads(os.getenv("ROOMS", "[]")) or [{"id": "main", "name": ROOM_NAME}]
# Denne instansens stue-id. Appen merker hver endring med X-Room, og vi avviser endringer
# merket for en annen stue — så Vigdis' innstillinger aldri kan havne hos Frank eller omvendt.
# Push (ntfy) — eget emne per stue, så hver får bare sine egne varsler
NTFY_URL = os.getenv("NTFY_URL", "").rstrip("/")
NTFY_TOKEN = os.getenv("NTFY_TOKEN", "")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
FILTER_DAYS_DEFAULT = int(os.getenv("FILTER_DAYS", "28"))
# Ny ønsket temp i appen blir ny standard for perioden som gjelder (dag-regelen eller natt), ikke bare til neste skifte
TARGET_SETS_STANDARD = os.getenv("TARGET_SETS_STANDARD", "") == "1"
ROOM_ID = os.getenv("ROOM_ID") or next((r["id"] for r in ROOMS if not r.get("port")), "main")
# Hvor utetemperaturen hentes, per stue (Sensibo-enhet). Låst i koden så en kopiert .env
# aldri kan gi Vigdis Franks Hue-sensor eller Finnsnes-været.
OUTDOOR_BY_DEVICE = {
    "YRqRonRH": {"place": "Vårheim, Finnsnes", "lat": 69.2430, "lon": 18.1244, "ha_sensor": True},  # Frank stue (Vårheim 12)
    "MTBb7tSM": {"place": "Tromsdalen", "lat": 69.6436, "lon": 18.9950, "ha_sensor": False},  # Vigdis stue
}
_outdoor = OUTDOOR_BY_DEVICE.get(SENSIBO_DEVICE_UID) or {
    "place": os.getenv("METNO_PLACE", ""), "lat": float(os.getenv("METNO_LAT", "69.2333")),
    "lon": float(os.getenv("METNO_LON", "17.9833")), "ha_sensor": True}
METNO_PLACE, METNO_LAT, METNO_LON = _outdoor["place"], _outdoor["lat"], _outdoor["lon"]
# Utetemperatur fra Hue-sensoren ved kjellerdøra via Home Assistant; met.no er reserve
HA_URL = os.getenv("HA_URL", "").rstrip("/")
HA_TOKEN = os.getenv("HA_TOKEN", "")
HA_OUTDOOR_ENTITY = os.getenv("HA_OUTDOOR_ENTITY", "") if _outdoor["ha_sensor"] else ""
SENSOR_MAX_AGE = 3 * 3600      # sensoren regnes som død hvis den ikke har rapportert på 3 t
SENSOR_POLL_EVERY = 5 * 60
SENSOR_GRACE = 30 * 60         # HA faller ut korte perioder — behold siste gode verdi så lenge
SERVER_PORT = int(os.getenv("SERVER_PORT", "8765"))
# Kun localhost — Cloudflare-tunnelen er eneste vei inn (0.0.0.0 ga åpen styring fra hele nettet)
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
LOG_FILE = "heat_history.csv"
STATE_FILE = "state.json"

SENSIBO_BASE = "https://home.sensibo.com/api/v2"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        # systemd sender stdout/stderr til server.log — en egen FileHandler ga doble linjer
        logging.StreamHandler()
    ]
)
log = logging.getLogger("2gm-varme")

# ============================================================
# GLOBAL STATE CACHE
# ============================================================
cache = {
    "ac": {},
    "temperature": None,
    "humidity": None,
    "outdoor_temp": None,
    "outdoor_forecast": [],
    "last_sensibo": 0,
    "last_metno": 0,
    "device_uid": SENSIBO_DEVICE_UID,  # kjeller-enhet bCCMwLk4 skal ikke brukes
    "auto_enabled": False,
    "auto_target_temp": 22.0,
    "hysteresis": 1.0,
    "normal_fan": "medium_high",
    "boost_temp": 22.0,
    "schedules": [],
    "last_command": None,
    "boost_active": False,
    "boost_target": 22.0,
    "boost_start_temp": None,
    "boost_start_time": None,
}

# ============================================================
# SENSIBO API
# ============================================================
def sensibo_get(endpoint, params=None):
    p = params or {}
    p["apiKey"] = SENSIBO_API_KEY
    try:
        r = requests.get(f"{SENSIBO_BASE}{endpoint}", params=p, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Sensibo GET {endpoint}: {getattr(getattr(e, 'response', None), 'status_code', type(e).__name__)}")
        return None

# ============================================================
# AKTIVITETSLOGG — hva pumpa gjør gjennom dagen (vises i appen)
# ============================================================
EVENTS_FILE = "events.jsonl"
EVENTS_KEEP_DAYS = 30
events = deque(maxlen=3000)
events_lock = threading.Lock()

FAN_NO = {"quiet": "stille", "low": "lav", "medium": "middels", "medium_high": "middels-høy",
          "high": "høy", "auto": "auto", "strong": "kraftig"}

def describe_ac(ac):
    if not ac.get("on", True):
        return "av"
    parts = []
    if ac.get("targetTemperature") is not None:
        parts.append(f"{ac['targetTemperature']}°C")
    if ac.get("fanLevel"):
        parts.append(f"{FAN_NO.get(ac['fanLevel'], ac['fanLevel'])} vifte")
    return " · ".join(parts) or "på"

def describe_change(prev, new):
    """Hva som endret seg, f.eks. «vifte middels-høy → høy · 24 → 30°C». None hvis ingenting."""
    prev = prev or {}
    if "on" in new and bool(new["on"]) != bool(prev.get("on", True)):
        return "slått på · " + describe_ac(new) if new["on"] else "slått av"
    if new.get("on") is False:
        return None
    parts = []
    pf, nf = prev.get("fanLevel"), new.get("fanLevel")
    if nf and nf != pf:
        parts.append(f"vifte {FAN_NO.get(pf, pf or '?')} → {FAN_NO.get(nf, nf)}")
    pt, nt = prev.get("targetTemperature"), new.get("targetTemperature")
    if nt is not None and nt != pt:
        parts.append(f"{pt if pt is not None else '?'} → {nt}°C")
    return " · ".join(parts) or None

def add_event(kind, msg, **data):
    """kind: pump | auto | schedule | external | status | setting | boost | error"""
    ev = {"ts": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"), "kind": kind, "msg": msg}
    if data:
        ev["data"] = data
    with events_lock:
        events.append(ev)
        try:
            with open(EVENTS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError as e:
            log.error(f"Kunne ikke skrive aktivitetslogg: {e}")

def load_events():
    """Leser loggen fra disk og kaster det som er eldre enn EVENTS_KEEP_DAYS."""
    if not os.path.exists(EVENTS_FILE):
        return
    cutoff = (datetime.now(LOCAL_TZ) - timedelta(days=EVENTS_KEEP_DAYS)).isoformat()
    kept = []
    with open(EVENTS_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("ts", "") >= cutoff:
                kept.append(ev)
    kept = kept[-events.maxlen:]
    events.extend(kept)
    # Unngå dobbel timestatus rett etter omstart
    for ev in reversed(kept):
        if ev.get("kind") == "status":
            cache["last_status_hour"] = ev["ts"][:13]
            break
    with open(EVENTS_FILE, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(ev, ensure_ascii=False) + "\n" for ev in kept)

def recent_events(hours=48, limit=500):
    cutoff = (datetime.now(LOCAL_TZ) - timedelta(hours=hours)).isoformat()
    return [ev for ev in list(events) if ev["ts"] >= cutoff][-limit:]

def sensibo_patch(uid, payload, reason=""):
    """Sender kommando til Sensibo — hopper over hvis identisk med siste sendte kommando."""
    # Sjekk om kommandoen er identisk med siste sendte
    last = cache.get("last_command")
    if last is not None:
        same = all(last.get(k) == v for k, v in payload.items())
        if same:
            log.debug(f"Hopper over duplikat-kommando: {payload}")
            return {"result": "skipped"}  # Returner som om det gikk bra

    # Sensibo avviser desimaltall ("22.0" → 400 InvalidState); kun heltall er gyldige
    if "targetTemperature" in payload:
        payload = {**payload, "targetTemperature": int(round(float(payload["targetTemperature"])))}

    prev_ac = dict(cache.get("ac") or {})
    try:
        r = requests.post(
            f"{SENSIBO_BASE}/pods/{uid}/acStates",
            params={"apiKey": SENSIBO_API_KEY},
            json={"acState": payload},
            timeout=10
        )
        r.raise_for_status()
        cache["last_command"] = dict(payload)
        log.info(f"Sensibo kommando sendt: {payload}")
        change = describe_change(prev_ac, payload) or f"pumpe → {describe_ac(payload)}"
        add_event("pump", f"{reason or 'Kommando'}: {change}", **payload)
        return r.json()
    except requests.HTTPError as e:
        log.error(f"Sensibo kommando avvist ({e.response.status_code}): {payload} → {e.response.text[:200]}")
        add_event("error", f"Sensibo avviste kommando ({reason or 'kommando'}): {describe_ac(payload)}")
        return None
    except Exception as e:
        log.error(f"Sensibo kommando feilet: {type(e).__name__}")
        return None

def get_device_uid():
    if cache["device_uid"]:
        return cache["device_uid"]
    data = sensibo_get("/users/me/pods", {"fields": "id,room"})
    if not data:
        return None
    for pod in data.get("result", []):
        if SENSIBO_DEVICE_NAME.lower() in pod.get("room", {}).get("name", "").lower():
            cache["device_uid"] = pod["id"]
            log.info(f"Fant enhet: {pod['room']['name']} ({pod['id']})")
            return pod["id"]
    # Fallback: ta første enhet
    if data.get("result"):
        uid = data["result"][0]["id"]
        cache["device_uid"] = uid
        log.warning(f"Bruker første enhet: {uid}")
        return uid
    return None

AC_KEYS = ("on", "mode", "targetTemperature", "fanLevel")

EXTERNAL_PAUSE_HOURS = 3
MIN_HOLD_OFF_TEMP = 16  # HA sin «verandadør åpen»-verdi

def resume_from_pause(why):
    if cache.get("external_pause_until"):
        cache["external_pause_until"] = None
        add_event("auto", f"Auto fortsetter ({why})")

def detect_external_change(ac):
    """Logger endringer som ikke kom fra oss (fjernkontroll, Sensibo-appen, HA) og pauser
    auto-styringen, så vi ikke sloss mot f.eks. HA-automasjonen for verandadøra (16 °C ved åpen dør).
    Auto gjenopptas ved neste endring utenfra (HA setter tilbake når døra lukkes) eller etter 3 t."""
    seen = {k: ac.get(k) for k in AC_KEYS}
    prev = cache.get("ac_seen")
    cache["ac_seen"] = seen
    if prev is None or seen == prev:
        return
    # 16 °C eller lavere / av = «hold av» (HA ved åpen verandadør): alltid pause, aldri gjenoppta på dette
    hold_off = seen.get("on") is False or (seen.get("targetTemperature") or 99) <= MIN_HOLD_OFF_TEMP
    if hold_off:
        ours = cache.get("last_command") or {}
        if ours and all(seen.get(k) == v for k, v in ours.items() if k in AC_KEYS):
            return
        cache["last_command"] = None
        change = describe_change(prev, seen) or f"pumpe → {describe_ac(seen)}"
        add_event("external", f"Endret utenfor appen: {change}", **seen)
        if cache.get("auto_enabled"):
            cache["external_pause_until"] = time.time() + EXTERNAL_PAUSE_HOURS * 3600
            cache.pop("auto_heat_start", None)
            add_event("auto", f"Auto pauset (pumpa satt til minimum/av utenfor appen) — fortsetter når den endres igjen, senest om {EXTERNAL_PAUSE_HOURS} t")
        return
    if cache.get("external_pause_until"):
        cache["external_pause_until"] = None
        cache["last_command"] = None
        change = describe_change(prev, seen) or f"pumpe → {describe_ac(seen)}"
        add_event("external", f"Endret utenfor appen: {change}", **seen)
        add_event("auto", "Auto fortsetter (pumpa ble endret tilbake utenfor appen)")
        return
    ours = cache.get("last_command") or {}
    if ours and all(seen.get(k) == v for k, v in ours.items() if k in AC_KEYS):
        return  # vår egen kommando som nå er bekreftet
    change = describe_change(prev, seen) or f"pumpe → {describe_ac(seen)}"
    add_event("external", f"Endret utenfor appen: {change}", **seen)
    # Ellers ville duplikatsperren i sensibo_patch hindre oss i å sette tilbake samme verdi
    cache["last_command"] = None
    if cache.get("auto_enabled"):
        cache["external_pause_until"] = time.time() + EXTERNAL_PAUSE_HOURS * 3600
        cache.pop("auto_heat_start", None)
        add_event("auto", f"Auto pauset (endret utenfor appen) — fortsetter når pumpa endres igjen, senest om {EXTERNAL_PAUSE_HOURS} t")

ALLOWED_MODES = ("heat", "fan")  # kjøling er aldri tillatt — heller ikke «auto»/«dry», som kan kjøle

def enforce_no_cool(ac):
    """Setter pumpa tilbake til varme hvis noen har satt den til kjøling (fjernkontroll/Sensibo-appen)."""
    mode = ac.get("mode")
    if not ac.get("on") or mode in ALLOWED_MODES or not mode:
        return
    uid = get_device_uid()
    if not uid:
        return
    cache["last_command"] = None  # duplikatsperren skal ikke stoppe tilbakestillingen
    log.warning(f"BLOKKERT: pumpa sto i {mode}-modus — setter tilbake til varme")
    if sensibo_patch(uid, {"mode": "heat"}, f"Kjøling ikke tillatt ({mode}-modus satt utenfor appen)"):
        ac["mode"] = "heat"
        cache["ac_seen"] = {k: ac.get(k) for k in AC_KEYS}

def fetch_sensibo_state():
    """Henter AC-state og romsensor-temperatur i ett API-kall, og kjører så styringen på FERSK temp."""
    data = sensibo_get(f"/pods/{SENSIBO_DEVICE_UID}", {"fields": "acState,motionSensors,mainMeasurementsSensor,measurements"})
    if not data:
        return
    result = data.get("result", {})
    ac = result.get("acState", {})
    detect_external_change(ac)
    enforce_no_cool(ac)
    cache["ac"] = ac
    cache["last_sensibo"] = time.time()

    # Romtemperatur: ekstern romsensor (mainMeasurementsSensor), ellers innebygd
    main = result.get("mainMeasurementsSensor")
    meas = (main or {}).get("measurements", {}) if main and main.get("isMainSensor") else {}
    if meas.get("temperature") is not None:
        cache["temperature"], cache["humidity"] = meas["temperature"], meas.get("humidity")
        log.info(f"Sensibo romsensor {main.get('qrId')}: {meas['temperature']}°C, {meas.get('humidity')}%, AC={'På' if ac.get('on') else 'Av'}")
    else:
        meas = result.get("measurements", {})
        cache["temperature"], cache["humidity"] = meas.get("temperature"), meas.get("humidity")
        log.info(f"Sensibo innebygd sensor: {cache['temperature']}°C, AC={'På' if ac.get('on') else 'Av'}")
    record_room_temp(cache.get("temperature"))
    record_series()

    check_boost_complete()
    auto_feedback_check()

# ── Tidsserie for grafen: romtemp, innstilt, ønsket, vifte, ute — én rad per måling ──
SERIES_FILE = "series.jsonl"
SERIES_KEEP_DAYS = 35
FAN_ORDER = ["quiet", "low", "medium", "medium_high", "high", "strong"]  # auto vises for seg
RANGES = {"1h": 3600, "12h": 12 * 3600, "24h": 24 * 3600, "7d": 7 * 86400, "30d": 30 * 86400}

def record_series():
    ac = cache.get("ac") or {}
    row = {"t": int(time.time()), "r": cache.get("temperature"),
           "s": ac.get("targetTemperature") if ac.get("on") else None, "f": ac.get("fanLevel") if ac.get("on") else "off",
           "m": cache.get("auto_target_temp"), "u": cache.get("outdoor_temp")}
    try:
        with open(SERIES_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        log.error(f"Tidsserie: {e}")

def init_series():
    """Rydder bort gammelt; første gang fylles det bakover fra timestatus/pumpehendelser i aktivitetsloggen."""
    cutoff = time.time() - SERIES_KEEP_DAYS * 86400
    rows = []
    if os.path.exists(SERIES_FILE):
        with open(SERIES_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("t", 0) >= cutoff:
                    rows.append(r)
    else:
        for ev in list(events):
            d = ev.get("data") or {}
            if ev.get("kind") not in ("status", "pump", "external") or not d:
                continue
            t = datetime.fromisoformat(ev["ts"]).timestamp()
            if t < cutoff:
                continue
            on = d.get("on", True)
            rows.append({"t": int(t), "r": d.get("room"), "u": d.get("outdoor"), "m": None,
                         "s": d.get("targetTemperature") if on else None,
                         "f": d.get("fanLevel") if on else "off", "b": 1})
        rows.sort(key=lambda r: r["t"])
    with open(SERIES_FILE, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)

def series(range_key):
    """Nedsamplet til ≤ ~300 punkter: snitt for temperaturer, siste verdi for innstilt/ønsket, mest brukte vifte."""
    span = RANGES.get(range_key, 86400)
    now = time.time()
    start = now - span
    rows = []
    with open(SERIES_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r["t"] >= start:
                rows.append(r)
    bucket = max(90, span / 300)
    out, cur = [], None
    for r in rows:
        k = int((r["t"] - start) // bucket)
        if cur is None or cur["k"] != k:
            if cur:
                out.append(cur)
            cur = {"k": k, "t": int(start + (k + 0.5) * bucket), "rs": [], "us": [], "fans": {}, "s": None, "m": None}
        if r.get("r") is not None: cur["rs"].append(r["r"])
        if r.get("u") is not None: cur["us"].append(r["u"])
        if r.get("s") is not None or r.get("f") == "off": cur["s"] = r.get("s")
        if r.get("m") is not None: cur["m"] = r["m"]
        if r.get("f"): cur["fans"][r["f"]] = cur["fans"].get(r["f"], 0) + 1
    if cur:
        out.append(cur)
    pts, last = [], {"r": None, "u": None, "m": None}
    for c in out:
        p = {"t": c["t"],
             "r": round(sum(c["rs"]) / len(c["rs"]), 2) if c["rs"] else None,
             "u": round(sum(c["us"]) / len(c["us"]), 1) if c["us"] else None,
             "s": c["s"], "m": c["m"],
             "f": max(c["fans"], key=c["fans"].get) if c["fans"] else None}
        for k in ("r", "u", "m"):  # hull (f.eks. pumpehendelser uten romtemp): bruk forrige verdi
            if p[k] is None:
                p[k] = last[k]
            last[k] = p[k]
        pts.append(p)
    return {"range": range_key, "start": int(start), "end": int(now), "points": pts}

# ── D-ledd: hvor fort rommet endrer seg ──
ROOM_HIST_SECONDS = 3600
RATE_WINDOW = 15 * 60       # stigning/fall måles over siste ~15 min

def record_room_temp(t):
    if t is None:
        return
    now = time.time()
    hist = cache.setdefault("room_hist", [])
    hist.append((now, t))
    while hist and now - hist[0][0] > ROOM_HIST_SECONDS:
        hist.pop(0)

def room_rate():
    """°C per time over siste ~15 min (minst 10 min data), ellers None."""
    hist = cache.get("room_hist") or []
    if len(hist) < 3:
        return None
    now, t_now = hist[-1]
    old = [h for h in hist if now - h[0] >= RATE_WINDOW]
    t0, v0 = old[-1] if old else hist[0]
    if now - t0 < 10 * 60:
        return None
    return (t_now - v0) / ((now - t0) / 3600)

# (fetch_room_sensor er slått sammen med fetch_sensibo_state)

# ============================================================
# MET.NO API
# ============================================================
def fetch_metno():
    try:
        url = f"https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={METNO_LAT}&lon={METNO_LON}"
        r = requests.get(url, headers={"User-Agent": "2GMVarme/1.0 frank@2gm.no"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        series = data["properties"]["timeseries"]
        if series:
            now = series[0]["data"]["instant"]["details"]
            cache["metno_temp"] = now.get("air_temperature")
            cache["outdoor_forecast"] = [
                {
                    "time": s["time"],
                    "temp": s["data"]["instant"]["details"].get("air_temperature")
                }
                for s in series[:12]
            ]
            cache["last_metno"] = time.time()
            log.info(f"met.no: ute={cache['metno_temp']}°C")
    except Exception as e:
        log.error(f"met.no feil: {e}")
    resolve_outdoor()

def fetch_outdoor_sensor():
    """Leser Hue-utesensoren fra HA. Setter sensor_temp=None hvis HA er nede eller sensoren er gammel."""
    cache["last_sensor_poll"] = time.time()
    if not (HA_URL and HA_TOKEN and HA_OUTDOOR_ENTITY):
        return
    try:
        r = requests.get(f"{HA_URL}/api/states/{HA_OUTDOOR_ENTITY}",
                         headers={"Authorization": f"Bearer {HA_TOKEN}"}, timeout=10)
        r.raise_for_status()
        d = r.json()
        seen = d.get("last_reported") or d.get("last_updated")
        age = time.time() - datetime.fromisoformat(seen).timestamp()
        val = float(d["state"])
        if age <= SENSOR_MAX_AGE:
            cache["sensor_temp"] = val
            cache["sensor_ok_at"] = time.time()
        else:
            cache["sensor_temp"] = None
            log.warning(f"Utesensor har ikke rapportert på {age/3600:.1f} t")
    except requests.HTTPError as e:
        log.warning(f"Utesensor (HA) utilgjengelig: HTTP {e.response.status_code}")
    except Exception as e:
        log.warning(f"Utesensor (HA) utilgjengelig: {type(e).__name__}")
    resolve_outdoor()

def resolve_outdoor():
    """Velger kilde for utetemperatur: Hue-sensor når den lever, ellers met.no."""
    if cache.get("sensor_temp") is not None and time.time() - cache.get("sensor_ok_at", 0) <= SENSOR_GRACE:
        temp, source = cache["sensor_temp"], "sensor"
    else:
        temp, source = cache.get("metno_temp"), "met.no"
    if temp is None:
        return
    cache["outdoor_temp"] = temp
    prev = cache.get("outdoor_source")
    cache["outdoor_source"] = source
    if prev and prev != source:
        add_event("setting", "Utetemperatur fra Hue-sensoren igjen" if source == "sensor"
                  else f"Ingen svar fra Home Assistant på {SENSOR_GRACE // 60} min — bruker met.no inntil videre")



# ============================================================
# AUTO FEEDBACK LOOP
# ============================================================
NORMAL_FAN = "medium_high"

# Hysteresis — hvor mye temp må falle før vi reagerer
def check_boost_complete():
    """Sjekker om boost-mål er nådd og avslutter boost."""
    if not cache.get("boost_active"):
        return
    current = cache.get("temperature")
    target = cache.get("boost_target", 22.0)
    start_temp = cache.get("boost_start_temp", 0)
    if current is None:
        return
    # Boost er bare meningsfull hvis målet er høyere enn starttemp
    if target <= start_temp:
        log.warning(f"Boost-mål ({target}°C) er ikke høyere enn starttemp ({start_temp}°C) — avbryter boost")
        cache["boost_active"] = False
        return
    if current >= target:
        # Mål nådd — logg og gå tilbake til normalinnstilling
        elapsed = (time.time() - cache.get("boost_start_time", time.time())) / 60
        delta = target - (cache.get("boost_start_temp") or current)
        min_per_deg = elapsed / max(0.1, delta)
        # Logg til CSV
        log_heat_session({
            "timestamp": datetime.now().isoformat(),
            "from_temp": cache.get("boost_start_temp"),
            "to_temp": target,
            "outdoor_temp": cache.get("outdoor_temp"),
            "elapsed_min": round(elapsed, 1),
            "min_per_deg": round(min_per_deg, 2),
            "fan_speed": "high"
        })
        cache["boost_active"] = False
        uid = get_device_uid()
        if uid:
            normal_fan = cache.get("normal_fan", "medium_high")
            payload = {"on": True, "mode": "heat", "targetTemperature": target, "fanLevel": normal_fan}
            result = sensibo_patch(uid, payload, "Boost ferdig")
            if result:
                cache["ac"].update(payload)
                add_event("boost", f"Boost ferdig: {delta:.1f}°C på {elapsed:.0f} min")
                log.info(f"Boost ferdig! {elapsed:.0f} min for {delta:.1f}°C → {target}°C, {normal_fan} vifte")

def log_auto_heat_session(current):
    """Logger en auto-oppvarming (30 °C + høy vifte) til læringshistorikken når den er ferdig."""
    start = cache.pop("auto_heat_start", None)
    if not start:
        return
    delta = current - start["temp"]
    elapsed = (time.time() - start["time"]) / 60
    if delta < 0.5 or elapsed < 1:
        return  # for lite til å si noe om oppvarmingstid
    log_heat_session({
        "timestamp": datetime.now().isoformat(),
        "from_temp": start["temp"],
        "to_temp": current,
        "outdoor_temp": start.get("outdoor"),
        "elapsed_min": round(elapsed, 1),
        "min_per_deg": round(elapsed / delta, 2),
        "fan_speed": "high"
    })
    add_event("auto", f"Oppvarmet {start['temp']:.1f} → {current:.1f}°C på {elapsed:.0f} min")

# Holdemodus: pumpa måler ved egen sensor (oppe på veggen) og demper seg før rommet er varmt nok.
# Derfor settes den til mål + et påslag som justerer seg selv, i stedet for å sykle 30 °C/høy vifte.
HOLD_OFFSET_MIN, HOLD_OFFSET_MAX = -3, 3
HOLD_ADJUST_EVERY = 30 * 60      # maks én justering av påslaget per halvtime (hver kommando piper)
HOLD_LOW, HOLD_HIGH = 0.3, 0.3   # rom under mål−0,3 → påslag +1; over mål+0,3 → påslag −1
HEAT_STOP_BELOW = 0.3            # oppvarming (30 °C) stopper ved mål−0,3

# ── Trinnvis oppvarming: varmere luft først, høy vifte bare som siste utvei ──
STAGED_DEFAULT = {"enabled": False, "step": 2, "every_min": 30, "full_below": 2.0, "fan": "medium_high",
                  "min_offset": 0,   # pumpa aldri lavere enn ønsket + dette (23 ved ønsket 23 holder ofte ikke)
                  "min_offset_below": -5}  # …men bare når det er så kaldt ute (mildt: 24 holdt stua på 24)
STAGED_START_BELOW = 0.3   # start trinn når rommet er så mye under ønsket
STAGED_DONE_BELOW = 0.1    # ferdig når rommet er innen 0,1 °C av ønsket
STAGED_RISING = 0.2        # har rommet steget så mye siden forrige trinn, venter vi (det virker)
STAGED_FAN_ABOVE = 3       # holdes pumpa mer enn 3° over ønsket, beholdes trinn-viften (ikke auto)
FLOOR_RELEASE_ABOVE = 0.8  # gulvet gis opp bare når rommet er tydelig for varmt (sol, vedovn o.l.)
# D-ledd (fart): bremse før målet når rommet stiger fort, reagere tidligere når det faller fort
D_RISE = 1.2               # °C/t (≈0,3 på 15 min) = stiger fort
D_FALL = -0.8              # °C/t = faller fort
D_BRAKE_WITHIN = 0.5       # stiger det fort og rommet er innen 0,5 av målet: avslutt trinnene nå
D_RISING = 0.3             # °C/t: rommet stiger allerede — ikke ta neste trinn
D_RISE_FULL = 2.0          # full gass stiger naturlig fort — brems først over 2 °C/t
AFTER_FULL_GRACE = 30 * 60 # etter full gass svinger rommet litt — ikke start trinn de første 30 min
QUICK_REACH = 15 * 60      # nås målet innen 15 min etter et trinn, var trinnet for stort → hold ett hakk under

def min_off():
    """Laveste påslag holdet kan gå til. Minstegrensen gjelder bare på dagtid — om natta skal
    nattsenkingen få virke (ellers holdt «ønsket +1» stua på 23 °C med nattemål 22) — og bare når
    det er kaldt ute (på en mild dag holdt «ønsket +1» stua fast på 23,9)."""
    st = get_staged()
    if not st["enabled"] or cache.get("target_mode") in ("night", "away"):
        return HOLD_OFFSET_MIN
    out = cache.get("outdoor_temp")
    if out is not None and out > st.get("min_offset_below", -5):
        return HOLD_OFFSET_MIN
    return max(HOLD_OFFSET_MIN, int(st.get("min_offset", 0)))

def get_staged():
    st = {**STAGED_DEFAULT, **(cache.get("staged") or {})}
    if st["fan"] in ("auto", "high"):
        st["fan"] = "medium_high"  # trinnene skal ha fast, rolig vifte
    return st

def _is_heating(ac):
    """Full trøkk (nødoppvarming) pågår — ikke et lært/holdt 30°/høy."""
    return bool(ac.get("on") and ac.get("targetTemperature") == 30 and ac.get("fanLevel") == "high"
                and cache.get("full_heat", True))

# ── Vedovn-stopp: sensoren henger på pipa over vedovnen ──
STOVE_DEFAULT = {"enabled": True, "off_above": 26}
STOVE_RESUME_BELOW = 1.0   # standard: auto tilbake når sensoren er 1 °C under grensen

def get_stove():
    st = {**STOVE_DEFAULT, **(cache.get("stove") or {})}
    # on_below = når pumpa slås på igjen; må ligge under avslagsgrensen
    if st.get("on_below") is None or st["on_below"] >= st["off_above"]:
        st["on_below"] = st["off_above"] - STOVE_RESUME_BELOW
    return st

def stove_check(uid, current):
    """True = vedovnen fyrer og pumpa skal stå av (auto gjør ingenting annet)."""
    st = get_stove()
    active = cache.get("stove_active")
    if not st["enabled"]:
        if active:
            stove_end(current, "vedovn-stopp slått av")
        return False
    if not active and current > st["off_above"]:
        cache["stove_active"] = True
        cache["stove_saved_offset"] = cache.get("hold_offset", 1)
        cache["boost_active"] = False
        cache.pop("auto_heat_start", None)
        cache.pop("esc", None)
        add_event("auto", f"Vedovnen fyrer ({current:.1f}°C over ovnen > {st['off_above']}°C) — slår av pumpa")
        save_state()
    elif active and current < st["on_below"]:
        stove_end(current, f"{current:.1f}°C over ovnen")
        return False
    if cache.get("stove_active"):
        ac = cache.get("ac", {})
        if ac.get("on") is not False:
            if sensibo_patch(uid, {"on": False}, "Vedovn-stopp"):
                cache["ac"]["on"] = False
        return True
    return False

def stove_end(current, why):
    cache["stove_active"] = False
    # Påslaget var frosset — sett tilbake det som gjaldt før ovnen ble fyrt
    if cache.get("stove_saved_offset") is not None:
        cache["hold_offset"] = cache.pop("stove_saved_offset")
    # Pipa er fortsatt varm en stund: ingen justering av påslaget før om 1,5 t
    cache["hold_adjusted_at"] = time.time() + 3600
    add_event("auto", f"Vedovnen har roet seg ({why}) — auto tar over igjen")
    save_state()

def auto_feedback_check():
    """
    1. Oppvarming: romtemp < mål − hysteresis → 30 °C + høy vifte, til rommet er mål − 0,3.
    2. Holde:      pumpa på mål + påslag (selvjusterende) + normalvifte.
    Sender kun kommando når innstillingen faktisk må endres.
    """
    if not cache["auto_enabled"]:
        return
    current_room = cache.get("temperature")
    uid0 = get_device_uid()
    if current_room is not None and uid0 and stove_check(uid0, current_room):
        return
    if cache.get("boost_active"):
        return  # Boost kjører — auto pauset
    pause = cache.get("external_pause_until")
    if pause:
        if time.time() < pause:
            return
        cache["external_pause_until"] = None
        add_event("auto", f"Auto fortsetter etter {EXTERNAL_PAUSE_HOURS} t pause")
    current = cache.get("temperature")
    if current is None:
        return

    target = cache["auto_target_temp"]
    hysteresis = cache.get("hysteresis", 1.0)
    ac = cache.get("ac", {})
    normal_fan = cache.get("normal_fan", NORMAL_FAN)
    uid = get_device_uid()
    if not uid:
        return

    staged = get_staged()
    morning = cache.get("morning_full")
    if morning and current >= target - HEAT_STOP_BELOW:
        cache.pop("morning_full", None)  # varm nok — morgenen er ferdig, vanlig styring
        morning = False
    elif morning and not _is_heating(ac) and current > target - get_night()["morning_full_below"]:
        # Lite som mangler (stua sank knapt i natt): trinnene klarer det uten høy vifte
        cache.pop("morning_full", None)
        morning = False
        add_event("auto", f"Ingen full gass: rommet {current:.1f}°C er under {get_night()['morning_full_below']:g}° fra målet {target:g}°C — trinnvis i stedet")
    if staged["enabled"] and not morning:
        if staged_check(uid, current, target, staged, ac, normal_fan):
            return
        hysteresis = staged["full_below"]  # full trøkk med en gang bare når rommet er langt under
    if morning:
        hysteresis = HEAT_STOP_BELOW      # etter natta: full gass helt til nesten varmt

    heating = _is_heating(ac)
    rate = room_rate()
    braking = heating and rate is not None and rate >= D_RISE_FULL and current >= target - D_BRAKE_WITHIN
    if braking:
        cache.pop("morning_full", None)
        add_event("auto", f"Bremser full gass: rommet stiger {rate:.1f}°C/t ved {current:.1f}°C")
    if (current < target - hysteresis or (heating and current < target - HEAT_STOP_BELOW)) and not braking:
        if heating:
            return
        # NB: påslaget økes ikke her — oppvarming skyldes ofte åpen dør, ikke for lavt hold.
        # Holdet justeres bare av den langsomme regulatoren under.
        payload = {"on": True, "mode": "heat", "targetTemperature": 30, "fanLevel": "high"}
        if sensibo_patch(uid, payload, f"Auto oppvarming (rom {current:.1f}°C, mål {target:g}°C)"):
            cache["ac"].update(payload)
            cache["full_heat"] = True
            cache["auto_heat_start"] = {"temp": current, "time": time.time(), "outdoor": cache.get("outdoor_temp")}
            log.info(f"Auto OPPVARMING: {current:.1f}°C < {target - hysteresis:.1f}°C | 30°C + høy vifte")
        return

    log_auto_heat_session(current)
    # HOLDE — juster påslaget sakte etter hvordan rommet faktisk ligger
    offset = cache.get("hold_offset", 1)
    now = time.time()
    def _hold_fan(off):
        if staged["enabled"] and cache.get("hold_fan"):
            return cache["hold_fan"]
        return staged["fan"] if (staged["enabled"] and off > STAGED_FAN_ABOVE) else normal_fan
    if staged["enabled"] and not heating:
        learned = hold_learning(current, target, offset, heating, _hold_fan(offset))
        if learned is not None:
            offset, lfan = learned
            cache["hold_offset"] = offset
            cache["hold_fan"] = lfan if lfan else None
            cache["hold_adjusted_at"] = now
            save_state()
    # Trinnvis modus: huset avgir restvarme etter oppvarming — trapp ned roligere (1 t mellom hvert hakk)
    adjust_every = HOLD_ADJUST_EVERY * (2 if staged["enabled"] else 1)
    too_warm = staged["enabled"] and current > target + 1.0
    if staged["enabled"] and current > target + 0.5:
        adjust_every = HOLD_ADJUST_EVERY  # godt over målet: ned hver halvtime, ikke hver time
    if too_warm:
        adjust_every = HOLD_ADJUST_EVERY  # tydelig for varmt (væromslag o.l.): raskere ned
    rate = room_rate()
    rising_fast = staged["enabled"] and rate is not None and rate >= D_RISE and current > target + STAGED_DONE_BELOW
    if rising_fast:
        adjust_every = 15 * 60  # D: over målet og stiger fort — vent ikke en time
    if staged["enabled"] and current > target + HOLD_HIGH and rate is not None and rate >= D_RISING:
        adjust_every = 10 * 60  # over målet og stiger fortsatt: ett pip ekstra er bedre enn 1° for varmt
    if offset < min_off():
        cache["hold_offset"] = offset = min_off()  # aldri under «ønsket + X»
    if not heating and now - cache.get("hold_adjusted_at", 0) >= adjust_every:
        new = offset
        if current < target - HOLD_LOW and not staged["enabled"]:
            new = min(HOLD_OFFSET_MAX, offset + 1)
        elif current > target + HOLD_HIGH or rising_fast:
            floor = current_floor() if staged["enabled"] else HOLD_OFFSET_MIN
            if offset - 1 < floor and current > target + FLOOR_RELEASE_ABOVE:
                floor = offset - 1  # tydelig for varmt: gi opp gulvet ett hakk
                cache["hold_floor"], cache["hold_floor_at"] = floor, now
            new = max(min_off(), floor, offset - (2 if too_warm or (rising_fast and current > target + HOLD_HIGH) else 1))
            if staged["enabled"] and cache.get("hold_fan") == "high":
                # For varmt med høy vifte: senk viften først (stillere), temperaturen etterpå
                cache["hold_fan"] = staged["fan"]
                cache["hold_adjusted_at"] = now
                new = offset
        if new != offset:
            cache["hold_offset"] = offset = new
            cache["hold_adjusted_at"] = now
            save_state()
    wanted_temp = min(30, int(round(target + offset)))
    if staged["enabled"] and cache.get("hold_fan") and cache["hold_fan"] != "high" and offset <= STAGED_FAN_ABOVE:
        cache["hold_fan"] = None  # nede på lavt hold igjen: normalvifte
    hold_fan = _hold_fan(offset)
    if ac.get("on") and ac.get("targetTemperature") == wanted_temp and ac.get("fanLevel") == hold_fan:
        return
    payload = {"on": True, "mode": "heat", "targetTemperature": wanted_temp, "fanLevel": hold_fan}
    if sensibo_patch(uid, payload, f"Auto holder (rom {current:.1f}°C, mål {target:g}°C)"):
        if _is_heating(ac):
            cache["full_heat_ended_at"] = now  # rommet svinger litt etter full gass
        cache["ac"].update(payload)
        cache["full_heat"] = False
        cache["hold_adjusted_at"] = now
        log.info(f"Auto HOLDER: rom {current:.1f}°C, mål {target}°C | pumpe {wanted_temp}°C (påslag {offset:+d})")

# ── Lært hold per utetemperatur: hva pumpa må stå på for å holde ønsket temp ──
HL_BUCKET = 2             # utetemp grupperes i trinn på 2 °C
HL_STABLE_SECONDS = 3600  # én time stabilt rundt ønsket = én måling
HL_STABLE_BAND = 0.3
HL_STABLE_DRIFT = 0.2     # rommet må ligge flatt (±0,2) gjennom timen — ikke kjøle seg ned fra overskudd
HL_APPLY_EVERY = 3600     # bytt til lært innstilling høyst én gang i timen (hver kommando piper)
HL_MAX_REACH = 3          # uten måling på begge sider: bruk bare lært verdi innen 3 °C fra der den er målt

def hl_bucket(out):
    return int(round(out / HL_BUCKET) * HL_BUCKET)

def hl_record(out, offset, kind, fan=None):
    """kind «stable»: denne kombinasjonen (temp + vifte) holdt. «fail»: den holdt ikke — trenger minst ett hakk mer."""
    if out is None:
        return
    hl = cache.setdefault("hold_learn", {})
    b = str(hl_bucket(out))
    e = hl.get(b)
    e = dict(e or {})
    if kind == "stable":
        e["v"] = round(offset if e.get("v") is None else 0.7 * e["v"] + 0.3 * offset, 2)
        if fan:
            e["fan"] = fan
        e["n"] = e.get("n", 0) + 1
        if e.get("floor") is not None and e["floor"] > e["v"] + 0.5:
            e["floor"] = int(round(e["v"]))  # holdt stabilt lavere enn gulvet: gulvet var for strengt
    else:
        e["floor"] = max(e.get("floor", -99), offset + 1)  # bare et gulv: «trenger minst dette»
    e["at"] = datetime.now(LOCAL_TZ).isoformat(timespec="minutes")
    hl[b] = e
    save_state()

def hl_predict(out):
    """Lært påslag ved denne utetemp (lineært mellom nærmeste trinn med data). None uten data."""
    hl = cache.get("hold_learn") or {}
    if out is None or not hl:
        return None
    pts = sorted((int(k), e["v"], e.get("fan")) for k, e in hl.items() if e.get("v") is not None)
    if not pts:
        return None
    below = [p for p in pts if p[0] <= out]
    above = [p for p in pts if p[0] >= out]
    nearest = min(pts, key=lambda p: abs(p[0] - out))  # viften følger nærmeste måling
    if below and above:
        (x0, y0, _), (x1, y1, _) = below[-1], above[0]
        v = y0 if x1 == x0 else y0 + (y1 - y0) * (out - x0) / (x1 - x0)
    else:
        edge = below[-1] if below else above[0]
        if abs(edge[0] - out) > HL_MAX_REACH:
            return None  # for langt unna det vi har lært — ikke gjett
        v = edge[1]
    floor = (hl.get(str(hl_bucket(out))) or {}).get("floor")
    if floor is not None:
        v = max(v, floor)
    return v, nearest[2]

def hold_learning(current, target, offset, heating, fan):
    """Kalles i holdet (trinnvis modus). Måler hva som holder (temp + vifte), og hopper rett dit
    når været skifter. Returnerer evt. (nytt påslag, vifte)."""
    now, out = time.time(), cache.get("outdoor_temp")
    # 1) Måling: en time stabilt rundt ønsket på samme innstilling
    if abs(current - target) <= HL_STABLE_BAND:
        cache["hl_in_band_at"], cache["hl_in_band_target"] = now, target
    if not heating and abs(current - target) <= HL_STABLE_BAND:
        if cache.get("hl_stable_offset") != (offset, fan) or not cache.get("hl_stable_since"):
            cache["hl_stable_since"], cache["hl_stable_offset"] = now, (offset, fan)
            cache["hl_stable_room"] = current
        elif now - cache["hl_stable_since"] >= HL_STABLE_SECONDS:
            if abs(current - cache.get("hl_stable_room", current)) <= HL_STABLE_DRIFT:
                hl_record(out, offset, "stable", fan)
            cache["hl_stable_since"], cache["hl_stable_room"] = now, current
    else:
        cache["hl_stable_since"] = None
    # 2) Bruk: gå til lært innstilling når den skiller seg tydelig fra dagens
    res = hl_predict(out)
    if res is None or now - cache.get("hl_applied_at", 0) < HL_APPLY_EVERY:
        return None
    pred, lfan = res
    # Bare når været skifter (nytt utetemp-trinn): ellers kjempet læringen mot nedtrappingen
    # (Frank 29.10 natt: 22→24→22→24→22 uten at været endret seg). Holdregulatoren tar finjusteringen.
    bucket = hl_bucket(out)
    if "hl_applied_bucket" not in cache:
        cache["hl_applied_bucket"] = bucket  # første gang: dagens vær er utgangspunktet, ikke et skifte
    if cache.get("hl_applied_bucket") == bucket:
        return None
    want = max(min_off(), min(int(30 - target), int(round(pred))))
    fan_change = bool(lfan) and lfan != fan and (lfan == "high" or fan == "high")
    if (want == offset or abs(pred - offset) < 0.75) and not fan_change:
        return None
    # Ta hensyn til innetemp nå: ikke skru opp i et varmt rom, ikke ned i et kaldt
    if want > offset and current >= target - STAGED_DONE_BELOW:
        return None  # aldri opp når rommet allerede er på eller over målet
    if want < offset and current < target - HL_STABLE_BAND:
        return None
    cache["hl_applied_at"], cache["hl_applied_bucket"] = now, bucket
    cache["hold_floor"], cache["hold_floor_at"] = want, now  # ikke trapp ned forbi det som har holdt ved denne utetemp
    add_event("auto", f"Lært: ved {out:.0f}°C ute holder pumpa ønsket temp på ca. {target + want:g}°C"
                      f"{' · ' + FAN_NO.get(lfan, lfan) + ' vifte' if lfan else ''} — justerer fra {target + offset:g}°C")
    return want, lfan

HOLD_FLOOR_TTL = 6 * 3600   # et gulv gjelder i 6 t; det varige ligger i lært tabell per utetemp

def current_floor():
    if time.time() - cache.get("hold_floor_at", 0) > HOLD_FLOOR_TTL:
        return HOLD_OFFSET_MIN
    return cache.get("hold_floor", HOLD_OFFSET_MIN)

def _step_fan(ac, st):
    """Trinn bruker fast rolig vifte — men går aldri NED fra høy (lært: høy kan være riktig ved kulde)."""
    return "high" if ac.get("fanLevel") == "high" else st["fan"]

def staged_check(uid, current, target, st, ac, normal_fan):
    """Trinnvis oppvarming. True = trinnene styrer pumpa nå (resten av auto hopper over)."""
    esc = cache.get("esc")
    now = time.time()
    handover = False
    if not esc and _is_heating(ac) and not cache.get("morning_full") and current >= target - st["full_below"]:
        # Full trøkk uten god grunn (ikke morgen, ikke langt under) — gå over til rolige trinn
        cache["full_heat"] = False
        handover = True
    elif not esc and (current < target - st["full_below"] or _is_heating(ac)):
        return False  # langt under / morgen på full gass: la den gjøre seg ferdig

    rate = room_rate()
    if esc:
        braking = rate is not None and rate >= D_RISE and current >= target - D_BRAKE_WITHIN
        if current >= target - STAGED_DONE_BELOW or braking:
            # Nådd: hold på innstillingen som virket (pumpa kan trenge f.eks. 27° for å holde 23°).
            # Den trappes ned bare når rommet blir for varmt (holdregulatoren, −1 per halvtime).
            cache.pop("esc", None)
            set_t = ac.get("targetTemperature") or target
            hold_t = set_t
            hold_fan = ac.get("fanLevel")  # behold viften som virket (også høy, f.eks. ved −10)
            quick = now - esc["last_step"] < QUICK_REACH and set_t > target + min_off()
            if (rate is not None and rate >= D_RISE) or quick:
                # Mye varme på vei / målet nådd raskt etter siste trinn: hold ett hakk under
                hold_t = set_t - 1
                if rate is not None and rate >= D_RISE:
                    # Stiger fort: rett ned til det som holder ved denne utetemp (lært), ellers
                    # holdet fra før trinnene — ett hakk ned ga 1° overskyting (Vigdis 28.10: 27 → 25,0°C)
                    pred = hl_predict(cache.get("outdoor_temp"))
                    base = int(round(pred[0])) if pred else int(cache.get("hold_offset", 0))
                    # Ett hakk over lært hold når vi er like under målet — ellers sank rommet og trinnene startet igjen
                    hold_t = max(int(round(target)) + min_off(), min(hold_t, int(round(target + base)) + (1 if current < target else 0)))
                if hold_fan == "high":
                    hold_fan = st["fan"]
                add_event("auto", (f"Bremser: rommet stiger {rate:.1f}°C/t" if rate is not None and rate >= D_RISE
                                   else f"Nådd på {(now - esc['last_step']) / 60:.0f} min — trinnet var stort")
                                  + f" ved {current:.1f}°C — holder {hold_t}°C, ikke {set_t}°C")
            cache["hold_offset"] = max(min_off(), min(int(30 - target), int(round(hold_t - target))))
            cache["hold_fan"] = hold_fan
            cache["full_heat"] = False
            cache["hl_applied_at"] = now  # ferskt bevis — ikke overstyr med lært verdi den neste timen
            cache["hold_adjusted_at"] = now
            save_state()
            log_auto_heat_session(current)
            add_event("auto", f"Trinnvis oppvarming ferdig: rommet {current:.1f}°C etter "
                              f"{(now - esc['start']) / 60:.0f} min (nådde {set_t}°C · {FAN_NO.get(ac.get('fanLevel'), ac.get('fanLevel'))} vifte)")
            return False  # holdet tar over med samme temp og vifte
        wait = st["every_min"] * 60
        if rate is not None and rate <= D_FALL:
            wait //= 2  # faller fort: ta neste trinn etter halv tid
        rising = rate is not None and rate >= D_RISING
        if now - esc["last_step"] < wait or current >= esc["room_at_step"] + STAGED_RISING or rising:
            if current >= esc["room_at_step"] + STAGED_RISING:
                # Det virker: nullstill klokka og referansen, ikke trapp opp
                esc.update(last_step=now, room_at_step=current)
            return True
        set_t = ac.get("targetTemperature") or target
        if set_t < 30:
            new_t, fan, why = min(30, set_t + st["step"]), _step_fan(ac, st), f"+{st['step']}°"
        elif ac.get("fanLevel") != "high":
            new_t, fan, why = 30, "high", "høy vifte (siste trinn)"
        else:
            return True  # alt er maks — vent
    else:
        falling_fast = rate is not None and rate <= D_FALL and current < target - STAGED_DONE_BELOW
        if current >= target - STAGED_START_BELOW and not falling_fast:
            return False  # nær målet (også ved overgang fra full trøkk): holdet tar over
        if (not handover and not falling_fast and now - cache.get("full_heat_ended_at", 0) < AFTER_FULL_GRACE
                and current >= target - 1.0):
            return False  # rett etter full gass: gi holdet tid før vi trapper opp
        if rate is not None and rate >= D_RISING and not handover:
            return False  # rommet er allerede på vei opp (f.eks. etter full gass) — ikke start trinn
        set_t = ac.get("targetTemperature") or int(round(target))
        if handover:
            set_t = int(round(target + cache.get("hold_offset", 0)))  # start trinnene fra holdet, ikke fra 30
            ac = {**ac, "fanLevel": st["fan"]}                          # og med rolig vifte, ikke høy
        new_t, fan, why = min(30, max(set_t, int(round(target))) + st["step"]), _step_fan(ac, st), f"+{st['step']}°"
        # Kjent vær: start rett på kombinasjonen som har holdt før (f.eks. 27° + høy ved −10)
        res = hl_predict(cache.get("outdoor_temp"))
        if res is not None:
            l_t = min(30, int(round(target + res[0])))
            if l_t > new_t or (res[1] == "high" and fan != "high" and l_t >= new_t):
                new_t, why = max(new_t, l_t), f"lært for {cache.get('outdoor_temp'):.0f}°C ute"
                if res[1] == "high":
                    fan = "high"
        # Holdet vi hadde var ikke nok — neste gang trappes det ikke ned forbi ett hakk over dette.
        # Bare ved ekte svikt: rommet lå rundt SAMME ønskede temp nylig (ikke morgenhopp, ikke trinn på vei opp)
        real_fail = (ac.get("on") and set_t < 30 and cache.get("hl_in_band_target") == target
                     and now - cache.get("hl_in_band_at", 0) < HL_STABLE_SECONDS)
        if real_fail:
            hl_record(cache.get("outdoor_temp"), int(set_t - target), "fail")
            floor = int(set_t - target) + 1
            if floor > current_floor():
                cache["hold_floor"], cache["hold_floor_at"] = floor, now
        esc = {"start": now, "last_step": now, "room_at_step": current}
        cache["esc"] = esc
        cache["auto_heat_start"] = {"temp": current, "time": now, "outdoor": cache.get("outdoor_temp")}

    payload = {"on": True, "mode": "heat", "targetTemperature": int(new_t), "fanLevel": fan}
    if sensibo_patch(uid, payload, f"Trinnvis oppvarming {why} (rom {current:.1f}°C, mål {target:g}°C)"):
        cache["ac"].update(payload)
        cache["full_heat"] = False
    esc.update(last_step=now, room_at_step=current)
    save_state()  # trinnene overlever omstart
    return True

# ============================================================
# MÅL ETTER UTETEMPERATUR
# ============================================================
# warm_from: «varmt» fra og med denne utetemp; cold_below: «kaldt» under denne. Gapet hindrer flipping.
OUTDOOR_RULE_DEFAULT = {"enabled": True, "warm_from": 5.0, "cold_below": 4.5, "warm_temp": 23.0, "cold_temp": 24.0}

def get_outdoor_rule():
    r = {**OUTDOOR_RULE_DEFAULT, **(cache.get("outdoor_rule") or {})}
    if "threshold" in r:  # eldre format: én terskel + fast 0,5 bånd
        r.setdefault("warm_from", r["threshold"])
        r["warm_from"] = r.pop("threshold")
        r["cold_below"] = min(r.get("cold_below", r["warm_from"] - 0.5), r["warm_from"])
    return r

# Nattsenking: eget mål om natta; om morgenen går målet tilbake til dagregelen (utetemp).
# weekend_start gjelder natt til lørdag og søndag (fredag/lørdag kveld).
NIGHT_DEFAULT = {"enabled": True, "start": "23:00", "weekend_start": "00:30", "end": "09:00", "temp": 20.0, "preheat": True,
                 "cold_skip": False, "cold_skip_below": -5,   # ingen nattsenking når det er kaldere enn dette ute
                 "morning_full": True,                         # full gass (30° + høy) fra natt til dag
                 "morning_full_below": 1.0}                    # …bare når stua er minst så mye under dagmålet
COLD_SKIP_BAND = 1.0  # nattsenking tilbake først når det er 1 °C over grensen (hindrer flipping)

def get_night():
    return {**NIGHT_DEFAULT, **(cache.get("night") or {})}

def _hm(txt):
    h, m = map(int, str(txt).split(":"))
    return h, m

def in_night(now, n):
    """Er vi i nattperioden? Natta «tilhører» kvelden den startet (før kl. 12 = gårsdagens kveld)."""
    return night_window(now, n)[0]

def night_window(now, n):
    """(i_natt, slutt) for natta som gjelder nå."""
    evening = (now - timedelta(days=1)).date() if now.hour < 12 else now.date()
    weekend = evening.weekday() in (4, 5)  # fredag/lørdag kveld
    sh, sm = _hm(n["weekend_start"] if weekend else n["start"])
    eh, em = _hm(n["end"])
    start_day = evening + timedelta(days=1) if sh < 12 else evening
    start = datetime(start_day.year, start_day.month, start_day.day, sh, sm, tzinfo=LOCAL_TZ)
    nxt = evening + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, eh, em, tzinfo=LOCAL_TZ)
    return start <= now < end, end

# ── Forvarming: læringsmotoren bestemmer når morgenoppvarmingen må starte ──
PREHEAT_FALLBACK_MIN_PER_DEG = 30   # før vi har egne data
PREHEAT_MARGIN = 1.15                # 15 % slingringsmonn
PREHEAT_MAX_MIN = 180
PREHEAT_OUTDOOR_WINDOW = 5           # bruk sesjoner med utetemp innenfor ±5 °C

def learned_min_per_deg(outdoor):
    """Median min/°C fra de 10 siste oppvarmingene med lignende utetemp (ellers alle). (verdi, antall)"""
    hist = load_history()
    similar = [h for h in hist if outdoor is not None and h.get("outdoor_temp") not in (None, "")
               and abs(float(h["outdoor_temp"]) - outdoor) <= PREHEAT_OUTDOOR_WINDOW]
    pool = (similar or hist)[-10:]
    vals = sorted(float(h["min_per_deg"]) for h in pool if float(h.get("min_per_deg") or 0) > 0)
    if not vals:
        return PREHEAT_FALLBACK_MIN_PER_DEG, 0, False
    mid = len(vals) // 2
    med = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    return med, len(vals), bool(similar)

def preheat_plan(day_temp, end):
    """Når må oppvarmingen starte for å nå day_temp til end? None hvis det ikke trengs."""
    room, out = cache.get("temperature"), cache.get("outdoor_temp")
    if room is None or day_temp is None:
        return None
    delta = day_temp - room
    if delta <= 0.2:
        return None
    mpd, n, similar = learned_min_per_deg(out)
    minutes = min(PREHEAT_MAX_MIN, round(delta * mpd * PREHEAT_MARGIN))
    return {"start": end - timedelta(minutes=minutes), "ready": end, "minutes": minutes,
            "delta": round(delta, 1), "min_per_deg": round(mpd, 1), "sessions": n, "similar": similar}

def make_standard(new_t):
    """Gjør en manuell endring av ønsket temp til ny standard for perioden som gjelder nå."""
    mode = cache.get("target_mode")
    if mode == "away":
        cache["away"] = {**get_away(), "temp": new_t}
        add_event("setting", f"Ny temp for tomt hus: {new_t:g}°C")
    elif mode == "night":
        cache["night"] = {**get_night(), "temp": new_t}
        add_event("setting", f"Ny standard natt: {new_t:g}°C")
    elif mode in ("warm", "cold"):
        rule = get_outdoor_rule()
        delta = new_t - (rule["warm_temp"] if mode == "warm" else rule["cold_temp"])
        if delta:
            rule["warm_temp"] = max(16, min(30, rule["warm_temp"] + delta))
            rule["cold_temp"] = max(16, min(30, rule["cold_temp"] + delta))
            cache["outdoor_rule"] = rule
            add_event("setting", f"Ny standard dag: {rule['warm_temp']:g}°C (mildt) / {rule['cold_temp']:g}°C (kaldt)")
    else:
        cache["day_target"] = new_t
        add_event("setting", f"Ny standard dag: {new_t:g}°C")

# ── Tomt hus (utleie/fravær): lav temp hele døgnet, varm opp i god tid før folk kommer ──
AWAY_DEFAULT = {"temp": 21.0, "lead_hours": 24, "periods": []}
# periode: {"from": "YYYY-MM-DD HH:MM" (huset tomt), "to": "YYYY-MM-DD HH:MM" (folk kommer)}

def get_away():
    a = {**AWAY_DEFAULT, **(cache.get("away") or {})}
    a["periods"] = list(a.get("periods") or [])
    return a

def _away_dt(txt):
    return datetime.strptime(str(txt), "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)

def away_state(now):
    """('away', periode) mens huset står tomt, ('warmup', periode) fra oppvarmingen starter til
    folk kommer, ellers (None, None). Oppvarmingen starter lead_hours før ankomst."""
    a = get_away()
    for p in a["periods"]:
        try:
            start, arrive = _away_dt(p["from"]), _away_dt(p["to"])
        except (KeyError, ValueError):
            continue
        warm = max(start, arrive - timedelta(hours=a["lead_hours"]))
        if start <= now < warm:
            return "away", p
        if warm <= now < arrive:
            return "warmup", p
    return None, None

def away_prune():
    """Fjern perioder der folk har kommet for over et døgn siden."""
    a = get_away()
    cut = datetime.now(LOCAL_TZ) - timedelta(days=1)
    keep = [p for p in a["periods"] if _safe_after(p.get("to"), cut)]
    if len(keep) != len(a["periods"]):
        cache["away"] = {**a, "periods": keep}

def _safe_after(txt, cut):
    try:
        return _away_dt(txt) >= cut
    except (TypeError, ValueError):
        return False

def outdoor_rule_check(force=False):
    """Bestemmer ønsket temp: nattsenking om natta, ellers mål etter utetemperatur.
    Ett utslag per skifte (natt↔dag, mild↔kaldt), så timeplan/manuelle endringer står seg til neste skifte."""
    rule, night = get_outdoor_rule(), get_night()
    out = cache.get("outdoor_temp")

    # Hold mild/kaldt oppdatert også om natta, så morgenen starter med riktig mål
    bracket = cache.get("outdoor_bracket")
    if rule["enabled"] and out is not None:
        if bracket == "cold":
            bracket = "warm" if out >= rule["warm_from"] else "cold"
        else:
            bracket = "cold" if out < rule["cold_below"] else "warm"
        cache["outdoor_bracket"] = bracket

    now = datetime.now(LOCAL_TZ)
    is_night, night_end = night_window(now, night) if night["enabled"] else (False, None)
    away, away_p = away_state(now)
    if away:
        is_night = False   # tomt hus: ingen nattsenking; oppvarming før ankomst: heller ikke

    # Kaldt ute: hopp over nattsenkingen (huset bruker lang tid på å ta igjen varmen)
    skip = cache.get("night_cold_skip", False)
    if night.get("cold_skip") and out is not None:
        lim = night["cold_skip_below"]
        new_skip = out < lim if not skip else out < lim + COLD_SKIP_BAND
        if new_skip != skip and is_night:
            if new_skip:
                cache["cold_skip_logged_for"] = night_end.isoformat()
            add_event("auto", f"Ute {out:.1f}°C — {'ingen nattsenking i natt (kaldere enn ' + format(lim, 'g') + '°C)' if new_skip else 'nattsenking igjen'}")
        skip = new_skip
    else:
        skip = False
    cache["night_cold_skip"] = skip
    if skip and is_night and cache.get("cold_skip_logged_for") != night_end.isoformat():
        cache["cold_skip_logged_for"] = night_end.isoformat()
        add_event("auto", f"Ute {out:.1f}°C — ingen nattsenking i natt (kaldere enn {night['cold_skip_below']:g}°C)")
    if skip:
        is_night = False
    cache["preheat"] = None
    if not away and is_night and night.get("preheat", True):
        # Har forvarmingen allerede startet for denne natta, står den (ikke tilbake til natt)
        if cache.get("preheat_for") == night_end.isoformat():
            is_night = False
        else:
            day_temp = (rule["warm_temp"] if bracket == "warm" else rule["cold_temp"]) if rule["enabled"] \
                       else (cache.get("day_target") or cache.get("auto_target_temp"))
            plan = preheat_plan(day_temp, night_end)
            if plan:
                cache["preheat"] = {**plan, "start": plan["start"].isoformat(), "ready": plan["ready"].isoformat()}
                if now >= plan["start"]:
                    cache["preheat_for"] = night_end.isoformat()
                    is_night = False
                    src = (f"{plan['min_per_deg']:g} min/°C fra {plan['sessions']} oppvarminger"
                           + ("" if plan["similar"] else " (ingen med lignende utetemp ennå)")) if plan["sessions"] \
                          else f"standard {PREHEAT_FALLBACK_MIN_PER_DEG} min/°C (ingen data ennå)"
                    add_event("auto", f"Forvarming startet: {cache.get('temperature'):.1f} → {day_temp:g}°C på ~{plan['minutes']} min "
                                      f"({src}), varmt kl. {night_end:%H:%M}")
    mode = "away" if away == "away" else ("night" if is_night else (bracket if rule["enabled"] else "day"))
    prev = cache.get("target_mode")
    if mode == prev and not force:
        return
    cache["target_mode"] = mode
    if prev in ("night", "away") and mode not in ("night", "away") and (prev == "away" or night.get("morning_full", True)):
        cache["morning_full"] = True   # varm opp etter natt/tomt hus på full gass (rask), så trinnvis
    elif mode in ("night", "away"):
        cache.pop("morning_full", None)

    if mode == "away":
        if prev not in ("night", "away"):
            cache["day_target"] = cache.get("auto_target_temp")
        temp = get_away()["temp"]
        why = f"Tomt hus til {_away_dt(away_p['to']):%d.%m kl. %H:%M}"
    elif mode == "night":
        if prev != "night":
            cache["day_target"] = cache.get("auto_target_temp")  # for dag uten utetemp-regel
        temp, why = night["temp"], "Nattsenking"
    elif mode in ("warm", "cold"):
        temp = rule["warm_temp"] if mode == "warm" else rule["cold_temp"]
        why = ("Oppvarming før ankomst: " if prev == "away" else "Dag: " if prev == "night" else "") \
              + (f"Ute {out:.1f}°C" if out is not None else "Dag")
    else:  # dag uten utetemp-regel: tilbake til det som gjaldt før natta
        temp, why = cache.get("day_target") or cache.get("auto_target_temp"), ("Oppvarming før ankomst" if prev == "away" else "Dag")
        if prev not in ("night", "away"):
            save_state()
            return
    if temp is not None and cache.get("auto_target_temp") != temp:
        cache["auto_target_temp"] = temp
        add_event("setting", f"{why} → ønsket temp {temp:g}°C")
        cache["last_sensibo"] = 0
    save_state()

# ============================================================
# BACKGROUND REFRESH
# ============================================================
# ============================================================
# TIMEPLAN
# ============================================================
LOCAL_TZ = ZoneInfo("Europe/Oslo")  # VPS-en går på UTC; timeplanen er i norsk tid

def latest_schedule_event(now):
    """Siste timeplan-punkt som har passert (søker en uke bakover). Dager: 0 = mandag."""
    best = None
    for s in cache.get("schedules") or []:
        try:
            hh, mm = map(int, str(s["time"]).split(":"))
            temp = float(s["temp"])
        except (KeyError, ValueError):
            continue
        days = s.get("days") or list(range(7))
        for back in range(8):
            d = (now - timedelta(days=back)).date()
            if d.weekday() not in days:
                continue
            at = datetime(d.year, d.month, d.day, hh, mm, tzinfo=LOCAL_TZ)
            if at <= now:
                if best is None or at > best[0]:
                    best = (at, temp)
                break
    return best

def schedule_check():
    """Setter ønsket temp når et nytt timeplan-punkt passeres — én gang per punkt,
    så manuelle endringer i appen står seg til neste punkt."""
    ev = latest_schedule_event(datetime.now(LOCAL_TZ))
    if ev is None or cache.get("target_mode") == "away":
        return
    at, temp = ev
    key = at.isoformat()
    if cache.get("last_schedule_key") is None:
        # Første kjøring: ikke overstyr det som er satt nå, bare marker punktet som brukt
        cache["last_schedule_key"] = key
        save_state()
        return
    if key == cache["last_schedule_key"]:
        return
    cache["last_schedule_key"] = key
    cache["auto_target_temp"] = max(16, min(30, temp))
    save_state()
    log.info(f"Timeplan {at:%a %H:%M}: ønsket temp → {cache['auto_target_temp']}°C")
    add_event("schedule", f"Timeplan {at:%H:%M}: ønsket temp → {cache['auto_target_temp']:g}°C")
    cache["last_sensibo"] = 0  # hent status og la auto-styringen reagere med en gang

# ============================================================
# FILTERPÅMINNELSE
# ============================================================
FILTER_PUSH_HOUR = 9          # push sendes tidligst kl. 09 norsk tid
FILTER_PUSH_BEFORE = 2        # forvarsel 2 dager før
FILTER_REPEAT_DAYS = 3        # deretter hver 3. dag til filtrene er rengjort

def push(title, message, tags="", priority=3):
    if not (NTFY_URL and NTFY_TOPIC):
        return False
    try:
        headers = {"Title": title.encode("utf-8"), "Tags": tags, "Priority": str(priority)}
        if NTFY_TOKEN:
            headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
        r = requests.post(f"{NTFY_URL}/{NTFY_TOPIC}", data=message.encode("utf-8"), headers=headers, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning(f"Push feilet: {getattr(getattr(e, 'response', None), 'status_code', type(e).__name__)}")
        return False

def get_filter():
    f = {"interval_days": FILTER_DAYS_DEFAULT, "last_cleaned": None, **(cache.get("filter") or {})}
    if not f["last_cleaned"]:  # første gang: nedtellingen starter i dag
        f["last_cleaned"] = datetime.now(LOCAL_TZ).date().isoformat()
        cache["filter"] = f
        save_state()
    due = datetime.fromisoformat(f["last_cleaned"]).date() + timedelta(days=f["interval_days"])
    return {**f, "due": due.isoformat(), "days_left": (due - datetime.now(LOCAL_TZ).date()).days}

def filter_check():
    f = get_filter()
    now = datetime.now(LOCAL_TZ)
    today = now.date().isoformat()
    if now.hour < FILTER_PUSH_HOUR or cache.get("filter_push_day") == today:
        return
    left = f["days_left"]
    if left == FILTER_PUSH_BEFORE:
        msg = f"Filtrene i {ROOM_NAME} bør rengjøres om {left} dager."
    elif left == 0:
        msg = f"I dag er det {f['interval_days']} dager siden filtrene i {ROOM_NAME} ble rengjort. Trykk «Filtre rengjort» i varme-appen når det er gjort."
    elif left < 0 and (-left) % FILTER_REPEAT_DAYS == 0:
        msg = f"Filtrene i {ROOM_NAME} skulle vært rengjort for {-left} dager siden."
    else:
        return
    cache["filter_push_day"] = today
    save_state()
    ok = push(f"Rens filtrene — {ROOM_NAME}", msg, tags="broom", priority=4 if left < 0 else 3)
    add_event("setting", f"Filterpåminnelse {'sendt' if ok else 'IKKE sendt (push feilet)'}: {msg}")

def hourly_status():
    """Ett øyeblikksbilde i timen: rom, ute og hva pumpa står på."""
    hour = datetime.now(LOCAL_TZ).strftime("%Y-%m-%dT%H")
    if cache.get("last_status_hour") == hour or cache.get("temperature") is None:
        return
    cache["last_status_hour"] = hour
    if hour.endswith("T03"):
        try:
            init_series()  # nattlig opprydding: behold 35 dager
            away_prune()
        except Exception as e:
            log.error(f"Tidsserie-opprydding: {e}")
    room, out, ac = cache.get("temperature"), cache.get("outdoor_temp"), cache.get("ac", {})
    out_txt = f" · ute {out:.1f}°C" if out is not None else ""
    add_event("status", f"Rom {room:.1f}°C{out_txt} · pumpe {describe_ac(ac)}",
              room=room, outdoor=out, **{k: ac.get(k) for k in AC_KEYS})

def background_loop():
    while True:
        try:
            schedule_check()
        except Exception as e:
            log.error(f"Timeplan-feil: {e}")
        now = time.time()
        if now - cache["last_sensibo"] > SENSIBO_POLL_SECONDS:
            fetch_sensibo_state()
        if now - cache["last_metno"] > 1800:  # 30 min
            fetch_metno()
        if now - cache.get("last_sensor_poll", 0) > SENSOR_POLL_EVERY:
            fetch_outdoor_sensor()
        try:
            outdoor_rule_check()
        except Exception as e:
            log.error(f"Utetemp-regel-feil: {e}")
        try:
            filter_check()
        except Exception as e:
            log.error(f"Filter-feil: {e}")
        try:
            hourly_status()
        except Exception as e:
            log.error(f"Timestatus-feil: {e}")
        time.sleep(60)

# ============================================================
# HEAT HISTORY LOGGING
# ============================================================
def log_heat_session(entry: dict):
    write_header = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "timestamp", "from_temp", "to_temp", "outdoor_temp",
            "elapsed_min", "min_per_deg", "fan_speed"
        ])
        if write_header:
            writer.writeheader()
        writer.writerow(entry)
    log.info(f"Loggført varmeøkt: {entry}")

def load_history():
    if not os.path.exists(LOG_FILE):
        return []
    rows = []
    with open(LOG_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            # Kun ekte oppvarminger (temp opp) gir mening i læringen
            try:
                if float(r["to_temp"]) > float(r["from_temp"]) and float(r["elapsed_min"]) > 0:
                    rows.append(r)
            except (KeyError, TypeError, ValueError):
                continue
    return rows

# ============================================================
# PERSISTENT STATE
# ============================================================
STATE_KEYS = [
    "auto_enabled", "auto_target_temp", "hysteresis", "normal_fan", "boost_temp", "schedules",
    "last_schedule_key", "hold_offset", "outdoor_rule", "outdoor_bracket",
    "night", "target_mode", "day_target", "preheat_for", "away",
    "stove", "stove_active", "stove_saved_offset", "staged", "hold_floor", "hold_floor_at", "hold_adjusted_at", "hl_in_band_at", "hl_in_band_target", "hold_learn", "hold_fan", "esc", "full_heat", "hl_applied_at", "hl_applied_bucket", "morning_full", "full_heat_ended_at", "filter", "filter_push_day"
]

def save_state():
    """Lagrer alle innstillinger til disk."""
    try:
        state = {k: cache[k] for k in STATE_KEYS if k in cache}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        log.debug("State lagret til disk")
    except Exception as e:
        log.error(f"Kunne ikke lagre state: {e}")

def load_state():
    """Laster innstillinger fra disk ved oppstart."""
    if not os.path.exists(STATE_FILE):
        log.info("Ingen lagret state — bruker standardverdier")
        return
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        for k, v in state.items():
            if k in STATE_KEYS:
                cache[k] = v
        log.info(f"State lastet fra disk: target={cache['auto_target_temp']}°C, auto={'PÅ' if cache['auto_enabled'] else 'AV'}")
    except Exception as e:
        log.error(f"Kunne ikke laste state: {e}")

# ============================================================
# PREDICTION ENGINE
# ============================================================
def predict_heatup(target_temp, outdoor_temp):
    """
    Enkel prediksjon basert på historiske data.
    Returnerer estimert tid i minutter.
    """
    current = cache.get("temperature") or 20.0
    delta = target_temp - current
    if delta <= 0:
        return {"needed": False, "delta": delta}

    history = load_history()

    # Filtrer på lignende utetemperatur (±7 grader)
    relevant = [
        h for h in history
        if outdoor_temp is not None and h.get("outdoor_temp") not in (None, "")
        and abs(float(h["outdoor_temp"]) - outdoor_temp) <= 7
    ] if outdoor_temp else history

    if relevant:
        avg_min_per_deg = sum(float(h["min_per_deg"]) for h in relevant) / len(relevant)
        confidence = min(1.0, len(relevant) / 10)
    else:
        # Fallback: grovt estimat basert på utetemperatur
        if outdoor_temp is not None and outdoor_temp < -10:
            avg_min_per_deg = 12
        elif outdoor_temp is not None and outdoor_temp < 0:
            avg_min_per_deg = 9
        else:
            avg_min_per_deg = 6
        confidence = 0.1

    est_minutes = delta * avg_min_per_deg

    return {
        "needed": True,
        "current_temp": current,
        "target_temp": target_temp,
        "delta": delta,
        "estimated_minutes": round(est_minutes),
        "avg_min_per_deg": round(avg_min_per_deg, 2),
        "sessions_used": len(relevant),
        "confidence": round(confidence, 2)
    }

def calculate_optimal_settings(target_temp, outdoor_temp):
    """
    Beregner optimal vifte og temperaturinnstilling for raskest mulig oppvarming.
    """
    current = cache.get("temperature") or 20.0
    delta = target_temp - current

    if delta <= 0:
        return {"targetTemperature": target_temp, "fanLevel": "medium", "mode": "heat"}

    # Aggressivitet basert på delta og utetemperatur
    cold_penalty = max(0, -(outdoor_temp or 0)) * 0.1  # Ekstra trøkk ved kaldt ute

    if delta + cold_penalty > 4:
        # Stor delta eller veldig kaldt: full trøkk
        fan = "high"
        ac_temp = 30
    elif delta + cold_penalty > 2:
        # Middels delta
        fan = "medium_high"
        ac_temp = 28
    else:
        # Liten delta: hold nøyaktig
        fan = "medium"
        ac_temp = target_temp + 2

    return {
        "targetTemperature": min(30, ac_temp),
        "fanLevel": fan,
        "mode": "heat"  # ALLTID heat — cool er aldri tillatt
    }

# ============================================================
# HTTP SERVER
# ============================================================
class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Undertrykk standard HTTP-logg

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(data))
            # Alltid sjekk etter ny versjon — ellers viser iPhone-appen gamle sider etter deploy
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def proxy_room(self, method, body=None):
        """Videresender /r/<id>/... til stuens egen instans. Returnerer False hvis stien ikke er en stue-sti."""
        parts = self.path.split("/", 3)
        if len(parts) < 4 or parts[1] != "r":
            return False
        room = next((r for r in ROOMS if r["id"] == parts[2] and r.get("port")), None)
        if not room:
            self.send_json({"ok": False, "error": "Ukjent stue"}, 404)
            return True
        try:
            headers = {"Content-Type": "application/json"}
            if self.headers.get("X-Room"):
                headers["X-Room"] = self.headers["X-Room"]
            r = requests.request(method, f"http://127.0.0.1:{room['port']}/{parts[3]}", data=body,
                                 headers=headers, timeout=20)
            self.send_response(r.status_code)
            self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", len(r.content))
            self.end_headers()
            self.wfile.write(r.content)
        except Exception as e:
            log.warning(f"Stue {room['id']} svarer ikke: {type(e).__name__}")
            self.send_json({"ok": False, "error": f"{room['name']} svarer ikke"}, 502)
        return True

    def do_GET(self):
        if self.proxy_room("GET"):
            return
        path = urlparse(self.path).path

        if path == "/api/rooms":
            # Cloudflare Access legger innlogget e-post på hver forespørsel gjennom tunnelen
            email = (self.headers.get("Cf-Access-Authenticated-User-Email") or "").strip().lower()
            mine = next((r["id"] for r in ROOMS if email and email in [e.lower() for e in r.get("emails", [])]), None)
            default = mine or next((r["id"] for r in ROOMS if not r.get("port")), ROOMS[0]["id"])
            self.send_json({"rooms": [{"id": r["id"], "name": r["name"], "local": not r.get("port")} for r in ROOMS],
                            "default": default, "signed_in": bool(email)})

        elif path == "/" or path == "/index.html":
            self.serve_file("index.html", "text/html; charset=utf-8")

        elif path == "/api/status":
            pred = predict_heatup(
                cache["ac"].get("targetTemperature", 22),
                cache.get("outdoor_temp")
            )
            self.send_json({
                "temperature": cache.get("temperature"),
                "humidity": cache.get("humidity"),
                "outdoor_temp": cache.get("outdoor_temp"),
                "outdoor_forecast": cache.get("outdoor_forecast", [])[:6],
                "outdoor_source": cache.get("outdoor_source"),
                "metno_temp": cache.get("metno_temp"),
                "outdoor_place": METNO_PLACE,
                "outdoor_has_sensor": bool(HA_URL and HA_TOKEN and HA_OUTDOOR_ENTITY),
                "ac": cache.get("ac", {}),
                "prediction": pred,
                "history_count": len(load_history()),
                "history": load_history(),
                "hysteresis": cache.get("hysteresis", 1.0),
                "target_temp": cache.get("auto_target_temp", 22),
                "auto_enabled": cache.get("auto_enabled", False),
                "normal_fan": cache.get("normal_fan", "medium_high"),
                "boost_temp": cache.get("boost_temp", 22.0),
                "schedules": cache.get("schedules", []),
                "boost_active": cache.get("boost_active", False),
                "auto_paused_until": cache.get("external_pause_until"),
                "hold_offset": cache.get("hold_offset", 1),
                "outdoor_rule": get_outdoor_rule(),
                "night": get_night(),
                "preheat": cache.get("preheat"),
                "night_cold_skip": bool(cache.get("night_cold_skip")),
                "stove": get_stove(),
                "staged": get_staged(),
                "staged_active": bool(cache.get("esc")),
                "hold_learn": cache.get("hold_learn") or {},
                "hold_learn_now": (lambda p: None if p is None else {"offset": round(p[0], 1), "fan": p[1]})(hl_predict(cache.get("outdoor_temp"))),
                "hold_fan": cache.get("hold_fan"),
                "stove_active": bool(cache.get("stove_active")),
                "target_mode": cache.get("target_mode"),
                "away": get_away(),
                "away_now": (lambda st: {"state": st[0], **st[1]} if st[0] else None)(away_state(datetime.now(LOCAL_TZ))),
                "events": recent_events(hours=48, limit=500),
                "room_name": ROOM_NAME,
                "room_id": ROOM_ID,
                "filter": get_filter(),
                "ts": datetime.now().isoformat()
            })

        elif path == "/api/series":
            q = urlparse(self.path).query
            rk = next((p.split("=", 1)[1] for p in q.split("&") if p.startswith("range=")), "24h")
            self.send_json(series(rk if rk in RANGES else "24h"))

        elif path == "/api/history":
            self.send_json({"history": load_history()})

        elif path == "/api/predict":
            target = float(urlparse(self.path).query.split("target=")[-1].split("&")[0]) if "target=" in self.path else 22
            self.send_json(predict_heatup(target, cache.get("outdoor_temp")))

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        if self.proxy_room("POST", raw):
            return
        want = self.headers.get("X-Room")
        if want and want != ROOM_ID:
            log.warning(f"Avviste endring merket for stue «{want}» (dette er {ROOM_ID}): {path}")
            self.send_json({"ok": False, "error": "Feil stue — last inn appen på nytt"}, 409)
            return
        body = json.loads(raw or b"{}")

        if path == "/api/command":
            result = self.handle_command(body)
            self.send_json(result)

        elif path == "/api/log_session":
            log_heat_session(body)
            self.send_json({"ok": True})

        elif path == "/api/settings":
            if "auto_enabled" in body:
                if cache["auto_enabled"] != bool(body["auto_enabled"]):
                    add_event("setting", f"Auto slått {'på' if body['auto_enabled'] else 'av'} i appen")
                cache["auto_enabled"] = bool(body["auto_enabled"])
                log.info(f"Auto: {'PÅ' if cache['auto_enabled'] else 'AV'}")
            if "normal_fan" in body:
                cache["normal_fan"] = str(body["normal_fan"])
                log.info(f"Normalvifte: {cache['normal_fan']}")
            if "boost_temp" in body:
                cache["boost_temp"] = max(16, min(30, float(body["boost_temp"])))
                log.info(f"Boost temp: {cache['boost_temp']}°C")
            if "schedules" in body:
                cache["schedules"] = body["schedules"]
                log.info(f"Timeplan oppdatert: {len(cache['schedules'])} tidspunkter")
                add_event("setting", f"Timeplan endret ({len(cache['schedules'])} tidspunkter)")
            if "away" in body and isinstance(body["away"], dict):
                a = {**get_away(), **body["away"]}
                periods = []
                for p in a.get("periods") or []:
                    try:
                        f, t = _away_dt(p["from"]), _away_dt(p["to"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if t > f:
                        periods.append({"from": f"{f:%Y-%m-%d %H:%M}", "to": f"{t:%Y-%m-%d %H:%M}"})
                periods.sort(key=lambda p: p["from"])
                new_a = {"temp": max(16, min(25, round(float(a["temp"])))),
                         "lead_hours": max(0, min(72, int(a["lead_hours"]))), "periods": periods}
                old_a = get_away()
                if new_a != old_a:
                    cache["away"] = new_a
                    if new_a["periods"] != old_a["periods"]:
                        add_event("setting", "Tomt hus: " + (", ".join(f"{_away_dt(p['from']):%d.%m %H:%M}–{_away_dt(p['to']):%d.%m %H:%M}" for p in periods) or "ingen perioder"))
                    if (new_a["temp"], new_a["lead_hours"]) != (old_a["temp"], old_a["lead_hours"]):
                        add_event("setting", f"Tomt hus: {new_a['temp']:g}°C, varm opp {new_a['lead_hours']} t før ankomst")
                    outdoor_rule_check(force=True)
                    cache["last_sensibo"] = 0
            if "outdoor_rule" in body and isinstance(body["outdoor_rule"], dict):
                r = {**get_outdoor_rule(), **body["outdoor_rule"]}
                warm_from = max(-25, min(20, float(r["warm_from"])))
                new_rule = {
                    "enabled": bool(r["enabled"]),
                    "warm_from": warm_from,
                    "cold_below": max(-25, min(warm_from, float(r["cold_below"]))),
                    "warm_temp": max(16, min(28, float(r["warm_temp"]))),
                    "cold_temp": max(16, min(28, float(r["cold_temp"]))),
                }
                old_rule = get_outdoor_rule()
                cache["outdoor_rule"] = new_rule
                if new_rule["enabled"] != old_rule["enabled"]:
                    add_event("setting", "Mål etter utetemp " + ("på" if new_rule["enabled"] else "av"))
                elif new_rule != old_rule:
                    add_event("setting", f"Mål etter utetemp: {new_rule['warm_temp']:g}° fra {new_rule['warm_from']:g}° ute, "
                                         f"{new_rule['cold_temp']:g}° under {new_rule['cold_below']:g}° ute")
                # Nye grenser/temperaturer skal gjelde med en gang, ikke først ved neste kryssing
                cache.pop("outdoor_bracket", None)
                outdoor_rule_check(force=True)
            if "filter" in body and isinstance(body["filter"], dict):
                f = get_filter()
                days, last = f["interval_days"], f["last_cleaned"]
                if "interval_days" in body["filter"]:
                    days = max(7, min(120, int(body["filter"]["interval_days"])))
                    if days != f["interval_days"]:
                        add_event("setting", f"Filterintervall: {days} dager")
                if body["filter"].get("last_cleaned"):  # rengjort tidligere enn i dag — ikke fram i tid, maks ett år tilbake
                    try:
                        d = datetime.fromisoformat(str(body["filter"]["last_cleaned"])).date()
                    except ValueError:
                        self.send_json({"ok": False, "error": "Ugyldig dato"}); return
                    today = datetime.now(LOCAL_TZ).date()
                    if not (today - timedelta(days=365) <= d <= today):
                        self.send_json({"ok": False, "error": "Datoen må være i løpet av det siste året"}); return
                    if d.isoformat() != f["last_cleaned"]:
                        last = d.isoformat()
                        cache["filter_push_day"] = None
                        add_event("setting", f"Filtre sist rengjort satt til {d:%d.%m.%Y}")
                cache["filter"] = {"interval_days": days, "last_cleaned": last}
            if "staged" in body and isinstance(body["staged"], dict):
                st = {**get_staged(), **body["staged"]}
                new_st = {"enabled": bool(st["enabled"]), "step": max(1, min(4, int(st["step"]))),
                          "every_min": max(10, min(90, int(st["every_min"]))),
                          "full_below": max(0.5, min(5.0, round(float(st["full_below"]) * 2) / 2)),
                          "fan": st["fan"] if st["fan"] in ("quiet", "low", "medium", "medium_high") else "medium_high",
                          "min_offset": max(-3, min(4, int(st.get("min_offset", 0)))),
                          "min_offset_below": max(-25, min(15, int(st.get("min_offset_below", -5))))}
                if new_st != get_staged():
                    cache["staged"] = new_st
                    if not new_st["enabled"]:
                        cache.pop("esc", None)
                    add_event("setting", (f"Trinnvis oppvarming: +{new_st['step']}° hvert {new_st['every_min']}. min, "
                                          f"høy vifte sist, full trøkk når {new_st['full_below']:g}° under, aldri under ønsket {new_st['min_offset']:+d}° når ute ≤ {new_st['min_offset_below']}°")
                              if new_st["enabled"] else "Trinnvis oppvarming av")
                    cache["last_sensibo"] = 0
            if "stove" in body and isinstance(body["stove"], dict):
                st = {**get_stove(), **body["stove"]}
                off = max(20, min(45, round(float(st["off_above"]))))
                on = max(15, min(off - 1, round(float(st["on_below"]))))
                new_st = {"enabled": bool(st["enabled"]), "off_above": off, "on_below": on}
                if new_st != get_stove():
                    cache["stove"] = new_st
                    add_event("setting", f"Vedovn-stopp: av over {off}°C, på igjen under {on}°C" if new_st["enabled"] else "Vedovn-stopp av")
                    cache["last_sensibo"] = 0
            if "night" in body and isinstance(body["night"], dict):
                n = {**get_night(), **body["night"]}
                try:
                    for k in ("start", "weekend_start", "end"):
                        h, m = _hm(n[k]); assert 0 <= h < 24 and 0 <= m < 60
                        n[k] = f"{h:02d}:{m:02d}"
                    new_night = {"enabled": bool(n["enabled"]), "start": n["start"], "weekend_start": n["weekend_start"],
                                 "end": n["end"], "temp": max(16, min(28, round(float(n["temp"])))),
                                 "preheat": bool(n.get("preheat", True)),
                                 "cold_skip": bool(n.get("cold_skip", False)),
                                 "morning_full": bool(n.get("morning_full", True)),
                                 "morning_full_below": max(0.0, min(3.0, round(float(n.get("morning_full_below", 1.0)) * 2) / 2)),
                                 "cold_skip_below": max(-30, min(10, round(float(n.get("cold_skip_below", -5)))))}
                except (ValueError, AssertionError, TypeError):
                    self.send_json({"ok": False, "error": "Ugyldig klokkeslett"}); return
                if new_night != get_night():
                    cache["night"] = new_night
                    add_event("setting", "Nattsenking av" if not new_night["enabled"] else
                              f"Nattsenking: {new_night['temp']:g}°C fra {new_night['start']} (helg {new_night['weekend_start']}) til {new_night['end']}"
                              + (f", ikke når det er kaldere enn {new_night['cold_skip_below']}°C ute" if new_night["cold_skip"] else ""))
                    outdoor_rule_check(force=True)
            if "target_temp" in body or body.get("auto_enabled"):
                resume_from_pause("endring i appen")
            if "target_temp" in body:
                new_t = max(16, min(30, float(body["target_temp"])))
                if new_t != cache.get("auto_target_temp"):
                    add_event("setting", f"Ønsket temp satt til {new_t:g}°C i appen")
                    if TARGET_SETS_STANDARD:
                        make_standard(new_t)
                cache["auto_target_temp"] = new_t
                log.info(f"Ønsket temp satt til {cache['auto_target_temp']}°C")
            if "hysteresis" in body:
                cache["hysteresis"] = max(0.5, min(3.0, float(body["hysteresis"])))
                log.info(f"Nedre hysteresis: {cache['hysteresis']}°C")
            save_state()
            self.send_json({
                "ok": True,
                "target_temp": cache["auto_target_temp"],
                "auto_enabled": cache["auto_enabled"],
                "hysteresis": cache["hysteresis"],
                "normal_fan": cache["normal_fan"],
                "boost_temp": cache["boost_temp"],
                "schedules": cache["schedules"]
            })

        else:
            self.send_response(404)
            self.end_headers()

    def handle_command(self, cmd):
        uid = get_device_uid()
        if not uid:
            return {"ok": False, "error": "Finner ikke Sensibo-enhet"}

        cmd_type = cmd.get("type")
        if cmd_type == "filter_cleaned":  # rører ikke pumpa — skal ikke oppheve en auto-pause
            f = get_filter()
            cache["filter"] = {"interval_days": f["interval_days"], "last_cleaned": datetime.now(LOCAL_TZ).date().isoformat()}
            cache["filter_push_day"] = None
            save_state()
            add_event("setting", f"Filtre rengjort — neste om {f['interval_days']} dager")
            return {"ok": True, "filter": get_filter()}

        resume_from_pause("handling i appen")

        if cmd_type == "power":
            payload = {"on": cmd["on"]}
            result = sensibo_patch(uid, payload, "Manuelt i appen")
            if result:
                cache["ac"]["on"] = cmd["on"]
                log.info(f"Kraft: {'På' if cmd['on'] else 'Av'}")
                return {"ok": True}
            return {"ok": False, "error": "Sensibo svarte ikke"}

        elif cmd_type == "manual":
            mode = cmd.get("mode", "heat")
            if mode == "cool":
                log.warning("BLOKKERT: Forsøk på å sette cool-modus avvist!")
                return {"ok": False, "error": "Kjøling er deaktivert — pumpa kjører alltid i varme-modus"}
            payload = {
                "on": cmd.get("on", True),
                "mode": "heat",  # ALLTID heat uansett hva som sendes inn
                "targetTemperature": cmd.get("targetTemperature", 22),
                "fanLevel": cmd.get("fanLevel", "medium"),
            }
            result = sensibo_patch(uid, payload, "Manuelt i appen")
            if result:
                cache["ac"].update(payload)
                log.info(f"Manuell: {payload}")
                return {"ok": True}
            return {"ok": False, "error": "Sensibo svarte ikke"}

        elif cmd_type == "auto":
            target = cmd.get("targetTemp", 22)
            enabled = cmd.get("enabled", True)
            cache["auto_enabled"] = enabled
            cache["auto_target_temp"] = target
            save_state()
            if enabled:
                # Beregn og send umiddelbart
                outdoor = cache.get("outdoor_temp")
                settings = calculate_optimal_settings(target, outdoor)
                settings["on"] = True
                result = sensibo_patch(uid, settings, f"Auto slått på (mål {target}°C)")
                if result:
                    cache["ac"].update(settings)
                    pred = predict_heatup(target, outdoor)
                    log.info(f"Auto PÅ: mål={target}°C, innstilling={settings}")
                    return {"ok": True, "settings": settings, "prediction": pred}
                return {"ok": False, "error": "Sensibo svarte ikke"}
            else:
                log.info("Auto AV")
                add_event("setting", "Auto slått av")
                return {"ok": True}

        elif cmd_type == "boost":
            target = cmd.get("boostTemp", cache.get("boost_temp", 22.0))
            cache["boost_active"] = True
            cache["boost_target"] = target
            cache["boost_start_temp"] = cache.get("temperature")
            cache["boost_start_time"] = time.time()
            payload = {"on": True, "mode": "heat", "targetTemperature": 30, "fanLevel": "high"}
            result = sensibo_patch(uid, payload, f"Boost startet (mål {target}°C)")
            if result:
                cache["ac"].update(payload)
                log.info(f"Boost aktivert: 30°C, høy vifte → mål {target}°C")
                return {"ok": True}
            return {"ok": False, "error": "Sensibo svarte ikke"}

        return {"ok": False, "error": f"Ukjent kommandotype: {cmd_type}"}


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    log.info("=" * 50)
    log.info("2GM Varme backend starter")
    log.info(f"Port: {SERVER_PORT}")
    log.info(f"Stue: {ROOM_NAME} [{ROOM_ID}] (Sensibo {SENSIBO_DEVICE_UID})")
    log.info(f"Koordinater: {METNO_LAT}, {METNO_LON} ({METNO_PLACE or 'fra .env'})"
             f"{'' if HA_OUTDOOR_ENTITY else ' — ingen Hue-sensor, kun met.no'}")
    log.info("=" * 50)

    if not SENSIBO_API_KEY:
        log.warning("ADVARSEL: SENSIBO_API_KEY ikke satt i .env!")

    # Last lagret state fra disk
    load_state()
    load_events()
    init_series()
    add_event("setting", "Varme-serveren startet")

    # Initial data-henting
    fetch_sensibo_state()
    fetch_outdoor_sensor()
    fetch_metno()

    # Bakgrunns-loop
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()

    # Start web server
    server = HTTPServer((SERVER_HOST, SERVER_PORT), Handler)
    log.info(f"Server kjører på http://{SERVER_HOST}:{SERVER_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Server stoppet")
