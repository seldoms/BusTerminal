
import sqlite3
import os
from pathlib import Path

ALLOW_SCHEMA_MIGRATION = "BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION"
ALLOW_UNSAFE_PATH_OUTPUT = "BUS_TERMINAL_SHOW_UNSAFE_PATHS"


def unsafe_path_output_enabled():
    return os.getenv(ALLOW_UNSAFE_PATH_OUTPUT, "").lower() in {"1", "true", "yes", "on"}


def display_path(path) -> str:
    path = Path(path)
    if unsafe_path_output_enabled():
        return str(path)
    return f"<path:{path.name}>"


def get_db_path():
    if os.getenv("DB_PATH"):
        return Path(os.getenv("DB_PATH")).expanduser().resolve()
    data_dir = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
    return (data_dir / "movies.db").expanduser().resolve()


def migrate():
    if os.getenv(ALLOW_SCHEMA_MIGRATION, "").lower() not in {"1", "true", "yes", "on"}:
        print(f"Schema migration skipped. Set {ALLOW_SCHEMA_MIGRATION}=true to alter the database.")
        return 0

    db_path = get_db_path()
    if not db_path.exists():
        print(f"DB not found at {display_path(db_path)}")
        return 1

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()

        print("Checking actresses table...")
        try:
            cursor.execute("SELECT created_at FROM actresses LIMIT 1")
            print("Column created_at already exists in actresses.")
        except sqlite3.OperationalError:
            print("Adding created_at to actresses...")
            cursor.execute("ALTER TABLE actresses ADD COLUMN created_at REAL")
            cursor.execute("UPDATE actresses SET created_at = last_updated WHERE created_at IS NULL")
            conn.commit()
            print("Migrated actresses.")

        print("Checking movies table...")
        try:
            cursor.execute("SELECT created_at FROM movies LIMIT 1")
            print("Column created_at already exists in movies.")
        except sqlite3.OperationalError:
            print("Adding created_at to movies...")
            cursor.execute("ALTER TABLE movies ADD COLUMN created_at REAL")
            cursor.execute("UPDATE movies SET created_at = scraped_at WHERE created_at IS NULL")
            conn.commit()
            print("Migrated movies.")
    finally:
        conn.close()
    print("Migration complete.")
    return 0

if __name__ == "__main__":
    raise SystemExit(migrate())
