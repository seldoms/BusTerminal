import os


BASE_URL = os.getenv("BUS_TERMINAL_TEST_BASE_URL", "http://127.0.0.1:8001")


def live_tests_enabled():
    return os.getenv("BUS_TERMINAL_ALLOW_LIVE_TESTS", "").lower() in {"1", "true", "yes", "on"}


def summarize_value(value):
    text = str(value or "").strip()
    if not text:
        return "missing"
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}...{text[-4:]}"


def safe_exception_label(exc):
    return type(exc).__name__


def check_api():
    if not live_tests_enabled():
        print("Live API check skipped. Set BUS_TERMINAL_ALLOW_LIVE_TESTS=true to run it.")
        return

    import requests

    url = f'{BASE_URL}/api/jellyfin/library?limit=5'
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json().get('data', [])
        print(f"Total results: {len(data)}")
        for i, item in enumerate(data):
            print(f"-- Item {i} --")
            print(f"ID: {summarize_value(item.get('id'))}")
            print(f"JF_COVER: {'configured' if item.get('jellyfin_cover') else 'missing'}")
            print(f"JF_ID: {summarize_value(item.get('jellyfin_id'))}")
            print(f"ORIG_COVER: {'configured' if item.get('cover') else 'missing'}")
    except Exception as e:
        print(f"Error: {safe_exception_label(e)}")

if __name__ == "__main__":
    check_api()
