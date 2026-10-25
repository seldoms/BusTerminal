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

def fix_db():
    if os.getenv(ALLOW_SCHEMA_MIGRATION, "").lower() not in {"1", "true", "yes", "on"}:
        print(f"Schema migration skipped. Set {ALLOW_SCHEMA_MIGRATION}=true to alter the database.")
        return 0

    db_path = get_db_path()
    if not db_path.exists():
        print(f"Database not found at {display_path(db_path)}")
        return 1

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()

        # Check columns in actresses
        cursor.execute("PRAGMA table_info(actresses)")
        columns = [row[1] for row in cursor.fetchall()]
        print(f"Current columns: {columns}")

        if 'created_at' not in columns:
            print("Adding created_at column...")
            try:
                cursor.execute("ALTER TABLE actresses ADD COLUMN created_at REAL")
                conn.commit()
                print("Added created_at.")
            except Exception as e:
                print(f"Error adding created_at: {type(e).__name__}")
        else:
            print("created_at already exists.")
    finally:
        conn.close()

    return 0

if __name__ == "__main__":
    raise SystemExit(fix_db())
