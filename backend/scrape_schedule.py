"""抓取 / 刷新节奏的唯一配置源（设置页「抓取节奏」读写这里）。

## 为什么要有这个模块

这些频次以前散落在四个地方：`config.py` 的模块级常量、`main.py` 里硬编码的
`hour == 22`、`actress_service` 的构造参数、`scrape_budget.DetailThrottle` 的
默认值。想调一次频次就得改代码、重新构建镜像、重新发布。

用户 2026-11-05 给的口径：

| 项 | 频率 |
|---|---|
| 首页 / 榜单预热 | 4 小时一次 |
| 订阅整轮 | 1 天一次（2026-11-09 用户定：每天刷新一次即可） |
| 订阅检索单次间隔 | 3 分钟内随机 |
| 订阅详情单次间隔 | 3 分钟内随机 |
| 女优元数据回填 | 7 天一次 |
| 外挂字幕补抓 | 3 分钟内随机一次 |
| 演员头像同步 | 7 天一次 |
| Jable 热门投递 | 每天 08:00 / 18:00 |

现在统一收敛：**出厂默认 = 上表**，运行时以 config 表 `scrape_schedule_v1`
覆盖（设置页可改数值与单位）。各循环每轮重新读取，保存即生效、不用重启。

优先级：**config 表覆盖 > 出厂默认**。

刻意不读环境变量：2026-11-05 上线时发现生产 compose 里还留着旧值
（`FEED_PRELOAD_REFRESH_SECONDS=10800`、`GFRIENDS_AVATAR_SYNC_INTERVAL_SECONDS=1800`），
设置页明明显示「未自定义」却跑着 3 小时 / 28 分钟，用户看到的和实际跑的不是一回事。
频率只允许有一个入口，所以 env 一律不参与。
"""

import random
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

CONFIG_KEY = "scrape_schedule_v1"

UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
UNIT_LABELS = {"second": "秒", "minute": "分钟", "hour": "小时", "day": "天"}

_TIME_SLOT = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
MAX_TIME_SLOTS = 8
# 随机间隔的下限：配置被写成 0 时不能真的连续开火。
MIN_RANDOM_GAP_SECONDS = 30


@dataclass(frozen=True)
class ScheduleSpec:
    """一个节奏项。`unit` 只是「建议单位」，用户可以在 `units` 里换。"""

    key: str
    label: str
    default_value: float
    unit: str
    units: Tuple[str, ...]
    min_seconds: float
    max_seconds: float
    description: str
    kind: str = "interval"  # interval | times
    default_times: Tuple[str, ...] = ()


SPECS: Tuple[ScheduleSpec, ...] = (
    ScheduleSpec(
        key="feed_preload",
        label="首页 / 榜单预热",
        default_value=4,
        unit="hour",
        units=("minute", "hour", "day"),
        min_seconds=60,
        max_seconds=7 * 86400,
        description="首页、字幕、高清、发现榜单的周期性重抓（只写本地缓存，不触发下载）",
    ),
    ScheduleSpec(
        key="subscription_cycle",
        label="订阅整轮周期",
        default_value=1,
        unit="day",
        units=("hour", "day"),
        min_seconds=60,
        max_seconds=30 * 86400,
        description="把所有订阅轮一遍的周期；跑不完就接着跑，不会加速打站点",
    ),
    ScheduleSpec(
        key="subscription_list_gap",
        label="订阅检索间隔",
        default_value=3,
        unit="minute",
        units=("second", "minute", "hour"),
        min_seconds=30,
        max_seconds=6 * 3600,
        description="每条订阅去站点列新片之间的间隔，实际取 0 ~ 该值的随机数（最小 30 秒，别填 0）",
    ),
    ScheduleSpec(
        key="subscription_detail_gap",
        label="订阅详情间隔",
        default_value=6,
        unit="second",
        units=("second", "minute"),
        min_seconds=1,
        max_seconds=600,
        description="同一条订阅内多个新片之间抓详情的间隔，取 0~该值随机；调大会明显拉长一轮",
    ),
    ScheduleSpec(
        key="actress_metadata",
        label="女优元数据回填",
        default_value=7,
        unit="day",
        units=("hour", "day"),
        min_seconds=3600,
        max_seconds=90 * 86400,
        description="netflav 补女优资料（生日/三围等）的轮询周期，候选本身也带重试冷却",
    ),
    ScheduleSpec(
        key="subtitle_backfill",
        label="外挂字幕补抓",
        default_value=3,
        unit="minute",
        units=("second", "minute", "hour"),
        min_seconds=30,
        max_seconds=24 * 3600,
        description="给还没字幕的本地片补抓，实际间隔取 0 ~ 该值的随机数（最小 30 秒）",
    ),
    ScheduleSpec(
        key="actress_avatar",
        label="演员头像同步",
        default_value=7,
        unit="day",
        units=("hour", "day"),
        min_seconds=3600,
        max_seconds=90 * 86400,
        description="gfriends 头像库回填（默认开关关闭），全量补完后自然空转",
    ),
    ScheduleSpec(
        key="jable_hot",
        label="热门投递时刻",
        default_value=0,
        unit="",
        units=(),
        min_seconds=0,
        max_seconds=0,
        description="每天在这些时刻（HH:MM）各投递一次 Jable 热门榜，可加多个",
        kind="times",
        default_times=("08:00", "18:00"),
    ),
)

SCHEDULES: Dict[str, ScheduleSpec] = {spec.key: spec for spec in SPECS}


def _default_db():
    # 延迟导入：测试里可以不碰真实数据库
    from database import db  # noqa: WPS433

    return db


def _as_float(raw: Any, fallback: float) -> float:
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return fallback
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return fallback
    return number


def get_overrides(db: Any = None) -> Dict[str, Any]:
    """读 config 表里的覆盖值。任何异常都退化成空（不能让设置读失败拖垮抓取）。"""
    try:
        raw = (db or _default_db()).get_config(CONFIG_KEY) or {}
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _unit_for(spec: ScheduleSpec, raw_unit: Any) -> str:
    unit = str(raw_unit or "").strip() or spec.unit
    return unit if unit in spec.units else spec.unit


def _tidy(value: float) -> float:
    """4.0 → 4，前端输入框里别显示 4.0。"""
    return int(value) if float(value).is_integer() else value


def resolve(key: str, db: Any = None) -> Tuple[float, str]:
    """返回 (数值, 单位)。config 覆盖 > 出厂默认（不读环境变量，见模块说明）。"""
    spec = SCHEDULES[key]
    override = get_overrides(db).get(key)
    if isinstance(override, dict) and override.get("value") is not None:
        return (
            _tidy(max(_as_float(override.get("value"), spec.default_value), 0.0)),
            _unit_for(spec, override.get("unit")),
        )
    return _tidy(float(spec.default_value)), spec.unit


def interval_seconds(key: str, db: Any = None) -> float:
    """该节奏项当前生效的秒数。"""
    value, unit = resolve(key, db)
    return max(value * UNIT_SECONDS.get(unit, 1), 0.0)


def random_delay_seconds(key: str, db: Any = None) -> float:
    """`0 ~ 该值` 的随机间隔。用于订阅检索 / 详情 / 字幕这类「不追求速度」的链路。

    配置被改成 0（或历史脏数据）时兜一个最小间隔：0 秒等于对站点连续开火。
    """
    total = interval_seconds(key, db)
    if total <= 0:
        return float(MIN_RANDOM_GAP_SECONDS)
    return random.uniform(0, total)


def time_slots(key: str = "jable_hot", db: Any = None) -> List[str]:
    """时刻列表（HH:MM，升序去重）。配置非法时回落默认值。"""
    spec = SCHEDULES[key]
    override = get_overrides(db).get(key)
    raw_times = None
    if isinstance(override, dict):
        raw_times = override.get("times")
    if not isinstance(raw_times, (list, tuple)) or not raw_times:
        return list(spec.default_times)
    cleaned = sorted({str(item).strip() for item in raw_times if _TIME_SLOT.match(str(item).strip())})
    return cleaned or list(spec.default_times)


def snapshot(db: Any = None) -> Dict[str, Any]:
    """给设置页用：每项的当前值、建议单位、可选单位与生效秒数。"""
    overridden = get_overrides(db)
    items: List[Dict[str, Any]] = []
    for spec in SPECS:
        base = {
            "key": spec.key,
            "label": spec.label,
            "description": spec.description,
            "kind": spec.kind,
            "configured": spec.key in overridden,
        }
        if spec.kind == "times":
            base.update({"times": time_slots(spec.key, db), "max_times": MAX_TIME_SLOTS})
        else:
            value, unit = resolve(spec.key, db)
            base.update(
                {
                    "value": value,
                    "unit": unit,
                    "default_unit": spec.unit,
                    "units": [
                        {"value": code, "label": UNIT_LABELS[code]} for code in spec.units
                    ],
                    "seconds": int(interval_seconds(spec.key, db)),
                }
            )
        items.append(base)
    return {"items": items, "config_key": CONFIG_KEY}


def validate_payload(payload: Any) -> List[str]:
    """校验设置页提交的整份节奏配置，返回错误码列表（空表示通过）。"""
    if not isinstance(payload, dict):
        return ["schedule_payload_invalid"]
    errors: List[str] = []
    for key, raw in payload.items():
        spec = SCHEDULES.get(key)
        if spec is None:
            errors.append(f"schedule_unknown_key:{key}")
            continue
        if spec.kind == "times":
            times = raw.get("times") if isinstance(raw, dict) else raw
            if not isinstance(times, (list, tuple)) or not times:
                errors.append(f"{key}_times_required")
                continue
            if len(times) > MAX_TIME_SLOTS:
                errors.append(f"{key}_too_many_times")
            for item in times:
                if not _TIME_SLOT.match(str(item).strip()):
                    errors.append(f"{key}_invalid_time")
                    break
            continue

        if not isinstance(raw, dict):
            errors.append(f"{key}_invalid_payload")
            continue
        value = _as_float(raw.get("value"), -1)
        if value < 0:
            errors.append(f"{key}_invalid_number")
            continue
        unit = str(raw.get("unit") or spec.unit)
        if unit not in spec.units:
            errors.append(f"{key}_invalid_unit")
            continue
        seconds = value * UNIT_SECONDS[unit]
        if seconds < spec.min_seconds:
            errors.append(f"{key}_below_min")
        elif seconds > spec.max_seconds:
            errors.append(f"{key}_above_max")
    return errors


def save_payload(payload: Any, db: Any = None) -> Dict[str, Any]:
    """合并写入（只覆盖提交上来的 key）。返回保存后的快照。"""
    target = db or _default_db()
    merged = dict(get_overrides(target))
    for key, raw in (payload or {}).items():
        spec = SCHEDULES.get(key)
        if spec is None:
            continue
        if spec.kind == "times":
            times = raw.get("times") if isinstance(raw, dict) else raw
            merged[key] = {
                "times": sorted({str(item).strip() for item in (times or []) if _TIME_SLOT.match(str(item).strip())})
            }
        else:
            merged[key] = {
                "value": _as_float((raw or {}).get("value"), spec.default_value),
                "unit": _unit_for(spec, (raw or {}).get("unit")),
            }
    target.set_config(CONFIG_KEY, merged)
    return snapshot(target)
