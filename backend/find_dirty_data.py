
import database
import logging
import hashlib
import os

logging.basicConfig(level=logging.INFO)

ALLOW_UNSAFE_DB_VALUES = "BUS_TERMINAL_SHOW_UNSAFE_DB_VALUES"


def unsafe_value_output_enabled() -> bool:
    return os.getenv(ALLOW_UNSAFE_DB_VALUES, "").lower() in {"1", "true", "yes", "on"}


def summarize_value(value) -> str:
    text = "" if value is None else str(value)
    if not text:
        return "<empty>"
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"<redacted len={len(text)} sha256={digest}>"


def display_value(value) -> str:
    if unsafe_value_output_enabled():
        return str(value or "")[:100]
    return summarize_value(value)


def check_dirty_data():
    db = database.Database()
    conn = db.get_connection()
    try:
        cursor = conn.cursor()

        print("--- 检查 actresses 表 ---")
        cursor.execute("SELECT id, name FROM actresses")
        rows = cursor.fetchall()
        dirty_actresses = []
        for r in rows:
            name = r['name'] if hasattr(r, 'keys') else r[1]
            a_id = r['id'] if hasattr(r, 'keys') else r[0]
            if db._is_suspicious_actress_name((name or '').strip()):
                dirty_actresses.append((a_id, name))

        print(f"找到 {len(dirty_actresses)} 条名字异常的女优数据。")
        for a_id, name in dirty_actresses[:20]:
            print(
                f"ID: {display_value(a_id)} | "
                f"长度: {len(name or '')} | "
                f"名字: {display_value(name)}"
            )

        print("\n--- 检查 subscriptions 表 ---")
        cursor.execute("SELECT url, name FROM subscriptions")
        rows = cursor.fetchall()
        dirty_subs = []
        for r in rows:
            name = r['name'] if hasattr(r, 'keys') else r[1]
            url = r['url'] if hasattr(r, 'keys') else r[0]
            if name and len(name) > 20: # 订阅名可能稍长，比如分类名，但如果是女优订阅应该短
                dirty_subs.append((url, name))

        print(f"找到 {len(dirty_subs)} 条名称过长的订阅 (阈值>20)。")
        for url, name in dirty_subs[:20]:
            print(
                f"URL: {display_value(url)} | "
                f"长度: {len(name)} | "
                f"名称: {display_value(name)}"
            )
        return 0
    finally:
        db._release_connection(conn)

if __name__ == "__main__":
    raise SystemExit(check_dirty_data())
