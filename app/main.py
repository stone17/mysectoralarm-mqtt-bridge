import asyncio
import json
import logging
import os
import yaml
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
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
system_state = "STARTING"
running = True

# --- CONFIG MANAGER ---
class ConfigManager:
    def __init__(self, filepath):
        self.filepath = filepath
        # Default values
        self.data = {
            "mqtt_broker": os.getenv("MQTT_BROKER", "192.168.0.100"),
            "mqtt_port": int(os.getenv("MQTT_PORT", 1883)),
            "mqtt_prefix": "sector",
            "discovery_prefix": "homeassistant",
            "poll_interval": 60,
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
                    if loaded: 
                        # Update defaults with loaded data
                        self.data.update(loaded)
                        # Ensure types are correct for critical fields
                        self.data["poll_interval"] = int(self.data.get("poll_interval", 60))
                        self.data["mqtt_port"] = int(self.data.get("mqtt_port", 1883))
            except Exception as e:
                logger.error(f"Config Load Error: {e}")

    def save(self):
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            with open(self.filepath, 'w') as f:
                yaml.dump(self.data, f, default_flow_style=False)
            print(f"DEBUG: Config saved to {self.filepath}: {self.data}")
        except Exception as e:
            logger.error(f"Config Save Error: {e}")

cfg = ConfigManager(CONFIG_FILE)

# --- MQTT HANDLER ---
class MqttHandler:
    def __init__(self):
        self.client = mqtt_client.Client()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def start(self):
        try:
            broker = cfg.data.get('mqtt_broker')
            if broker:
                print(f"DEBUG: MQTT Connecting to {broker}...")
                self.client.connect(broker, int(cfg.data.get('mqtt_port', 1883)), 60)
                self.client.loop_start()
        except Exception as e:
            logger.error(f"MQTT Connect Failed: {e}")

    def stop(self):
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except: pass

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            print("DEBUG: MQTT Connected!")
            base = cfg.data.get("mqtt_prefix", "sector")
            client.subscribe(f"{base}/+/set")
            self.publish_discovery()
        else:
            print(f"DEBUG: MQTT Connect Failed code={rc}")

    def on_message(self, client, userdata, msg):
        pass

    def publish_discovery(self):
        p_id = cfg.data.get("panel_id")
        if not p_id: return
        disc = cfg.data.get("discovery_prefix", "homeassistant")
        base = cfg.data.get("mqtt_prefix", "sector")
        
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

        if not sector_api:
            sector_api = SectorAlarmAPI(cfg.data["email"], cfg.data["password"], cfg.data["panel_id"], cfg.data.get("token"))

        # LOGIN CHECK
        if system_state != "WAITING_2FA":
            if not sector_api.access_token or not await sector_api.validate_token():
                print("DEBUG: Loop needs login...")
                login_result = await sector_api.login(force=False)
                
                if login_result == "SUCCESS":
                    system_state = "CONNECTED"
                    cfg.data["token"] = sector_api.access_token
                    cfg.save()
                elif login_result == "2FA_REQUIRED":
                    system_state = "WAITING_2FA"
                    print("DEBUG: Loop paused. Waiting for 2FA.")
                else:
                    system_state = "ERROR"
            else:
                system_state = "CONNECTED"
        
        # FETCH DATA
        if system_state == "CONNECTED":
            try:
                logs = await sector_api.get_logs()
                if logs:
                    last = logs[0].get("EventType", "")
                    status = "armed" if "armed" in last and "disarmed" not in last else "disarmed"
                    if "partial" in last: status = "partialarmed"
                    latest_data["status"] = status
                    mqtt_handler.publish_state(status)

                temps = await sector_api.get_temperatures() or {}
                hums = await sector_api.get_humidity() or {}
                
                sensors = {} 
                def process_s(data, key):
                    for sec in data.get("Sections", []):
                        for p in sec.get("Places", []):
                            for c in p.get("Components", []):
                                if key in c:
                                    s, l, v = c["SerialNo"], c["Label"], c[key]
                                    if s not in sensors: sensors[s] = {"name": l, "serial": s}
                                    sensors[s][key.lower()] = v
                                    mqtt_handler.publish_sensor(s, l, "temp" if key=="Temperature" else "hum", v)

                process_s(temps, "Temperature")
                process_s(hums, "Humidity")
                
                latest_data["sensors"] = list(sensors.values())
                latest_data["last_update"] = time.strftime("%H:%M:%S")
            except Exception as e:
                print(f"DEBUG: Poll Exception: {e}")
                # Don't set ERROR immediately on one poll fail

        # Dynamic Sleep
        interval = int(cfg.data.get("poll_interval", 60))
        print(f"DEBUG: Sleeping for {interval}s")
        sleep_time = interval if system_state == "CONNECTED" else 5
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

@app.get("/api/status")
async def api_status():
    return JSONResponse({"state": system_state})

@app.post("/trigger_2fa")
async def trigger_2fa():
    global system_state, sector_api
    print("DEBUG: Manual 2FA Trigger Button Clicked.")
    
    if not sector_api:
        sector_api = SectorAlarmAPI(
            cfg.data["email"], cfg.data["password"], 
            cfg.data["panel_id"], cfg.data.get("token")
        )

    res = await sector_api.login(force=True)
    print(f"DEBUG: Manual Trigger Result: {res}")
    
    if res == "2FA_REQUIRED":
        system_state = "WAITING_2FA"
    elif res == "SUCCESS":
        print("DEBUG: Login succeeded immediately. Connecting...")
        system_state = "CONNECTED"
        cfg.data["token"] = sector_api.access_token
        cfg.save()
    
    return RedirectResponse("/", status_code=303)

@app.post("/submit_2fa")
async def submit_2fa(code: str = Form(...)):
    global system_state
    print(f"DEBUG: Submitting Code {code}")
    if sector_api:
        success = await sector_api.validate_2fa(code)
        if success:
            system_state = "CONNECTED"
            cfg.data["token"] = sector_api.access_token
            cfg.save()
    return RedirectResponse("/", status_code=303)

@app.post("/save_config")
async def save_config(
    email: str = Form(...), password: str = Form(""), 
    panel_id: str = Form(...), panel_code: str = Form(""),
    mqtt_broker: str = Form(...), mqtt_port: int = Form(...),
    discovery_prefix: str = Form(...), poll_interval: int = Form(...)
):
    global sector_api, system_state
    
    # 1. Clear the token because credentials might have changed
    cfg.data["token"] = "" 
    
    # 2. Update Data (Force int for numbers)
    cfg.data.update({
        "email": email, 
        "password": password, 
        "panel_id": panel_id, 
        "panel_code": panel_code, 
        "mqtt_broker": mqtt_broker, 
        "mqtt_port": int(mqtt_port),
        "discovery_prefix": discovery_prefix, 
        "poll_interval": int(poll_interval)
    })
    
    # 3. Save
    print(f"DEBUG: Saving Config -> {cfg.data}")
    cfg.save()
    
    # 4. Restart Services
    mqtt_handler.stop(); mqtt_handler.start()
    
    if sector_api: await sector_api.close()
    sector_api = None
    system_state = "STARTING"
    
    return RedirectResponse(url="/", status_code=303)