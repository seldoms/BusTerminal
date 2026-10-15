import os


BASE_URL = os.getenv("BUS_TERMINAL_TEST_BASE_URL", "http://127.0.0.1:8001")
SENSITIVE_KEYS = {
    "api_key",
    "token",
    "password",
    "secret",
    "id",
    "item_id",
    "jellyfin_id",
    "path",
    "url",
}


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


def sanitize_value(key, value):
    key_text = str(key or "").lower()
    if any(part in key_text for part in SENSITIVE_KEYS):
        return summarize_value(value)
    return value


def sanitize_response(value):
    if isinstance(value, dict):
        return {key: sanitize_response(sanitize_value(key, item)) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_response(item) for item in value]
    return value


def print_response(resp):
    try:
        body = sanitize_response(resp.json())
    except Exception:
        body = "<non-json response>"
    print(f"Status: {resp.status_code}")
    print(f"Response: {body}")


def post_webhook(payload):
    if not live_tests_enabled():
        print("Live webhook test skipped. Set BUS_TERMINAL_ALLOW_LIVE_TESTS=true to run it.")
        return None
    import requests

    return requests.post(f"{BASE_URL}/api/webhooks/jellyfin", json=payload, timeout=15)


def test_favorite():
    print("--- Testing ItemFavorite webhook ---")
    payload = {
        "Name": "ABP-123",
        "NotificationType": "ItemFavorite",
        "ItemType": "Movie",
    }
    try:
        resp = post_webhook(payload)
        if resp is None:
            return
        print_response(resp)
    except Exception as e:
        print(f"Error: {safe_exception_label(e)}")


def test_watched():
    print("\n--- Testing PlaybackStopped webhook ---")
    payload = {
        "Name": "START-487",
        "NotificationType": "PlaybackStopped",
        "PlayedToCompletion": True,
        "ItemType": "Movie",
    }
    try:
        resp = post_webhook(payload)
        if resp is None:
            return
        print_response(resp)
    except Exception as e:
        print(f"Error: {safe_exception_label(e)}")


if __name__ == "__main__":
    test_favorite()
    test_watched()
