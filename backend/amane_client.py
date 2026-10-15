"""amane REST 客户端。

BusTerminal 是决策层，不直接读写媒体文件；扫描 / 整理 / 刮削 / 清理全部
通过 amane 的 HTTP API 触发并回读状态。本模块只负责这一个通道，不含业务判断。
"""
import logging
import time
from typing import Any, Dict, List, Optional

import requests

from net_policy import apply_policy

DEFAULT_TIMEOUT = 10.0
TERMINAL_TASK_STATUSES = {"done", "failed"}


class AmaneError(Exception):
    """amane 调用失败的基类。"""

    code = "amane_error"

    def __init__(self, message: str = "", **details: Any) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


class AmaneNotConfigured(AmaneError):
    """地址或 token 未配置。"""

    code = "amane_not_configured"


class AmaneUnavailable(AmaneError):
    """网络不可达 / 超时。"""

    code = "amane_unavailable"


class AmaneAuthError(AmaneError):
    """token 缺失或被拒绝（401 / 403）。"""

    code = "amane_auth_failed"


class AmaneApiError(AmaneError):
    """amane 返回了非 2xx（鉴权除外）。"""

    code = "amane_api_error"

    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message or f"amane_http_{status_code}", status_code=status_code)
        self.status_code = status_code


class AmaneClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        session: Any = None,
        logger: Any = None,
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.token = str(token or "").strip()
        self.timeout = timeout
        self._session = session or requests.Session()
        # amane 是内网服务：默认不走系统代理，否则 requests 会按代理协议发出绝对 URI，
        # 服务端把它当路径解析，结果是 404 而不是 401（本机实测踩过）。
        # 只有明确打开 net_policy 的开关才让内网调用跟随 http_proxy/https_proxy。
        if session is None:
            apply_policy(self._session)
        self.logger = logger or logging.getLogger(__name__)

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _error_detail(response: Any) -> str:
        try:
            payload = response.json()
        except ValueError:
            return (response.text or "")[:200]
        if isinstance(payload, dict):
            return str(payload.get("detail") or payload.get("message") or "")[:200]
        return str(payload)[:200]

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        if not self.base_url:
            raise AmaneNotConfigured("amane_base_url_missing")
        url = f"{self.base_url}{path}"
        try:
            response = self._session.request(
                method,
                url,
                params={k: v for k, v in (params or {}).items() if v is not None},
                json=json_body,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise AmaneUnavailable(f"amane_unreachable:{type(exc).__name__}") from exc

        if response.status_code in (401, 403):
            raise AmaneAuthError(self._error_detail(response) or "amane_token_rejected")
        if response.status_code >= 400:
            raise AmaneApiError(response.status_code, self._error_detail(response))
        if response.status_code == 204:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise AmaneApiError(response.status_code, "amane_invalid_json") from exc

    @staticmethod
    def _items(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            items = payload.get("items")
            return items if isinstance(items, list) else []
        return []

    # ---------- 只读 ----------

    def health(self) -> Dict[str, Any]:
        payload = self.request("GET", "/api/health")
        return payload if isinstance(payload, dict) else {}

    def list_libraries(self) -> List[Dict[str, Any]]:
        return self._items(self.request("GET", "/api/libraries"))

    def get_library(self, library_id: int) -> Dict[str, Any]:
        payload = self.request("GET", f"/api/libraries/{int(library_id)}")
        return payload if isinstance(payload, dict) else {}

    def list_media(
        self,
        *,
        library_id: Optional[int] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        payload = self.request(
            "GET",
            "/api/media",
            params={
                "library_id": library_id,
                "status": status,
                "search": search,
                "limit": max(1, min(int(limit), 200)),
                "offset": max(0, int(offset)),
            },
        )
        if isinstance(payload, dict):
            return {"items": self._items(payload), "total": payload.get("total")}
        return {"items": [], "total": 0}

    def list_tasks(
        self,
        *,
        status: Optional[List[str]] = None,
        task_type: Optional[List[str]] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        payload = self.request(
            "GET",
            "/api/tasks",
            params={
                "status": status,
                "type": task_type,
                "limit": max(1, min(int(limit), 200)),
            },
        )
        if isinstance(payload, dict):
            return {"items": self._items(payload), "total": payload.get("total")}
        return {"items": [], "total": 0}

    def get_task(self, task_id: int) -> Dict[str, Any]:
        payload = self.request("GET", f"/api/tasks/{int(task_id)}")
        return payload if isinstance(payload, dict) else {}

    def list_files(self, path: str, *, base: Optional[str] = None) -> List[Dict[str, Any]]:
        payload = self.request("GET", "/api/files", params={"path": path, "base": base})
        return self._items(payload)

    def get_media(self, media_id: int) -> Dict[str, Any]:
        payload = self.request("GET", f"/api/media/{int(media_id)}")
        return payload if isinstance(payload, dict) else {}

    def patch_media(self, media_id: int, **fields: Any) -> Dict[str, Any]:
        """补写 media_file 字段（自愈主要用于回填空 number）。

        request() 直接把 method 透传给 requests.Session.request，PATCH 原生支持，
        不需要额外扩展；body 走 json_body 通道。
        """
        payload = self.request("PATCH", f"/api/media/{int(media_id)}", json_body=dict(fields))
        return payload if isinstance(payload, dict) else {}

    # ---------- 任务 ----------

    def submit_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self.request("POST", "/api/tasks", json_body=payload)
        return result if isinstance(result, dict) else {}

    def submit_refresh(
        self,
        library_id: int,
        *,
        path: str = "",
        scan: Optional[List[str]] = None,
        scrape: Optional[List[str]] = None,
        recursive: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """扫描库目录。

        默认只登记新文件（scan=["add"]）**不刮削**：方案 C 下刮削只由 BusTerminal
        显式提交的任务触发。若沿用 amane 的 scrape=["pending"]，一次 refresh 会把
        库里所有 pending 条目（实测 384 条）全部丢进刮削队列，既抢跑又打爆刮削源。
        调用方要连刮削一起做，必须显式传 scrape=[...]。
        """
        body: Dict[str, Any] = {
            "type": "refresh",
            "library_id": int(library_id),
            "path": str(path or ""),
            "scan": scan or ["add"],
            "scrape": scrape if scrape is not None else [],
        }
        if recursive is not None:
            body["recursive"] = bool(recursive)
        return self.submit_task(body)

    def submit_organize(
        self,
        library_id: int,
        *,
        path: str = "",
        media_file_ids: Optional[List[int]] = None,
        write_nfo: Optional[bool] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"type": "organize", "library_id": int(library_id)}
        if media_file_ids:
            body["media_file_ids"] = [int(i) for i in media_file_ids]
        else:
            body["path"] = str(path or "")
        if write_nfo is not None:
            body["write_nfo"] = bool(write_nfo)
        return self.submit_task(body)

    def submit_scrape(
        self,
        *,
        number: Optional[str] = None,
        media_id: Optional[int] = None,
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"type": "scrape"}
        if number:
            body["number"] = str(number).strip()
        if media_id:
            body["media_id"] = int(media_id)
        if content_type:
            body["content_type"] = content_type
        return self.submit_task(body)

    def submit_cleanup(
        self,
        *,
        remove_missing_files: bool = True,
        remove_unreferenced_resources: bool = True,
    ) -> Dict[str, Any]:
        return self.submit_task(
            {
                "type": "cleanup",
                "remove_missing_files": bool(remove_missing_files),
                "remove_unreferenced_resources": bool(remove_unreferenced_resources),
            }
        )

    def wait_for_task(
        self,
        task_id: int,
        *,
        timeout: float = 300.0,
        interval: float = 3.0,
        stop_event: Any = None,
    ) -> Dict[str, Any]:
        """轮询任务直至 done / failed，或超时（超时返回最后一次快照）。"""
        deadline = time.time() + max(0.0, float(timeout))
        snapshot = self.get_task(task_id)
        while str(snapshot.get("status") or "") not in TERMINAL_TASK_STATUSES:
            if stop_event is not None and stop_event.is_set():
                break
            if time.time() >= deadline:
                snapshot["wait_timeout"] = True
                break
            time.sleep(max(0.5, float(interval)))
            snapshot = self.get_task(task_id)
        return snapshot
