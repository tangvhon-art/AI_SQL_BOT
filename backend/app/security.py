"""安全模块：AES-GCM 加密（数据源密码/API Key）、JWT、密码哈希。"""
import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import get_settings

_ALGO = "HS256"


def _derive_key() -> bytes:
    """从 SECRET_KEY 派生 32 字节 AES 密钥。"""
    return hashlib.sha256(get_settings().secret_key.encode("utf-8")).digest()


def aes_encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    nonce = os.urandom(12)
    ct = AESGCM(_derive_key()).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def aes_decrypt(token: str) -> str:
    if not token:
        return ""
    raw = base64.b64decode(token)
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(_derive_key()).decrypt(nonce, ct, None).decode("utf-8")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), 100_000)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:  # noqa: BLE001
        return False


def create_access_token(user_id: int, username: str) -> str:
    settings = get_settings()
    payload = {
        "sub": str(user_id),
        "username": username,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=_ALGO)


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    return jwt.decode(token, settings.secret_key, algorithms=[_ALGO])
