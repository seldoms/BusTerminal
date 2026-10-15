"""Non-destructive dry-run against the configured real environment.

This script only reads configuration and remote state. It does not add torrents,
change Jellyfin favorites, approve candidates, create subscriptions, or delete
anything.

Run from the repository root:
    python backend/real_env_dry_run.py

Optional:
    set BUS_TERMINAL_API_URL=http://127.0.0.1:8000
    set BUS_TERMINAL_SHOW_ENDPOINTS=true  # print real hostnames in local-only debugging
"""

import json
import os
import sys
import warnings
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 .* doesn't match a supported version!",
    category=Warning,
)

import requests

import config
import qb_api
from database import db


@dataclass
class Check:
    name: str
    status: str
    detail: str


def redact(value: Optional[str], keep: int = 0) -> str:
    if not value:
        return "<missing>"
    value = str(value)
    if keep <= 0:
        return f"<redacted:{len(value)} chars>"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}...{len(value)} chars"


def show_endpoints() -> bool:
    return os.getenv("BUS_TERMINAL_SHOW_ENDPOINTS", "").lower() in {"1", "true", "yes", "on"}


def redact_url(value: Optional[str]) -> str:
    if not value:
        return "<missing>"
    text = str(value)
    if show_endpoints():
        return text
    parsed = urlparse(text if "://" in text else f"http://{text}")
    port = f":{parsed.port}" if parsed.port else ""
    scheme = f"{parsed.scheme}://" if parsed.scheme else ""
    return f"{scheme}<redacted-host>{port}"


def redact_path(value: Optional[str]) -> str:
    if not value:
        return "<missing>"
    text = str(value)
    if show_endpoints():
        return text
    return f"<redacted-path:{os.path.basename(text) or 'root'}>"


def safe_exception_label(exc: Exception) -> str:
    return type(exc).__name__


def add_check(checks: List[Check], name: str, status: str, detail: str):
    checks.append(Check(name=name, status=status, detail=detail))


def load_qb_config() -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], bool]:
    raw = db.get_config("qb_config")
    host = None
    port = None
    username = config.QB_USERNAME or "admin"
    password = config.QB_PASSWORD or ""

    if raw:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        if isinstance(raw, dict):
            host = raw.get("host")
            port = raw.get("port")
            username = raw.get("username") or username
            password = raw.get("password") or ""

    if not host and config.QB_HOST and config.QB_PORT:
        host = config.QB_HOST
        port = config.QB_PORT
        username = config.QB_USERNAME or username
        password = config.QB_PASSWORD or password

    verify_ssl = getattr(config, "QB_VERIFY_SSL", True)
    return host, str(port) if port else None, username, password, verify_ssl


def build_qb_base_url(host: str, port: Optional[str]) -> str:
    raw_host = str(host or "").strip().rstrip("/")
    if not raw_host:
        return ""

    parsed = urlparse(raw_host if "://" in raw_host else f"http://{raw_host}")
    scheme = parsed.scheme or "http"
    hostname = parsed.hostname or raw_host
    resolved_port = parsed.port or (int(port) if port else None)
    if resolved_port:
        return f"{scheme}://{hostname}:{resolved_port}"
    return f"{scheme}://{hostname}"


def check_qb(checks: List[Check]):
    host, port, username, password, verify_ssl = load_qb_config()
    if not host or not username:
        add_check(checks, "qB config", "WARN", "qB 配置不完整，跳过 qB 只读探测")
        return

    base_url = build_qb_base_url(host, port)
    add_check(
        checks,
        "qB config",
        "PASS",
        f"base={redact_url(base_url)}, user={redact(username, keep=1)}, password={redact(password)}, verify_ssl={verify_ssl}",
    )

    session = qb_api.login(base_url, username, password, timeout=8, verify=verify_ssl)
    if not session:
        add_check(checks, "qB login", "FAIL", "无法登录 qB，自动下载可能会被阻断")
        return

    version = qb_api.get_app_version(session, base_url)
    state = qb_api.get_server_state(session, base_url)
    torrents = qb_api.get_torrents(session, base_url)
    free_bytes = int(state.get("free_space_on_disk") or 0)
    add_check(
        checks,
        "qB read-only state",
        "PASS",
        f"version={version or '<unknown>'}, torrents={len(torrents)}, free_space_gb={free_bytes / (1024 ** 3):.1f}",
    )


def load_jellyfin_config() -> Tuple[str, str, str]:
    host = db.get_config("jellyfin_host") or os.getenv("JELLYFIN_HOST", "")
    api_key = db.get_config("jellyfin_api_key") or os.getenv("JELLYFIN_API_KEY", "")
    user_id = db.get_config("jellyfin_user_id") or os.getenv("JELLYFIN_USER_ID", "")
    return str(host).rstrip("/") if host else "", str(api_key or ""), str(user_id or "")


def jellyfin_get(host: str, api_key: str, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    headers = {
        "X-Emby-Token": api_key,
        "X-MediaBrowser-Token": api_key,
        "Accept": "application/json",
    }
    resp = requests.get(f"{host}{path}", headers=headers, params=params or {}, timeout=10)
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return resp.json() if resp.text else None


def check_jellyfin(checks: List[Check]):
    host, api_key, user_id = load_jellyfin_config()
    if not host or not api_key:
        add_check(checks, "Jellyfin config", "WARN", "Jellyfin host/api_key 未配置，跳过 Jellyfin 只读探测")
        return

    add_check(checks, "Jellyfin config", "PASS", f"host={redact_url(host)}, api_key={redact(api_key)}, user_id={redact(user_id)}")

    try:
        system_info = jellyfin_get(host, api_key, "/System/Info")
        add_check(
            checks,
            "Jellyfin system",
            "PASS",
            f"name={system_info.get('ServerName')}, version={system_info.get('Version')}",
        )
    except Exception as exc:
        add_check(checks, "Jellyfin system", "FAIL", f"无法读取系统信息: {safe_exception_label(exc)}")
        return

    try:
        if not user_id:
            users = jellyfin_get(host, api_key, "/Users") or []
            if users:
                user_id = users[0].get("Id", "")
        if not user_id:
            add_check(checks, "Jellyfin user", "FAIL", "无法确定 user_id")
            return
        add_check(checks, "Jellyfin user", "PASS", f"user_id={redact(user_id)}")

        views = jellyfin_get(host, api_key, f"/Users/{user_id}/Views") or {}
        libraries = views.get("Items", [])
        movie_libraries = [it for it in libraries if it.get("CollectionType") == "movies"]
        add_check(checks, "Jellyfin libraries", "PASS", f"libraries={len(libraries)}, movie_libraries={len(movie_libraries)}")

        items = jellyfin_get(
            host,
            api_key,
            f"/Users/{user_id}/Items",
            {
                "IncludeItemTypes": "Movie",
                "Recursive": "true",
                "Fields": "UserData,ProviderIds,Path,DateCreated",
                "SortBy": "DateCreated",
                "SortOrder": "Descending",
                "Limit": 5,
            },
        ) or {}
        sample_items = items.get("Items", [])
        add_check(checks, "Jellyfin movie sample", "PASS", f"sample_count={len(sample_items)}")

        favorites = jellyfin_get(
            host,
            api_key,
            f"/Users/{user_id}/Items",
            {
                "Filters": "IsFavorite",
                "IncludeItemTypes": "Movie",
                "Recursive": "true",
                "Limit": 5,
                "Fields": "UserData,ProviderIds",
            },
        ) or {}
        watched = jellyfin_get(
            host,
            api_key,
            f"/Users/{user_id}/Items",
            {
                "Filters": "IsPlayed",
                "IncludeItemTypes": "Movie",
                "Recursive": "true",
                "Limit": 5,
                "Fields": "UserData,ProviderIds",
            },
        ) or {}
        add_check(
            checks,
            "Jellyfin behavior sample",
            "PASS",
            f"favorite_sample={len(favorites.get('Items', []))}, watched_sample={len(watched.get('Items', []))}",
        )
    except Exception as exc:
        add_check(checks, "Jellyfin library read", "FAIL", f"读取媒体库失败: {safe_exception_label(exc)}")


def check_database_state(checks: List[Check]):
    try:
        db_target = getattr(db, "db_path", config.DATABASE_URL)
        add_check(
            checks,
            "Database target",
            "PASS",
            f"DATA_DIR={redact_path(config.DATA_DIR)}, target={redact_path(db_target)}",
        )
        cleanup = db.get_cleanup_candidates(limit=5)
        automation = db.get_automation_candidates(status=None, limit=5)
        lifecycle = db.get_movie_lifecycle(limit=5)
        subscriptions = db.get_subscriptions()
        add_check(
            checks,
            "Database lifecycle state",
            "PASS",
            (
                f"lifecycle_sample={len(lifecycle)}, cleanup_sample={len(cleanup)}, "
                f"automation_sample={len(automation)}, subscriptions={len(subscriptions)}"
            ),
        )
    except Exception as exc:
        add_check(checks, "Database lifecycle state", "FAIL", safe_exception_label(exc))


def check_safety_config(checks: List[Check]):
    delete_enabled = str(db.get_config("jellyfin_delete_enabled")).lower() == "true"
    auto_download_enabled = str(db.get_config("auto_download_enabled")).lower() == "true"
    daily_limit = db.get_config("auto_download_daily_limit") or "3"
    cleanup_enabled = str(db.get_config("cleanup_enabled")).lower() == "true"

    add_check(
        checks,
        "Safety switches",
        "PASS" if not delete_enabled else "WARN",
        (
            f"jellyfin_delete_enabled={delete_enabled}, "
            f"auto_download_enabled={auto_download_enabled}, "
            f"auto_download_daily_limit={daily_limit}, cleanup_enabled={cleanup_enabled}"
        ),
    )


def check_backend_api(checks: List[Check]):
    api_url = os.getenv("BUS_TERMINAL_API_URL", "").rstrip("/")
    if not api_url:
        add_check(checks, "Backend API", "WARN", "未设置 BUS_TERMINAL_API_URL，跳过运行中后端 HTTP 只读检查")
        return

    endpoints = [
        "/api/lifecycle/movies?limit=5",
        "/api/cleanup/candidates?limit=5",
        "/api/automation/candidates?limit=5",
        "/api/qb/status",
    ]
    ok_count = 0
    for endpoint in endpoints:
        try:
            resp = requests.get(f"{api_url}{endpoint}", timeout=10)
            if resp.ok:
                ok_count += 1
            else:
                add_check(checks, f"Backend API {endpoint}", "FAIL", f"HTTP {resp.status_code}")
        except Exception as exc:
            add_check(checks, f"Backend API {endpoint}", "FAIL", safe_exception_label(exc))
    if ok_count == len(endpoints):
        add_check(checks, "Backend API", "PASS", f"{ok_count}/{len(endpoints)} read-only endpoints reachable")


def print_report(checks: List[Check]) -> int:
    print("BusTerminal real environment non-destructive dry-run")
    print("=" * 56)
    for check in checks:
        print(f"[{check.status}] {check.name}: {check.detail}")

    fail_count = sum(1 for c in checks if c.status == "FAIL")
    warn_count = sum(1 for c in checks if c.status == "WARN")
    pass_count = sum(1 for c in checks if c.status == "PASS")
    print("=" * 56)
    print(f"Summary: PASS={pass_count}, WARN={warn_count}, FAIL={fail_count}")
    return 1 if fail_count else 0


def main() -> int:
    checks: List[Check] = []
    check_safety_config(checks)
    check_database_state(checks)
    check_qb(checks)
    check_jellyfin(checks)
    check_backend_api(checks)
    return print_report(checks)


if __name__ == "__main__":
    sys.exit(main())
