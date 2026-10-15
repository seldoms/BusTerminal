import sqlite3
import os
import hashlib
from pathlib import Path

ALLOW_DESTRUCTIVE_MAINTENANCE = "BUS_TERMINAL_ALLOW_DESTRUCTIVE_MAINTENANCE"
ALLOW_UNSAFE_DB_VALUES = "BUS_TERMINAL_SHOW_UNSAFE_DB_VALUES"
ALLOW_UNSAFE_PATH_OUTPUT = "BUS_TERMINAL_SHOW_UNSAFE_PATHS"
SENSITIVE_KEY_PARTS = ("key", "secret", "password", "pass", "token", "host", "url", "user_id")


def get_db_path():
    if os.getenv("DB_PATH"):
        return Path(os.getenv("DB_PATH")).expanduser().resolve()
    data_dir = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
    return (data_dir / "movies.db").expanduser().resolve()


def unsafe_value_output_enabled():
    return os.getenv(ALLOW_UNSAFE_DB_VALUES, "").lower() in {"1", "true", "yes", "on"}


def unsafe_path_output_enabled():
    return os.getenv(ALLOW_UNSAFE_PATH_OUTPUT, "").lower() in {"1", "true", "yes", "on"}


def display_path(path) -> str:
    path = Path(path)
    if unsafe_path_output_enabled():
        return str(path)
    return f"<path:{path.name}>"


def is_sensitive_key(key):
    return any(part in str(key).lower() for part in SENSITIVE_KEY_PARTS)


def summarize_value(value) -> str:
    text = "" if value is None else str(value)
    if not text:
        return "<empty>"
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"<redacted len={len(text)} sha256={digest}>"


def display_config_value(key, value) -> str:
    if unsafe_value_output_enabled() or not is_sensitive_key(key):
        return str(value or "")
    return summarize_value(value)


def cleanup_db(db_path=None):
    if os.getenv(ALLOW_DESTRUCTIVE_MAINTENANCE, "").lower() not in {"1", "true", "yes", "on"}:
        print(
            "Database cleanup skipped. "
            f"Set {ALLOW_DESTRUCTIVE_MAINTENANCE}=true to update config rows."
        )
        return 0

    db_path = Path(db_path).expanduser().resolve() if db_path else get_db_path()
    if not db_path.exists():
        print(f"DB not found at {display_path(db_path)}")
        return 1

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        # Get all config keys
        cursor.execute('SELECT key, value FROM config')
        rows = cursor.fetchall()

        updated_count = 0
        for key, value in rows:
            if isinstance(value, str) and value.startswith('"') and value.endswith('"'):
                new_value = value[1:-1]
                print(f"Cleaning {key}: {display_config_value(key, value)} -> {display_config_value(key, new_value)}")
                cursor.execute('UPDATE config SET value = ? WHERE key = ?', (new_value, key))
                updated_count += 1

        conn.commit()
        print("Database cleanup completed.")
        return updated_count
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(cleanup_db())
