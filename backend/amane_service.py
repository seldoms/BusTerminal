"""amane 对接业务层。

职责：把 amane 的库配置、任务状态翻译成 BusTerminal 能直接消费的结论
（盘点报告 / 自检结果），并集中管理地址与 token 的读取与刷新。
"""
import threading
import time
from typing import Any, Dict, List, Optional

import config
from amane_client import (
    AmaneClient,
    AmaneError,
    AmaneNotConfigured,
)

CONFIG_KEY_BASE_URL = "amane_base_url"
CONFIG_KEY_TOKEN = "amane_token"

# 小于这个体积基本是广告样片 / 假种子产物，amane 侧应当直接跳过。
RECOMMENDED_MIN_FILE_SIZE = 100 * 1024 * 1024
# 下载目录里最常见的非影片产物，amane 应列入 blacklist_patterns。
# ⚠️ amane 的 blacklist_patterns 是**正则**不是 glob：`*.url` 会被告警
# "nothing to repeat at position 0"（422），必须是 `.*\.url$` 这种写法。
AD_BLACKLIST_HINTS = (
    r".*\.url$", r".*\.txt$", r".*\.exe$", r".*\.lnk$", r".*\.html$", r".*\.jpg$",
    r".*\.part$", r"(?i).*sample.*", r".*广告.*", r".*推广.*", r".*宣传.*",
)
# 与 AD_BLACKLIST_HINTS 一一对应的"人话关键词"，只用来判断库里是否已覆盖该规则
# （不能拿正则原文去比对，写法不同不代表没覆盖）。
AD_BLACKLIST_KEYWORDS = (
    "url", "txt", "exe", "lnk", "html", "jpg", "part", "sample", "广告", "推广", "宣传",
)
LIBRARY_CACHE_TTL = 60.0


def _mask_token(token: str) -> str:
    value = str(token or "")
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def _suggestion(field: str, current: Any, recommended: Any, reason: str, severity: str = "warn") -> Dict[str, Any]:
    return {
        "field": field,
        "current": current,
        "recommended": recommended,
        "reason": reason,
        "severity": severity,
    }


def review_library(library: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按 BusTerminal 的入库要求审查单个 amane 库，输出待改项。"""
    suggestions: List[Dict[str, Any]] = []
    # 分工：入库登记交给 amane（watch），**刮削只能由 BusTerminal 的任务发起**（§方案 C）。
    # scrape = 监控并自动刮削，会和 BusTerminal 抢跑（还会在下载未完成时刮到半成品），故不推荐。
    automation = library.get("automation")
    if automation == "scrape":
        suggestions.append(_suggestion(
            "automation", automation, "watch",
            "automation=scrape 会自行刮削，与 BusTerminal 发起的任务抢跑，且可能刮到未下完的半成品；"
            "改成 watch（只自动登记、刮削等我们触发）或 none（完全被动）。",
            "fail",
        ))
    elif automation == "none":
        suggestions.append(_suggestion(
            "automation", automation, "watch",
            "automation=none 时新文件不会自动登记，全靠 BusTerminal 提交 refresh 任务才会被发现；"
            "想省掉这一步可改 watch。",
            "info",
        ))
    if library.get("ingest") not in (None, "native"):
        suggestions.append(_suggestion(
            "ingest", library.get("ingest"), "native",
            "非 native 通道（如 clouddrive）不走本地文件事件，BusTerminal 无法据此判断入库结果。",
        ))

    min_size = library.get("min_file_size")
    if not isinstance(min_size, int) or min_size < RECOMMENDED_MIN_FILE_SIZE:
        suggestions.append(_suggestion(
            "min_file_size", min_size, RECOMMENDED_MIN_FILE_SIZE,
            "小于 100MB 的基本是广告样片/假种子产物，应在入库前被过滤。",
        ))

    blacklist = library.get("blacklist_patterns") or []
    missing = [
        pattern
        for keyword, pattern in zip(AD_BLACKLIST_KEYWORDS, AD_BLACKLIST_HINTS)
        if not any(keyword in str(b).lower() for b in blacklist)
    ]
    if missing:
        suggestions.append(_suggestion(
            "blacklist_patterns", blacklist, sorted(set(list(blacklist) + list(missing))),
            f"缺少常见非影片产物规则：{', '.join(AD_BLACKLIST_KEYWORDS[:6])}"
            "（amane 这里是正则，不能写 *.url 这种 glob）",
        ))

    if library.get("recursive") is False:
        suggestions.append(_suggestion(
            "recursive", False, True,
            "分层目录（番号/子目录）必须递归扫描，否则成品扫不到。",
        ))

    # 元数据归属收口在 amane（§6.2 决策 2）：amane 写 NFO、Jellyfin 读 NFO。
    # 所以 amane 侧应该开着；要防的是 BusTerminal 的 Organizer 再写一份（已由
    # Organizer(allow_file_writes=False) 闸门默认挡住），不是把 amane 关掉。
    if library.get("write_nfo") is False:
        suggestions.append(_suggestion(
            "write_nfo", False, True,
            "Jellyfin 靠 amane 写的 NFO 拿元数据；关掉后 Jellyfin 只能靠文件名猜。",
        ))
    elif library.get("write_nfo") is True:
        suggestions.append(_suggestion(
            "write_nfo", True, True,
            "符合方案 C（amane 写 NFO、Jellyfin 读 NFO）。确认 BusTerminal 侧 Organizer 保持 "
            "allow_file_writes=False（默认），两边都写会互相覆盖。",
            "info",
        ))
    return suggestions


class AmaneService:
    def __init__(
        self,
        *,
        db: Any,
        logger: Any,
        safe_exception_label: Any = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.db = db
        self.logger = logger
        self.safe_exception_label = safe_exception_label or (lambda exc: type(exc).__name__)
        self.timeout = float(timeout or getattr(config, "AMANE_TIMEOUT_SECONDS", 10.0))
        self._lock = threading.Lock()
        self._client: Optional[AmaneClient] = None
        self._client_key: Optional[tuple] = None
        self._libraries: Optional[List[Dict[str, Any]]] = None
        self._libraries_at = 0.0

    # ---------- 配置 ----------

    def _setting(self, key: str, fallback: str) -> str:
        try:
            value = self.db.get_config(key)
        except Exception:
            value = None
        return str(value or fallback or "").strip()

    def _credentials(self) -> tuple:
        """内部使用：返回明文地址与 token，绝不外泄到接口响应。"""
        base_url = self._setting(CONFIG_KEY_BASE_URL, getattr(config, "AMANE_BASE_URL", "")).rstrip("/")
        token = self._setting(CONFIG_KEY_TOKEN, getattr(config, "AMANE_TOKEN", ""))
        return base_url, token

    def get_settings(self) -> Dict[str, Any]:
        """对外视图：只给掩码，不给明文。"""
        base_url, token = self._credentials()
        return {
            "base_url": base_url,
            "token_set": bool(token),
            "token_masked": _mask_token(token),
            "token_source": "db" if self._setting(CONFIG_KEY_TOKEN, "") else ("env" if token else ""),
            "configured": bool(base_url and token),
        }

    def save_settings(self, *, base_url: Optional[str] = None, token: Optional[str] = None) -> Dict[str, Any]:
        if base_url is not None:
            value = str(base_url).strip().rstrip("/")
            self.db.set_config(CONFIG_KEY_BASE_URL, value)
        if token is not None:
            value = str(token).strip()
            # 传空串表示清除；传掩码原样则忽略（前端回填场景）。
            if value and "*" in value:
                value = self._setting(CONFIG_KEY_TOKEN, getattr(config, "AMANE_TOKEN", ""))
            self.db.set_config(CONFIG_KEY_TOKEN, value)
        self.reload_config()
        return self.get_settings()

    def reload_config(self) -> None:
        with self._lock:
            self._client = None
            self._client_key = None
            self._libraries = None
            self._libraries_at = 0.0

    def get_client(self, *, require_token: bool = True) -> AmaneClient:
        base_url, token = self._credentials()
        if not base_url:
            raise AmaneNotConfigured("amane_base_url_missing")
        if require_token and not token:
            raise AmaneNotConfigured("amane_token_missing")
        key = (base_url, token)
        with self._lock:
            if self._client is not None and self._client_key == key:
                return self._client
            client = AmaneClient(
                base_url,
                token,
                timeout=self.timeout,
                logger=self.logger,
            )
            self._client = client
            self._client_key = key
            return client

    # ---------- 只读视图 ----------

    def get_status(self) -> Dict[str, Any]:
        base_url, token = self._credentials()
        status: Dict[str, Any] = {
            "base_url": base_url,
            "token_set": bool(token),
            "token_masked": _mask_token(token),
            "configured": bool(base_url and token),
            "reachable": False,
            "authenticated": False,
            "version": None,
            "library_count": None,
            "error": None,
        }
        try:
            client = self.get_client(require_token=False)
            status["reachable"] = True
            status["version"] = client.health().get("version")
        except AmaneError as exc:
            status["error"] = exc.as_dict()
            return status
        except Exception as exc:  # 兜底：不让自检接口 500
            status["error"] = {"code": "amane_unexpected", "message": self.safe_exception_label(exc)}
            return status

        if not token:
            return status
        try:
            libraries = self.list_libraries()
            status["authenticated"] = True
            status["library_count"] = len(libraries)
        except AmaneError as exc:
            status["error"] = exc.as_dict()
        return status

    def list_libraries(self, *, force: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            cached = self._libraries
            fresh = cached is not None and (time.time() - self._libraries_at) < LIBRARY_CACHE_TTL
        if fresh and not force:
            return list(cached or [])
        libraries = self.get_client().list_libraries()
        with self._lock:
            self._libraries = libraries
            self._libraries_at = time.time()
        return libraries

    def get_inventory(self, *, force: bool = False) -> Dict[str, Any]:
        """库配置盘点：现状 + 按 BusTerminal 入库要求的待改项。"""
        base_url, _ = self._credentials()
        libraries = self.list_libraries(force=force)
        reviewed = []
        for library in libraries:
            suggestions = review_library(library)
            reviewed.append({
                "id": library.get("id"),
                "name": library.get("name"),
                "path": library.get("path"),
                "automation": library.get("automation"),
                "ingest": library.get("ingest"),
                "recursive": library.get("recursive"),
                "min_file_size": library.get("min_file_size"),
                "patterns": library.get("patterns") or [],
                "blacklist_patterns": library.get("blacklist_patterns") or [],
                "write_nfo": library.get("write_nfo"),
                "suggestions": suggestions,
            })
        blocking = [lib for lib in reviewed if any(s["severity"] == "fail" for s in lib["suggestions"])]
        return {
            "base_url": base_url,
            "generated_at": int(time.time()),
            "libraries": reviewed,
            "summary": {
                "total": len(reviewed),
                "blocking": len(blocking),
                "with_suggestions": len([lib for lib in reviewed if lib["suggestions"]]),
            },
        }

    def get_selfcheck(self) -> Dict[str, Any]:
        """媒体栈自检：一条链路上到底哪一环没通。"""
        checks: List[Dict[str, Any]] = []

        def add(check_id: str, name: str, ok: bool, detail: str, hint: str = "", severity: str = "fail") -> None:
            checks.append({
                "id": check_id,
                "name": name,
                "status": "ok" if ok else (severity if severity != "fail" else "fail"),
                "detail": detail,
                "hint": hint,
            })

        base_url, token = self._credentials()
        add("base_url", "amane 地址", bool(base_url),
            base_url or "未配置", "POST /api/amane/config 填写 amane_base_url")
        add("token", "amane token", bool(token),
            _mask_token(token) or "未配置",
            "amane 设置页复制 API token，填到 /api/amane/config",
            severity="fail")

        status = self.get_status()
        version = status.get("version")
        add("reachable", "amane 可达", bool(status.get("reachable")),
            f"version={version}" if version else ("服务已响应" if status.get("reachable")
                                                 else str(status.get("error"))),
            "确认 amane 容器在跑、端口与 AMANE_BASE_URL 一致")
        if not token or not status.get("authenticated"):
            add("authenticated", "token 可用", False,
                str(status.get("error") or "token 未配置"), "token 是否过期 / 是否复制完整")
            add("libraries", "媒体库", False, "跳过（前置未通过）", "", severity="warn")
            return {"ok": False, "generated_at": int(time.time()), "checks": checks}

        add("authenticated", "token 可用", True, "库列表读取成功")
        try:
            inventory = self.get_inventory()
        except AmaneError as exc:
            add("libraries", "媒体库", False, str(exc.as_dict()), "")
            return {"ok": False, "generated_at": int(time.time()), "checks": checks}

        libraries = inventory["libraries"]
        add("libraries", "媒体库数量", bool(libraries), f"{len(libraries)} 个库",
            "在 amane 里至少建一个指向媒体根目录的库" if not libraries else "")

        blocking = [lib for lib in libraries if any(s["severity"] == "fail" for s in lib["suggestions"])]
        add("automation", "库自动化", not blocking,
            "全部库为 watch/none（只登记，刮削等 BusTerminal 触发）" if not blocking else
            "仍在自动刮削：" + ", ".join(str(lib.get("name") or lib.get("id")) for lib in blocking),
            "把库的 automation 从 scrape 降为 watch" if blocking else "")

        filtered = [lib for lib in libraries
                    if not any(s["field"] in ("min_file_size", "blacklist_patterns") for s in lib["suggestions"])]
        add("filters", "广告/样片过滤", len(filtered) == len(libraries) and bool(libraries),
            f"{len(filtered)}/{len(libraries)} 个库过滤到位",
            "补 min_file_size ≥ 100MB 与 blacklist_patterns" if len(filtered) != len(libraries) else "")

        return {
            "ok": all(c["status"] != "fail" for c in checks),
            "generated_at": int(time.time()),
            "checks": checks,
        }

    # ---------- 任务 ----------

    def list_tasks(self, *, limit: int = 20, status: Optional[List[str]] = None) -> Dict[str, Any]:
        return self.get_client().list_tasks(limit=limit, status=status)

    def get_task(self, task_id: int) -> Dict[str, Any]:
        return self.get_client().get_task(task_id)

    def trigger_refresh(
        self,
        library_id: int,
        *,
        path: str = "",
        scan: Optional[List[str]] = None,
        scrape: Optional[List[str]] = None,
        wait: bool = False,
        wait_timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        client = self.get_client()
        task = client.submit_refresh(library_id, path=path, scan=scan, scrape=scrape)
        if wait:
            task = client.wait_for_task(
                task.get("id"),
                timeout=float(wait_timeout or getattr(config, "AMANE_TASK_TIMEOUT_SECONDS", 300.0)),
            )
        return task

    def trigger_organize(
        self,
        library_id: int,
        *,
        path: str = "",
        media_file_ids: Optional[List[int]] = None,
        wait: bool = False,
        wait_timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        client = self.get_client()
        task = client.submit_organize(library_id, path=path, media_file_ids=media_file_ids)
        if wait:
            task = client.wait_for_task(
                task.get("id"),
                timeout=float(wait_timeout or getattr(config, "AMANE_TASK_TIMEOUT_SECONDS", 300.0)),
            )
        return task

    def trigger_scrape(
        self,
        *,
        number: Optional[str] = None,
        media_id: Optional[int] = None,
        content_type: Optional[str] = None,
        wait: bool = False,
        wait_timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        client = self.get_client()
        task = client.submit_scrape(number=number, media_id=media_id, content_type=content_type)
        if wait:
            task = client.wait_for_task(
                task.get("id"),
                timeout=float(wait_timeout or getattr(config, "AMANE_TASK_TIMEOUT_SECONDS", 300.0)),
            )
        return task

    def find_media(self, number: str, *, limit: int = 20) -> Dict[str, Any]:
        """按番号查 amane 已索引的媒体文件（判断某片是否真的进了库）。"""
        return self.get_client().list_media(search=str(number or "").strip(), limit=limit)
