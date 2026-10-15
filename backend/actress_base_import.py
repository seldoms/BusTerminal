import json
import logging
import os
import sqlite3
from typing import Dict

import config
from database import db

logger = logging.getLogger(__name__)

DEFAULT_ACTRESS_BASE_DB = os.path.join(config.DATA_DIR, "actress_base.db")
IMPORT_SIGNATURE_KEY = "actress_base_import_signature"


def get_actress_base_db_path() -> str:
    return os.getenv("ACTRESS_BASE_DB_PATH") or DEFAULT_ACTRESS_BASE_DB


def _table_exists(cursor, table_name: str) -> bool:
    cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    return cursor.fetchone() is not None


def _row_to_dict(row) -> Dict:
    return {key: row[key] for key in row.keys()}


def _import_actresses(cursor) -> int:
    if not _table_exists(cursor, "actresses"):
        return 0

    cursor.execute("SELECT * FROM actresses")
    count = 0
    for row in cursor.fetchall():
        data = _row_to_dict(row)
        if db.save_actress(data):
            count += 1
    return count


def _import_actress_names(cursor) -> int:
    if not _table_exists(cursor, "actress_names"):
        return 0

    cursor.execute("SELECT actress_id, name, name_type, source FROM actress_names")
    count = 0
    for row in cursor.fetchall():
        data = _row_to_dict(row)
        if db.save_actress_name(
            data.get("actress_id"),
            data.get("name"),
            data.get("name_type"),
            data.get("source"),
        ):
            count += 1
    return count


def _import_movies(cursor) -> int:
    if not _table_exists(cursor, "movies"):
        return 0

    cursor.execute("SELECT id, title, original_title, cover, detail_url, release_date, detail_json FROM movies")
    count = 0
    for row in cursor.fetchall():
        data = _row_to_dict(row)
        detail_json = data.pop("detail_json", None)
        movie = {
            "id": data.get("id"),
            "title": data.get("title"),
            "original_title": data.get("original_title"),
            "cover": data.get("cover"),
            "detail_url": data.get("detail_url"),
            "date": data.get("release_date"),
        }
        if detail_json:
            try:
                movie["detail"] = json.loads(detail_json) if isinstance(detail_json, str) else detail_json
            except Exception:
                movie["detail"] = detail_json
        if db.save_movie(movie):
            count += 1
    return count


def _attached_table_exists(cursor, schema: str, table_name: str) -> bool:
    cursor.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (table_name,))
    return cursor.fetchone() is not None


def _count_rows(cursor, table_name: str) -> int:
    cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
    row = cursor.fetchone()
    return int(row[0] if row else 0)


def _fast_import_sqlite(path: str) -> Dict:
    if os.path.abspath(path) == os.path.abspath(getattr(db, "db_path", "")):
        return {"actresses": 0, "names": 0, "movies": 0, "fast": True, "reason": "same_database"}

    conn = db.get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("ATTACH DATABASE ? AS actress_base", (path,))

        imported = {"actresses": 0, "names": 0, "movies": 0, "fast": True}

        if _attached_table_exists(cursor, "actress_base", "actresses"):
            before = _count_rows(cursor, "actresses")
            cursor.execute(
                """
                INSERT OR IGNORE INTO actresses (
                    id, name, avatar, birth_date, height, cup, bust, waist, hip,
                    hometown, hobby, page_url, bio, wiki_url, aliases, name_en,
                    source, source_id, source_updated_at, video_count,
                    metadata_requested_at, metadata_last_attempt_at, metadata_attempt_count,
                    is_followed, last_updated, created_at
                )
                SELECT
                    id, name, avatar, birth_date, height, cup, bust, waist, hip,
                    hometown, hobby, page_url, bio, wiki_url, aliases, name_en,
                    source, source_id, source_updated_at, video_count,
                    metadata_requested_at, metadata_last_attempt_at, metadata_attempt_count,
                    is_followed, last_updated, created_at
                FROM actress_base.actresses
                """
            )
            imported["actresses"] = _count_rows(cursor, "actresses") - before

        if _attached_table_exists(cursor, "actress_base", "actress_names"):
            before = _count_rows(cursor, "actress_names")
            cursor.execute(
                """
                INSERT OR IGNORE INTO actress_names (actress_id, name, name_type, source, created_at)
                SELECT actress_id, name, name_type, source, created_at
                FROM actress_base.actress_names
                """
            )
            imported["names"] = _count_rows(cursor, "actress_names") - before

        if _attached_table_exists(cursor, "actress_base", "movies"):
            before = _count_rows(cursor, "movies")
            # 2026-09-24 防二次污染：原实现是无条件 INSERT OR IGNORE ... SELECT，
            # 只在主键完全相同时才忽略；而基础库番号写法各异（SSD-03 / SSD003 / SSD-000003），
            # 同一部片会以不同 id 重复入库（实测曾因此灌入 18 万条冗余）。
            # 另：源库自身对同一 detail_url 也可能有多条（它自己就脏），仅用 NOT EXISTS 判断
            # 会让同批次的多条一起通过检查、相互撞唯一索引 → 故对同 URL 只取 rowid 最小的一条。
            cursor.execute(
                """
                INSERT OR IGNORE INTO movies (id, title, original_title, cover, detail_url, release_date, scraped_at, detail_json)
                SELECT src.id, src.title, src.original_title, src.cover, src.detail_url,
                       src.release_date, src.scraped_at, src.detail_json
                FROM actress_base.movies src
                WHERE (
                        src.detail_url IS NULL OR src.detail_url = ''
                        OR (
                            NOT EXISTS (SELECT 1 FROM movies m WHERE m.detail_url = src.detail_url)
                            AND src.rowid = (
                                SELECT MIN(s2.rowid) FROM actress_base.movies s2
                                WHERE s2.detail_url = src.detail_url
                            )
                        )
                      )
                """
            )
            imported["movies"] = _count_rows(cursor, "movies") - before

        conn.commit()
        return imported
    finally:
        try:
            conn.execute("DETACH DATABASE actress_base")
        except Exception:
            pass
        db._release_connection(conn)


def import_actress_base_if_available(force: bool = False) -> Dict:
    path = get_actress_base_db_path()
    if not path or not os.path.exists(path):
        return {"imported": False, "reason": "missing", "path": path}

    stat = os.stat(path)
    signature = f"{os.path.abspath(path)}:{int(stat.st_mtime)}:{stat.st_size}"
    if not force and db.get_config(IMPORT_SIGNATURE_KEY) == signature:
        return {"imported": False, "reason": "already_imported", "path": path}

    if not db.is_postgres:
        imported = _fast_import_sqlite(path)
        db.set_config(IMPORT_SIGNATURE_KEY, signature)
        report = {"imported": True, "path": path, **imported}
        logger.info("Actress base DB imported: %s", report)
        return report

    source = sqlite3.connect(path)
    source.row_factory = sqlite3.Row
    try:
        cursor = source.cursor()
        actresses = _import_actresses(cursor)
        names = _import_actress_names(cursor)
        movies = _import_movies(cursor)
        db.set_config(IMPORT_SIGNATURE_KEY, signature)
        report = {
            "imported": True,
            "path": path,
            "actresses": actresses,
            "names": names,
            "movies": movies,
        }
        logger.info("Actress base DB imported: %s", report)
        return report
    finally:
        source.close()
