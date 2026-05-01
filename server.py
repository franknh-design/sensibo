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
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CONFIG
# ============================================================
SENSIBO_API_KEY = os.getenv("SENSIBO_API_KEY", "")
SENSIBO_DEVICE_NAME = os.getenv("SENSIBO_DEVICE_NAME", "Frank's device AC")
METNO_LAT = float(os.getenv("METNO_LAT", "69.2333"))
METNO_LON = float(os.getenv("METNO_LON", "17.9833"))
SERVER_PORT = int(os.getenv("SERVER_PORT", "8765"))
LOG_FILE = "heat_history.csv"
DATA_FILE = "state.json"

SENSIBO_BASE = "https://home.sensibo.com/api/v2"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("server.log", encoding="utf-8"),
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
    "device_uid": None,
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
        log.error(f"Sensibo GET {endpoint}: {e}")
        return None

def sensibo_patch(uid, payload):
    try:
        r = requests.patch(
            f"{SENSIBO_BASE}/pods/{uid}/acStates",
            params={"apiKey": SENSIBO_API_KEY},
            json={"acState": payload},
            timeout=10
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Sensibo PATCH: {e}")
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
    uid = get_device_uid()
    if not uid:
        return
    data = sensibo_get(f"/pods/{uid}", {"fields": "*"})
    if not data:
        return
    result = data.get("result", {})
    meas = result.get("measurements", {})
    ac = result.get("acState", {})

    # Logg rådata så vi ser hva som faktisk returneres
    log.info(f"Sensibo rådata measurements: {json.dumps(meas)}")
    log.info(f"Sensibo rådata acState: {json.dumps(ac)}")

    # Prøv romsensor først, fall tilbake til measurements
    room_sensors = result.get("roomSensors", {})
    if room_sensors:
        log.info(f"Sensibo roomSensors: {json.dumps(room_sensors)}")
        for sensor_uid, sensor_data in room_sensors.items():
            sensor_meas = sensor_data.get("measurements", {})
            if sensor_meas.get("temperature") is not None:
                cache["temperature"] = sensor_meas["temperature"]
                cache["humidity"] = sensor_meas.get("humidity")
                log.info(f"Bruker romsensor {sensor_uid}: {cache['temperature']}°C")
                break
    else:
        cache["temperature"] = meas.get("temperature")
        cache["humidity"] = meas.get("humidity")

    cache["ac"] = ac
    cache["last_sensibo"] = time.time()
    log.info(f"Sensibo: {cache['temperature']}°C, {cache['humidity']}%, AC={'På' if ac.get('on') else 'Av'}")

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

def fetch_room_sensor():
    """Henter temperatur fra romsensor - lister alle pods på kontoen"""
    data = sensibo_get("/users/me/pods", {"fields": "id,room,measurements,productModel"})
    if not data:
        return
    pods = data.get("result", [])
    log.info(f"Alle pods på kontoen: {len(pods)} stk")
    for pod in pods:
        uid = pod.get("id")
        room = pod.get("room", {}).get("name", "?")
        model = pod.get("productModel", "?")
        meas = pod.get("measurements", {})
        temp = meas.get("temperature")
        log.info(f"  Pod {uid} ({model}) rom={room} temp={temp}")
        # Hvis dette er romsensoren vår
        if uid == "FTKRATUUWC" and temp is not None:
            cache["temperature"] = temp
            cache["humidity"] = meas.get("humidity")
            log.info(f"Romsensor funnet: {temp}°C")
            return

# ============================================================
# BACKGROUND REFRESH
# ============================================================
def background_loop():
    while True:
        now = time.time()
        if now - cache["last_sensibo"] > 30:
            fetch_sensibo_state()
            fetch_room_sensor()
        if now - cache["last_metno"] > 1800:  # 30 min
            fetch_metno()
        time.sleep(25)

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
            # Beregn optimale innstillinger
            target = cmd.get("targetTemp", 22)
            outdoor = cache.get("outdoor_temp")
            settings = calculate_optimal_settings(target, outdoor)
            settings["on"] = True
            result = sensibo_patch(uid, settings)
            if result:
                cache["ac"].update(settings)
                pred = predict_heatup(target, outdoor)
                log.info(f"Auto: {settings}, prediksjon: {pred.get('estimated_minutes')} min")
                return {"ok": True, "settings": settings, "prediction": pred}
            return {"ok": False, "error": "Sensibo svarte ikke"}

        elif cmd_type == "boost":
            payload = {
                "on": True, "mode": "heat",
                "targetTemperature": 30, "fanLevel": "high"
            }
            result = sensibo_patch(uid, payload)
            if result:
                cache["ac"].update(payload)
                log.info("Boost aktivert: 30°C, høy vifte")
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

    # Initial data-henting
    fetch_sensibo_state()
    fetch_room_sensor()
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
