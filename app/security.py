import logging
import os
from pathlib import Path
from cryptography.fernet import Fernet

# Data directory matches the docker volume mapping in docker-compose.yaml
DATA_DIR = Path("/config")
KEY_FILE = DATA_DIR / "secrets.key"
_LOGGER = logging.getLogger(__name__)

def generate_key():
    """Generates a new Fernet key and saves it to the key file."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        with open(KEY_FILE, "wb") as key_file:
            key_file.write(key)
        _LOGGER.info("New encryption key generated and saved.")
        return key
    except Exception as e:
        _LOGGER.critical(f"Failed to generate key: {e}")
        return None

def load_key():
    """Loads the Fernet key from the key file, generating it if it doesn't exist."""
    if not KEY_FILE.exists():
        return generate_key()
    try:
        with open(KEY_FILE, "rb") as key_file:
            return key_file.read()
    except Exception as e:
        _LOGGER.critical(f"Failed to load key: {e}")
        return None

# Initialize Fernet
_key = load_key()
_fernet = Fernet(_key) if _key else None

def encrypt_password(password: str) -> str | None:
    """Encrypts a password string."""
    if not _fernet or not password:
        return None
    try:
        return _fernet.encrypt(password.encode()).decode()
    except Exception as e:
        _LOGGER.error(f"Encryption failed: {e}")
        return None

def decrypt_password(encrypted_password: str) -> str | None:
    """Decrypts an encrypted password string."""
    if not _fernet or not encrypted_password:
        return None
    try:
        return _fernet.decrypt(encrypted_password.encode()).decode()
    except Exception:
        # If decryption fails, it might be a plain text password (migration scenario)
        _LOGGER.warning("Password decryption failed. Treating as plain text or invalid.")
        return None