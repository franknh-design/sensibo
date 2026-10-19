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
SENSIBO_DEVICE_UID = os.getenv("SENSIBO_DEVICE_UID", "YRqRonRH")  # Hardkodet riktig enhet
METNO_LAT = float(os.getenv("METNO_LAT", "69.2333"))
METNO_LON = float(os.getenv("METNO_LON", "17.9833"))
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
    "device_uid": "YRqRonRH",  # Hardkodet — kjeller-enhet bCCMwLk4 skal ikke brukes
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

def fetch_sensibo_state():
    """Henter AC-state og romsensor-temperatur i ett API-kall"""
    data = sensibo_get("/pods/YRqRonRH", {"fields": "acState,motionSensors,mainMeasurementsSensor,measurements"})
    if not data:
        return
    result = data.get("result", {})
    ac = result.get("acState", {})
    detect_external_change(ac)
    cache["ac"] = ac
    cache["last_sensibo"] = time.time()
    check_boost_complete()
    auto_feedback_check()

    # Hent temperatur fra ekstern romsensor (mainMeasurementsSensor)
    main = result.get("mainMeasurementsSensor")
    if main and main.get("isMainSensor"):
        meas = main.get("measurements", {})
        temp = meas.get("temperature")
        if temp is not None:
            cache["temperature"] = temp
            cache["humidity"] = meas.get("humidity")
            log.info(f"Sensibo romsensor {main.get('qrId')}: {temp}°C, {meas.get('humidity')}%, AC={'På' if ac.get('on') else 'Av'}")
            return

    # Fallback: innebygd sensor
    meas = result.get("measurements", {})
    cache["temperature"] = meas.get("temperature")
    cache["humidity"] = meas.get("humidity")
    log.info(f"Sensibo innebygd sensor: {cache['temperature']}°C, AC={'På' if ac.get('on') else 'Av'}")

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
            cache["outdoor_temp"] = now.get("air_temperature")
            cache["outdoor_forecast"] = [
                {
                    "time": s["time"],
                    "temp": s["data"]["instant"]["details"].get("air_temperature")
                }
                for s in series[:12]
            ]
            cache["last_metno"] = time.time()
            log.info(f"met.no: ute={cache['outdoor_temp']}°C")
    except Exception as e:
        log.error(f"met.no feil: {e}")



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

def _is_heating(ac):
    return ac.get("on") and ac.get("targetTemperature") == 30 and ac.get("fanLevel") == "high"

def auto_feedback_check():
    """
    1. Oppvarming: romtemp < mål − hysteresis → 30 °C + høy vifte, til rommet er mål − 0,3.
    2. Holde:      pumpa på mål + påslag (selvjusterende) + normalvifte.
    Sender kun kommando når innstillingen faktisk må endres.
    """
    if not cache["auto_enabled"]:
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

    heating = _is_heating(ac)
    if current < target - hysteresis or (heating and current < target - HEAT_STOP_BELOW):
        if heating:
            return
        # NB: påslaget økes ikke her — oppvarming skyldes ofte åpen dør, ikke for lavt hold.
        # Holdet justeres bare av den langsomme regulatoren under.
        payload = {"on": True, "mode": "heat", "targetTemperature": 30, "fanLevel": "high"}
        if sensibo_patch(uid, payload, f"Auto oppvarming (rom {current:.1f}°C, mål {target:g}°C)"):
            cache["ac"].update(payload)
            cache["auto_heat_start"] = {"temp": current, "time": time.time(), "outdoor": cache.get("outdoor_temp")}
            log.info(f"Auto OPPVARMING: {current:.1f}°C < {target - hysteresis:.1f}°C | 30°C + høy vifte")
        return

    log_auto_heat_session(current)
    # HOLDE — juster påslaget sakte etter hvordan rommet faktisk ligger
    offset = cache.get("hold_offset", 1)
    now = time.time()
    if not heating and now - cache.get("hold_adjusted_at", 0) >= HOLD_ADJUST_EVERY:
        new = offset
        if current < target - HOLD_LOW:
            new = min(HOLD_OFFSET_MAX, offset + 1)
        elif current > target + HOLD_HIGH:
            new = max(HOLD_OFFSET_MIN, offset - 1)
        if new != offset:
            cache["hold_offset"] = offset = new
            cache["hold_adjusted_at"] = now
            save_state()
    wanted_temp = int(round(target + offset))
    if ac.get("on") and ac.get("targetTemperature") == wanted_temp and ac.get("fanLevel") == normal_fan:
        return
    payload = {"on": True, "mode": "heat", "targetTemperature": wanted_temp, "fanLevel": normal_fan}
    if sensibo_patch(uid, payload, f"Auto holder (rom {current:.1f}°C, mål {target:g}°C)"):
        cache["ac"].update(payload)
        cache["hold_adjusted_at"] = now
        log.info(f"Auto HOLDER: rom {current:.1f}°C, mål {target}°C | pumpe {wanted_temp}°C (påslag {offset:+d})")

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

def outdoor_rule_check(force=False):
    """Setter ønsket temp etter utetemperaturen når den krysser terskelen (eller når regelen slås på).
    Ett utslag per bytte, så timeplan/manuelle endringer står seg til neste bytte."""
    rule = get_outdoor_rule()
    out = cache.get("outdoor_temp")
    if not rule["enabled"] or out is None:
        return
    prev = cache.get("outdoor_bracket")
    if prev == "cold":
        bracket = "warm" if out >= rule["warm_from"] else "cold"
    else:
        bracket = "cold" if out < rule["cold_below"] else "warm"
    if bracket == prev and not force:
        return
    cache["outdoor_bracket"] = bracket
    temp = rule["warm_temp"] if bracket == "warm" else rule["cold_temp"]
    if cache.get("auto_target_temp") != temp:
        cache["auto_target_temp"] = temp
        add_event("setting", f"Ute {out:.1f}°C → ønsket temp {temp:g}°C")
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
    if ev is None:
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

def hourly_status():
    """Ett øyeblikksbilde i timen: rom, ute og hva pumpa står på."""
    hour = datetime.now(LOCAL_TZ).strftime("%Y-%m-%dT%H")
    if cache.get("last_status_hour") == hour or cache.get("temperature") is None:
        return
    cache["last_status_hour"] = hour
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
        if now - cache["last_sensibo"] > 90:
            fetch_sensibo_state()
        if now - cache["last_metno"] > 1800:  # 30 min
            fetch_metno()
        try:
            outdoor_rule_check()
        except Exception as e:
            log.error(f"Utetemp-regel-feil: {e}")
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
    "last_schedule_key", "hold_offset", "outdoor_rule", "outdoor_bracket"
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

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/" or path == "/index.html":
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
                "events": recent_events(hours=48, limit=500),
                "ts": datetime.now().isoformat()
            })

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
        body = json.loads(self.rfile.read(length) or b"{}")

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
            if "target_temp" in body or body.get("auto_enabled"):
                resume_from_pause("endring i appen")
            if "target_temp" in body:
                new_t = max(16, min(30, float(body["target_temp"])))
                if new_t != cache.get("auto_target_temp"):
                    add_event("setting", f"Ønsket temp satt til {new_t:g}°C i appen")
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
    log.info(f"Enhet: {SENSIBO_DEVICE_NAME}")
    log.info(f"Koordinater: {METNO_LAT}, {METNO_LON}")
    log.info("=" * 50)

    if not SENSIBO_API_KEY:
        log.warning("ADVARSEL: SENSIBO_API_KEY ikke satt i .env!")

    # Last lagret state fra disk
    load_state()
    load_events()
    add_event("setting", "Varme-serveren startet")

    # Initial data-henting
    fetch_sensibo_state()
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
