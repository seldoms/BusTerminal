import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterable, Tuple

from api_contract import classify_operation, operation_counts


HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def iter_operations(schema: Dict) -> Iterable[Tuple[str, str, Dict]]:
    for path in sorted(schema.get("paths", {})):
        path_item = schema["paths"][path]
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if operation:
                yield method.upper(), path, operation


def render_api_markdown(schema: Dict) -> str:
    info = schema.get("info", {})
    counts = operation_counts(schema)
    lines = [
        "# BusTerminal API",
        "",
        f"> Generated from OpenAPI `{info.get('version', '')}`. Do not edit the endpoint table manually.",
        "",
        "Runtime documentation: `/docs`; machine-readable schema: `/openapi.json`.",
        "",
        "The service currently has no built-in authentication. Keep it on a trusted LAN or behind an authenticated reverse proxy/VPN.",
        "",
        "## Verification levels",
        "",
        "- `read-only`: safe for Docker smoke checks, although some GET requests may refresh local caches or contact upstream sources.",
        "- `local-write`: changes BusTerminal's local database/cache/configuration; verify in an isolated test database.",
        "- `external-write`: may control qBittorrent/Jellyfin or enqueue real downloads; automated tests must mock the external service.",
        "",
        f"Operations: {sum(counts.values())} total; {counts['read-only']} read-only; {counts['local-write']} local-write; {counts['external-write']} external-write.",
        "",
        "## Endpoints",
        "",
        "| Method | Path | Risk | Tags | Parameters | Request body | Summary |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for method, path, operation in iter_operations(schema):
        parameters = []
        for parameter in operation.get("parameters", []):
            suffix = "*" if parameter.get("required") else ""
            parameters.append(f"{parameter.get('in', 'query')}:{parameter.get('name', '')}{suffix}")
        request_body = operation.get("requestBody") or {}
        body_label = "required" if request_body.get("required") else ("optional" if request_body else "-")
        summary = str(operation.get("summary") or operation.get("operationId") or "-").replace("|", "\\|")
        tags = ", ".join(operation.get("tags") or []) or "-"
        lines.append(
            f"| `{method}` | `{path}` | `{classify_operation(method, path)}` | {tags} | "
            f"{', '.join(parameters) or '-'} | {body_label} | {summary} |"
        )
    lines.extend([
        "",
        "`*` marks required path/query/header parameters. Request and response schemas remain authoritative in `/docs` and `/openapi.json`.",
        "",
        "## Test commands",
        "",
        "```powershell",
        ".\\.venv\\Scripts\\python.exe -m pytest backend/test_api_contract.py -q",
        ".\\.venv\\Scripts\\python.exe -m pytest backend/test_lifecycle_scenario.py -q",
        "```",
        "",
        "Docker verification must run read-only endpoints first. External-write operations are covered with mocks and are not replayed against real qBittorrent, Jellyfin, NAS paths, or production data.",
        "",
    ])
    return "\n".join(lines)


def load_schema() -> Dict:
    temp_dir = tempfile.TemporaryDirectory(prefix="busterminal_openapi_")
    os.environ["DATA_DIR"] = temp_dir.name
    os.environ["DATABASE_URL"] = f"sqlite:///{(Path(temp_dir.name) / 'movies.db').as_posix()}"
    os.environ["BUS_TERMINAL_DISABLE_STARTUP_TASKS"] = "true"
    os.environ["BUS_TERMINAL_DISABLE_DOMAIN_ROTATION"] = "true"
    os.environ["EXTRA_DOMAINS"] = ""
    backend_dir = Path(__file__).resolve().parent
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    from main import app

    schema = app.openapi()
    temp_dir.cleanup()
    return schema


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate API.md from the FastAPI OpenAPI schema.")
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "docs" / "API.md"),
    )
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_api_markdown(load_schema()), encoding="utf-8")
    print(f"Generated {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
