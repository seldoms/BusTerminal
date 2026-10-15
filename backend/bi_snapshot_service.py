"""BI 看板快照与调度

看板数据要跑 9~13 秒（145 万本地库 + 演员关联统计 + 分页读 Jellyfin），
不该压在页面请求上，也不该每次点开就现算。

按用户要求：**默认每天只汇总一次**（可配置为每天两次或实时），
结果落 `config` 表，接口只读快照 —— 打开看板永远秒开。
"""
import json
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from bi_dashboard_service import (
    BI_REFRESH_MODE_DAILY,
    BI_REFRESH_MODE_KEY,
    BI_REFRESH_MODE_REALTIME,
    BI_REFRESH_MODE_TWICE,
    BI_REFRESH_SLOTS,
    BI_SNAPSHOT_KEY,
    resolve_refresh_slots,
)
from insight_stats_service import DISPLAY_TZ

# 快照存config 表时用的键
BI_SNAPSHOT_META_KEY = "bi_snapshot_meta"
BI_ENABLED_KEY = "bi_dashboard_enabled"

# 循环自检间隔（秒）。24 小时一轮足够 —— 每天只需命中一到两个时刻。
BI_LOOP_INTERVAL_SECONDS = 60


def _slots_passed(now: datetime, slots) -> List[str]:
    """今天已过的刷新时刻。"""
    return [f"{h:02d}:{m:02d}" for h, m in slots if (h, m) <= (now.hour, now.minute)]


def should_refresh(meta: Optional[Dict[str, Any]], now: Optional[datetime] = None, slots=None) -> bool:
    """判断是否到了该刷新的时刻。

    规则：当天某个刷新时刻已过、而上次刷新**不在**今天 → 需要刷新。
    这样无论进程何时重启，当天该刷的都只刷一次，不会因为重启反复重算。
    """
    now = now or datetime.now(DISPLAY_TZ)
    slots = slots or BI_REFRESH_SLOTS
    today = now.strftime("%Y-%m-%d")
    if not meta:
        return True
    last_at = str(meta.get("refreshed_at") or "")
    if not last_at:
        return True
    if not last_at.startswith(today):
        # 今天还没刷过：看是否已到任一时刻
        return bool(_slots_passed(now, slots))
    return False


class BISnapshotService:
    def __init__(
        self,
        *,
        db: Any,
        builder: Callable[..., Dict[str, Any]],
        logger: Any,
        uptime_provider: Optional[Callable[[], Optional[float]]] = None,
    ):
        self.db = db
        self.builder = builder
        self.logger = logger
        self.uptime_provider = uptime_provider

    # ------------------------------------------------------------------
    def read_meta(self) -> Dict[str, Any]:
        raw = self.db.get_config(BI_SNAPSHOT_META_KEY)
        if not raw:
            return {}
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except Exception:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return raw if isinstance(raw, dict) else {}

    def read_snapshot(self) -> Optional[Dict[str, Any]]:
        raw = self.db.get_config(BI_SNAPSHOT_KEY)
        if not raw:
            return None
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except Exception:
                return None
            return parsed if isinstance(parsed, dict) else None
        return raw if isinstance(raw, dict) else None

    def read_mode(self) -> str:
        """读取刷新频率配置。取值非法或缺失时退回默认的「每天一次」。"""
        try:
            raw = self.db.get_config(BI_REFRESH_MODE_KEY)
        except Exception:
            raw = None
        mode = str(raw or "").strip() or BI_REFRESH_MODE_DAILY
        if mode not in (BI_REFRESH_MODE_DAILY, BI_REFRESH_MODE_TWICE, BI_REFRESH_MODE_REALTIME):
            return BI_REFRESH_MODE_DAILY
        return mode

    @staticmethod
    def slot_labels(mode: str) -> List[str]:
        return [f"{h:02d}:{m:02d}" for h, m in resolve_refresh_slots(mode)]

    def get_payload(self, *, allow_refresh: bool = False) -> Dict[str, Any]:
        """给接口用：**只读快照**。

        只有显式 allow_refresh（后台循环、或设置页点「立即重算」）才会现算。
        打开看板绝不现算 —— 用户明确要求过：点开就刷新显得很傻瓜，
        而且一次聚合要9~13 秒，压在页面请求路径上必然卡住首屏。
        """
        meta = self.read_meta()
        mode = self.read_mode()
        labels = self.slot_labels(mode)
        snapshot = self.read_snapshot()

        # 一次快照都没有时只能算一次，否则页面彻底空白。
        if not snapshot:
            if not allow_refresh:
                return {
                    "generated_at": None,
                    "warnings": ["尚无汇总快照，请到 设置 → 缓存与性能 点「立即重算」生成一次"],
                    "library": {}, "cast": {}, "playback": {}, "downloads": {},
                    "storage": {}, "runtime": {}, "stale": True, "meta": meta,
                    "refresh_mode": mode, "refresh_slots": labels,
                }
            return {**self.refresh(reason="first_build"),
                    "refresh_mode": mode, "refresh_slots": labels}

        # 实时模式是用户显式选的，此时允许每次现算。
        if mode == BI_REFRESH_MODE_REALTIME and allow_refresh:
            return {**self.refresh(reason="realtime"),
                    "refresh_mode": mode, "refresh_slots": labels}

        return {
            **snapshot,
            "stale": should_refresh(meta, slots=resolve_refresh_slots(mode)),
            "meta": meta,
            "refresh_mode": mode,
            "refresh_slots": labels,
        }

    # ------------------------------------------------------------------
    def refresh(self, *, reason: str = "manual") -> Dict[str, Any]:
        """现算并落快照。约 9~13 秒，失败不抛异常（保留旧快照）。"""
        uptime = None
        if self.uptime_provider:
            try:
                uptime = self.uptime_provider()
            except Exception:
                uptime = None
        try:
            payload = self.builder(container_uptime_hours=uptime)
        except Exception as exc:
            self.logger.error("BI snapshot build failed (%s): %s", reason, type(exc).__name__)
            old = self.read_snapshot()
            if old:
                return {**old, "stale": True, "refresh_error": type(exc).__name__, "meta": self.read_meta()}
            raise

        mode = self.read_mode()
        labels = self.slot_labels(mode)
        meta = {
            "refreshed_at": datetime.now(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "reason": reason,
            "slots": labels,
        }
        self.db.set_config(BI_SNAPSHOT_KEY, payload)
        self.db.set_config(BI_SNAPSHOT_META_KEY, meta)
        return {**payload, "stale": False, "meta": meta,
                "refresh_mode": mode, "refresh_slots": labels}

    # ------------------------------------------------------------------
    def loop(self, stop_event: Any) -> None:
        """后台循环：到点就刷一次。默认不自动跑，需显式开启。"""
        while True:
            try:
                meta = self.read_meta()
                mode = self.read_mode()
                if should_refresh(meta, slots=resolve_refresh_slots(mode)):
                    log = self.refresh(reason="scheduled")
                    self.logger.info(
                        "BI snapshot refreshed at %s (mode=%s, warnings=%s)",
                        log.get("meta", {}).get("refreshed_at"),
                        mode,
                        len(log.get("warnings") or []),
                    )
            except Exception as exc:
                self.logger.warning("BI snapshot loop error: %s", type(exc).__name__)
            if stop_event.wait(BI_LOOP_INTERVAL_SECONDS):
                return