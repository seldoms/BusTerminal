import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

import scrape_schedule
from common_utils import redact_log_message
from scrape_budget import DetailThrottle
import javbus_path

# netflav 回落标记：`netflav:actress:<名字>`。
# javbus 全站反爬时（302 到 /doc/driver-verify 并回 200），内部女优订阅一条都解析不出
# 真实来源页；netflav 的 `/all?actress=<名字>` 仍能正常列出该女优的片单，
# 于是用它只做「发现」，magnet 仍回 javbus 详情页取。
NETFLAV_ACTRESS_PREFIX = "netflav:actress:"


class SubscriptionService:
    def __init__(
        self,
        *,
        db: Any,
        scraper: Any,
        logger: Any,
        is_internal_subscription_url: Callable[[Any], bool],
        normalize_scraper_url: Callable[[Any], Optional[str]],
        normalize_magnet_url: Callable[[Any], Optional[str]],
        queue_actress_metadata_from_detail: Callable[[Dict[str, Any], str], Any],
        evaluate_download_permission: Callable[..., Dict[str, Any]],
        queue_download_if_absent: Callable[[str, str, str, Optional[str]], Dict[str, Any]],
        safe_exception_label: Callable[[Exception], str],
        display_value_for_log: Callable[[Any], str],
        page_limit: int = 30,
        magnet_preference: str = "1080P",
        scrape_budget: Any = None,
        list_throttle: Optional[DetailThrottle] = None,
        netflav_scraper: Any = None,
    ):
        self.db = db
        self.scraper = scraper
        self.logger = logger
        self.is_internal_subscription_url = is_internal_subscription_url
        self.normalize_scraper_url = normalize_scraper_url
        self.normalize_magnet_url = normalize_magnet_url
        self.queue_actress_metadata_from_detail = queue_actress_metadata_from_detail
        self.evaluate_download_permission = evaluate_download_permission
        self.queue_download_if_absent = queue_download_if_absent
        self.safe_exception_label = safe_exception_label
        self.display_value_for_log = display_value_for_log
        self.page_limit = page_limit
        self.magnet_preference = magnet_preference
        self.scrape_budget = scrape_budget
        # 订阅检索的节奏由它管：间隔取 0 ~ 设置值（默认 3 分钟）的随机数，
        # **不占**首页/榜单的日配额。
        # 详见 scrape_budget 模块文档里的取舍说明。
        self.list_throttle = list_throttle
        # 新源：javbus 解析不出女优页时的「发现」后备（只列片，不供 magnet）
        self.netflav_scraper = netflav_scraper
        # 女优来源页解析的连败熔断：javbus 一旦反爬（2026-11-04 实测全站
        # `/doc/driver-verify`），291 条内部订阅会逐条白打一次搜索请求。
        self._resolve_failure_streak = 0
        self._resolve_blocked_until = 0.0

    def _new_report(self, sub: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "sub_name": sub.get("name"),
            "source_url": sub.get("url"),
            "total_checked": 0,
            "added_count": 0,
            "skipped_count": 0,
            "filtered_count": 0,
            "blocked_count": 0,
            "pending_count": 0,
            "failed_count": 0,
            "logs": [],
        }

    def _inline_detail_gap(self) -> None:
        """同一条订阅内多个新片之间抓详情的间隔（默认 0~6 秒随机）。

        为什么默认这么短：一条订阅常带十几个未入库新片，若每个详情也等 3 分钟，
        光它就占半小时，308 条订阅一轮 20+ 小时 —— 用户要的「12 小时一轮」永远到不了。
        这一项在设置页「抓取节奏 → 订阅详情间隔」可改，调大就会明显拉长一轮。
        """
        time.sleep(scrape_schedule.random_delay_seconds("subscription_detail_gap", self.db))

    def _append_log(
        self,
        report: Dict[str, Any],
        *,
        status: str,
        reason: str,
        movie_id: Any = None,
        title: Any = None,
    ) -> None:
        report["logs"].append({
            "id": movie_id,
            "title": title,
            "status": status,
            "reason": reason,
        })
        count_key = f"{status}_count"
        if count_key in report:
            report[count_key] += 1
        elif status == "error":
            report["failed_count"] += 1

    def _extract_internal_subject(self, raw_url: Any) -> Optional[str]:
        value = str(raw_url or "").strip()
        for prefix in ("jellyfin:actress:", "jellyfin:actor:", "jellyfin:person:", "jf_person:", "jf_actor:"):
            if value.lower().startswith(prefix):
                subject = value[len(prefix):].strip()
                return subject or None
        return None

    def _actress_for_internal_subject(self, subject: str) -> Optional[Dict[str, Any]]:
        """把内部订阅的 subject 换成 actresses 表里的那一行。

        ⚠️ `jellyfin:actress:r18:1059227` 剥掉前缀后剩 `r18:1059227`，这是
        **`source:source_id`**，不是 `actresses.id`。原实现一律按 id 查
        （`get_actress(subject)`）→ 查不到 → `name` 为空 → 直接 `return None`，
        于是这些订阅每轮都是「秒跳过 + 更新 last_checked_at」。
        2026-11-04 实测：294 条内部订阅里 **291 条**走到这条死路，
        订阅功能对它们从来没真正跑过一次（日志里一整轮只有 1 条
        `Checking subscription`，却有 278 条 `last_checked_at` 被更新）。
        """
        subject = str(subject or "").strip()
        if not subject:
            return None
        parts = subject.split(":", 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            try:
                found = self.db.get_actress_by_source(parts[0], parts[1])
            except Exception as exc:
                self.logger.warning(
                    "解析女优订阅: 按 source 查女优 %s 失败: %s",
                    self.display_value_for_log(subject),
                    self.safe_exception_label(exc),
                )
                found = None
            if found:
                return found
        # ⚠️ 2026-11-07 实测的断链点（161 条 person 订阅全挂在这）：
        #   订阅 url 是 `jellyfin:person:<JellyfinId>`
        #   而 actresses 表里那一行的 id 是 **`jf_person:<JellyfinId>`**
        #   —— 同一个 id，前缀差一个字。按 subject 直接查 id 必然查不到，
        #   于是每轮都是 `actress_url_unresolved` 秒跳过，
        #   订阅对这些人从来没真正跑过一次。
        #   `jf_actor:` 同理（`jellyfin:actor:<名字>` → `jf_actor:<名字>`）。
        #
        #   `jellyfin:actress:netflav:<hash>` → `netflav:<hash>`：
        #   netflav 同步演员时建的订阅，演员行 id 是 `netflav:<source_id>`。
        for candidate_id in (
            f"jf_person:{subject}",
            f"jf_actor:{subject}",
            f"netflav:{subject}",
            f"name:{subject}",
        ):
            try:
                found = self.db.get_actress(candidate_id)
            except Exception:
                found = None
            if found:
                return found
        try:
            found = self.db.get_actress(subject)
        except Exception as exc:
            self.logger.warning(
                "解析女优订阅: 读取女优 %s 失败: %s",
                self.display_value_for_log(subject),
                self.safe_exception_label(exc),
            )
            found = None
        if found:
            return found
        # 老格式 `jellyfin:actor:<名字>` / `jf_actor:<名字>`：subject 是**名字**，
        # 库里那行的 id 是 `jf_actor:<名字>`，按 id 查必然查不到 —— 按名字再兜一层。
        # 2026-11-05 实测：这类订阅不补这层就永远解析不出女优，也就走不到 netflav 回落。
        try:
            return self.db.get_actress_by_name(subject)
        except Exception as exc:
            self.logger.warning(
                "解析女优订阅: 按名字查女优 %s 失败: %s",
                self.display_value_for_log(subject),
                self.safe_exception_label(exc),
            )
            return None
            return None

    RESOLVE_FAILURE_STREAK_LIMIT = 5
    RESOLVE_BLOCK_SECONDS = 1800

    def _note_resolve_failure(self) -> None:
        """连续几次搜索都拿不到 star 链接 → 冷却半小时，别再逐条白打。"""
        self._resolve_failure_streak += 1
        if self._resolve_failure_streak >= self.RESOLVE_FAILURE_STREAK_LIMIT:
            if time.time() >= self._resolve_blocked_until:
                self.logger.warning(
                    "解析女优订阅连续失败 %s 次（站点可能在反爬），暂停 %s 秒",
                    self._resolve_failure_streak,
                    self.RESOLVE_BLOCK_SECONDS,
                )
            self._resolve_blocked_until = time.time() + self.RESOLVE_BLOCK_SECONDS
            self._resolve_failure_streak = 0

    def resolve_actress_to_real_source_url(self, raw_url: Any) -> Optional[str]:
        """把内部女优订阅 URL 解析为可抓取的真实来源页（如 JavBus 女优页）。

        只返回 JavBus 侧结果；解析不出就返回 None。需要「JavBus 不行就换源」的
        调用方用 `resolve_subscription_target`。
        """
        target = self.resolve_subscription_target(raw_url, allow_fallback=False)
        if target and target.get("kind") == "javbus":
            return target.get("url")
        return None

    def resolve_subscription_target(
        self, raw_url: Any, *, allow_fallback: bool = True
    ) -> Optional[Dict[str, Any]]:
        """把内部订阅解析成抓取目标。

        返回 `{"kind": "javbus", "url": ..., "name": ...}` 或
        `{"kind": "netflav", "name": ...}`；都解析不出返回 None。

        JavBus 优先（详情页带 magnet，是唯一下载来源）。JavBus 解析失败
        （反爬 / 熔断中 / 找不到女优页）时才回落到 netflav 做「发现」。
        """
        subject = self._extract_internal_subject(raw_url)
        if not subject:
            return None

        actress = self._actress_for_internal_subject(subject)
        name = str((actress or {}).get("name") or "").strip()

        url = self._javbus_page_url_for(actress)
        if url:
            return {"kind": "javbus", "url": url, "name": name}

        if allow_fallback and self.netflav_scraper is not None and name:
            return {"kind": "netflav", "name": name}
        return None

    def resolve_subscription_target_from_actress(
        self, actress: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """已经有演员行时，直接解析成抓取目标（不再回头查 subject）。

        用于「订阅行的 subject 与 actresses.id 对不上、但按名字能查到」的兜底
        （2026-11-07 实测 62 条 person 订阅属于这种）。
        """
        if not actress:
            return None
        name = str(actress.get("name") or "").strip()
        url = self._javbus_page_url_for(actress)
        if url:
            return {"kind": "javbus", "url": url, "name": name}
        if self.netflav_scraper is not None and name:
            return {"kind": "netflav", "name": name}
        return None

    def _javbus_page_url_for(self, actress: Optional[Dict[str, Any]]) -> Optional[str]:
        """定位女优在 JavBus 的来源页；失败返回 None（并累计连败计数）。"""
        if time.time() < self._resolve_blocked_until:
            # 刚刚连续失败过：这一轮不再打站点。订阅循环会照常更新
            # last_checked_at，等冷却结束再试。
            return None

        # 1) 优先用已存的 page_url
        if actress:
            page_url = actress.get("page_url")
            if page_url:
                normalized = self.normalize_scraper_url(page_url)
                if normalized:
                    return normalized

        # 2) 用女优名在 JavBus 搜索女优页
        name = (actress or {}).get("name") or ""
        if not name:
            return None
        try:
            base_url = str(getattr(self.scraper, "base_url", "") or "").rstrip("/")
            if not base_url:
                return None
            # 2026-11-07 实测：站点改了女优搜索路由。
            #   旧路由 /search/actresses?fKeyword=<kw> 现在返回的是**影片**列表
            #     （结果区是 movie-box 指向 /<番号>），一个 /star/ 链接都没有，
            #     于是「解析失败」→ 回落 netflav → 一条磁力都拿不到。
            #   新路由 /searchstar/<kw>（站点 searchs() 函数里拼的）才返回女优页。
            # 两个路由都试：先新的，再兜底旧的（站点改回旧路由时仍能工作）。
            search_urls = [
                f"{base_url}/searchstar/{quote(name)}",
                f"{base_url}/search/actresses?fKeyword={quote(name)}",
            ]
            link = None
            for search_url in search_urls:
                resp = self.scraper.session.get(search_url, timeout=10)
                if resp.status_code != 200 or not resp.content:
                    continue
                soup = self.scraper.parse_html(resp)
                link = soup.select_one('a[href*="/star/"]') if soup else None
                if link:
                    break
            if not link:
                # javbus 反爬时会 302 到 `/doc/driver-verify` 并回 200，
                # 页面里一个 `/star/` 链接都没有 —— 这里必须当失败处理。
                self._note_resolve_failure()
                return None
            href = link.get("href") or ""
            if href.startswith("/"):
                href = base_url + href
            normalized = self.normalize_scraper_url(href)
            if not normalized:
                self._note_resolve_failure()
                return None
            self._resolve_failure_streak = 0
            # 回写 page_url，下次免搜索
            try:
                if actress:
                    actress["page_url"] = normalized
                    self.db.save_actress(actress)
            except Exception:
                pass
            return normalized
        except Exception as exc:
            self.logger.warning(
                "解析女优订阅: 搜索女优 %s 失败: %s",
                self.display_value_for_log(name),
                self.safe_exception_label(exc),
            )
            return None

    def _external_netflav_actress(self, source_url: Any) -> Optional[str]:
        """外部订阅 URL 直接指向 netflav 时，返回女优名（否则 None）。

        2026-11-07 生产实测：库里 3 条订阅的 url 形如
        `https://netflav.com/all?actress=%E5%A4%A7%E5%B3%B6%E5%84%AA%E9%A6%99`，
        netflav 不在 JavBus 的 SSRF 白名单里 → 每轮都被判 `invalid_subscription_url`，
        `auto_crawl_subscriptions` 的 failed_count 恒为 3（数量与这 3 条完全吻合）。

        识别出来就能走 netflav 发现（磁力仍回 JavBus 详情页取）。
        """
        if self.netflav_scraper is None:
            return None
        value = str(source_url or "").strip()
        # 只认 http(s) 绝对地址：内部订阅（jellyfin:…）走的是另一条解析路径。
        if not value.lower().startswith(("http://", "https://")):
            return None
        try:
            name = self.netflav_scraper.actress_name_from_url(value)
        except Exception:
            return None
        return str(name or "").strip() or None

    def _fetch_movies_from_netflav(
        self, name: str, *, force_refresh: bool = False
    ) -> Any:
        """用 netflav 列出该女优的最新片单（发现层）。

        netflav 的详情页没有 magnet，所以这里把 `detail_url` 改写成 JavBus 的
        `/<番号>` —— 后续 `get_movie_detail` 会按当前可用域名重新拼，
        下载链路完全不用改。
        """
        if self.netflav_scraper is None:
            return []
        base = str(getattr(self.scraper, "base_url", "") or "").rstrip("/")
        if not base:
            return []
        try:
            movies = self.netflav_scraper.list_movies_for_actress(
                name, limit=self.page_limit, force_refresh=force_refresh
            )
        except Exception as exc:
            self.logger.warning(
                "netflav 订阅发现失败 [%s]: %s",
                self.display_value_for_log(name),
                self.safe_exception_label(exc),
            )
            return []
        prepared = []
        for movie in movies or []:
            movie_id = str(movie.get("id") or "").strip()
            if not movie_id:
                continue
            # netflav 的番号写法与库内不一定一致（`JUR-97` vs 库里的 `JUR-097`），
            # 先用 code_norm 归一到库内 id，否则拼出来的 javbus 详情页会 404。
            try:
                canonical = self.db.get_movie_id_by_code_norm(movie_id)
            except Exception:
                canonical = None
            if isinstance(canonical, str) and canonical.strip():
                movie_id = canonical.strip()
                movie["id"] = movie_id
            # 详情页 URL 用 JavBus 的路径形态（库内 id 可能是 amane 的 0 填充写法，
            # 直接拼必然 404 —— 2026-11-07 实测 gma00090 → 404、GMA-090 → 200）。
            movie["detail_url"] = javbus_path.build_detail_url(base, movie_id)
            prepared.append(movie)
        return prepared

    def process_subscription(self, sub: Dict[str, Any], force_refresh: bool = False) -> Dict[str, Any]:
        report = self._new_report(sub)
        source_url = sub.get("url")

        # 用于抓取的 URL：内部女优订阅需先解析到真实来源页
        scrape_url = source_url
        netflav_name: Optional[str] = None
        # ⚠️ 解析来源页这一步**必须在 try 里面**。
        # 2026-11-07 实测：`resolve_subscription_target` 抛异常时（原代码在 try 外），
        # 异常直接冒泡到 `auto_crawl_subscriptions` 的 except，被记成
        # `failed_count += 1` —— 但**报告里一条日志都没有**，事后完全查不出
        # 是哪几条订阅、什么原因失败。
        try:
            external_netflav_name = self._external_netflav_actress(source_url)
            if external_netflav_name:
                # 订阅源就是 netflav 的演员页：直接走 netflav 发现，
                # 别再当 JavBus 页面去抓（那个域名过不了白名单，必然失败）。
                netflav_name = external_netflav_name
                report["discovery_source"] = "netflav"
                self.logger.info(
                    "订阅 %s 指向 netflav 演员页，直接用 netflav 发现",
                    self.display_value_for_log(external_netflav_name),
                )
            elif self.is_internal_subscription_url(source_url):
                target = self.resolve_subscription_target(source_url)
                if not target:
                    # 2026-11-07 兜底：订阅行本身带 `name`（演员名），
                    # 而 subject 是 Jellyfin 侧的哈希，两边对不上时用名字再找一次。
                    # 实测 62 条 person 订阅就是这种情况（楪カレン / 小花のん …），
                    # 按名字都能在 actresses 里找到对应行（r18:1067267 等）。
                    fallback = self._actress_for_internal_subject(str(sub.get("name") or "").strip())
                    if fallback:
                        target = self.resolve_subscription_target_from_actress(fallback)
                if not target:
                    # 解析失败：仍更新检查时间，避免前端永远显示"等待初始化"
                    try:
                        self.db.update_subscription_check_time(source_url)
                    except Exception:
                        pass
                    self._append_log(
                        report,
                        movie_id=source_url,
                        title=sub.get("name"),
                        status="skipped",
                        reason="actress_url_unresolved",
                    )
                    return report
                if target.get("kind") == "netflav":
                    netflav_name = target.get("name")
                    report["discovery_source"] = "netflav"
                    self.logger.info(
                        "订阅女优 %s 的 JavBus 来源页解析失败，回落 netflav 发现",
                        self.display_value_for_log(netflav_name),
                    )
                else:
                    scrape_url = target.get("url")
                    report["discovery_source"] = "javbus"
                    self.logger.info(
                        "订阅女优 %s 解析到真实来源: %s",
                        self.display_value_for_log(sub.get("name")),
                        self.display_value_for_log(scrape_url),
                    )
        except Exception as exc:
            error_label = self._describe_exception(exc)
            self.logger.error(
                "Failed to resolve subscription source %s: %s",
                self.display_value_for_log(sub.get("name")),
                error_label,
            )
            self._append_log(report, status="error", reason=error_label)
            return report

        safe_url = source_url if netflav_name else self.normalize_scraper_url(scrape_url)
        if not safe_url:
            self._append_log(
                report,
                movie_id=source_url,
                title=sub.get("name"),
                status="failed",
                reason="invalid_subscription_url",
            )
            return report

        # 订阅检索按随机间隔排队，**不占**首页/榜单的每日配额。
        # 2026-11-04 实测：库里 308 条订阅（294 条是 jellyfin:person: 内部订阅），
        # 若按每站每天 10 次算要 59 天才能轮完，整轮订阅连几天都以
        # `subscription_sources_unavailable` 收尾 —— 等于功能停摆。
        # 2026-11-05 用户进一步降频：整轮 12 小时、单条间隔 0~3 分钟随机，
        # 不追求速度、后台慢慢做。
        if self.list_throttle is not None:
            self.list_throttle.wait()

        try:
            self.logger.info(
                "Checking subscription %s (force_refresh=%s)",
                self.display_value_for_log(sub.get("name")),
                force_refresh,
            )
            movies = None
            if netflav_name:
                movies = self._fetch_movies_from_netflav(netflav_name, force_refresh=force_refresh)
            else:
                movies = self.scraper.get_movies(
                    safe_url,
                    page=1,
                    limit=self.page_limit,
                    force_refresh=force_refresh,
                    perform_translate=False,
                )
                # JavBus 能打开但列表是空的：多半也是反爬的软失败（验证页解析出 0 条），
                # 这时同样回落到 netflav 发现，别把「站点挡了」当成「女优没新片」。
                if not movies and self.netflav_scraper is not None:
                    fallback_name = report.get("sub_name")
                    if fallback_name:
                        movies = self._fetch_movies_from_netflav(
                            str(fallback_name), force_refresh=force_refresh
                        )
                        if movies:
                            report["discovery_source"] = "netflav_fallback"
            report["total_checked"] = len(movies)

            exclude_list = [
                tag.strip().upper()
                for tag in str(sub.get("exclude_tags") or "").split(",")
                if tag.strip()
            ]
            start_date = str(sub.get("start_date") or "")

            for movie in movies:
                movie_id = movie.get("id")
                title = movie.get("title", "Unknown")

                if start_date and movie.get("date") and movie["date"] < start_date:
                    self._append_log(
                        report,
                        movie_id=movie_id,
                        title=title,
                        status="filtered",
                        reason="release_date_before_subscription_start",
                    )
                    continue

                detail_url = self.normalize_scraper_url(movie.get("detail_url"))
                if not detail_url:
                    self._append_log(
                        report,
                        movie_id=movie_id,
                        title=title,
                        status="failed",
                        reason="invalid_movie_detail_url",
                    )
                    continue

                # 已入库/已下载的不必再抓详情：详情抓取被节流到 0~3 分钟随机一次，
                # 先剔掉这些不需要回源的，否则一轮订阅要跑几个小时。
                if self.db.is_movie_downloaded(movie_id):
                    self._append_log(
                        report,
                        movie_id=movie_id,
                        title=title,
                        status="skipped",
                        reason="already_downloaded",
                    )
                    continue

                if self.db.get_local_file(movie_id):
                    self._append_log(
                        report,
                        movie_id=movie_id,
                        title=title,
                        status="skipped",
                        reason="local_file_exists",
                    )
                    continue

                # 同一批新片之间只隔几秒（见 INLINE_DETAIL_GAP_SECONDS 的说明）。
                self._inline_detail_gap()

                detail = self.scraper.get_movie_detail(detail_url)
                self.queue_actress_metadata_from_detail(detail, "subscription_download")
                self.db.save_movie({"id": movie_id, "detail": detail, **movie})

                if exclude_list:
                    movie_genres = []
                    for genre in detail.get("genres", []):
                        name = genre.get("name", "") if isinstance(genre, dict) else genre
                        if name:
                            movie_genres.append(str(name).upper())

                    matched_tags = [tag for tag in exclude_list if tag in movie_genres]
                    if matched_tags:
                        self._append_log(
                            report,
                            movie_id=movie_id,
                            title=title,
                            status="filtered",
                            reason=f"excluded_tags:{','.join(matched_tags)}",
                        )
                        continue

                if "VR" in str(movie_id or "").upper() or "VR" in str(title or "").upper():
                    self._append_log(
                        report,
                        movie_id=movie_id,
                        title=title,
                        status="filtered",
                        reason="vr_auto_filter",
                    )
                    continue

                magnets = detail.get("magnets") or []
                if not magnets:
                    self.db.save_pending_download(
                        movie_id,
                        title,
                        source="subscription",
                        note="No magnet on source; retry later",
                    )
                    self._append_log(
                        report,
                        movie_id=movie_id,
                        title=title,
                        status="pending",
                        reason="no_magnet_on_source",
                    )
                    continue

                best_magnet = self.scraper.select_alive_magnet(magnets, preference=self.magnet_preference)
                if not best_magnet:
                    self.db.save_pending_download(
                        movie_id,
                        title,
                        source="subscription",
                        note="No suitable magnet found",
                    )
                    self._append_log(
                        report,
                        movie_id=movie_id,
                        title=title,
                        status="pending",
                        reason="no_suitable_magnet",
                    )
                    continue

                selected_magnet = self.normalize_magnet_url(best_magnet.get("magnet"))
                if selected_magnet and self.db.get_active_queue_entry(movie_id=movie_id, magnet=selected_magnet):
                    self._append_log(
                        report,
                        movie_id=movie_id,
                        title=title,
                        status="skipped",
                        reason="active_queue_exists",
                    )
                    continue

                resolution = best_magnet.get("resolution") or self.magnet_preference
                queue_title = f"{title} ({resolution})"
                permission = self.evaluate_download_permission(
                    movie_id,
                    selected_magnet,
                    queue_title,
                    source_url,
                    automated=True,
                )
                if not permission["allowed"]:
                    if permission["reasons"] in (
                        ["auto_download_disabled"],
                        ["automation_mode_not_auto_approve"],
                    ):
                        matched_rules = [
                            "subscription_match",
                            f"category:{sub.get('category_type') or 'unknown'}",
                        ]
                        candidate = self.db.upsert_automation_candidate(
                            "subscription_download",
                            movie_id,
                            title,
                            movie_id,
                            title,
                            "subscription",
                            "review_download",
                            "subscription_match_requires_approval",
                            0.7,
                            {
                                "magnet": selected_magnet,
                                "resolution": resolution,
                                "source_url": source_url,
                                "subscription_name": sub.get("name"),
                                "subscription_category": sub.get("category_type"),
                                "matched_rules": matched_rules,
                            },
                        )
                        candidate_status = str(candidate.get("status") or "candidate")
                        if candidate_status == "rejected":
                            self._append_log(
                                report,
                                movie_id=movie_id,
                                title=title,
                                status="skipped",
                                reason="automation_candidate_previously_rejected",
                            )
                            continue
                        if candidate_status == "approved":
                            self._append_log(
                                report,
                                movie_id=movie_id,
                                title=title,
                                status="skipped",
                                reason="automation_candidate_already_approved",
                            )
                            continue
                        self._append_log(
                            report,
                            movie_id=movie_id,
                            title=title,
                            status="pending",
                            reason="automation_candidate_created",
                        )
                        continue
                    self._append_log(
                        report,
                        movie_id=movie_id,
                        title=title,
                        status="blocked",
                        reason=", ".join(permission["reasons"]),
                    )
                    continue

                queue_result = self.queue_download_if_absent(
                    selected_magnet,
                    movie_id,
                    queue_title,
                    source_url,
                )
                if not queue_result["queued"]:
                    self._append_log(
                        report,
                        movie_id=movie_id,
                        title=title,
                        status="skipped",
                        reason="active_queue_exists",
                    )
                    continue

                self._append_log(
                    report,
                    movie_id=movie_id,
                    title=title,
                    status="added",
                    reason=f"queued:{resolution}",
                )

            self.db.update_subscription_check_time(source_url)
            return report

        except Exception as exc:
            error_label = self.safe_exception_label(exc)
            # 光有异常类型名定位不了问题（`safe_exception_label` 只返回类名），
            # 这里额外带上异常消息和出错位置 —— 消息里可能含 URL/路径，
            # 走 `redact_log_message` 脱敏后再落日志。
            detail = self._describe_exception(exc)
            self.logger.error(
                "Failed to process subscription %s: %s",
                self.display_value_for_log(sub.get("name")),
                detail,
            )
            self._append_log(report, status="error", reason=detail)
            return report

    def _describe_exception(self, exc: Exception) -> str:
        """把异常压成一行可定位的描述：类型 + 消息 + 出错位置。

        `safe_exception_label` 只返回异常类名，光看它无法判断问题在哪
        （2026-11-07 实测：订阅轮次 failed_count=3 却查不到是哪 3 条、
        什么原因，只能看到 `RuntimeError` 这种没信息量的标签）。
        """
        label = self.safe_exception_label(exc)
        try:
            message = redact_log_message(str(exc))
        except Exception:
            message = ""
        message = " ".join(str(message or "").split())[:200]
        if not message:
            return label
        # 消息里常带 `类型: 内容`（例如 `HTTPError: 404 Client Error`），
        # 去掉与 label 重复的前缀，避免 `RuntimeError: RuntimeError: ...`
        if message.startswith(label + ":"):
            message = message[len(label) + 1:].strip()
        if not message:
            return label
        return f"{label}: {message}"
