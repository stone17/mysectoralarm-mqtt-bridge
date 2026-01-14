import asyncio
import json
import logging
import os
import yaml
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import paho.mqtt.client as mqtt_client

from app.sector import SectorAlarmAPI

# --- CONFIG & LOGGING ---
CONFIG_FILE = os.getenv("CONFIG_PATH", "app/sector_config.yaml")
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("SectorBridge")

# --- GLOBAL STATE ---
sector_api: SectorAlarmAPI = None
latest_data = {"status": "Unknown", "temps": [], "humidity": []}
system_state = "STARTING" # STATES: CONNECTED, WAITING_2FA, ERROR, STARTING
running = True

# --- CONFIG MANAGER (Same as before) ---
class ConfigManager:
    def __init__(self, filepath):
        self.filepath = filepath
        self.data = {
            "mqtt_broker": os.getenv("MQTT_BROKER", "192.168.0.100"),
            "mqtt_port": int(os.getenv("MQTT_PORT", 1883)),
            "mqtt_prefix": "sector",
            "discovery_prefix": "homeassistant",
            "email": "",
            "password": "",
            "panel_id": "",
            "panel_code": "",
            "token": ""
        }
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r') as f:
                    loaded = yaml.safe_load(f)
                    if loaded: self.data.update(loaded)
            except Exception as e:
                logger.error(f"Config Error: {e}")

    def save(self):
        try:
            with open(self.filepath, 'w') as f:
                yaml.dump(self.data, f)
        except Exception as e:
            logger.error(f"Save Error: {e}")

cfg = ConfigManager(CONFIG_FILE)

# --- MQTT HANDLER (Same logic, condensed) ---
class MqttHandler:
    def __init__(self):
        self.client = mqtt_client.Client()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def start(self):
        try:
            self.client.connect(cfg.data['mqtt_broker'], cfg.data['mqtt_port'], 60)
            self.client.loop_start()
        except: logger.error("MQTT Connect Failed")

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            base = cfg.data.get("mqtt_prefix", "sector")
            client.subscribe(f"{base}/+/set")
            self.publish_discovery()

    def on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode().upper()
            if cfg.data.get("panel_code") and sector_api:
                mode = {"ARM_AWAY":"Total", "ARM_HOME":"Partial", "DISARM":"Disarm"}.get(payload)
                if mode: asyncio.run_coroutine_threadsafe(sector_api.arm_system(cfg.data["panel_code"], mode), loop)
        except: pass

    def publish_discovery(self):
        p_id = cfg.data.get("panel_id")
        if not p_id: return
        disc = cfg.data.get("discovery_prefix", "homeassistant")
        base = cfg.data.get("mqtt_prefix", "sector")
        
        # Alarm Panel
        dev = {"identifiers": [f"sa_{p_id}"], "name": "Sector Alarm", "manufacturer": "Sector Alarm"}
        p = {"name": "Sector Alarm Panel", "unique_id": f"sa_panel_{p_id}", "command_topic": f"{base}/{p_id}/set", "state_topic": f"{base}/{p_id}/state", "device": dev}
        self.client.publish(f"{disc}/alarm_control_panel/sa_{p_id}/config", json.dumps(p), retain=True)

    def publish_sensor(self, serial, name, type_, val):
        disc = cfg.data.get("discovery_prefix", "homeassistant")
        base = cfg.data.get("mqtt_prefix", "sector")
        clean = serial.replace(":", "")
        dev = {"identifiers": [f"sa_dev_{serial}"], "name": name, "via_device": f"sa_{cfg.data.get('panel_id')}"}
        
        t_conf = {
            "temp": {"u": "°C", "c": "temperature", "t": "temperature"},
            "hum": {"u": "%", "c": "humidity", "t": "humidity"}
        }[type_]
        
        p = {
            "name": f"{name} {type_.title()}", "unique_id": f"sa_{clean}_{type_}",
            "state_topic": f"{base}/sensor/{clean}/state", "unit_of_measurement": t_conf['u'],
            "device_class": t_conf['c'], "value_template": f"{{{{ value_json.{t_conf['t']} }}}}", "device": dev
        }
        self.client.publish(f"{disc}/sensor/sa_{clean}_{type_}/config", json.dumps(p), retain=True)
        self.client.publish(f"{base}/sensor/{clean}/state", json.dumps({t_conf['t']: val}))

    def publish_state(self, state):
        base = cfg.data.get("mqtt_prefix", "sector")
        ha_state = {"armed": "armed_away", "partialarmed": "armed_home", "disarmed": "disarmed"}.get(state, "disarmed")
        self.client.publish(f"{base}/{cfg.data['panel_id']}/state", ha_state)

mqtt_handler = MqttHandler()

# --- BACKGROUND POLLING ---
async def poll_sector():
    global sector_api, latest_data, system_state
    
    while running:
        if not cfg.data.get("email") or not cfg.data.get("panel_id"):
            system_state = "CONFIG_REQUIRED"
            await asyncio.sleep(5)
            continue

        # Initialize API if needed
        if not sector_api:
            sector_api = SectorAlarmAPI(
                cfg.data["email"], cfg.data["password"], 
                cfg.data["panel_id"], cfg.data.get("token")
            )

        # LOGIN CHECK
        if not sector_api.access_token or not await sector_api.validate_token():
            logger.info("Token invalid or expired. Attempting login...")
            login_result = await sector_api.login()
            
            if login_result == "SUCCESS":
                system_state = "CONNECTED"
                # Update token in config so it persists
                cfg.data["token"] = sector_api.access_token
                cfg.save()
            elif login_result == "2FA_REQUIRED":
                system_state = "WAITING_2FA"
                logger.warning("2FA Required. Pausing polling until code entered in Web UI.")
            else:
                system_state = "ERROR"
        
        # POLLING (Only if connected)
        if system_state == "CONNECTED":
            try:
                # 1. Logs/Status
                logs = await sector_api.get_logs()
                if logs:
                    last = logs[0].get("EventType", "")
                    status = "armed" if "armed" in last and "disarmed" not in last else "disarmed"
                    if "partial" in last: status = "partialarmed"
                    latest_data["status"] = status
                    mqtt_handler.publish_state(status)

                # 2. Temps & Hum
                temps = await sector_api.get_temperatures() or {}
                hums = await sector_api.get_humidity() or {}
                
                sensors = {} # Map serial -> {name, temp, hum}
                
                # Helper to process sensors
                def process_s(data, key):
                    for sec in data.get("Sections", []):
                        for p in sec.get("Places", []):
                            for c in p.get("Components", []):
                                if key in c:
                                    s, l, v = c["SerialNo"], c["Label"], c[key]
                                    if s not in sensors: sensors[s] = {"name": l, "serial": s}
                                    sensors[s][key.lower()] = v
                                    # Mqtt Publish immediately
                                    mqtt_handler.publish_sensor(s, l, "temp" if key=="Temperature" else "hum", v)

                process_s(temps, "Temperature")
                process_s(hums, "Humidity")
                
                latest_data["sensors"] = list(sensors.values())
                latest_data["last_update"] = time.strftime("%H:%M:%S")
            except Exception as e:
                logger.error(f"Poll Error: {e}")
                system_state = "ERROR"

        # Wait loop (skip heavy sleep if waiting for 2FA interaction)
        sleep_time = 60 if system_state == "CONNECTED" else 5
        await asyncio.sleep(sleep_time)

# --- LIFECYCLE ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global loop
    loop = asyncio.get_running_loop()
    mqtt_handler.start()
    asyncio.create_task(poll_sector())
    yield
    mqtt_handler.stop()
    if sector_api: await sector_api.close()

app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="app/templates")

# --- ROUTES ---
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request, "config": cfg.data, "data": latest_data, "state": system_state
    })

@app.post("/trigger_2fa")
async def trigger_2fa():
    """Manually triggers the login flow to send SMS."""
    global system_state
    if sector_api:
        res = await sector_api.login() # This triggers the SMS
        if res == "2FA_REQUIRED" or res == "SUCCESS":
            system_state = "WAITING_2FA" # Force UI to show input
            return RedirectResponse("/", status_code=303)
    return RedirectResponse("/", status_code=303)

@app.post("/submit_2fa")
async def submit_2fa(code: str = Form(...)):
    """Validates the code entered by user."""
    global system_state
    if sector_api:
        success = await sector_api.validate_2fa(code)
        if success:
            system_state = "CONNECTED"
            cfg.data["token"] = sector_api.access_token
            cfg.save()
        else:
            # Maybe log error or flash message
            pass
    return RedirectResponse("/", status_code=303)

@app.post("/save_config")
async def save_config(
    email: str = Form(...), password: str = Form(""), 
    panel_id: str = Form(...), panel_code: str = Form(""),
    mqtt_broker: str = Form(...), mqtt_port: int = Form(...)
):
    global sector_api, system_state
    cfg.data.update({
        "email": email, "password": password, "panel_id": panel_id, 
        "panel_code": panel_code, "mqtt_broker": mqtt_broker, "mqtt_port": mqtt_port
    })
    cfg.save()
    mqtt_handler.stop(); mqtt_handler.start()
    
    # Reset API
    if sector_api: await sector_api.close()
    sector_api = None
    system_state = "STARTING"
    
    return RedirectResponse(url="/", status_code=303)