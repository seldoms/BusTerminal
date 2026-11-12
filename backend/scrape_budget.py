"""站点抓取的节奏控制，分两类，别混：

1. **首页 / 排行榜刷新**（`preload_*`、`jable_hot`、feed 预热）：每站每天最多 10 次，
   见 `ScrapeBudget`。用户口径原话：「每个站点也没有 10 个页面吧」——这一类就是
   站点上那几个固定榜单页的重复刷新，10 次足够。
2. **订阅流程**：
   - 列表检索（每条订阅去站点找新片）：**不占日配额**，间隔取 `0 ~ 3 分钟` 的随机数，
     见 `DetailThrottle` + `scrape_schedule`。理由见下；
   - 详情抓取（发现内容后点详情、拿下载按钮）：同样取 `0 ~ 3 分钟` 随机数，另一个计数器。

## 为什么订阅检索不能吃日配额

用户口径是「订阅流程抓取不受上述次数限制，但要控频、间隔加随机抖动」。
2026-11-04 实测库里有 **308 条订阅**，其中 294 条是 `jellyfin:person:` 内部订阅，
按「每站每天 10 次」根本轮不完（要 59 天），整轮订阅连连续几天以
`subscription_sources_unavailable` 收尾，功能等于停摆。

2026-11-05 用户进一步降频：整轮 12 小时、单条间隔 3 分钟内随机 —— 不追求速度，
后台慢慢做。频次的唯一入口现在在 `scrape_schedule.py`（设置页可改）。

## 为什么要有配额

2026-11-03 实测 feed 预热 30 分钟一轮、每轮 24 次抓取，一天上千次请求；
jable.tv / javdb.com 因此被 Cloudflare 按出口 IP 拉黑（同一刻 javbus 仍 200，
说明是封 IP 而不是限速）。熔断只能事后止损，配额是事前刹车。

计数落 config 表（`scrape_budget:preload:<源>` / `scrape_subscription_throttle` /
`scrape_detail_throttle`），重启不清零，配额按自然日归零。
"""

import os
import random
import threading
import time
from datetime import date
from typing import Any, Dict, Optional

import scrape_schedule

# 首页/排行榜每天每站的抓取上限。
# 2026-11-05 从 10 提到 30：用户把首页/榜单节奏定成「4 小时一次」后，
# javbus 一轮就是 首页+字幕+高清+榜单 4 次，6 轮/天 = 24 次，10 次会在第 3 轮
# 就把额度烧光、后面全部只能读缓存 —— 设置里的 4 小时等于形同虚设。
# 30 覆盖 24 次并留出翻页余量；仍能挡住历史上那种「30 分钟一轮、一天上千次」
# 的调用（真正会招来 Cloudflare 的量级）。
DEFAULT_PRELOAD_LIMIT = 30
PRELOAD_LIMIT_ENV = "SCRAPE_PRELOAD_LIMIT_PER_SOURCE"

PURPOSE_PRELOAD = "preload"
PURPOSES = (PURPOSE_PRELOAD,)

CONFIG_PREFIX = "scrape_budget:"

# 订阅检索与详情抓取共用同一套节奏（0 ~ 3 分钟随机），但计数分开：
# 否则详情抓完一次会让紧随其后的订阅检索白等一轮。
# 实际生效值来自 scrape_schedule（设置页「抓取节奏」可改），下面的常量只是
# 不传 schedule_key 时的兜底。
DETAIL_MIN_INTERVAL_SECONDS = 0
DETAIL_JITTER_SECONDS = 180
DETAIL_THROTTLE_KEY = "scrape_detail_throttle"
SUBSCRIPTION_LIST_THROTTLE_KEY = "scrape_subscription_throttle"

# 站点域名 → 配额计数用的源名。归不进来的算 other，同样受配额约束。
# javbus 有大量官方防屏蔽域名（首页公告的 .casa 系 + 历史 .cyou），必须全部
# 归到 javbus 桶，否则同一站点被拆成多个桶，每个桶各算一份额度，等于配额失效。
_SITE_TO_SOURCE = (
    ("jable.tv", "jable"),
    ("jable.com", "jable"),
    ("javbus.com", "javbus"),
    ("javbus.casa", "javbus"),
    ("javbus.cyou", "javbus"),
    ("busdmm.casa", "javbus"),
    ("busdmm.cyou", "javbus"),
    ("busfan.casa", "javbus"),
    ("busfan.cyou", "javbus"),
    ("seedmm.casa", "javbus"),
    ("seedmm.cyou", "javbus"),
    ("dmmbus.casa", "javbus"),
    ("dmmsee.casa", "javbus"),
    ("cdnbus.casa", "javbus"),
    ("buscdn.casa", "javbus"),
    ("busjav.casa", "javbus"),
    ("seejav.bond", "javbus"),
    ("seejav.cyou", "javbus"),
    ("javbus.hair", "javbus"),
    ("javdb.com", "javdb"),
    ("airav.io", "airav"),
    ("airav.wiki", "airav"),
    ("airav.cc", "airav"),
    ("netflav.com", "netflav"),
)

# 配额/节流统计覆盖的源名（快照与告警用）。
SOURCES = ("jable", "javbus", "javdb", "airav", "netflav", "other")


def limit_for(purpose: str = PURPOSE_PRELOAD) -> int:
    """某个用途每天每站允许的抓取次数。"""
    try:
        value = int(os.getenv(PRELOAD_LIMIT_ENV, "") or DEFAULT_PRELOAD_LIMIT)
    except ValueError:
        value = DEFAULT_PRELOAD_LIMIT
    return max(value, 0)


def daily_limit() -> int:
    """每站每日上限。订阅检索不走这里，它按「0~设置值随机」节流。"""
    return sum(limit_for(purpose) for purpose in PURPOSES)


def source_for_url(url: Any) -> str:
    """把站点 URL 归到配额用的源名。"""
    text = str(url or "").lower()
    for site, source in _SITE_TO_SOURCE:
        if site in text:
            return source
    return "other"


class ScrapeBudget:
    def __init__(self, db: Any, logger: Any):
        self.db = db
        self.logger = logger
        self._lock = threading.Lock()

    def _today(self) -> str:
        return date.today().isoformat()

    def _key(self, source: str, purpose: str) -> str:
        return f"{CONFIG_PREFIX}{purpose}:{source}"

    def _read(self, source: str, purpose: str) -> Dict[str, Any]:
        state = self.db.get_config(self._key(source, purpose)) or {}
        return state if isinstance(state, dict) else {}

    def _used_one(self, source: str, purpose: str) -> int:
        state = self._read(source, purpose)
        if state.get("date") != self._today():
            return 0
        try:
            return int(state.get("count") or 0)
        except (TypeError, ValueError):
            return 0

    def used(self, source: str, purpose: Optional[str] = None) -> int:
        purposes = PURPOSES if purpose is None else (purpose,)
        return sum(self._used_one(source, item) for item in purposes)

    def remaining(self, source: str, purpose: Optional[str] = None) -> int:
        if purpose is None:
            return max(daily_limit() - self.used(source), 0)
        return max(limit_for(purpose) - self._used_one(source, purpose), 0)

    def try_acquire(self, source: str, purpose: str = PURPOSE_PRELOAD) -> bool:
        """占用一次额度。返回 False 表示今天的次数已用完，不要再打站点。"""
        limit = limit_for(purpose)
        if limit <= 0:
            return False
        today = self._today()
        key = self._key(source, purpose)
        with self._lock:
            state = self._read(source, purpose)
            used = 0
            if state.get("date") == today:
                try:
                    used = int(state.get("count") or 0)
                except (TypeError, ValueError):
                    used = 0
            if used >= limit:
                return False
            try:
                self.db.set_config(key, {"date": today, "count": used + 1})
            except Exception as exc:
                # 记不上账就放行：宁可偶尔多打一次，也不要让整个抓取链路停摆。
                self.logger.warning(
                    "Scrape budget write failed for %s/%s: %s",
                    purpose,
                    source,
                    type(exc).__name__,
                )
            return True

    def snapshot(self) -> Dict[str, Any]:
        """给接口用：各源今天的已用/剩余额度。"""
        total = daily_limit()
        result: Dict[str, Any] = {}
        for source in SOURCES:
            used = self.used(source)
            result[source] = {
                "used": used,
                "remaining": max(total - used, 0),
                "limit": total,
            }
        return result


class DetailThrottle:
    """抓取节流：默认每次至少隔 `min_interval`，再叠 0~`jitter` 秒随机抖动。

    传了 `schedule_key` 时完全由 `scrape_schedule` 决定：间隔取
    `0 ~ 该配置值` 的随机数（用户口径「3 分钟内的随机数」）。
    固定节奏本身是可识别的，所以间隔必须带随机。

    上一次的抓取时间落 config 表，重启后不会立刻再打一次。
    订阅检索与详情抓取各用一个 `config_key`，计数互不干扰。
    """

    def __init__(
        self,
        db: Any,
        logger: Any,
        stop_event: Any = None,
        min_interval: int = DETAIL_MIN_INTERVAL_SECONDS,
        jitter: int = DETAIL_JITTER_SECONDS,
        config_key: str = DETAIL_THROTTLE_KEY,
        schedule_key: Optional[str] = None,
    ):
        self.db = db
        self.logger = logger
        self.stop_event = stop_event
        self.min_interval = max(int(min_interval), 0)
        self.jitter = max(int(jitter), 0)
        self.config_key = config_key
        self.schedule_key = schedule_key
        self._lock = threading.Lock()

    def _next_gap(self) -> float:
        if self.schedule_key:
            return scrape_schedule.random_delay_seconds(self.schedule_key, self.db)
        return self.min_interval + random.uniform(0, self.jitter)

    def _read_last(self) -> float:
        state = self.db.get_config(self.config_key) or {}
        try:
            return float((state or {}).get("last_at") or 0)
        except (TypeError, ValueError, AttributeError):
            return 0.0

    def wait(self) -> float:
        """阻塞到下一次允许抓取的时间点，返回实际等待秒数。

        stop_event 被置位时提前返回 0，让订阅循环能正常退出。
        """
        with self._lock:
            gap = self._next_gap()
            delay = max(self._read_last() + gap - time.time(), 0)
        if delay > 0:
            if self.stop_event is not None and hasattr(self.stop_event, "wait"):
                if self.stop_event.wait(delay):
                    return 0.0
            else:
                time.sleep(delay)
        with self._lock:
            try:
                self.db.set_config(self.config_key, {"last_at": time.time()})
            except Exception as exc:
                self.logger.warning(
                    "Detail throttle write failed: %s", type(exc).__name__
                )
        return delay
