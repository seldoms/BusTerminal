import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from database import db

TOKEN_TTL_SECONDS = 7 * 24 * 3600
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 60
LOGIN_RATE_LIMIT_MAX_FAILURES = 5

# Paths that never require authentication.
# 注意：首次部署引导（/api/onboarding/*）**不在**这里。auth_required() 在库里还没有
# 任何用户时本身就返回 False，前端此时照样能读写引导配置；一旦建了管理员账号，引导接口
# 就和其他接口一样要求 token —— 否则任何匿名请求都能改 Jellyfin key / 代理 / 屏蔽词。
AUTH_EXEMPT_EXACT_PATHS = {
    "/api/auth/login",
    "/api/auth/setup",
    "/api/auth/status",
}
AUTH_EXEMPT_PREFIXES = (
    "/api/health/",
    "/api/webhooks/jellyfin",
    "/api/proxy/image",
)

_login_failures = {}
_login_failures_lock = threading.Lock()


def auth_required() -> bool:
    if os.getenv("REQUIRE_AUTH", "true").strip().lower() in ("0", "false", "no"):
        return False
    return db.has_any_user()


def _get_secret() -> str:
    secret = db.get_config("auth_secret")
    if not secret:
        secret = secrets.token_hex(32)
        db.set_config("auth_secret", secret)
    return str(secret)


def create_token(username: str) -> str:
    expiry = int(time.time()) + TOKEN_TTL_SECONDS
    user_part = base64.urlsafe_b64encode(str(username).encode("utf-8")).decode("ascii").rstrip("=")
    payload = f"{user_part}.{expiry}"
    signature = hmac.new(_get_secret().encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_token(token: str) -> Optional[str]:
    try:
        user_part, expiry_str, signature = str(token).split(".")
        expiry = int(expiry_str)
    except (ValueError, AttributeError):
        return None
    if expiry < time.time():
        return None
    payload = f"{user_part}.{expiry_str}"
    expected = hmac.new(_get_secret().encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        padding = "=" * (-len(user_part) % 4)
        return base64.urlsafe_b64decode(user_part + padding).decode("utf-8")
    except Exception:
        return None


def login_rate_limit_key(request: Request, username: str) -> str:
    client_host = request.client.host if request.client else "unknown"
    return f"{client_host}:{username}"


def is_login_rate_limited(key: str) -> bool:
    now = time.time()
    with _login_failures_lock:
        timestamps = [t for t in _login_failures.get(key, []) if now - t < LOGIN_RATE_LIMIT_WINDOW_SECONDS]
        _login_failures[key] = timestamps
        return len(timestamps) >= LOGIN_RATE_LIMIT_MAX_FAILURES


def record_login_failure(key: str) -> None:
    with _login_failures_lock:
        _login_failures.setdefault(key, []).append(time.time())


def clear_login_failures(key: str) -> None:
    with _login_failures_lock:
        _login_failures.pop(key, None)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (
            request.method == "OPTIONS"
            or not path.startswith("/api/")
            or path in AUTH_EXEMPT_EXACT_PATHS
            or path.startswith(AUTH_EXEMPT_PREFIXES)
        ):
            return await call_next(request)
        if not auth_required():
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        if token and verify_token(token):
            return await call_next(request)
        return JSONResponse(status_code=401, content={"detail": "authentication_required"})
