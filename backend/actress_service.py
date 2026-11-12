import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional

import scrape_schedule

# 元数据「更新周期」是**同一条女优的复查冷却**，不是循环 sleep 时长。
# 缺资料的积压要靠后台小批次持续消化，真等 7 天才跑一轮的话，
# 10 万条缺口按每轮 10 条要 200 年。所以循环本身按小步长轮询，
# 候选筛选时用配置的周期做冷却。
METADATA_POLL_MAX_SECONDS = 1800


class ActressService:
    def __init__(
        self,
        *,
        db: Any,
        netflav_scraper: Any,
        logger: Any,
        task_registry: Any,
        stop_event: Any,
        safe_number: Callable[..., float],
        safe_exception_label: Callable[[Exception], str],
        display_value_for_log: Callable[[Any], str],
        as_bool: Callable[[Any, bool], bool],
        resolve_actress_subscription_url: Callable[..., Optional[str]],
        get_actress_subscription_url_candidates: Callable[..., list],
        get_subscription_delete_url_candidates: Callable[[Any], list],
        subscription_url_exists: Callable[[Optional[str]], bool],
        subscription_visible_for_actress: Callable[..., bool],
        normalize_subscription_display_name: Callable[[Optional[str], str], str],
        metadata_retry_seconds: int,
        metadata_batch_size: int,
        metadata_max_attempts: int,
    ):
        self.db = db
        self.netflav_scraper = netflav_scraper
        self.logger = logger
        self.task_registry = task_registry
        self.stop_event = stop_event
        self.safe_number = safe_number
        self.safe_exception_label = safe_exception_label
        self.display_value_for_log = display_value_for_log
        self.as_bool = as_bool
        self.resolve_actress_subscription_url = resolve_actress_subscription_url
        self.get_actress_subscription_url_candidates = get_actress_subscription_url_candidates
        self.get_subscription_delete_url_candidates = get_subscription_delete_url_candidates
        self.subscription_url_exists = subscription_url_exists
        self.subscription_visible_for_actress = subscription_visible_for_actress
        self.normalize_subscription_display_name = normalize_subscription_display_name
        self.metadata_retry_seconds = metadata_retry_seconds
        self.metadata_batch_size = metadata_batch_size
        self.metadata_max_attempts = metadata_max_attempts

    def has_basic_metadata(self, actress: Optional[Dict]) -> bool:
        if not actress:
            return False
        return bool(actress.get("avatar") and actress.get("birth_date") and actress.get("height"))

    def list_actresses(
        self,
        *,
        page: int,
        limit: int,
        query: Optional[str],
        followed_only: bool,
        subscribed_only: Optional[bool],
        subscription_state: Optional[str],
        sort_by_new: bool,
        sort_by_cup: bool,
    ) -> Dict[str, Any]:
        offset = (page - 1) * limit
        followed_filter = None
        if subscription_state == "subscribed":
            followed_filter = True
        elif subscription_state == "unsubscribed":
            followed_filter = False
        elif followed_only or subscribed_only is True:
            followed_filter = True
        actresses = self.db.get_actresses(
            limit,
            offset,
            False,
            query,
            sort_by_new,
            sort_by_cup,
            followed_filter,
        )
        last_viewed_at = self.safe_number(self.db.get_config("actresses_last_viewed_at"), 0)
        for actress in actresses:
            created_at = self.safe_number(actress.get("created_at"), 0)
            actress["is_new"] = created_at > last_viewed_at
        return {"success": True, "data": actresses, "count": len(actresses)}

    def get_detail(self, actress_id: str) -> Dict[str, Any]:
        actress_id = (actress_id or "").strip()
        if not actress_id:
            return {"success": False, "message": "missing_actress_id"}

        actress = self.db.get_actress(actress_id)
        if not actress:
            return {"success": False, "message": "actress_not_found"}

        movies = self.db.get_movies_by_actress_id_indexed(
            actress_id,
            60,
            0,
            actress.get("name"),
        )
        if not movies:
            movies = self.db.get_movies_by_actress_id(actress_id, 60, 0)

        return {
            "success": True,
            "data": {
                "actress": actress,
                "movies": movies,
            },
        }

    def mark_viewed(self) -> Dict[str, bool]:
        self.db.set_config("actresses_last_viewed_at", time.time())
        return {"success": True}

    def toggle_subscription(self, actress_id: str) -> Dict[str, Any]:
        actress_id = (actress_id or "").strip()
        if not actress_id:
            return {"success": False, "message": "missing_actress_id"}

        actress = self.db.get_actress(actress_id)
        if not actress:
            return {"success": False, "message": "actress_not_found"}

        subscription_url = self.resolve_actress_subscription_url(
            actress_id,
            actress.get("page_url"),
        )
        subscription_urls = self.get_actress_subscription_url_candidates(
            actress_id,
            actress.get("page_url"),
            None,
            subscription_url,
        )
        is_subscribed = not self.as_bool(actress.get("is_followed"), False)

        subscribed = False
        subscription_preexisting = False
        if is_subscribed:
            if not subscription_url:
                return {"success": False, "message": "missing_subscription_url"}
            subscription_preexisting = self.subscription_url_exists(subscription_url)
            subscribed = self.db.add_subscription(
                subscription_url,
                self.normalize_subscription_display_name(actress.get("name"), actress_id),
                "star",
                (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"),
            )
            if not subscribed:
                return {
                    "success": False,
                    "message": "subscription_save_failed",
                    "subscription_url": subscription_url,
                    "subscription_urls": subscription_urls,
                }
            subscription_visible = self.subscription_visible_for_actress(
                actress_id,
                actress.get("page_url"),
                None,
                subscription_url,
            )
            if not subscription_visible:
                return {
                    "success": False,
                    "message": "subscription_not_visible",
                    "subscribed": subscribed,
                    "subscription_url": subscription_url,
                    "subscription_urls": subscription_urls,
                    "subscription_visible": False,
                }
        elif subscription_url:
            for item in subscription_urls:
                for target in self.get_subscription_delete_url_candidates(item):
                    self.db.delete_subscription(target)

        is_followed = self.db.toggle_follow_actress(actress_id, is_subscribed)
        subscription_visible = is_subscribed

        return {
            "success": True,
            "is_followed": is_followed,
            "is_subscribed": is_followed,
            "subscribed": subscribed if is_subscribed else False,
            "subscription_created": bool(is_subscribed and subscribed and not subscription_preexisting),
            "subscription_url": subscription_url,
            "subscription_urls": subscription_urls,
            "subscription_visible": subscription_visible,
        }

    def queue_metadata_from_detail(self, detail: Optional[Dict], source: str = "download_detail") -> int:
        if not detail:
            return 0

        queued = 0
        now_ts = time.time()
        for star in detail.get("stars") or []:
            if not isinstance(star, dict):
                continue

            star_name = (star.get("name") or "").strip()
            star_id = (star.get("id") or "").strip()
            if not star_name:
                continue
            if not star_id:
                star_id = f"name:{star_name}"

            actress = self.db.get_actress(star_id)
            if not actress:
                saved = self.db.save_actress({
                    "id": star_id,
                    "name": star_name,
                    "page_url": star.get("url") or star.get("page_url"),
                    "source": source,
                    "metadata_requested_at": now_ts,
                    "metadata_attempt_count": 0,
                })
                if saved:
                    queued += 1
                continue

            if not self.has_basic_metadata(actress):
                self.db.request_actress_metadata(star_id)
                queued += 1

        return queued

    def _metadata_search_candidates(self, actress: Dict[str, Any]) -> list:
        """给出查 netflav 时可用的女优名候选，**原名优先**。

        库里 actresses.name 存的是**中文译名**（`天使萌`），而 netflav 站上
        索引的是**日文原名**（`天使もえ`）—— 2026-11-07 生产实测按中文译名
        搜 20 条命中 0 条，换日文原名命中 9 条。

        候选顺序：
          1. 日文原名（translations 表按译文反查）—— 最准，站上就是这个写法
          2. 库里已存的别名（aliases，含 name_en）
          3. 英文名（name_en）
          4. 中文译名本身 —— 兜底，命中率最低
        """
        name = str((actress or {}).get("name") or "").strip()
        candidates: list = []

        def push(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in candidates:
                candidates.append(text)

        # 1) 日文原名：库里存的是译名，译名的原文才是站上的写法
        if name and hasattr(self.db, "get_translation_originals"):
            try:
                for original in self.db.get_translation_originals(name, limit=3):
                    push(original)
            except Exception as exc:
                self.logger.debug(
                    "反查女优原名失败 [%s]: %s",
                    self.display_value_for_log(name),
                    self.safe_exception_label(exc),
                )

        # 2) 已存别名
        aliases = str((actress or {}).get("aliases") or "")
        for alias in aliases.split(","):
            push(alias)

        # 3) 英文名
        push((actress or {}).get("name_en"))

        # 4) 中文译名兜底
        push(name)
        return candidates

    def metadata_backfill_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                # 设置页可改（默认 7 天）：同一条女优的资料多久后才重新抓。
                retry_seconds = scrape_schedule.interval_seconds("actress_metadata")
                candidates = self.db.get_actresses_needing_metadata(
                    limit=self.metadata_batch_size,
                    retry_after_seconds=retry_seconds,
                    max_attempts=self.metadata_max_attempts,
                )
                attempted_count = 0
                success_count = 0
                failed_count = 0
                not_found_count = 0
                source_error_count = 0
                for actress in candidates:
                    if self.stop_event.is_set():
                        return

                    actress_id = actress.get("id")
                    name = actress.get("name")
                    success = False
                    error_label = None
                    attempted_count += 1
                    try:
                        search_candidates = self._metadata_search_candidates(actress)
                        updated = self.netflav_scraper.backfill_actress_by_candidates(
                            search_candidates,
                            target_id=actress_id,
                            save_movies=False,
                        )
                        success = self.has_basic_metadata(updated)
                        if not success:
                            error_label = "incomplete_metadata"
                    except Exception as e:
                        error_label = self.safe_exception_label(e)
                        # 抓取本身抛异常 = **源不可用**（反爬 / 断网 / 域名全挂）。
                        # 「站上查无此人」不算源不可用，见下面的 not_found_count。
                        source_error_count += 1
                        if int(actress.get("metadata_attempt_count") or 0) == 0:
                            self.logger.warning(
                                "Actress metadata source unavailable [%s]: %s",
                                self.display_value_for_log(name),
                                error_label,
                            )
                    finally:
                        self.db.mark_actress_metadata_attempt(
                            actress_id,
                            success=success,
                            error=error_label,
                        )

                    if success:
                        success_count += 1
                    elif error_label == "incomplete_metadata":
                        # 站上确实没这个人（或资料不全）—— 这是**常态**，不是故障。
                        # 2026-11-07 实测：候选里相当一部分演员 netflav 根本没收录，
                        # 硬算成失败会让任务卡长期挂红、看不出真问题。
                        not_found_count += 1
                    else:
                        failed_count += 1

                    self.stop_event.wait(1.5)

                if attempted_count:
                    summary = {
                        "attempted": attempted_count,
                        "completed": success_count,
                        "failed": failed_count,
                        "not_found": not_found_count,
                    }
                    # 只有**源真的不可用**才标错误；「站上查无此人」是正常结果。
                    if source_error_count:
                        self.task_registry.mark_error(
                            "actress_metadata_backfill_loop",
                            RuntimeError("actress_metadata_sources_unavailable"),
                            summary=summary,
                        )
                    else:
                        self.task_registry.mark_success(
                            "actress_metadata_backfill_loop",
                            summary=summary,
                        )
            except Exception as e:
                self.logger.error("Actress metadata backfill loop failed: %s", self.safe_exception_label(e))
                self.task_registry.mark_error("actress_metadata_backfill_loop", e)

            # 循环步长按「小批次持续消化」走，不跟着 7 天冷却一起停摆。
            poll_seconds = min(
                scrape_schedule.interval_seconds("actress_metadata"),
                METADATA_POLL_MAX_SECONDS,
            )
            self.stop_event.wait(max(poll_seconds, 60))
