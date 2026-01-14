import aiohttp
import async_timeout
import logging
import json
from typing import Optional, Dict

_LOGGER = logging.getLogger(__name__)

API_URL = "https://mypagesapi.sectoralarm.net"

class SectorAlarmAPI:
    def __init__(self, email, password, panel_id, token=None):
        self.email = email
        self.password = password
        self.panel_id = panel_id
        self.access_token = token
        self.session: Optional[aiohttp.ClientSession] = None
        self.headers = {}
        if token:
             self.headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json", 
                "Content-Type": "application/json"
            }

    async def ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def login(self):
        """Logs in using credentials. Returns True if success."""
        await self.ensure_session()
        
        # If we already have a manual token, verify it works first
        if self.access_token:
             if await self.validate_token():
                 return True

        url = f"{API_URL}/api/Login/Login"
        payload = {"userId": self.email, "password": self.password}
        
        try:
            async with async_timeout.timeout(15):
                async with self.session.post(url, json=payload) as response:
                    if response.status == 200:
                        data = await response.json()
                        self.access_token = data.get("AuthorizationToken")
                        self.headers = {
                             "Authorization": f"Bearer {self.access_token}",
                             "Accept": "application/json", "Content-Type": "application/json",
                        }
                        return True
                    else:
                        _LOGGER.error(f"Login failed: {response.status}")
                        return False
        except Exception as e:
            _LOGGER.error(f"Login Error: {e}")
            return False

    async def validate_token(self):
        """Checks if current token is valid by fetching status."""
        try:
            status = await self.get_panel_status()
            return status is not None
        except:
            return False

    async def get_panel_status(self):
        return await self._get(f"{API_URL}/api/panel/GetPanelStatus?panelId={self.panel_id}")

    async def get_temperatures(self):
        # Using POST as per your original script
        return await self._post(f"{API_URL}/api/v2/housecheck/temperatures", {"PanelId": self.panel_id})

    async def get_humidity(self):
        return await self._get(f"{API_URL}/api/housecheck/panels/{self.panel_id}/humidity")

    async def get_logs(self):
        return await self._get(f"{API_URL}/api/panel/GetLogs?panelId={self.panel_id}")

    async def arm_system(self, code, mode="Total"):
        """
        Mode: Total (Arm), Partial (Home), Disarm
        """
        cmd_map = {
            "Total": "Arm",
            "Partial": "PartialArm",
            "Disarm": "Disarm"
        }
        cmd = cmd_map.get(mode)
        if not cmd: return False

        url = f"{API_URL}/api/panel/{cmd}"
        payload = {"PanelCode": code, "PanelId": self.panel_id}
        
        # Determine strict or not based on API quirks, simple implementation:
        return await self._post(url, payload)

    async def _get(self, url):
        await self.ensure_session()
        try:
            async with self.session.get(url, headers=self.headers) as resp:
                if resp.status == 200: return await resp.json()
        except Exception as e:
            _LOGGER.error(f"GET Error {url}: {e}")
        return None

    async def _post(self, url, payload):
        await self.ensure_session()
        try:
            async with self.session.post(url, json=payload, headers=self.headers) as resp:
                if resp.status in [200, 204]: 
                    try: return await resp.json()
                    except: return True
        except Exception as e:
            _LOGGER.error(f"POST Error {url}: {e}")
        return None

    async def close(self):
        if self.session: await self.session.close()