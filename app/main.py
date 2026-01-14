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
                        self.data.update(loaded)
                        self.data["poll_interval"] = int(self.data.get("poll_interval", 60))
                        self.data["mqtt_port"] = int(self.data.get("mqtt_port", 1883))
            except Exception as e:
                logger.error(f"Config Load Error: {e}")

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            with open(self.filepath, 'w') as f:
                yaml.dump(self.data, f, default_flow_style=False)
            print(f"DEBUG: Config saved to {self.filepath}")
        except Exception as e:
            logger.error(f"Config Save Error: {e}")

cfg = ConfigManager(CONFIG_FILE)

# --- MQTT HANDLER ---
class MqttHandler:
    def __init__(self):
        self.client = mqtt_client.Client()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.discovery_sent = False

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
            # We try to publish discovery immediately, but if PanelID is missing,
            # the poll loop will retry it later.
            self.publish_discovery()
        else:
            print(f"DEBUG: MQTT Connect Failed code={rc}")

    def on_message(self, client, userdata, msg):
        # Handle commands from HA (e.g., Arm/Disarm)
        try:
            payload = msg.payload.decode().upper()
            print(f"DEBUG: MQTT Command Received: {payload}")
            # Logic to trigger sector_api.arm_system would go here
            # using asyncio.run_coroutine_threadsafe to call the async API
        except: pass

    def publish_discovery(self):
        p_id = cfg.data.get("panel_id")
        if not p_id: 
            return # Can't discover without Panel ID
            
        disc = cfg.data.get("discovery_prefix", "homeassistant")
        base = cfg.data.get("mqtt_prefix", "sector")
        
        # --- Alarm Panel Discovery ---
        dev = {
            "identifiers": [f"sa_{p_id}"], 
            "name": "Sector Alarm", 
            "manufacturer": "Sector Alarm",
            "model": "Hub"
        }
        
        p = {
            "name": "Sector Alarm Panel", 
            "unique_id": f"sa_panel_{p_id}", 
            "command_topic": f"{base}/{p_id}/set", 
            "state_topic": f"{base}/{p_id}/state",
            "code_arm_required": False,
            "code_disarm_required": False, # We handle code in the bridge config
            "device": dev
        }
        
        print(f"DEBUG: Sending Alarm Discovery for sa_{p_id}")
        self.client.publish(f"{disc}/alarm_control_panel/sa_{p_id}/config", json.dumps(p), retain=True)
        self.discovery_sent = True

    def publish_sensor(self, serial, name, type_, val):
        disc = cfg.data.get("discovery_prefix", "homeassistant")
        base = cfg.data.get("mqtt_prefix", "sector")
        clean = serial.replace(":", "")
        p_id = cfg.data.get("panel_id")
        
        dev = {"identifiers": [f"sa_dev_{serial}"], "name": name, "via_device": f"sa_{p_id}"}
        
        t_conf = {
            "temp": {"u": "°C", "c": "temperature", "t": "temperature"},
            "hum": {"u": "%", "c": "humidity", "t": "humidity"}
        }[type_]
        
        p = {
            "name": f"{name} {type_.title()}", "unique_id": f"sa_{clean}_{type_}",
            "state_topic": f"{base}/sensor/{clean}/state", "unit_of_measurement": t_conf['u'],
            "device_class": t_conf['c'], "value_template": f"{{{{ value_json.{t_conf['t']} }}}}", "device": dev
        }
        # Publish Config
        self.client.publish(f"{disc}/sensor/sa_{clean}_{type_}/config", json.dumps(p), retain=True)
        # Publish State (RETAINED)
        self.client.publish(f"{base}/sensor/{clean}/state", json.dumps({t_conf['t']: val}), retain=True)

    def publish_state(self, state):
        base = cfg.data.get("mqtt_prefix", "sector")
        # Map Sector states to HA states
        # armed = armed_away, partialarmed = armed_home
        ha_state = "disarmed"
        if state == "armed": ha_state = "armed_away"
        elif state == "partialarmed": ha_state = "armed_home"
        
        print(f"DEBUG: Publishing Alarm State: {ha_state} (Retained)")
        # RETAIN=True is critical for HA to see state after restart
        self.client.publish(f"{base}/{cfg.data['panel_id']}/state", ha_state, retain=True)

mqtt_handler = MqttHandler()

# --- BACKGROUND POLLING ---
async def poll_sector():
    global sector_api, latest_data, system_state
    
    while running:
        if not cfg.data.get("email") or not cfg.data.get("panel_id"):
            system_state = "CONFIG_REQUIRED"
            await asyncio.sleep(5)
            continue

        # Retry discovery if it wasn't sent yet (e.g. config was missing on boot)
        if not mqtt_handler.discovery_sent:
            mqtt_handler.publish_discovery()

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
                    # Re-send discovery on fresh login just in case
                    mqtt_handler.publish_discovery()
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

        # Dynamic Sleep
        interval = int(cfg.data.get("poll_interval", 60))
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
    if not sector_api:
        sector_api = SectorAlarmAPI(
            cfg.data["email"], cfg.data["password"], 
            cfg.data["panel_id"], cfg.data.get("token")
        )

    res = await sector_api.login(force=True)
    if res == "2FA_REQUIRED":
        system_state = "WAITING_2FA"
    elif res == "SUCCESS":
        system_state = "CONNECTED"
        cfg.data["token"] = sector_api.access_token
        cfg.save()
    
    return RedirectResponse("/", status_code=303)

@app.post("/submit_2fa")
async def submit_2fa(code: str = Form(...)):
    global system_state
    if sector_api:
        success = await sector_api.validate_2fa(code)
        if success:
            system_state = "CONNECTED"
            cfg.data["token"] = sector_api.access_token
            cfg.save()
            mqtt_handler.publish_discovery() # Refresh discovery on new login
    return RedirectResponse("/", status_code=303)

@app.post("/save_config")
async def save_config(
    email: str = Form(...), password: str = Form(""), 
    panel_id: str = Form(...), panel_code: str = Form(""),
    mqtt_broker: str = Form(...), mqtt_port: int = Form(...),
    discovery_prefix: str = Form(...), poll_interval: int = Form(...)
):
    global sector_api, system_state
    
    cfg.data["token"] = "" 
    cfg.data.update({
        "email": email, "password": password, "panel_id": panel_id, 
        "panel_code": panel_code, "mqtt_broker": mqtt_broker, "mqtt_port": int(mqtt_port),
        "discovery_prefix": discovery_prefix, "poll_interval": int(poll_interval)
    })
    cfg.save()
    
    mqtt_handler.stop(); mqtt_handler.start()
    if sector_api: await sector_api.close()
    sector_api = None
    system_state = "STARTING"
    
    return RedirectResponse(url="/", status_code=303)