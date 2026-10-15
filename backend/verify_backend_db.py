import os
import sqlite3
import tempfile
import sys

# Ensure backend directory is in path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from database import Database

def test_indexes_and_queries():
    """验证索引与批量查询。

    ⚠️ 必须在**一次性 sqlite 库**上跑。2026-10-12 之前这里直接 `Database()`，
    在生产上等于连到 PostgreSQL 的真实业务库，于是 `test_batch_m2` / `movie_2`
    两条测试数据被写进了生产的 local_files（added_at=1000）。
    下面先把 config.DATABASE_URL 指向临时文件，再建库，杜绝误写生产。
    """
    print("=== 1. Testing Database Indexes Creation & Query Plans ===")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        import config

        config.DATABASE_URL = f"sqlite:///{db_path}"
        db = Database()
        conn = db.get_connection()
        cursor = conn.cursor()
        
        # Ensure schema and indexes created
        db._ensure_core_tables(cursor)
        db._ensure_indexes(cursor)
        conn.commit()
        
        # Fetch all indexes from sqlite_master
        cursor.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='index'")
        indexes = {row[0]: row[1] for row in cursor.fetchall()}
        
        target_indexes = {
            "idx_movies_watched_liked_last": "movies",
            "idx_actresses_metadata_queue": "actresses",
            "idx_actresses_name": "actresses",
            "idx_subscriptions_created_at": "subscriptions",
            "idx_local_files_added_at": "local_files",
        }
        
        for idx_name, expected_tbl in target_indexes.items():
            if idx_name in indexes and indexes[idx_name] == expected_tbl:
                print(f"[PASS] Index '{idx_name}' exists on table '{expected_tbl}'")
            else:
                print(f"[FAIL] Index '{idx_name}' NOT found on table '{expected_tbl}'! Present indexes: {indexes}")
                sys.exit(1)
                
        # Test EXPLAIN QUERY PLAN for movies query using idx_movies_watched_liked_last
        cursor.execute("""
            EXPLAIN QUERY PLAN 
            SELECT id FROM movies 
            WHERE is_watched = 1 AND is_liked = 0 AND last_watched_at < 1700000000 
            ORDER BY last_watched_at ASC LIMIT 50
        """)
        plan = [dict(row) for row in cursor.fetchall()]
        plan_str = str(plan)
        print(f"Movies Query Plan: {plan_str}")
        if "idx_movies_watched_liked_last" in plan_str:
            print("[PASS] Movies query uses idx_movies_watched_liked_last")
        else:
            print(f"[INFO] Movies query plan detail: {plan_str}")
            
        # Test batch queries get_downloaded_movie_ids and get_local_file_movie_ids
        print("\n=== 2. Testing Batch Database Queries ===")
        db.record_download("test_batch_m1")
        cursor.execute("INSERT OR REPLACE INTO local_files (movie_id, file_path, added_at) VALUES (?, ?, ?)", ("test_batch_m2", "/path/2.mp4", 1000))
        conn.commit()
        
        dl_set = db.get_downloaded_movie_ids(["test_batch_m1", "test_batch_m2", "test_batch_m3"])
        print(f"get_downloaded_movie_ids result: {dl_set}")
        assert "test_batch_m1" in dl_set and "test_batch_m2" not in dl_set, "Batch downloaded query failed!"
        print("[PASS] get_downloaded_movie_ids works as expected")
        
        lf_set = db.get_local_file_movie_ids(["test_batch_m1", "test_batch_m2", "test_batch_m3"])
        print(f"get_local_file_movie_ids result: {lf_set}")
        assert "test_batch_m2" in lf_set and "test_batch_m1" not in lf_set, "Batch local_file query failed!"
        print("[PASS] get_local_file_movie_ids works as expected")
        
        db._release_connection(conn)
    finally:
        try:
            os.remove(db_path)
        except Exception:
            pass

def test_imports_and_routers():
    print("\n=== 3. Testing Core Modules and Async Endpoints Imports ===")
    import download_service
    import jellyfin_service
    import qb_service
    import main
    
    print("[PASS] All modified modules imported cleanly without syntax or import errors")

if __name__ == "__main__":
    test_indexes_and_queries()
    test_imports_and_routers()
    print("\n=== ALL VERIFICATION TESTS PASSED SUCCESSFULLY ===")
