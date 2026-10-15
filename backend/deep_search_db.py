import sqlite3
import os
import sys
import hashlib
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
ALLOW_UNSAFE_DB_VALUES = "BUS_TERMINAL_SHOW_UNSAFE_DB_VALUES"


def get_db_paths():
    configured_data_dir = Path(os.getenv("DATA_DIR", ROOT_DIR / "data")).expanduser()
    candidates = [
        configured_data_dir / "movies.db",
        ROOT_DIR / "backend" / "data" / "movies.db",
        ROOT_DIR / "backend" / "movies.db",
        ROOT_DIR / "movies.db",
    ]
    return list(dict.fromkeys(path.resolve() for path in candidates))


def quote_identifier(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def unsafe_value_output_enabled() -> bool:
    return os.getenv(ALLOW_UNSAFE_DB_VALUES, "").lower() in {"1", "true", "yes", "on"}


def summarize_value(value) -> str:
    text = "" if value is None else str(value)
    if not text:
        return "<empty>"
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"<redacted len={len(text)} sha256={digest}>"


def deep_search(search_term: str, max_value_length: int = 100) -> int:
    if not search_term:
        print("Usage: python backend/deep_search_db.py <search-term>")
        return 1

    hit_count = 0
    for path in get_db_paths():
        if not path.exists():
            continue

        print(f"\nChecking database: {path}")
        conn = sqlite3.connect(path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [t[0] for t in cursor.fetchall()]

            for table in tables:
                cursor.execute(f"PRAGMA table_info({quote_identifier(table)})")
                columns = [c[1] for c in cursor.fetchall()]

                for col in columns:
                    try:
                        query = f"SELECT {quote_identifier(col)} FROM {quote_identifier(table)} WHERE {quote_identifier(col)} LIKE ? LIMIT 20"
                        cursor.execute(query, (f"%{search_term}%",))
                        results = cursor.fetchall()
                        if results:
                            hit_count += len(results)
                            print(f"  [FOUND] Table: {table}, Column: {col}, Rows: {len(results)}")
                            for r in results:
                                value = str(r[0])
                                if unsafe_value_output_enabled():
                                    print(f"    Value: {value[:max_value_length]}...")
                                else:
                                    print(f"    Value: {summarize_value(value)}")
                    except sqlite3.Error:
                        pass
        except Exception as e:
            print(f"  Error checking {path}: {e}")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    term = sys.argv[1] if len(sys.argv) > 1 else os.getenv("BUS_TERMINAL_DB_SEARCH_TERM", "")
    raise SystemExit(deep_search(term))
