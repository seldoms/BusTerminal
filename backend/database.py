# backend/database.py
"""Database abstraction supporting SQLite (fallback) and PostgreSQL.

The original implementation used SQLite exclusively with `?` placeholders and
`INSERT OR REPLACE` UPSERT syntax. This refactor introduces:

- Detection of the database type via `config.DATABASE_URL`.
- For PostgreSQL: a thread-safe connection pool (`psycopg2.pool.ThreadedConnectionPool`).
- Unified `get_connection` that returns a connection object appropriate for the backend.
- SQL statements rewritten to use `%s` placeholders for PostgreSQL.
- UPSERT statements converted to `ON CONFLICT ... DO UPDATE`.
- Schema definitions using PostgreSQL-compatible types while keeping SQLite
  definitions for backward compatibility.
- Proper cleanup of PostgreSQL connections (`putconn`).

All existing public methods retain their signatures; callers do not need to change.
"""

import os
import json
import uuid
import base64
import time
import logging
import re
import re
import hashlib
import threading
from datetime import date, datetime
from typing import Any, List, Dict, Optional, Tuple
from urllib.parse import urlparse
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

import sqlite3
from pathlib import Path
from common_utils import redact_log_message
# 2026-09-24 防二次污染：番号归一化的唯一事实来源（与清洗脚本共用同一实现）
from movie_code import normalize_movie_code
from cover_upgrade_service import should_replace_cover

logger = logging.getLogger(__name__)
ALLOW_UNSAFE_PATH_OUTPUT = "BUS_TERMINAL_SHOW_UNSAFE_PATHS"
ALLOW_SCHEMA_MIGRATION = "BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION"

# config 表读取的进程内缓存有效期（秒）。
# 配置几乎不变但读取极频繁；sqlite 下每次 get_config 要 1.5 秒（打开 3GB 库文件），
# 不缓存会让每个页面加载都卡数秒。set_config 会立即失效，不会读到旧值。
CONFIG_CACHE_TTL_SECONDS = 20.0

# 演员列表「内存去重」时，一次 SQL 拉取的放大倍数。
# 同名演员要合并成一条，所以要多拉一些行才能保证分页后仍有 limit 条。
# 实测 4 倍足够（拉 120 行去重后得 115~120 条），且翻到第 10 页仍稳定。
DEDUPE_SCAN_FACTOR = 4
SCHEMA_MIGRATIONS = (
    (1, "legacy_columns"),
    (2, "queue_constraints"),
    (3, "actress_name_backfill"),
    (4, "download_queue_retry_schedule"),
    (5, "external_task_queue"),
    (6, "actress_metadata_retry_audit"),
    (7, "lifecycle_state_dimensions"),
    (8, "cleanup_candidate_decision_audit"),
    (9, "download_queue_options"),
    (10, "local_files_amane_identity"),
    (11, "local_files_jellyfin_identity"),
)


CONFIG_SECRET_ENV = "BUS_TERMINAL_CONFIG_SECRET"
CONFIG_ENCRYPTION_PREFIX = "enc:v1:"
# 这份清单决定"哪些键走 AES 加密"，漏一个就等于那个键永远明文。
# amane_token 曾漏在这里（而 system_config_service 的粘贴清洗清单里有它），
# 结果是补了 CONFIG_SECRET 也照样明文入库 —— 见 KI-048。
SENSITIVE_CONFIG_KEYS = {
    "auth_secret",
    "baidu_secret",
    "jellyfin_api_key",
    "email_pass",
    "system_proxy",
    "qb_config",
    "qb_password",
    "amane_token",
}


def _get_config_secret_key() -> Optional[bytes]:
    secret = os.getenv(CONFIG_SECRET_ENV, "").strip()
    if not secret:
        return None
    return hashlib.sha256(secret.encode("utf-8")).digest()


def config_encryption_enabled() -> bool:
    """是否配置了库内敏感配置的加密密钥。"""
    return _get_config_secret_key() is not None


def _is_sensitive_config_key(key: str) -> bool:
    return str(key or "").strip().lower() in SENSITIVE_CONFIG_KEYS


def _is_encrypted_config_value(raw: Any) -> bool:
    """判断 config.value 这一列里存的**原始字符串**是不是密文。

    写入时统一经过 json.dumps，所以密文读出来是带引号的字符串 '"enc:v1:..."'。
    """
    if raw is None:
        return False
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = raw
    return isinstance(parsed, str) and parsed.startswith(CONFIG_ENCRYPTION_PREFIX)


def _encrypt_config_value(value: Any) -> Any:
    key = _get_config_secret_key()
    if not key:
        return value
    plaintext = json.dumps(value, ensure_ascii=False).encode("utf-8")
    nonce = get_random_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    token = base64.urlsafe_b64encode(nonce + tag + ciphertext).decode("ascii")
    return f"{CONFIG_ENCRYPTION_PREFIX}{token}"


def _decrypt_config_value(value: Any) -> Any:
    token = str(value or "")
    if not token.startswith(CONFIG_ENCRYPTION_PREFIX):
        return value
    key = _get_config_secret_key()
    if not key:
        return value
    try:
        payload = base64.urlsafe_b64decode(token[len(CONFIG_ENCRYPTION_PREFIX):].encode("ascii"))
        nonce, tag, ciphertext = payload[:12], payload[12:28], payload[28:]
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
        return plaintext.decode("utf-8")
    except Exception:
        logger.warning("Failed to decrypt sensitive config value for key=%s", display_value_for_log(token[:16]))
        return value

# 把已经是中文的文本（典型是中文演员名）送进翻译器时，上游不返回译文，而是回一句
# 中文元评论，实测有「该内容无法识别为有效影视标题。」「该内容无实际意义，无法进行翻译。」
# 这类文本不含假名、也不像原文，英文错误页那套检查挡不住，会被当成正常译文入库并
# 直接显示给用户。这里按实测句式拦截；教训是别用「抱歉/我无法/请提供」这类宽泛词——
# 「ごめんなさい」的合法译文就是「对不起」，宽泛匹配会大面积误杀正常标题。
_TRANSLATION_REFUSAL_PREFIXES = (
    "该内容", "该文本", "该标题", "该影片", "该视频", "该作品",
)
_TRANSLATION_REFUSAL_MARKERS = (
    "无法识别为有效", "无法识别为合法", "不是有效的标题", "不是有效的视频标题",
    "无实际意义", "没有实际意义", "请提供要翻译", "请提供需要翻译",
    "无法进行翻译", "无法为您翻译", "无法完成翻译",
    "作为ai语言模型", "作为一个人工智能", "不在我的职责范围",
)


# 模型偶尔把结束控制符和译文一起吐出来（实测「巨乳新人教师</|im_end|>」）。它不是译文的
# 一部分，读出来要剥掉——否则会原样显示到标题上。
# 用正则而不是逐个 token 精确匹配：实测模型吐出过畸形变体，精确匹配漏得干干净净
# （库里就躺着「052020_001 性感西園さくや<|imim_end|>」「…菜月RBC-158/2026-10-10<|im|_end|>」）。
_TRANSLATION_CONTROL_TOKEN_PATTERN = re.compile(r"</?\|[^<>]{0,32}\|>")


def strip_translation_control_tokens(value: Any) -> str:
    text = _TRANSLATION_CONTROL_TOKEN_PATTERN.sub("", str(value or ""))
    return text.strip()


def is_bad_translation_text(value: Any) -> bool:
    """Return True when a cached translation is an upstream error page or a
    refusal comment, not a title."""
    text = str(value or "").strip()
    if not text:
        return True

    lowered = text.lower()
    if lowered.startswith(("error 500", "500.", "500 ")):
        return True

    normalized = lowered.replace("’", "'")
    error_markers = (
        "that's an error",
        "there was an error",
        "that's all we know",
    )
    if any(marker in normalized for marker in error_markers):
        return True

    if "server error" in normalized and "please try again later" in normalized:
        return True

    if any(text.startswith(prefix) for prefix in _TRANSLATION_REFUSAL_PREFIXES):
        return True
    return any(marker in text for marker in _TRANSLATION_REFUSAL_MARKERS)


def sanitize_movie_title_record(data: Dict) -> Dict:
    """Replace polluted movie titles with original_title/id at read time."""
    if not isinstance(data, dict):
        return data
    if not is_bad_translation_text(data.get("title")):
        return data

    fallback = str(data.get("original_title") or data.get("id") or "").strip()
    if fallback:
        data = dict(data)
        data["title"] = fallback
    return data


def sqlite_path_from_url(database_url: str) -> str:
    path = urlparse(database_url).path
    if not path:
        return "movies.db"
    if path.startswith("//"):
        return path[1:]
    if re.match(r"^/[A-Za-z]:/", path):
        return path[1:]
    return path.lstrip("/")


LIFECYCLE_PATH_PAYLOAD_KEYS = {
    "path",
    "file_path",
    "save_path",
    "content_path",
    "root_path",
    "download_path",
    "completion_path",
    "symlink_path",
    "folder",
    "delivery_folder",
    "directory",
}


def display_path_for_log(path: str) -> str:
    text = str(path or "")
    if os.getenv(ALLOW_UNSAFE_PATH_OUTPUT, "").lower() in {"1", "true", "yes", "on"}:
        return text
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"<path len={len(text)} sha256={digest}>"


def display_value_for_log(value: str) -> str:
    text = str(value or "")
    if os.getenv(ALLOW_UNSAFE_PATH_OUTPUT, "").lower() in {"1", "true", "yes", "on"}:
        return text
    if not text:
        return "<empty>"
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"<redacted len={len(text)} sha256={digest}>"


def sanitize_lifecycle_payload(payload):
    if isinstance(payload, list):
        return [sanitize_lifecycle_payload(item) for item in payload]
    if isinstance(payload, str):
        return redact_log_message(payload)
    if not isinstance(payload, dict):
        return payload

    sanitized = {}
    for key, value in payload.items():
        key_text = str(key)
        key_lower = key_text.lower()
        if key_lower in {
            "api_key",
            "token",
            "secret",
            "password",
            "authorization",
            "jellyfin_api_key",
            "qb_password",
            "baidu_secret",
            "email_pass",
            "system_proxy",
        }:
            sanitized[key] = "<redacted>"
            continue
        if (
            key_lower in LIFECYCLE_PATH_PAYLOAD_KEYS
            or key_lower.endswith("_path")
            or key_lower.endswith("_folder")
            or key_lower.endswith("_directory")
        ):
            continue
        sanitized[key] = sanitize_lifecycle_payload(value)
    return sanitized


def parse_bool_value(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    return default


def parse_int_value(value, default=0, minimum=None, maximum=None) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and parsed < minimum:
        return default
    if maximum is not None and parsed > maximum:
        return default
    return parsed


# Local configuration module
import config


def _split_managed_roots(raw_value: str) -> List[str]:
    if not raw_value:
        return []
    return [item.strip() for item in re.split(r"[;,\n]+", raw_value) if item.strip()]


def get_managed_media_roots() -> List[Path]:
    candidates = [
        getattr(config, "DELIVERY_FOLDER", ""),
        os.getenv("JELLYFIN_LIBRARY_PATH", ""),
        *_split_managed_roots(os.getenv("BUS_TERMINAL_MANAGED_MEDIA_ROOTS", "")),
    ]
    roots: List[Path] = []
    for candidate in candidates:
        if not candidate:
            continue
        try:
            roots.append(Path(candidate).expanduser().resolve())
        except Exception:
            logger.warning("Invalid managed media root ignored: %s", candidate)
    return roots


def is_path_under_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

try:
    import psycopg2
    from psycopg2 import pool as pg_pool
    from psycopg2.extras import RealDictCursor
except Exception:  # pragma: no cover - psycopg2 may be missing in SQLite-only environments
    psycopg2 = None
    pg_pool = None
    RealDictCursor = None


def _row_value(row, key: str, index: int):
    """跨后端取值：PG 的 cursor 返回 RealDictRow（按列名），SQLite 返回元组（按序号）。

    写成 row[0] 会在 PostgreSQL 上抛 KeyError——这类失败很隐蔽：方法内部
    except 兜住只记一行日志就返回空列表，调用方看到的是「没数据」而不是「出错」。
    生产实测踩过：翻译占位保护拿到 0 个演员名、缺头像查询恒返回空，都是这个原因。
    """
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[index]
    except (IndexError, KeyError, TypeError):
        return None



class Database:
    """Unified database interface.

    The constructor parses ``config.DATABASE_URL`` and initialises either a SQLite
    connection (default) or a PostgreSQL connection pool.
    """

    def __init__(self):
        self._cache_metrics_lock = threading.Lock()
        self._cache_metrics: Dict[str, Dict[str, int]] = {}
        # 配置读取的进程内短缓存：{key: (读取时间戳, 值)}
        self._config_cache: Dict[str, Tuple[float, Any]] = {}
        self.db_url = config.DATABASE_URL
        self.parsed = urlparse(self.db_url)
        self.is_postgres = self.parsed.scheme.startswith("postgres")
        if self.is_postgres:
            if not psycopg2:
                raise RuntimeError("psycopg2 is required for PostgreSQL support but is not installed.")
            self._init_postgres_pool()
        else:
            self.db_path = sqlite_path_from_url(self.db_url)
            self._init_sqlite()

    # ---------------------------------------------------------------------
    # SQLite helpers
    # ---------------------------------------------------------------------
    def _init_sqlite(self):
        self._ensure_sqlite_db()
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        finally:
            conn.close()
        self.init_db()

    def _ensure_sqlite_db(self):
        # Ensure the directory exists for the SQLite file
        dir_name = os.path.dirname(self.db_path)
        if dir_name and not os.path.isdir(dir_name):
            os.makedirs(dir_name, exist_ok=True)

    def _sqlite_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------------------------------------------------------------------
    # PostgreSQL helpers
    # ---------------------------------------------------------------------
    def _init_postgres_pool(self):
        # Expected format: postgresql://user:pass@host:port/dbname
        dbname = self.parsed.path.lstrip("/")
        user = self.parsed.username
        password = self.parsed.password
        host = self.parsed.hostname or "localhost"
        port = self.parsed.port or 5432

        # 宿主机环境适配：若在容器外运行且 Docker 内部主机名不可解析，自动切为本地映射端口 127.0.0.1:15432
        if not os.path.exists('/.dockerenv'):
            try:
                import socket
                socket.gethostbyname(host)
            except Exception:
                if host in ("postgres", "yilin-postgres", "db"):
                    host = "127.0.0.1"
                    if port == 5432:
                        port = 15432

        self.pg_pool = pg_pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=config.DATABASE_POOL_MAX_CONNECTIONS,
            dbname=dbname,
            user=user,
            password=password,
            host=host,
            port=port,
        )

        # Initialise schema for PostgreSQL
        self.init_db()

    def _postgres_connection(self):
        conn = self.pg_pool.getconn()
        # 池子里可能残留已被对端掐断的连接（空闲超时/网络抖动），拿出来先验一下：
        # 坏连接直接丢弃换新的，否则会持续报 "server closed the connection unexpectedly"
        for _ in range(2):
            if not getattr(conn, 'closed', 0):
                break
            self.pg_pool.putconn(conn, close=True)
            conn = self.pg_pool.getconn()
        try:
            conn.set_client_encoding('UTF8')
        except Exception:
            pass
        # Use RealDictCursor for dict-like rows (compatible with SQLite Row objects)
        conn.cursor_factory = RealDictCursor
        return conn

    # ---------------------------------------------------------------------
    # Unified connection handling
    # ---------------------------------------------------------------------
    def get_connection(self):
        """Return a database connection.

        For SQLite a fresh connection is returned (caller must close it).
        For PostgreSQL a connection from the pool is returned; callers must call
        ``self._release_connection(conn)`` after use. The public methods in this
        class handle the release automatically.
        """
        if self.is_postgres:
            return self._postgres_connection()
        else:
            return self._sqlite_connection()

    def _release_connection(self, conn):
        if self.is_postgres:
            # Pooled PostgreSQL connections must never retain an open or failed
            # transaction for the next request. Successful writes commit before
            # release, while reads and failed writes are safe to roll back.
            if getattr(conn, 'closed', 0):
                # 连接已经废了，别再放回池子污染后续请求
                self.pg_pool.putconn(conn, close=True)
                return
            try:
                conn.rollback()
            except Exception:
                logging.warning("Failed to reset PostgreSQL connection before release")
                self.pg_pool.putconn(conn, close=True)
                return
            self.pg_pool.putconn(conn)
        else:
            conn.close()

    # ---------------------------------------------------------------------
    # ---------------------------------------------------------------------
    def init_db(self):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            schema_preexisting = self._table_exists(cursor, "movies")
            if self.is_postgres:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS movies (
                        id TEXT PRIMARY KEY,
                        title TEXT,
                        original_title TEXT,
                        cover TEXT,
                        detail_url TEXT,
                        release_date TEXT,
                        scraped_at DOUBLE PRECISION,
                        detail_json JSONB,
                        is_watched BOOLEAN DEFAULT FALSE,
                        last_watched_at DOUBLE PRECISION,
                        watch_count INTEGER DEFAULT 0,
                        is_liked BOOLEAN DEFAULT FALSE,
                        created_at DOUBLE PRECISION
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS local_files (
                        movie_id TEXT PRIMARY KEY,
                        file_path TEXT,
                        symlink_path TEXT,
                        file_size BIGINT,
                        added_at DOUBLE PRECISION
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cache (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        updated_at DOUBLE PRECISION
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS subscriptions (
                        url TEXT PRIMARY KEY,
                        name TEXT,
                        category_type TEXT,
                        start_date TEXT,
                        exclude_tags TEXT,
                        last_checked_at DOUBLE PRECISION,
                        created_at DOUBLE PRECISION
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        username TEXT PRIMARY KEY,
                        password_hash TEXT,
                        created_at DOUBLE PRECISION,
                        must_change_password BOOLEAN DEFAULT FALSE
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS downloads (
                        movie_id TEXT PRIMARY KEY,
                        downloaded_at DOUBLE PRECISION
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS translations (
                        original_text TEXT PRIMARY KEY,
                        translated_text TEXT,
                        updated_at DOUBLE PRECISION
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS config (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        updated_at DOUBLE PRECISION
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jable_hot_list (
                        id TEXT PRIMARY KEY,
                        video_id TEXT,
                        sort_by TEXT,
                        title TEXT,
                        added_at DOUBLE PRECISION,
                        status TEXT DEFAULT 'pending'
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pending_downloads (
                        id TEXT PRIMARY KEY,
                        movie_id TEXT,
                        title TEXT,
                        source TEXT,
                        added_at DOUBLE PRECISION,
                        last_checked_at DOUBLE PRECISION,
                        magnet TEXT,
                        status TEXT DEFAULT 'pending',
                        retry_count INTEGER DEFAULT 0,
                        note TEXT
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS download_queue (
                        id TEXT PRIMARY KEY,
                        magnet TEXT NOT NULL,
                        movie_id TEXT,
                        title TEXT,
                        status TEXT,
                        source_url TEXT,
                        save_path TEXT,
                        category TEXT,
                        retry_count INTEGER DEFAULT 0,
                        error_message TEXT,
                        next_retry_at DOUBLE PRECISION,
                        created_at DOUBLE PRECISION,
                        updated_at DOUBLE PRECISION
                    )
                    """,
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS actresses (
                        id TEXT PRIMARY KEY,
                        name TEXT,
                        avatar TEXT,
                        birth_date DATE,
                        height TEXT,
                        cup TEXT,
                        bust TEXT,
                        waist TEXT,
                        hip TEXT,
                        hometown TEXT,
                        hobby TEXT,
                        page_url TEXT,
                        bio TEXT,
                        wiki_url TEXT,
                        aliases TEXT,
                        name_en TEXT,
                        source TEXT,
                        source_id TEXT,
                        source_updated_at DOUBLE PRECISION,
                        video_count INTEGER,
                        metadata_requested_at DOUBLE PRECISION,
                        metadata_last_attempt_at DOUBLE PRECISION,
                        metadata_attempt_count INTEGER DEFAULT 0,
                        metadata_first_error_at DOUBLE PRECISION,
                        metadata_last_error TEXT,
                        metadata_completed_at DOUBLE PRECISION,
                        is_followed BOOLEAN DEFAULT FALSE,
                        last_updated DOUBLE PRECISION,
                        created_at DOUBLE PRECISION
                    )
                    """,
                )
            else:
                cursor.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS movies (
                        id TEXT PRIMARY KEY,
                        title TEXT,
                        original_title TEXT,
                        cover TEXT,
                        detail_url TEXT,
                        release_date TEXT,
                        scraped_at REAL,
                        detail_json TEXT,
                        is_watched INTEGER DEFAULT 0,
                        last_watched_at REAL,
                        watch_count INTEGER DEFAULT 0,
                        is_liked INTEGER DEFAULT 0,
                        created_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS local_files (
                        movie_id TEXT PRIMARY KEY,
                        file_path TEXT,
                        symlink_path TEXT,
                        file_size INTEGER,
                        added_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS cache (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        updated_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS subscriptions (
                        url TEXT PRIMARY KEY,
                        name TEXT,
                        category_type TEXT,
                        start_date TEXT,
                        exclude_tags TEXT,
                        last_checked_at REAL,
                        created_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS users (
                        username TEXT PRIMARY KEY,
                        password_hash TEXT,
                        created_at REAL,
                        must_change_password INTEGER DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS downloads (
                        movie_id TEXT PRIMARY KEY,
                        downloaded_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS translations (
                        original_text TEXT PRIMARY KEY,
                        translated_text TEXT,
                        updated_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS config (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        updated_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS jable_hot_list (
                        id TEXT PRIMARY KEY,
                        video_id TEXT,
                        sort_by TEXT,
                        title TEXT,
                        added_at REAL,
                        status TEXT DEFAULT 'pending'
                    );
                    CREATE TABLE IF NOT EXISTS pending_downloads (
                        id TEXT PRIMARY KEY,
                        movie_id TEXT,
                        title TEXT,
                        source TEXT,
                        added_at REAL,
                        last_checked_at REAL,
                        magnet TEXT,
                        status TEXT DEFAULT 'pending',
                        retry_count INTEGER DEFAULT 0,
                        note TEXT
                    );
                    CREATE TABLE IF NOT EXISTS download_queue (
                        id TEXT PRIMARY KEY,
                        magnet TEXT NOT NULL,
                        movie_id TEXT,
                        title TEXT,
                        status TEXT,
                        source_url TEXT,
                        save_path TEXT,
                        category TEXT,
                        retry_count INTEGER DEFAULT 0,
                        error_message TEXT,
                        next_retry_at REAL,
                        created_at REAL,
                        updated_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS actresses (
                        id TEXT PRIMARY KEY,
                        name TEXT,
                        avatar TEXT,
                        birth_date DATE,
                        height TEXT,
                        cup TEXT,
                        bust TEXT,
                        waist TEXT,
                        hip TEXT,
                        hometown TEXT,
                        hobby TEXT,
                        page_url TEXT,
                        bio TEXT,
                        wiki_url TEXT,
                        aliases TEXT,
                        name_en TEXT,
                        source TEXT,
                        source_id TEXT,
                        source_updated_at REAL,
                        video_count INTEGER,
                        metadata_requested_at REAL,
                        metadata_last_attempt_at REAL,
                        metadata_attempt_count INTEGER DEFAULT 0,
                        metadata_first_error_at REAL,
                        metadata_last_error TEXT,
                        metadata_completed_at REAL,
                        is_followed INTEGER DEFAULT 0,
                        last_updated REAL,
                        created_at REAL
                    );
                    """,
                )
            if not schema_preexisting:
                # 全新库：历史遗留列（如 movies.code_norm）不在 CREATE TABLE 里，
                # 必须先补齐再建索引，否则 idx_movies_code_norm 会因缺列直接报错。
                self._ensure_core_tables(cursor)
                self._ensure_legacy_columns(cursor)
                self._ensure_indexes(cursor, include_unique=True)
                self._ensure_schema_migrations_table(cursor)
                self._mark_all_schema_migrations_applied(cursor)
            elif self._schema_migration_enabled():
                self._ensure_core_tables(cursor)
                self._ensure_schema_migrations_table(cursor)
                self._run_pending_schema_migrations(cursor)
            else:
                logging.warning(
                    "Automatic schema migration is disabled; inspect /api/health/check and set %s=true only during an approved migration window.",
                    ALLOW_SCHEMA_MIGRATION,
                )
            conn.commit()
        finally:
            self._release_connection(conn)

    @staticmethod
    def _schema_migration_enabled() -> bool:
        return os.getenv(ALLOW_SCHEMA_MIGRATION, "").lower() in {"1", "true", "yes", "on"}

    def _table_exists(self, cursor, table_name: str) -> bool:
        if self.is_postgres:
            cursor.execute("SELECT to_regclass(%s) AS table_name", (table_name,))
            row = cursor.fetchone()
            return bool(row and row["table_name"])
        cursor.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,))
        return cursor.fetchone() is not None

    def _ensure_schema_migrations_table(self, cursor) -> None:
        timestamp_type = "DOUBLE PRECISION" if self.is_postgres else "REAL"
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at {timestamp_type} NOT NULL
            )
            """
        )

    def _mark_schema_migration_applied(self, cursor, version: int, name: str) -> None:
        sql = (
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (%s, %s, %s) ON CONFLICT (version) DO NOTHING"
            if self.is_postgres
            else "INSERT OR IGNORE INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)"
        )
        cursor.execute(sql, (version, name, time.time()))

    def _mark_all_schema_migrations_applied(self, cursor) -> None:
        for version, name in SCHEMA_MIGRATIONS:
            self._mark_schema_migration_applied(cursor, version, name)

    def _get_applied_schema_versions(self, cursor) -> set:
        if not self._table_exists(cursor, "schema_migrations"):
            return set()
        cursor.execute("SELECT version FROM schema_migrations")
        return {int(row["version"]) for row in cursor.fetchall()}

    def _run_pending_schema_migrations(self, cursor) -> None:
        applied = self._get_applied_schema_versions(cursor)
        for version, name in SCHEMA_MIGRATIONS:
            if version in applied:
                continue
            if version == 1:
                self._ensure_legacy_columns(cursor)
                self._backfill_legacy_created_at(cursor)
                self._normalize_postgres_birth_date(cursor)
            elif version == 2:
                self._dedupe_active_download_queue(cursor)
                self._ensure_indexes(cursor, include_unique=True)
            elif version == 3:
                self._sync_existing_actress_names(cursor)
            elif version == 4:
                self._ensure_download_queue_retry_schedule(cursor)
            elif version == 5:
                self._ensure_external_tasks_table(cursor)
            elif version == 6:
                self._ensure_actress_metadata_retry_audit(cursor)
            elif version == 7:
                self._ensure_lifecycle_state_dimensions(cursor)
            elif version == 8:
                self._ensure_cleanup_candidate_decision_audit(cursor)
            elif version == 9:
                self._ensure_download_queue_options(cursor)
            elif version == 10:
                self._ensure_local_files_amane_columns(cursor)
            elif version == 11:
                self._ensure_local_files_jellyfin_columns(cursor)
            self._mark_schema_migration_applied(cursor, version, name)
            logging.info("Applied schema migration %s (%s)", version, name)

    def _normalize_postgres_birth_date(self, cursor) -> None:
        if not self.is_postgres:
            return
        cursor.execute(
            """
            ALTER TABLE actresses
            ALTER COLUMN birth_date TYPE DATE
            USING CASE
                WHEN birth_date::text ~ '^\\d{4}-\\d{2}-\\d{2}$' THEN birth_date::date
                ELSE NULL
            END
            """
        )

    def _backfill_legacy_created_at(self, cursor) -> None:
        cursor.execute("UPDATE movies SET created_at = scraped_at WHERE created_at IS NULL")
        cursor.execute("UPDATE actresses SET created_at = last_updated WHERE created_at IS NULL")

    def _ensure_download_queue_retry_schedule(self, cursor) -> None:
        columns = self._get_table_columns(cursor, "download_queue")
        if columns and "next_retry_at" not in columns:
            column_type = "DOUBLE PRECISION" if self.is_postgres else "REAL"
            cursor.execute(f"ALTER TABLE download_queue ADD COLUMN next_retry_at {column_type}")

        cursor.execute("DROP INDEX IF EXISTS uq_download_queue_active_movie")
        cursor.execute("DROP INDEX IF EXISTS uq_download_queue_active_magnet")
        self._ensure_indexes(cursor, include_unique=True)

    def _ensure_download_queue_options(self, cursor) -> None:
        columns = self._get_table_columns(cursor, "download_queue")
        for name in ("save_path", "category"):
            if columns and name not in columns:
                cursor.execute(f"ALTER TABLE download_queue ADD COLUMN {name} TEXT")

    def _ensure_external_tasks_table(self, cursor) -> None:
        timestamp_type = "DOUBLE PRECISION" if self.is_postgres else "REAL"
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS external_tasks (
                id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                payload TEXT,
                source TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 20,
                first_error_at {timestamp_type},
                last_error_at {timestamp_type},
                last_error TEXT,
                next_retry_at {timestamp_type},
                result TEXT,
                created_at {timestamp_type} NOT NULL,
                updated_at {timestamp_type} NOT NULL,
                completed_at {timestamp_type},
                UNIQUE (task_type, dedupe_key)
            )
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_external_tasks_due ON external_tasks (status, next_retry_at, created_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_external_tasks_type_status ON external_tasks (task_type, status, updated_at)"
        )

    def _ensure_actress_metadata_retry_audit(self, cursor) -> None:
        columns = self._get_table_columns(cursor, "actresses")
        column_types = {
            "metadata_first_error_at": "DOUBLE PRECISION" if self.is_postgres else "REAL",
            "metadata_last_error": "TEXT",
            "metadata_completed_at": "DOUBLE PRECISION" if self.is_postgres else "REAL",
        }
        for column_name, column_type in column_types.items():
            if columns and column_name not in columns:
                cursor.execute(f"ALTER TABLE actresses ADD COLUMN {column_name} {column_type}")

    def _ensure_lifecycle_state_dimensions(self, cursor) -> None:
        lifecycle_columns = self._get_table_columns(cursor, "movie_lifecycle")
        lifecycle_additions = {
            "discovery_status": "TEXT DEFAULT 'unknown'",
            "decision_status": "TEXT DEFAULT 'pending'",
            "organize_status": "TEXT DEFAULT 'pending'",
        }
        for column_name, column_type in lifecycle_additions.items():
            if lifecycle_columns and column_name not in lifecycle_columns:
                cursor.execute(f"ALTER TABLE movie_lifecycle ADD COLUMN {column_name} {column_type}")

        event_columns = self._get_table_columns(cursor, "movie_lifecycle_events")
        event_additions = {
            "discovery_status": "TEXT",
            "decision_status": "TEXT",
            "organize_status": "TEXT",
            "status_dimension": "TEXT",
            "old_status": "TEXT",
            "new_status": "TEXT",
            "execution_result": "TEXT",
            "error_summary": "TEXT",
        }
        for column_name, column_type in event_additions.items():
            if event_columns and column_name not in event_columns:
                cursor.execute(f"ALTER TABLE movie_lifecycle_events ADD COLUMN {column_name} {column_type}")

    def _ensure_local_files_amane_columns(self, cursor) -> None:
        """local_files 记 amane 侧的媒体文件/任务身份，便于双向定位。"""
        if not self._table_exists(cursor, "local_files"):
            return
        columns = self._get_table_columns(cursor, "local_files")
        timestamp_type = "DOUBLE PRECISION" if self.is_postgres else "REAL"
        for column_name, column_type in (
            ("amane_media_file_id", "INTEGER"),
            ("amane_task_id", "INTEGER"),
            ("amane_synced_at", timestamp_type),
        ):
            if columns and column_name not in columns:
                cursor.execute(f"ALTER TABLE local_files ADD COLUMN {column_name} {column_type}")

    def _ensure_local_files_jellyfin_columns(self, cursor) -> None:
        """local_files 记 Jellyfin 侧条目身份，闭环收尾时确认该片已可见。"""
        if not self._table_exists(cursor, "local_files"):
            return
        columns = self._get_table_columns(cursor, "local_files")
        timestamp_type = "DOUBLE PRECISION" if self.is_postgres else "REAL"
        for column_name, column_type in (
            ("jellyfin_item_id", "TEXT"),
            ("jellyfin_synced_at", timestamp_type),
        ):
            if columns and column_name not in columns:
                cursor.execute(f"ALTER TABLE local_files ADD COLUMN {column_name} {column_type}")

    def _ensure_cleanup_candidate_decision_audit(self, cursor) -> None:
        columns = self._get_table_columns(cursor, "cleanup_candidates")
        if not columns:
            return
        timestamp_type = "DOUBLE PRECISION" if self.is_postgres else "REAL"
        for column_name, column_type in (
            ("decision_actor", "TEXT"),
            ("decision_reason", "TEXT"),
            ("decided_at", timestamp_type),
        ):
            if column_name in columns:
                continue
            if self.is_postgres:
                cursor.execute(
                    f"ALTER TABLE cleanup_candidates ADD COLUMN IF NOT EXISTS {column_name} {column_type}"
                )
            else:
                cursor.execute(f"ALTER TABLE cleanup_candidates ADD COLUMN {column_name} {column_type}")

    def _ensure_core_tables(self, cursor):
        self._ensure_external_tasks_table(cursor)
        if self.is_postgres:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_downloads (
                    id TEXT PRIMARY KEY,
                    movie_id TEXT,
                    title TEXT,
                    source TEXT,
                    added_at DOUBLE PRECISION,
                    last_checked_at DOUBLE PRECISION,
                    magnet TEXT,
                    status TEXT DEFAULT 'pending',
                    retry_count INTEGER DEFAULT 0,
                    note TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS cleanup_candidates (
                    id TEXT PRIMARY KEY,
                    jellyfin_id TEXT UNIQUE,
                    movie_id TEXT,
                    title TEXT,
                    file_path TEXT,
                    progress INTEGER,
                    is_favorite BOOLEAN DEFAULT FALSE,
                    reason TEXT,
                    status TEXT DEFAULT 'candidate',
                    source TEXT,
                    decision_actor TEXT,
                    decision_reason TEXT,
                    decided_at DOUBLE PRECISION,
                    created_at DOUBLE PRECISION,
                    updated_at DOUBLE PRECISION,
                    last_seen_at DOUBLE PRECISION
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS movie_lifecycle (
                    movie_id TEXT PRIMARY KEY,
                    title TEXT,
                    discovery_status TEXT DEFAULT 'unknown',
                    decision_status TEXT DEFAULT 'pending',
                    download_status TEXT DEFAULT 'unknown',
                    organize_status TEXT DEFAULT 'pending',
                    jellyfin_status TEXT DEFAULT 'unknown',
                    cleanup_status TEXT DEFAULT 'none',
                    source TEXT,
                    last_event TEXT,
                    last_reason TEXT,
                    created_at DOUBLE PRECISION,
                    updated_at DOUBLE PRECISION
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS movie_lifecycle_events (
                    id TEXT PRIMARY KEY,
                    movie_id TEXT,
                    event_type TEXT,
                    discovery_status TEXT,
                    decision_status TEXT,
                    download_status TEXT,
                    organize_status TEXT,
                    jellyfin_status TEXT,
                    cleanup_status TEXT,
                    status_dimension TEXT,
                    old_status TEXT,
                    new_status TEXT,
                    source TEXT,
                    reason TEXT,
                    execution_result TEXT,
                    error_summary TEXT,
                    payload TEXT,
                    created_at DOUBLE PRECISION
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS automation_candidates (
                    id TEXT PRIMARY KEY,
                    candidate_type TEXT,
                    subject_id TEXT,
                    subject_name TEXT,
                    source_movie_id TEXT,
                    source_title TEXT,
                    source TEXT,
                    recommended_action TEXT,
                    status TEXT DEFAULT 'candidate',
                    confidence DOUBLE PRECISION DEFAULT 0,
                    reason TEXT,
                    payload TEXT,
                    created_at DOUBLE PRECISION,
                    updated_at DOUBLE PRECISION,
                    decided_at DOUBLE PRECISION
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS automation_events (
                    id TEXT PRIMARY KEY,
                    candidate_id TEXT,
                    event_type TEXT,
                    actor TEXT,
                    reason TEXT,
                    payload TEXT,
                    created_at DOUBLE PRECISION
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS actress_names (
                    actress_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    name_type TEXT,
                    source TEXT,
                    created_at DOUBLE PRECISION,
                    PRIMARY KEY (actress_id, name)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS movie_actresses (
                    movie_id TEXT NOT NULL,
                    actress_id TEXT NOT NULL,
                    actress_name TEXT,
                    source TEXT,
                    source_id TEXT,
                    created_at DOUBLE PRECISION,
                    PRIMARY KEY (movie_id, actress_id)
                )
                """
            )
            return

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_downloads (
                id TEXT PRIMARY KEY,
                movie_id TEXT,
                title TEXT,
                source TEXT,
                added_at REAL,
                last_checked_at REAL,
                magnet TEXT,
                status TEXT DEFAULT 'pending',
                retry_count INTEGER DEFAULT 0,
                note TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS cleanup_candidates (
                id TEXT PRIMARY KEY,
                jellyfin_id TEXT UNIQUE,
                movie_id TEXT,
                title TEXT,
                file_path TEXT,
                progress INTEGER,
                is_favorite INTEGER DEFAULT 0,
                reason TEXT,
                status TEXT DEFAULT 'candidate',
                source TEXT,
                decision_actor TEXT,
                decision_reason TEXT,
                decided_at REAL,
                created_at REAL,
                updated_at REAL,
                last_seen_at REAL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS movie_lifecycle (
                movie_id TEXT PRIMARY KEY,
                title TEXT,
                discovery_status TEXT DEFAULT 'unknown',
                decision_status TEXT DEFAULT 'pending',
                download_status TEXT DEFAULT 'unknown',
                organize_status TEXT DEFAULT 'pending',
                jellyfin_status TEXT DEFAULT 'unknown',
                cleanup_status TEXT DEFAULT 'none',
                source TEXT,
                last_event TEXT,
                last_reason TEXT,
                created_at REAL,
                updated_at REAL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS movie_lifecycle_events (
                id TEXT PRIMARY KEY,
                movie_id TEXT,
                event_type TEXT,
                discovery_status TEXT,
                decision_status TEXT,
                download_status TEXT,
                organize_status TEXT,
                jellyfin_status TEXT,
                cleanup_status TEXT,
                status_dimension TEXT,
                old_status TEXT,
                new_status TEXT,
                source TEXT,
                reason TEXT,
                execution_result TEXT,
                error_summary TEXT,
                payload TEXT,
                created_at REAL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS automation_candidates (
                id TEXT PRIMARY KEY,
                candidate_type TEXT,
                subject_id TEXT,
                subject_name TEXT,
                source_movie_id TEXT,
                source_title TEXT,
                source TEXT,
                recommended_action TEXT,
                status TEXT DEFAULT 'candidate',
                confidence REAL DEFAULT 0,
                reason TEXT,
                payload TEXT,
                created_at REAL,
                updated_at REAL,
                decided_at REAL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS automation_events (
                id TEXT PRIMARY KEY,
                candidate_id TEXT,
                event_type TEXT,
                actor TEXT,
                reason TEXT,
                payload TEXT,
                created_at REAL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS actress_names (
                actress_id TEXT NOT NULL,
                name TEXT NOT NULL,
                name_type TEXT,
                source TEXT,
                created_at REAL,
                PRIMARY KEY (actress_id, name)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS movie_actresses (
                movie_id TEXT NOT NULL,
                actress_id TEXT NOT NULL,
                actress_name TEXT,
                source TEXT,
                source_id TEXT,
                created_at REAL,
                PRIMARY KEY (movie_id, actress_id)
            )
            """
        )

    def _ensure_legacy_columns(self, cursor):
        columns_by_table = {
            "download_queue": {
                "source_url": "TEXT",
            },
            "movies": {
                "is_watched": "BOOLEAN DEFAULT FALSE" if self.is_postgres else "INTEGER DEFAULT 0",
                "last_watched_at": "DOUBLE PRECISION" if self.is_postgres else "REAL",
                "watch_count": "INTEGER DEFAULT 0",
                "is_liked": "BOOLEAN DEFAULT FALSE" if self.is_postgres else "INTEGER DEFAULT 0",
                "created_at": "DOUBLE PRECISION" if self.is_postgres else "REAL",
                # 2026-09-24 防二次污染：归一化番号（与清洗脚本同一规则，供入库查重）
                "code_norm": "TEXT",
            },
            "users": {
                "must_change_password": "BOOLEAN DEFAULT FALSE" if self.is_postgres else "INTEGER DEFAULT 0",
            },
            "actresses": {
                "bio": "TEXT",
                "wiki_url": "TEXT",
                "aliases": "TEXT",
                "name_en": "TEXT",
                "source": "TEXT",
                "source_id": "TEXT",
                "source_updated_at": "DOUBLE PRECISION" if self.is_postgres else "REAL",
                "video_count": "INTEGER",
                "metadata_requested_at": "DOUBLE PRECISION" if self.is_postgres else "REAL",
                "metadata_last_attempt_at": "DOUBLE PRECISION" if self.is_postgres else "REAL",
                "metadata_attempt_count": "INTEGER DEFAULT 0",
                "created_at": "DOUBLE PRECISION" if self.is_postgres else "REAL",
            },
        }

        for table_name, columns in columns_by_table.items():
            existing = self._get_table_columns(cursor, table_name)
            if not existing:
                continue

            for column_name, column_type in columns.items():
                if column_name in existing:
                    continue
                if self.is_postgres:
                    cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {column_type}")
                else:
                    cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")

    def _sync_existing_actress_names(self, cursor):
        existing = self._get_table_columns(cursor, "actress_names")
        if not existing:
            return
        cursor.execute("SELECT id, name, aliases, name_en, source FROM actresses")
        for row in cursor.fetchall():
            data = dict(row)
            self._save_actress_names_cursor(
                cursor,
                data.get("id"),
                self._collect_actress_names(data, data.get("aliases") or ""),
            )

    def _get_table_columns(self, cursor, table_name: str) -> set:
        if self.is_postgres:
            cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = %s
                """,
                (table_name,),
            )
            return {row["column_name"] if isinstance(row, dict) else row[0] for row in cursor.fetchall()}

        cursor.execute(f"PRAGMA table_info({table_name})")
        return {row["name"] if "name" in row.keys() else row[1] for row in cursor.fetchall()}

    def _dedupe_active_download_queue(self, cursor):
        active_statuses = {"pending", "waiting_external_service", "submitted", "processing"}
        cursor.execute("SELECT id, movie_id, magnet, status, updated_at, created_at FROM download_queue")
        rows = [dict(row) for row in cursor.fetchall()]

        kept_by_movie = set()
        kept_by_magnet = set()
        duplicate_ids = set()

        def sort_key(row):
            return row.get("updated_at") or row.get("created_at") or 0

        for row in sorted(rows, key=sort_key, reverse=True):
            if row.get("status") not in active_statuses:
                continue

            movie_id = row.get("movie_id")
            magnet = row.get("magnet")
            duplicate = False

            if movie_id:
                if movie_id in kept_by_movie:
                    duplicate = True
                else:
                    kept_by_movie.add(movie_id)

            if magnet:
                if magnet in kept_by_magnet:
                    duplicate = True
                else:
                    kept_by_magnet.add(magnet)

            if duplicate:
                duplicate_ids.add(row["id"])

        if not duplicate_ids:
            return

        now = time.time()
        placeholder = "%s" if self.is_postgres else "?"
        sql = (
            f"UPDATE download_queue SET status = {placeholder}, error_message = {placeholder}, updated_at = {placeholder} "
            f"WHERE id = {placeholder}"
        )
        for task_id in duplicate_ids:
            cursor.execute(sql, ("duplicate_skipped", "Duplicate active queue entry suppressed by schema migration", now, task_id))
        logging.info("Marked %s duplicate download_queue rows as duplicate_skipped", len(duplicate_ids))

    def _ensure_indexes(self, cursor, include_unique: bool = False):
        index_statements = [
            "CREATE INDEX IF NOT EXISTS idx_movies_release_date ON movies (release_date)",
            "CREATE INDEX IF NOT EXISTS idx_movies_scraped_at ON movies (scraped_at)",
            "CREATE INDEX IF NOT EXISTS idx_movies_detail_url ON movies (detail_url)",
            "CREATE INDEX IF NOT EXISTS idx_movies_code_norm ON movies (code_norm)",
            "CREATE INDEX IF NOT EXISTS idx_download_queue_status_created ON download_queue (status, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_download_queue_movie_status ON download_queue (movie_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_download_queue_magnet_status ON download_queue (magnet, status)",
            "CREATE INDEX IF NOT EXISTS idx_pending_downloads_status_added ON pending_downloads (status, added_at)",
            "CREATE INDEX IF NOT EXISTS idx_jable_hot_status_added ON jable_hot_list (status, added_at)",
            "CREATE INDEX IF NOT EXISTS idx_actresses_followed_updated ON actresses (is_followed, last_updated)",
            "CREATE INDEX IF NOT EXISTS idx_actresses_source ON actresses (source, source_id)",
            "CREATE INDEX IF NOT EXISTS idx_actress_names_name ON actress_names (name)",
            "CREATE INDEX IF NOT EXISTS idx_actress_names_actress ON actress_names (actress_id)",
            "CREATE INDEX IF NOT EXISTS idx_movie_actresses_actress ON movie_actresses (actress_id, movie_id)",
            "CREATE INDEX IF NOT EXISTS idx_movie_actresses_name ON movie_actresses (actress_name, movie_id)",
            "CREATE INDEX IF NOT EXISTS idx_cleanup_candidates_status_seen ON cleanup_candidates (status, last_seen_at)",
            "CREATE INDEX IF NOT EXISTS idx_cleanup_candidates_movie ON cleanup_candidates (movie_id)",
            "CREATE INDEX IF NOT EXISTS idx_movie_lifecycle_download ON movie_lifecycle (download_status, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_movie_lifecycle_jellyfin ON movie_lifecycle (jellyfin_status, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_movie_lifecycle_cleanup ON movie_lifecycle (cleanup_status, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_movie_lifecycle_events_movie ON movie_lifecycle_events (movie_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_automation_candidates_status ON automation_candidates (status, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_automation_candidates_subject ON automation_candidates (candidate_type, subject_id)",
            "CREATE INDEX IF NOT EXISTS idx_automation_events_candidate ON automation_events (candidate_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_movies_watched_liked_last ON movies (is_watched, is_liked, last_watched_at)",
            "CREATE INDEX IF NOT EXISTS idx_actresses_metadata_queue ON actresses (metadata_requested_at, metadata_attempt_count, metadata_last_attempt_at)",
            "CREATE INDEX IF NOT EXISTS idx_actresses_name ON actresses (name)",
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_created_at ON subscriptions (created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_local_files_added_at ON local_files (added_at)",
        ]
        unique_statements = [
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_download_queue_active_movie
            ON download_queue (movie_id)
            WHERE movie_id IS NOT NULL AND status IN ('pending', 'waiting_external_service', 'submitted', 'processing')
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_download_queue_active_magnet
            ON download_queue (magnet)
            WHERE magnet IS NOT NULL AND status IN ('pending', 'waiting_external_service', 'submitted', 'processing')
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_automation_candidate_source_subject
            ON automation_candidates (candidate_type, subject_id, source_movie_id)
            WHERE subject_id IS NOT NULL AND source_movie_id IS NOT NULL
            """,
        ]

        if include_unique and self.is_postgres:
            # 仅 PG:同 detail_url 唯一(硬兜底)。SQLite 测试库仍有存量重复,不加。
            unique_statements.append(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_movies_detail_url
                ON movies (detail_url)
                WHERE detail_url IS NOT NULL AND detail_url <> ''
                """
            )

        statements = index_statements + unique_statements if include_unique else index_statements
        for statement in statements:
            cursor.execute(statement)

    def get_schema_migration_status(self) -> Dict[str, Any]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            applied_versions = self._get_applied_schema_versions(cursor)
            migrations = [
                {
                    "version": version,
                    "name": name,
                    "status": "applied" if version in applied_versions else "pending",
                }
                for version, name in SCHEMA_MIGRATIONS
            ]
            return {
                "current_version": max(applied_versions) if applied_versions else 0,
                "latest_version": SCHEMA_MIGRATIONS[-1][0],
                "migration_required": any(item["status"] == "pending" for item in migrations),
                "migration_enabled": self._schema_migration_enabled(),
                "migrations": migrations,
            }
        finally:
            self._release_connection(conn)

    # ---------------------------------------------------------------------
    # Helper to adapt parameter placeholders
    # ---------------------------------------------------------------------
    def _placeholders(self, count: int) -> str:
        """Return a comma-separated placeholder string.

        SQLite uses ``?`` while PostgreSQL uses ``%s``.
        """
        ph = "%s" if self.is_postgres else "?"
        return ", ".join([ph] * count)

    def _extract_movie_actresses(self, detail_json) -> List[Dict]:
        if not detail_json:
            return []
        try:
            detail = json.loads(detail_json) if isinstance(detail_json, str) else detail_json
        except Exception:
            return []
        if not isinstance(detail, dict):
            return []

        stars = detail.get("stars") or detail.get("actresses") or []
        if not isinstance(stars, list):
            return []

        items = []
        seen = set()
        for star in stars:
            if not isinstance(star, dict):
                continue
            actress_id = str(star.get("id") or "").strip()
            actress_name = str(star.get("name") or star.get("name_en") or "").strip()
            if not actress_id and not actress_name:
                continue
            relation_id = actress_id or f"name:{actress_name}"
            if relation_id in seen:
                continue
            seen.add(relation_id)
            items.append({
                "actress_id": relation_id,
                "actress_name": actress_name,
                "source": star.get("source"),
                "source_id": star.get("source_id") or actress_id or None,
            })
        return items

    def _save_movie_actresses_cursor(self, cursor, movie_id: str, detail_json):
        actresses = self._extract_movie_actresses(detail_json)
        if not movie_id or not actresses:
            return

        # 2026-09-24 数据保护:下面这个「先删后插」在 detail_json 不完整时会吃掉已有演员关联。
        # 实测:用旧库/列表页数据回填时,同一部片只提取到 1 个演员而库里原有 5 个,
        # DELETE 全删再插 1 条 → 净丢 4 条(全流程验证中曾因此丢失 3,083 条关联)。
        # 规则:只要库里现有演员没有全部被新数据覆盖,就拒绝覆盖,宁可少更新也不丢数据。
        placeholder = "%s" if self.is_postgres else "?"
        cursor.execute(
            f"SELECT actress_id FROM movie_actresses WHERE movie_id = {placeholder}",
            (movie_id,),
        )
        previous_ids = set()
        for row in cursor.fetchall():
            actress_id = row.get("actress_id") if hasattr(row, "get") else row[0]
            if actress_id:
                previous_ids.add(str(actress_id))
        incoming_ids = {str(item.get("actress_id")) for item in actresses if item.get("actress_id")}
        if previous_ids and not previous_ids.issubset(incoming_ids):
            logger.warning(
                "skip actress sync for %s: 新数据只覆盖 %d/%d 个演员,拒绝覆盖以免丢关联",
                movie_id, len(incoming_ids & previous_ids), len(previous_ids),
            )
            return

        now = time.time()
        if self.is_postgres:
            cursor.execute("DELETE FROM movie_actresses WHERE movie_id = %s", (movie_id,))
            sql = """
                INSERT INTO movie_actresses (
                    movie_id, actress_id, actress_name, source, source_id, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (movie_id, actress_id) DO UPDATE SET
                    actress_name = EXCLUDED.actress_name,
                    source = EXCLUDED.source,
                    source_id = EXCLUDED.source_id
            """
        else:
            cursor.execute("DELETE FROM movie_actresses WHERE movie_id = ?", (movie_id,))
            sql = """
                INSERT OR REPLACE INTO movie_actresses (
                    movie_id, actress_id, actress_name, source, source_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """

        for item in actresses:
            cursor.execute(sql, (
                movie_id,
                item["actress_id"],
                item.get("actress_name"),
                item.get("source"),
                item.get("source_id"),
                now,
            ))

    # ---------------------------------------------------------------------
    # ---------------------------------------------------------------------
    def save_movie(self, movie: Dict) -> bool:
        if not isinstance(movie, dict):
            movie = dict(movie)

        if movie.get("original_title") and (not movie.get("title") or movie.get("title") == movie.get("original_title")):
            # Fill the display title from the translation cache before saving.
            cached_trans = self.get_translation(movie["original_title"])
            if cached_trans:
                movie["title"] = cached_trans

        conn = self.get_connection()
        try:
            cursor = conn.cursor()

            # 封面质量保护：下游 UPSERT 是 `cover = EXCLUDED.cover`（无条件覆盖）。
            # 若候选封面比库里已有的差（JavDB 800×538 > DMM 590×800 > JavBus），
            # 就**保留原图**—— 否则一次抓取失败就会把高清封面降级成模糊图。
            incoming_cover = str(movie.get("cover") or "").strip()
            if incoming_cover:
                cursor.execute(
                    "SELECT cover FROM movies WHERE id = %s LIMIT 1" if self.is_postgres
                    else "SELECT cover FROM movies WHERE id = ? LIMIT 1",
                    (movie.get("id"),),
                )
                cover_row = cursor.fetchone()
                if cover_row:
                    existing_cover = str(dict(cover_row).get("cover") or "").strip()
                    if existing_cover and not should_replace_cover(existing_cover, incoming_cover):
                        movie["cover"] = existing_cover

            # 防重复守卫:同 detail_url 即同一部影片。番号写法不同(如 ANPD-01/ANPD001)
            # 会绕过 id 冲突,这里把写入重定向到库内已有的规范 id,避免产生重复行。
            guard_url = (movie.get("detail_url") or "").strip()
            if guard_url:
                cursor.execute(
                    "SELECT id FROM movies WHERE detail_url = %s LIMIT 1" if self.is_postgres
                    else "SELECT id FROM movies WHERE detail_url = ? LIMIT 1",
                    (guard_url,),
                )
                url_row = cursor.fetchone()
                if url_row:
                    canonical_id = dict(url_row)["id"]
                    if canonical_id and canonical_id != movie.get("id"):
                        movie["id"] = canonical_id
            # 跨源防重复守卫（2026-09-24）：同一部片在不同源（javbus / r18.dev / jable）
            # 的番号写法不同、detail_url 也不同，仅靠上面的 URL 守卫拦不住，会导致跨源重复。
            # 这里再按归一化番号（code_norm）查一次，命中则复用库内已有 id，转成 UPSERT 更新。
            norm_code = normalize_movie_code(movie.get("id"))
            if norm_code:
                cursor.execute(
                    "SELECT id FROM movies WHERE code_norm = %s LIMIT 1" if self.is_postgres
                    else "SELECT id FROM movies WHERE code_norm = ? LIMIT 1",
                    (norm_code,),
                )
                norm_row = cursor.fetchone()
                if norm_row:
                    canonical_norm_id = dict(norm_row)["id"]
                    if canonical_norm_id and canonical_norm_id != movie.get("id"):
                        movie["id"] = canonical_norm_id
            # Retrieve existing detail_json if not supplied
            cursor.execute(
                "SELECT title, detail_json FROM movies WHERE id = %s" if self.is_postgres else "SELECT title, detail_json FROM movies WHERE id = ?",
                (movie["id"],)
            )
            row = cursor.fetchone()
            existing = dict(row) if row else None

            if existing and existing.get("title") and existing["title"] != movie.get("original_title"):
                 if not movie.get("title") or movie.get("title") == movie.get("original_title"):
                     movie["title"] = existing["title"]

            detail_json = existing["detail_json"] if existing and existing.get("detail_json") else None
            if "detail" in movie:
                detail_json = json.dumps(movie["detail"], ensure_ascii=False)

            if isinstance(detail_json, dict):
                detail_json = json.dumps(detail_json, ensure_ascii=False)

            # UPSERT using PostgreSQL syntax when needed
            if self.is_postgres:
                cursor.execute(
                    """
                    INSERT INTO movies (id, title, original_title, cover, detail_url, release_date, scraped_at, detail_json, code_norm)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        title = EXCLUDED.title,
                        original_title = EXCLUDED.original_title,
                        cover = EXCLUDED.cover,
                        detail_url = EXCLUDED.detail_url,
                        release_date = EXCLUDED.release_date,
                        scraped_at = EXCLUDED.scraped_at,
                        detail_json = EXCLUDED.detail_json,
                        code_norm = EXCLUDED.code_norm
                    """,
                    (
                        movie.get("id"),
                        movie.get("title"),
                        movie.get("original_title", ""),
                        movie.get("cover"),
                        movie.get("detail_url"),
                        movie.get("date", ""),
                        time.time(),
                        detail_json,
                        norm_code or None,
                    ),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO movies (id, title, original_title, cover, detail_url, release_date, scraped_at, detail_json, code_norm)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title,
                        original_title = excluded.original_title,
                        cover = excluded.cover,
                        detail_url = excluded.detail_url,
                        release_date = excluded.release_date,
                        scraped_at = excluded.scraped_at,
                        detail_json = excluded.detail_json,
                        code_norm = excluded.code_norm
                    """,
                    (
                        movie.get("id"),
                        movie.get("title"),
                        movie.get("original_title", ""),
                        movie.get("cover"),
                        movie.get("detail_url"),
                        movie.get("date", ""),
                        time.time(),
                        detail_json,
                        norm_code or None,
                    ),
                )
            self._save_movie_actresses_cursor(cursor, movie.get("id"), detail_json)
            conn.commit()
            return True
        except Exception as e:
            # 2026-09-24:必须回滚。PostgreSQL 中一条语句报错会让整个事务进入 aborted 状态,
            # 连接归还连接池后,后续所有操作都会连环失败(实测表现为大批
            # "Failed to save movie ... UniqueViolation" 刷屏 + server closed the connection)。
            # 同时把唯一约束冲突降级为 warning:它只说明「该影片已在库中」(源数据与库内
            # detail_url 存在细微差异时 guard 查不到),属于正常重复写入,不是系统故障。
            try:
                conn.rollback()
            except Exception:
                pass
            _msg = str(e).lower()
            if "unique" in _msg or "duplicate key" in _msg:
                logging.warning(
                    "skip duplicate movie %s: %s",
                    display_value_for_log(movie.get("id")), type(e).__name__,
                )
            else:
                # 2026-09-24:原来只记录异常类型名,定位时看不到具体原因(如哪个 KeyError),
                # 这里补上异常内容,便于后续排查。
                logging.error(
                    "Failed to save movie %s: %s: %s",
                    display_value_for_log(movie.get("id")), type(e).__name__, str(e)[:160],
                )
            return False
        finally:
            self._release_connection(conn)

    def save_movie_summaries_batch(self, movies: List[Dict]) -> int:
        """Persist list-page movie summaries in one transaction."""
        unique_movies = {
            str(movie.get("id") or "").strip(): movie
            for movie in movies
            if isinstance(movie, dict) and str(movie.get("id") or "").strip()
        }
        if not unique_movies:
            return 0

        # 防重复守卫(批内):同 detail_url 只保留第一条,避免同片不同番号写法在一批里各插一行。
        # 2026-09-24 追加：归一化番号(code_norm)相同也视为同一部片（跨源场景，如 javbus 与 r18.dev）。
        seen_urls = set()
        seen_norms = set()
        deduped_movies = {}
        for movie_id, movie in unique_movies.items():
            url = str(movie.get("detail_url") or "").strip()
            if url:
                if url in seen_urls:
                    continue
                seen_urls.add(url)
            norm_code = normalize_movie_code(movie_id)
            if norm_code:
                if norm_code in seen_norms:
                    continue
                seen_norms.add(norm_code)
            deduped_movies[movie_id] = movie
        unique_movies = deduped_movies

        now = time.time()
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholders = "%s" if self.is_postgres else "?"

            # 防重复守卫(库内):detail_url 已存在的,重定向到库内已有 id,让 upsert 命中现有行。
            if seen_urls:
                url_list = list(seen_urls)
                in_clause = ", ".join([placeholders] * len(url_list))
                cursor.execute(
                    f"SELECT id, detail_url FROM movies WHERE detail_url IN ({in_clause})",
                    url_list,
                )
                url_to_id = {dict(r)["detail_url"]: dict(r)["id"] for r in cursor.fetchall()}
            else:
                url_to_id = {}

            # 防重复守卫(库内-跨源,2026-09-24):归一化番号已存在的,同样重定向到库内已有 id。
            norm_to_id = {}
            if seen_norms:
                norm_list = list(seen_norms)
                norm_in = ", ".join([placeholders] * len(norm_list))
                cursor.execute(
                    f"SELECT id, code_norm FROM movies WHERE code_norm IN ({norm_in})",
                    norm_list,
                )
                norm_to_id = {dict(r)["code_norm"]: dict(r)["id"] for r in cursor.fetchall()}

            rows = []
            emitted_ids = set()
            for movie_id, movie in unique_movies.items():
                url = str(movie.get("detail_url") or "").strip()
                canonical_id = url_to_id.get(url, movie_id) if url else movie_id
                norm_code = normalize_movie_code(movie_id)
                if norm_code and norm_code in norm_to_id:
                    canonical_id = norm_to_id[norm_code]
                if canonical_id in emitted_ids:
                    continue
                emitted_ids.add(canonical_id)
                rows.append((
                    canonical_id,
                    movie.get("title"),
                    movie.get("original_title", ""),
                    movie.get("cover"),
                    movie.get("detail_url"),
                    movie.get("date", ""),
                    now,
                    norm_code or None,
                ))
            if not rows:
                return 0
            values = ", ".join([placeholders] * 8)
            cursor.executemany(
                f"""
                INSERT INTO movies (id, title, original_title, cover, detail_url, release_date, scraped_at, code_norm)
                VALUES ({values})
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    original_title = excluded.original_title,
                    cover = excluded.cover,
                    detail_url = excluded.detail_url,
                    release_date = excluded.release_date,
                    scraped_at = excluded.scraped_at,
                    code_norm = excluded.code_norm
                """,
                rows,
            )
            conn.commit()
            return len(rows)
        finally:
            self._release_connection(conn)

    def update_movie_titles_by_translation(self, original: str, translated: str):
        """
        Keep translated movie titles consistent when a new translation is saved.
        """
        if not original or not translated or original == translated:
            return

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "UPDATE movies SET title = %s WHERE original_title = %s" if self.is_postgres else "UPDATE movies SET title = ? WHERE original_title = ?"
            cursor.execute(sql, (translated, original))
            conn.commit()
            # logger.info("Linked database title update succeeded: %s -> %s", original, translated)
        except Exception as e:
            logging.error("Failed to update movie title translation: %s", type(e).__name__)
        finally:
            self._release_connection(conn)

    def get_movie(self, movie_id: str) -> Optional[Dict]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "SELECT * FROM movies WHERE id = %s" if self.is_postgres else "SELECT * FROM movies WHERE id = ?"
            cursor.execute(sql, (movie_id,))
            row = cursor.fetchone()
            if not row:
                return None
            # For PostgreSQL we get a tuple; convert to dict manually
            data = dict(row)
            if data.get("detail_json"):
                detail_json = data["detail_json"]
                if isinstance(detail_json, dict):
                    data["detail"] = detail_json
                elif isinstance(detail_json, str):
                    try:
                        data["detail"] = json.loads(detail_json)
                    except (TypeError, json.JSONDecodeError) as exc:
                        logging.debug("Invalid movie detail_json for %s: %s", display_value_for_log(movie_id), type(exc).__name__)
                        data["detail"] = {}
                else:
                    data["detail"] = {}
            return sanitize_movie_title_record(data)
        finally:
            self._release_connection(conn)

    def search_movies(self, query: str = None, limit: int = 50, offset: int = 0) -> List[Dict]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            if query:
                sql = (
                    "SELECT * FROM movies WHERE title LIKE %s OR original_title LIKE %s OR id LIKE %s ORDER BY release_date DESC LIMIT %s OFFSET %s"
                    if self.is_postgres
                    else "SELECT * FROM movies WHERE title LIKE ? OR original_title LIKE ? OR id LIKE ? ORDER BY release_date DESC LIMIT ? OFFSET ?"
                )
                params = (
                    f"%{query}%",
                    f"%{query}%",
                    f"%{query}%",
                    limit,
                    offset,
                )
            else:
                sql = (
                    "SELECT * FROM movies ORDER BY release_date DESC LIMIT %s OFFSET %s"
                    if self.is_postgres
                    else "SELECT * FROM movies ORDER BY release_date DESC LIMIT ? OFFSET ?"
                )
                params = (limit, offset)
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            result = [sanitize_movie_title_record(dict(r)) for r in rows]
            return result
        finally:
            self._release_connection(conn)

    def get_recent_movies_fallback(self, limit: int = 30, offset: int = 0) -> List[Dict]:
        """抓取超时或不可达时的本地数据库备选秒级降级数据"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            order_by = "id DESC"
            param_placeholder = "%s" if self.is_postgres else "?"
            sql = f"""
                SELECT id, title, original_title, cover, detail_url, release_date AS date, detail_json
                FROM movies
                ORDER BY {order_by}
                LIMIT {param_placeholder} OFFSET {param_placeholder}
            """
            cursor.execute(sql, (limit, offset))
            rows = cursor.fetchall()
            result = []
            for r in rows:
                row_dict = dict(r)
                detail = row_dict.get("detail_json") or {}
                if isinstance(detail, str):
                    try:
                        detail = json.loads(detail)
                    except Exception:
                        detail = {}
                if not isinstance(detail, dict):
                    detail = {}
                actress_names = []
                for actress in detail.get("stars") or detail.get("actresses") or []:
                    if isinstance(actress, dict):
                        name = actress.get("name") or actress.get("name_en")
                        if name:
                            actress_names.append(str(name))
                result.append(sanitize_movie_title_record({
                    "id": row_dict.get("id"),
                    "title": row_dict.get("title") or row_dict.get("original_title") or row_dict.get("id"),
                    "original_title": row_dict.get("original_title") or row_dict.get("title"),
                    "cover": row_dict.get("cover"),
                    "detail_url": row_dict.get("detail_url") or f"https://www.javbus.com/{row_dict.get('id')}",
                    "date": row_dict.get("date"),
                    "actress": ", ".join(actress_names) or None,
                    "source": detail.get("source") or detail.get("site") or "javbus"
                }))
            return result
        except Exception as e:
            logging.error("get_recent_movies_fallback 异常: %s", str(e))
            return []
        finally:
            self._release_connection(conn)

    def mark_movie_watched(self, movie_id: str):
        """Mark a movie as watched from Jellyfin."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = time.time()
            if self.is_postgres:
                sql = "UPDATE movies SET is_watched = %s, last_watched_at = %s WHERE id = %s"
                cursor.execute(sql, (True, now, movie_id))
            else:
                sql = "UPDATE movies SET is_watched = 1, last_watched_at = ? WHERE id = ?"
                cursor.execute(sql, (now, movie_id))
            conn.commit()
            logging.info(f"Movie {movie_id} marked as watched")
            self.record_lifecycle_event(
                movie_id,
                "jellyfin_watched",
                jellyfin_status="watched",
                reason="Jellyfin reported watched/playback completed",
            )
        except Exception as e:
            logging.error("Error marking movie %s as watched: %s", display_value_for_log(movie_id), type(e).__name__)
        finally:
            self._release_connection(conn)

    def get_followed_actresses(self) -> List[Dict]:
        """Return all followed actresses."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "SELECT * FROM actresses WHERE is_followed = 1" if not self.is_postgres else "SELECT * FROM actresses WHERE is_followed = TRUE"
            cursor.execute(sql)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            self._release_connection(conn)

    # ------------------- Cache helpers -------------------
    def get_cache_full(self, key: str, track: bool = True) -> Optional[Dict]:
        """Returns both value and updated_at timestamp"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "SELECT value, updated_at FROM cache WHERE key = %s" if self.is_postgres else "SELECT value, updated_at FROM cache WHERE key = ?"
            cursor.execute(sql, (key,))
            row = cursor.fetchone()
            if row:
                value = row["value"]
                updated_at = row["updated_at"]

                # Handling PostgreSQL JSONB or string
                if isinstance(value, dict):
                    # Already a dict (PostgreSQL JSONB)
                    pass
                elif isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except (TypeError, json.JSONDecodeError) as exc:
                        logging.debug("Cache value is not JSON for key %s: %s", display_value_for_log(key), type(exc).__name__)
                        delete_sql = "DELETE FROM cache WHERE key = %s" if self.is_postgres else "DELETE FROM cache WHERE key = ?"
                        cursor.execute(delete_sql, (key,))
                        conn.commit()
                        if track:
                            self.record_cache_event(key, "corrupt")
                            self.record_cache_event(key, "miss")
                        return None

                if track:
                    self.record_cache_event(key, "hit")
                return {"value": value, "updated_at": updated_at}
            if track:
                self.record_cache_event(key, "miss")
            return None
        finally:
            self._release_connection(conn)

    def get_cache(self, key: str) -> Optional[Dict]:
        res = self.get_cache_full(key)
        return res["value"] if res else None

    def set_cache(self, key: str, value: Dict):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "INSERT INTO cache (key, value, updated_at) VALUES (%s, %s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at" if self.is_postgres else "INSERT OR REPLACE INTO cache (key, value, updated_at) VALUES (?, ?, ?)"
            cursor.execute(
                sql,
                (key, json.dumps(value, ensure_ascii=False), time.time()),
            )
            conn.commit()
            self.record_cache_event(key, "write")
        finally:
            self._release_connection(conn)

    def _cache_category_for_key(self, key: str) -> str:
        normalized = str(key or "").lower()
        if normalized.startswith((
            "discovery",
            "jable_hot",
            "javdb_hot",
            "airav_hot",
            "movies_list_",
            "netflav_actress_list_",
            "netflav_actress_search_",
        )):
            return "discovery_feeds"
        if normalized.startswith(("movie_detail", "detail_", "detail:", "netflav_actress_detail_")):
            return "movie_details"
        if normalized.startswith(("navigation", "genres", "genre_")):
            return "navigation"
        return "other"

    def record_cache_event(
        self,
        key: str,
        event: str,
        count: int = 1,
        category: Optional[str] = None,
    ) -> None:
        if count <= 0:
            return
        cache_category = category or self._cache_category_for_key(key)
        with self._cache_metrics_lock:
            bucket = self._cache_metrics.setdefault(cache_category, {})
            bucket[event] = bucket.get(event, 0) + count

    def get_cache_metrics(self) -> Dict[str, Any]:
        with self._cache_metrics_lock:
            categories = {key: dict(value) for key, value in self._cache_metrics.items()}

        totals: Dict[str, int] = {}
        for bucket in categories.values():
            for event, count in bucket.items():
                totals[event] = totals.get(event, 0) + count

        def with_hit_rate(bucket: Dict[str, int]) -> Dict[str, Any]:
            reads = bucket.get("hit", 0) + bucket.get("miss", 0)
            return {
                **bucket,
                "hit_rate": round(bucket.get("hit", 0) / reads, 4) if reads else None,
            }

        return {
            "totals": with_hit_rate(totals),
            "categories": {
                key: with_hit_rate(bucket)
                for key, bucket in categories.items()
            },
        }

    def get_structured_cache_stats(self) -> Dict[str, Any]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT key FROM cache")
            rows = cursor.fetchall()
            categories = {
                "discovery_feeds": 0,
                "movie_details": 0,
                "navigation": 0,
                "other": 0,
            }
            for row in rows:
                category = self._cache_category_for_key(row["key"])
                categories[category] += 1

            cursor.execute("SELECT COUNT(*) AS count FROM translations")
            translation_row = cursor.fetchone()
            translation_count = int(translation_row["count"] or 0) if translation_row else 0
            return {
                "total_entries": len(rows) + translation_count,
                "categories": {
                    **{key: {"count": count} for key, count in categories.items()},
                    "translations": {"count": translation_count},
                },
                "metrics": self.get_cache_metrics(),
            }
        finally:
            self._release_connection(conn)

    def invalidate_cache_category(self, category: str) -> Dict[str, int]:
        allowed_categories = {
            "discovery_feeds",
            "movie_details",
            "navigation",
            "translations",
            "other",
            "all",
        }
        if category not in allowed_categories:
            raise ValueError("unsupported_cache_category")

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            deleted_cache_entries = 0
            deleted_translations = 0

            if category == "all":
                cursor.execute("DELETE FROM cache")
                deleted_cache_entries = max(cursor.rowcount, 0)
                cursor.execute("DELETE FROM translations")
                deleted_translations = max(cursor.rowcount, 0)
            elif category == "translations":
                cursor.execute("DELETE FROM translations")
                deleted_translations = max(cursor.rowcount, 0)
            else:
                cursor.execute("SELECT key FROM cache")
                keys = [
                    str(row["key"])
                    for row in cursor.fetchall()
                    if self._cache_category_for_key(row["key"]) == category
                ]
                if keys:
                    placeholder = "%s" if self.is_postgres else "?"
                    cursor.executemany(
                        f"DELETE FROM cache WHERE key = {placeholder}",
                        [(key,) for key in keys],
                    )
                    deleted_cache_entries = len(keys)

            conn.commit()
            return {
                "cache_entries": deleted_cache_entries,
                "translations": deleted_translations,
            }
        finally:
            self._release_connection(conn)

    # ------------------- User helpers -------------------
    def has_any_user(self) -> bool:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM users LIMIT 1")
            return cursor.fetchone() is not None
        finally:
            self._release_connection(conn)

    def create_user(self, username: str, password_hash: str, must_change_password: bool = False) -> bool:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = time.time()
            if self.is_postgres:
                cursor.execute(
                    """
                    INSERT INTO users (username, password_hash, created_at, must_change_password)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (username) DO NOTHING
                    """,
                    (username, password_hash, now, must_change_password),
                )
            else:
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO users (username, password_hash, created_at, must_change_password)
                    VALUES (?, ?, ?, ?)
                    """,
                    (username, password_hash, now, 1 if must_change_password else 0),
                )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            self._release_connection(conn)

    def get_user_password_hash(self, username: str) -> Optional[str]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "SELECT password_hash FROM users WHERE username = %s" if self.is_postgres else "SELECT password_hash FROM users WHERE username = ?"
            cursor.execute(sql, (username,))
            row = cursor.fetchone()
            return row["password_hash"] if row else None
        finally:
            self._release_connection(conn)

    def update_user_password(self, username: str, password_hash: str, must_change_password: bool = False) -> bool:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            if self.is_postgres:
                cursor.execute(
                    "UPDATE users SET password_hash = %s, must_change_password = %s WHERE username = %s",
                    (password_hash, must_change_password, username),
                )
            else:
                cursor.execute(
                    "UPDATE users SET password_hash = ?, must_change_password = ? WHERE username = ?",
                    (password_hash, 1 if must_change_password else 0, username),
                )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            self._release_connection(conn)

    def is_managed_file_path(self, file_path: str) -> bool:
        if not file_path:
            return False
        try:
            target = Path(file_path).expanduser().resolve()
        except Exception:
            return False
        return any(is_path_under_root(target, root) for root in get_managed_media_roots())

    # ------------------- Subscription helpers -------------------
    def get_subscriptions(self) -> List[Dict]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM subscriptions ORDER BY created_at DESC")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            self._release_connection(conn)

    def get_movie_actresses(self, movie_id: str) -> List[Dict]:
        """某部片关联的演员（id + 当时记录的名字），用于按片反查演员。"""
        movie_id = str(movie_id or "").strip()
        if not movie_id:
            return []
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            if self.is_postgres:
                cursor.execute(
                    "SELECT actress_id, actress_name FROM movie_actresses WHERE movie_id = %s",
                    (movie_id,),
                )
            else:
                cursor.execute(
                    "SELECT actress_id, actress_name FROM movie_actresses WHERE movie_id = ?",
                    (movie_id,),
                )
            rows = cursor.fetchall()
            return [dict(r) for r in rows if (r.get("actress_id") or r.get("actress_name"))]
        except Exception as e:
            logging.error("Failed to read movie actresses: %s", type(e).__name__)
            return []
        finally:
            self._release_connection(conn)

    def add_subscription(self, url: str, name: str, category_type: str, start_date: str, exclude_tags: str = ""):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = (
                "INSERT INTO subscriptions (url, name, category_type, start_date, exclude_tags, last_checked_at, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (url) DO UPDATE SET name = EXCLUDED.name, category_type = EXCLUDED.category_type, start_date = EXCLUDED.start_date, exclude_tags = EXCLUDED.exclude_tags"
                if self.is_postgres
                else """
                INSERT INTO subscriptions (url, name, category_type, start_date, exclude_tags, last_checked_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    name = excluded.name,
                    category_type = excluded.category_type,
                    start_date = excluded.start_date,
                    exclude_tags = excluded.exclude_tags
                """
            )
            cursor.execute(
                sql,
                (url, name, category_type, start_date, exclude_tags, 0, time.time()),
            )
            conn.commit()
            return True
        except Exception as e:
            logging.error("Failed to add subscription: %s", type(e).__name__)
            return False
        finally:
            self._release_connection(conn)

    def delete_subscription(self, url: str):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "DELETE FROM subscriptions WHERE url = %s" if self.is_postgres else "DELETE FROM subscriptions WHERE url = ?"
            cursor.execute(sql, (url,))
            conn.commit()
        finally:
            self._release_connection(conn)

    def update_subscription_check_time(self, url: str):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "UPDATE subscriptions SET last_checked_at = %s WHERE url = %s" if self.is_postgres else "UPDATE subscriptions SET last_checked_at = ? WHERE url = ?"
            cursor.execute(sql, (time.time(), url))
            conn.commit()
        finally:
            self._release_connection(conn)



    # ------------------- Download helpers -------------------
    def is_movie_downloaded(self, movie_id: str) -> bool:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "SELECT 1 FROM downloads WHERE movie_id = %s" if self.is_postgres else "SELECT 1 FROM downloads WHERE movie_id = ?"
            cursor.execute(sql, (movie_id,))
            return cursor.fetchone() is not None
        finally:
            self._release_connection(conn)

    def get_downloaded_movie_ids(self, movie_ids: List[str]) -> set:
        if not movie_ids:
            return set()
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholders = ",".join(["%s" if self.is_postgres else "?"] * len(movie_ids))
            sql = f"SELECT movie_id FROM downloads WHERE movie_id IN ({placeholders})"
            cursor.execute(sql, tuple(movie_ids))
            rows = cursor.fetchall()
            return {row["movie_id"] if isinstance(row, dict) else row[0] for row in rows}
        finally:
            self._release_connection(conn)

    def record_download(self, movie_id: str):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "INSERT INTO downloads (movie_id, downloaded_at) VALUES (%s, %s) ON CONFLICT (movie_id) DO UPDATE SET downloaded_at = EXCLUDED.downloaded_at" if self.is_postgres else "INSERT OR REPLACE INTO downloads (movie_id, downloaded_at) VALUES (?, ?)"
            cursor.execute(sql, (movie_id, time.time()))
            conn.commit()
            self.record_lifecycle_event(
                movie_id,
                "download_recorded",
                download_status="downloaded",
                reason="Local file exists or download was confirmed",
            )
        finally:
            self._release_connection(conn)

    # ------------------- Translation cache -------------------
    def get_translation(self, text: str) -> Optional[str]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "SELECT translated_text FROM translations WHERE original_text = %s" if self.is_postgres else "SELECT translated_text FROM translations WHERE original_text = ?"
            cursor.execute(sql, (text,))
            row = cursor.fetchone()
            translated = row["translated_text"] if row else None
            if translated and is_bad_translation_text(translated):
                logger.warning("忽略异常翻译缓存: original=%s translated=%s", text[:80], translated[:80])
                translated = None
            self.record_cache_event("translations", "hit" if translated else "miss", category="translations")
            return translated
        finally:
            self._release_connection(conn)

    def save_translation(self, original: str, translated: str):
        translated = strip_translation_control_tokens(translated)
        if not original or not translated or is_bad_translation_text(translated):
            logger.warning("跳过异常翻译缓存写入: original=%s translated=%s", str(original or "")[:80], str(translated or "")[:80])
            return

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "INSERT INTO translations (original_text, translated_text, updated_at) VALUES (%s, %s, %s) ON CONFLICT (original_text) DO UPDATE SET translated_text = EXCLUDED.translated_text, updated_at = EXCLUDED.updated_at" if self.is_postgres else "INSERT OR REPLACE INTO translations (original_text, translated_text, updated_at) VALUES (?, ?, ?)"
            cursor.execute(sql, (original, translated, time.time()))
            conn.commit()
            self.record_cache_event("translations", "write", category="translations")
        finally:
            self._release_connection(conn)

    def save_translations_batch(self, translations: Dict[str, str]):
        """Persist translation results in one transaction."""
        rows = [
            (original, cleaned, time.time())
            for original, cleaned in (
                (o, strip_translation_control_tokens(t)) for o, t in translations.items()
            )
            if original and cleaned and not is_bad_translation_text(cleaned)
        ]
        skipped = max(len(translations) - len(rows), 0)
        if skipped:
            logger.warning("跳过 %s 条异常或空翻译缓存写入", skipped)
        if not rows:
            return

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "INSERT INTO translations (original_text, translated_text, updated_at) VALUES (%s, %s, %s) ON CONFLICT (original_text) DO UPDATE SET translated_text = EXCLUDED.translated_text, updated_at = EXCLUDED.updated_at" if self.is_postgres else "INSERT OR REPLACE INTO translations (original_text, translated_text, updated_at) VALUES (?, ?, ?)"
            cursor.executemany(sql, rows)
            conn.commit()
            self.record_cache_event("translations", "write", count=len(rows), category="translations")
        finally:
            self._release_connection(conn)

    def get_translations_batch(self, texts: List[str]) -> Dict[str, str]:
        """Batch read translation cache and return {original: translated}."""
        if not texts:
            return {}

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            result = {}
            rejected = 0
            unique_texts = list(dict.fromkeys(texts))
            chunk_size = 900
            for i in range(0, len(unique_texts), chunk_size):
                chunk = unique_texts[i:i + chunk_size]
                placeholders = self._placeholders(len(chunk))
                sql = f"SELECT original_text, translated_text FROM translations WHERE original_text IN ({placeholders})"
                cursor.execute(sql, tuple(chunk))
                rows = cursor.fetchall()
                for row in rows:
                    translated = row["translated_text"]
                    if is_bad_translation_text(translated):
                        rejected += 1
                        continue
                    result[row["original_text"]] = translated
            if rejected:
                logger.warning("忽略 %s 条异常翻译缓存", rejected)
            self.record_cache_event("translations", "hit", count=len(result), category="translations")
            self.record_cache_event(
                "translations",
                "miss",
                count=max(len(unique_texts) - len(result), 0),
                category="translations",
            )
            return result
        finally:
            self._release_connection(conn)

    def get_translation_backfill_batch(self, cursor_date: str, limit: int) -> List[Dict]:
        """
        取一批仍未翻译的标题，供历史积压回填使用。

        按**发行日期从新到旧**取，新片先翻；条件用 release_date <= cursor_date，
        保证游标一次只推进一天、不跳条。返回项含 release_date，调用方据此推进游标。
        """
        if not limit or limit <= 0:
            return []
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            # 用字符串拼接而不是 str.format：SQL 里的正则含 {4}，会被 format 当占位符
            placeholder = "%s" if self.is_postgres else "?"
            sql = (
                "SELECT m.original_title AS original_title, m.release_date AS release_date "
                "FROM movies m "
                "WHERE m.release_date IS NOT NULL AND m.release_date <> '' "
                "AND m.release_date ~ '^[0-9]{4}' AND m.release_date <= " + placeholder + " "
                "AND m.original_title ~ '[ぁ-んァ-ヶ]' "
                "AND NOT EXISTS (SELECT 1 FROM translations t WHERE t.original_text = m.original_title) "
                "ORDER BY m.release_date DESC LIMIT " + placeholder
            )
            cursor.execute(sql, (cursor_date, limit))
            rows = cursor.fetchall()
            return [
                {"original_title": row["original_title"], "release_date": row["release_date"]}
                for row in rows
                if row["original_title"]
            ]
        finally:
            self._release_connection(conn)

    def count_translations(self, recent_seconds: int = 600) -> Dict[str, int]:
        """翻译条数总量与近 N 分钟增量（translations 表很小，这俩查询很便宜）。"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholder = "%s" if self.is_postgres else "?"
            cursor.execute("SELECT count(*) AS c FROM translations")
            row = cursor.fetchone()
            total = int((row["c"] if row else 0) or 0)
            cursor.execute(
                f"SELECT count(*) AS c FROM translations WHERE updated_at > {placeholder}",
                (time.time() - max(int(recent_seconds), 1),),
            )
            row = cursor.fetchone()
            recent = int((row["c"] if row else 0) or 0)
            return {"total": total, "recent": recent}
        finally:
            self._release_connection(conn)

    def get_translation_progress(self, by_year_limit: int = 6, recent_seconds: int = 600) -> Dict:
        """
        翻译进度总览：已翻、待翻（按标题去重）、近 N 分钟速率、待翻按发行年份分布。
        整个查询是 1.4M 行的一次扫描（约 2~4 秒），调用方自行做短期缓存。
        """
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholder = "%s" if self.is_postgres else "?"

            cursor.execute("SELECT count(*) AS c FROM translations")
            row = cursor.fetchone()
            translated_total = int((row["c"] if row else 0) or 0)

            cursor.execute(
                f"SELECT count(*) AS c FROM translations WHERE updated_at > {placeholder}",
                (time.time() - max(int(recent_seconds), 1),),
            )
            row = cursor.fetchone()
            recent_total = int((row["c"] if row else 0) or 0)

            # 先按标题去重（每个标题取最新发行年份），再按年份分组：
            # 直接对 movies 分组会把同一标题在不同年份重复计数，总数会虚高
            cursor.execute(
                "WITH pending AS ("
                "  SELECT m.original_title AS title, max(substring(m.release_date from 1 for 4)) AS year"
                "    FROM movies m"
                "   WHERE m.original_title ~ '[ぁ-んァ-ヶ]'"
                "     AND NOT EXISTS (SELECT 1 FROM translations t WHERE t.original_text = m.original_title)"
                "   GROUP BY m.original_title"
                ") "
                "SELECT COALESCE(year, '未知') AS year, count(*) AS pending FROM pending GROUP BY 1"
            )
            by_year = []
            pending_total = 0
            for result_row in cursor.fetchall():
                year = str(result_row["year"] or "未知")
                pending = int(result_row["pending"] or 0)
                pending_total += pending
                by_year.append({"year": year, "pending": pending})
            by_year.sort(key=lambda item: item["year"], reverse=True)

            rate_per_min = round(recent_total / (max(int(recent_seconds), 1) / 60.0), 1)
            return {
                "translated_total": translated_total,
                "pending_total": pending_total,
                "rate_per_min": rate_per_min,
                "recent_seconds": int(recent_seconds),
                "recent_total": recent_total,
                "by_year": by_year[: max(int(by_year_limit), 1)],
            }
        finally:
            self._release_connection(conn)

    # ------------------- Config persistence -------------------
    def get_config(self, key: str) -> Optional[Dict]:
        # 配置读取极其频繁（每个页面加载都要读几十个键），而配置几乎不变。
        # 单次 get_config 在 sqlite 下要 1.5 秒（打开 3GB 库文件），
        # 累积起来会让每个页面都卡数秒 —— 实测引导 status 接口稳定 5 秒。
        # 这里加一个很短的进程内缓存，写入时立即失效。
        cached = self._config_cache.get(key)
        now = time.time()
        if cached is not None and (now - cached[0]) < CONFIG_CACHE_TTL_SECONDS:
            return cached[1]

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "SELECT value FROM config WHERE key = %s" if self.is_postgres else "SELECT value FROM config WHERE key = ?"
            cursor.execute(sql, (key,))
            row = cursor.fetchone()
            if not row:
                self._config_cache[key] = (time.time(), None)
                return None
            val = row["value"] if self.is_postgres else row["value"]
            try:
                parsed = json.loads(val)
            except Exception:
                parsed = val

            if _is_sensitive_config_key(key) and isinstance(parsed, str):
                decrypted = _decrypt_config_value(parsed)
                if decrypted != parsed:
                    try:
                        parsed = json.loads(decrypted)
                    except Exception:
                        parsed = decrypted

            if isinstance(parsed, str):
                parsed = parsed.strip('"\'')
            self._config_cache[key] = (time.time(), parsed)
            return parsed
        finally:
            self._release_connection(conn)

    def get_all_config(self) -> Dict[str, Any]:
        """一次性把 config 表全读进内存并返回解密后的字典。

        需要读几十个配置键时必须走这个方法（引导页的 current_settings 等）：
        逐键 get_config 在 sqlite 下每次要 1.5 秒，40 个键就是 5 秒。
        """
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "SELECT key, value FROM config" if self.is_postgres else "SELECT key, value FROM config"
            cursor.execute(sql)
            result: Dict[str, Any] = {}
            now = time.time()
            for row in cursor.fetchall():
                key = row["key"]
                val = row["value"]
                try:
                    parsed = json.loads(val)
                except Exception:
                    parsed = val
                if _is_sensitive_config_key(key) and isinstance(parsed, str):
                    decrypted = _decrypt_config_value(parsed)
                    if decrypted != parsed:
                        try:
                            parsed = json.loads(decrypted)
                        except Exception:
                            parsed = decrypted
                if isinstance(parsed, str):
                    parsed = parsed.strip('"\'')
                result[key] = parsed
                self._config_cache[key] = (now, parsed)
            return result
        finally:
            self._release_connection(conn)

    def set_config(self, key: str, value):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            stored_value = _encrypt_config_value(value) if _is_sensitive_config_key(key) else value
            val_str = json.dumps(stored_value, ensure_ascii=False)
            sql = "INSERT INTO config (key, value, updated_at) VALUES (%s, %s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at" if self.is_postgres else "INSERT OR REPLACE INTO config (key, value, updated_at) VALUES (?, ?, ?)"
            cursor.execute(sql, (key, val_str, time.time()))
            conn.commit()
            # 立即失效缓存，否则改了配置要等 TTL 过期才生效（保存后界面不刷新）
            self._config_cache.pop(key, None)
        finally:
            self._release_connection(conn)

    def plaintext_sensitive_config_keys(self) -> List[str]:
        """列出当前以**明文**躺在 config 表里的敏感键。

        加密只在写入时发生，所以两种情况下会留明文：① 从没配过
        `BUS_TERMINAL_CONFIG_SECRET`；② 密钥是后补的，之前写入的旧值没重存。
        启动时点一次名，避免"以为配了密钥就安全了"（见 KI-044 / KI-048）。
        """
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM config")
            offenders: List[str] = []
            for row in cursor.fetchall():
                key = row["key"] if self.is_postgres else row[0]
                raw = row["value"] if self.is_postgres else row[1]
                if not _is_sensitive_config_key(key) or _is_encrypted_config_value(raw):
                    continue
                offenders.append(str(key).strip().lower())
            return sorted(offenders)
        finally:
            self._release_connection(conn)

    # ------------------- Movie lifecycle state -------------------
    def record_lifecycle_event(
        self,
        movie_id: str,
        event_type: str,
        *,
        title: str = None,
        discovery_status: str = None,
        decision_status: str = None,
        download_status: str = None,
        organize_status: str = None,
        jellyfin_status: str = None,
        cleanup_status: str = None,
        source: str = None,
        reason: str = None,
        execution_result: str = None,
        error_summary: str = None,
        payload: Dict = None,
    ) -> bool:
        """Record a lifecycle event and update the latest per-movie state."""
        if not movie_id:
            return False

        event_id = str(uuid.uuid4())
        now = time.time()
        payload_text = json.dumps(sanitize_lifecycle_payload(payload or {}), ensure_ascii=False)
        status_values = {
            "discovery_status": discovery_status,
            "decision_status": decision_status,
            "download_status": download_status,
            "organize_status": organize_status,
            "jellyfin_status": jellyfin_status,
            "cleanup_status": cleanup_status,
        }
        status_values = {
            key: (str(value).strip() if value is not None and str(value).strip() else None)
            for key, value in status_values.items()
        }
        status_defaults = {
            "discovery_status": "unknown",
            "decision_status": "pending",
            "download_status": "unknown",
            "organize_status": "pending",
            "jellyfin_status": "unknown",
            "cleanup_status": "none",
        }
        safe_source = redact_log_message(source)[:500] if source else None
        safe_reason = redact_log_message(reason)[:500] if reason else None
        safe_error = redact_log_message(error_summary)[:240] if error_summary else None
        safe_result = str(execution_result or ("failed" if safe_error else "recorded"))[:64]
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholder = "%s" if self.is_postgres else "?"
            cursor.execute(
                f"SELECT {', '.join(status_values)} FROM movie_lifecycle WHERE movie_id = {placeholder}",
                (movie_id,),
            )
            existing_row = cursor.fetchone()
            existing = dict(existing_row) if existing_row else {}
            changed = {key: value for key, value in status_values.items() if value is not None}
            if len(changed) == 1:
                status_dimension, new_status = next(iter(changed.items()))
                old_status = existing.get(status_dimension) or status_defaults[status_dimension]
            elif changed:
                status_dimension = ",".join(changed)
                old_status = json.dumps(
                    {
                        key: existing.get(key) or status_defaults[key]
                        for key in changed
                    },
                    ensure_ascii=False,
                )
                new_status = json.dumps(changed, ensure_ascii=False)
            else:
                status_dimension = None
                old_status = None
                new_status = None

            event_columns = (
                "id", "movie_id", "event_type",
                "discovery_status", "decision_status", "download_status",
                "organize_status", "jellyfin_status", "cleanup_status",
                "status_dimension", "old_status", "new_status", "source", "reason",
                "execution_result", "error_summary", "payload", "created_at",
            )
            event_values = (
                event_id,
                movie_id,
                event_type,
                status_values["discovery_status"],
                status_values["decision_status"],
                status_values["download_status"],
                status_values["organize_status"],
                status_values["jellyfin_status"],
                status_values["cleanup_status"],
                status_dimension,
                old_status,
                new_status,
                safe_source,
                safe_reason,
                safe_result,
                safe_error,
                payload_text,
                now,
            )
            cursor.execute(
                f"INSERT INTO movie_lifecycle_events ({', '.join(event_columns)}) "
                f"VALUES ({', '.join([placeholder] * len(event_columns))})",
                event_values,
            )

            lifecycle_columns = (
                "movie_id", "title", *status_values.keys(), "source",
                "last_event", "last_reason", "created_at", "updated_at",
            )
            lifecycle_values = (
                movie_id,
                title,
                *(status_values[key] or status_defaults[key] for key in status_values),
                safe_source,
                event_type,
                safe_reason,
                now,
                now,
            )
            status_updates = ", ".join(
                f"{key} = COALESCE({placeholder}, movie_lifecycle.{key})"
                for key in status_values
            )
            cursor.execute(
                f"""
                INSERT INTO movie_lifecycle ({', '.join(lifecycle_columns)})
                VALUES ({', '.join([placeholder] * len(lifecycle_columns))})
                ON CONFLICT (movie_id) DO UPDATE SET
                    title = COALESCE(excluded.title, movie_lifecycle.title),
                    {status_updates},
                    source = COALESCE(excluded.source, movie_lifecycle.source),
                    last_event = excluded.last_event,
                    last_reason = excluded.last_reason,
                    updated_at = excluded.updated_at
                """,
                (*lifecycle_values, *status_values.values()),
            )
            conn.commit()
            return True
        except Exception as e:
            logging.error(
                "Failed to record lifecycle event %s/%s: %s",
                display_value_for_log(movie_id),
                display_value_for_log(event_type),
                type(e).__name__,
            )
            return False
        finally:
            self._release_connection(conn)

    def get_movie_lifecycle(
        self,
        movie_id: str = None,
        *,
        discovery_status: str = None,
        decision_status: str = None,
        download_status: str = None,
        organize_status: str = None,
        jellyfin_status: str = None,
        cleanup_status: str = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            clauses = []
            params = []
            placeholder = "%s" if self.is_postgres else "?"

            for column, value in (
                ("movie_id", movie_id),
                ("discovery_status", discovery_status),
                ("decision_status", decision_status),
                ("download_status", download_status),
                ("organize_status", organize_status),
                ("jellyfin_status", jellyfin_status),
                ("cleanup_status", cleanup_status),
            ):
                if value:
                    clauses.append(f"{column} = {placeholder}")
                    params.append(value)

            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            sql = f"SELECT * FROM movie_lifecycle {where} ORDER BY updated_at DESC LIMIT {placeholder} OFFSET {placeholder}"
            cursor.execute(sql, (*params, limit, offset))
            return [dict(row) for row in cursor.fetchall()]
        finally:
            self._release_connection(conn)

    def get_movie_lifecycle_events(self, movie_id: str, limit: int = 100) -> List[Dict]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = (
                "SELECT * FROM movie_lifecycle_events WHERE movie_id = %s ORDER BY created_at DESC LIMIT %s"
                if self.is_postgres
                else "SELECT * FROM movie_lifecycle_events WHERE movie_id = ? ORDER BY created_at DESC LIMIT ?"
            )
            cursor.execute(sql, (movie_id, limit))
            rows = []
            for row in cursor.fetchall():
                data = dict(row)
                if data.get("payload"):
                    try:
                        data["payload"] = json.loads(data["payload"])
                    except Exception:
                        pass
                rows.append(data)
            return rows
        finally:
            self._release_connection(conn)

    # ------------------- Persistent external task queue -------------------
    @staticmethod
    def _decode_external_task(row: Any) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        task = dict(row)
        for field in ("payload", "result"):
            if task.get(field) and isinstance(task[field], str):
                try:
                    task[field] = json.loads(task[field])
                except Exception:
                    task[field] = {}
        return task

    def enqueue_external_task(
        self,
        task_type: str,
        dedupe_key: str,
        payload: Optional[Dict[str, Any]] = None,
        source: Optional[str] = None,
        max_retries: int = 20,
    ) -> Dict[str, Any]:
        task_type = str(task_type or "").strip()
        dedupe_key = str(dedupe_key or "").strip()
        if not task_type or not dedupe_key:
            raise ValueError("external_task_identity_required")

        task_id = str(uuid.uuid4())
        now = time.time()
        payload_text = json.dumps(sanitize_lifecycle_payload(payload or {}), ensure_ascii=False)
        retry_limit = max(1, min(int(max_retries or 20), 1000))
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholders = "%s" if self.is_postgres else "?"
            sql = f"""
                INSERT INTO external_tasks (
                    id, task_type, dedupe_key, payload, source, status,
                    retry_count, max_retries, next_retry_at, created_at, updated_at
                ) VALUES (
                    {', '.join([placeholders] * 8)}, NULL, {placeholders}, {placeholders}
                )
                ON CONFLICT (task_type, dedupe_key) DO NOTHING
            """
            cursor.execute(
                sql,
                (
                    task_id,
                    task_type,
                    dedupe_key,
                    payload_text,
                    source,
                    "pending",
                    0,
                    retry_limit,
                    now,
                    now,
                ),
            )
            cursor.execute(
                f"SELECT * FROM external_tasks WHERE task_type = {placeholders} AND dedupe_key = {placeholders}",
                (task_type, dedupe_key),
            )
            row = cursor.fetchone()
            conn.commit()
            return self._decode_external_task(row) or {"id": task_id}
        finally:
            self._release_connection(conn)

    def get_external_task(self, task_type: str, dedupe_key: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholder = "%s" if self.is_postgres else "?"
            cursor.execute(
                f"SELECT * FROM external_tasks WHERE task_type = {placeholder} AND dedupe_key = {placeholder}",
                (task_type, dedupe_key),
            )
            return self._decode_external_task(cursor.fetchone())
        finally:
            self._release_connection(conn)

    def get_external_tasks(
        self,
        status: Optional[str] = None,
        task_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholder = "%s" if self.is_postgres else "?"
            clauses = []
            params: List[Any] = []
            if status:
                clauses.append(f"status = {placeholder}")
                params.append(status)
            if task_type:
                clauses.append(f"task_type = {placeholder}")
                params.append(task_type)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            cursor.execute(
                f"""
                SELECT * FROM external_tasks
                {where}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT {placeholder} OFFSET {placeholder}
                """,
                (*params, max(1, min(int(limit or 100), 500)), max(0, int(offset or 0))),
            )
            return [
                task
                for row in cursor.fetchall()
                if (task := self._decode_external_task(row)) is not None
            ]
        finally:
            self._release_connection(conn)

    def claim_due_external_tasks(
        self,
        task_type: Optional[str] = None,
        limit: int = 20,
        lease_seconds: int = 300,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        current_time = float(now if now is not None else time.time())
        lease_until = current_time + max(30, min(int(lease_seconds or 300), 3600))
        safe_limit = max(1, min(int(limit or 20), 100))
        conn = self.get_connection()
        claimed = []
        try:
            cursor = conn.cursor()
            placeholder = "%s" if self.is_postgres else "?"
            clauses = [
                f"((status IN ('pending', 'waiting_external_service') AND COALESCE(next_retry_at, 0) <= {placeholder})"
                f" OR (status = 'processing' AND COALESCE(next_retry_at, 0) <= {placeholder}))"
            ]
            params: List[Any] = [current_time, current_time]
            if task_type:
                clauses.append(f"task_type = {placeholder}")
                params.append(task_type)
            cursor.execute(
                f"""
                SELECT id FROM external_tasks
                WHERE {' AND '.join(clauses)}
                ORDER BY COALESCE(next_retry_at, created_at) ASC, created_at ASC
                LIMIT {placeholder}
                """,
                (*params, safe_limit),
            )
            candidate_ids = [row["id"] for row in cursor.fetchall()]
            for task_id in candidate_ids:
                cursor.execute(
                    f"""
                    UPDATE external_tasks
                    SET status = 'processing', next_retry_at = {placeholder}, updated_at = {placeholder}
                    WHERE id = {placeholder}
                      AND ((status IN ('pending', 'waiting_external_service') AND COALESCE(next_retry_at, 0) <= {placeholder})
                           OR (status = 'processing' AND COALESCE(next_retry_at, 0) <= {placeholder}))
                    """,
                    (lease_until, current_time, task_id, current_time, current_time),
                )
                if cursor.rowcount != 1:
                    continue
                cursor.execute(f"SELECT * FROM external_tasks WHERE id = {placeholder}", (task_id,))
                task = self._decode_external_task(cursor.fetchone())
                if task:
                    claimed.append(task)
            conn.commit()
            return claimed
        finally:
            self._release_connection(conn)

    def defer_external_task(
        self,
        task_id: str,
        error: str,
        base_delay_seconds: int = 60,
        max_delay_seconds: int = 3600,
    ) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholder = "%s" if self.is_postgres else "?"
            cursor.execute(f"SELECT * FROM external_tasks WHERE id = {placeholder}", (task_id,))
            existing = self._decode_external_task(cursor.fetchone())
            if not existing or existing.get("status") == "completed":
                return existing

            previous_count = int(existing.get("retry_count") or 0)
            retry_count = previous_count + 1
            max_retries = int(existing.get("max_retries") or 20)
            now = time.time()
            exhausted = retry_count >= max_retries
            status = "needs_attention" if exhausted else "waiting_external_service"
            delay = min(
                max(int(base_delay_seconds or 60), 1) * (2 ** min(previous_count, 10)),
                max(int(max_delay_seconds or 3600), 1),
            )
            next_retry_at = None if exhausted else now + delay
            first_error_at = existing.get("first_error_at") or now
            cursor.execute(
                f"""
                UPDATE external_tasks
                SET status = {placeholder}, retry_count = {placeholder}, first_error_at = {placeholder},
                    last_error_at = {placeholder}, last_error = {placeholder}, next_retry_at = {placeholder},
                    updated_at = {placeholder}
                WHERE id = {placeholder}
                """,
                (
                    status,
                    retry_count,
                    first_error_at,
                    now,
                    str(error or "external_service_error")[:120],
                    next_retry_at,
                    now,
                    task_id,
                ),
            )
            conn.commit()
            cursor.execute(f"SELECT * FROM external_tasks WHERE id = {placeholder}", (task_id,))
            return self._decode_external_task(cursor.fetchone())
        finally:
            self._release_connection(conn)

    def complete_external_task(
        self,
        task_id: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        now = time.time()
        result_text = json.dumps(sanitize_lifecycle_payload(result or {}), ensure_ascii=False)
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholder = "%s" if self.is_postgres else "?"
            cursor.execute(
                f"""
                UPDATE external_tasks
                SET status = 'completed', result = {placeholder}, next_retry_at = NULL,
                    completed_at = {placeholder}, updated_at = {placeholder}
                WHERE id = {placeholder}
                """,
                (result_text, now, now, task_id),
            )
            conn.commit()
            cursor.execute(f"SELECT * FROM external_tasks WHERE id = {placeholder}", (task_id,))
            return self._decode_external_task(cursor.fetchone())
        finally:
            self._release_connection(conn)

    def get_external_task_stats(self) -> Dict[str, Any]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT status, COUNT(*) AS count FROM external_tasks GROUP BY status")
            by_status = {row["status"]: int(row["count"] or 0) for row in cursor.fetchall()}
            return {
                "by_status": by_status,
                "total": sum(by_status.values()),
                "waiting": by_status.get("waiting_external_service", 0),
                "needs_attention": by_status.get("needs_attention", 0),
            }
        finally:
            self._release_connection(conn)

    # ------------------- Automation candidate helpers -------------------
    def record_automation_event(
        self,
        candidate_id: Optional[str],
        event_type: str,
        actor: str = "system",
        reason: Optional[str] = None,
        payload: Optional[Dict] = None,
    ) -> str:
        event_id = str(uuid.uuid4())
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = time.time()
            payload_text = json.dumps(sanitize_lifecycle_payload(payload or {}), ensure_ascii=False)
            sql = (
                """
                INSERT INTO automation_events (id, candidate_id, event_type, actor, reason, payload, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """
                if self.is_postgres else
                """
                INSERT INTO automation_events (id, candidate_id, event_type, actor, reason, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """
            )
            cursor.execute(sql, (event_id, candidate_id, event_type, actor, reason, payload_text, now))
            conn.commit()
            return event_id
        finally:
            self._release_connection(conn)

    def get_automation_candidate(self, candidate_type: str, subject_id: str, source_movie_id: Optional[str]) -> Optional[Dict]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            if source_movie_id is None:
                sql = (
                    "SELECT * FROM automation_candidates WHERE candidate_type = %s AND subject_id = %s AND source_movie_id IS NULL LIMIT 1"
                    if self.is_postgres else
                    "SELECT * FROM automation_candidates WHERE candidate_type = ? AND subject_id = ? AND source_movie_id IS NULL LIMIT 1"
                )
                cursor.execute(sql, (candidate_type, subject_id))
            else:
                sql = (
                    "SELECT * FROM automation_candidates WHERE candidate_type = %s AND subject_id = %s AND source_movie_id = %s LIMIT 1"
                    if self.is_postgres else
                    "SELECT * FROM automation_candidates WHERE candidate_type = ? AND subject_id = ? AND source_movie_id = ? LIMIT 1"
                )
                cursor.execute(sql, (candidate_type, subject_id, source_movie_id))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            self._release_connection(conn)

    def get_automation_candidate_by_id(self, candidate_id: str) -> Optional[Dict]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "SELECT * FROM automation_candidates WHERE id = %s" if self.is_postgres else "SELECT * FROM automation_candidates WHERE id = ?"
            cursor.execute(sql, (candidate_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            self._release_connection(conn)

    def upsert_automation_candidate(
        self,
        candidate_type: str,
        subject_id: str,
        subject_name: str,
        source_movie_id: Optional[str],
        source_title: Optional[str],
        source: str,
        recommended_action: str,
        reason: str,
        confidence: float = 0.5,
        payload: Optional[Dict] = None,
    ) -> Dict:
        existing = self.get_automation_candidate(candidate_type, subject_id, source_movie_id)
        now = time.time()
        payload_text = json.dumps(sanitize_lifecycle_payload(payload or {}), ensure_ascii=False)

        if existing:
            conn = self.get_connection()
            try:
                cursor = conn.cursor()
                sql = (
                    """
                    UPDATE automation_candidates
                    SET subject_name = %s, source_title = %s, source = %s,
                        recommended_action = %s, reason = %s, confidence = %s,
                        payload = %s, updated_at = %s
                    WHERE id = %s
                    """
                    if self.is_postgres else
                    """
                    UPDATE automation_candidates
                    SET subject_name = ?, source_title = ?, source = ?,
                        recommended_action = ?, reason = ?, confidence = ?,
                        payload = ?, updated_at = ?
                    WHERE id = ?
                    """
                )
                cursor.execute(
                    sql,
                    (
                        subject_name,
                        source_title,
                        source,
                        recommended_action,
                        reason,
                        confidence,
                        payload_text,
                        now,
                        existing["id"],
                    ),
                )
                conn.commit()
                candidate_id = existing["id"]
            finally:
                self._release_connection(conn)
            self.record_automation_event(
                candidate_id,
                "candidate_refreshed",
                reason=reason,
                payload={"candidate_type": candidate_type, "subject_id": subject_id, "source_movie_id": source_movie_id},
            )
            return self.get_automation_candidate_by_id(candidate_id) or existing

        candidate_id = str(uuid.uuid4())
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = (
                """
                INSERT INTO automation_candidates (
                    id, candidate_type, subject_id, subject_name, source_movie_id,
                    source_title, source, recommended_action, status, confidence,
                    reason, payload, created_at, updated_at, decided_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'candidate', %s, %s, %s, %s, %s, NULL)
                """
                if self.is_postgres else
                """
                INSERT INTO automation_candidates (
                    id, candidate_type, subject_id, subject_name, source_movie_id,
                    source_title, source, recommended_action, status, confidence,
                    reason, payload, created_at, updated_at, decided_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?, ?, NULL)
                """
            )
            cursor.execute(
                sql,
                (
                    candidate_id,
                    candidate_type,
                    subject_id,
                    subject_name,
                    source_movie_id,
                    source_title,
                    source,
                    recommended_action,
                    confidence,
                    reason,
                    payload_text,
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            self._release_connection(conn)

        self.record_automation_event(
            candidate_id,
            "candidate_created",
            reason=reason,
            payload={"candidate_type": candidate_type, "subject_id": subject_id, "source_movie_id": source_movie_id},
        )
        if candidate_type == "subscription_download" and (subject_id or source_movie_id):
            self.record_lifecycle_event(
                subject_id or source_movie_id,
                "download_candidate_created",
                title=source_title or subject_name,
                decision_status="candidate",
                source=source or "subscription",
                reason=reason,
                execution_result="success",
                payload={
                    "candidate_id": candidate_id,
                    "matched_rules": (payload or {}).get("matched_rules", []),
                },
            )
        return self.get_automation_candidate_by_id(candidate_id) or {"id": candidate_id}

    def get_automation_candidates(
        self,
        status: Optional[str] = "candidate",
        candidate_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            clauses = []
            params = []
            if status:
                clauses.append("status = %s" if self.is_postgres else "status = ?")
                params.append(status)
            if candidate_type:
                clauses.append("candidate_type = %s" if self.is_postgres else "candidate_type = ?")
                params.append(candidate_type)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            placeholder = "%s" if self.is_postgres else "?"
            sql = f"SELECT * FROM automation_candidates {where} ORDER BY updated_at DESC LIMIT {placeholder} OFFSET {placeholder}"
            cursor.execute(sql, (*params, limit, offset))
            return [dict(row) for row in cursor.fetchall()]
        finally:
            self._release_connection(conn)

    def update_automation_candidate_status(
        self,
        candidate_id: str,
        status: str,
        actor: str = "system",
        reason: Optional[str] = None,
    ) -> Optional[Dict]:
        candidate = self.get_automation_candidate_by_id(candidate_id)
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = time.time()
            sql = (
                """
                UPDATE automation_candidates
                SET status = %s, updated_at = %s, decided_at = %s
                WHERE id = %s
                """
                if self.is_postgres else
                """
                UPDATE automation_candidates
                SET status = ?, updated_at = ?, decided_at = ?
                WHERE id = ?
                """
            )
            cursor.execute(sql, (status, now, now, candidate_id))
            conn.commit()
        finally:
            self._release_connection(conn)

        self.record_automation_event(candidate_id, f"candidate_{status}", actor=actor, reason=reason)
        if candidate and candidate.get("candidate_type") == "subscription_download":
            movie_id = candidate.get("subject_id") or candidate.get("source_movie_id")
            if movie_id:
                self.record_lifecycle_event(
                    movie_id,
                    f"download_candidate_{status}",
                    title=candidate.get("source_title") or candidate.get("subject_name"),
                    decision_status=status,
                    source=f"automation:{actor or 'system'}",
                    reason=reason,
                    execution_result="success",
                    payload={"candidate_id": candidate_id},
                )
        return self.get_automation_candidate_by_id(candidate_id)

    # ------------------- Download queue management -------------------
    def get_active_queue_entry(self, movie_id: str = None, magnet: str = None) -> Optional[Dict]:
        """Return an active queue row for the same movie or magnet, if one exists."""
        if not movie_id and not magnet:
            return None

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            active_statuses = ("pending", "waiting_external_service", "submitted", "processing")
            placeholder = "%s" if self.is_postgres else "?"
            placeholders = ", ".join(["%s"] * len(active_statuses)) if self.is_postgres else ", ".join(["?"] * len(active_statuses))
            clauses = []
            params = []

            if movie_id:
                clauses.append("movie_id = %s" if self.is_postgres else "movie_id = ?")
                params.append(movie_id)
            if magnet:
                clauses.append("magnet = %s" if self.is_postgres else "magnet = ?")
                params.append(magnet)

            sql = f"""
                SELECT *
                FROM download_queue
                WHERE (status IN ({placeholders}) OR (status = {placeholder} AND retry_count < {placeholder}))
                  AND ({' OR '.join(clauses)})
                ORDER BY updated_at DESC
                LIMIT 1
            """
            cursor.execute(sql, (*active_statuses, "failed", 5, *params))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            self._release_connection(conn)

    def add_to_queue(
        self,
        magnet: str,
        movie_id: str,
        title: str,
        source_url: str = None,
        save_path: str = None,
        category: str = None,
    ) -> str:
        existing = self.get_active_queue_entry(movie_id=movie_id, magnet=magnet)
        if existing:
            logging.info(
                "Skip duplicate download queue item: movie_id=%s title=%s existing_task=%s status=%s",
                movie_id,
                display_value_for_log(title),
                existing.get("id"),
                existing.get("status"),
            )
            if movie_id:
                self.record_lifecycle_event(
                    movie_id,
                    "download_queue_duplicate",
                    title=title,
                    source=source_url,
                    reason=f"Active queue item already exists: {existing.get('id')} ({existing.get('status')})",
                    payload={"existing_task_id": existing.get("id"), "status": existing.get("status")},
                )
            return existing["id"]

        task_id = str(uuid.uuid4())
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = time.time()
            if self.is_postgres:
                sql = "INSERT INTO download_queue (id, magnet, movie_id, title, status, source_url, save_path, category, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                cursor.execute(sql, (task_id, magnet, movie_id, title, 'pending', source_url, save_path, category, now, now))
            else:
                sql = "INSERT INTO download_queue (id, magnet, movie_id, title, status, source_url, save_path, category, created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)"
                cursor.execute(sql, (task_id, magnet, movie_id, title, source_url, save_path, category, now, now))
            conn.commit()
            if movie_id:
                self.record_lifecycle_event(
                    movie_id,
                    "download_queued",
                    title=title,
                    download_status="queued",
                    source=source_url,
                    payload={"task_id": task_id},
                )
            return task_id
        except Exception:
            conn.rollback()
            existing = self.get_active_queue_entry(movie_id=movie_id, magnet=magnet)
            if existing:
                return existing["id"]
            raise
        finally:
            self._release_connection(conn)

    def get_pending_downloads(self, limit: int = 10) -> List[Dict]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholder = "%s" if self.is_postgres else "?"
            sql = f"""
                SELECT * FROM download_queue
                WHERE status = 'pending'
                   OR (status = 'waiting_external_service' AND COALESCE(next_retry_at, 0) <= {placeholder})
                   OR (status = 'failed' AND retry_count < 5 AND COALESCE(next_retry_at, 0) <= {placeholder})
                ORDER BY COALESCE(next_retry_at, created_at) ASC, created_at ASC
                LIMIT {placeholder}
            """
            now = time.time()
            cursor.execute(sql, (now, now, limit))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            self._release_connection(conn)

    def defer_download_for_external_service(
        self,
        task_id: str,
        error: str,
        base_delay_seconds: int = 30,
        max_delay_seconds: int = 3600,
    ) -> Optional[Dict]:
        conn = self.get_connection()
        task = None
        previous_retry_count = 0
        try:
            cursor = conn.cursor()
            placeholder = "%s" if self.is_postgres else "?"
            cursor.execute(
                f"SELECT movie_id, title, source_url, retry_count FROM download_queue WHERE id = {placeholder}",
                (task_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            task = dict(row)
            previous_retry_count = int(task.get("retry_count") or 0)
            retry_count = previous_retry_count + 1
            delay = min(max(base_delay_seconds, 1) * (2 ** min(previous_retry_count, 10)), max_delay_seconds)
            now = time.time()
            next_retry_at = now + delay
            cursor.execute(
                f"""
                UPDATE download_queue
                SET status = {placeholder}, error_message = {placeholder}, retry_count = {placeholder},
                    next_retry_at = {placeholder}, updated_at = {placeholder}
                WHERE id = {placeholder}
                """,
                ("waiting_external_service", error, retry_count, next_retry_at, now, task_id),
            )
            conn.commit()
        finally:
            self._release_connection(conn)

        if task and task.get("movie_id") and previous_retry_count == 0:
            self.record_lifecycle_event(
                task["movie_id"],
                "download_waiting_external_service",
                title=task.get("title"),
                download_status="waiting_external_service",
                source=task.get("source_url"),
                reason=error,
                execution_result="deferred",
                error_summary=error,
                payload={"task_id": task_id, "retry_count": 1},
            )
        return self.get_active_queue_entry(movie_id=task.get("movie_id")) if task else None

    def update_download_status(self, task_id: str, status: str, error: str = None):
        conn = self.get_connection()
        task = None
        try:
            cursor = conn.cursor()
            select_sql = "SELECT movie_id, title, source_url, retry_count FROM download_queue WHERE id = %s" if self.is_postgres else "SELECT movie_id, title, source_url, retry_count FROM download_queue WHERE id = ?"
            cursor.execute(select_sql, (task_id,))
            row = cursor.fetchone()
            task = dict(row) if row else None
            now = time.time()
            if status == "failed":
                sql = "UPDATE download_queue SET status = %s, error_message = %s, retry_count = retry_count + 1, updated_at = %s WHERE id = %s" if self.is_postgres else "UPDATE download_queue SET status = ?, error_message = ?, retry_count = retry_count + 1, updated_at = ? WHERE id = ?"
                cursor.execute(sql, (status, error, now, task_id))
            else:
                sql = "UPDATE download_queue SET status = %s, error_message = %s, next_retry_at = NULL, updated_at = %s WHERE id = %s" if self.is_postgres else "UPDATE download_queue SET status = ?, error_message = ?, next_retry_at = NULL, updated_at = ? WHERE id = ?"
                cursor.execute(sql, (status, error, now, task_id))
            conn.commit()
            if task and task.get("movie_id"):
                status_map = {
                    "pending": "queued",
                    "submitted": "submitted",
                    "processing": "downloading",
                    "waiting_external_service": "waiting_external_service",
                    "failed": "failed",
                    "skipped": "skipped",
                    "duplicate_skipped": "skipped",
                }
                self.record_lifecycle_event(
                    task["movie_id"],
                    f"download_{status}",
                    title=task.get("title"),
                    download_status=status_map.get(status),
                    source=task.get("source_url"),
                    reason=(
                        f"Recovered after {int(task.get('retry_count') or 0)} retries"
                        if status == "submitted" and int(task.get("retry_count") or 0) > 0
                        else error
                    ),
                    execution_result="failed" if status == "failed" else "success",
                    error_summary=error if status == "failed" else None,
                    payload={
                        "task_id": task_id,
                        "queue_status": status,
                        "retry_count": int(task.get("retry_count") or 0),
                    },
                )
        finally:
            self._release_connection(conn)

    def is_movie_pending(self, movie_id: str) -> bool:
        """Check if a movie is currently in the active download queue."""
        return self.get_active_queue_entry(movie_id=movie_id) is not None

    def get_queue_stats(self) -> Dict:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT status, COUNT(*) as count FROM download_queue GROUP BY status")
            rows = cursor.fetchall()
            stats = {
                "pending": 0,
                "waiting_external_service": 0,
                "submitted": 0,
                "failed": 0,
                "total_managed": 0,
            }
            total = 0
            for row in rows:
                s = row["status"]
                c = row["count"]
                if s in stats:
                    stats[s] = c
                total += c
            stats["total_managed"] = total
            return stats
        finally:
            self._release_connection(conn)

    def count_download_queue_since(self, since_ts: float, automated_only: bool = True) -> int:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            if automated_only:
                sql = (
                    "SELECT COUNT(*) as count FROM download_queue WHERE created_at >= %s AND source_url IS NOT NULL"
                    if self.is_postgres else
                    "SELECT COUNT(*) as count FROM download_queue WHERE created_at >= ? AND source_url IS NOT NULL"
                )
            else:
                sql = (
                    "SELECT COUNT(*) as count FROM download_queue WHERE created_at >= %s"
                    if self.is_postgres else
                    "SELECT COUNT(*) as count FROM download_queue WHERE created_at >= ?"
                )
            cursor.execute(sql, (since_ts,))
            row = cursor.fetchone()
            return int(row["count"] if row else 0)
        finally:
            self._release_connection(conn)

    def count_table_rows(self, table: str) -> int:
        """只读统计指定表的行数，供首次部署引导页做数据库体检。

        表名走白名单校验（不接受外部拼接的任意标识符），失败抛异常由调用方兜住。
        """
        allowed = {"movies", "actresses", "movie_actresses", "translations", "local_files", "download_queue"}
        name = str(table or "").strip()
        if name not in allowed:
            raise ValueError(f"unsupported_table:{name}")
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(f"SELECT COUNT(*) AS count FROM {name}")
            row = cursor.fetchone()
            return int(row["count"] if row else 0)
        finally:
            self._release_connection(conn)

    def list_download_queue(
        self,
        status: str = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict:
        """Return a paginated slice of download_queue rows plus status counts.

        Read-only helper for per-entry observability. Never used to submit or
        connect to qBittorrent.
        """
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholder = "%s" if self.is_postgres else "?"

            where_sql = ""
            params: List = []
            if status:
                where_sql = f"WHERE status = {placeholder}"
                params.append(status)

            count_sql = f"SELECT COUNT(*) as count FROM download_queue {where_sql}"
            cursor.execute(count_sql, tuple(params))
            row = cursor.fetchone()
            total = int(row["count"] if row else 0)

            list_sql = (
                f"SELECT * FROM download_queue {where_sql} "
                f"ORDER BY updated_at DESC, created_at DESC "
                f"LIMIT {placeholder} OFFSET {placeholder}"
            )
            cursor.execute(list_sql, (*params, int(limit), int(offset)))
            items = [dict(r) for r in cursor.fetchall()]

            return {
                "items": items,
                "total": total,
                "limit": int(limit),
                "offset": int(offset),
                "by_status": self.get_queue_stats(),
            }
        finally:
            self._release_connection(conn)

    def get_download_queue_entry(self, task_id: str) -> Optional[Dict]:
        """Fetch a single download_queue row by id (read-only)."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholder = "%s" if self.is_postgres else "?"
            cursor.execute(
                f"SELECT * FROM download_queue WHERE id = {placeholder}",
                (task_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            self._release_connection(conn)

    def retry_download_now(self, task_id: str) -> Dict:
        """Force a waiting/failed task to be picked up on the next dispatch cycle.

        Only sets next_retry_at to now; status and retry_count are preserved so
        the existing backoff history stays intact. Does not touch qBittorrent.
        """
        entry = self.get_download_queue_entry(task_id)
        if not entry:
            return {"ok": False, "reason": "task_not_found"}

        current_status = entry.get("status")
        retry_count = int(entry.get("retry_count") or 0)
        eligible = current_status == "waiting_external_service" or (
            current_status == "failed" and retry_count < 5
        )
        if not eligible:
            return {"ok": False, "reason": "invalid_state_for_retry", "status": current_status}

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholder = "%s" if self.is_postgres else "?"
            now = time.time()
            cursor.execute(
                f"UPDATE download_queue SET next_retry_at = {placeholder}, updated_at = {placeholder} "
                f"WHERE id = {placeholder}",
                (now, now, task_id),
            )
            conn.commit()
        finally:
            self._release_connection(conn)

        if entry.get("movie_id"):
            self.record_lifecycle_event(
                entry["movie_id"],
                "download_retry_requested",
                title=entry.get("title"),
                download_status=current_status,
                source=entry.get("source_url"),
                reason="Manual immediate retry requested",
                execution_result="scheduled",
                payload={"task_id": task_id, "retry_count": retry_count},
            )
        return {"ok": True, "status": current_status}

    def recover_download(self, task_id: str) -> Dict:
        """Recover a failed/cancelled task back to pending.

        Resets status to pending, clears next_retry_at and retry_count so the
        task can flow through the normal dispatch/backoff again. Refuses if
        another active queue entry already holds the same movie_id or magnet,
        because the partial unique indexes only allow one active row per key.
        """
        entry = self.get_download_queue_entry(task_id)
        if not entry:
            return {"ok": False, "reason": "task_not_found"}

        current_status = entry.get("status")
        if current_status not in ("failed", "cancelled"):
            return {"ok": False, "reason": "invalid_state_for_recover", "status": current_status}

        conflict = self.get_active_queue_entry(
            movie_id=entry.get("movie_id"),
            magnet=entry.get("magnet"),
        )
        if conflict and conflict.get("id") != task_id:
            return {"ok": False, "reason": "recover_conflict_active_exists"}

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholder = "%s" if self.is_postgres else "?"
            now = time.time()
            cursor.execute(
                f"UPDATE download_queue SET status = {placeholder}, next_retry_at = NULL, "
                f"retry_count = 0, error_message = NULL, updated_at = {placeholder} "
                f"WHERE id = {placeholder}",
                ("pending", now, task_id),
            )
            conn.commit()
        finally:
            self._release_connection(conn)

        if entry.get("movie_id"):
            self.record_lifecycle_event(
                entry["movie_id"],
                "download_recovered",
                title=entry.get("title"),
                download_status="queued",
                source=entry.get("source_url"),
                reason=f"Manually recovered from {current_status}",
                execution_result="success",
                payload={"task_id": task_id, "previous_status": current_status},
            )
        return {"ok": True, "status": "pending"}

    def cancel_download(self, task_id: str) -> Dict:
        """Cancel a queued/waiting/failed task.

        Marks the row as cancelled, which frees the movie/magnet dedup slot
        because the partial unique indexes exclude cancelled rows. Refuses to
        cancel submitted tasks (already handed to qBittorrent) and rows already
        skipped as duplicates.
        """
        entry = self.get_download_queue_entry(task_id)
        if not entry:
            return {"ok": False, "reason": "task_not_found"}

        current_status = entry.get("status")
        if current_status not in ("pending", "waiting_external_service", "failed"):
            return {"ok": False, "reason": "cannot_cancel_in_state", "status": current_status}

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholder = "%s" if self.is_postgres else "?"
            now = time.time()
            cursor.execute(
                f"UPDATE download_queue SET status = {placeholder}, next_retry_at = NULL, "
                f"updated_at = {placeholder} WHERE id = {placeholder}",
                ("cancelled", now, task_id),
            )
            conn.commit()
        finally:
            self._release_connection(conn)

        if entry.get("movie_id"):
            self.record_lifecycle_event(
                entry["movie_id"],
                "download_cancelled",
                title=entry.get("title"),
                download_status="cancelled",
                source=entry.get("source_url"),
                reason=f"Manually cancelled from {current_status}",
                execution_result="success",
                payload={"task_id": task_id, "previous_status": current_status},
            )
        return {"ok": True, "status": "cancelled"}

    # ------------------- Actress helpers -------------------
    def _normalize_birth_date(self, value):
        if not value:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value if self.is_postgres else value.isoformat()

        text = str(value).strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", text):
            return None
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d").date()
            return parsed if self.is_postgres else parsed.isoformat()
        except Exception:
            return None

    def _merge_missing_actress_fields(self, data: Dict, existing: Optional[Dict]):
        data["birth_date"] = self._normalize_birth_date(data.get("birth_date"))
        if not existing:
            return

        preserve_fields = (
            "avatar", "birth_date", "height", "cup", "bust", "waist", "hip",
            "hometown", "hobby", "page_url", "bio", "wiki_url", "name_en",
            "source", "source_id", "source_updated_at", "video_count",
            "metadata_requested_at", "metadata_last_attempt_at", "metadata_attempt_count",
            "metadata_first_error_at", "metadata_last_error", "metadata_completed_at",
        )
        for field in preserve_fields:
            if data.get(field) in (None, "") and existing.get(field) not in (None, ""):
                data[field] = existing.get(field)

    def _collect_actress_names(self, data: Dict, aliases_str: str = "") -> List[Dict]:
        names = []
        seen = set()

        def add(value, name_type):
            text = str(value or "").strip()
            if not text or text in seen:
                return
            seen.add(text)
            names.append({
                "name": text,
                "name_type": name_type,
                "source": data.get("source"),
            })

        add(data.get("name"), "primary")
        add(data.get("name_en"), "english")
        for alias in re.split(r"[,，、/|]", aliases_str or ""):
            add(alias, "alias")
        return names

    def _save_actress_names_cursor(self, cursor, actress_id: str, names: List[Dict]):
        if not actress_id or not names:
            return
        now = time.time()
        if self.is_postgres:
            sql = """
                INSERT INTO actress_names (actress_id, name, name_type, source, created_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (actress_id, name) DO UPDATE SET
                    name_type = COALESCE(EXCLUDED.name_type, actress_names.name_type),
                    source = COALESCE(EXCLUDED.source, actress_names.source)
            """
        else:
            sql = """
                INSERT INTO actress_names (actress_id, name, name_type, source, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(actress_id, name) DO UPDATE SET
                    name_type = COALESCE(excluded.name_type, actress_names.name_type),
                    source = COALESCE(excluded.source, actress_names.source)
            """
        for item in names:
            cursor.execute(sql, (actress_id, item["name"], item.get("name_type"), item.get("source"), now))

    def save_actress_name(self, actress_id: str, name: str, name_type: str = None, source: str = None) -> bool:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            self._save_actress_names_cursor(
                cursor,
                actress_id,
                [{"name": name, "name_type": name_type, "source": source}],
            )
            conn.commit()
            return True
        except Exception as e:
            logging.error(
                "Save actress name failed %s/%s: %s",
                display_value_for_log(actress_id),
                display_value_for_log(name),
                type(e).__name__,
            )
            return False
        finally:
            self._release_connection(conn)

    def save_actress(self, data: Dict) -> bool:
        if not isinstance(data, dict):
            data = dict(data)

        name = (data.get("name", "") or "").strip()
        if self._is_suspicious_actress_name(name):
            logging.warning("Discard suspicious actress record: %s...", name[:40])
            return False

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = time.time()
            if self.is_postgres:
                cursor.execute("SELECT * FROM actresses WHERE id = %s", (data.get("id"),))
                existing = cursor.fetchone()
                existing = dict(existing) if existing else None
                self._merge_missing_actress_fields(data, existing)

                is_followed = parse_bool_value(data.get("is_followed", False), False)
                if existing:
                    if "is_followed" not in data:
                        is_followed = parse_bool_value(existing["is_followed"], False)


                name = data.get("name")
                aliases_list = []
                if existing:
                    old_name = existing["name"]
                    current_aliases = existing["aliases"] or ""
                    aliases_list = [a.strip() for a in current_aliases.split(",") if a.strip()]

                    if old_name and name != old_name and old_name not in aliases_list:
                        aliases_list.append(old_name)

                    if name in aliases_list:
                        aliases_list.remove(name)

                if data.get("aliases"):
                    new_aliases = [a.strip() for a in data["aliases"].split(",") if a.strip()]
                    for na in new_aliases:
                        if na != name and na not in aliases_list:
                            aliases_list.append(na)

                aliases_str = ",".join(aliases_list)

                sql = """
                INSERT INTO actresses (
                    id, name, avatar, birth_date, height, cup, bust, waist, hip, hometown, hobby, page_url, bio, wiki_url, aliases, name_en, source, source_id, source_updated_at, video_count, metadata_requested_at, metadata_last_attempt_at, metadata_attempt_count, metadata_first_error_at, metadata_last_error, metadata_completed_at, is_followed, last_updated, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    COALESCE((SELECT created_at FROM actresses WHERE id = %s), %s)
                )
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    avatar = EXCLUDED.avatar,
                    birth_date = EXCLUDED.birth_date,
                    height = EXCLUDED.height,
                    cup = EXCLUDED.cup,
                    bust = EXCLUDED.bust,
                    waist = EXCLUDED.waist,
                    hip = EXCLUDED.hip,
                    hometown = EXCLUDED.hometown,
                    hobby = EXCLUDED.hobby,
                    page_url = EXCLUDED.page_url,
                    bio = EXCLUDED.bio,
                    wiki_url = EXCLUDED.wiki_url,
                    aliases = EXCLUDED.aliases,
                    name_en = EXCLUDED.name_en,
                    source = EXCLUDED.source,
                    source_id = EXCLUDED.source_id,
                    source_updated_at = EXCLUDED.source_updated_at,
                    video_count = EXCLUDED.video_count,
                    metadata_requested_at = EXCLUDED.metadata_requested_at,
                    metadata_last_attempt_at = EXCLUDED.metadata_last_attempt_at,
                    metadata_attempt_count = EXCLUDED.metadata_attempt_count,
                    metadata_first_error_at = EXCLUDED.metadata_first_error_at,
                    metadata_last_error = EXCLUDED.metadata_last_error,
                    metadata_completed_at = EXCLUDED.metadata_completed_at,
                    is_followed = EXCLUDED.is_followed,
                    last_updated = EXCLUDED.last_updated
                """
                params = (
                    data.get("id"), name, data.get("avatar"),
                    data.get("birth_date"), data.get("height"), data.get("cup"),
                    data.get("bust"), data.get("waist"), data.get("hip"),
                    data.get("hometown"), data.get("hobby"), data.get("page_url"),
                    data.get("bio"), data.get("wiki_url"), aliases_str,
                    data.get("name_en"), data.get("source"), data.get("source_id"),
                    data.get("source_updated_at"), data.get("video_count"),
                    data.get("metadata_requested_at"), data.get("metadata_last_attempt_at"),
                    data.get("metadata_attempt_count") or 0,
                    data.get("metadata_first_error_at"), data.get("metadata_last_error"),
                    data.get("metadata_completed_at"),
                    is_followed, now,
                    data.get("id"), now # values for created_at logic
                )
            else:
                # SQLite Logic
                cursor.execute("SELECT * FROM actresses WHERE id = ?", (data["id"],))
                res = cursor.fetchone()
                res = dict(res) if res else None
                self._merge_missing_actress_fields(data, res)
                if "is_followed" in data:
                    is_followed = data.get("is_followed")
                elif res:
                    is_followed = res["is_followed"]
                else:
                    is_followed = 0
                is_followed = 1 if parse_bool_value(is_followed, False) else 0

                name = data.get("name")
                aliases_list = []
                if res:
                    old_name = res["name"]
                    current_aliases = res["aliases"] or ""
                    aliases_list = [a.strip() for a in current_aliases.split(",") if a.strip()]
                    if old_name and name != old_name and old_name not in aliases_list:
                        aliases_list.append(old_name)
                    if name in aliases_list:
                        aliases_list.remove(name)

                if data.get("aliases"):
                    new_aliases = [a.strip() for a in data["aliases"].split(",") if a.strip()]
                    for na in new_aliases:
                        if na != name and na not in aliases_list:
                            aliases_list.append(na)

                aliases_str = ",".join(aliases_list)

                # Need to handle created_at for SQLite UPSERT logic
                # SQLite's ON CONFLICT replacement strategy might lose created_at if we use REPLACE
                # So we must check existence first or use proper UPSERT syntax (newer sqlite)
                # But here we stick to the manual check logic from above to preserve old fields

                # Check created_at
                created_at = now
                if res and "created_at" in res.keys(): # Check if column exists in row (it might not if schema just updated but data old)
                     # row factory is Row, behaves like dict but robust
                     try:
                         if res["created_at"]:
                            created_at = res["created_at"]
                     except (KeyError, IndexError):
                        pass
                elif res:
                     pass

                sql = """
                INSERT OR REPLACE INTO actresses (id, name, avatar, birth_date, height, cup, bust, waist, hip, hometown, hobby, page_url, bio, wiki_url, aliases, name_en, source, source_id, source_updated_at, video_count, metadata_requested_at, metadata_last_attempt_at, metadata_attempt_count, metadata_first_error_at, metadata_last_error, metadata_completed_at, is_followed, last_updated, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
                params = (
                    data.get("id"), name, data.get("avatar"),
                    data.get("birth_date"), data.get("height"), data.get("cup"),
                    data.get("bust"), data.get("waist"), data.get("hip"),
                    data.get("hometown"), data.get("hobby"), data.get("page_url"),
                    data.get("bio"), data.get("wiki_url"), aliases_str,
                    data.get("name_en"), data.get("source"), data.get("source_id"),
                    data.get("source_updated_at"), data.get("video_count"),
                    data.get("metadata_requested_at"), data.get("metadata_last_attempt_at"),
                    data.get("metadata_attempt_count") or 0,
                    data.get("metadata_first_error_at"), data.get("metadata_last_error"),
                    data.get("metadata_completed_at"),
                    is_followed, now, created_at
                )
            cursor.execute(sql, params)
            self._save_actress_names_cursor(
                cursor,
                data.get("id"),
                self._collect_actress_names(data, aliases_str),
            )
            conn.commit()
            return True
        except Exception as e:
            logging.error("Failed to save actress %s: %s", display_value_for_log(data.get("id")), type(e).__name__)
            return False
        finally:
            self._release_connection(conn)

    def _is_suspicious_actress_name(self, name: str) -> bool:
        """Return whether an actress name looks suspicious."""
        if not name:
            return False

        if len(name) > 40:
            return True

        if re.search(r'\b[A-Z]{2,6}-?\d{2,5}\b', name, re.IGNORECASE):
            return True

        if re.search(r'\d{4}-\d{2}-\d{2}', name):
            return True

        separators = sum(name.count(ch) for ch in [':', '|', '/', '\\'])
        if separators >= 2:
            return True

        return False

    def get_actress(self, actress_id: str) -> Optional[Dict]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "SELECT * FROM actresses WHERE id = %s" if self.is_postgres else "SELECT * FROM actresses WHERE id = ?"
            cursor.execute(sql, (actress_id,))
            row = cursor.fetchone()
            if not row:
                return None
            data = dict(row)
            sql_names = (
                "SELECT name, name_type, source FROM actress_names WHERE actress_id = %s ORDER BY created_at ASC"
                if self.is_postgres else
                "SELECT name, name_type, source FROM actress_names WHERE actress_id = ? ORDER BY created_at ASC"
            )
            cursor.execute(sql_names, (actress_id,))
            data["names"] = [dict(name_row) for name_row in cursor.fetchall()]
            return data
        finally:
            self._release_connection(conn)

    def get_actress_by_source(self, source: str, source_id: str) -> Optional[Dict]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "SELECT * FROM actresses WHERE source = %s AND source_id = %s" if self.is_postgres else "SELECT * FROM actresses WHERE source = ? AND source_id = ?"
            cursor.execute(sql, (source, source_id))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            self._release_connection(conn)

    def get_actress_by_name(self, name: str) -> Optional[Dict]:
        """Return an actress matched by primary name or known alias."""
        name = str(name or "").strip()
        if not name:
            return None

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            if self.is_postgres:
                sql = """
                    SELECT a.*
                    FROM actresses a
                    LEFT JOIN actress_names an ON an.actress_id = a.id
                    WHERE LOWER(a.name) = LOWER(%s) OR LOWER(an.name) = LOWER(%s)
                    ORDER BY a.is_followed DESC, a.last_updated DESC
                    LIMIT 1
                """
            else:
                sql = """
                    SELECT a.*
                    FROM actresses a
                    LEFT JOIN actress_names an ON an.actress_id = a.id
                    WHERE a.name = ? COLLATE NOCASE OR an.name = ? COLLATE NOCASE
                    ORDER BY a.is_followed DESC, a.last_updated DESC
                    LIMIT 1
                """
            cursor.execute(sql, (name, name))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            self._release_connection(conn)

    def request_actress_metadata(self, actress_id: str) -> bool:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = time.time()
            sql = (
                "UPDATE actresses SET metadata_requested_at = COALESCE(metadata_requested_at, %s) WHERE id = %s"
                if self.is_postgres else
                "UPDATE actresses SET metadata_requested_at = COALESCE(metadata_requested_at, ?) WHERE id = ?"
            )
            cursor.execute(sql, (now, actress_id))
            conn.commit()
            return True
        except Exception as e:
            logging.error("Request actress metadata failed %s: %s", display_value_for_log(actress_id), type(e).__name__)
            return False
        finally:
            self._release_connection(conn)

    def get_actress_names_for_translation_guard(self, limit: int = 200000) -> List[str]:
        """演员名，供标题翻译的占位保护使用。

        **含假名的必收**：用户口径是「艺人姓名保留日文假名」，这类名字被音译成汉字是主要失效点
        （实测 `水野アコ` → `水野绫子`、`水野優香` → `水野优香`）。
        **纯汉字名也收**：200 条抽检里实测 `宮前幸恵` → `宫前幸惠`，汉字被转成简体，
        同样不符合「保留原样」的口径。只含 ASCII 字母数字/符号的排除——那是番号不是人名。

        过滤放在 Python 侧：PG 支持 `~` 正则而 SQLite 不支持，双后端下写法会分叉。
        """
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT name FROM actresses WHERE name IS NOT NULL AND TRIM(name) <> '' LIMIT {int(limit)}"
            )
            rows = cursor.fetchall()
        finally:
            self._release_connection(conn)
        kana = re.compile(r"[぀-ゟ゠-ヿ]")
        cjk = re.compile(r"[一-鿿]")
        latin_only = re.compile(r"^[\x00-\x7f\s·・.\-_/]+$")
        seen = set()
        for row in rows:
            text = str(_row_value(row, "name", 0) or "").strip()
            if len(text) < 2:
                continue
            if kana.search(text) or cjk.search(text):
                if not latin_only.match(text):
                    seen.add(text)
        return sorted(seen)

    def get_actresses_missing_avatar(
        self,
        limit: Optional[int] = None,
        *,
        randomize: bool = False,
    ) -> List[Dict]:
        """缺头像的演员（附逗号分隔的别名），供头像补全使用。

        `randomize=True` 用 `ORDER BY random()` 打散候选顺序。**这不是为了随机性，而是
        为了避开饥饿**：查询没有 ORDER BY 时返回的是存储顺序的前 N 行，而实测 gfriends
        索引对靠前那批演员的命中率是 0（连续两次 sync(limit=300) 都匹配 0 条，而
        limit=3000 能匹配到 82 条）。不打散就会永远只扫那批低命中的，**后面的候选
        一条都轮不到**，循环看着在跑、实际零产出。缺口 6 万行，每半小时随机采样一次
        的排序开销可以忽略。
        """
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            aggregate = "string_agg(n.name, ',')" if self.is_postgres else "group_concat(n.name, ',')"
            sql = f"""
                SELECT a.id, a.name, COALESCE({aggregate}, '') AS alias_names
                FROM actresses a
                LEFT JOIN actress_names n ON n.actress_id = a.id
                WHERE (a.avatar IS NULL OR a.avatar = '')
                  AND a.name IS NOT NULL AND a.name <> ''
                GROUP BY a.id, a.name
            """
            if randomize:
                sql += " ORDER BY random()"
            if limit:
                sql += f" LIMIT {int(limit)}"
            cursor.execute(sql)
            return [
                {
                    "id": _row_value(row, "id", 0),
                    "name": _row_value(row, "name", 1),
                    "alias_names": _row_value(row, "alias_names", 2) or "",
                }
                for row in cursor.fetchall()
            ]
        except Exception as e:
            logging.error("Get actresses missing avatar failed: %s", type(e).__name__)
            return []
        finally:
            self._release_connection(conn)

    def update_actress_avatars(self, updates: List[Tuple[str, str]]) -> int:
        """批量回填头像；只覆盖原本为空的行，已有头像不被动。"""
        if not updates:
            return 0
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholder = "%s" if self.is_postgres else "?"
            sql = (
                f"UPDATE actresses SET avatar = {placeholder} "
                f"WHERE id = {placeholder} AND (avatar IS NULL OR avatar = '')"
            )
            cursor.executemany(sql, [(url, actress_id) for actress_id, url in updates])
            conn.commit()
            affected = cursor.rowcount
            return affected if isinstance(affected, int) and affected >= 0 else len(updates)
        except Exception as e:
            logging.error("Update actress avatars failed: %s", type(e).__name__)
            return 0
        finally:
            self._release_connection(conn)

    def get_actresses_needing_metadata(self, limit: int = 10, retry_after_seconds: int = 21600, max_attempts: int = 5) -> List[Dict]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cutoff = time.time() - retry_after_seconds
            missing_birth = "birth_date IS NULL" if self.is_postgres else "birth_date IS NULL OR birth_date = ''"
            where = f"""
                metadata_requested_at IS NOT NULL
                AND COALESCE(metadata_attempt_count, 0) < {{attempt_placeholder}}
                AND (metadata_last_attempt_at IS NULL OR metadata_last_attempt_at < {{cutoff_placeholder}})
                AND (
                    avatar IS NULL OR avatar = ''
                    OR {missing_birth}
                    OR height IS NULL OR height = ''
                    OR bust IS NULL OR bust = ''
                    OR waist IS NULL OR waist = ''
                    OR hip IS NULL OR hip = ''
                )
            """
            if self.is_postgres:
                sql = f"""
                    SELECT * FROM actresses
                    WHERE {where.format(attempt_placeholder='%s', cutoff_placeholder='%s')}
                    ORDER BY metadata_requested_at ASC
                    LIMIT %s
                """
            else:
                sql = f"""
                    SELECT * FROM actresses
                    WHERE {where.format(attempt_placeholder='?', cutoff_placeholder='?')}
                    ORDER BY metadata_requested_at ASC
                    LIMIT ?
                """
            cursor.execute(sql, (max_attempts, cutoff, limit))
            return [dict(row) for row in cursor.fetchall()]
        finally:
            self._release_connection(conn)

    def mark_actress_metadata_attempt(
        self,
        actress_id: str,
        success: bool = False,
        error: Optional[str] = None,
    ):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = time.time()
            placeholder = "%s" if self.is_postgres else "?"
            if success:
                sql = f"""
                    UPDATE actresses
                    SET metadata_last_attempt_at = {placeholder}, metadata_requested_at = NULL,
                        metadata_attempt_count = COALESCE(metadata_attempt_count, 0) + 1,
                        metadata_completed_at = {placeholder}
                    WHERE id = {placeholder}
                """
                params = (now, now, actress_id)
            else:
                sql = f"""
                    UPDATE actresses
                    SET metadata_last_attempt_at = {placeholder},
                        metadata_attempt_count = COALESCE(metadata_attempt_count, 0) + 1,
                        metadata_first_error_at = COALESCE(metadata_first_error_at, {placeholder}),
                        metadata_last_error = {placeholder}, metadata_completed_at = NULL
                    WHERE id = {placeholder}
                """
                params = (
                    now,
                    now,
                    str(error or "incomplete_metadata")[:120],
                    actress_id,
                )
            cursor.execute(sql, params)
            conn.commit()
        finally:
            self._release_connection(conn)

    def get_actress_metadata_task_stats(self, max_attempts: int = 5) -> Dict[str, int]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholder = "%s" if self.is_postgres else "?"
            cursor.execute(
                f"""
                SELECT
                    SUM(CASE WHEN metadata_requested_at IS NOT NULL
                                  AND COALESCE(metadata_attempt_count, 0) < {placeholder}
                             THEN 1 ELSE 0 END) AS waiting,
                    SUM(CASE WHEN metadata_requested_at IS NOT NULL
                                  AND COALESCE(metadata_attempt_count, 0) >= {placeholder}
                             THEN 1 ELSE 0 END) AS needs_attention,
                    SUM(CASE WHEN metadata_completed_at IS NOT NULL THEN 1 ELSE 0 END) AS completed
                FROM actresses
                """,
                (max_attempts, max_attempts),
            )
            row = cursor.fetchone() or {}
            return {
                "waiting": int(row["waiting"] or 0),
                "needs_attention": int(row["needs_attention"] or 0),
                "completed": int(row["completed"] or 0),
            }
        finally:
            self._release_connection(conn)

    def get_actresses(self, limit: int = 50, offset: int = 0, followed_only: bool = False, query: str = None, sort_by_new: bool = False, sort_by_cup: bool = False, followed_filter: Optional[bool] = None) -> List[Dict]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            where_clauses = []
            params = []

            if followed_filter is not None:
                if followed_filter:
                    where_clauses.append("is_followed = TRUE" if self.is_postgres else "is_followed = 1")
                else:
                    where_clauses.append("(is_followed IS NULL OR is_followed = FALSE)" if self.is_postgres else "(is_followed IS NULL OR is_followed = 0)")
            elif followed_only:
                where_clauses.append("is_followed = TRUE" if self.is_postgres else "is_followed = 1")

            if query:
                name_like = "%s" if self.is_postgres else "?"
                where_clauses.append(
                    f"""(
                        name LIKE {name_like}
                        OR aliases LIKE {name_like}
                        OR id IN (
                            SELECT actress_id FROM actress_names WHERE name LIKE {name_like}
                        )
                    )"""
                )
                params.extend([f"%{query}%", f"%{query}%", f"%{query}%"])

            where_sql = ""
            if where_clauses:
                where_sql = " WHERE " + " AND ".join(where_clauses)

            has_avatar_rank = "CASE WHEN avatar IS NOT NULL AND avatar <> '' THEN 1 ELSE 0 END"

            if sort_by_cup:
                if self.is_postgres:
                    cup_rank = """
                    CASE
                        WHEN upper(cup) LIKE 'G%%' THEN 70
                        WHEN upper(cup) LIKE 'F%%' THEN 60
                        WHEN upper(cup) LIKE 'E%%' THEN 50
                        WHEN upper(cup) LIKE 'D%%' THEN 40
                        WHEN upper(cup) LIKE 'C%%' THEN 30
                        WHEN upper(cup) LIKE 'B%%' THEN 20
                        WHEN upper(cup) LIKE 'A%%' THEN 10
                        ELSE
                            CASE
                                WHEN NULLIF(regexp_replace(coalesce(bust, ''), '[^0-9.]', '', 'g'), '') IS NULL THEN 0
                                WHEN CAST(NULLIF(regexp_replace(coalesce(bust, ''), '[^0-9.]', '', 'g'), '') AS DOUBLE PRECISION) < 60 THEN CAST(NULLIF(regexp_replace(coalesce(bust, ''), '[^0-9.]', '', 'g'), '') AS DOUBLE PRECISION) * 2.54
                                ELSE CAST(NULLIF(regexp_replace(coalesce(bust, ''), '[^0-9.]', '', 'g'), '') AS DOUBLE PRECISION)
                            END
                    END
                    """
                else:
                    cup_rank = """
                    CASE
                        WHEN upper(cup) LIKE 'G%' THEN 70
                        WHEN upper(cup) LIKE 'F%' THEN 60
                        WHEN upper(cup) LIKE 'E%' THEN 50
                        WHEN upper(cup) LIKE 'D%' THEN 40
                        WHEN upper(cup) LIKE 'C%' THEN 30
                        WHEN upper(cup) LIKE 'B%' THEN 20
                        WHEN upper(cup) LIKE 'A%' THEN 10
                        WHEN CAST(NULLIF(bust, '') AS REAL) < 60 THEN CAST(NULLIF(bust, '') AS REAL) * 2.54
                        ELSE COALESCE(CAST(NULLIF(bust, '') AS REAL), 0)
                    END
                    """
                order_sql = f" ORDER BY ({has_avatar_rank}) DESC, ({cup_rank}) DESC, is_followed DESC, last_updated DESC, created_at DESC"
            elif sort_by_new:
                order_sql = f" ORDER BY ({has_avatar_rank}) DESC, last_updated DESC, created_at DESC"
            else:
                order_sql = f" ORDER BY ({has_avatar_rank}) DESC, is_followed DESC, last_updated DESC, created_at DESC"

            # 同名演员（多为跨来源重复：r18:xxx 与 netflav:xxx 是同一人）在列表页聚合成一条。
            #
            # ⚠️ 原实现用双窗口函数（COUNT(*) OVER + ROW_NUMBER() OVER，都 PARTITION BY name）
            # 让 PG 必须物化并排序全部 10.2 万行才能取 30 条 —— 实测每页 0.9~1.2s，
            # 而同样的排序不加窗口函数只要 0.1~0.25s（实测对比过 DISTINCT ON 反而更慢，
            # 因为它要先按分组键排一次再按展示序排第二次）。
            # 改为：按展示序拉「够用的一段行」→ 内存按名去重 → 切片出该页。
            # 拉取量放大 DEDUPE_SCAN_FACTOR 倍，保证跨页不会因同名组被截断而漏条。
            group_key = "COALESCE(NULLIF(name, ''), id)"
            placeholder = "%s" if self.is_postgres else "?"
            scan_limit = offset + max(int(limit), 1) * DEDUPE_SCAN_FACTOR
            sql = (
                f"SELECT * FROM actresses{where_sql}{order_sql} "
                f"LIMIT {placeholder}"
            )
            params.extend([scan_limit])

            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()

            results = []
            seen_keys = set()
            for row in rows:
                record = dict(row)
                dedupe_key = record.get("name") or record.get("id")
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                record["dup_count"] = 1
                results.append(record)
            # 无论第几页都切出 limit 条；第一页也必须切，
            # 否则会一次返回全部去重后的行（实测本地库 page1 返回 115 条）。
            return results[offset:offset + limit]
        finally:
            self._release_connection(conn)

    def toggle_follow_actress(self, actress_id: str, force_state: Optional[bool] = None) -> bool:
        """Toggle follow status and return the new state."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql_get = "SELECT is_followed FROM actresses WHERE id = %s" if self.is_postgres else "SELECT is_followed FROM actresses WHERE id = ?"
            cursor.execute(sql_get, (actress_id,))
            row = cursor.fetchone()
            if not row:
                return False

            if force_state is not None:
                new_state = parse_bool_value(force_state, False)
            else:
                current = parse_bool_value(row["is_followed"], False)
                new_state = not current

            sql_update = "UPDATE actresses SET is_followed = %s WHERE id = %s" if self.is_postgres else "UPDATE actresses SET is_followed = ? WHERE id = ?"
            val = new_state if self.is_postgres else (1 if new_state else 0)
            cursor.execute(sql_update, (val, actress_id))
            conn.commit()
            return new_state
        except Exception as e:
            logging.error("Failed to toggle actress subscription status: %s", type(e).__name__)
            return False
        finally:
            self._release_connection(conn)

    # ------------------- Lifecycle & Local Media helpers -------------------
    def update_movie_watched_status(self, movie_id: str, is_watched: bool, last_watched_at: float = None, watch_count_inc: int = 0):
        """Update movie watched status and counters."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = last_watched_at or time.time()
            is_w = is_watched if self.is_postgres else (1 if is_watched else 0)

            sql = """
            UPDATE movies
            SET is_watched = %s, last_watched_at = %s, watch_count = watch_count + %s
            WHERE id = %s
            """ if self.is_postgres else """
            UPDATE movies
            SET is_watched = ?, last_watched_at = ?, watch_count = watch_count + ?
            WHERE id = ?
            """
            cursor.execute(sql, (is_w, now, watch_count_inc, movie_id))
            conn.commit()
            self.record_lifecycle_event(
                movie_id,
                "jellyfin_watched_status_updated",
                jellyfin_status="watched" if is_watched else "unwatched",
                payload={"watch_count_inc": watch_count_inc, "last_watched_at": now},
            )
        finally:
            self._release_connection(conn)

    def update_movie_liked_status(self, movie_id: str, is_liked: bool):
        """Update movie favorite status from Jellyfin."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            is_l = is_liked if self.is_postgres else (1 if is_liked else 0)
            sql = "UPDATE movies SET is_liked = %s WHERE id = %s" if self.is_postgres else "UPDATE movies SET is_liked = ? WHERE id = ?"
            cursor.execute(sql, (is_l, movie_id))
            conn.commit()
            self.record_lifecycle_event(
                movie_id,
                "jellyfin_favorite_status_updated",
                jellyfin_status="favorite" if is_liked else "not_favorite",
            )
        finally:
            self._release_connection(conn)

    def save_local_file(
        self,
        movie_id: str,
        file_path: str,
        symlink_path: str = None,
        file_size: int = 0,
    ) -> bool:
        """Save a local file mapping for a movie."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = time.time()
            if self.is_postgres:
                sql = """
                INSERT INTO local_files (movie_id, file_path, symlink_path, file_size, added_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (movie_id) DO UPDATE SET
                    file_path = EXCLUDED.file_path,
                    symlink_path = EXCLUDED.symlink_path,
                    file_size = EXCLUDED.file_size,
                    added_at = EXCLUDED.added_at
                """
            else:
                sql = """
                INSERT OR REPLACE INTO local_files (movie_id, file_path, symlink_path, file_size, added_at)
                VALUES (?, ?, ?, ?, ?)
                """
            cursor.execute(sql, (movie_id, file_path, symlink_path, file_size, now))
            conn.commit()
            return True
        finally:
            self._release_connection(conn)

    def save_local_file_jellyfin(self, movie_id: str, *, item_id: Optional[str] = None) -> bool:
        """记录 Jellyfin 侧条目身份（闭环收尾：该片已在 Jellyfin 可见）。"""
        if not movie_id:
            return False
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = time.time()
            ph = "%s" if self.is_postgres else "?"
            cursor.execute(
                f"""
                UPDATE local_files SET
                    jellyfin_item_id = {ph},
                    jellyfin_synced_at = {ph}
                WHERE movie_id = {ph}
                """,
                (item_id, now, movie_id),
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    f"""
                    INSERT INTO local_files (
                        movie_id, file_path, symlink_path, file_size, added_at,
                        jellyfin_item_id, jellyfin_synced_at
                    ) VALUES ({ph}, NULL, NULL, NULL, {ph}, {ph}, {ph})
                    """,
                    (movie_id, now, item_id, now),
                )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False
        finally:
            self._release_connection(conn)

    def save_local_file_amane(
        self,
        movie_id: str,
        *,
        media_file_id: Optional[int] = None,
        task_id: Optional[int] = None,
        file_path: Optional[str] = None,
        file_size: Optional[int] = None,
    ) -> bool:
        """记录 amane 侧的媒体文件/任务身份（P1-3 双向定位）。"""
        if not movie_id:
            return False
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = time.time()
            if self.is_postgres:
                sql = """
                INSERT INTO local_files (
                    movie_id, file_path, symlink_path, file_size, added_at,
                    amane_media_file_id, amane_task_id, amane_synced_at
                ) VALUES (%s, %s, NULL, %s, %s, %s, %s, %s)
                ON CONFLICT (movie_id) DO UPDATE SET
                    file_path = COALESCE(EXCLUDED.file_path, local_files.file_path),
                    file_size = COALESCE(EXCLUDED.file_size, local_files.file_size),
                    amane_media_file_id = COALESCE(EXCLUDED.amane_media_file_id, local_files.amane_media_file_id),
                    amane_task_id = COALESCE(EXCLUDED.amane_task_id, local_files.amane_task_id),
                    amane_synced_at = EXCLUDED.amane_synced_at
                """
                cursor.execute(sql, (movie_id, file_path, file_size, now, media_file_id, task_id, now))
            else:
                cursor.execute(
                    """
                    UPDATE local_files SET
                        file_path = COALESCE(?, file_path),
                        file_size = COALESCE(?, file_size),
                        amane_media_file_id = COALESCE(?, amane_media_file_id),
                        amane_task_id = COALESCE(?, amane_task_id),
                        amane_synced_at = ?
                    WHERE movie_id = ?
                    """,
                    (file_path, file_size, media_file_id, task_id, now, movie_id),
                )
                if cursor.rowcount == 0:
                    cursor.execute(
                        """
                        INSERT INTO local_files (
                            movie_id, file_path, symlink_path, file_size, added_at,
                            amane_media_file_id, amane_task_id, amane_synced_at
                        ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
                        """,
                        (movie_id, file_path, file_size, now, media_file_id, task_id, now),
                    )
            conn.commit()
            return True
        except Exception as e:
            logging.error("Failed to save amane identity for %s: %s", display_value_for_log(movie_id), type(e).__name__)
            return False
        finally:
            self._release_connection(conn)

    def get_local_file(self, movie_id: str) -> Optional[Dict]:
        """查询本地文件映射"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "SELECT * FROM local_files WHERE movie_id = %s" if self.is_postgres else "SELECT * FROM local_files WHERE movie_id = ?"
            cursor.execute(sql, (movie_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            self._release_connection(conn)

    def get_local_file_movie_ids(self, movie_ids: List[str]) -> set:
        if not movie_ids:
            return set()
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholders = ",".join(["%s" if self.is_postgres else "?"] * len(movie_ids))
            sql = f"SELECT movie_id FROM local_files WHERE movie_id IN ({placeholders})"
            cursor.execute(sql, tuple(movie_ids))
            rows = cursor.fetchall()
            return {row["movie_id"] if isinstance(row, dict) else row[0] for row in rows}
        finally:
            self._release_connection(conn)

    def get_lifecycle_expired_movies(self, retention_days: int) -> List[Dict]:
        """
        Return movies that meet lifecycle cleanup conditions:
        1. Watched.
        2. Watched once.
        3. Not liked.
        4. Last watched earlier than retention_days.
        """
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = time.time()
            retention_seconds = retention_days * 86400

            if self.is_postgres:
                sql = """
                SELECT m.id, m.title, l.file_path, l.symlink_path
                FROM movies m
                JOIN local_files l ON m.id = l.movie_id
                WHERE m.is_watched = TRUE
                  AND m.watch_count = 1
                  AND m.is_liked = FALSE
                  AND m.last_watched_at < %s
                """
            else:
                sql = """
                SELECT m.id, m.title, l.file_path, l.symlink_path
                FROM movies m
                JOIN local_files l ON m.id = l.movie_id
                WHERE m.is_watched = 1
                  AND m.watch_count = 1
                  AND m.is_liked = 0
                  AND m.last_watched_at < ?
                """
            cursor.execute(sql, (now - retention_seconds,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            self._release_connection(conn)

    def get_auto_delete_targets(
        self,
        *,
        watch_count_max: int = 1,
        retention_days: int = 30,
        limit: int = 50,
    ) -> List[Dict]:
        """返回可自动删除的已看片候选：

        条件：is_watched + watch_count<=N + not is_liked + 最后观看超过保留期，
        且 local_files 有磁盘文件、cleanup_candidates 里有 jellyfin_id 映射。
        按 last_watched_at 升序（最久没看优先）。用于自动删除执行器。
        """
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = time.time()
            retention_seconds = max(int(retention_days), 1) * 86400
            cutoff = now - retention_seconds
            cap = max(1, min(int(limit), 200))

            if self.is_postgres:
                sql = """
                SELECT m.id, m.title, m.watch_count, m.last_watched_at,
                       l.file_path, l.symlink_path,
                       (SELECT c.jellyfin_id FROM cleanup_candidates c WHERE c.movie_id = m.id LIMIT 1) AS jellyfin_id
                FROM movies m
                JOIN local_files l ON m.id = l.movie_id
                WHERE m.is_watched = TRUE
                  AND m.watch_count <= %s
                  AND m.is_liked = FALSE
                  AND m.last_watched_at < %s
                ORDER BY m.last_watched_at ASC
                LIMIT %s
                """
                cursor.execute(sql, (int(watch_count_max), cutoff, cap))
            else:
                sql = """
                SELECT m.id, m.title, m.watch_count, m.last_watched_at,
                       l.file_path, l.symlink_path,
                       (SELECT c.jellyfin_id FROM cleanup_candidates c WHERE c.movie_id = m.id LIMIT 1) AS jellyfin_id
                FROM movies m
                JOIN local_files l ON m.id = l.movie_id
                WHERE m.is_watched = 1
                  AND m.watch_count <= ?
                  AND m.is_liked = 0
                  AND m.last_watched_at < ?
                ORDER BY m.last_watched_at ASC
                LIMIT ?
                """
                cursor.execute(sql, (int(watch_count_max), cutoff, cap))

            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            self._release_connection(conn)

    def get_magnet_for_movie(self, movie_id: str) -> Optional[str]:
        """返回某影片最近一次入队的磁力链接（用于解析 btih 删除 qBittorrent 种子）。"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = (
                "SELECT magnet FROM download_queue WHERE movie_id = %s ORDER BY created_at DESC LIMIT 1"
                if self.is_postgres
                else "SELECT magnet FROM download_queue WHERE movie_id = ? ORDER BY created_at DESC LIMIT 1"
            )
            cursor.execute(sql, (movie_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return dict(row).get("magnet")
        finally:
            self._release_connection(conn)

    def upsert_cleanup_candidate(
        self,
        *,
        jellyfin_id: str,
        movie_id: str = None,
        title: str = "",
        file_path: str = "",
        progress: int = 0,
        is_favorite: bool = False,
        reason: str = "",
        source: str = "jellyfin_cleanup_scan",
    ) -> str:
        """Record a non-destructive cleanup candidate for later review."""
        candidate_id = str(uuid.uuid4())
        now = time.time()
        favorite_value = is_favorite if self.is_postgres else (1 if is_favorite else 0)
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            if self.is_postgres:
                sql = """
                INSERT INTO cleanup_candidates (
                    id, jellyfin_id, movie_id, title, file_path, progress, is_favorite,
                    reason, status, source, created_at, updated_at, last_seen_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'candidate', %s, %s, %s, %s)
                ON CONFLICT (jellyfin_id) DO UPDATE SET
                    movie_id = EXCLUDED.movie_id,
                    title = EXCLUDED.title,
                    file_path = EXCLUDED.file_path,
                    progress = EXCLUDED.progress,
                    is_favorite = EXCLUDED.is_favorite,
                    reason = EXCLUDED.reason,
                    source = EXCLUDED.source,
                    status = CASE
                        WHEN cleanup_candidates.status IN ('approved', 'deleted', 'rejected') THEN cleanup_candidates.status
                        ELSE 'candidate'
                    END,
                    updated_at = EXCLUDED.updated_at,
                    last_seen_at = EXCLUDED.last_seen_at
                RETURNING id, status
                """
            else:
                sql = """
                INSERT INTO cleanup_candidates (
                    id, jellyfin_id, movie_id, title, file_path, progress, is_favorite,
                    reason, status, source, created_at, updated_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?)
                ON CONFLICT(jellyfin_id) DO UPDATE SET
                    movie_id = excluded.movie_id,
                    title = excluded.title,
                    file_path = excluded.file_path,
                    progress = excluded.progress,
                    is_favorite = excluded.is_favorite,
                    reason = excluded.reason,
                    source = excluded.source,
                    status = CASE
                        WHEN cleanup_candidates.status IN ('approved', 'deleted', 'rejected') THEN cleanup_candidates.status
                        ELSE 'candidate'
                    END,
                    updated_at = excluded.updated_at,
                    last_seen_at = excluded.last_seen_at
                """
            params = (
                candidate_id,
                jellyfin_id,
                movie_id,
                title,
                file_path,
                progress,
                favorite_value,
                reason,
                source,
                now,
                now,
                now,
            )
            cursor.execute(sql, params)
            candidate_status = "candidate"
            if self.is_postgres:
                row = cursor.fetchone()
                candidate_id = row["id"] if isinstance(row, dict) else row[0]
                candidate_status = row["status"] if isinstance(row, dict) else row[1]
            else:
                lookup = "SELECT id, status FROM cleanup_candidates WHERE jellyfin_id = ?"
                cursor.execute(lookup, (jellyfin_id,))
                row = cursor.fetchone()
                if row:
                    candidate_id = row["id"]
                    candidate_status = row["status"]
            conn.commit()
            if movie_id and candidate_status == "candidate":
                self.record_lifecycle_event(
                    movie_id,
                    "cleanup_candidate_detected",
                    title=title,
                    cleanup_status="candidate",
                    source=source,
                    reason=reason,
                    payload={
                        "candidate_id": candidate_id,
                        "jellyfin_id": jellyfin_id,
                        "progress": progress,
                        "is_favorite": is_favorite,
                    },
                )
            return candidate_id
        finally:
            self._release_connection(conn)

    def get_cleanup_candidates(self, status: str = "candidate", limit: int = 100, offset: int = 0) -> List[Dict]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            if status:
                sql = (
                    "SELECT * FROM cleanup_candidates WHERE status = %s ORDER BY last_seen_at DESC LIMIT %s OFFSET %s"
                    if self.is_postgres
                    else "SELECT * FROM cleanup_candidates WHERE status = ? ORDER BY last_seen_at DESC LIMIT ? OFFSET ?"
                )
                cursor.execute(sql, (status, limit, offset))
            else:
                sql = (
                    "SELECT * FROM cleanup_candidates ORDER BY last_seen_at DESC LIMIT %s OFFSET %s"
                    if self.is_postgres
                    else "SELECT * FROM cleanup_candidates ORDER BY last_seen_at DESC LIMIT ? OFFSET ?"
                )
                cursor.execute(sql, (limit, offset))
            return [dict(row) for row in cursor.fetchall()]
        finally:
            self._release_connection(conn)

    def decide_cleanup_candidate(
        self,
        candidate_id: str,
        status: str,
        actor: str,
        reason: str,
    ) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            placeholder = "%s" if self.is_postgres else "?"
            cursor.execute(
                f"SELECT * FROM cleanup_candidates WHERE id = {placeholder}",
                (candidate_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            existing = dict(row)
            if existing.get("status") == status:
                existing["duplicate"] = True
                return existing
            if existing.get("status") != "candidate":
                existing["conflict"] = True
                return existing

            now = time.time()
            safe_actor = redact_log_message(actor)[:120]
            safe_reason = redact_log_message(reason)[:500]
            cursor.execute(
                f"""
                UPDATE cleanup_candidates
                SET status = {placeholder}, decision_actor = {placeholder},
                    decision_reason = {placeholder}, decided_at = {placeholder}, updated_at = {placeholder}
                WHERE id = {placeholder} AND status = 'candidate'
                """,
                (status, safe_actor, safe_reason, now, now, candidate_id),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                cursor.execute(
                    f"SELECT * FROM cleanup_candidates WHERE id = {placeholder}",
                    (candidate_id,),
                )
                concurrent_row = cursor.fetchone()
                concurrent = dict(concurrent_row) if concurrent_row else None
                if concurrent and concurrent.get("status") == status:
                    concurrent["duplicate"] = True
                elif concurrent:
                    concurrent["conflict"] = True
                return concurrent
            conn.commit()
            cursor.execute(
                f"SELECT * FROM cleanup_candidates WHERE id = {placeholder}",
                (candidate_id,),
            )
            decided = dict(cursor.fetchone())
        finally:
            self._release_connection(conn)

        if decided.get("movie_id"):
            self.record_lifecycle_event(
                decided["movie_id"],
                f"cleanup_candidate_{status}",
                title=decided.get("title"),
                cleanup_status=status,
                source=safe_actor,
                reason=safe_reason,
                execution_result="recorded",
                payload={"candidate_id": candidate_id, "jellyfin_id": decided.get("jellyfin_id")},
            )
        return decided

    # ------------------- Jable Hot List helpers -------------------
    def save_jable_hot_item(self, video_id: str, sort_by: str, title: str) -> bool:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            id_key = f"{video_id}_{sort_by}"
            now = time.time()
            if self.is_postgres:
                sql = """
                INSERT INTO jable_hot_list (id, video_id, sort_by, title, added_at, status)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """
                cursor.execute(sql, (id_key, video_id, sort_by, title, now, 'pending'))
            else:
                sql = "INSERT OR IGNORE INTO jable_hot_list (id, video_id, sort_by, title, added_at, status) VALUES (?, ?, ?, ?, ?, ?)"
                cursor.execute(sql, (id_key, video_id, sort_by, title, now, 'pending'))
            conn.commit()
            return True
        except Exception as e:
            logging.error("Save Jable hot item %s failed: %s", display_value_for_log(video_id), type(e).__name__)
            return False
        finally:
            self._release_connection(conn)

    def get_pending_jable_hot_items(self, limit: int = 20) -> List[Dict]:
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "SELECT * FROM jable_hot_list WHERE status = %s LIMIT %s" if self.is_postgres else "SELECT * FROM jable_hot_list WHERE status = ? LIMIT ?"
            cursor.execute(sql, ('pending', limit))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            self._release_connection(conn)

    def update_jable_hot_status(self, video_id: str, sort_by: str, status: str):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            id_key = f"{video_id}_{sort_by}"
            sql = "UPDATE jable_hot_list SET status = %s WHERE id = %s" if self.is_postgres else "UPDATE jable_hot_list SET status = ? WHERE id = ?"
            cursor.execute(sql, (status, id_key))
            conn.commit()
        finally:
            self._release_connection(conn)

    def save_pending_download(self, movie_id: str, title: str, source: str = 'jable', note: str = '') -> bool:
        """Save a pending download movie."""
        import time
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = time.time()
            id_key = f"{movie_id}_{source}"

            if self.is_postgres:
                sql = """
                INSERT INTO pending_downloads (id, movie_id, title, source, added_at, last_checked_at, status, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    last_checked_at = EXCLUDED.last_checked_at,
                    note = EXCLUDED.note
                """
            else:
                sql = """
                INSERT INTO pending_downloads (id, movie_id, title, source, added_at, last_checked_at, status, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    last_checked_at = excluded.last_checked_at,
                    note = excluded.note
                """
            cursor.execute(sql, (id_key, movie_id, title, source, now, now, 'pending', note))
            conn.commit()
            return True
        except Exception as e:
            logger.error("Save pending download %s failed: %s", display_value_for_log(movie_id), type(e).__name__)
            return False
        finally:
            self._release_connection(conn)

    def get_pending_downloads_list(self, limit: int = 50) -> List[Dict]:
        """Return pending movies from the pending_downloads table."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = "SELECT * FROM pending_downloads WHERE status = %s ORDER BY added_at DESC LIMIT %s" if self.is_postgres else "SELECT * FROM pending_downloads WHERE status = ? ORDER BY added_at DESC LIMIT ?"
            cursor.execute(sql, ('pending', limit))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            self._release_connection(conn)

    def update_pending_download_status(self, movie_id: str, status: str, magnet: str = None, note: str = None):
        """Update pending download status."""
        import time
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = time.time()

            if magnet:
                sql = "UPDATE pending_downloads SET status = %s, magnet = %s, last_checked_at = %s WHERE movie_id = %s" if self.is_postgres else "UPDATE pending_downloads SET status = ?, magnet = ?, last_checked_at = ? WHERE movie_id = ?"
                cursor.execute(sql, (status, magnet, now, movie_id))
            elif note:
                sql = "UPDATE pending_downloads SET status = %s, note = %s, last_checked_at = %s, retry_count = retry_count + 1 WHERE movie_id = %s" if self.is_postgres else "UPDATE pending_downloads SET status = ?, note = ?, last_checked_at = ?, retry_count = retry_count + 1 WHERE movie_id = ?"
                cursor.execute(sql, (status, note, now, movie_id))
            else:
                sql = "UPDATE pending_downloads SET status = %s, last_checked_at = %s WHERE movie_id = %s" if self.is_postgres else "UPDATE pending_downloads SET status = ?, last_checked_at = ? WHERE movie_id = ?"
                cursor.execute(sql, (status, now, movie_id))
            conn.commit()
        finally:
            self._release_connection(conn)

    def get_movies_by_actress_id_indexed(
        self,
        actress_id: str,
        limit: int = 30,
        offset: int = 0,
        actress_name: str = None,
    ) -> List[Dict]:
        """Return movies for an actress from the indexed relation table."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            if self.is_postgres:
                where_sql = "ma.actress_id = %s"
                params = [actress_id]
                if actress_name:
                    where_sql = f"({where_sql} OR ma.actress_name = %s)"
                    params.append(actress_name)
                sql = """
                    SELECT
                        m.id, m.title, m.original_title, m.cover, m.release_date, m.detail_json,
                        ma.actress_id, ma.actress_name
                    FROM movie_actresses ma
                    JOIN movies m ON m.id = ma.movie_id
                    WHERE """ + where_sql + """
                    ORDER BY m.release_date DESC
                    LIMIT %s OFFSET %s
                """
                cursor.execute(sql, tuple(params + [limit, offset]))
            else:
                where_sql = "ma.actress_id = ?"
                params = [actress_id]
                if actress_name:
                    where_sql = f"({where_sql} OR ma.actress_name = ?)"
                    params.append(actress_name)
                sql = """
                    SELECT
                        m.id, m.title, m.original_title, m.cover, m.release_date, m.detail_json,
                        ma.actress_id, ma.actress_name
                    FROM movie_actresses ma
                    JOIN movies m ON m.id = ma.movie_id
                    WHERE """ + where_sql + """
                    ORDER BY m.release_date DESC
                    LIMIT ? OFFSET ?
                """
                cursor.execute(sql, tuple(params + [limit, offset]))

            movies = []
            for row in cursor.fetchall():
                data = dict(row)
                matched_id = data.pop("actress_id", actress_id)
                matched_name = data.pop("actress_name", actress_name or "")
                data["stars"] = [{"id": matched_id, "name": matched_name}]
                movies.append(data)
            return movies
        finally:
            self._release_connection(conn)

    def get_movies_by_actress_id(self, actress_id: str, limit: int = 30, offset: int = 0) -> List[Dict]:
        """
        Find movies that include the actress id in detail_json stars data.
        """
        import json
        conn = self.get_connection()
        try:
            cursor = conn.cursor()

            if self.is_postgres:
                sql = """
                    SELECT id, title, original_title, cover, release_date, detail_json
                    FROM movies
                    WHERE detail_json @> %s::jsonb
                       OR detail_json::text LIKE %s
                    ORDER BY release_date DESC
                    LIMIT %s OFFSET %s
                """
                # PostgreSQL: use JSONB contains when possible and LIKE as fallback.
                jsonb_pattern = json.dumps({"stars": [{"id": actress_id}]})
                cursor.execute(sql, (jsonb_pattern, f'%{actress_id}%', limit, offset))
            else:
                # SQLite: use LIKE against stored detail JSON.
                sql = """
                    SELECT id, title, original_title, cover, release_date, detail_json
                    FROM movies
                    WHERE detail_json LIKE ?
                    ORDER BY release_date DESC
                    LIMIT ? OFFSET ?
                """
                cursor.execute(sql, (f'%{actress_id}%', limit, offset))

            rows = cursor.fetchall()
            movies = []
            for row in rows:
                data = dict(row)
                # 瑙ｆ瀽 detail_json
                if data.get('detail_json'):
                    try:
                        detail = json.loads(data['detail_json']) if isinstance(data['detail_json'], str) else data['detail_json']
                        stars = detail.get('stars', [])
                        actress_in_movie = [s for s in stars if s.get('id') == actress_id]
                        if actress_in_movie:
                            data['stars'] = actress_in_movie
                            movies.append(data)
                    except (TypeError, json.JSONDecodeError, AttributeError) as exc:
                        logging.debug("Invalid detail_json while loading actress %s movies: %s", display_value_for_log(actress_id), type(exc).__name__)

            return movies
        finally:
            self._release_connection(conn)

    def get_library_movies(self, is_watched=None, is_liked=None, query=None, limit=50, offset=0):
        """Return library movies with optional watched/favorite filters."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            sql = """
                SELECT m.*, lf.file_path, lf.file_size, lf.added_at
                FROM movies m
                LEFT JOIN local_files lf ON m.id = lf.movie_id
                WHERE 1=1
            """
            params = []
            if is_watched is not None:
                if self.is_postgres:
                    sql += " AND m.is_watched = %s"
                    params.append(is_watched)
                else:
                    sql += " AND m.is_watched = ?"
                    params.append(1 if is_watched else 0)
            if is_liked is not None:
                if self.is_postgres:
                    sql += " AND m.is_liked = %s"
                    params.append(is_liked)
                else:
                    sql += " AND m.is_liked = ?"
                    params.append(1 if is_liked else 0)
            if query:
                sql += " AND (m.title LIKE %s OR m.id LIKE %s)" if self.is_postgres else " AND (m.title LIKE ? OR m.id LIKE ?)"
                params.extend([f"%{query}%", f"%{query}%"])
            sql += " ORDER BY m.scraped_at DESC LIMIT %s OFFSET %s" if self.is_postgres else " ORDER BY m.scraped_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            cursor.execute(sql, tuple(params))
            return [dict(r) for r in cursor.fetchall()]
        finally:
            self._release_connection(conn)

    def get_movie_category_distribution(self, limit: int = 8) -> List[Dict[str, Any]]:
        """Aggregate local movie genre tags for the category distribution chart."""
        safe_limit = max(1, min(int(limit or 8), 20))
        now = time.time()
        cached = getattr(self, "_movie_category_distribution_cache", None)
        if cached and cached[0] > now and cached[1].get("limit") == safe_limit:
            return cached[1]["data"]
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT detail_json FROM movies WHERE detail_json LIKE '%\"genres\"%'")
            counts = {}
            for row in cursor.fetchall():
                raw = row["detail_json"] if not isinstance(row, tuple) else row[0]
                try:
                    detail = json.loads(raw) if isinstance(raw, str) else raw
                    genres = detail.get("genres", []) if isinstance(detail, dict) else []
                except (TypeError, json.JSONDecodeError, AttributeError):
                    genres = []
                primary_name = next(
                    (
                        str(item.get("name") or item.get("name_cn") or item.get("id") or "").strip()
                        for item in genres
                        if isinstance(item, dict)
                        and str(item.get("name") or item.get("name_cn") or item.get("id") or "").strip()
                    ),
                    "",
                )
                if not primary_name:
                    continue
                counts[primary_name] = counts.get(primary_name, 0) + 1

            top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            selected = top[:safe_limit]
            other_count = sum(value for _, value in top[safe_limit:])
            result = [{"name": name, "count": count} for name, count in selected]
            if other_count:
                result.append({"name": "其他", "count": other_count})
            self._movie_category_distribution_cache = (now + 600, {"limit": safe_limit, "data": result})
            return result
        finally:
            self._release_connection(conn)

    def delete_movie_full(self, movie_id, delete_file=False):
        """Delete movie metadata and optionally remove managed local media file."""
        import os as _os
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            file_path = None
            if delete_file:
                sql_p = "SELECT file_path FROM local_files WHERE movie_id = %s" if self.is_postgres else "SELECT file_path FROM local_files WHERE movie_id = ?"
                cursor.execute(sql_p, (movie_id,))
                row = cursor.fetchone()
                if row:
                    file_path = row["file_path"] if not isinstance(row, tuple) else row[0]
            ph = "%s" if self.is_postgres else "?"
            cursor.execute(f"DELETE FROM movies WHERE id = {ph}", (movie_id,))
            cursor.execute(f"DELETE FROM local_files WHERE movie_id = {ph}", (movie_id,))
            cursor.execute(f"DELETE FROM downloads WHERE movie_id = {ph}", (movie_id,))
            if delete_file and file_path and _os.path.exists(file_path):
                if not self.is_managed_file_path(file_path):
                    logging.warning("Skipped deleting unmanaged movie file path: %s", display_path_for_log(file_path))
                else:
                    try:
                        _os.remove(file_path)
                    except Exception as exc:
                        logging.warning("Failed to delete movie file %s: %s", display_path_for_log(file_path), type(exc).__name__)
            conn.commit()
            return True
        except Exception as exc:
            logging.error("Failed to delete movie metadata for %s: %s", display_value_for_log(movie_id), type(exc).__name__)
            return False
        finally:
            self._release_connection(conn)

# Global instance used throughout the project
db = Database()
