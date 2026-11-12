import json
import os
import tempfile
from pathlib import Path


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


def summarize_value(value):
    text = str(value or "").strip()
    if not text:
        return "missing"
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}...{text[-4:]}"


def safe_exception_label(exc):
    return type(exc).__name__


def raw_file_output_enabled():
    return os.getenv("BUS_TERMINAL_WRITE_RAW_API_RESPONSE", "").lower() in {"1", "true", "yes", "on"}


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


def check_api_to_file():
    if not live_tests_enabled():
        print("Live API check skipped. Set BUS_TERMINAL_ALLOW_LIVE_TESTS=true to run it.")
        return

    import requests

    try:
        r = requests.get(f'{BASE_URL}/api/jellyfin/library?limit=10', timeout=15)
        data = r.json()
        output_path = Path(tempfile.gettempdir()) / 'busterminal_api_response_debug.json'
        output_data = data if raw_file_output_enabled() else sanitize_response(data)
        with output_path.open('w', encoding='utf-8') as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)

        items = data.get('data', [])
        mode = "raw" if raw_file_output_enabled() else "sanitized"
        print(f"Debug response written to: {output_path} ({mode})")
        print(f"Total items: {len(items)}")
        for i, it in enumerate(items):
            print(
                f"[{i}] ID: {summarize_value(it.get('id'))} | "
                f"JF_COVER: {'configured' if it.get('jellyfin_cover') else 'missing'}"
            )
    except Exception as e:
        print(f"Error: {safe_exception_label(e)}")


if __name__ == "__main__":
    check_api_to_file()
