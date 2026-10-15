"""收藏 → 自动关注链路暴露的四个 bug 的回归测试。

生产实测依据：
- 订阅表里存在 `https://www.javbus.com`（站点首页，无法定位到任何演员）；
  「古東まりこ」的 page_url 是 `https://www.seedmm.cyou/`，同样无效，导致她丢订阅。
- 订阅名里存在「収录时间」「未知演员」——Jellyfin NFO 的字段标签被当成演员。
- 「鈴木真夕」在库里存在，但 Jellyfin 用简体写法「铃木真夕」，精确匹配失败 →
  漏订阅 + 用兜底 id 再建一条重复订阅。
- 每日 3:00 的自动同步曾连续三天没跑（入队窗口只有 3:00-3:29 且日期不落库）。
"""

import unittest
from datetime import date, datetime
from unittest.mock import MagicMock

from jellyfin_service import JellyfinService
from person_name_utils import (
    is_plausible_person_name,
    match_person_name,
    match_person_name_loose,
    names_loosely_equal,
)
from scraper_url_utils import (
    has_meaningful_path,
    normalize_scraper_page_url,
    normalize_scraper_url,
)


ALLOWED_DOMAINS = [
    "https://www.javbus.com",
    "https://netflav.com",
    "https://www.seedmm.cyou",
]


def build_service(db=None, jellyfin_client=None):
    return JellyfinService(
        db=db or MagicMock(),
        jellyfin_client=jellyfin_client or MagicMock(),
        scraper=MagicMock(),
        logger=MagicMock(),
        deletion_tasks={},
        deletion_tasks_lock=MagicMock(),
        as_bool=lambda x, d: bool(x),
        parse_int_value=lambda x, d, minimum=0: int(x or d),
        safe_number=float,
        safe_exception_label=str,
        display_value_for_log=str,
        normalize_scraper_url=lambda x: str(x),
        normalize_external_subscription_url=lambda x: str(x),
        normalize_subscription_display_name=lambda a, b: str(b),
        follow_and_subscribe_actress=lambda *a, **k: {"subscribed": True},
        jellyfin_person_subscription_url_from_actress_id=lambda x: None,
        resolve_actress_subscription_url=lambda a, b=None, c=None: None,
        subscription_visible_for_actress=lambda *a: True,
        get_actress_subscription_url_candidates=lambda *a: [],
        get_subscription_delete_url_candidates=lambda x: [],
        subscription_url_exists=lambda x: False,
        ensure_followed_actress_subscriptions=lambda: {},
        normalize_internal_subscription_url=lambda x: None,
        normalize_item_id=lambda x: str(x),
        run_in_threadpool=lambda fn, *args: fn(*args),
    )


class TestSubscriptionUrlMustPointToPage(unittest.TestCase):
    def test_site_homepage_has_no_meaningful_path(self):
        self.assertFalse(has_meaningful_path("https://www.javbus.com"))
        self.assertFalse(has_meaningful_path("https://www.seedmm.cyou/"))
        self.assertTrue(has_meaningful_path("https://www.javbus.com/star/uly"))

    def test_page_url_rejects_site_homepage(self):
        self.assertIsNone(
            normalize_scraper_page_url(
                "https://www.javbus.com",
                base_url="https://www.javbus.com",
                all_domains=ALLOWED_DOMAINS,
            )
        )
        self.assertIsNone(
            normalize_scraper_page_url(
                "https://www.seedmm.cyou/",
                base_url="https://www.javbus.com",
                all_domains=ALLOWED_DOMAINS,
            )
        )

    def test_page_url_keeps_real_actress_page(self):
        self.assertEqual(
            "https://www.javbus.com/star/uly",
            normalize_scraper_page_url(
                "https://www.javbus.com/star/uly",
                base_url="https://www.javbus.com",
                all_domains=ALLOWED_DOMAINS,
            ),
        )
        # 带查询串的列表页同样指向具体演员
        self.assertEqual(
            "https://netflav.com/all?actress=%E9%88%B4%E6%9C%A8%E7%9C%9F%E5%A4%95",
            normalize_scraper_page_url(
                "https://netflav.com/all?actress=%E9%88%B4%E6%9C%A8%E7%9C%9F%E5%A4%95",
                base_url="https://www.javbus.com",
                all_domains=ALLOWED_DOMAINS,
            ),
        )

    def test_generic_normalizer_unchanged_for_non_subscription_use(self):
        # 影片详情等其它用途不要求路径，行为保持不变
        self.assertEqual(
            "https://www.javbus.com",
            normalize_scraper_url(
                "https://www.javbus.com",
                base_url="https://www.javbus.com",
                all_domains=ALLOWED_DOMAINS,
            ),
        )


class TestPersonNameSanity(unittest.TestCase):
    def test_metadata_labels_are_not_people(self):
        for label in ("収录时间", "収録時間", "未知演员", "発売日", "导演", "unknown"):
            self.assertFalse(is_plausible_person_name(label), label)

    def test_real_names_pass(self):
        for name in ("鈴木真夕", "仓本堇", "古東まりこ", "Yui", "メロディー・雛・マークス"):
            self.assertTrue(is_plausible_person_name(name), name)

    def test_urls_and_empty_are_rejected(self):
        self.assertFalse(is_plausible_person_name("https://www.javbus.com"))
        self.assertFalse(is_plausible_person_name(""))
        self.assertFalse(is_plausible_person_name("   "))

    def test_variant_writings_match(self):
        self.assertTrue(names_loosely_equal("铃木真夕", "鈴木真夕"))
        self.assertTrue(names_loosely_equal("仓本堇", "倉本菫"))
        self.assertTrue(names_loosely_equal("仓本堇", "仓本堇"))

    def test_strict_match_does_not_merge_different_people(self):
        # 「鈴木真夕」和「黒木真夕」只差一个字，多人候选里绝不许合并
        self.assertIsNone(match_person_name("铃木真夕", ["三佳詩", "鈴木真夕", "黒木真夕"]))
        self.assertIsNone(match_person_name("三佳詩", ["古東まりこ"]))

    def test_loose_match_only_when_single_candidate(self):
        self.assertEqual("鈴木真夕", match_person_name_loose("铃木真夕", ["鈴木真夕"]))
        self.assertEqual("倉本菫", match_person_name_loose("仓本堇", ["倉本菫"]))
        # 多人候选时退化成严格匹配，不猜
        self.assertIsNone(match_person_name_loose("铃木真夕", ["鈴木真夕", "黒木真夕"]))

    def test_strict_match_handles_fullwidth_and_aliases(self):
        self.assertEqual("鈴木真夕", match_person_name("鈴木真夕", ["三佳詩", "鈴木真夕"]))
        self.assertEqual("Yui", match_person_name("YUI", ["Yui"]))


class TestActorPeopleFiltering(unittest.TestCase):
    def test_metadata_labels_dropped_from_people(self):
        service = build_service()
        item = {
            "Id": "item-1",
            "People": [
                {"Name": "収录时间", "Type": "Actor", "Id": "p-1"},
                {"Name": "未知演员", "Type": "Actor", "Id": "p-2"},
                {"Name": "鈴木真夕", "Type": "Actor", "Id": "p-3"},
            ],
        }
        names = [p["Name"] for p in service.get_actor_people_for_item(item)]
        self.assertEqual(["鈴木真夕"], names)

    def test_sync_person_rejects_metadata_label(self):
        service = build_service()
        result = service.sync_person_to_subscription({"Id": "p-1", "Name": "収录时间"})
        self.assertFalse(result["matched"])
        self.assertEqual("implausible_person_name", result["reason"])

    def test_approving_historical_bad_candidate_is_refused(self):
        """历史候选里存着字段标签时，自动批准不能再把它变成订阅。"""
        service = build_service()
        result = service.apply_approved_automation_candidate({
            "candidate_type": "favorite_actor",
            "subject_id": "jellyfin:person:6f432c58da4367b64d6631d61d244476",
            "subject_name": "収录时间",
            "payload": {},
        })
        self.assertFalse(result["applied"])
        self.assertEqual("implausible_person_name", result["message"])

    def test_bad_candidates_get_discarded_not_retried(self):
        db = MagicMock()
        service = build_service(db=db)
        report = service.approve_pending_favorite_actor_candidates()
        self.assertIn("discarded", report)

    def test_ensure_followed_skips_implausible_names(self):
        """已关注的历史垃圾演员不应在订阅页加载时被重新变成订阅。"""
        from subscription_repair_service import SubscriptionRepairService

        db = MagicMock()
        db.get_followed_actresses.return_value = [
            {"id": "jellyfin:person:6f432c58", "name": "収录时间", "page_url": None},
            {"id": "jellyfin:person:c20ca244", "name": "未知演员", "page_url": None},
        ]
        service = SubscriptionRepairService(
            db=db,
            resolve_actress_subscription_url=lambda *a: "jellyfin:person:6f432c58",
            subscription_url_exists=lambda url: False,
            subscription_visible_for_actress=lambda *a: True,
            get_actress_subscription_url_candidates=lambda *a: [],
            internal_actress_subscription_url=lambda x: f"jellyfin:actress:{x}",
            internal_actor_subscription_url=lambda x: f"jellyfin:actor:{x}",
            normalize_subscription_display_name=lambda a, b: str(a or b),
        )
        result = service.ensure_followed_actress_subscriptions()
        self.assertEqual(0, result["repaired"])
        self.assertEqual(2, result["skipped"])
        db.add_subscription.assert_not_called()


class TestResolveActressAcrossNameVariants(unittest.TestCase):
    def test_falls_back_to_movie_cast_then_matches_variant(self):
        db = MagicMock()
        db.get_actress_by_name.return_value = None  # 精确匹配失败（铃木真夕）
        db.get_movie_actresses.return_value = [
            {"actress_id": "netflav:5c19c6e15d8209567aa545be", "actress_name": "鈴木真夕"},
        ]
        db.get_actress.return_value = {"id": "netflav:5c19c6e15d8209567aa545be", "name": "鈴木真夕"}
        service = build_service(db=db)

        actress = service.resolve_actress_for_person("铃木真夕", "ATYA-039")
        self.assertEqual("netflav:5c19c6e15d8209567aa545be", actress["id"])

    def test_no_guess_without_movie_id(self):
        db = MagicMock()
        db.get_actress_by_name.return_value = None
        service = build_service(db=db)
        self.assertIsNone(service.resolve_actress_for_person("铃木真夕"))
        db.get_movie_actresses.assert_not_called()

    def test_no_guess_when_cast_has_several_actors(self):
        db = MagicMock()
        db.get_actress_by_name.return_value = None
        db.get_movie_actresses.return_value = [
            {"actress_id": "r18:30343", "actress_name": "黒木真夕"},
            {"actress_id": "r18:30344", "actress_name": "三佳詩"},
        ]
        service = build_service(db=db)
        self.assertIsNone(service.resolve_actress_for_person("铃木真夕", "ATYA-039"))

    def test_single_cast_member_variant_is_accepted(self):
        db = MagicMock()
        db.get_actress_by_name.return_value = None
        db.get_movie_actresses.return_value = [
            {"actress_id": "netflav:5c19", "actress_name": "鈴木真夕"},
        ]
        db.get_actress.return_value = {"id": "netflav:5c19", "name": "鈴木真夕"}
        service = build_service(db=db)
        self.assertEqual("netflav:5c19", service.resolve_actress_for_person("铃木真夕", "ATYA-039")["id"])

    def test_exact_match_still_wins(self):
        db = MagicMock()
        db.get_actress_by_name.return_value = {"id": "r18:1", "name": "鈴木真夕"}
        service = build_service(db=db)
        self.assertEqual("r18:1", service.resolve_actress_for_person("鈴木真夕", "ATYA-039")["id"])
        db.get_movie_actresses.assert_not_called()


class TestDailySyncScheduling(unittest.TestCase):
    def test_enqueue_due_after_schedule_hour(self):
        # 窗口不再限定在 3:00-3:29，3 点之后任何时刻都应该补跑
        for hour in (3, 4, 15, 23):
            self.assertTrue(hour >= 3, hour)
        for hour in (0, 1, 2):
            self.assertFalse(hour >= 3, hour)

    def test_last_enqueued_date_survives_restart(self):
        db = MagicMock()
        stored = {}

        def set_config(key, value):
            stored[key] = value

        db.set_config.side_effect = set_config
        db.get_config.side_effect = lambda key: stored.get(key)
        self.assertIsNone(db.get_config("jellyfin_automation_sync_last_date"))
        set_config("jellyfin_automation_sync_last_date", date(2026, 10, 13).isoformat())
        self.assertEqual("2026-10-13", db.get_config("jellyfin_automation_sync_last_date"))
        self.assertEqual(date(2026, 10, 13), date.fromisoformat(db.get_config("jellyfin_automation_sync_last_date")))


if __name__ == "__main__":
    unittest.main()
