import logging
import os
import hashlib

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
ALLOW_DESTRUCTIVE_MAINTENANCE = "BUS_TERMINAL_ALLOW_DESTRUCTIVE_MAINTENANCE"
ALLOW_UNSAFE_DB_VALUES = "BUS_TERMINAL_SHOW_UNSAFE_DB_VALUES"


def unsafe_value_output_enabled():
    return os.getenv(ALLOW_UNSAFE_DB_VALUES, "").lower() in {"1", "true", "yes", "on"}


def summarize_value(value) -> str:
    text = "" if value is None else str(value)
    if not text:
        return "<empty>"
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"<redacted len={len(text)} sha256={digest}>"


def display_value(value) -> str:
    if unsafe_value_output_enabled():
        return str(value or "")[:120]
    return summarize_value(value)

def clean_anomalous_actresses():
    if os.getenv(ALLOW_DESTRUCTIVE_MAINTENANCE, "").lower() not in {"1", "true", "yes", "on"}:
        logger.warning(
            "Destructive actress cleanup skipped. Set %s=true to delete matching database rows.",
            ALLOW_DESTRUCTIVE_MAINTENANCE,
        )
        return 0

    import database

    db = database.Database()
    conn = db.get_connection()
    try:
        cursor = conn.cursor()
        # 查找
        cursor.execute("SELECT id, name FROM actresses WHERE id IS NOT NULL")
        rows = cursor.fetchall()
        
        deleted_count = 0
        for row in rows:
            # 兼容 dict 和 tuple
            if hasattr(row, 'keys'): # RealDictRow
                a_id = row['id']
                name = row['name']
            else:
                a_id = row[0]
                name = row[1]
                
            if db._is_suspicious_actress_name((name or '').strip()):
                logger.info(
                    "Deleting suspicious actress row: ID=%s, Name=%s",
                    display_value(a_id),
                    display_value(name),
                )
                del_cursor = conn.cursor()
                # 使用参数化查询避免引号问题
                if db.is_postgres:
                    del_cursor.execute("DELETE FROM actresses WHERE id = %s", (a_id,))
                else:
                    del_cursor.execute("DELETE FROM actresses WHERE id = ?", (a_id,))
                deleted_count += 1
        
        conn.commit()
        logger.info("Suspicious actress cleanup completed; deleted=%s", deleted_count)
        return deleted_count
    except Exception as e:
        logger.error("Suspicious actress cleanup failed: %s", type(e).__name__)
        return 0
    finally:
        db._release_connection(conn)

if __name__ == "__main__":
    clean_anomalous_actresses()
    raise SystemExit(0)
