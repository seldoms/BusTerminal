import sqlite3
import time
import os
import sys
import configparser
from pathlib import Path

ALLOW_CONFIG_WRITE = "BUS_TERMINAL_ALLOW_CONFIG_WRITE"
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

def load_jellyfin_config():
    """Load Jellyfin config from environment or local INI."""
    # 1. Environment variables
    host = os.getenv("JELLYFIN_HOST")
    api_key = os.getenv("JELLYFIN_API_KEY")
    user_id = os.getenv("JELLYFIN_USER_ID")
    
    if all([host, api_key, user_id]):
        return host, api_key, user_id
    
    # 2. Local INI config file for debugging
    config_file = os.path.join(os.path.dirname(__file__), 'jellyfin_config.ini')
    if os.path.exists(config_file):
        try:
            config = configparser.ConfigParser()
            config.read(config_file, encoding='utf-8')
            if 'jellyfin' in config:
                host = config['jellyfin'].get('host', '')
                api_key = config['jellyfin'].get('api_key', '')
                user_id = config['jellyfin'].get('user_id', '')
                if all([host, api_key, user_id]):
                    print(f"Loaded Jellyfin config from {display_path(config_file)}")
                    return host, api_key, user_id
        except Exception as e:
            print(f"Warning: Failed to read config file: {type(e).__name__}")
    
    print("Error: Missing Jellyfin configuration")
    print("Required: JELLYFIN_HOST, JELLYFIN_USER_ID, JELLYFIN_API_KEY")
    sys.exit(1)

def update_jellyfin_config():
    if os.getenv(ALLOW_CONFIG_WRITE, "").lower() not in {"1", "true", "yes", "on"}:
        print(f"Jellyfin config write skipped. Set {ALLOW_CONFIG_WRITE}=true to update the database.")
        return 0

    conn = None
    try:
        host, api_key, user_id = load_jellyfin_config()
        user_id = os.getenv("JELLYFIN_USER_ID", user_id)
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        configs = [
            ('jellyfin_host', host),
            ('jellyfin_api_key', api_key),
            ('jellyfin_user_id', user_id)
        ]
        
        now = time.time()
        for key, value in configs:
            cursor.execute("""
                INSERT OR REPLACE INTO config (key, value, updated_at)
                VALUES (?, ?, ?)
            """, (key, value, now))
            
        conn.commit()
        print(f"Jellyfin configuration updated in {display_path(db_path)}")
    except Exception as e:
        print(f"Error updating config: {type(e).__name__}")
        return 1
    finally:
        if conn is not None:
            conn.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(update_jellyfin_config())
