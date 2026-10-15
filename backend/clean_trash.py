
import sqlite3
import os
import hashlib
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
ALLOW_DESTRUCTIVE_MAINTENANCE = "BUS_TERMINAL_ALLOW_DESTRUCTIVE_MAINTENANCE"
ALLOW_UNSAFE_DB_VALUES = "BUS_TERMINAL_SHOW_UNSAFE_DB_VALUES"

db_paths = [
    str(ROOT_DIR / 'data' / 'movies.db'),
    str(ROOT_DIR / 'backend' / 'data' / 'movies.db'),
    str(ROOT_DIR / 'backend' / 'movies.db'),
]

trash_keyword = "Amii的接送街"


def is_allowed():
    return os.getenv(ALLOW_DESTRUCTIVE_MAINTENANCE, "").lower() in {"1", "true", "yes", "on"}


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


def display_path(path) -> str:
    if unsafe_value_output_enabled():
        return str(path)
    return f"<db {summarize_value(Path(path).name)}>"


def clean_trash():
    if not is_allowed():
        print(
            "Destructive maintenance skipped. "
            f"Set {ALLOW_DESTRUCTIVE_MAINTENANCE}=true to delete matching database rows."
        )
        return 0

    total_deleted = 0
    for path in db_paths:
        if not os.path.exists(path):
            continue

        print(f"\nCleaning database: {display_path(path)}")
        conn = sqlite3.connect(path)
        cursor = conn.cursor()
        try:
            # 1. Clean actresses table
            cursor.execute("SELECT id, name, aliases FROM actresses")
            rows = cursor.fetchall()

            deleted_ids = []

            for row in rows:
                a_id, name, aliases = row
                name = name or ""
                aliases = aliases or ""

                # If name is trash or aliases is huge trash
                if trash_keyword in name or trash_keyword in aliases or len(name) > 20 or len(aliases) > 500:
                    print(f"  Deleting actress ID={display_value(a_id)}, Name={display_value(name)}")
                    deleted_ids.append(a_id)

            for a_id in deleted_ids:
                cursor.execute("DELETE FROM actresses WHERE id=?", (a_id,))
            total_deleted += len(deleted_ids)

            # 2. Clean translations table
            cursor.execute("DELETE FROM translations WHERE translated_text LIKE ?", (f"%{trash_keyword}%",))
            total_deleted += cursor.rowcount
            print(f"  Deleted {cursor.rowcount} translations containing configured trash keyword")

            # 3. Clean any other tables that might have it (optional but good)
            # Search in movies table too? Just in case it's a movie title error
            cursor.execute("DELETE FROM movies WHERE title LIKE ?", (f"%{trash_keyword}%",))
            total_deleted += cursor.rowcount
            print(f"  Deleted {cursor.rowcount} movies containing configured trash keyword")

            conn.commit()
            print(f"Done cleaning {display_path(path)}")
        finally:
            conn.close()

    return total_deleted


if __name__ == "__main__":
    clean_trash()
    raise SystemExit(0)
