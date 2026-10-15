import time
import statistics
import os

BASE_URL = os.getenv("BUS_TERMINAL_TEST_BASE_URL", "http://127.0.0.1:8001")


def live_tests_enabled():
    return os.getenv("BUS_TERMINAL_ALLOW_LIVE_TESTS", "").lower() in {"1", "true", "yes", "on"}

def benchmark_endpoint(name, url, iterations=5):
    import requests

    print(f"Benchmarking {name} ({url})...")
    times = []
    for i in range(iterations):
        start = time.time()
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            end = time.time()
            duration = end - start
            times.append(duration)
            print(f"  Iteration {i+1}: {duration:.2f}s")
        except Exception as e:
            print(f"  Iteration {i+1} failed: {e}")
    
    if times:
        avg = statistics.mean(times)
        min_time = min(times)
        max_time = max(times)
        print(f"  Average: {avg:.2f}s (Min: {min_time:.2f}s, Max: {max_time:.2f}s)")
        return avg
    return None

if __name__ == "__main__":
    if not live_tests_enabled():
        print("Live benchmark skipped. Set BUS_TERMINAL_ALLOW_LIVE_TESTS=true to run it.")
        raise SystemExit(0)

    # Test navigation
    benchmark_endpoint("Navigation", f"{BASE_URL}/api/navigation")
    
    # Test movies (a common category)
    category_url = "https://www.javbus.com/uncensored"
    benchmark_endpoint("Movies List", f"{BASE_URL}/api/movies?url={category_url}")
    
    # Test cached movies (second run)
    print("\nVerifying cache...")
    benchmark_endpoint("Movies List (Cached)", f"{BASE_URL}/api/movies?url={category_url}")
