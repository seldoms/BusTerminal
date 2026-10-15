import threading
import time
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse


class DomainRouter:
    """Keep source domains replaceable without coupling them to movie data."""

    def __init__(
        self,
        *,
        db: Any,
        source_key: str,
        default_domains: Iterable[str],
        logger: Any,
        failure_threshold: int = 2,
        cooldown_seconds: int = 600,
    ):
        self.db = db
        self.source_key = source_key
        self.logger = logger
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(30, int(cooldown_seconds))
        self.config_key = f"source_domains_v1:{source_key}"
        self._lock = threading.RLock()
        self._states: Dict[str, Dict[str, Any]] = {}
        self._last_persist_at = 0.0
        self._load(default_domains)

    @staticmethod
    def normalize_domain(value: Any) -> Optional[str]:
        raw = str(value or "").strip().rstrip("/")
        if not raw:
            return None
        parsed = urlparse(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        if parsed.username or parsed.password or parsed.path not in {"", "/"}:
            return None
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"

    def _load(self, default_domains: Iterable[str]) -> None:
        defaults = [self.normalize_domain(item) for item in default_domains]
        defaults = [item for item in defaults if item]
        stored = self.db.get_config(self.config_key)
        stored_domains = stored.get("domains") if isinstance(stored, dict) else None

        with self._lock:
            if isinstance(stored_domains, list):
                for raw_state in stored_domains:
                    if not isinstance(raw_state, dict):
                        continue
                    domain = self.normalize_domain(raw_state.get("domain"))
                    if not domain:
                        continue
                    self._states[domain] = self._clean_state(raw_state, domain)

            for index, domain in enumerate(defaults):
                if domain not in self._states:
                    self._states[domain] = self._new_state(domain, index)
                else:
                    self._states[domain]["priority"] = min(
                        int(self._states[domain].get("priority", index)),
                        index,
                    )

            if not self._states:
                raise ValueError(f"No valid domains configured for source {self.source_key}")

    @staticmethod
    def _new_state(domain: str, priority: int = 0, discovered_from: str = "builtin") -> Dict[str, Any]:
        return {
            "domain": domain,
            "priority": priority,
            "enabled": True,
            "score": 50.0,
            "latency_ms": None,
            "consecutive_failures": 0,
            "circuit_open_until": 0.0,
            "last_success_at": None,
            "last_failure_at": None,
            "last_failure_reason": "",
            "discovered_from": discovered_from,
        }

    def _clean_state(self, raw_state: Dict[str, Any], domain: str) -> Dict[str, Any]:
        try:
            priority = int(raw_state.get("priority", 1000) or 1000)
        except (TypeError, ValueError):
            priority = 1000
        state = self._new_state(
            domain,
            priority=priority,
            discovered_from=str(raw_state.get("discovered_from") or "persisted"),
        )
        for key in (
            "enabled",
            "score",
            "latency_ms",
            "consecutive_failures",
            "circuit_open_until",
            "last_success_at",
            "last_failure_at",
            "last_failure_reason",
        ):
            if key in raw_state:
                state[key] = raw_state[key]
        return state

    def _persist_locked(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_persist_at < 30:
            return
        payload = {
            "version": 1,
            "source": self.source_key,
            "updated_at": now,
            "domains": list(self._states.values()),
        }
        try:
            self.db.set_config(self.config_key, payload)
            self._last_persist_at = now
        except Exception as exc:
            self.logger.warning(
                "Persist source domain state failed [%s]: %s",
                self.source_key,
                type(exc).__name__,
            )

    def add_domains(self, domains: Iterable[str], discovered_from: str) -> int:
        added = 0
        with self._lock:
            next_priority = max(
                [int(state.get("priority", 0)) for state in self._states.values()] or [0]
            ) + 1
            for raw_domain in domains:
                domain = self.normalize_domain(raw_domain)
                if not domain or domain in self._states:
                    continue
                self._states[domain] = self._new_state(
                    domain,
                    priority=next_priority,
                    discovered_from=discovered_from,
                )
                next_priority += 1
                added += 1
            if added:
                self._persist_locked(force=True)
        return added

    def candidates(self) -> List[str]:
        now = time.time()
        with self._lock:
            available = [
                state for state in self._states.values()
                if state.get("enabled", True)
                and float(state.get("circuit_open_until", 0) or 0) <= now
            ]
            if not available:
                # When every domain is open, probe the one whose cooldown expires first.
                available = [
                    state for state in self._states.values()
                    if state.get("enabled", True)
                ]
            available.sort(
                key=lambda state: (
                    float(state.get("circuit_open_until", 0) or 0) > now,
                    -float(state.get("score", 0) or 0),
                    float(state.get("latency_ms") or 999999),
                    int(state.get("priority", 1000) or 1000),
                )
            )
            return [state["domain"] for state in available]

    def record_success(self, domain: str, latency_ms: Optional[float] = None) -> None:
        normalized = self.normalize_domain(domain)
        if not normalized:
            return
        with self._lock:
            state = self._states.get(normalized)
            if not state:
                return
            state["consecutive_failures"] = 0
            state["circuit_open_until"] = 0.0
            state["last_success_at"] = time.time()
            state["last_failure_reason"] = ""
            state["score"] = min(100.0, float(state.get("score", 50.0) or 50.0) + 5.0)
            if latency_ms is not None:
                previous = state.get("latency_ms")
                state["latency_ms"] = (
                    float(latency_ms)
                    if previous is None
                    else round((float(previous) * 0.7) + (float(latency_ms) * 0.3), 2)
                )
            self._persist_locked()

    def record_failure(self, domain: str, reason: str) -> None:
        normalized = self.normalize_domain(domain)
        if not normalized:
            return
        with self._lock:
            state = self._states.get(normalized)
            if not state:
                return
            failures = int(state.get("consecutive_failures", 0) or 0) + 1
            state["consecutive_failures"] = failures
            state["last_failure_at"] = time.time()
            state["last_failure_reason"] = str(reason or "")[:120]
            state["score"] = max(0.0, float(state.get("score", 50.0) or 50.0) - 15.0)
            if failures >= self.failure_threshold:
                state["circuit_open_until"] = time.time() + self.cooldown_seconds
                self.logger.warning(
                    "Source domain circuit opened [%s]: %s",
                    self.source_key,
                    normalized,
                )
            self._persist_locked(force=True)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "source": self.source_key,
                "domains": [dict(state) for state in self._states.values()],
            }
