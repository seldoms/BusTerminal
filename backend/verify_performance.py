import time
import os

BASE_URL = os.getenv("BUS_TERMINAL_TEST_BASE_URL", "http://127.0.0.1:8001")


def live_tests_enabled():
    return os.getenv("BUS_TERMINAL_ALLOW_LIVE_TESTS", "").lower() in {"1", "true", "yes", "on"}

def test_endpoint(name, url):
    import requests

    print(f"Testing {name}...")
    start = time.time()
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        duration = time.time() - start
        print(f"  Success: {data.get('success', False)}")
        print(f"  Duration: {duration:.4f}s")
        if 'count' in data:
            print(f"  Count: {data['count']}")
        return duration
    except Exception as e:
        print(f"  Failed: {e}")
        return None

if __name__ == "__main__":
    if not live_tests_enabled():
        print("Live performance verification skipped. Set BUS_TERMINAL_ALLOW_LIVE_TESTS=true to run it.")
        raise SystemExit(0)

    # 1. Navigation (Should be cached)
    test_endpoint("Navigation (Cached)", f"{BASE_URL}/api/navigation")
    
    # 2. Genres (Should be cached)
    test_endpoint("Genres (Cached)", f"{BASE_URL}/api/genres?type=censored")
    
    # 3. Movies List (A new category to test parallel scraping)
    # Using a rare category to avoid cache hit from previous runs if possible
    # Or just use an existing one to see cache hit
    new_cat_url = "https://www.javbus.com/genre/2f" # "制服" category
    print("\nTesting parallel scraping (Cold)...")
    test_endpoint("Movies List (Cold)", f"{BASE_URL}/api/movies?url={new_cat_url}")
    
    print("\nTesting cache hit...")
    test_endpoint("Movies List (Cached)", f"{BASE_URL}/api/movies?url={new_cat_url}")
