# -*- coding: utf-8 -*-
"""女优元数据回填：用**日文原名**去 netflav 搜，而不是中文译名。

背景（2026-11-07 生产实测）：
- 库里 `actresses.name` 存的是**中文译名**（`天使萌` / `田野忧` / `藤川之风`），
  而 netflav 站上索引的是**日文原名**（`天使もえ` / `田野憂` / `藤川乃風`）。
- 实测 20 条候选：按中文译名搜**命中 0 条**，按日文原名搜**命中 9 条**。
  —— 所以 `actress_metadata_backfill_loop` 一直报 `actress_metadata_sources_unavailable`，
  421 条女优的资料全部卡在 `incomplete_metadata`。
- 反查路径现成：translations 表存的就是 `original_text → translated_text`，
  按译文反查即得日文原名。

本文件锁死：
1. 候选名的构造顺序（原名优先，中文译名兜底）。
2. netflav 侧「多候选名依次搜索」的行为。
3. 回填循环确实按候选名去搜，而不是只搜译名。
"""

from unittest.mock import MagicMock

from actress_service import ActressService
from netflav_scraper import NetflavScraper


def _build_service(db=None, netflav=None):
    db = db or MagicMock()
    netflav = netflav or MagicMock()
    service = ActressService(
        db=db,
        netflav_scraper=netflav,
        logger=MagicMock(),
        task_registry=MagicMock(),
        stop_event=MagicMock(),
        safe_number=lambda value, default=0: default,
        safe_exception_label=lambda exc: type(exc).__name__,
        display_value_for_log=lambda value: str(value),
        as_bool=lambda value, default=False: bool(value),
        resolve_actress_subscription_url=lambda *a, **k: None,
        get_actress_subscription_url_candidates=lambda *a, **k: [],
        get_subscription_delete_url_candidates=lambda *a, **k: [],
        subscription_url_exists=lambda url: False,
        subscription_visible_for_actress=lambda *a, **k: True,
        normalize_subscription_display_name=lambda value: value,
        metadata_retry_seconds=3600,
        metadata_batch_size=10,
        metadata_max_attempts=5,
    )
    return service


# MARK: - 候选名构造


def test_candidates_prefer_japanese_original_name():
    """译名的原文（日文原名）必须排第一 —— 站上索引的是它。"""
    db = MagicMock()
    db.get_translation_originals.return_value = ["天使もえ"]
    service = _build_service(db=db)

    candidates = service._metadata_search_candidates({
        "id": "name:天使萌",
        "name": "天使萌",
        "name_en": "Moe Amatsuka",
        "aliases": "天使萌,Moe Amatsuka",
    })

    assert candidates[0] == "天使もえ"
    assert "天使萌" in candidates          # 中文译名兜底仍在
    assert "Moe Amatsuka" in candidates    # 英文名也在
    db.get_translation_originals.assert_called_once_with("天使萌", limit=3)


def test_candidates_survive_reverse_lookup_failure():
    """反查失败不能把整轮回填带崩 —— 至少还能用译名兜底。"""
    db = MagicMock()
    db.get_translation_originals.side_effect = RuntimeError("boom")
    service = _build_service(db=db)

    candidates = service._metadata_search_candidates({"id": "x", "name": "天使萌"})

    assert candidates == ["天使萌"]


def test_candidates_are_deduplicated():
    db = MagicMock()
    db.get_translation_originals.return_value = ["天使もえ"]
    service = _build_service(db=db)

    candidates = service._metadata_search_candidates({
        "name": "天使もえ",
        "aliases": "天使もえ,天使萌",
        "name_en": "Moe Amatsuka",
    })

    assert len(candidates) == len(set(candidates))


# MARK: - netflav 多候选搜索


def test_search_by_candidates_returns_first_hit():
    scraper = NetflavScraper(domain_provider=lambda: [])
    calls = []

    def fake_search(keyword, force_refresh=False):
        calls.append(keyword)
        if keyword == "天使もえ":
            return [{"_id": "1", "name": "天使もえ", "name_en": "Moe Amatsuka"}]
        return []

    scraper.search_actress = fake_search

    match = scraper.search_actress_by_candidates(["天使もえ", "天使萌"])

    assert match is not None
    assert match["name"] == "天使もえ"
    assert calls == ["天使もえ"]


def test_search_by_candidates_skips_empty_and_erroring_keywords():
    scraper = NetflavScraper(domain_provider=lambda: [])

    def fake_search(keyword, force_refresh=False):
        if keyword == "":
            return []
        if keyword == "坏":
            raise RuntimeError("network")
        if keyword == "天使もえ":
            return [{"_id": "1", "name": "天使もえ"}]
        return []

    scraper.search_actress = fake_search

    match = scraper.search_actress_by_candidates(["", "坏", "天使もえ"])

    assert match is not None
    assert match["name"] == "天使もえ"


def test_search_by_candidates_returns_none_when_all_miss():
    scraper = NetflavScraper(domain_provider=lambda: [])
    scraper.search_actress = lambda keyword, force_refresh=False: []

    assert scraper.search_actress_by_candidates(["天使もえ"]) is None


def test_backfill_by_candidates_passes_target_id_through():
    scraper = NetflavScraper(domain_provider=lambda: [])
    scraper.search_actress = lambda keyword, force_refresh=False: [
        {"_id": "1", "name": "天使もえ"}
    ]
    captured = {}

    def fake_detail(list_item, force_refresh=False, save_movies=True, target_id=None):
        captured["target_id"] = target_id
        captured["save_movies"] = save_movies
        return {"id": target_id, "name": "天使もえ", "avatar": "a", "birth_date": "1994-07-09", "height": "150"}

    scraper.get_actress_detail = fake_detail

    result = scraper.backfill_actress_by_candidates(
        ["天使もえ"], target_id="name:天使萌", save_movies=False
    )

    assert result is not None
    assert captured["target_id"] == "name:天使萌"
    assert captured["save_movies"] is False


# MARK: - 回填循环走候选名


def test_backfill_loop_searches_with_original_name_not_translated_name():
    """核心回归：回填必须用日文原名搜，否则一条都补不上。"""
    db = MagicMock()
    db.get_translation_originals.return_value = ["天使もえ"]
    actress = {
        "id": "name:天使萌",
        "name": "天使萌",
        "name_en": "",
        "aliases": "",
        "metadata_attempt_count": 0,
    }
    db.get_actresses_needing_metadata.return_value = [actress]
    db.mark_actress_metadata_attempt.return_value = True

    netflav = MagicMock()
    netflav.backfill_actress_by_candidates.return_value = {
        "id": "name:天使萌",
        "name": "天使もえ",
        "avatar": "https://example/a.jpg",
        "birth_date": "1994-07-09",
        "height": "150",
    }

    service = _build_service(db=db, netflav=netflav)
    stop_event = MagicMock()
    # while 头部检查 → for 循环内检查 → 下一轮 while 头部检查
    stop_event.is_set.side_effect = [False, False, True]
    service.stop_event = stop_event

    service.metadata_backfill_loop()

    kwargs = netflav.backfill_actress_by_candidates.call_args
    assert kwargs is not None
    candidates = kwargs[0][0]
    assert candidates[0] == "天使もえ"
    assert "天使萌" in candidates
    db.mark_actress_metadata_attempt.assert_called_once()
    success_flag = db.mark_actress_metadata_attempt.call_args[1]["success"]
    assert success_flag is True


# MARK: - 错误语义：站上查无此人 ≠ 源不可用


def _run_backfill_loop(service, db, netflav, candidates=1):
    # is_set() 调用顺序：while 头部检查 1 次 + 每个候选 1 次 + 下一轮 while 1 次
    stop_event = MagicMock()
    stop_event.is_set.side_effect = [False] * (candidates + 1) + [True]
    service.stop_event = stop_event
    service.metadata_backfill_loop()


def test_not_found_does_not_mark_task_as_error():
    """站上查无此人（`incomplete_metadata`）是正常结果，不能把任务卡拉红。

    2026-11-07 实测：一批 10 条里 9 条补上、1 条站上没收录，
    按旧逻辑整卡就标了 `actress_metadata_sources_unavailable`。
    """
    db = MagicMock()
    db.get_translation_originals.return_value = ["天使もえ"]
    db.get_actresses_needing_metadata.return_value = [
        {"id": "name:查无此人", "name": "查无此人", "metadata_attempt_count": 0}
    ]
    db.mark_actress_metadata_attempt.return_value = True

    netflav = MagicMock()
    netflav.backfill_actress_by_candidates.return_value = None  # 搜不到

    service = _build_service(db=db, netflav=netflav)
    _run_backfill_loop(service, db, netflav)

    service.task_registry.mark_error.assert_not_called()
    service.task_registry.mark_success.assert_called_once()
    summary = service.task_registry.mark_success.call_args[1]["summary"]
    assert summary["not_found"] == 1
    assert summary["failed"] == 0
    assert summary["completed"] == 0


def test_source_exception_marks_task_as_error():
    """真的抓不动了（抛异常）才标错误。"""
    db = MagicMock()
    db.get_translation_originals.return_value = ["天使もえ"]
    db.get_actresses_needing_metadata.return_value = [
        {"id": "name:天使萌", "name": "天使萌", "metadata_attempt_count": 0}
    ]
    db.mark_actress_metadata_attempt.return_value = True

    netflav = MagicMock()
    netflav.backfill_actress_by_candidates.side_effect = RuntimeError("boom")

    service = _build_service(db=db, netflav=netflav)
    _run_backfill_loop(service, db, netflav)

    service.task_registry.mark_error.assert_called_once()
    err = service.task_registry.mark_error.call_args[0][1]
    assert err.args[0] == "actress_metadata_sources_unavailable"
    summary = service.task_registry.mark_error.call_args[1]["summary"]
    assert summary["failed"] == 1


def test_mixed_batch_counts_are_separated():
    """成功 / 查无此人 / 源异常 三种要分开计数。"""
    db = MagicMock()
    db.get_translation_originals.return_value = ["原名"]
    db.get_actresses_needing_metadata.return_value = [
        {"id": "a", "name": "甲", "metadata_attempt_count": 0},
        {"id": "b", "name": "乙", "metadata_attempt_count": 0},
        {"id": "c", "name": "丙", "metadata_attempt_count": 0},
    ]
    db.mark_actress_metadata_attempt.return_value = True

    netflav = MagicMock()
    responses = [
        {"avatar": "a", "birth_date": "1994-07-09", "height": "150"},  # 成功
        None,                                                            # 站上查无此人
        RuntimeError("boom"),                                            # 源不可用
    ]

    def fake_backfill(candidates, target_id=None, save_movies=False):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    netflav.backfill_actress_by_candidates.side_effect = fake_backfill

    service = _build_service(db=db, netflav=netflav)
    _run_backfill_loop(service, db, netflav, candidates=3)

    service.task_registry.mark_error.assert_called_once()
    summary = service.task_registry.mark_error.call_args[1]["summary"]
    assert summary["attempted"] == 3
    assert summary["completed"] == 1
    assert summary["not_found"] == 1
    assert summary["failed"] == 1
