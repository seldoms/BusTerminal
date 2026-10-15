import sys
import os
import re

# 把当前目录加入 sys.path
sys.path.append(os.getcwd())

ALLOW_LIVE_WRITES = "BUS_TERMINAL_ALLOW_LIVE_WRITES"
CONFIRM_FORCE_SYNC = "BUS_TERMINAL_CONFIRM_FORCE_SYNC"
CONFIRM_FORCE_SYNC_PHRASE = "FORCE_SYNC_JELLYFIN"


def live_writes_allowed():
    return (
        os.getenv(ALLOW_LIVE_WRITES, "").lower() in {"1", "true", "yes", "on"}
        and os.getenv(CONFIRM_FORCE_SYNC, "") == CONFIRM_FORCE_SYNC_PHRASE
    )


def force_sync():
    if not live_writes_allowed():
        print(
            "Force sync skipped. Set "
            f"{ALLOW_LIVE_WRITES}=true and {CONFIRM_FORCE_SYNC}={CONFIRM_FORCE_SYNC_PHRASE} "
            "to read Jellyfin and update the database."
        )
        return {"success": False, "skipped": True}

    from database import db
    from jellyfin_api import jellyfin_client
    from scraper import scraper

    print("Starting force sync from Jellyfin...")
    jellyfin_client.reload_config()
    
    # 1. 同步收藏
    favs = jellyfin_client.get_favorite_movies()
    print(f"Found {len(favs)} favorites in Jellyfin.")
    
    for item in favs:
        name = item.get("Name", "")
        movie_id = None
        match = re.search(r'([A-Za-z0-9]+-[0-9]+)', name)
        if match:
            movie_id = match.group(1).upper()
        
        if movie_id:
            print(f"Syncing movie: {movie_id}")
            # 标记为已收藏
            try:
                # 获取/保存 详情
                scraper.get_movie_detail(f"/{movie_id}")
                # 手动更新收藏状态
                conn = db.get_connection()
                try:
                    cursor = conn.cursor()
                    if db.is_postgres:
                        cursor.execute("UPDATE movies SET is_liked = %s WHERE id = %s", (True, movie_id))
                    else:
                        cursor.execute("UPDATE movies SET is_liked = 1 WHERE id = ?", (movie_id,))
                    conn.commit()
                finally:
                    db._release_connection(conn)
            except Exception as e:
                print(f"Error syncing {movie_id}: {e}")

    print("Force sync completed.")
    return {"success": True, "skipped": False}

if __name__ == "__main__":
    force_sync()
