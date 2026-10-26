"""Runtime configuration, read from environment variables (see .env.example)."""
import base64
import hashlib
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'sentinel.db').as_posix()}")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-secret-change-me-0123456789abcdef")
# Keyed hash used to pseudonymise APAAR IDs (never store the raw ID in an indexed column).
HASH_KEY = os.getenv("HASH_KEY", "dev-only-hash-key-change-me")
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:8080").split(",")]
ACCESS_TOKEN_MINUTES = int(os.getenv("ACCESS_TOKEN_MINUTES", "120"))
SIGNING_KEY_B64 = os.getenv("SIGNING_KEY", "")  # base64 raw Ed25519 private key; generated into DATA_DIR if empty
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "600"))

# Protocol constants taken from the idea document
SEAT_ALERT_PCT = 90
SEAT_STOP_PCT = 100
UNCONFIRMED_AFTER_DAYS = 30
PROVISIONAL_DEADLINE_DAYS = 30
OTP_TTL_MINUTES = 15
OTP_MAX_ATTEMPTS = 5
MIN_GROUP_SIZE = 5  # reports suppress groups under 5

if not DEMO_MODE and ("dev-only" in SECRET_KEY or "dev-only" in HASH_KEY):
    raise RuntimeError("Refusing to start with DEMO_MODE=false and the built-in development secrets. Set SECRET_KEY and HASH_KEY.")

FERNET_KEY =base64.urlsafe_b64encode(hashlib.sha256(("fernet:" + SECRET_KEY).encode()).digest())
