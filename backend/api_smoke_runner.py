"""Run reusable, read-only smoke checks against a BusTerminal deployment."""

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from api_contract import RISK_READ_ONLY, classify_operation
from api_smoke_cases import MODULE_CASES, SmokeCase, selected_cases


@dataclass(frozen=True)
class SmokeResult:
    module: str
    name: str
    path: str
    status: str
    http_status: Optional[int]
    elapsed_ms: int
    detail: str


def normalize_base_url(value: str) -> str:
    base_url = str(value or "").strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url_must_be_http_or_https")
    return base_url


def validate_case_contract(schema: Dict[str, Any], case: SmokeCase) -> Optional[str]:
    operation = schema.get("paths", {}).get(case.path, {}).get("get")
    if not operation:
        return "openapi_get_operation_missing"

    declared_params = {
        item.get("name")
        for item in operation.get("parameters", [])
        if item.get("in") == "query"
    }
    unknown_params = sorted(set(case.params) - declared_params)
    if unknown_params:
        return f"openapi_query_parameter_missing:{','.join(unknown_params)}"
    return None


def validate_payload(payload: Any, case: SmokeCase) -> Optional[str]:
    if not isinstance(payload, dict):
        return "response_is_not_object"
    missing = [key for key in case.required_keys if key not in payload]
    if missing:
        return f"response_keys_missing:{','.join(missing)}"
    if case.require_success and payload.get("success") is not True:
        message = payload.get("message") or payload.get("detail") or "success_false"
        safe_message = str(message) if isinstance(message, (str, int, float, bool)) else "success_false"
        return f"business_failure:{safe_message}"
    return None


def run_case(
    session: requests.Session,
    base_url: str,
    module_name: str,
    case: SmokeCase,
    schema: Dict[str, Any],
    default_timeout: float,
) -> SmokeResult:
    contract_error = validate_case_contract(schema, case)
    if contract_error:
        return SmokeResult(module_name, case.name, case.path, "FAIL", None, 0, contract_error)
    if classify_operation("GET", case.path) != RISK_READ_ONLY:
        return SmokeResult(module_name, case.name, case.path, "FAIL", None, 0, "operation_is_not_read_only")

    started_at = time.perf_counter()
    try:
        response = session.get(
            f"{base_url}{case.path}",
            params=dict(case.params),
            timeout=case.timeout_seconds or default_timeout,
        )
        elapsed_ms = round((time.perf_counter() - started_at) * 1000)
        if response.status_code != 200:
            return SmokeResult(
                module_name,
                case.name,
                case.path,
                "FAIL",
                response.status_code,
                elapsed_ms,
                f"unexpected_http_status:{response.status_code}",
            )
        try:
            payload = response.json()
        except ValueError:
            return SmokeResult(module_name, case.name, case.path, "FAIL", 200, elapsed_ms, "response_is_not_json")
        payload_error = validate_payload(payload, case)
        return SmokeResult(
            module_name,
            case.name,
            case.path,
            "FAIL" if payload_error else "PASS",
            200,
            elapsed_ms,
            payload_error or "ok",
        )
    except requests.RequestException as exc:
        elapsed_ms = round((time.perf_counter() - started_at) * 1000)
        return SmokeResult(
            module_name,
            case.name,
            case.path,
            "FAIL",
            None,
            elapsed_ms,
            type(exc).__name__,
        )


def run_modules(base_url: str, modules: Tuple[str, ...], timeout: float) -> List[SmokeResult]:
    with requests.Session() as session:
        schema_response = session.get(f"{base_url}/openapi.json", timeout=timeout)
        schema_response.raise_for_status()
        schema = schema_response.json()
        return [
            run_case(session, base_url, module_name, case, schema, timeout)
            for module_name, case in selected_cases(modules)
        ]


def print_text_report(results: Iterable[SmokeResult]) -> None:
    current_module = None
    for result in results:
        if result.module != current_module:
            current_module = result.module
            print(f"\n[{current_module}]")
        http_status = result.http_status if result.http_status is not None else "-"
        print(
            f"{result.status:4} {result.name:28} "
            f"HTTP {str(http_status):>3} {result.elapsed_ms:>6} ms  {result.detail}"
        )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run grouped read-only BusTerminal API smoke checks")
    parser.add_argument(
        "--base-url",
        default=os.getenv("BUS_TERMINAL_API_URL", "http://127.0.0.1:8000"),
        help="BusTerminal API base URL",
    )
    parser.add_argument(
        "--module",
        action="append",
        choices=tuple(MODULE_CASES),
        dest="modules",
        help="Business module to check; repeat to select multiple modules",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="OpenAPI and default request timeout")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        base_url = normalize_base_url(args.base_url)
        modules = tuple(args.modules or MODULE_CASES.keys())
        results = run_modules(base_url, modules, args.timeout)
    except (ValueError, requests.RequestException, json.JSONDecodeError) as exc:
        print(f"smoke runner startup failed: {type(exc).__name__}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
    else:
        print_text_report(results)
        passed = sum(result.status == "PASS" for result in results)
        print(f"\nsummary: {passed}/{len(results)} passed")
    return 1 if any(result.status != "PASS" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
