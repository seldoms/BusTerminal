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
        dead_threshold: int = 20,
    ):
        self.db = db
        self.source_key = source_key
        self.logger = logger
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(30, int(cooldown_seconds))
        # 连续失败到这个次数就判定该域名已死，不再参与轮换。
        # 2026-11-04 实测 javbus.cyou / seejav.bond 各累计失败 212 次仍在候选里，
        # 每次挑选最佳域名都要白等一次超时（5s×2），把订阅解析拖到分钟级。
        self.dead_threshold = max(self.failure_threshold + 1, int(dead_threshold))
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
                    state = self._clean_state(raw_state, domain)
                    # 上线前累计的失败次数也要生效：老版本没有「判死」逻辑，
                    # persist 下来的 javbus.cyou / seejav.bond 各带 212 次失败却仍是
                    # enabled=true，每次选域名都要白等一次超时。
                    #
                    # 但 2026-11-07 实测发现：累计失败判死把**健康域名**也误杀了。
                    # busdmm.casa / seedmm.casa / dmmsee.casa 详情页实测全部 OK_GID，
                    # 它们只是历史上一段 SSL/超时波动累积到 20 次就被永久排除，
                    # 于是候选池只剩 3 个，其中还有 1 个是停放页。
                    # 现在只有「内容级判死」（停放页）才永久排除；
                    # 累计失败只做暂时下线并清零计数，给它复活的机会。
                    #
                    # dead_page 字段是 v2.02 才加的，存量状态没有这个键，
                    # 所以要按 last_failure_reason 反推：v2.01 的 record_dead_domain
                    # 写的 reason 形如 dead_domain:parked_page。
                    if not state.get("dead_page") and "parked_page" in str(
                        state.get("last_failure_reason") or ""
                    ):
                        state["dead_page"] = True
                    if state.get("dead_page"):
                        state["enabled"] = False
                    elif int(state.get("consecutive_failures") or 0) >= self.dead_threshold:
                        state["enabled"] = False
                        state["consecutive_failures"] = 0
                        state["score"] = max(float(state.get("score", 0) or 0), 20.0)
                        state["circuit_open_until"] = 0.0
                    self._states[domain] = state

            for index, domain in enumerate(defaults):
                if domain not in self._states:
                    self._states[domain] = self._new_state(domain, index)
                else:
                    self._states[domain]["priority"] = min(
                        int(self._states[domain].get("priority", index)),
                        index,
                    )
                    # 内置域名是人工实测确认可用的清单（scraper.DOMAINS）。
                    # 存量状态里它们可能因历史波动被「累计失败」误判下线，
                    # 重启时给一次全新的机会：清零计数、放开熔断、恢复可用。
                    # 真死掉的（停放页）已由 dead_page 单独排除，不走这条。
                    state = self._states[domain]
                    if not state.get("dead_page"):
                        state["enabled"] = True
                        state["consecutive_failures"] = 0
                        state["circuit_open_until"] = 0.0
                        state["score"] = max(float(state.get("score", 0) or 0), 20.0)

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
            # 内容级判死（停放页）：域名被挂卖/关闭，不会自己恢复，永久排除。
            # 与「累计失败暂时下线」区分开，见 _load 的注释。
            "dead_page": False,
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
            "dead_page",
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
            # 永久判死（停放页）永远排除：这类域名是域名被挂卖/关闭，
            # 试它只会白等一次超时，还可能把停放页当正常页面解析。
            pool = [
                state for state in self._states.values()
                if not state.get("dead_page")
            ]
            enabled = [
                state for state in pool
                if state.get("enabled", True)
            ]
            # 全被判死时不能返回空——否则抓取链路静默停摆。退回全部域名再试一轮，
            # 分数最低的排最后，等于给它们一次「复活」机会。
            # 注意这里用 pool（含暂时下线的），不是 _states 全量，
            # 免得把停放页也放进来。
            pool = enabled or pool
            available = [
                state for state in pool
                if float(state.get("circuit_open_until", 0) or 0) <= now
            ]
            if not available:
                # When every domain is open, probe the one whose cooldown expires first.
                available = pool
            available.sort(
                key=lambda state: (
                    float(state.get("circuit_open_until", 0) or 0) > now,
                    -float(state.get("score", 0) or 0),
                    float(state.get("latency_ms") or 999999),
                    # 注意不能写成 `state.get("priority") or 1000`：priority 0 是
                    # 合法值（主域名就是 0），会被 or 当成缺省丢到队尾。
                    int(state["priority"]) if state.get("priority") is not None else 1000,
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
            # 抓到内容说明域名是活的：如果它之前只是「累计失败暂时下线」，
            # 这里把它恢复成可用（否则一次波动就可能永久失去一个健康域名）。
            # 但 dead_page（停放页）判死是内容级的，不在这里复活。
            if not state.get("dead_page"):
                state["enabled"] = True
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
            # 已判死（停放页）的域名不再累计普通失败，也不改写失败原因：
            # last_failure_reason 里的 parked_page 是存量状态反推 dead_page
            # 的唯一依据，被覆盖后就再也认不出这是停放页了。
            if state.get("dead_page"):
                self._persist_locked()
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
            if failures >= self.dead_threshold and state.get("enabled", True):
                # 累计失败只是**暂时下线**，不是永久死亡：实测 busdmm.casa 等
                # 健康域名曾因一段 SSL/超时波动被累计到 20 次而误杀。
                # 这里不打 dead_page 标记，重启后 _load 会清零计数并给它复活机会，
                # 真正不可恢复的停放页走 record_dead_domain。
                state["enabled"] = False
                state["last_failure_reason"] = f"dead_domain:{str(reason or '')[:100]}"
                self.logger.warning(
                    "Source domain disabled after %s consecutive failures [%s]: %s",
                    failures,
                    self.source_key,
                    normalized,
                )
            self._persist_locked(force=True)

    def record_dead_domain(self, domain: str, reason: str = "dead_page") -> None:
        """立刻把一个域名判死，不再参与轮换。

        2026-11-07：``www.busdmm.cyou`` 已经变成域名停放页（HTTP 200 +
        "This domain is for sale"）。它不是「临时失败」而是**永久失效**——
        域名被挂卖了，放映期不会恢复。所以不能按普通失败累计 20 次，
        也不能因为它返回 200 就记成功（那会让它的评分一路上涨、
        长期霸占候选池，每次抓详情都白跑一趟）。

        这里直接 ``enabled=False`` 并把分数打到 0，同时打上 ``dead_page``
        标记 —— 这个标记会持久化，重启后依然被永久排除；
        而「累计失败判死」只是暂时下线，详见 _load 的注释。
        """
        normalized = self.normalize_domain(domain)
        if not normalized:
            return
        with self._lock:
            state = self._states.get(normalized)
            if not state:
                return
            if state.get("enabled", True):
                self.logger.warning(
                    "Source domain disabled immediately (dead page) [%s]: %s",
                    self.source_key,
                    normalized,
                )
            state["enabled"] = False
            state["dead_page"] = True
            state["score"] = 0.0
            state["consecutive_failures"] = max(
                int(state.get("consecutive_failures", 0) or 0), self.dead_threshold
            )
            state["circuit_open_until"] = 0.0
            state["last_failure_at"] = time.time()
            state["last_failure_reason"] = f"dead_domain:{str(reason or '')[:100]}"
            self._persist_locked(force=True)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "source": self.source_key,
                "domains": [dict(state) for state in self._states.values()],
            }
