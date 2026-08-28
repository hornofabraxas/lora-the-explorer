import hashlib
import hmac
import os
import time

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse

SESSION_MAX_AGE = 90 * 86400  # 90 days
COOKIE_NAME = "lora_session"
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 300  # 5 minutes

_failed_attempts: dict[str, list[float]] = {}


def _secret_path(db_path: str) -> str:
    return os.path.join(os.path.dirname(db_path), ".session_secret")


def _write_secret(secret_path: str) -> str:
    secret = os.urandom(32).hex()
    os.makedirs(os.path.dirname(secret_path), exist_ok=True)
    with open(secret_path, "w") as f:
        f.write(secret)
    # Signing-key material: owner-only, so another local account can't mint
    # session cookies. (Best-effort on Windows, where chmod is mostly a no-op.)
    os.chmod(secret_path, 0o600)
    return secret


def _get_secret(db_path: str) -> str:
    secret_path = _secret_path(db_path)
    if os.path.exists(secret_path):
        with open(secret_path) as f:
            return f.read().strip()
    return _write_secret(secret_path)


def rotate_secret(db_path: str) -> str:
    """Replace the session-signing secret, invalidating every outstanding session
    cookie. Called on password change so a previously stolen cookie can't outlive
    the credential it was minted under."""
    return _write_secret(_secret_path(db_path))


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex() + ":" + dk.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, AttributeError):
        return False


def create_session_cookie(secret: str) -> str:
    s = URLSafeTimedSerializer(secret)
    return s.dumps({"authenticated": True})


def validate_session_cookie(cookie: str, secret: str) -> bool:
    s = URLSafeTimedSerializer(secret)
    try:
        s.loads(cookie, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def is_rate_limited(client_ip: str) -> bool:
    now = time.time()
    attempts = _failed_attempts.get(client_ip, [])
    attempts = [t for t in attempts if now - t < LOCKOUT_SECONDS]
    _failed_attempts[client_ip] = attempts
    return len(attempts) >= MAX_FAILED_ATTEMPTS


def record_failed_attempt(client_ip: str) -> None:
    if client_ip not in _failed_attempts:
        _failed_attempts[client_ip] = []
    _failed_attempts[client_ip].append(time.time())


def clear_failed_attempts(client_ip: str) -> None:
    _failed_attempts.pop(client_ip, None)


_PUBLIC_PREFIXES = ("/login", "/static/", "/auth/oidc/", "/favicon.ico", "/apple-touch-icon", "/sw.js")
_SETUP_PATHS = ("/setup", "/setup/password", "/setup/oidc")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        db = request.app.state.db
        has_password = await db.get_setting("password_hash") is not None
        has_oidc = await db.get_oidc_config() is not None
        has_any_auth = has_password or has_oidc

        request.state.has_password = has_password
        request.state.has_oidc = has_oidc

        if not has_any_auth:
            path = request.url.path
            if path not in _SETUP_PATHS and not path.startswith("/static/"):
                return RedirectResponse("/setup", status_code=302)
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        secret = _get_secret(request.app.state.config["db_path"])
        cookie = request.cookies.get(COOKIE_NAME)
        if cookie and validate_session_cookie(cookie, secret):
            return await call_next(request)

        return RedirectResponse("/login", status_code=302)
