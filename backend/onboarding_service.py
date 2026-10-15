"""首次部署引导（onboarding）

部署后第一次打开前端时，引导用户把「接口与授权 / 下载习惯 / 屏蔽关键词 / 缓存策略 /
是否接入现有数据库 / Jellyfin 自动刮削」这些基础配置一次填完，避免上线后靠翻文档补配置。

三块职责：
1. **屏蔽词闸门** —— 全链路（列表展示、磁力、下载入队）统一按 DB 配置的关键词拦截。
2. **配置落库** —— 引导表单一次性写入 DB config（密钥类走已有的加密通道）。
3. **体检** —— 数据库只读体检 + 各外部依赖连通性测试。

屏蔽词匹配口径（用户拍板）：对 标题 / 番号 / 演员名 做「去分隔符 + 忽略大小写」的
子串包含匹配。填 `vr` 能同时挡住 `VR`, `vr`, `ＶＲ`, `VR-`, `HD VR` 等写法，且不会
漏掉日文标题里夹着的标记。
"""
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

# 引导完成标记与版本号。标记写进 DB config，所以只要库在，引导状态就在。
ONBOARDING_COMPLETED_KEY = "onboarding_completed_at"
ONBOARDING_VERSION_KEY = "onboarding_version"
ONBOARDING_VERSION = 1

# 屏蔽词在 config 表里的键
BLOCK_KEYWORDS_KEY = "block_keywords"

# 引导表单写这些键时要按「敏感」处理：空值代表保持原样，避免擦掉已有密钥。
# 注意这套名单是「引导页自己的」——db.set_config 的加密通道以
# database.SENSITIVE_CONFIG_KEYS 为准，两者不完全一致（比如 qb_password 走嵌套 qb_config）。
SENSITIVE_ONBOARDING_KEYS = {
    "jellyfin_api_key",
    "baidu_secret",
    "email_pass",
    "system_proxy",
    "amane_token",
}

# 这些键引导页不回填明文，只回填 *_configured 布尔位
MASKED_ONBOARDING_KEYS = SENSITIVE_ONBOARDING_KEYS

# 关键词归一化时剥掉的分隔符与全角字符，让 `VR-` / `ＶＲ` / `VR 视角` 都归到 `vr`
_KEYWORD_STRIP_RE = re.compile(r"[\s\-_./\\|·・:：,，、+＋()（）\[\]【】'\"“”]+")
_KEYWORD_NORMALIZE_RE = re.compile(r"[！-～]")  # 全角 ASCII -> 半角

MAX_BLOCK_KEYWORDS = 100
MAX_BLOCK_KEYWORD_LENGTH = 64


def normalize_block_keyword(raw: Any) -> str:
    """把用户填的一个关键词归一化：小写、去分隔符、全角转半角。

    空结果表示这个关键词无效（比如只填了符号），调用方应丢弃它。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    text = _KEYWORD_NORMALIZE_RE.sub(
        lambda m: chr(ord(m.group(0)) - 0xFEE0), text
    )
    text = text.lower()
    text = _KEYWORD_STRIP_RE.sub("", text)
    return text[:MAX_BLOCK_KEYWORD_LENGTH]


def parse_block_keywords(raw: Any) -> List[str]:
    """解析用户在引导页填的屏蔽词（换行/逗号/顿号分隔），去重后返回。

    存进 DB 的是**归一化后**的词，所以运行期匹配不再做剥离，只需一次子串判断。
    """
    if isinstance(raw, (list, tuple, set)):
        candidates: Iterable[Any] = raw
    else:
        candidates = re.split(r"[\r\n,，;；、|]+", str(raw or ""))

    result: List[str] = []
    for candidate in candidates:
        normalized = normalize_block_keyword(candidate)
        if not normalized:
            continue
        if normalized not in result:
            result.append(normalized)
        if len(result) >= MAX_BLOCK_KEYWORDS:
            break
    return result


def match_block_keyword(
    texts: Iterable[Any],
    keywords: Iterable[str],
) -> Optional[str]:
    """返回第一个命中的屏蔽词，没命中返回 None。

    匹配前对被检文本做同样的归一化，保证「填 vr 挡 ＶＲ-」这类写法成立。
    """
    normalized_keywords = [kw for kw in keywords if kw]
    if not normalized_keywords:
        return None
    for text in texts:
        normalized_text = normalize_block_keyword(text)
        if not normalized_text:
            continue
        for keyword in normalized_keywords:
            if keyword in normalized_text:
                return keyword
    return None


class OnboardingService:
    def __init__(
        self,
        *,
        db: Any,
        config: Any,
        logger: Any,
        is_valid_http_url: Callable[..., bool],
        normalize_qb_root_path: Callable[[Any], Any],
        probe_qb: Callable[[], Dict[str, Any]],
        probe_jellyfin: Callable[[], Dict[str, Any]],
        probe_amane: Callable[[], Dict[str, Any]],
        probe_translation: Callable[[Dict[str, Any]], Dict[str, Any]],
        config_payload_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        save_qb_config: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ):
        self.db = db
        self.config = config
        self.logger = logger
        self.is_valid_http_url = is_valid_http_url
        self.normalize_qb_root_path = normalize_qb_root_path
        self.probe_qb = probe_qb
        self.probe_jellyfin = probe_jellyfin
        self.probe_amane = probe_amane
        self.probe_translation = probe_translation
        self.config_payload_provider = config_payload_provider
        self.save_qb_config = save_qb_config

    # ------------------------------------------------------------------
    # 屏蔽词闸门
    # ------------------------------------------------------------------
    def get_block_keywords(self) -> List[str]:
        """读当前生效的屏蔽词。运行期每次下载判定都会调，所以不回填默认值。"""
        return parse_block_keywords(self.db.get_config(BLOCK_KEYWORDS_KEY))

    def is_blocked(self, *texts: Any) -> Dict[str, Any]:
        """下载侧闸门：命中屏蔽词就拒绝，并给出命中的词方便前端提示与排障。"""
        keywords = self.get_block_keywords()
        if not keywords:
            return {"blocked": False, "keyword": None}
        hit = match_block_keyword(texts, keywords)
        if not hit:
            return {"blocked": False, "keyword": None}
        return {"blocked": True, "keyword": hit}

    def filter_blocked_movies(self, movies: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
        """列表侧闸门：从 feed 里摘掉命中屏蔽词的条目，返回 (保留项, 被挡数)。"""
        keywords = self.get_block_keywords()
        if not keywords or not movies:
            return movies, 0

        kept: List[Dict[str, Any]] = []
        blocked_count = 0
        for movie in movies:
            if not isinstance(movie, dict):
                kept.append(movie)
                continue
            actresses = movie.get("actresses") or movie.get("actors") or []
            actress_names = []
            if isinstance(actresses, list):
                for actress in actresses:
                    if isinstance(actress, dict):
                        actress_names.append(actress.get("name") or actress.get("actress_name"))
                    elif isinstance(actress, str):
                        actress_names.append(actress)
            hit = match_block_keyword(
                [movie.get("title"), movie.get("original_title"), movie.get("id"), movie.get("movie_id")]
                + actress_names,
                keywords,
            )
            if hit:
                blocked_count += 1
                continue
            kept.append(movie)
        return kept, blocked_count

    def preview_blocked_movies(self, limit: int = 20) -> List[Dict[str, Any]]:
        """从本地 movies 表抽样，找出当前屏蔽词会挡掉的条目。

        只读。给引导页做「保存前先看效果」用 —— 屏蔽词填太宽（比如误填一个常见
        单词）会静默挡掉大量正常片子，必须让用户看得见。
        """
        keywords = self.get_block_keywords()
        if not keywords:
            return []

        conn = self.db.get_connection()
        try:
            cursor = conn.cursor()
            # 多取一些再筛，因为命中可能很稀疏；LIMIT 放大到 limit 的 40 倍封顶。
            scan_limit = max(limit * 40, 200)
            is_postgres = bool(getattr(self.db, "is_postgres", False))
            # ⚠️ 不要写 COALESCE(release_date, 0) / NULLS LAST —— 那会让 145 万行退化成全表排序，
            # 实测试算从 ~1s 变成 15s。movies.release_date 上有索引，直接按原列倒序即可，
            # 空值排在哪对「找几个命中样本」毫无影响。
            sql = (
                f"SELECT id, title, original_title FROM movies "
                f"ORDER BY release_date DESC LIMIT {scan_limit}"
            )
            cursor.execute(sql)
            rows = cursor.fetchall() or []
        except Exception as exc:
            self.logger.warning("Block keyword preview query failed: %s", type(exc).__name__)
            return []
        finally:
            try:
                self.db._release_connection(conn)
            except Exception:
                pass

        matched: List[Dict[str, Any]] = []
        for row in rows:
            data = dict(row) if not isinstance(row, dict) else row
            hit = match_block_keyword(
                [data.get("title"), data.get("original_title"), data.get("id")], keywords
            )
            if hit:
                matched.append({
                    "id": data.get("id"),
                    "title": data.get("title"),
                    "original_title": data.get("original_title"),
                    "keyword": hit,
                })
            if len(matched) >= limit:
                break
        return matched

    # ------------------------------------------------------------------
    # 引导状态
    # ------------------------------------------------------------------
    def is_completed(self) -> bool:
        return bool(self.db.get_config(ONBOARDING_COMPLETED_KEY))

    def status(self) -> Dict[str, Any]:
        return {
            "completed": self.is_completed(),
            "completed_at": self.db.get_config(ONBOARDING_COMPLETED_KEY),
            "version": self.db.get_config(ONBOARDING_VERSION_KEY),
            "block_keywords": self.get_block_keywords(),
        }

    def current_settings(self) -> Dict[str, Any]:
        """把引导页要显示的字段读出来。敏感项只回 *_configured，不回明文。

        qBittorrent 的配置在 db 里是**一个嵌套 dict**（`qb_config`，字段 host/port/
        username/password），所以这里委托 qb_service.config_payload() 读，而不是
        自己按扁平键去猜 —— 扁平键根本没人读。
        """
        keys = [
            # 接口与授权（qB 走 qb_config，见下）
            "jellyfin_host", "jellyfin_api_key", "jellyfin_user_id",
            "amane_base_url", "amane_token", "amane_library_id", "amane_scan_path",
            "translation_provider", "baidu_app_id", "baidu_secret",
            "ollama_base_url", "ollama_model", "system_proxy",
            # 下载习惯
            "disk_threshold_gb", "seeding_time_limit_min", "delivery_folder",
            "auto_download_daily_limit", "preferred_resolution", "jable_hot_auto_download",
            "auto_download_enabled", "auto_subscribe_enabled", "automation_mode",
            # 屏蔽与缓存
            BLOCK_KEYWORDS_KEY, "cache_size_limit_gb",
            "movie_detail_cache_ttl_hours", "discovery_cache_ttl_hours",
            # Jellyfin 自动刮削补齐（= amane 入库闭环开关，方案 C 下真实消费点）
            "jellyfin_library_name", "amane_ingest_enabled", "amane_organize_enabled",
            "amane_ingest_verify_visible", "amane_dedupe_enabled",
            # 数据库
            "onboarding_database_url",
        ]
        payload: Dict[str, Any] = {}
        # 一次性读全表：逐键 get_config 在 sqlite 下每次 1.5 秒，40 个键就是 5 秒，
        # 而这个接口在每个页面加载时都会被启动门调用一次。
        try:
            all_config = self.db.get_all_config()
        except Exception:
            all_config = {}
        for key in keys:
            value = all_config.get(key)
            if key in MASKED_ONBOARDING_KEYS:
                # 密钥类**绝不能**回填明文，只回一个 configured 布尔位
                payload[key] = ""
                payload[f"{key}_configured"] = bool(value)
                continue
            payload[key] = value
        payload[BLOCK_KEYWORDS_KEY] = self.get_block_keywords()
        payload["onboarding_completed"] = self.is_completed()
        try:
            payload["qb_config"] = self.config_payload_provider() if self.config_payload_provider else {}
        except Exception:
            payload["qb_config"] = {}
        return payload

    # ------------------------------------------------------------------
    # 数据库体检（只读）
    # ------------------------------------------------------------------
    def database_check(self, expected_url: Optional[str] = None) -> Dict[str, Any]:
        """只读体检当前库，并对比引导页期望接入的库。

        `DATABASE_URL` 是进程启动时读的环境变量，**运行期无法切库**，所以这里如实
        告诉用户「当前连的是哪个库、和期望是否一致、不一致要怎么改」，不假装能切换。
        """
        active_url = str(self.config.DATABASE_URL or "")
        scheme = urlparse(active_url).scheme
        dialect = "postgresql" if scheme.startswith("postgres") else "sqlite"
        masked_url = _mask_database_url(active_url)

        expected_masked = _mask_database_url(expected_url) if expected_url else None
        if expected_url and _database_url_identity(active_url) != _database_url_identity(expected_url):
            matches = False
            hint = (
                "期望接入的数据库与当前生效的连接不一致。DATABASE_URL 只在进程启动时读取，"
                "请修改环境变量 DATABASE_URL 后重启容器再回到本引导页确认。"
            )
        elif expected_url:
            matches = True
            hint = "与期望接入的数据库一致。"
        else:
            matches = True
            hint = None

        counts: Dict[str, int] = {}
        errors: List[str] = []
        for table in ("movies", "actresses", "translations", "local_files"):
            try:
                counts[table] = int(self.db.count_table_rows(table) or 0)
            except Exception as exc:
                errors.append(f"{table}: {type(exc).__name__}")

        return {
            "reachable": True,
            "dialect": dialect,
            "active_url_masked": masked_url,
            "matches_expected": matches,
            "hint": hint,
            "counts": counts,
            "errors": errors,
            "switch_requires_restart": True,
        }

    # ------------------------------------------------------------------
    # 连通性测试
    # ------------------------------------------------------------------
    def test_target(self, target: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """按 target 分派到对应探针。探针只做只读调用，不改任何外部状态。"""
        if target == "qb":
            return self.probe_qb()
        if target == "jellyfin":
            return self.probe_jellyfin()
        if target == "amane":
            return self.probe_amane()
        if target == "translation":
            return self.probe_translation(payload or {})
        return {"ok": False, "message": "unsupported_target"}

    # ------------------------------------------------------------------
    # 落库
    # ------------------------------------------------------------------
    def validate(self, payload: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if not isinstance(payload, dict):
            return ["invalid_payload"]

        if "block_keywords" in payload:
            if isinstance(payload.get("block_keywords"), str) and len(payload.get("block_keywords")) > 20000:
                errors.append("block_keywords_too_long")

        for key in ("jellyfin_host", "amane_base_url", "system_proxy",
                    "ollama_base_url", "baidu_app_id"):
            if key not in payload:
                continue
            value = str(payload.get(key) or "").strip()
            if not value:
                continue
            allow_credentials = key == "system_proxy"
            allow_path = key in ("jellyfin_host", "amane_base_url")
            if not self.is_valid_http_url(
                value, allow_credentials=allow_credentials, allow_path=allow_path
            ):
                errors.append(f"{key}_invalid_url")

        if str(payload.get("delivery_folder") or "").strip():
            if not self.normalize_qb_root_path(payload.get("delivery_folder")):
                errors.append("delivery_folder_invalid_path")

        if "translation_provider" in payload:
            provider = str(payload.get("translation_provider") or "").strip()
            if provider and provider not in {"google_free", "baidu_dev", "ollama_local"}:
                errors.append("translation_provider_unsupported")

        numeric_ranges = {
            "disk_threshold_gb": (0, None),
            "seeding_time_limit_min": (0, None),
            "cache_size_limit_gb": (0.1, 1000),
            "auto_download_daily_limit": (-1, None),
            "movie_detail_cache_ttl_hours": (1, 720),
            "discovery_cache_ttl_hours": (1, 720),
        }
        for key, (minimum, maximum) in numeric_ranges.items():
            if key not in payload or payload.get(key) in (None, ""):
                continue
            try:
                number = float(payload.get(key))
            except (TypeError, ValueError):
                errors.append(f"{key}_invalid_number")
                continue
            if minimum is not None and number < minimum:
                errors.append(f"{key}_below_min")
            if maximum is not None and number > maximum:
                errors.append(f"{key}_above_max")

        if str(payload.get("automation_mode") or "").strip():
            if payload.get("automation_mode") not in {"off", "suggest", "auto_approve_low_risk"}:
                errors.append("automation_mode_unsupported")

        return errors

    def apply(self, payload: Dict[str, Any], *, completed: bool = True) -> Dict[str, Any]:
        """把引导表单写进 DB config。

        - 屏蔽词按归一化形式存储（运行期只做子串匹配）。
        - 密钥类空值表示「保持原样」，避免前端拿不到明文就把已有密钥擦掉。
        - 缓存 TTL 以小时入库，运行期由 cache_ttl_hours_to_seconds 换算。
        """
        if not isinstance(payload, dict):
            return {"success": False, "errors": ["invalid_payload"]}

        errors = self.validate(payload)
        if errors:
            return {"success": False, "errors": errors}

        written: List[str] = []
        skipped: List[str] = []

        # qB 的连接参数是嵌套 dict，必须走 qb_service 的归一化 + 密码保留逻辑，
        # 不能当成扁平键直接 set_config（那样写下去根本没人读）。
        qb_section = payload.get("qb_config")
        if isinstance(qb_section, dict) and self.save_qb_config:
            try:
                self.save_qb_config(qb_section)
                written.append("qb_config")
            except ValueError as exc:
                return {"success": False, "errors": [str(exc)]}

        for key, value in payload.items():
            if key in ("onboarding_completed", "completed", "qb_config"):
                continue
            if key not in _ALLOWED_ONBOARDING_KEYS:
                continue

            if key == BLOCK_KEYWORDS_KEY:
                keywords = parse_block_keywords(value)
                self.db.set_config(BLOCK_KEYWORDS_KEY, ",".join(keywords))
                written.append(BLOCK_KEYWORDS_KEY)
                continue

            if key in SENSITIVE_ONBOARDING_KEYS and (value is None or str(value).strip() == ""):
                skipped.append(key)
                continue

            self.db.set_config(key, value)
            written.append(key)

        if completed:
            self.db.set_config(ONBOARDING_COMPLETED_KEY, int(time.time()))
            self.db.set_config(ONBOARDING_VERSION_KEY, ONBOARDING_VERSION)
            written.extend([ONBOARDING_COMPLETED_KEY, ONBOARDING_VERSION_KEY])

        # 缓存 TTL 落的是小时，真实消费点读的是秒，这里补一个派生键方便单点读取
        ttl_pairs = (
            ("movie_detail_cache_ttl_hours", "movie_detail_cache_ttl_seconds"),
            ("discovery_cache_ttl_hours", "discovery_cache_ttl_seconds"),
        )
        for hours_key, seconds_key in ttl_pairs:
            if hours_key not in payload:
                continue
            seconds = cache_ttl_hours_to_seconds(payload.get(hours_key), fallback=self._ttl_fallback(hours_key))
            self.db.set_config(seconds_key, seconds)

        self.logger.info(
            "Onboarding applied: written=%s skipped=%s completed=%s",
            len(written), len(skipped), completed,
        )
        return {"success": True, "written": written, "skipped": skipped, "settings": self.current_settings()}

    def _ttl_fallback(self, hours_key: str) -> int:
        if hours_key == "movie_detail_cache_ttl_hours":
            return int(self.config.MOVIE_DETAIL_CACHE_TTL_SECONDS)
        return int(self.config.get_int_env("BUS_TERMINAL_DISCOVERY_CACHE_TTL", 6 * 3600, minimum=1))

    def reset(self) -> Dict[str, Any]:
        """重新走引导：清完成标记。已写入的业务配置不动，只解除拦截。"""
        self.db.set_config(ONBOARDING_COMPLETED_KEY, 0)
        return {"success": True, "status": self.status()}


def cache_ttl_hours_to_seconds(raw: Any, *, fallback: int) -> int:
    """把引导页填的小时数换成秒。非法值退回 fallback，不抛异常。"""
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return int(fallback)
    if hours <= 0:
        return int(fallback)
    return int(hours * 3600)


def _database_url_identity(url: Any) -> str:
    """抽出连接的身份部分用于比较：库名 + 主机 + 端口。

    刻意不比较用户名/密码 —— 同一套库换个账号密码不该被判成「不是那个库」。
    SQLite 则只认文件路径。
    """
    text = str(url or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme.startswith("postgres"):
        return "pg://{}:{}/{}".format(
            (parsed.hostname or "").lower(),
            parsed.port or 5432,
            (parsed.path or "").strip("/"),
        )
    return text


def _mask_database_url(url: Any) -> str:
    """给用户看的脱敏连接串：隐藏密码与用户名，绝不回显明文口令。"""
    text = str(url or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme.startswith("postgres"):
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        database = (parsed.path or "").strip("/")
        return f"postgresql://***@{host}{port}/{database}" if host else "postgresql://***"
    return text


# 引导表单允许写入的键白名单，防止前端传任意键污染 config 表。
# 键名必须与各 service 实际读取的键一致（qb 走嵌套 qb_config，不在此列）。
_ALLOWED_ONBOARDING_KEYS = {
    # 接口与授权
    "jellyfin_host", "jellyfin_api_key", "jellyfin_user_id",
    "amane_base_url", "amane_token", "amane_library_id", "amane_scan_path",
    "translation_provider", "baidu_app_id", "baidu_secret",
    "ollama_base_url", "ollama_model", "system_proxy",
    # 下载习惯
    "disk_threshold_gb", "seeding_time_limit_min", "delivery_folder",
    "auto_download_daily_limit", "preferred_resolution", "jable_hot_auto_download",
    "auto_download_enabled", "auto_subscribe_enabled", "automation_mode",
    # 屏蔽与缓存
    BLOCK_KEYWORDS_KEY, "cache_size_limit_gb",
    "movie_detail_cache_ttl_hours", "discovery_cache_ttl_hours",
    # Jellyfin 自动刮削补齐（amane 入库闭环）
    "jellyfin_library_name", "amane_ingest_enabled", "amane_organize_enabled",
    "amane_ingest_verify_visible", "amane_dedupe_enabled",
    # 数据库
    "onboarding_database_url",
}