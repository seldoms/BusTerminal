import json
import os


BASE_URL = os.getenv("BUS_TERMINAL_TEST_BASE_URL", "http://127.0.0.1:8001")
SENSITIVE_KEYS = {
    "id",
    "jellyfin_id",
    "path",
    "file_path",
    "url",
    "cover",
    "jellyfin_cover",
    "poster",
    "image",
    "api_key",
    "token",
    "password",
    "secret",
}


def live_tests_enabled():
    return os.getenv("BUS_TERMINAL_ALLOW_LIVE_TESTS", "").lower() in {"1", "true", "yes", "on"}


def raw_output_enabled():
    return os.getenv("BUS_TERMINAL_SHOW_RAW_API_RESPONSE", "").lower() in {"1", "true", "yes", "on"}


def unsafe_raw_output_enabled():
    return os.getenv("BUS_TERMINAL_SHOW_UNSAFE_RAW_API_RESPONSE", "").lower() in {"1", "true", "yes", "on"}


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


def check_api_raw():
    if not live_tests_enabled():
        print("Live API check skipped. Set BUS_TERMINAL_ALLOW_LIVE_TESTS=true to run it.")
        return

    import requests

    try:
        r = requests.get(f'{BASE_URL}/api/jellyfin/library?limit=5', timeout=15)
        data = r.json()
        if raw_output_enabled():
            output_data = data if unsafe_raw_output_enabled() else sanitize_response(data)
            print(json.dumps(output_data, indent=2, ensure_ascii=False))
            if not unsafe_raw_output_enabled():
                print("Raw API response is sanitized. Set BUS_TERMINAL_SHOW_UNSAFE_RAW_API_RESPONSE=true to print unredacted data.")
            return

        items = data.get("data") or []
        print(f"success={data.get('success')} count={len(items)}")
        if items:
            first = items[0]
            print(f"first_item_keys={sorted(first.keys())}")
            print(f"first_item_has_cover={bool(first.get('cover') or first.get('jellyfin_cover'))}")
            print(f"first_item_id={summarize_value(first.get('id'))}")
            print(f"first_jellyfin_id={summarize_value(first.get('jellyfin_id'))}")
    except Exception as e:
        print(f"Error: {safe_exception_label(e)}")


if __name__ == "__main__":
    check_api_raw()
