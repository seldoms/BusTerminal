import sqlite3
import json
import os
from pathlib import Path

SENSITIVE_KEY_PARTS = ("key", "secret", "password", "pass", "token", "host", "url", "user_id")
ALLOW_UNSAFE_PATH_OUTPUT = "BUS_TERMINAL_SHOW_UNSAFE_PATHS"


def unsafe_path_output_enabled():
    return os.getenv(ALLOW_UNSAFE_PATH_OUTPUT, "").lower() in {"1", "true", "yes", "on"}


def display_path(path) -> str:
    path = Path(path)
    if unsafe_path_output_enabled():
        return str(path)
    return f"<path:{path.name}>"


def get_db_path():
    data_dir = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
    return data_dir / "movies.db"


def is_sensitive_key(key):
    return any(part in str(key).lower() for part in SENSITIVE_KEY_PARTS)


def mask_value(key, value):
    text = str(value or "")
    if not is_sensitive_key(key):
        return text
    return "configured" if text.strip() else "missing"


def mask_config_value(key, value):
    if not value:
        return "missing"
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return mask_value(key, value)
    if not isinstance(data, dict):
        return "configured"
    masked = {item_key: mask_value(item_key, item_value) for item_key, item_value in data.items()}
    return json.dumps(masked, ensure_ascii=False)


def inspect_db():
    db_path = get_db_path()
    if not os.path.exists(db_path):
        print("DB File not found!")
        return

    print(f"Checking {display_path(db_path)}...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check config table
    try:
        cursor.execute("SELECT key, value, updated_at FROM config")
        rows = cursor.fetchall()
        print("\n[CONFIG TABLE]")
        found_qb = False
        for row in rows:
            key, val, updated = row
            if key == 'qb_config':
                found_qb = True
                print(f"Key: {key}, Updated: {updated}")
                print(f"Value: {mask_config_value(key, val)}")
            else:
                print(f"Key: {key}")
        
        if not found_qb:
            print("\nWARNING: 'qb_config' NOT FOUND in database!")
            
    except Exception as e:
        print(f"Error reading config: {e}")

    # Check download_queue
    try:
        cursor.execute("SELECT COUNT(*) FROM download_queue")
        count = cursor.fetchone()[0]
        print(f"\n[DOWNLOAD QUEUE] Total items: {count}")
    except Exception as e:
        print(f"Error reading download_queue: {e}")

    conn.close()

if __name__ == "__main__":
    inspect_db()
