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
    "hysteresis_upper": 1.0,
    "rest_after_hours": 2.0,
    "cooling_phase": None,
    "cooling_phase_since": None,
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

def sensibo_patch(uid, payload):
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
        return r.json()
    except requests.HTTPError as e:
        log.error(f"Sensibo kommando avvist ({e.response.status_code}): {payload} → {e.response.text[:200]}")
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

def fetch_sensibo_state():
    """Henter AC-state og romsensor-temperatur i ett API-kall"""
    data = sensibo_get("/pods/YRqRonRH", {"fields": "acState,motionSensors,mainMeasurementsSensor,measurements"})
    if not data:
        return
    result = data.get("result", {})
    ac = result.get("acState", {})
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
            result = sensibo_patch(uid, payload)
            if result:
                cache["ac"].update(payload)
                log.info(f"Boost ferdig! {elapsed:.0f} min for {delta:.1f}°C → {target}°C, {normal_fan} vifte")

def auto_feedback_check():
    """
    To tilstander:
    1. Oppvarming: romtemp < ønsket - hysteresis → 30C + høy vifte
    2. Holde:      romtemp >= ønsket - hysteresis → ønsket temp + mid-høy vifte

    Sender kun kommando hvis innstillingen faktisk er annerledes enn det som kjører.
    """
    if not cache["auto_enabled"]:
        return
    if cache.get("boost_active"):
        return  # Boost kjører — auto pauset
    current = cache.get("temperature")
    if current is None:
        return

    target = cache["auto_target_temp"]
    hysteresis = cache.get("hysteresis", 1.0)
    ac = cache.get("ac", {})
    ac_temp = ac.get("targetTemperature", 22)
    ac_fan = ac.get("fanLevel", NORMAL_FAN)
    ac_on = ac.get("on", False)
    uid = get_device_uid()
    if not uid:
        return

    if current < target - hysteresis:
        # OPPVARMING — trenger full trøkk
        wanted_temp = 30
        wanted_fan = "high"
        if ac_on and ac_temp == wanted_temp and ac_fan == wanted_fan:
            log.debug(f"Auto oppvarming allerede aktiv ({current:.1f}°C < {target-hysteresis:.1f}°C)")
            return
        payload = {"on": True, "mode": "heat", "targetTemperature": wanted_temp, "fanLevel": wanted_fan}
        result = sensibo_patch(uid, payload)
        if result:
            cache["ac"].update(payload)
            log.info(f"Auto OPPVARMING: {current:.1f}°C → mål {target}°C (hysteresis {hysteresis}°C) | 30°C + høy vifte")
    else:
        # HOLDE — normalinnstilling
        wanted_temp = target
        wanted_fan = NORMAL_FAN
        if ac_on and ac_temp == wanted_temp and ac_fan == wanted_fan:
            log.debug(f"Auto holder temp ({current:.1f}°C ≥ {target-hysteresis:.1f}°C)")
            return
        payload = {"on": True, "mode": "heat", "targetTemperature": wanted_temp, "fanLevel": wanted_fan}
        result = sensibo_patch(uid, payload)
        if result:
            cache["ac"].update(payload)
            log.info(f"Auto HOLDER: {current:.1f}°C ≥ {target-hysteresis:.1f}°C | {target}°C + {NORMAL_FAN} vifte")

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
    cache["last_sensibo"] = 0  # hent status og la auto-styringen reagere med en gang

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
    with open(LOG_FILE, encoding="utf-8") as f:
        return list(csv.DictReader(f))

# ============================================================
# PERSISTENT STATE
# ============================================================
STATE_KEYS = [
    "auto_enabled", "auto_target_temp", "hysteresis", "hysteresis_upper",
    "rest_after_hours", "normal_fan", "boost_temp", "schedules",
    "last_schedule_key"
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
        if outdoor_temp is not None and h.get("outdoor_temp") is not None
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
                "auto_phase": cache.get("cooling_phase"),
                "normal_fan": cache.get("normal_fan", "medium_high"),
                "boost_temp": cache.get("boost_temp", 22.0),
                "schedules": cache.get("schedules", []),
                "hysteresis_upper": cache.get("hysteresis_upper", 1.0),
                "rest_after_hours": cache.get("rest_after_hours", 2.0),
                "cooling_phase": cache.get("cooling_phase"),
                "boost_active": cache.get("boost_active", False),
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
            if "target_temp" in body:
                cache["auto_target_temp"] = max(16, min(30, float(body["target_temp"])))
                log.info(f"Ønsket temp satt til {cache['auto_target_temp']}°C")
            if "hysteresis" in body:
                cache["hysteresis"] = max(0.5, min(3.0, float(body["hysteresis"])))
                log.info(f"Nedre hysteresis: {cache['hysteresis']}°C")
            if "hysteresis_upper" in body:
                cache["hysteresis_upper"] = max(0.5, min(5.0, float(body["hysteresis_upper"])))
                log.info(f"Øvre hysteresis: {cache['hysteresis_upper']}°C")
            if "rest_after_hours" in body:
                cache["rest_after_hours"] = max(0.5, min(12.0, float(body["rest_after_hours"])))
                log.info(f"Hvilemodus etter: {cache['rest_after_hours']}t")
            save_state()
            self.send_json({
                "ok": True,
                "target_temp": cache["auto_target_temp"],
                "auto_enabled": cache["auto_enabled"],
                "hysteresis": cache["hysteresis"],
                "hysteresis_upper": cache["hysteresis_upper"],
                "rest_after_hours": cache["rest_after_hours"],
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

        if cmd_type == "power":
            payload = {"on": cmd["on"]}
            result = sensibo_patch(uid, payload)
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
            result = sensibo_patch(uid, payload)
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
                result = sensibo_patch(uid, settings)
                if result:
                    cache["ac"].update(settings)
                    pred = predict_heatup(target, outdoor)
                    log.info(f"Auto PÅ: mål={target}°C, innstilling={settings}")
                    return {"ok": True, "settings": settings, "prediction": pred}
                return {"ok": False, "error": "Sensibo svarte ikke"}
            else:
                log.info("Auto AV")
                return {"ok": True}

        elif cmd_type == "boost":
            target = cmd.get("boostTemp", cache.get("boost_temp", 22.0))
            cache["boost_active"] = True
            cache["boost_target"] = target
            cache["boost_start_temp"] = cache.get("temperature")
            cache["boost_start_time"] = time.time()
            payload = {"on": True, "mode": "heat", "targetTemperature": 30, "fanLevel": "high"}
            result = sensibo_patch(uid, payload)
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

    # Initial data-henting
    fetch_sensibo_state()
    fetch_metno()

    # Bakgrunns-loop
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()

    # Start web server
    server = HTTPServer(("0.0.0.0", SERVER_PORT), Handler)
    log.info(f"Server kjører på http://0.0.0.0:{SERVER_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Server stoppet")
