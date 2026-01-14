import asyncio
import json
import logging
import os
import yaml
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, BackgroundTasks
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
            "email": "",
            "password": "",
            "panel_id": "",
            "panel_code": "", # For arming/disarming
            "token": "" # Manual token for 2FA bypass
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

# --- MQTT HANDLER ---
class MqttHandler:
    def __init__(self):
        self.client = mqtt_client.Client()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def start(self):
        try:
            broker = cfg.data['mqtt_broker']
            port = cfg.data['mqtt_port']
            logger.info(f"Connecting to MQTT {broker}:{port}")
            self.client.connect(broker, port, 60)
            self.client.loop_start()
        except Exception as e:
            logger.error(f"MQTT Start Error: {e}")

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            base = cfg.data.get("mqtt_prefix", "sector")
            logger.info(f"MQTT Connected. Listening on {base}/+/set")
            self.client.subscribe(f"{base}/+/set")
            self.publish_discovery()

    def on_message(self, client, userdata, msg):
        # Handle Arm/Disarm commands via MQTT
        # Topic: sector/{panel_id}/set Payload: ARM_HOME, ARM_AWAY, DISARM
        try:
            payload = msg.payload.decode().upper()
            logger.info(f"Received MQTT Command: {payload}")
            # Logic to call sector_api.arm_system would go here
            # For security, ensure 'panel_code' is set in config
            if cfg.data.get("panel_code"):
                mode = None
                if payload == "ARM_AWAY": mode = "Total"
                elif payload == "ARM_HOME": mode = "Partial"
                elif payload == "DISARM": mode = "Disarm"
                
                if mode and sector_api:
                    asyncio.run_coroutine_threadsafe(sector_api.arm_system(cfg.data["panel_code"], mode), loop)
        except Exception as e:
            logger.error(f"MQTT Msg Error: {e}")

    def publish_discovery(self):
        p_id = cfg.data.get("panel_id")
        if not p_id: return
        
        disc = cfg.data.get("discovery_prefix", "homeassistant")
        base = cfg.data.get("mqtt_prefix", "sector")
        
        # 1. Alarm Control Panel
        dev_info = {"identifiers": [f"sa_{p_id}"], "name": "Sector Alarm", "manufacturer": "Sector Alarm"}
        payload_alarm = {
            "name": "Sector Alarm Panel",
            "unique_id": f"sa_panel_{p_id}",
            "command_topic": f"{base}/{p_id}/set",
            "state_topic": f"{base}/{p_id}/state",
            "device": dev_info
        }
        self.client.publish(f"{disc}/alarm_control_panel/sa_{p_id}/config", json.dumps(payload_alarm), retain=True)

    def publish_sensor_discovery(self, sensor_serial, name, type_):
        # Dynamic discovery for sensors found during polling
        disc = cfg.data.get("discovery_prefix", "homeassistant")
        base = cfg.data.get("mqtt_prefix", "sector")
        p_id = cfg.data.get("panel_id")
        
        dev_info = {"identifiers": [f"sa_dev_{sensor_serial}"], "name": name, "via_device": f"sa_{p_id}"}
        
        clean_serial = sensor_serial.replace(":", "")

        if type_ == "temp":
            payload = {
                "name": f"{name} Temperature",
                "unique_id": f"sa_{clean_serial}_temp",
                "state_topic": f"{base}/sensor/{clean_serial}/state",
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "value_template": "{{ value_json.temperature }}",
                "device": dev_info
            }
            self.client.publish(f"{disc}/sensor/sa_{clean_serial}_temp/config", json.dumps(payload), retain=True)

        elif type_ == "hum":
            payload = {
                "name": f"{name} Humidity",
                "unique_id": f"sa_{clean_serial}_hum",
                "state_topic": f"{base}/sensor/{clean_serial}/state",
                "unit_of_measurement": "%",
                "device_class": "humidity",
                "value_template": "{{ value_json.humidity }}",
                "device": dev_info
            }
            self.client.publish(f"{disc}/sensor/sa_{clean_serial}_hum/config", json.dumps(payload), retain=True)


    def publish_state(self, p_id, state):
        base = cfg.data.get("mqtt_prefix", "sector")
        # Map Sector states to HA states
        # armed, disarmed, partialarmed
        ha_state = "disarmed"
        if state == "armed": ha_state = "armed_away"
        elif state == "partialarmed": ha_state = "armed_home"
        
        self.client.publish(f"{base}/{p_id}/state", ha_state)

    def publish_sensor_data(self, serial, data):
        base = cfg.data.get("mqtt_prefix", "sector")
        clean_serial = serial.replace(":", "")
        self.client.publish(f"{base}/sensor/{clean_serial}/state", json.dumps(data))

mqtt_handler = MqttHandler()

# --- BACKGROUND POLLING ---
async def poll_sector():
    global sector_api, latest_data
    while running:
        if cfg.data.get("email") and cfg.data.get("panel_id"):
            if not sector_api:
                sector_api = SectorAlarmAPI(
                    cfg.data["email"], cfg.data["password"], 
                    cfg.data["panel_id"], cfg.data.get("token")
                )
                if not await sector_api.login():
                    logger.error("Login failed. Check credentials or 2FA.")
            
            try:
                # 1. Get Logs for Alarm Status
                logs = await sector_api.get_logs()
                if logs and len(logs) > 0:
                    last_event = logs[0].get("EventType", "")
                    status = "disarmed"
                    if "armed" in last_event and "partial" not in last_event: status = "armed"
                    elif "partial" in last_event: status = "partialarmed"
                    elif "disarmed" in last_event: status = "disarmed"
                    
                    latest_data["status"] = status
                    mqtt_handler.publish_state(cfg.data["panel_id"], status)

                # 2. Get Temperatures
                temps = await sector_api.get_temperatures()
                temp_map = {}
                if temps:
                    for section in temps.get("Sections", []):
                        for place in section.get("Places", []):
                            for comp in place.get("Components", []):
                                if "Temperature" in comp:
                                    serial = comp["SerialNo"]
                                    val = comp["Temperature"]
                                    label = comp["Label"]
                                    temp_map[serial] = {"val": val, "label": label}
                                    mqtt_handler.publish_sensor_discovery(serial, label, "temp")

                # 3. Get Humidity
                hums = await sector_api.get_humidity()
                hum_map = {}
                if hums:
                    for section in hums.get("Sections", []):
                        for place in section.get("Places", []):
                            for comp in place.get("Components", []):
                                if "Humidity" in comp:
                                    serial = comp["SerialNo"]
                                    val = comp["Humidity"]
                                    label = comp["Label"]
                                    hum_map[serial] = {"val": val, "label": label}
                                    mqtt_handler.publish_sensor_discovery(serial, label, "hum")

                # Merge and Publish
                all_serials = set(temp_map.keys()) | set(hum_map.keys())
                combined_list = []
                for s in all_serials:
                    name = temp_map.get(s, {}).get("label") or hum_map.get(s, {}).get("label") or "Unknown"
                    payload = {}
                    if s in temp_map: payload["temperature"] = float(temp_map[s]["val"])
                    if s in hum_map: payload["humidity"] = int(hum_map[s]["val"])
                    
                    mqtt_handler.publish_sensor_data(s, payload)
                    
                    combined_list.append({"name": name, "serial": s, **payload})
                
                latest_data["sensors"] = combined_list
                latest_data["last_update"] = time.strftime("%H:%M:%S")

            except Exception as e:
                logger.error(f"Polling Error: {e}")
                sector_api = None # Force re-login next time

        await asyncio.sleep(60)

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
        "request": request, 
        "config": cfg.data,
        "data": latest_data
    })

@app.post("/save_config")
async def save_config(
    email: str = Form(...), password: str = Form(""), 
    panel_id: str = Form(...), panel_code: str = Form(""),
    token: str = Form(""),
    mqtt_broker: str = Form(...), mqtt_port: int = Form(...)
):
    global sector_api
    cfg.data.update({
        "email": email, "password": password, "panel_id": panel_id, 
        "panel_code": panel_code, "token": token,
        "mqtt_broker": mqtt_broker, "mqtt_port": mqtt_port
    })
    cfg.save()
    
    # Restart MQTT and API
    mqtt_handler.stop()
    mqtt_handler.start()
    if sector_api: await sector_api.close()
    sector_api = None # Forces re-login in poll loop
    
    return RedirectResponse(url="/", status_code=303)