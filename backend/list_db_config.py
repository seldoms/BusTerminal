import sqlite3
import os
from pathlib import Path

db_path = Path(os.getenv("DATA_DIR", ".")).expanduser() / "movies.db"

SENSITIVE_KEYS = ("key", "secret", "password", "pass", "token", "qb_config", "host", "url", "user_id")
ALLOW_UNSAFE_DB_VALUES = "BUS_TERMINAL_SHOW_UNSAFE_DB_VALUES"
ALLOW_UNSAFE_PATH_OUTPUT = "BUS_TERMINAL_SHOW_UNSAFE_PATHS"


def unsafe_db_value_output_enabled():
    return os.getenv(ALLOW_UNSAFE_DB_VALUES, "").lower() in {"1", "true", "yes", "on"}


def unsafe_path_output_enabled():
    return os.getenv(ALLOW_UNSAFE_PATH_OUTPUT, "").lower() in {"1", "true", "yes", "on"}


def display_path(path) -> str:
    path = Path(path)
    if unsafe_path_output_enabled():
        return str(path)
    return f"<path:{path.name}>"


def mask_config_value(key, value):
    text = str(value or "")
    if not any(part in key.lower() for part in SENSITIVE_KEYS):
        return text
    if unsafe_db_value_output_enabled():
        return text
    return "configured" if text.strip() else "missing"


def list_config():
    if not db_path.exists():
        print(f"DB not found at {display_path(db_path)}")
        return 1

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM config")
        rows = cursor.fetchall()
        for row in rows:
            print(f"{row['key']}: {mask_config_value(row['key'], row['value'])}")
        return 0
    except Exception as e:
        print(f"Error reading DB: {e}")
        return 1
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    raise SystemExit(list_config())
