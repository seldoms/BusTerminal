import os

from database import db

ALLOW_CACHE_MAINTENANCE = "BUS_TERMINAL_ALLOW_CACHE_MAINTENANCE"


def is_allowed():
    return os.getenv(ALLOW_CACHE_MAINTENANCE, "").lower() in {"1", "true", "yes", "on"}

def clear_nav_cache():
    if not is_allowed():
        print(
            "Navigation cache cleanup skipped. "
            f"Set {ALLOW_CACHE_MAINTENANCE}=true to delete cache rows."
        )
        return 0

    # 清除导航缓存
    conn = db.get_connection()
    try:
        cursor = conn.cursor()

        # 清除所有翻译相关的缓存键
        cache_keys = ['navigation_data', 'genres_censored', 'genres_uncensored']

        # 同时清除所有以 'movies_list_' 开头的缓存（可选，如果电影列表也需要重新翻译）
        cursor.execute("SELECT key FROM cache WHERE key LIKE 'movies_list_%'")
        movie_cache_keys = [row["key"] if hasattr(row, "keys") else row[0] for row in cursor.fetchall()]

        all_keys = cache_keys + movie_cache_keys
        if not all_keys:
            return 0

        placeholder = "%s" if db.is_postgres else "?"
        placeholders = ','.join([placeholder] * len(all_keys))
        cursor.execute(f"DELETE FROM cache WHERE key IN ({placeholders})", all_keys)

        deleted_count = cursor.rowcount
        conn.commit()

        print(f"已清除 {deleted_count} 个缓存项")
        print("清除的缓存类型: 导航、分类、电影列表")
        return deleted_count
    finally:
        db._release_connection(conn)

if __name__ == "__main__":
    clear_nav_cache()
    raise SystemExit(0)
