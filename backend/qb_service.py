import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse
import requests


QB_TORRENT_RESPONSE_FIELDS = {
    "hash",
    "name",
    "state",
    "progress",
    "size",
    "total_size",
    "dlspeed",
    "upspeed",
    "tags",
    "category",
    "added_on",
    "addedOn",
    "added_at",
    "eta",
    "ratio",
    "downloaded",
    "uploaded",
}


class QBService:
    def __init__(
        self,
        *,
        db: Any,
        qb_api: Any,
        config: Any,
        logger: Any,
        safe_number: Callable[..., float],
        format_bytes: Callable[[float], str],
        format_speed: Callable[[float], str],
        display_value_for_log: Callable[[Any], str],
        normalize_qb_root_path: Callable[[Any], Optional[str]],
        normalize_qb_hashes: Callable[[Any], List[str]],
        normalize_qb_category: Callable[[Any], Optional[str]],
        run_jav_file_filter_once: Callable[[], Dict[str, Any]],
        control_all_confirmation: str,
    ):
        self.db = db
        self.qb_api = qb_api
        self.config = config
        self.logger = logger
        self.safe_number = safe_number
        self.format_bytes = format_bytes
        self.format_speed = format_speed
        self.display_value_for_log = display_value_for_log
        self.normalize_qb_root_path = normalize_qb_root_path
        self.normalize_qb_hashes = normalize_qb_hashes
        self.normalize_qb_category = normalize_qb_category
        self.run_jav_file_filter_once = run_jav_file_filter_once
        self.control_all_confirmation = control_all_confirmation
        self._last_incomplete_config_warn = 0.0
        self._last_unavailable_reason = "qb_not_configured"
        self.session_provider: Optional[Callable[[], Any]] = None
        self.config_payload_normalizer: Optional[Callable[[Any, str], Dict[str, Any]]] = None
        self._session: Optional[Any] = None
        self._session_key: Optional[tuple] = None
        self._offline_until: float = 0.0
        self._cb_lock = threading.Lock()

    def is_circuit_open(self) -> bool:
        with self._cb_lock:
            return time.time() < self._offline_until

    def record_failure(self, cooldown_seconds: float = 60.0) -> None:
        with self._cb_lock:
            self._offline_until = time.time() + cooldown_seconds
            self._session = None
            self._session_key = None
            self._last_unavailable_reason = "qb_unavailable"

    def record_success(self) -> None:
        with self._cb_lock:
            self._offline_until = 0.0

    def invalidate_session(self) -> None:
        with self._cb_lock:
            self._session = None
            self._session_key = None

    def normalize_host(self, raw_host: Any) -> Optional[str]:
        """规范化主机地址。

        ⚠️ 早期版本拒绝"地址里带端口"（`parsed.port` 或出现 `:` 直接判非法），
        但界面上这个字段的标签是「服务器 API 地址」，用户按最自然的习惯填
        `http://192.168.1.5:8080` 就会保存失败 —— 实测生产就是这个写法被拒
        （invalid_qb_host）。现在允许带端口，端口会被拆出来（见 split_host_port），
        端口本身非法（0 / >65535）时仍然拒绝。
        """
        host = str(raw_host or "").strip().rstrip("/")
        if not host or any(char.isspace() for char in host):
            return None

        if host.startswith(("http://", "https://")):
            parsed = urlparse(host)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return None
            if parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                return None
            # 地址里写了端口就必须合法。urlparse 对 `http://h:abc` 会把 :abc 当 netloc
            # 的一部分，parsed.port 抛异常；这里直接按文本判断并拒绝非法值，
            # 否则它会被悄悄剥成 host=http://h，端口回退成表单里的值 —— 地址错了但"保存成功"。
            bare = host.split("://", 1)[-1]
            if ":" in bare:
                port_text = bare.rpartition(":")[2]
                if not port_text.isdigit() or not (1 <= int(port_text) <= 65535):
                    return None
            return f"{parsed.scheme}://{parsed.hostname}"

        if "/" in host or "\\" in host or "@" in host:
            return None
        # 裸主机允许带端口（交给 split_host_port 拆），只拒绝非法端口号
        if ":" in host:
            name, _, port_text = host.rpartition(":")
            if not name or not port_text.isdigit():
                return None
            if not (1 <= int(port_text) <= 65535):
                return None
            host = name
        return host

    def split_host_port(self, raw_host: Any, fallback_port: int) -> tuple:
        """从"地址"里把端口拆出来。地址没带端口时用 fallback（表单的端口字段）。

        返回 (normalized_host, port)。
        地址本身非法 -> invalid_qb_host；端口非法 -> invalid_qb_port（分开报，
        用户才知道该改地址还是改端口）。
        """
        text = str(raw_host or "").strip().rstrip("/")
        bare = text.split("://", 1)[-1] if "://" in text else text
        if ":" in bare and not bare.endswith(":"):
            port_text = bare.rpartition(":")[2]
            if not port_text.isdigit() or not (1 <= int(port_text) <= 65535):
                raise ValueError("invalid_qb_port")
        host = self.normalize_host(raw_host)
        if not host:
            raise ValueError("invalid_qb_host")
        if ":" in bare:
            return host, int(bare.rpartition(":")[2])
        return host, int(fallback_port)

    def normalize_config_payload(self, payload: Any, existing_password: str = "") -> Dict[str, Any]:
        host, port = self.split_host_port(payload.host, payload.port)
        if port < 1 or port > 65535:
            raise ValueError("invalid_qb_port")

        username = str(payload.username or "").strip()
        if not username:
            raise ValueError("missing_qb_username")

        incoming_password = payload.password
        password = existing_password if str(incoming_password or "").strip() == "" else incoming_password
        return {
            "host": host,
            "port": port,
            "username": username,
            "password": password,
        }

    def safe_config_port(self, raw_port: Any, default: int = 8080) -> int:
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            return default
        if port < 1 or port > 65535:
            return default
        return port

    def safe_config_error(self, exc: ValueError) -> str:
        message = str(exc)
        if message in {"invalid_qb_host", "invalid_qb_port", "missing_qb_username"}:
            return message
        return "invalid_qb_config"

    def config_payload(self) -> Dict[str, Any]:
        saved = self.db.get_config("qb_config") or {}
        if isinstance(saved, str):
            try:
                saved = json.loads(saved)
            except Exception:
                saved = {}
        if not isinstance(saved, dict):
            saved = {}
        return {
            "host": saved.get("host") or self.config.QB_HOST or "",
            "port": self.safe_config_port(saved.get("port") or self.config.QB_PORT or 8080),
            "username": saved.get("username") or self.config.QB_USERNAME or "admin",
            "password": "",
            "password_configured": bool(saved.get("password") or self.config.QB_PASSWORD),
        }

    def save_config(self, payload: Any) -> Dict[str, Any]:
        saved = self.db.get_config("qb_config")
        existing_password = self.config.QB_PASSWORD or ""
        if isinstance(saved, dict):
            existing_password = saved.get("password") or existing_password
        elif isinstance(saved, str):
            try:
                parsed = json.loads(saved)
                if isinstance(parsed, dict):
                    existing_password = parsed.get("password") or existing_password
            except Exception:
                existing_password = self.config.QB_PASSWORD or ""

        normalizer = self.config_payload_normalizer or self.normalize_config_payload
        data = normalizer(payload, existing_password)
        self.db.set_config("qb_config", data)
        self.invalidate_session()
        self.record_success()
        return {**data, "password": "", "password_configured": bool(data.get("password"))}

    def unavailable_response(self) -> Dict[str, Any]:
        return {
            "success": False,
            "connected": False,
            "degraded": True,
            "message": self._last_unavailable_reason or "qb_unavailable",
            "configured": self._last_unavailable_reason != "qb_not_configured",
        }

    def get_session(self):
        if self.is_circuit_open():
            self._last_unavailable_reason = "qb_unavailable"
            return None, None

        qb_config = self.db.get_config("qb_config")

        host = None
        port = None
        username = self.config.QB_USERNAME or "admin"
        password = self.config.QB_PASSWORD or ""

        if qb_config:
            if isinstance(qb_config, str):
                try:
                    qb_config = json.loads(qb_config)
                except Exception:
                    qb_config = {}

            if isinstance(qb_config, dict) and qb_config.get("host"):
                host = qb_config.get("host")
                port = qb_config.get("port")
                username = qb_config.get("username") or username
                password = qb_config.get("password") or ""
                self.logger.debug("Loaded QB config from database: %s:%s", self.display_value_for_log(host), port)

        if not host and self.config.QB_HOST and self.config.QB_PORT:
            host = self.config.QB_HOST
            port = self.config.QB_PORT
            username = self.config.QB_USERNAME or username
            password = self.config.QB_PASSWORD or password
            self.logger.debug("Loaded QB config from environment: %s:%s", self.display_value_for_log(host), port)

        if not host or not port or not username:
            self._last_unavailable_reason = "qb_not_configured"
            if time.time() - self._last_incomplete_config_warn > 300:
                self.logger.warning("QB connection config is incomplete; QB related background actions will be skipped.")
                self._last_incomplete_config_warn = time.time()
            return None, None

        normalized_host = self.normalize_host(host)
        try:
            normalized_port = int(port)
        except (TypeError, ValueError):
            normalized_port = 0
        # 地址里带了端口就以它为准（save_config 那条写入路径已经这么拆了）。
        # 早期这里只做 normalize_host + int(port)，于是库里若存着
        # `http://h:9090` + port=8080 这样的历史数据，会拼出
        # `http://h:9090:8080` 这种双端口地址 —— 登录必然失败。
        try:
            normalized_host, embedded_port = self.split_host_port(host, normalized_port)
            if embedded_port:
                normalized_port = embedded_port
        except ValueError:
            self._last_unavailable_reason = "qb_invalid_config"
            self.logger.warning("QB connection config is invalid; QB related background actions will be skipped.")
            return None, None
        if not normalized_host or normalized_port < 1 or normalized_port > 65535:
            self._last_unavailable_reason = "qb_invalid_config"
            self.logger.warning("QB connection config is invalid; QB related background actions will be skipped.")
            return None, None

        if normalized_host.startswith(("http://", "https://")):
            base_url = f"{normalized_host}:{normalized_port}"
        else:
            base_url = f"http://{normalized_host}:{normalized_port}"

        verify_ssl = getattr(self.config, "QB_VERIFY_SSL", True)
        config_key = (base_url, username, password, verify_ssl)

        with self._cb_lock:
            if self._session is not None and self._session_key == config_key:
                return self._session, base_url

        try:
            session = self.qb_api.login(base_url, username, password, timeout=5, verify=verify_ssl)
        except Exception:
            session = None

        if session is None:
            # 先 record_failure 再写具体原因：record_failure 会把原因一律刷成
            # qb_unavailable，顺序反了就等于把"登录失败"这个可诊断信息抹掉。
            self.record_failure()
            self._last_unavailable_reason = "qb_login_failed"
            return None, base_url

        self.record_success()
        with self._cb_lock:
            self._session = session
            self._session_key = config_key
        self._last_unavailable_reason = ""
        return session, base_url

    def session(self):
        if self.session_provider:
            return self.session_provider()
        return self.get_session()

    def get_status(self) -> Dict[str, Any]:
        if self.is_circuit_open():
            return self.unavailable_response()

        session, base_url = self.session()
        if not session:
            return self.unavailable_response()

        try:
            version = self.qb_api.get_app_version(session, base_url)
            state = self.qb_api.get_server_state(session, base_url)
            if version is None and not state:
                self.invalidate_session()
                return self.unavailable_response()
            return {"success": True, "data": {"version": version, "server_state": state}}
        except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError) as e:
            self.logger.warning("QB get_status connection failed: %s", e)
            self.record_failure()
            return self.unavailable_response()

    def sanitize_torrent_for_response(self, torrent: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(torrent, dict):
            return {}
        return {
            key: torrent.get(key)
            for key in QB_TORRENT_RESPONSE_FIELDS
            if key in torrent
        }

    def sanitize_torrents_for_response(self, torrents: Any) -> List[Dict[str, Any]]:
        if not isinstance(torrents, list):
            return []
        return [
            sanitized
            for sanitized in (self.sanitize_torrent_for_response(torrent) for torrent in torrents)
            if sanitized
        ]

    def build_dashboard(self, session: Any, base_url: str) -> Dict[str, Any]:
        main_data = self.qb_api.get_main_data(session, base_url)
        server_state = main_data.get("server_state") or {}
        main_torrents = main_data.get("torrents")
        if isinstance(main_torrents, dict):
            torrents = [torrent for torrent in main_torrents.values() if isinstance(torrent, dict)]
        elif isinstance(main_torrents, list):
            torrents = [torrent for torrent in main_torrents if isinstance(torrent, dict)]
        else:
            torrents = self.qb_api.get_torrents(session, base_url)
        transfer = server_state or self.qb_api.get_transfer_info(session, base_url)
        queue_stats = self.db.get_queue_stats()

        free_space = int(max(self.safe_number(server_state.get("free_space_on_disk")), 0))
        total_size = int(sum(
            max(self.safe_number(t.get("total_size") or t.get("size")), 0)
            for t in torrents or []
            if isinstance(t, dict)
        ))
        estimated_total = free_space + total_size
        used_percent = round((total_size / estimated_total) * 100, 1) if estimated_total else 0

        dl_speed = int(max(self.safe_number(transfer.get("dl_info_speed", server_state.get("dl_info_speed", 0))), 0))
        up_speed = int(max(self.safe_number(transfer.get("up_info_speed", server_state.get("up_info_speed", 0))), 0))
        alltime_dl = int(max(self.safe_number(transfer.get("dl_info_data", server_state.get("alltime_dl", 0))), 0))

        return {
            "connected": True,
            "configured": True,
            "disk": {
                "free": free_space,
                "free_display": self.format_bytes(free_space),
                "total_display": self.format_bytes(estimated_total),
                "estimated_total_display": self.format_bytes(estimated_total),
                "used_percent": used_percent,
            },
            "transfer": {
                "dl_speed": dl_speed,
                "up_speed": up_speed,
                "dl_speed_display": self.format_speed(dl_speed),
                "up_speed_display": self.format_speed(up_speed),
                "alltime_dl": alltime_dl,
                "alltime_dl_display": self.format_bytes(alltime_dl),
            },
            "queue_stats": queue_stats,
            "torrent_count": len(torrents or []),
        }

    def get_dashboard(self) -> Dict[str, Any]:
        if self.is_circuit_open():
            return {
                "success": True,
                "degraded": True,
                "message": self._last_unavailable_reason or "qb_unavailable",
                "data": {
                    "connected": False,
                    "degraded": True,
                    "configured": True,
                    "disk": {},
                    "transfer": {},
                    "queue_stats": self.db.get_queue_stats(),
                    "torrent_count": 0,
                },
            }

        session, base_url = self.session()
        if not session:
            return {
                "success": True,
                "degraded": True,
                "message": self._last_unavailable_reason or "qb_unavailable",
                "data": {
                    "connected": False,
                    "degraded": True,
                    "configured": bool(base_url),
                    "disk": {},
                    "transfer": {},
                    "queue_stats": self.db.get_queue_stats(),
                    "torrent_count": 0,
                },
            }

        try:
            return {"success": True, "data": self.build_dashboard(session, base_url)}
        except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError) as e:
            self.logger.warning("QB get_dashboard connection failed: %s", e)
            self.record_failure()
            return {
                "success": True,
                "degraded": True,
                "message": "qb_unavailable",
                "data": {
                    "connected": False,
                    "degraded": True,
                    "configured": bool(base_url),
                    "disk": {},
                    "transfer": {},
                    "queue_stats": self.db.get_queue_stats(),
                    "torrent_count": 0,
                },
            }

    def get_torrents(self, category: Optional[str], limit: int, offset: int) -> Dict[str, Any]:
        normalized_category = self.normalize_qb_category(category) if category else None
        if category and not normalized_category:
            return {"success": False, "message": "invalid_qb_category"}

        if self.is_circuit_open():
            return self.unavailable_response()

        session, base_url = self.session()
        if not session:
            return self.unavailable_response()

        try:
            torrents = self.qb_api.get_torrents(session, base_url, normalized_category)
            if not isinstance(torrents, list):
                return {"success": True, "data": [], "total": 0, "limit": limit, "offset": offset}

            return {
                "success": True,
                "data": self.sanitize_torrents_for_response(torrents[offset:offset + limit]),
                "total": len(torrents),
                "limit": limit,
                "offset": offset,
            }
        except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError) as e:
            self.logger.warning("QB get_torrents connection failed: %s", e)
            self.record_failure()
            return self.unavailable_response()

    def control_torrents(self, payload: Any) -> Dict[str, Any]:
        action = payload.action.strip().lower()
        if action not in {"pause", "resume"}:
            return {"success": False, "message": "unsupported_torrent_action"}

        if payload.all and str(payload.confirm or "").strip() != self.control_all_confirmation:
            return {
                "success": False,
                "message": "torrent_all_control_requires_confirmation",
                "requires_confirmation": True,
                "confirmation_phrase": self.control_all_confirmation,
            }

        hashes: Any = "all" if payload.all else self.normalize_qb_hashes(payload.hashes)
        if not payload.all and not hashes:
            return {"success": False, "message": "missing_or_invalid_torrent_hashes"}

        if self.is_circuit_open():
            return self.unavailable_response()

        session, base_url = self.session()
        if not session:
            return self.unavailable_response()

        try:
            fn = self.qb_api.pause_torrents if action == "pause" else self.qb_api.resume_torrents
            success = fn(session, base_url, hashes)
            count = "all" if payload.all else len(hashes)
            if not success:
                return {"success": False, "message": "qb_torrent_control_failed", "count": count}
            return {"success": True, "count": count}
        except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError) as e:
            self.logger.warning("QB control_torrents connection failed: %s", e)
            self.record_failure()
            return self.unavailable_response()

    def filter_jav_files(self) -> Dict[str, Any]:
        result = self.run_jav_file_filter_once()
        return {"success": bool(result.get("success")), "data": result, "message": result.get("message")}

