import os
import time
from urllib.parse import urlsplit, urlunsplit

# Usage:
# docker-compose exec backend python fix_pg_schema_created_at.py

DB_URL = os.getenv("DATABASE_URL", "")
ALLOW_SCHEMA_MIGRATION = "BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION"


def mask_database_url(value: str) -> str:
    if not value:
        return "(unset)"
    try:
        parsed = urlsplit(value)
        if parsed.password is None:
            return value
        username = parsed.username or ""
        host = parsed.hostname or ""
        netloc = f"{username}:***@" if username else "***@"
        netloc += host
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    except Exception:
        return "<invalid database url>"

def migrate():
    if os.getenv(ALLOW_SCHEMA_MIGRATION, "").lower() not in {"1", "true", "yes", "on"}:
        print(f"Schema migration skipped. Set {ALLOW_SCHEMA_MIGRATION}=true to alter the database.")
        return 0
    if not DB_URL:
        print("DATABASE_URL is required when schema migration is enabled.")
        return 1

    try:
        import psycopg2
    except ImportError:
        print("psycopg2 not found. Please run this inside the docker container where dependencies are installed.")
        return 1

    print(f"Attempting migration on: {mask_database_url(DB_URL)}")
    try:
        conn = psycopg2.connect(DB_URL)
        conn.autocommit = True
        cursor = conn.cursor()
        
        # 1. Add created_at
        print("1. Adding 'created_at' column to 'actresses'...")
        try:
            cursor.execute("ALTER TABLE actresses ADD COLUMN IF NOT EXISTS created_at DOUBLE PRECISION")
            print("   -> Success.")
        except Exception as e:
            print(f"   -> Error: {type(e).__name__}")

        # 2. Backfill
        print("2. Backfilling NULL 'created_at'...")
        try:
            # Use last_updated if available, else current time
            # Note: last_updated might be null too, so we fallback to time.time()
            now = time.time()
            # We assume last_updated exists because the error didn't complain about it, 
            # but if it doesn't, we might get another error.
            # Safe backfill: just use now for everything null
            cursor.execute("UPDATE actresses SET created_at = %s WHERE created_at IS NULL", (now,))
            print("   -> Success.")
        except Exception as e:
            print(f"   -> Error during backfill: {type(e).__name__}")

        conn.close()
        print("\nMigration script finished.")
        
    except Exception as e:
        print(f"\nCRITICAL CONNECTION ERROR: {type(e).__name__}")
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(migrate())
