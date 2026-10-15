
import sys
import os
sys.path.append("/app")
from database import db
from security import hash_password


def main() -> int:
    print("Checking data...")
    try:
        movies = db.search_movies(limit=5)
        print(f"Movies found: {len(movies)}")
    except Exception as e:
        print(f"Error searching movies: {e}")

    if not db.has_any_user():
        password = os.getenv("BUS_TERMINAL_ADMIN_PASSWORD")
        if not password:
            print("No users found. Set BUS_TERMINAL_ADMIN_PASSWORD to create the initial admin user.")
            return 0

        print("Creating initial admin user...")
        if db.create_user("admin", hash_password(password)):
            print("User admin created.")
        else:
            print("User admin already exists.")
    else:
        print("User already exists.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
