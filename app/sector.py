import aiohttp
import async_timeout
import logging
import json
from typing import Optional

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
            self._update_headers(token)

    def _update_headers(self, token):
        self.access_token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json", 
            "Content-Type": "application/json",
            "API-Version": "6"
        }

    async def ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def login(self):
        """
        Returns:
            "SUCCESS": Login OK.
            "2FA_REQUIRED": Login failed, but triggered 2FA (SMS sent).
            "FAILED": Wrong password or other error.
        """
        await self.ensure_session()
        
        # If we have a manual token, try to validate it first to avoid re-triggering SMS
        if self.access_token and await self.validate_token():
            return "SUCCESS"

        url = f"{API_URL}/api/Login/Login"
        payload = {"userId": self.email, "password": self.password}
        
        try:
            async with async_timeout.timeout(15):
                async with self.session.post(url, json=payload) as response:
                    if response.status == 200:
                        data = await response.json()
                        self._update_headers(data.get("AuthorizationToken"))
                        return "SUCCESS"
                    elif response.status == 204: 
                        # 204 No Content often means "Credentials OK, but 2FA required"
                        _LOGGER.info("Login returned 204. 2FA likely required.")
                        return "2FA_REQUIRED"
                    elif response.status == 401:
                        # Sometimes 401 also implies 2FA if creds are technically correct but unauthorized
                        return "FAILED"
                    else:
                        _LOGGER.error(f"Login failed: {response.status}")
                        return "FAILED"
        except Exception as e:
            _LOGGER.error(f"Login Error: {e}")
            return "FAILED"

    async def validate_2fa(self, code):
        """Submits the SMS code to the API."""
        await self.ensure_session()
        # Endpoint can vary, but this is the standard one for the web-app
        url = f"{API_URL}/api/Login/ValidateTwoWayVerificationCode"
        payload = {
            "UserId": self.email, 
            "Password": self.password, 
            "Code": code
        }

        try:
            async with self.session.post(url, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    token = data.get("AuthorizationToken")
                    if token:
                        self._update_headers(token)
                        return True
        except Exception as e:
            _LOGGER.error(f"2FA Verify Error: {e}")
        return False

    async def validate_token(self):
        try:
            return await self.get_panel_status() is not None
        except:
            return False

    # --- Data Methods ---
    async def get_panel_status(self):
        return await self._get(f"{API_URL}/api/panel/GetPanelStatus?panelId={self.panel_id}")

    async def get_temperatures(self):
        return await self._post(f"{API_URL}/api/v2/housecheck/temperatures", {"PanelId": self.panel_id})

    async def get_humidity(self):
        return await self._get(f"{API_URL}/api/housecheck/panels/{self.panel_id}/humidity")

    async def get_logs(self):
        return await self._get(f"{API_URL}/api/panel/GetLogs?panelId={self.panel_id}")

    async def arm_system(self, code, mode="Total"):
        cmd_map = {"Total": "Arm", "Partial": "PartialArm", "Disarm": "Disarm"}
        cmd = cmd_map.get(mode)
        if not cmd: return False
        return await self._post(f"{API_URL}/api/panel/{cmd}", {"PanelCode": code, "PanelId": self.panel_id})

    async def _get(self, url):
        await self.ensure_session()
        try:
            async with self.session.get(url, headers=self.headers) as resp:
                if resp.status == 200: return await resp.json()
        except: pass
        return None

    async def _post(self, url, payload):
        await self.ensure_session()
        try:
            async with self.session.post(url, json=payload, headers=self.headers) as resp:
                if resp.status in [200, 204]:
                    try: return await resp.json()
                    except: return True
        except: pass
        return None

    async def close(self):
        if self.session: await self.session.close()