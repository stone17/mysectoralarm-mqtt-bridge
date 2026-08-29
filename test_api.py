import asyncio
import json
import os
from app.main import ConfigManager
from app.sector import SectorAlarmAPI

async def main():
    config_path = os.getenv("CONFIG_PATH", "app/sector_config.yaml")
    cfg = ConfigManager(config_path)
    
    if not cfg.data.get("email") or not cfg.data.get("panel_id"):
        print("No email or panel_id in config. Cannot test.")
        return
        
    api = SectorAlarmAPI(
        cfg.data["email"], cfg.data["password"], 
        cfg.data["panel_id"], cfg.data.get("token")
    )
    
    print("Logging in...")
    login_result = await api.login(force=False)
    print(f"Login result: {login_result}")
    
    if login_result == "SUCCESS":
        print("\n--- GET LOGS ---")
        try:
            logs = await api.get_logs()
            print(json.dumps(logs, indent=2)[:1000] + ("..." if logs and len(json.dumps(logs)) > 1000 else ""))
        except Exception as e:
            print(f"Error getting logs: {e}")
            
        print("\n--- GET TEMPERATURES ---")
        try:
            temps = await api.get_temperatures()
            print(json.dumps(temps, indent=2)[:1000] + ("..." if temps and len(json.dumps(temps)) > 1000 else ""))
        except Exception as e:
            print(f"Error getting temps: {e}")
            
        print("\n--- GET HUMIDITY ---")
        try:
            hums = await api.get_humidity()
            print(json.dumps(hums, indent=2)[:1000] + ("..." if hums and len(json.dumps(hums)) > 1000 else ""))
        except Exception as e:
            print(f"Error getting hums: {e}")
            
    await api.close()

if __name__ == "__main__":
    asyncio.run(main())
