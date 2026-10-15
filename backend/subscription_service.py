from typing import Any, Callable, Dict, Optional
from urllib.parse import quote


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

    def resolve_actress_to_real_source_url(self, raw_url: Any) -> Optional[str]:
        """把内部女优订阅 URL 解析为可抓取的真实来源页（如 JavBus 女优页）。

        优先用 actresses.page_url；否则用女优名在 JavBus 搜索女优页。
        解析失败返回 None（调用方据此跳过，但仍更新 last_checked_at）。
        """
        subject = self._extract_internal_subject(raw_url)
        if not subject:
            return None

        actress = None
        try:
            actress = self.db.get_actress(subject)
        except Exception as exc:
            self.logger.warning(
                "解析女优订阅: 读取女优 %s 失败: %s",
                self.display_value_for_log(subject),
                self.safe_exception_label(exc),
            )

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
            search_url = f"{base_url}/search/actresses?fKeyword={quote(name)}"
            resp = self.scraper.session.get(search_url, timeout=10)
            if resp.status_code != 200 or not resp.content:
                return None
            soup = self.scraper.parse_html(resp)
            link = soup.select_one('a[href*="/star/"]') if soup else None
            if not link:
                return None
            href = link.get("href") or ""
            if href.startswith("/"):
                href = base_url + href
            normalized = self.normalize_scraper_url(href)
            if not normalized:
                return None
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

    def process_subscription(self, sub: Dict[str, Any], force_refresh: bool = False) -> Dict[str, Any]:
        report = self._new_report(sub)
        source_url = sub.get("url")

        # 用于抓取的 URL：内部女优订阅需先解析到真实来源页
        scrape_url = source_url
        if self.is_internal_subscription_url(source_url):
            resolved = self.resolve_actress_to_real_source_url(source_url)
            if not resolved:
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
            scrape_url = resolved
            self.logger.info(
                "订阅女优 %s 解析到真实来源: %s",
                self.display_value_for_log(sub.get("name")),
                self.display_value_for_log(scrape_url),
            )

        safe_url = self.normalize_scraper_url(scrape_url)
        if not safe_url:
            self._append_log(
                report,
                movie_id=source_url,
                title=sub.get("name"),
                status="failed",
                reason="invalid_subscription_url",
            )
            return report

        try:
            self.logger.info(
                "Checking subscription %s (force_refresh=%s)",
                self.display_value_for_log(sub.get("name")),
                force_refresh,
            )
            movies = self.scraper.get_movies(
                safe_url,
                page=1,
                limit=self.page_limit,
                force_refresh=force_refresh,
                perform_translate=False,
            )
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

                best_magnet = self.scraper.select_best_magnet(magnets, preference=self.magnet_preference)
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
            self.logger.error(
                "Failed to process subscription %s: %s",
                self.display_value_for_log(sub.get("name")),
                error_label,
            )
            self._append_log(report, status="error", reason=error_label)
            return report
