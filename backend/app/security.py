"""Passwords, JWT, TOTP MFA, keyed hashes, field encryption, Ed25519 signing, RBAC dependencies."""
import base64
import hashlib
import hmac
import json
import secrets
import struct
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Optional

import jwt
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from . import config
from .db import get_db
from .models import User


# ---------- passwords (PBKDF2-SHA256, stdlib) ----------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
        new = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000)
        return hmac.compare_digest(new.hex(), dk)
    except Exception:
        return False


# ---------- keyed hashes & field encryption ----------
def keyed_hash(value: str) -> str:
    return hmac.new(config.HASH_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


_fernet = Fernet(config.FERNET_KEY)


def encrypt(value: Optional[str]) -> Optional[str]:
    return _fernet.encrypt(value.encode()).decode() if value else None


def decrypt(token: Optional[str]) -> Optional[str]:
    return _fernet.decrypt(token.encode()).decode() if token else None


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


# ---------- TOTP (RFC 6238) ----------
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(10)).decode()


def totp_code(secret: str, at: Optional[float] = None, step: int = 30) -> str:
    counter = int((at or time.time()) // step)
    key = base64.b32decode(secret)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def verify_totp(secret: str, code: str) -> bool:
    now = time.time()
    return any(hmac.compare_digest(totp_code(secret, now + d * 30), code.strip()) for d in (-1, 0, 1))


# ---------- JWT ----------
def make_token(user: User, kind: str = "access", minutes: Optional[int] = None) -> str:
    exp = datetime.utcnow() + timedelta(minutes=minutes or config.ACCESS_TOKEN_MINUTES)
    return jwt.encode({"sub": str(user.id), "role": user.role, "kind": kind, "exp": exp}, config.SECRET_KEY, "HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, config.SECRET_KEY, algorithms=["HS256"])


# ---------- Ed25519 signing (credentials, cohort roots, yes/no answers) ----------
def _load_key() -> Ed25519PrivateKey:
    if config.SIGNING_KEY_B64:
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(config.SIGNING_KEY_B64))
    path = config.DATA_DIR / "signing.key"
    if path.exists():
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(path.read_text().strip()))
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    path.write_text(base64.b64encode(raw).decode())
    return key


_signing_key = _load_key()


def public_key_b64() -> str:
    raw = _signing_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def sign(obj) -> str:
    return base64.b64encode(_signing_key.sign(canonical(obj).encode())).decode()


def verify_signature(obj, signature: str, pub_b64: Optional[str] = None) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64 or public_key_b64()))
        pub.verify(base64.b64decode(signature), canonical(obj).encode())
        return True
    except Exception:
        return False


# ---------- RBAC / ABAC dependencies ----------
_bearer = HTTPBearer(auto_error=False)


def current_user(cred: Optional[HTTPAuthorizationCredentials] = Depends(_bearer), db: Session = Depends(get_db)) -> User:
    if not cred:
        raise HTTPException(401, "Not authenticated")
    try:
        data = decode_token(cred.credentials)
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    if data.get("kind") != "access":
        raise HTTPException(401, "Wrong token type")
    user = db.get(User, int(data["sub"]))
    if not user:
        raise HTTPException(401, "Unknown user")
    return user


def require_roles(*roles: str):
    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, f"Role {user.role} may not access this resource")
        return user
    return dep


AICTE = ("AICTE_ADMIN", "AICTE_REVIEWER")

# ---------- rate limiting (per-process sliding window; use the gateway in production) ----------
_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def rate_limit(request: Request, user: User = Depends(current_user)):
    key = f"{user.id}:{request.url.path}"
    now = time.time()
    with _lock:
        q = _hits[key]
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= config.RATE_LIMIT_PER_MIN:
            raise HTTPException(429, "Rate limit exceeded; slow down")
        q.append(now)
    return user


def reset_rate_limits():
    with _lock:
        _hits.clear()
