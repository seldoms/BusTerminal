import math
from typing import Any, Callable, Dict, List
from urllib.parse import urlparse


SENSITIVE_SYSTEM_CONFIG_KEYS = {"baidu_secret", "jellyfin_api_key", "email_pass", "system_proxy"}

# 这些字段是「粘贴型」输入（从 Jellyfin 后台复制），用户很容易连着标点一起粘进来。
# 实测生产里的 jellyfin_api_key 存成了`...b64;;;;;`（末尾 5 个分号），
# 导致 Jellyfin 一律 401，而界面只看"保存成功"完全看不出来。
# 保存前统一剥掉包裹引号与首尾空白；不做更激进的字符过滤，避免误删合法密钥。
_TRIM_WRAPPERS = ("\"", "'", "`", " ", "\t", "\r", "\n", ";")


def sanitize_pasted_value(value: Any) -> Any:
    """清洗粘贴型配置值。只去包裹字符，不改动内容本身。"""
    if not isinstance(value, str):
        return value
    text = value.strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in ("\"", "'", "`"):
        text = text[1:-1].strip()
    while text and text[-1] in _TRIM_WRAPPERS:
        text = text[:-1].strip()
    while text and text[0] in _TRIM_WRAPPERS:
        text = text[1:].strip()
    # 注意：清洗后为空就返回空，不要回退原值 ——
    # 早期写成 `text or value`，结果「;;;」这种纯包裹字符会原样返回，脏值继续留在库里。
    return text


# 需要做粘贴清洗的字段（其余字段保持原样，避免误改）
_PASTED_CONFIG_KEYS = {
    "jellyfin_api_key", "jellyfin_host", "jellyfin_user_id", "jellyfin_library_name",
    "baidu_app_id", "baidu_secret", "amane_token", "amane_base_url", "amane_library_id",
    "ollama_base_url", "ollama_model", "delivery_folder", "email_user", "email_pass",
    "qb_username",
}


def sanitize_pasted_config_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """对粘贴型字段做清洗，其余字段原样返回。"""
    if not isinstance(data, dict):
        return data
    cleaned = dict(data)
    for key in _PASTED_CONFIG_KEYS:
        if key in cleaned:
            cleaned[key] = sanitize_pasted_value(cleaned[key])
    return cleaned


class SystemConfigService:
    def __init__(
        self,
        *,
        db: Any,
        config: Any,
        normalize_qb_root_path: Callable[[Any], Any],
        default_jellyfin_library_name: str,
    ):
        self.db = db
        self.config = config
        self.normalize_qb_root_path = normalize_qb_root_path
        self.default_jellyfin_library_name = default_jellyfin_library_name

    def payload(self) -> Dict[str, Any]:
        defaults = {
            "disk_threshold_gb": 800,
            "seeding_time_limit_min": 60,
            "translation_provider": self.config.TRANSLATION_PROVIDER,
            "baidu_app_id": self.config.BAIDU_APP_ID,
            "baidu_secret": self.config.BAIDU_SECRET,
            "ollama_base_url": self.config.OLLAMA_BASE_URL,
            "ollama_model": self.config.OLLAMA_MODEL,
            "system_proxy": self.config.SYSTEM_PROXY,
            "enable_domain_rotation": True,
            "fixed_domain": "https://www.javbus.com",
            "cache_size_limit_gb": self.config.CACHE_SIZE_LIMIT_GB,
            "jellyfin_host": "",
            "jellyfin_api_key": "",
            "jable_hot_auto_download": False,
            "internal_proxy_enabled": self.config.INTERNAL_PROXY_ENABLED,
            "automation_mode": "suggest",
            "auto_follow_enabled": False,
            "auto_subscribe_enabled": False,
            "auto_download_enabled": False,
            "auto_cleanup_enabled": False,
            "auto_download_daily_limit": 3,
            "delivery_folder": self.config.DELIVERY_FOLDER,
            "jellyfin_library_name": self.default_jellyfin_library_name,
            "cleanup_enabled": False,
            "cleanup_progress_threshold": 80,
            "cleanup_run_hour": 3,
            "jellyfin_delete_enabled": False,
            "jellyfin_auto_approve_candidates": False,
            "jellyfin_auto_delete_enabled": False,
            "auto_delete_mode": "both",
            "auto_delete_run_hour": 3,
            "auto_delete_watch_count_max": 1,
            "auto_delete_keep_favorite": True,
            "auto_delete_batch_limit": 50,
            "auto_delete_retention_days": 30,
            "email_enabled": False,
            "email_host": "smtp.qq.com",
            "email_port": 465,
            "email_user": "",
            "email_pass": "",
            "email_to": "",
        }
        payload = {}
        for key, default in defaults.items():
            value = self.db.get_config(key)
            resolved = default if value is None else value
            if key in SENSITIVE_SYSTEM_CONFIG_KEYS:
                payload[key] = ""
                payload[f"{key}_configured"] = bool(resolved)
                continue
            payload[key] = resolved
        return payload

    def should_skip_sensitive_update(self, key: str, value: Any) -> bool:
        if key not in SENSITIVE_SYSTEM_CONFIG_KEYS:
            return False
        return value is None or str(value).strip() == ""

    def is_valid_http_config_url(
        self,
        value: Any,
        *,
        allow_credentials: bool = False,
        allow_path: bool = False,
    ) -> bool:
        text = str(value or "").strip()
        if not text or len(text) > 2048 or any(char.isspace() for char in text) or "\x00" in text or "\\" in text:
            return False

        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if not allow_credentials and (parsed.username or parsed.password):
            return False
        if not allow_path and parsed.path not in ("", "/"):
            return False
        if parsed.query or parsed.fragment:
            return False
        return True

    def validate_payload(self, data: Dict[str, Any]) -> List[str]:
        errors = []
        numeric_ranges = {
            "disk_threshold_gb": (0, None),
            "seeding_time_limit_min": (0, None),
            "cache_size_limit_gb": (0.1, 1000),
            "auto_download_daily_limit": (-1, None),
            "cleanup_progress_threshold": (0, 100),
            "cleanup_run_hour": (0, 23),
            "email_port": (1, 65535),
        }

        for key, (minimum, maximum) in numeric_ranges.items():
            if key not in data:
                continue
            value = data.get(key)
            try:
                number = float(value)
            except (TypeError, ValueError):
                errors.append(f"{key}_invalid_number")
                continue
            if not math.isfinite(number):
                errors.append(f"{key}_invalid_number")
                continue
            if minimum is not None and number < minimum:
                errors.append(f"{key}_below_min")
            if maximum is not None and number > maximum:
                errors.append(f"{key}_above_max")

        if "translation_provider" in data and data.get("translation_provider") not in {"google_free", "baidu_dev", "ollama_local"}:
            errors.append("translation_provider_unsupported")
        if "fixed_domain" in data and not self.is_valid_http_config_url(data.get("fixed_domain")):
            errors.append("fixed_domain_invalid_url")
        if "jellyfin_host" in data:
            jellyfin_host = str(data.get("jellyfin_host") or "").strip()
            if jellyfin_host and not self.is_valid_http_config_url(jellyfin_host, allow_path=True):
                errors.append("jellyfin_host_invalid_url")
            current_jellyfin_host = str(self.db.get_config("jellyfin_host") or "").strip().rstrip("/")
            normalized_jellyfin_host = jellyfin_host.rstrip("/")
            if normalized_jellyfin_host and normalized_jellyfin_host != current_jellyfin_host:
                jellyfin_api_key = str(data.get("jellyfin_api_key") or "").strip()
                if not jellyfin_api_key:
                    errors.append("jellyfin_api_key_required_for_host_change")
        if "system_proxy" in data:
            system_proxy = str(data.get("system_proxy") or "").strip()
            if system_proxy and not self.is_valid_http_config_url(system_proxy, allow_credentials=True):
                errors.append("system_proxy_invalid_url")
        if "delivery_folder" in data:
            delivery_folder = str(data.get("delivery_folder") or "").strip()
            if delivery_folder and not self.normalize_qb_root_path(delivery_folder):
                errors.append("delivery_folder_invalid_path")
        return errors
