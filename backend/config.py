"""
集中配置模块
从环境变量读取配置项，提供统一的默认值
"""
import os
import logging
import math
import re
from urllib.parse import urlparse

# 尝试加载 .env 文件
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass



# ========== 日志配置 ==========
LOG_LEVEL: str = os.getenv('LOG_LEVEL', 'INFO').upper()

def get_int_env(key: str, default: int, minimum: int | None = None) -> int:
    raw = os.getenv(key)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def get_float_env(
    key: str,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    raw = os.getenv(key)
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    if minimum is not None and value < minimum:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


def get_log_level() -> int:
    """将字符串日志级别转换为 logging 常量"""
    levels = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO,
        'WARNING': logging.WARNING,
        'ERROR': logging.ERROR,
        'CRITICAL': logging.CRITICAL,
    }
    return levels.get(LOG_LEVEL, logging.INFO)

# ========== 后台任务配置 ==========
# 订阅自动检查间隔（秒）
SUBSCRIPTION_CHECK_INTERVAL: int = get_int_env('SUBSCRIPTION_CHECK_INTERVAL', 4 * 3600, minimum=1)

# 智能过滤任务间隔（秒）
GROOM_TASK_INTERVAL: int = get_int_env('GROOM_TASK_INTERVAL', 60, minimum=1)

# 做种维护巡检间隔（秒），默认 4 小时。
# 停止超时做种属于低频维护，不需要跟智能过滤一样每分钟轮询。
SEEDING_MAINTENANCE_INTERVAL: int = get_int_env('SEEDING_MAINTENANCE_INTERVAL', 4 * 3600, minimum=1)

# 磁盘监控任务间隔（秒）
DISK_MONITOR_INTERVAL: int = get_int_env('DISK_MONITOR_INTERVAL', 60, minimum=1)

# 磁盘空间阈值（字节），默认 800GB
DISK_SPACE_THRESHOLD: int = get_int_env('DISK_SPACE_THRESHOLD', 800 * 1024 * 1024 * 1024, minimum=0)

# 做种时间限制（分钟），默认 60 分钟
SEEDING_TIME_LIMIT: int = get_int_env('SEEDING_TIME_LIMIT', 60, minimum=0)

# 下载分发任务间隔（秒）
DOWNLOAD_DISPATCHER_INTERVAL: int = get_int_env('DOWNLOAD_DISPATCHER_INTERVAL', 5, minimum=1)

# 首页自动刷新间隔（秒），默认 10 分钟
HOMEPAGE_REFRESH_INTERVAL: int = get_int_env('HOMEPAGE_REFRESH_INTERVAL', 600, minimum=1)

# Jable 热门榜单检查间隔（秒），默认 12 小时
JABLE_HOT_CHECK_INTERVAL: int = get_int_env('JABLE_HOT_CHECK_INTERVAL', 12 * 3600, minimum=1)

# 免费翻译接口批量大小。Google 免费接口单批过大时不稳定，默认尽量合并到 80 条。
TRANSLATION_BATCH_SIZE: int = get_int_env('TRANSLATION_BATCH_SIZE', 80, minimum=1)

# 是否开启 Jable 热门自动下载，默认 False，用户需手动开启
JABLE_HOT_AUTO_DOWNLOAD: bool = os.getenv('JABLE_HOT_AUTO_DOWNLOAD', 'false').lower() == 'true'

# 基础数据目录，绝对定位至项目根目录下的 data 文件夹，决不随 CWD 改变
PROJECT_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_REAL_DATA_DIR = os.path.join(PROJECT_ROOT_DIR, 'data')
env_data_dir = os.getenv('DATA_DIR')
if env_data_dir and os.path.isabs(env_data_dir) and os.path.exists(env_data_dir):
    DATA_DIR: str = env_data_dir
else:
    DATA_DIR: str = DEFAULT_REAL_DATA_DIR

# 封面缓存目录
COVER_CACHE_DIR: str = os.path.join(DATA_DIR, 'cover_cache')

# Disk cache capacity. Runtime database configuration can override this value.
CACHE_SIZE_LIMIT_GB: float = get_float_env('CACHE_SIZE_LIMIT_GB', 10.0, minimum=0.1, maximum=1000.0)

# Cache policy. List/detail freshness is separate from the disk capacity limit.
INFORMATION_FLOW_PRELOAD_PAGES: int = min(
    get_int_env('BUS_TERMINAL_INFORMATION_FLOW_PRELOAD_PAGES', 3, minimum=1), 30
)
# 首页/字幕/高清/发现 feed 的周期性预热（只写本地缓存，不触发下载/Jellyfin 写入等副作用）
FEED_PRELOAD_REFRESH_ENABLED: bool = os.getenv(
    'FEED_PRELOAD_REFRESH_ENABLED', 'true',
).strip().lower() not in ('0', 'false', 'no', 'off')
FEED_PRELOAD_REFRESH_SECONDS: int = min(
    get_int_env('FEED_PRELOAD_REFRESH_SECONDS', 1800, minimum=60), 24 * 3600
)
# 翻译进度快照间隔（秒）。全量去重统计要扫 1.4M 行、约 20~30 秒，只能后台算好给页面读
TRANSLATION_PROGRESS_SNAPSHOT_SECONDS: int = min(
    get_int_env('TRANSLATION_PROGRESS_SNAPSHOT_SECONDS', 1800, minimum=60), 24 * 3600
)
MOVIE_DETAIL_CACHE_TTL_SECONDS: int = min(
    get_int_env('BUS_TERMINAL_MOVIE_DETAIL_CACHE_TTL', 6 * 3600, minimum=60),
    30 * 24 * 3600,
)
IMAGE_FAILURE_TTL_SECONDS: int = min(
    get_int_env('BUS_TERMINAL_IMAGE_FAILURE_TTL', 30 * 60, minimum=60),
    24 * 3600,
)

# ========== 数据库配置 ==========
# 数据库连接URL，支持PostgreSQL和SQLite
# 如果是 SQLite，默认存放在 DATA_DIR 下
DEFAULT_DB_PATH = os.path.join(DATA_DIR, 'movies.db')
DEFAULT_DATABASE_URL = f"sqlite:///{DEFAULT_DB_PATH.replace(os.sep, '/')}"
DATABASE_URL: str = os.getenv('DATABASE_URL') or DEFAULT_DATABASE_URL
DATABASE_POOL_MAX_CONNECTIONS: int = min(
    get_int_env(
        'BUS_TERMINAL_DATABASE_POOL_MAX_CONNECTIONS',
        10,
        minimum=5,
    ),
    50,
)

# ========== 爬虫配置 ==========
# 额外的 JavBus 备用域名（逗号、分号或空格分隔）
EXTRA_DOMAINS_STR: str = os.getenv('EXTRA_DOMAINS', '')

def get_extra_domains() -> list[str]:
    """解析并返回额外的域名列表"""
    if not EXTRA_DOMAINS_STR:
        return []
    # 使用正则根据逗号、分号或空格分割，并去空
    domains = re.split(r'[;,\s]+', EXTRA_DOMAINS_STR)
    cleaned = []
    for domain in domains:
        value = domain.strip().rstrip('/')
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if value not in cleaned:
            cleaned.append(value)
    return cleaned

# ========== qBittorrent 配置 (环境变量) ==========
QB_HOST: str = os.getenv('QB_HOST')
QB_PORT: str = os.getenv('QB_PORT')
QB_USERNAME: str = os.getenv('QB_USERNAME', 'admin')
QB_PASSWORD: str = os.getenv('QB_PASSWORD', '')
QB_VERIFY_SSL: bool = os.getenv('QB_VERIFY_SSL', 'true').lower() == 'true'

# ========== amane（本地媒体库执行层）配置 ==========
# BusTerminal 不碰文件；扫描/整理/刮削/清理全部通过 amane 的 HTTP API 触发。
# 运行时可用 DB config 键 amane_base_url / amane_token 覆盖（见 amane_service）。
# 默认留空 = 未配置。部署者在引导页 /setup 或后台设置里填自己的 amane 地址；
# 留空的后果是 amane 相关功能报「未配置」，不会静默连到某个特定主机。
AMANE_BASE_URL: str = os.getenv('AMANE_BASE_URL', '').strip().rstrip('/')
AMANE_TOKEN: str = os.getenv('AMANE_TOKEN', '').strip()
AMANE_TIMEOUT_SECONDS: float = get_float_env('AMANE_TIMEOUT_SECONDS', 10.0, minimum=1.0)
# 提交任务后同步等待的上限（秒），超时返回最后一次任务快照
AMANE_TASK_TIMEOUT_SECONDS: float = min(
    get_float_env('AMANE_TASK_TIMEOUT_SECONDS', 300.0, minimum=5.0), 3600.0
)

# 入库自愈巡检：定时找出「下载已完成、却迟迟没被整理入库」的影片，自动补号 + 刮削 + 整理
# （绕开 amane refresh 失败，见 docs/AMANE_INGEST_HEAL.md）。
# **默认关闭** —— 这是会自动写 amane 的操作，开关交给运维；每轮限量、串行处理，避免打爆刮削源。
AMANE_HEAL_ENABLED: bool = os.getenv(
    'AMANE_HEAL_ENABLED', 'false',
).strip().lower() not in ('0', 'false', 'no', 'off')
# 巡检间隔（秒）：5 分钟 ~ 24 小时
AMANE_HEAL_INTERVAL_SECONDS: int = min(
    get_int_env('AMANE_HEAL_INTERVAL_SECONDS', 3600, minimum=300), 24 * 3600
)
# 判定「滞留」的阈值（秒）：入库状态多久没推进才算卡住，10 分钟 ~ 7 天
AMANE_HEAL_STUCK_AFTER_SECONDS: int = min(
    get_int_env('AMANE_HEAL_STUCK_AFTER_SECONDS', 7200, minimum=600), 7 * 24 * 3600
)
# 每轮最多处理几部：1 ~ 50
AMANE_HEAL_BATCH_LIMIT: int = min(get_int_env('AMANE_HEAL_BATCH_LIMIT', 10, minimum=1), 50)

# ========== 服务与目录配置 ==========
DELIVERY_FOLDER: str = os.getenv('DELIVERY_FOLDER', '/downloads/staging')

DEFAULT_CORS_ORIGINS = [
    'http://localhost:3000',
    'http://localhost:5173',
]

CORS_ORIGINS_STR: str = os.getenv('CORS_ORIGINS', '')

def get_cors_origins() -> list[str]:
    """解析允许的 CORS 来源，未配置时使用内网开发默认值。"""
    if not CORS_ORIGINS_STR.strip():
        return DEFAULT_CORS_ORIGINS

    origins = re.split(r'[;,\s]+', CORS_ORIGINS_STR)
    cleaned = []
    for origin in origins:
        origin = origin.strip().rstrip('/')
        if origin == "*":
            if origin not in cleaned:
                cleaned.append(origin)
            continue
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        if parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            continue
        normalized = f"{parsed.scheme}://{parsed.netloc}"
        if normalized and normalized not in cleaned:
            cleaned.append(normalized)
    return cleaned or DEFAULT_CORS_ORIGINS


def get_cors_allow_credentials() -> bool:
    """Only allow credentials when CORS origins are an explicit whitelist."""
    return "*" not in get_cors_origins()


# ========== 翻译与代理配置 ==========
# 默认系统代理 (如 http://127.0.0.1:7890)
# —— 只作用于**外网抓取**（javbus / jable / discovery / 封面下载）。
# 生产部署在 NAS 上时留空，出网由路由器负责。
SYSTEM_PROXY: str = os.getenv('SYSTEM_PROXY', '')

# 内网服务（amane / Jellyfin / qB）是否跟随 http_proxy / https_proxy 环境变量。
# 默认 False：IDE/工具链注入的代理会把内网请求劫持到随机端口，表现为读超时或
# amane 返回 404 而不是 401。开发机确实需要时再打开（也可在库里配
# internal_proxy_enabled，优先级高于此处）。
INTERNAL_PROXY_ENABLED: bool = os.getenv('BUS_TERMINAL_INTERNAL_PROXY_ENABLED', 'false').strip().lower() == 'true'

# 百度翻译 API 配置
BAIDU_APP_ID: str = os.getenv('BAIDU_APP_ID', '')
BAIDU_SECRET: str = os.getenv('BAIDU_SECRET', '')

# 默认翻译提供商 (google_free, baidu_dev, ollama_local)
# 如果提供了百度 ID 和密钥，建议设为 baidu_dev
TRANSLATION_PROVIDER: str = os.getenv('TRANSLATION_PROVIDER', 'google_free')

# 本地大模型翻译（内网 Ollama：原生 /api/chat + think=false）
OLLAMA_BASE_URL: str = os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434')
OLLAMA_MODEL: str = os.getenv('OLLAMA_MODEL', 'qwen3.8:27b')
# 单次调用超时（秒）。Ollama 并发为 1，排队时更久，别设太短
OLLAMA_TIMEOUT: int = min(get_int_env('OLLAMA_TIMEOUT', 300, minimum=10), 900)
# 每次请求翻译多少条标题：实测 12 条约 27 秒，再大单次等待过长
OLLAMA_BATCH_SIZE: int = min(get_int_env('OLLAMA_BATCH_SIZE', 12, minimum=1), 30)

# 标题翻译的质量保障（2026-11-11）：
# 实测两个模型都守不住「艺人姓名保留日文假名」这条口径（qwen3.8:27b 把 `水野アコ` 译成
# `水野绫子`；hy-mt2 把 `水野優香` 译成 `水野优香`），因此改为代码保证：翻译前把命中演员表的
# 假名串换成占位符、译后原样还原。
#   TRANSLATION_GUARD_ENABLED      关掉占位保护（默认开）
#   TRANSLATION_STRICT_QUALITY_GATE 关掉则不合格译文仍返回给调用方，仅打日志（默认开=严格）
# 严格模式下坏译文不入 translations 表：一旦落库后续会以它为准传播，清理成本远高于当场拒绝。
TRANSLATION_GUARD_ENABLED: bool = os.getenv(
    'TRANSLATION_GUARD_ENABLED', 'true',
).strip().lower() not in ('0', 'false', 'no', 'off')
TRANSLATION_STRICT_QUALITY_GATE: bool = os.getenv(
    'TRANSLATION_STRICT_QUALITY_GATE', 'true',
).strip().lower() not in ('0', 'false', 'no', 'off')

# 演员头像定时补全（2026-11-11）：
# 实测生产缺口 62,175 / 102,314（61% 演员没有头像），而页面头像丰富度直接取决于它。
# 幂等：sync() 只取 get_actresses_missing_avatar()（avatar 为空的行），
# 且 update_actress_avatars() 只覆盖原本为空的行，所以已补过的天然被跳过，可放心长期跑。
# 默认关闭：会持续出网拉 gfriends 索引并写库，需要显式开启。
GFRIENDS_AVATAR_SYNC_ENABLED: bool = os.getenv(
    'GFRIENDS_AVATAR_SYNC_ENABLED', 'false',
).strip().lower() in ('1', 'true', 'yes', 'on')
GFRIENDS_AVATAR_SYNC_INTERVAL_SECONDS: int = min(
    get_int_env('GFRIENDS_AVATAR_SYNC_INTERVAL_SECONDS', 1800, minimum=300), 86400
)
GFRIENDS_AVATAR_SYNC_BATCH: int = min(
    get_int_env('GFRIENDS_AVATAR_SYNC_BATCH', 300, minimum=10), 5000
)
# gfriends 索引对候选的命中率实测只有 2~15%，所以「扫描条数」要放大到「期望写入量」的
# 若干倍，否则一轮下来几乎补不到东西。
GFRIENDS_AVATAR_SYNC_SCAN_FACTOR: int = min(
    get_int_env('GFRIENDS_AVATAR_SYNC_SCAN_FACTOR', 12, minimum=1), 50
)

# 历史标题翻译回填：后台排队慢慢翻完积压（仅本地大模型启用时生效）
TRANSLATION_BACKFILL_ENABLED: bool = os.getenv(
    'TRANSLATION_BACKFILL_ENABLED', 'true',
).strip().lower() not in ('0', 'false', 'no', 'off')
# 每轮往队列里放几个片子。注意：真正发请求的是队列工作线程，永远"一个片子一个请求"，
# 这里只是喂料批量，越大数据库查询越少、喂得越快
TRANSLATION_BACKFILL_BATCH: int = min(get_int_env('TRANSLATION_BACKFILL_BATCH', 8, minimum=1), 200)
# 两轮之间的间隔（秒），0 = 连续喂料到队列上限为止
TRANSLATION_BACKFILL_INTERVAL_SECONDS: int = min(
    get_int_env('TRANSLATION_BACKFILL_INTERVAL_SECONDS', 0, minimum=0), 24 * 3600
)

# ========== 辅助服务健康依赖（心跳 / 备份新鲜度） ==========
# bus_backup / bus_backup_files / pt_ignore_guard 三个无状态辅助容器每轮跑完把
# JSON 心跳写到 DATA_DIR/heartbeat/<service>.json。bus_terminal 只挂了 /app/data
# （看不到 storage 上的备份产物），所以产物新鲜度也由心跳文件带过来。
#
# 心跳超过下面这些秒数没更新，即判定该服务停摆：
#   备份类 36h —— 每日任务，容忍错过一次 + 一点余量
#   每周档 8d  —— jf-full 只在周日跑
#   守卫 600s  —— 60s 一轮，10 分钟没动静基本可以确定进程没了
HEALTH_DEPS_CHECK_SECONDS: int = min(
    get_int_env('BUS_TERMINAL_HEALTH_DEPS_CHECK_SECONDS', 300, minimum=60), 24 * 3600
)
HEALTH_ALERT_COOLDOWN_SECONDS: int = min(
    get_int_env('BUS_TERMINAL_HEALTH_ALERT_COOLDOWN_SECONDS', 6 * 3600, minimum=60),
    30 * 24 * 3600,
)
HEALTH_BACKUP_MAX_AGE_SECONDS: int = min(
    get_int_env('BUS_TERMINAL_HEALTH_BACKUP_MAX_AGE_SECONDS', 36 * 3600, minimum=3600),
    30 * 24 * 3600,
)
HEALTH_WEEKLY_BACKUP_MAX_AGE_SECONDS: int = min(
    get_int_env('BUS_TERMINAL_HEALTH_WEEKLY_BACKUP_MAX_AGE_SECONDS', 8 * 24 * 3600, minimum=24 * 3600),
    60 * 24 * 3600,
)
HEALTH_GUARD_MAX_AGE_SECONDS: int = min(
    get_int_env('BUS_TERMINAL_HEALTH_GUARD_MAX_AGE_SECONDS', 600, minimum=120), 24 * 3600
)

# Image hosts occasionally have broken certificates. Keep TLS verification on by
# default and only allow insecure fallback when the deployment explicitly opts in.
SCRAPER_ALLOW_INSECURE_IMAGE_FETCH: bool = os.getenv(
    'SCRAPER_ALLOW_INSECURE_IMAGE_FETCH',
    'false',
).lower() == 'true'

