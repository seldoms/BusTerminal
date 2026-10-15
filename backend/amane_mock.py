"""amane 替身服务（仅调试 / 演练用，生产不加载）。

按 amane 0.18.0 的契约实现 health / libraries / media / tasks / files 的最小子集：
- `Bearer` token 鉴权（`/api/health` 除外）
- `refresh` 会**真的扫磁盘**：按库的 `min_file_size` / `blacklist_patterns` / 视频扩展名过滤，
  所以"5MB 假文件不被登记""广告文件不被登记"这类规则能被真实验证
- 任务提交后由后台线程推进 queued → running → done，可验证轮询逻辑

启动：
    AMANE_MOCK_ROOT=/tmp/amane-root AMANE_MOCK_TOKEN=mock-token \
        uvicorn amane_mock:app --host 127.0.0.1 --port 8100
"""
import os
import re
import shutil
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv", ".ts", ".m2ts"}
DEFAULT_TOKEN = "mock-token"

app = FastAPI(title="amane-mock", version="0.18.0")

STATE: Dict[str, Any] = {
    "root": os.getenv("AMANE_MOCK_ROOT") or "",
    "libraries": [],
    "media": {},
    "tasks": {},
    "sequence": 0,
    "next_media_id": 1,
    "next_task_id": 1,
}
STATE_LOCK = threading.Lock()


def _mock_token() -> str:
    return os.getenv("AMANE_MOCK_TOKEN") or DEFAULT_TOKEN


def _require_token(authorization: Optional[str]) -> None:
    expected = _mock_token()
    provided = str(authorization or "").strip()
    if provided.startswith("Bearer "):
        provided = provided[7:].strip()
    if provided != expected:
        raise HTTPException(status_code=401, detail="缺少或无效的 API token.")


def _library_defaults(root: str) -> Dict[str, Any]:
    return {
        "id": 1,
        "name": "hp-mock",
        "path": root,
        "automation": "scrape",
        "ingest": "native",
        "recursive": True,
        "patterns": [],
        "min_file_size": 100 * 1024 * 1024,
        "blacklist_patterns": [
            r".*\.url$", r".*\.txt$", r".*\.exe$", r".*\.part$", r"(?i).*sample.*", r".*广告.*",
        ],
        "write_nfo": True,
        "copy_resources": [],
        "path_template": "{number}/{number}",
    }


def _next_id(bucket: str) -> int:
    with STATE_LOCK:
        STATE["sequence"] += 1
        value = STATE["sequence"]
    return value


def _is_blacklisted(name: str, patterns: List[str]) -> bool:
    """与真 amane 一致：blacklist_patterns 是**正则**（glob 写法会被 422 拒绝）。"""
    for pattern in patterns or []:
        try:
            if re.search(str(pattern), name, re.IGNORECASE):
                return True
        except re.error:
            # 非法正则不当成命中，避免替身比真机宽松而掩盖配置错误
            continue
    return False


def _scan(library: Dict[str, Any], sub_path: str = "") -> Dict[str, Any]:
    """扫描目录并按库规则登记媒体文件（模拟 amane 的入库过滤）。"""
    root = str(library.get("path") or "")
    target = os.path.join(root, sub_path) if sub_path else root
    added, skipped = [], []
    if not root or not os.path.isdir(target):
        return {"added": added, "skipped": skipped, "scanned_dir": target}

    min_size = int(library.get("min_file_size") or 0)
    patterns = library.get("blacklist_patterns") or []
    for dirpath, _dirnames, filenames in os.walk(target):
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            ext = os.path.splitext(filename)[1].lower()
            if ext in VIDEO_EXTS and size >= min_size and not _is_blacklisted(filename, patterns):
                added.append({"path": full, "size": size, "name": filename})
            else:
                skipped.append({"path": full, "size": size,
                                "reason": "blacklist" if _is_blacklisted(filename, patterns)
                                else ("too_small" if size < min_size else "not_video")})
    return {"added": added, "skipped": skipped, "scanned_dir": target}


def _number_from_path(path: str) -> Optional[str]:
    """从文件名解析番号（替身不做完整解析，够演练用即可）。"""
    stem = os.path.splitext(os.path.basename(path))[0]
    match = re.search(r"([A-Za-z0-9]{2,10}-[0-9]{2,10})", stem)
    return (match.group(1) if match else stem).upper()


def _add_media(path: str, size: int, library_id: int) -> Dict[str, Any]:
    with STATE_LOCK:
        for item in STATE["media"].values():
            if item["path"] == path:
                return item
        media_id = STATE["next_media_id"]
        STATE["next_media_id"] += 1
        item = {
            "id": media_id,
            "path": path,
            "oshash": uuid.uuid4().hex[:16],
            "size": size,
            "duration": None,
            "codec": None,
            "number": _number_from_path(path),
            "status": "pending",
            "content_type": None,
            "mosaic": None,
            "has_subtitle": False,
            "definition": "1080p",
            "metadata_id": None,
            "library_id": library_id,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        STATE["media"][media_id] = item
        return dict(item)


def _run_task(task_id: int) -> None:
    with STATE_LOCK:
        task = STATE["tasks"][task_id]
        task["status"] = "running"
        payload = dict(task.get("payload") or {})
        task_type = payload.get("type")

    time.sleep(0.4)
    result: Dict[str, Any] = {}
    try:
        if task_type == "refresh":
            with STATE_LOCK:
                library = next((lib for lib in STATE["libraries"]
                                if lib["id"] == int(payload.get("library_id") or 0)), None)
            if library is None:
                raise ValueError("library_not_found")
            scan = _scan(library, str(payload.get("path") or ""))
            for entry in scan["added"]:
                _add_media(entry["path"], entry["size"], library["id"])
            result = {"added": len(scan["added"]), "skipped": len(scan["skipped"]),
                      "skipped_detail": scan["skipped"][:10], "scanned_dir": scan["scanned_dir"]}
        elif task_type == "scrape":
            number = payload.get("number")
            media_id = payload.get("media_id")
            with STATE_LOCK:
                target = None
                if media_id:
                    target = STATE["media"].get(int(media_id))
                if target is None and number:
                    wanted = str(number).upper()
                    target = next((m for m in STATE["media"].values()
                                   if str(m.get("number") or "").upper() == wanted), None)
                if target is None:
                    raise ValueError("media_not_found")
                if not target.get("number") and number:
                    target["number"] = str(number).upper()
                target["status"] = "scraped"
                target["content_type"] = payload.get("content_type") or "censored"
                target["metadata_id"] = target["id"]
                target["updated_at"] = time.time()
                result = {"media_id": target["id"], "number": target.get("number")}
        elif task_type == "organize":
            ids = payload.get("media_file_ids") or []
            moved = []
            with STATE_LOCK:
                for raw_id in ids:
                    item = STATE["media"].get(int(raw_id))
                    if not item:
                        continue
                    number = item.get("number") or os.path.splitext(os.path.basename(item["path"]))[0]
                    target = os.path.join(STATE["root"], "organized", str(number).upper(),
                                          f"{str(number).upper()}.mp4")
                    # 真搬家：Jellyfin 替身扫的是 organized 目录，不移动就永远看不见
                    source = item.get("path")
                    if source and os.path.isfile(source) and os.path.abspath(source) != os.path.abspath(target):
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        if os.path.exists(target):
                            os.remove(target)
                        shutil.move(source, target)
                    item["path"] = target
                    item["updated_at"] = time.time()
                    moved.append({"id": item["id"], "path": item["path"]})
            result = {"moved": moved}
        elif task_type == "cleanup":
            result = {"remove_missing_files": payload.get("remove_missing_files", True)}
        status, error = "done", None
    except Exception as exc:  # 演练环境：把失败如实写进任务
        status, error = "failed", str(exc)

    with STATE_LOCK:
        task = STATE["tasks"][task_id]
        task["status"] = status
        task["error"] = error
        task["result"] = result
        task["finished_at"] = time.time()


class TaskSubmission(BaseModel):
    type: str
    library_id: Optional[int] = None
    path: Optional[str] = ""
    scan: Optional[List[str]] = None
    scrape: Optional[List[str]] = None
    number: Optional[str] = None
    media_id: Optional[int] = None
    media_file_ids: Optional[List[int]] = None
    content_type: Optional[str] = None
    write_nfo: Optional[bool] = None
    recursive: Optional[bool] = None
    remove_missing_files: Optional[bool] = None
    remove_unreferenced_resources: Optional[bool] = None


@app.on_event("startup")
def _bootstrap() -> None:
    root = STATE["root"]
    if root:
        os.makedirs(root, exist_ok=True)
    with STATE_LOCK:
        if not STATE["libraries"]:
            STATE["libraries"] = [_library_defaults(root)]


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "version": app.version}


@app.get("/api/libraries")
def list_libraries(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_token(authorization)
    with STATE_LOCK:
        return {"items": [dict(lib) for lib in STATE["libraries"]]}


@app.get("/api/libraries/{library_id}")
def get_library(library_id: int, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_token(authorization)
    with STATE_LOCK:
        library = next((lib for lib in STATE["libraries"] if lib["id"] == library_id), None)
    if not library:
        raise HTTPException(status_code=404, detail="library_not_found")
    return dict(library)


@app.get("/api/media")
def list_media(
    authorization: Optional[str] = Header(None),
    status: Optional[str] = None,
    search: Optional[str] = None,
    library_id: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    _require_token(authorization)
    with STATE_LOCK:
        items = list(STATE["media"].values())
    if status:
        items = [m for m in items if m.get("status") == status]
    if library_id:
        items = [m for m in items if m.get("library_id") == library_id]
    keyword = str(search or "").strip().lower()
    if keyword:
        items = [m for m in items
                 if keyword in str(m.get("path") or "").lower()
                 or keyword in str(m.get("number") or "").lower()]
    items.sort(key=lambda m: m.get("updated_at") or 0, reverse=True)
    return {"items": [dict(m) for m in items[offset:offset + limit]], "total": len(items)}


@app.post("/api/tasks", status_code=202)
def submit_task(payload: TaskSubmission, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_token(authorization)
    body = payload.dict(exclude_none=True)
    with STATE_LOCK:
        task_id = STATE["next_task_id"]
        STATE["next_task_id"] += 1
        task = {
            "id": task_id,
            "type": body.get("type"),
            "status": "queued",
            "title": body.get("type"),
            "payload": body,
            "result": None,
            "error": None,
            "log_file": None,
            "retries": 0,
            "priority": 0,
            "root_task_id": task_id,
            "created_at": time.time(),
        }
        STATE["tasks"][task_id] = task
    threading.Thread(target=_run_task, args=(task_id,), daemon=True).start()
    return dict(task)


@app.get("/api/tasks")
def list_tasks(
    authorization: Optional[str] = Header(None),
    status: Optional[List[str]] = Query(None),
    limit: int = 50,
) -> Dict[str, Any]:
    _require_token(authorization)
    with STATE_LOCK:
        items = [dict(t) for t in STATE["tasks"].values()]
    if status:
        items = [t for t in items if t.get("status") in status]
    items.sort(key=lambda t: t.get("created_at") or 0, reverse=True)
    return {"items": items[:limit], "total": len(items)}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_token(authorization)
    with STATE_LOCK:
        task = STATE["tasks"].get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task_not_found")
    return dict(task)


@app.get("/api/tasks/{task_id}/report")
def get_task_report(task_id: int, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_token(authorization)
    with STATE_LOCK:
        task = STATE["tasks"].get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task_not_found")
    return {"task_id": task_id, "status": task.get("status"), "result": task.get("result"),
            "error": task.get("error")}


@app.get("/api/files")
def list_files(path: str = "/", authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_token(authorization)
    target = path if os.path.isabs(path) else os.path.join(STATE["root"] or ".", path)
    if not os.path.isdir(target):
        raise HTTPException(status_code=404, detail="path_not_found")
    entries = []
    for name in sorted(os.listdir(target)):
        full = os.path.join(target, name)
        entries.append({"name": name, "path": full, "is_dir": os.path.isdir(full)})
    return {"items": entries, "path": target}


@app.get("/api/mock/state")
def mock_state(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """演练用：直接看替身的内部状态。"""
    _require_token(authorization)
    with STATE_LOCK:
        return {
            "root": STATE["root"],
            "libraries": [dict(lib) for lib in STATE["libraries"]],
            "media": [dict(m) for m in STATE["media"].values()],
            "tasks": [dict(t) for t in STATE["tasks"].values()],
        }


@app.post("/api/mock/reset")
def mock_reset(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """演练用：清空媒体与任务，保留库配置。"""
    _require_token(authorization)
    with STATE_LOCK:
        STATE["media"] = {}
        STATE["tasks"] = {}
        STATE["next_media_id"] = 1
        STATE["next_task_id"] = 1
    return {"ok": True}


@app.post("/api/mock/libraries")
def mock_set_library(library: Dict[str, Any], authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """演练用：覆盖库配置（例如把 automation 改成 none 来验证盘点告警）。"""
    _require_token(authorization)
    with STATE_LOCK:
        base = _library_defaults(STATE["root"])
        base.update(library)
        STATE["libraries"] = [base]
    return dict(base)
