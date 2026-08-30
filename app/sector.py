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
            "Content-Type": "application/json"
        }

    async def ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def login(self, force=False):
        """
        Args:
            force (bool): If True, forces a request to the server even if token seems valid.
        Returns: "SUCCESS", "2FA_REQUIRED", or "FAILED"
        """
        _LOGGER.info(f"Starting Login for {self.email} (Force={force})...")
        await self.ensure_session()
        
        # 1. Try Token Check First (Skip if forced)
        if not force and self.access_token:
            _LOGGER.debug("Checking existing token before logging in...")
            val_res = await self.validate_token()
            if val_res == "SUCCESS":
                _LOGGER.info("Existing token is valid. Skipping login.")
                return "SUCCESS"
            elif val_res == "RATE_LIMITED":
                _LOGGER.warning("Token check rate limited. Assuming token is still valid for now.")
                return "SUCCESS"
            else:
                _LOGGER.info("Existing token is INVALID.")

        # 2. Perform Login
        url = f"{API_URL}/api/Login/Login"
        payload = {"userId": self.email, "password": self.password}
        
        try:
            _LOGGER.debug(f"POST {url}")
            async with async_timeout.timeout(15):
                async with self.session.post(url, json=payload) as response:
                    _LOGGER.debug(f"Login Response Status: {response.status}")
                    
                    if response.status == 200:
                        data = await response.json()
                        _LOGGER.info("Login 200 OK. Token received.")
                        self._update_headers(data.get("AuthorizationToken"))
                        return "SUCCESS"
                    
                    elif response.status == 204: 
                        _LOGGER.info("Login 204 No Content. 2FA Triggered (SMS should be sent).")
                        return "2FA_REQUIRED"
                    
                    elif response.status == 401:
                        _LOGGER.error("Login 401 Unauthorized. Password wrong or 2FA required.")
                        return "FAILED"
                    
                    elif response.status == 429:
                        _LOGGER.warning("Login 429 Rate Limited.")
                        return "RATE_LIMITED"
                    
                    else:
                        text = await response.text()
                        _LOGGER.error(f"Login Failed. Status: {response.status}, Body: {text}")
                        return "FAILED"

        except Exception as e:
            _LOGGER.error(f"Login Exception: {e}")
            return "FAILED"

    async def validate_2fa(self, code):
        _LOGGER.info(f"Submitting 2FA Code: {code}")
        await self.ensure_session()
        
        url = f"{API_URL}/api/Login/ValidateTwoWayVerificationCode"
        payload = {
            "UserId": self.email, 
            "Password": self.password, 
            "Code": code
        }

        try:
            async with self.session.post(url, json=payload) as response:
                _LOGGER.debug(f"Validate 2FA Status: {response.status}")
                if response.status == 200:
                    data = await response.json()
                    token = data.get("AuthorizationToken")
                    if token:
                        _LOGGER.info("2FA Success! Token obtained.")
                        self._update_headers(token)
                        return True
                else:
                    text = await response.text()
                    _LOGGER.error(f"2FA Failed Body: {text}")
        except Exception as e:
            _LOGGER.error(f"2FA Exception: {e}")
        return False

    async def validate_token(self):
        try:
            res = await self.get_panel_status()
            return "SUCCESS" if res is not None else "INVALID"
        except Exception as e:
            if str(e) == "RATE_LIMITED":
                return "RATE_LIMITED"
            return "INVALID"

    # --- Data Methods ---
    async def get_panel_status(self):
        return await self._get(f"{API_URL}/api/panel/GetPanelStatus?panelId={self.panel_id}")

    async def get_temperatures(self):
        res = await self._post(f"{API_URL}/api/v2/housecheck/temperatures", {"PanelId": self.panel_id})
        if not res:
            res = await self._get(f"{API_URL}/api/panel/GetTemperatures?panelId={self.panel_id}")
        return res

    async def get_humidity(self):
        res = await self._get(f"{API_URL}/api/housecheck/panels/{self.panel_id}/humidity")
        if not res:
            res = await self._get(f"{API_URL}/api/panel/GetHumidity?panelId={self.panel_id}")
        return res

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
                elif resp.status == 429:
                    _LOGGER.warning(f"GET {url} failed: 429 Rate Limited")
                    raise Exception("RATE_LIMITED")
                else: _LOGGER.error(f"GET {url} failed: {resp.status}")
        except Exception as e: 
            if str(e) == "RATE_LIMITED": raise
            _LOGGER.error(f"GET Error {url}: {e}")
        return None

    async def _post(self, url, payload):
        await self.ensure_session()
        try:
            async with self.session.post(url, json=payload, headers=self.headers) as resp:
                if resp.status in [200, 204]:
                    try: return await resp.json()
                    except: return True
                elif resp.status == 429:
                    _LOGGER.warning(f"POST {url} failed: 429 Rate Limited")
                    raise Exception("RATE_LIMITED")
                else: _LOGGER.error(f"POST {url} failed: {resp.status}")
        except Exception as e: 
            if str(e) == "RATE_LIMITED": raise
            _LOGGER.error(f"POST Error {url}: {e}")
        return None

    async def close(self):
        if self.session: await self.session.close()