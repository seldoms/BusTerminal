from pathlib import Path
from typing import Any, Dict, Optional
import hashlib
import logging
import os
import re


logger = logging.getLogger(__name__)
ALLOW_UNSAFE_PATH_OUTPUT = "BUS_TERMINAL_SHOW_UNSAFE_PATHS"

AUTHORIZATION_LOG_PATTERN = re.compile(
    r"(?i)([\"']?authorization[\"']?\s*[:=]\s*)([\"']?)(?:Bearer|Basic)\s+"
    r"([^,\s\"'\]}]+)([\"']?)"
)
SENSITIVE_LOG_PATTERNS = (
    re.compile(
        r"(?i)([\"']?(?:api[_-]?key|token|secret|password|passwd|pwd|authorization|"
        r"x-emby-token|x-mediabrowser-token|jellyfin_api_key|baidu_secret|email_pass|system_proxy|"
        r"qb_password)[\"']?\s*[:=]\s*)([\"'])(.*?)(\2)"
    ),
    re.compile(
        r"(?i)([\"']?(?:api[_-]?key|token|secret|password|passwd|pwd|authorization|"
        r"x-emby-token|x-mediabrowser-token|jellyfin_api_key|baidu_secret|email_pass|system_proxy|"
        r"qb_password)[\"']?\s*[:=]\s*)([\"']?)([^,\s\"'\]}]+)([\"']?)"
    ),
    re.compile(r"(?i)([?&](?:api[_-]?key|token|secret|password|passwd|pwd)=)[^&\s]+"),
    re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\b[A-Za-z]:[\\/][^\s\"',;<>]+"),
    re.compile(r"(?:/mnt|/media|/volume\d*|/home|/Users|/app/data|/app/downloads)[^\s\"',;<>]*"),
)


def redact_log_message(value: Any) -> str:
    text = "" if value is None else str(value)
    text = AUTHORIZATION_LOG_PATTERN.sub(r"\1\2***\4", text)
    text = SENSITIVE_LOG_PATTERNS[0].sub(r"\1\2***\4", text)
    text = SENSITIVE_LOG_PATTERNS[1].sub(r"\1\2***\4", text)
    text = SENSITIVE_LOG_PATTERNS[2].sub(r"\1***", text)
    text = SENSITIVE_LOG_PATTERNS[3].sub(r"\1 ***", text)
    text = SENSITIVE_LOG_PATTERNS[4].sub("<path redacted>", text)
    text = SENSITIVE_LOG_PATTERNS[5].sub("<path redacted>", text)
    return text


def safe_exception_label(exc: Exception) -> str:
    return type(exc).__name__


def safe_error_response(message: str, exc: Exception) -> Dict[str, Any]:
    return {"success": False, "message": message, "error_type": safe_exception_label(exc)}


def get_float_env(key: str, default: float, minimum: Optional[float] = None) -> float:
    raw = os.getenv(key)
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def display_path_for_log(path: Any) -> str:
    text = "" if path is None else str(path)
    if os.getenv(ALLOW_UNSAFE_PATH_OUTPUT, "").lower() in {"1", "true", "yes", "on"}:
        return text
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"<path len={len(text)} sha256={digest}>"


def is_safe_cache_file(path: Any, root: Any, allowed_suffixes: Optional[set] = None) -> bool:
    try:
        candidate = Path(path)
        root_path = Path(root).resolve()
        if allowed_suffixes and candidate.suffix.lower() not in allowed_suffixes:
            return False
        if candidate.is_symlink() or not candidate.is_file():
            return False
        candidate.resolve().relative_to(root_path)
        return True
    except Exception:
        return False


def write_cache_bytes_if_safe(path: Any, root: Any, content: bytes) -> bool:
    try:
        target = Path(path)
        root_path = Path(root).resolve()
        target.parent.resolve().relative_to(root_path)
        if target.exists() and (target.is_symlink() or not target.is_file()):
            return False
        with target.open("wb") as handle:
            handle.write(content)
        return True
    except Exception as exc:
        logger.debug("Skipped unsafe cache write %s: %s", display_path_for_log(path), safe_exception_label(exc))
        return False


def format_bytes(value: Any) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        size = 0.0
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    if index == 0:
        return f"{int(size)} {units[index]}"
    return f"{size:.1f} {units[index]}"


def format_speed(value: Any) -> str:
    return f"{format_bytes(value)}/s"


def safe_number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
