# Sector Alarm MQTT Bridge

A standalone bridge that connects your **Sector Alarm** system to **MQTT**, designed for easy integration with **Home Assistant** or other smart home platforms.

It features a **Web Dashboard** for configuration, status monitoring, and handling Two-Factor Authentication (2FA) challenges.

![Dashboard Preview](https://via.placeholder.com/800x400?text=Sector+Alarm+Bridge+Dashboard)

## ✨ Features

* **Real-time Status**: Polls alarm state, temperature, and humidity sensors.
* **MQTT Auto-Discovery**: Automatically creates devices in Home Assistant.
* **Web UI**: clean interface to view sensors and configure settings.
* **2FA Support**: 
    * Handles SMS verification directly via the Web UI.
    * Supports manual token bypass for complex login scenarios.
* **Dockerized**: Easy to deploy with Docker Compose.

## 🚀 Quick Start

### 1. Docker Compose
Create a `docker-compose.yaml` file:

```yaml
version: '3.8'
services:
  sector-bridge:
    image: sector-bridge:latest
    build: .
    container_name: sector-bridge
    restart: unless-stopped
    network_mode: host  # Recommended for local MQTT brokers
    volumes:
      - ./config:/config
    environment:
      - MQTT_BROKER=192.168.1.100
      - MQTT_PORT=1883
      - CONFIG_PATH=/config/sector_config.yaml
```

### 2. Run the Container
```bash
docker-compose up -d --build
```

### 3. Configure
Open your browser and navigate to `http://<YOUR_IP>:8000`.
1.  Click **⚙️ Settings**.
2.  Enter your MQTT Broker details.
3.  Enter your Sector Alarm email, password, and Panel ID.
4.  Click **Save & Restart**.

## 🔐 Two-Factor Authentication (2FA)
Sector Alarm often requires SMS verification. This bridge handles it gracefully:

1.  **Automatic Trigger**: If the logs show `WAITING_2FA` or the UI shows a warning, an SMS has been sent to your phone.
2.  **Enter Code**: Type the code into the input box on the Web UI.
3.  **Success**: The system saves the token and resumes polling automatically.

**Troubleshooting 2FA:**
If you don't receive an SMS, you can use the **Manual Trigger** button in the UI to force a login attempt. If you are already logged in (e.g. recognized IP), the system will simply connect without asking for a code.

## 🏠 Home Assistant Integration
The bridge publishes **Home Assistant Auto-Discovery** messages. 
* **Alarm Panel**: Appears as an `alarm_control_panel` entity.
* **Sensors**: Temperature and Humidity sensors appear as individual entities.

Ensure your Home Assistant MQTT integration has `discovery: true` enabled.

## 📡 MQTT Topics
If you want to use this with other systems (Domoticz, OpenHAB, Node-RED), here are the raw topics:

| Topic | Payload | Description |
| :--- | :--- | :--- |
| `sector/<panel_id>/state` | `armed_away`, `armed_home`, `disarmed` | Current alarm state |
| `sector/<panel_id>/set` | `ARM_AWAY`, `ARM_HOME`, `DISARM` | Command to change state |
| `sector/sensor/<serial>/state` | `{"temperature": 21.5}` | Sensor data |

## 🛠️ Development
To run locally without Docker:

```bash
# Install dependencies
pip install -r requirements.txt

# Run server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## 📄 License
MIT License