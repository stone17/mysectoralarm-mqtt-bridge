import asyncio
import collections
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
from app import security

# --- CONFIG & LOGGING ---
CONFIG_FILE = os.getenv("CONFIG_PATH", "/config/sector_config.yaml")
STATE_FILE = os.getenv("STATE_PATH", "/config/state.json")

class StateManager:
    def __init__(self, filepath):
        self.filepath = filepath
        self.data = {
            "latest_data": {"status": "Unknown", "temps": [], "humidity": []},
            "backoff_multiplier": 1,
            "last_poll_try": 0,
            "polls_hour": 0,
            "polls_day": 0,
            "current_hour": "",
            "current_day": ""
        }
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r') as f:
                    file_data = json.load(f)
                    for key in self.data.keys():
                        if key in file_data:
                            self.data[key] = file_data[key]
            except Exception as e:
                logger.error(f"State Load Error: {e}")

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            with open(self.filepath, 'w') as f:
                json.dump(self.data, f)
        except Exception as e:
            logger.error(f"State Save Error: {e}")

state_mgr = StateManager(STATE_FILE)

class MemoryLogHandler(logging.Handler):
    def __init__(self, capacity=500):
        super().__init__()
        self.logs = collections.deque(maxlen=capacity)

    def emit(self, record):
        self.logs.append(self.format(record))

memory_handler = MemoryLogHandler()
memory_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("SectorBridge")
logger.setLevel(logging.DEBUG)
logger.addHandler(memory_handler)
# Also attach to sector module logger to capture its output
logging.getLogger("app.sector").addHandler(memory_handler)
logging.getLogger("app.sector").setLevel(logging.DEBUG)

# --- GLOBAL STATE ---
sector_api: SectorAlarmAPI = None
latest_data = state_mgr.data["latest_data"]
system_state = "STARTING"
running = True
poll_wakeup = asyncio.Event()

class ConfigManager:
    def __init__(self, filepath):
        self.filepath = filepath
        self.data = {
            "mqtt_broker": os.getenv("MQTT_BROKER", "192.168.0.100"),
            "mqtt_port": int(os.getenv("MQTT_PORT", 1883)),
            "mqtt_username": os.getenv("MQTT_USERNAME", ""),
            "mqtt_password": os.getenv("MQTT_PASSWORD", ""),
            "mqtt_prefix": "sector",
            "discovery_prefix": "homeassistant",
            "poll_interval": 60,
            "rate_limit_strategy": "wait_next_hour",
            "email": "",
            "password": "",
            "panel_id": "",
            "panel_code": "",
            "token": ""
        }
        self.load()
        # Save immediately to ensure file exists and passwords get encrypted if they were defaults/env vars
        self.save()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r') as f:
                    file_data = yaml.safe_load(f) or {}

                # Fields that require decryption
                sensitive_fields = ["mqtt_password", "password", "panel_code"]
                
                # Merge non-sensitive fields directly
                for k, v in file_data.items():
                    if k not in sensitive_fields:
                        self.data[k] = v
                
                # Handle Passwords: Try decrypt, fallback to plain (migration support)
                for field in sensitive_fields:
                    raw_val = file_data.get(field)
                    if raw_val:
                        decrypted = security.decrypt_password(raw_val)
                        if decrypted:
                            self.data[field] = decrypted
                        else:
                            # If decrypt fails, assume it's plain text (user edited file manually)
                            self.data[field] = raw_val

                # Type conversions
                self.data["poll_interval"] = int(self.data.get("poll_interval", 60))
                self.data["mqtt_port"] = int(self.data.get("mqtt_port", 1883))
                if "panel_id" in self.data:
                    self.data["panel_id"] = str(self.data["panel_id"])
                            
            except Exception as e:
                logger.error(f"Config Load Error: {e}")

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            
            # Create a copy to modify for storage without affecting running app
            storage_data = self.data.copy()
            
            # Encrypt sensitive fields before writing to disk
            sensitive_fields = ["mqtt_password", "password", "panel_code"]
            
            for field in sensitive_fields:
                plain = storage_data.get(field)
                if plain:
                    encrypted = security.encrypt_password(plain)
                    if encrypted:
                        storage_data[field] = encrypted
                    else:
                        logger.error(f"Failed to encrypt {field}, not saving it to avoid leak.")
                        del storage_data[field]

            with open(self.filepath, 'w') as f:
                yaml.dump(storage_data, f, default_flow_style=False)
            logger.debug(f"Config saved to {self.filepath}")
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
                logger.debug(f"MQTT Connecting to {broker}...")
                
                # Set Username/Password if configured
                user = cfg.data.get('mqtt_username')
                pwd = cfg.data.get('mqtt_password')
                if user and pwd:
                    self.client.username_pw_set(user, pwd)

                self.client.connect(broker, int(cfg.data.get('mqtt_port', 1883)), 60)
                # Set Last Will (Availability)
                base = cfg.data.get("mqtt_prefix", "sector")
                self.client.will_set(f"{base}/bridge/status", "offline", retain=True)
                self.client.loop_start()
        except Exception as e:
            logger.error(f"MQTT Connect Failed: {e}")

    def stop(self):
        try:
            base = cfg.data.get("mqtt_prefix", "sector")
            self.client.publish(f"{base}/bridge/status", "offline", retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except: pass

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("MQTT Connected!")
            base = cfg.data.get("mqtt_prefix", "sector")
            
            # Subscribe to command topics
            client.subscribe(f"{base}/+/set")          # Alarm Panel commands
            client.subscribe(f"{base}/+/set_switch")   # Switch commands
            client.subscribe(f"{base}/bridge/force_update") # Force update command
            
            # Publish Online Status
            client.publish(f"{base}/bridge/status", "online", retain=True)
            
            self.publish_discovery()
        else:
            logger.error(f"MQTT Connect Failed code={rc}")

    def on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode().upper()
            topic = msg.topic
            logger.debug(f"MQTT Command Received: {payload} on {topic}")
            
            if topic.endswith("/bridge/force_update"):
                if payload == "UPDATE":
                    logger.info("MQTT Command: Force Update")
                    poll_wakeup.set()
                return
            
            if cfg.data.get("panel_code") and sector_api:
                mode = None
                
                # Handle Standard Alarm Panel Commands
                if payload == "ARM_AWAY": mode = "Total"
                elif payload == "ARM_HOME": mode = "Partial"
                elif payload == "DISARM": mode = "Disarm"
                
                # Handle Simple Switch Commands (ON=Arm Total, OFF=Disarm)
                elif payload == "ON": mode = "Total"
                elif payload == "OFF": mode = "Disarm"
                
                if mode:
                    logger.info(f"Executing Sector Action: {mode}")
                    asyncio.run_coroutine_threadsafe(sector_api.arm_system(cfg.data["panel_code"], mode), loop)
        except Exception as e: 
            logger.error(f"MQTT Message Error: {e}")

    def publish_discovery(self):
        p_id = str(cfg.data.get("panel_id", ""))
        if not p_id: return 
            
        disc = cfg.data.get("discovery_prefix", "homeassistant")
        base = cfg.data.get("mqtt_prefix", "sector")
        
        # Device Info (Shared)
        dev = {
            "identifiers": [f"sa_{p_id}"], 
            "name": "Sector Alarm", 
            "manufacturer": "Sector Alarm",
            "model": "Hub",
            "sw_version": "1.0"
        }
        
        # 1. Alarm Panel Entity (Best for Home Assistant)
        p_alarm = {
            "name": "Sector Alarm Panel", 
            "unique_id": f"sa_panel_{p_id}", 
            "command_topic": f"{base}/{p_id}/set", 
            "state_topic": f"{base}/{p_id}/state",
            "availability_topic": f"{base}/bridge/status",
            "code_arm_required": False,
            "code_disarm_required": False,
            "payload_disarm": "DISARM",
            "payload_arm_home": "ARM_HOME",
            "payload_arm_away": "ARM_AWAY",
            "device": dev
        }
        self.client.publish(f"{disc}/alarm_control_panel/sa_{p_id}/config", json.dumps(p_alarm), retain=True)

        # 2. Simple Switch Entity (Best for Domoticz/Fallback)
        p_switch = {
            "name": "Sector Alarm Toggle",
            "unique_id": f"sa_switch_{p_id}",
            "command_topic": f"{base}/{p_id}/set_switch",
            "state_topic": f"{base}/{p_id}/state_switch",
            "availability_topic": f"{base}/bridge/status",
            "payload_on": "ON",
            "payload_off": "OFF",
            "icon": "mdi:shield-home",
            "device": dev
        }
        self.client.publish(f"{disc}/switch/sa_{p_id}_switch/config", json.dumps(p_switch), retain=True)

        # 3. Force Update Button Entity
        p_button = {
            "name": "Sector Alarm Force Update",
            "unique_id": f"sa_button_{p_id}_update",
            "command_topic": f"{base}/bridge/force_update",
            "availability_topic": f"{base}/bridge/status",
            "payload_press": "UPDATE",
            "icon": "mdi:update",
            "device": dev
        }
        self.client.publish(f"{disc}/button/sa_{p_id}_update/config", json.dumps(p_button), retain=True)

    def publish_sensor(self, serial, name, type_, val):
        disc = cfg.data.get("discovery_prefix", "homeassistant")
        base = cfg.data.get("mqtt_prefix", "sector")
        clean = serial.replace(":", "")
        p_id = str(cfg.data.get("panel_id"))
        
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
        self.client.publish(f"{disc}/sensor/sa_{clean}_{type_}/config", json.dumps(p), retain=True)
        self.client.publish(f"{base}/sensor/{clean}/state", json.dumps({t_conf['t']: val}), retain=True)

    def publish_state(self, state):
        base = cfg.data.get("mqtt_prefix", "sector")
        p_id = str(cfg.data.get("panel_id"))
        
        # 1. Update Alarm Panel Topic
        ha_state = "disarmed"
        if state == "armed": ha_state = "armed_away"
        elif state == "partialarmed": ha_state = "armed_home"
        
        self.client.publish(f"{base}/{p_id}/state", ha_state, retain=True)

        # 2. Update Switch Topic
        sw_state = "ON" if state in ["armed", "partialarmed"] else "OFF"
        self.client.publish(f"{base}/{p_id}/state_switch", sw_state, retain=True)
        
        logger.debug(f"Published State: Panel={ha_state}, Switch={sw_state}")

mqtt_handler = MqttHandler()

# --- BACKGROUND POLLING ---
async def poll_sector():
    global sector_api, latest_data, system_state
    first_run = True
    
    while running:
        if not cfg.data.get("email") or not cfg.data.get("panel_id"):
            system_state = "CONFIG_REQUIRED"
            try:
                await asyncio.wait_for(poll_wakeup.wait(), timeout=5)
                poll_wakeup.clear()
            except asyncio.TimeoutError:
                pass
            continue

        interval = int(cfg.data.get("poll_interval", 60))
        
        if first_run:
            first_run = False
            now = int(time.time() * 1000)
            next_retry_ms = latest_data.get("next_retry")
            if next_retry_ms and next_retry_ms > now:
                system_state = "RATE_LIMITED"
                remaining_s = (next_retry_ms - now) / 1000.0
                logger.info(f"Resuming wait from previous run. Waiting {remaining_s:.1f}s.")
                try:
                    await asyncio.wait_for(poll_wakeup.wait(), timeout=remaining_s)
                    poll_wakeup.clear()
                except asyncio.TimeoutError:
                    pass
        
        import datetime
        now_dt = datetime.datetime.now()
        current_hour_str = now_dt.strftime("%Y-%m-%d %H")
        current_day_str = now_dt.strftime("%Y-%m-%d")
        
        if state_mgr.data.get("current_hour") != current_hour_str:
            state_mgr.data["current_hour"] = current_hour_str
            state_mgr.data["polls_hour"] = 0
        if state_mgr.data.get("current_day") != current_day_str:
            state_mgr.data["current_day"] = current_day_str
            state_mgr.data["polls_day"] = 0
            
        state_mgr.data["polls_hour"] = state_mgr.data.get("polls_hour", 0) + 1
        state_mgr.data["polls_day"] = state_mgr.data.get("polls_day", 0) + 1
        
        state_mgr.data["last_poll_try"] = int(time.time() * 1000)
        state_mgr.save()
        latest_data["next_retry"] = None

        if not sector_api:
            sector_api = SectorAlarmAPI(cfg.data["email"], cfg.data["password"], cfg.data["panel_id"], cfg.data.get("token"))

        # LOGIN CHECK
        if system_state != "WAITING_2FA":
            val_res = await sector_api.validate_token()
            if val_res == "RATE_LIMITED":
                system_state = "RATE_LIMITED"
            elif not sector_api.access_token or val_res == "INVALID":
                logger.info("Loop needs login...")
                login_result = await sector_api.login(force=False)
                
                if login_result == "SUCCESS":
                    system_state = "CONNECTED"
                    cfg.data["token"] = sector_api.access_token
                    cfg.save()
                    mqtt_handler.publish_discovery()
                elif login_result == "RATE_LIMITED":
                    system_state = "RATE_LIMITED"
                elif login_result == "2FA_REQUIRED":
                    system_state = "WAITING_2FA"
                    logger.warning("Loop paused. Waiting for 2FA.")
                else:
                    system_state = "ERROR"
            else:
                if system_state == "RATE_LIMITED":
                    system_state = "CONNECTED"
                elif system_state != "CONNECTED":
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
                    latest_data["raw_logs"] = logs
                    state_mgr.save()
                    
                    mqtt_handler.publish_discovery() # Ensure discovery is fresh
                    mqtt_handler.publish_state(status)
            except Exception as e:
                if str(e) == "RATE_LIMITED":
                    system_state = "RATE_LIMITED"
                else:
                    logger.error(f"Logs Poll Exception: {e}")

            try:
                temps = await sector_api.get_temperatures() or {}
                hums = await sector_api.get_humidity() or {}
                
                latest_data["raw_temps"] = temps
                latest_data["raw_hums"] = hums
                
                sensors = {} 
                def process_s(data, key):
                    if not data: return
                    components = []
                    if isinstance(data, list):
                        components = data
                    elif isinstance(data, dict):
                        for sec in data.get("Sections", []):
                            if isinstance(sec, dict):
                                for p in sec.get("Places", []):
                                    if isinstance(p, dict):
                                        components.extend(p.get("Components", []))
                        for fallback_key in ["Temperatures", "Humidity", "Components", "components"]:
                            if fallback_key in data and isinstance(data[fallback_key], list):
                                components.extend(data[fallback_key])
                    
                    for c in components:
                        if isinstance(c, dict) and key in c:
                            s = c.get("SerialNo") or c.get("Id") or ""
                            l = c.get("Label", "Unknown")
                            v = c[key]
                            if not s: continue
                            if s not in sensors: sensors[s] = {"name": l, "serial": s, "raw": {}}
                            sensors[s][key.lower()] = v
                            sensors[s]["raw"].update(c)
                            mqtt_handler.publish_sensor(s, l, "temp" if key=="Temperature" else "hum", v)

                process_s(temps, "Temperature")
                process_s(hums, "Humidity")
                
                latest_data["sensors"] = list(sensors.values())
                latest_data["last_update"] = int(time.time() * 1000)
                state_mgr.data["backoff_multiplier"] = 1
                state_mgr.save()
            except Exception as e:
                if str(e) == "RATE_LIMITED":
                    system_state = "RATE_LIMITED"
                else:
                    logger.error(f"Sensors Poll Exception: {e}")

        interval = int(cfg.data.get("poll_interval", 60))
        sleep_time = interval if system_state == "CONNECTED" else 5
        if system_state == "RATE_LIMITED":
            strategy = cfg.data.get("rate_limit_strategy", "wait_next_hour")
            if strategy == "wait_next_hour":
                now = time.time()
                sleep_time = 3600 - (now % 3600) + 60
                logger.info(f"Rate limited by API, waiting until next hour ({sleep_time:.0f}s).")
            else:
                current_multiplier = state_mgr.data.get("backoff_multiplier", 1)
                sleep_time = interval * current_multiplier
                logger.info(f"Rate limited by API, waiting {sleep_time}s before next attempt (multiplier: {current_multiplier}).")
                state_mgr.data["backoff_multiplier"] = min(current_multiplier * 2, 60)
            state_mgr.save()
            
        latest_data["next_retry"] = int(time.time() * 1000) + int(sleep_time * 1000)
        
        try:
            await asyncio.wait_for(poll_wakeup.wait(), timeout=sleep_time)
            poll_wakeup.clear()
        except asyncio.TimeoutError:
            pass

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
@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request, "config": cfg.data, "data": latest_data, "state": system_state, "state_mgr": state_mgr.data
        }
    )

@app.get("/api/status")
async def api_status():
    return JSONResponse({
        "state": system_state, 
        "last_update": latest_data.get("last_update"),
        "next_retry": latest_data.get("next_retry")
    })

@app.get("/api/logs")
async def api_logs():
    return JSONResponse({"logs": list(memory_handler.logs)})

@app.get("/api/debug_raw")
async def api_debug_raw():
    global sector_api
    if not sector_api:
        return JSONResponse({"error": "Not logged in"})
    
    try:
        logs = await sector_api.get_logs()
        temps = await sector_api.get_temperatures()
        hums = await sector_api.get_humidity()
        
        return JSONResponse({
            "logs": logs,
            "temps": temps,
            "hums": hums
        })
    except Exception as e:
        return JSONResponse({"error": str(e)})

@app.post("/override_status")
async def override_status(status: str = Form(...)):
    global latest_data
    if status in ["armed", "partialarmed", "disarmed"]:
        logger.info(f"Manual override of status to: {status}")
        latest_data["status"] = status
        latest_data["last_update"] = int(time.time() * 1000)
        state_mgr.save()
        mqtt_handler.publish_state(status)
    return RedirectResponse("/", status_code=303)

@app.post("/force_update")
async def force_update():
    logger.info("Manual Force Update Triggered.")
    poll_wakeup.set()
    return RedirectResponse("/", status_code=303)

@app.post("/reset_wait")
async def reset_wait():
    logger.info("Manual Reset Wait Triggered.")
    state_mgr.data["backoff_multiplier"] = 1
    state_mgr.save()
    poll_wakeup.set()
    return RedirectResponse("/", status_code=303)

@app.post("/trigger_2fa")
async def trigger_2fa():
    global system_state, sector_api
    logger.info("Manual 2FA Trigger Button Clicked.")
    
    if not sector_api:
        sector_api = SectorAlarmAPI(
            cfg.data["email"], cfg.data["password"], 
            cfg.data["panel_id"], cfg.data.get("token")
        )

    res = await sector_api.login(force=True)
    logger.debug(f"Manual Trigger Result: {res}")
    
    if res == "2FA_REQUIRED":
        system_state = "WAITING_2FA"
    elif res == "SUCCESS":
        logger.info("Login succeeded immediately. Connecting...")
        system_state = "CONNECTED"
        cfg.data["token"] = sector_api.access_token
        cfg.save()
    elif res == "RATE_LIMITED":
        system_state = "RATE_LIMITED"
    
    return RedirectResponse("/", status_code=303)

@app.post("/submit_2fa")
async def submit_2fa(code: str = Form(...)):
    global system_state
    logger.info(f"Submitting Code {code}")
    if sector_api:
        success = await sector_api.validate_2fa(code)
        if success:
            system_state = "CONNECTED"
            cfg.data["token"] = sector_api.access_token
            cfg.save()
            mqtt_handler.publish_discovery()
    return RedirectResponse("/", status_code=303)

@app.post("/save_config")
async def save_config(
    email: str = Form(...), password: str = Form(""), 
    panel_id: str = Form(...), panel_code: str = Form(""),
    mqtt_broker: str = Form(...), mqtt_port: int = Form(...),
    mqtt_username: str = Form(""), mqtt_password: str = Form(""),
    discovery_prefix: str = Form(...), poll_interval: int = Form(...),
    rate_limit_strategy: str = Form("wait_next_hour")
):
    global sector_api, system_state
    
    cfg.data["token"] = "" 
    cfg.data.update({
        "email": email, "password": password, "panel_id": str(panel_id), 
        "panel_code": panel_code, "mqtt_broker": mqtt_broker, "mqtt_port": int(mqtt_port),
        "mqtt_username": mqtt_username, "mqtt_password": mqtt_password,
        "discovery_prefix": discovery_prefix, "poll_interval": int(poll_interval),
        "rate_limit_strategy": rate_limit_strategy
    })
    
    logger.info("Saving Config...")
    cfg.save()
    
    mqtt_handler.stop(); mqtt_handler.start()
    if sector_api: await sector_api.close()
    sector_api = None
    system_state = "STARTING"
    
    return RedirectResponse(url="/", status_code=303)