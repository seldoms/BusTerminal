import os

ALLOW_SCHEMA_MIGRATION = "BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION"

DB_NAME = "busterminal"
DB_USER = "busterminal"
DB_HOST = "localhost"
DB_PORT = "5432"

def add_column():
    if os.getenv(ALLOW_SCHEMA_MIGRATION, "").lower() not in {"1", "true", "yes", "on"}:
        print(f"Schema migration skipped. Set {ALLOW_SCHEMA_MIGRATION}=true to alter the database.")
        return 0

    import psycopg2
    from dotenv import load_dotenv

    # Load env to get password if needed
    load_dotenv('backend/.env')
    db_pass = os.getenv("POSTGRES_PASSWORD")
    if not db_pass:
        print("POSTGRES_PASSWORD is required when schema migration is enabled.")
        return 1

    print("Attempting to add 'aliases' column to 'actresses' table...")
    try:
        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=db_pass,
            host=DB_HOST,
            port=DB_PORT
        )
        conn.autocommit = True
        cur = conn.cursor()
        
        # Check if column exists
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='actresses' AND column_name='aliases';")
        if cur.fetchone():
            print("Column 'aliases' already exists.")
        else:
            cur.execute("ALTER TABLE actresses ADD COLUMN aliases TEXT;")
            print("Successfully added 'aliases' column.")
            
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Error: {type(e).__name__}")
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(add_column())
