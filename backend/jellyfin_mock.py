"""Jellyfin 替身服务（仅调试 / 演练用，生产不加载）。

按 Jellyfin REST 的最小子集实现，用来验证入库闭环的收尾一环"该片是否真的可见"：
- `X-Emby-Token` 鉴权
- `POST /Library/Refresh` 是**异步**的：后台线程延迟若干秒后才完成登记，
  所以 BusTerminal 的轮询逻辑能被真实验证（不是一次查就命中）
- `GET /Items?SearchTerm=` 按番号回查，登记前查不到、登记后查得到

启动：
    JELLYFIN_MOCK_ROOT=/tmp/amane-root/organized JELLYFIN_MOCK_TOKEN=mock-token \
        uvicorn jellyfin_mock:app --host 127.0.0.1 --port 8101
"""
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import Response

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv", ".ts", ".m2ts"}
DEFAULT_TOKEN = "mock-token"
DEFAULT_SCAN_DELAY = 2.0

app = FastAPI(title="jellyfin-mock", version="10.9.0")

STATE: Dict[str, Any] = {
    "root": os.getenv("JELLYFIN_MOCK_ROOT") or "",
    "items": {},
    "next_item_id": 1,
    "scan_started_at": None,
    "scan_finished_at": None,
    "refresh_count": 0,
}
STATE_LOCK = threading.Lock()


def _mock_token() -> str:
    return os.getenv("JELLYFIN_MOCK_TOKEN") or DEFAULT_TOKEN


def _scan_delay() -> float:
    raw = os.getenv("JELLYFIN_MOCK_SCAN_DELAY") or ""
    try:
        return max(float(raw), 0.0)
    except (TypeError, ValueError):
        return DEFAULT_SCAN_DELAY


def _require_token(x_emby_token: Optional[str]) -> None:
    """默认不校验 token：演练要改的只有 host，不该动真实密钥。
    需要验证鉴权时设 JELLYFIN_MOCK_REQUIRE_TOKEN=1。"""
    if os.getenv("JELLYFIN_MOCK_REQUIRE_TOKEN") != "1":
        return
    provided = str(x_emby_token or "").strip()
    if provided != _mock_token():
        raise HTTPException(status_code=401, detail="缺少或无效的 API token.")


def _number_from_path(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    match = re.search(r"([A-Za-z0-9]{2,10}-[0-9]{2,10})", stem)
    return (match.group(1) if match else stem).upper()


def _walk_videos(root: str) -> List[str]:
    found: List[str] = []
    if not root or not os.path.isdir(root):
        return found
    for current, _dirs, files in os.walk(root):
        for name in files:
            if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                found.append(os.path.join(current, name))
    return sorted(found)


def _do_scan() -> None:
    """登记当前根目录下的所有视频（模拟 Jellyfin 扫描完成）。"""
    paths = _walk_videos(str(STATE.get("root") or ""))
    with STATE_LOCK:
        known = {item["Path"]: item for item in STATE["items"].values()}
        for path in paths:
            if path in known:
                continue
            item_id = str(STATE["next_item_id"])
            STATE["next_item_id"] += 1
            number = _number_from_path(path)
            STATE["items"][item_id] = {
                "Id": item_id,
                "Name": number,
                "Path": path,
                "Type": "Movie",
            }
        STATE["scan_finished_at"] = time.time()


@app.get("/System/Info")
def system_info():
    return {
        "ServerName": "jellyfin-mock",
        "Version": "10.9.0",
        "Id": "mock-server-id",
        "OperatingSystem": "mock",
    }


@app.get("/System/Info/Public")
def system_info_public():
    return {"ServerName": "jellyfin-mock", "Version": "10.9.0", "Id": "mock-server-id"}


@app.post("/Library/Refresh")
def refresh_library(x_emby_token: Optional[str] = Header(None)):
    """触发扫描。真正登记发生在后台线程的延迟之后。"""
    _require_token(x_emby_token)
    with STATE_LOCK:
        STATE["refresh_count"] += 1
        STATE["scan_started_at"] = time.time()
    threading.Timer(_scan_delay(), _do_scan).start()
    return Response(status_code=204)


@app.get("/Items")
def list_items(
    search_term: str = Query("", alias="SearchTerm"),
    x_emby_token: Optional[str] = Header(None),
):
    _require_token(x_emby_token)
    wanted = str(search_term or "").strip().upper()
    with STATE_LOCK:
        items = list(STATE["items"].values())
    if wanted:
        items = [item for item in items if wanted in str(item.get("Name") or "").upper()]
    return {"Items": items, "TotalRecordCount": len(items)}


@app.get("/api/mock/state")
def mock_state():
    """替身自检：看刷新次数与已登记条目。"""
    with STATE_LOCK:
        return {
            "root": STATE["root"],
            "refresh_count": STATE["refresh_count"],
            "items": list(STATE["items"].values()),
            "scan_started_at": STATE["scan_started_at"],
            "scan_finished_at": STATE["scan_finished_at"],
        }


@app.post("/api/mock/reset")
def mock_reset():
    with STATE_LOCK:
        STATE["items"] = {}
        STATE["next_item_id"] = 1
        STATE["refresh_count"] = 0
        STATE["scan_started_at"] = None
        STATE["scan_finished_at"] = None
    return {"success": True}
