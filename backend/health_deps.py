"""辅助服务心跳与健康依赖检查（供 `GET /api/health/deps` 与后台告警循环使用）。

背景：NAS 上已经没有任何监控（netdata 已彻底下线，beszel 的 hub `43.160.250.6:8090`
不可达），`bus_backup` / `bus_backup_files` / `pt_ignore_guard` 三个无状态辅助容器
挂了只能人去看日志。备份又是最典型的「静默失败」场景：脚本死了、盘满了、源库被锁了，
都不会有人知道，直到某天需要恢复才发现备份是空的或过期的。

方案：三个容器每轮跑完往 `DATA_DIR/heartbeat/<service>.json` 写一个 JSON 心跳，
本模块负责读出来判定健康度。四种状态：

    ok       —— 心跳新鲜且本轮 `ok=true`
    failing  —— 心跳新鲜但本轮 `ok=false`：**活着，但这一轮失败了**
    stale    —— 心跳超时没更新：**整个进程死了或卡住了**
    unknown  —— 心跳目录不存在（本地开发 / 非 NAS 部署，不告警）

bus_terminal 只挂了 `/app/data`，**看不到备份产物本身**（PG dump 在 storage 的
`backups/`、文件备份在 storage 的 `/bk`），所以产物的名字 / 大小 / 时间也由
心跳文件带过来，本模块据此再判一遍产物新鲜度 —— 心跳新鲜只说明「进程活着」，
产物新鲜才说明「备份真的在产出」。
"""
import json
import os
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional

import config

HEARTBEAT_DIRNAME = "heartbeat"
ALERT_STATE_FILENAME = "alerts.json"

STATUS_OK = "ok"
STATUS_FAILING = "failing"
STATUS_STALE = "stale"
STATUS_UNKNOWN = "unknown"

# 心跳文件里 artifacts[].kind 的预期种类与最长可接受年龄。"daily" 走每日档阈值
# （默认 36h，容忍错过一次 + 余量），"weekly" 走每周档（jf-full 只在周日跑）。
ARTIFACT_KINDS = (
    {"kind": "pg_dump", "cadence": "daily"},
    {"kind": "amane_sqlite", "cadence": "daily"},
    {"kind": "jellyfin_core", "cadence": "daily"},
    {"kind": "jellyfin_full", "cadence": "weekly"},
)

# service -> 心跳最长可接受年龄的档位
SERVICE_CADENCE = {
    "bus_backup": "daily",
    "bus_backup_files": "daily",
    "pt_ignore_guard": "guard",
}

_ISSUE_SEVERITY = {
    "heartbeat_stale": "critical",
    "heartbeat_missing": "critical",
    "heartbeat_unreadable": "critical",
    "heartbeat_failing": "warning",
}

_ISSUE_TEXT = {
    "heartbeat_stale": "心跳超时未更新（进程可能已退出或卡死）",
    "heartbeat_missing": "从未写过心跳文件",
    "heartbeat_unreadable": "心跳文件无法解析",
    "heartbeat_failing": "心跳新鲜但本轮执行失败",
}


def heartbeat_dir() -> str:
    return os.path.join(config.DATA_DIR, HEARTBEAT_DIRNAME)


def thresholds() -> Dict[str, int]:
    daily = config.HEALTH_BACKUP_MAX_AGE_SECONDS
    weekly = config.HEALTH_WEEKLY_BACKUP_MAX_AGE_SECONDS
    return {
        "daily": daily,
        "weekly": weekly,
        "guard": config.HEALTH_GUARD_MAX_AGE_SECONDS,
        "alert_cooldown_seconds": config.HEALTH_ALERT_COOLDOWN_SECONDS,
        "check_interval_seconds": config.HEALTH_DEPS_CHECK_SECONDS,
        "artifact": {item["kind"]: (daily if item["cadence"] == "daily" else weekly)
                     for item in ARTIFACT_KINDS},
    }


def _max_age_for(cadence: str, table: Dict[str, int]) -> int:
    if cadence == "guard":
        return table["guard"]
    if cadence == "weekly":
        return table["weekly"]
    return table["daily"]


def _read_heartbeat(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _as_float(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result != float("inf") else None


def _build_artifact(raw: Any, now: float, table: Dict[str, int]) -> Optional[Dict[str, Any]]:
    """把心跳里带过来的一份产物描述补全成带新鲜度判定的结构。"""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip()
    if not kind:
        return None
    entry: Dict[str, Any] = {
        "kind": kind,
        "name": str(raw.get("name") or ""),
        "bytes": int(_as_float(raw.get("bytes")) or 0),
    }
    ts = _as_float(raw.get("ts"))
    max_age = table["artifact"].get(kind, table["daily"])
    if ts is None:
        entry.update({"ts": None, "age_seconds": None, "max_age_seconds": max_age,
                      "status": STATUS_STALE})
        return entry
    age = max(0.0, now - ts)
    entry.update({
        "ts": ts,
        "age_seconds": round(age, 1),
        "max_age_seconds": max_age,
        "status": STATUS_OK if age <= max_age else STATUS_STALE,
    })
    return entry


def evaluate_service(service: str, now: float, table: Dict[str, int],
                     directory: str) -> Dict[str, Any]:
    """判定单个辅助服务的健康度。"""
    cadence = SERVICE_CADENCE.get(service, "daily")
    max_age = _max_age_for(cadence, table)
    result: Dict[str, Any] = {
        "service": service,
        "max_age_seconds": max_age,
        "heartbeat": None,
        "artifacts": [],
        "issues": [],
    }

    path = os.path.join(directory, f"{service}.json")
    if not os.path.exists(path):
        result["issues"].append("heartbeat_missing")
        result["status"] = STATUS_STALE
        return result

    payload = _read_heartbeat(path)
    if payload is None:
        result["issues"].append("heartbeat_unreadable")
        result["status"] = STATUS_STALE
        return result

    ts = _as_float(payload.get("ts"))
    if ts is None:
        ts = os.path.getmtime(path)
    age = max(0.0, now - ts)
    result["heartbeat"] = {
        "path": path,
        "ts": ts,
        "age_seconds": round(age, 1),
        "iso": payload.get("iso"),
        "ok": payload.get("ok"),
        "elapsed_s": _as_float(payload.get("elapsed_s")),
        "detail": payload.get("detail") if isinstance(payload.get("detail"), dict) else {},
    }

    if age > max_age:
        result["issues"].append("heartbeat_stale")
    elif payload.get("ok") is False:
        # 心跳新鲜 = 进程活着；ok=false = 这一轮没干成（备份失败 / 守卫报错）
        result["issues"].append("heartbeat_failing")

    for raw in payload.get("artifacts") or []:
        entry = _build_artifact(raw, now, table)
        if entry is None:
            continue
        result["artifacts"].append(entry)
        if entry["status"] != STATUS_OK:
            result["issues"].append(f"artifact_stale:{entry['kind']}")

    result["status"] = STATUS_OK if not result["issues"] else (
        STATUS_STALE if any(issue.startswith(("heartbeat_stale", "heartbeat_missing",
                                              "heartbeat_unreadable"))
                            for issue in result["issues"]) else STATUS_FAILING
    )
    return result


def evaluate(now: Optional[float] = None, directory: Optional[str] = None) -> Dict[str, Any]:
    """读出全部心跳并判定，返回 `/api/health/deps` 的载荷。"""
    now = time.time() if now is None else now
    directory = directory or heartbeat_dir()
    table = thresholds()

    payload: Dict[str, Any] = {
        "success": True,
        "timestamp": now,
        "heartbeat_dir": directory,
        "thresholds": table,
        "services": [],
        "alerts": [],
        "issues": [],
        "status": STATUS_OK,
    }

    if not os.path.isdir(directory):
        payload["status"] = STATUS_UNKNOWN
        payload["issues"].append("heartbeat_dir_missing")
        return payload

    for service in SERVICE_CADENCE:
        payload["services"].append(evaluate_service(service, now, table, directory))

    for item in payload["services"]:
        for issue in item["issues"]:
            if issue.startswith("artifact_stale:"):
                kind = issue.split(":", 1)[1]
                message = f"{item['service']} 的产物 {kind} 已过期（超过 {_artifact_max_age(table, kind)} 秒没有新产物）"
            else:
                message = f"{item['service']} {_ISSUE_TEXT.get(issue, issue)}"
            payload["alerts"].append({
                "key": f"{item['service']}/{issue}",
                "service": item["service"],
                "issue": issue,
                "severity": _ISSUE_SEVERITY.get(
                    issue, "warning" if issue.startswith("artifact_stale:") else "critical"),
                "title": f"辅助服务 {item['service']} 异常",
                "message": message,
            })

    payload["status"] = STATUS_OK if not payload["alerts"] else "degraded"
    return payload


def _artifact_max_age(table: Dict[str, int], kind: str) -> int:
    return table["artifact"].get(kind, table["daily"])


# ============ 告警去重 / 冷却 ============

def _load_alert_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save_alert_state(path: str, state: Dict[str, Any]) -> None:
    """原子写，避免读到写了一半的 JSON。"""
    directory = os.path.dirname(path) or "."
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, prefix=".alerts-", suffix=".tmp", delete=False
    )
    try:
        with handle:
            json.dump(state, handle, ensure_ascii=False)
        os.replace(handle.name, path)
    except Exception:
        try:
            os.remove(handle.name)
        except OSError:
            pass
        raise


def dispatch_health_alerts(
    alerts: List[Dict[str, Any]],
    *,
    cooldown_seconds: int,
    state_path: str,
    now: Optional[float] = None,
    sender: Optional[Callable[[str, str], Any]] = None,
    logger=None,
) -> List[Dict[str, Any]]:
    """按 key 去重 + 冷却后发送告警，返回本次真正发出的告警。

    同一个 key 在 `cooldown_seconds` 内只发一次（期间命中只累加计数，不重复打扰）；
    key 消失（恢复）时清掉状态并在日志里留一行。
    """
    now = time.time() if now is None else now
    state = _load_alert_state(state_path)
    active = {alert["key"]: alert for alert in alerts}
    sent: List[Dict[str, Any]] = []

    for key, alert in active.items():
        entry = state.get(key) or {}
        last_sent = _as_float(entry.get("last_sent"))
        if last_sent is not None and now - last_sent < cooldown_seconds:
            entry["suppressed"] = int(entry.get("suppressed") or 0) + 1
            state[key] = entry
            continue
        delivered = False
        if sender is not None:
            try:
                delivered = bool(sender(alert.get("title") or key, alert.get("message") or ""))
            except Exception as exc:  # 告警通道挂了不能拖垮整个检查循环
                if logger is not None:
                    logger.warning("Health deps alert send failed: %s %s", key, type(exc).__name__)
        state[key] = {
            "last_sent": now,
            "count": int(entry.get("count") or 0) + 1,
            "suppressed": 0,
            "delivered": delivered,
            "last_message": alert.get("message") or "",
        }
        if delivered:
            sent.append(alert)
        elif logger is not None:
            logger.warning("Health deps alert not delivered: %s（通道未启用或发送失败）", key)

    for key in [key for key in state if key not in active]:
        if logger is not None:
            logger.info("Health deps alert recovered: %s", key)
        state.pop(key, None)

    try:
        _save_alert_state(state_path, state)
    except OSError as exc:
        if logger is not None:
            logger.warning("Health deps alert state write failed: %s", type(exc).__name__)
    return sent
