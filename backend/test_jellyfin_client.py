import logging
import os
import sys


sys.path.append(os.getcwd())


logging.basicConfig(level=logging.INFO)


def live_tests_enabled():
    return os.getenv("BUS_TERMINAL_ALLOW_LIVE_TESTS", "").lower() in {"1", "true", "yes", "on"}


def summarize_item(item):
    item_id = str(item.get("Id") or "").strip()
    return {
        "name_present": bool(str(item.get("Name") or "").strip()),
        "id": f"{item_id[:4]}...{item_id[-4:]}" if len(item_id) > 8 else ("***" if item_id else "missing"),
    }


def test_jellyfin_client():
    if not live_tests_enabled():
        print("Live Jellyfin client test skipped. Set BUS_TERMINAL_ALLOW_LIVE_TESTS=true to run it.")
        return

    from jellyfin_api import jellyfin_client

    print("Reloading config...")
    jellyfin_client.reload_config()

    print("\n--- Testing Favorites ---")
    favorites = jellyfin_client.get_favorite_movies()
    print(f"Found {len(favorites)} favorites.")
    for item in favorites[:5]:
        print(f" - {summarize_item(item)}")

    print("\n--- Testing Watched ---")
    watched = jellyfin_client.get_watched_movies()
    print(f"Found {len(watched)} watched movies.")
    for item in watched[:5]:
        print(f" - {summarize_item(item)}")


if __name__ == "__main__":
    test_jellyfin_client()
