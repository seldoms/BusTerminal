import os
import sqlite3
from pathlib import Path


SENSITIVE_KEY_PARTS = ("key", "secret", "password", "pass", "token", "host", "url", "user_id")
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


def mask_value(key, value):
    text = "" if value is None else str(value)
    if not any(part in str(key).lower() for part in SENSITIVE_KEY_PARTS):
        return text
    if unsafe_db_value_output_enabled():
        return text
    return "configured" if text.strip() else "missing"


def get_db_path():
    data_dir = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
    return data_dir / "movies.db"


def main() -> int:
    db_path = get_db_path()
    if not db_path.exists():
        print(f"DB not found at {display_path(db_path)}")
        return 1

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT key, value FROM config WHERE key LIKE "jellyfin%"')
        for key, value in cursor.fetchall():
            print(f"{key}: {mask_value(key, value)}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
