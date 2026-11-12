"""翻译回填游标推进的锁定测试（2026-11-09）。

背景：回填游标原本**只按日期**推进（`release_date <= cursor`），但同一天可能有
几百条待翻（实测 2026-01-22 有 213 条）。标题要等翻译落库才从 `NOT EXISTS` 里
消失 —— 只要当天有任意一条被质量闸门拦下（不落库），游标就永远卡在这天，
后面 122.9 万条一条都翻不到。

改成「日期|标题」双游标：同一天内按标题字典序继续往后翻。
"""

from unittest.mock import MagicMock

import pytest

from maintenance_service import MaintenanceService


def _svc(db=None):
    db = db or MagicMock()
    svc = MaintenanceService.__new__(MaintenanceService)
    svc.db = db
    svc.logger = MagicMock()
    svc.scraper = MagicMock()
    svc.scraper.ollama_translator = object()
    svc.scraper.pending_translation_queue_size.return_value = 0
    svc.scraper.queue_title_translations.return_value = 10
    return svc


def test_cursor_parses_plain_date():
    """旧的纯日期游标仍能解析（向后兼容）→ 标题为空。"""
    assert MaintenanceService.parse_backfill_cursor("2026-01-22") == ("2026-01-22", "")


def test_cursor_parses_date_and_title():
    assert MaintenanceService.parse_backfill_cursor("2026-01-22|作品A") == ("2026-01-22", "作品A")


def test_cursor_rejects_legacy_timestamp():
    """历史遗留的时间戳游标一律从头重来。"""
    date, title = MaintenanceService.parse_backfill_cursor("2026-01-22T10:00:00")
    assert date == MaintenanceService.BACKFILL_CURSOR_START
    assert title == ""


def test_cursor_empty_falls_back_to_start():
    assert MaintenanceService.parse_backfill_cursor("") == (
        MaintenanceService.BACKFILL_CURSOR_START, ""
    )
    assert MaintenanceService.parse_backfill_cursor(None) == (
        MaintenanceService.BACKFILL_CURSOR_START, ""
    )


def test_format_cursor():
    assert MaintenanceService.format_backfill_cursor("2026-01-22", "作品A") == "2026-01-22|作品A"
    assert MaintenanceService.format_backfill_cursor("2026-01-22", "") == "2026-01-22"


def test_backfill_advances_cursor_within_same_day():
    """同一天的多条标题 → 游标必须带标题推进，不能只停在日期上。"""
    db = MagicMock()
    db.get_config.return_value = "2026-01-22"
    db.get_translation_backfill_batch.return_value = [
        {"original_title": "作品A", "release_date": "2026-01-22"},
        {"original_title": "作品B", "release_date": "2026-01-22"},
        {"original_title": "作品C", "release_date": "2026-01-21"},
    ]
    svc = _svc(db)

    result = svc.run_translation_backfill_once(10)

    # 游标取「最旧那天的最后一条标题」
    assert result["cursor"] == "2026-01-21|作品C"
    # 取数时把标题游标也传下去了（关键：SQL 要能按标题继续往后翻）
    args = db.get_translation_backfill_batch.call_args
    assert args[0][0] == "2026-01-22"
    assert args[0][2] == ""  # 旧游标没有标题部分


def test_backfill_passes_title_cursor_to_query():
    """已有标题游标时，取数必须带上它 —— 否则同一天会反复取同一批。"""
    db = MagicMock()
    db.get_config.return_value = "2026-01-22|作品A"
    db.get_translation_backfill_batch.return_value = [
        {"original_title": "作品B", "release_date": "2026-01-22"},
    ]
    svc = _svc(db)

    result = svc.run_translation_backfill_once(10)

    args = db.get_translation_backfill_batch.call_args
    assert args[0][0] == "2026-01-22"
    assert args[0][2] == "作品A"
    assert result["cursor"] == "2026-01-22|作品B"


def test_backfill_restarts_when_exhausted():
    """扫完一轮 → 游标复位，从最新重新扫（捞漏翻的）。"""
    db = MagicMock()
    db.get_config.return_value = "2026-01-22|作品Z"
    db.get_translation_backfill_batch.return_value = []
    svc = _svc(db)

    result = svc.run_translation_backfill_once(10)

    assert result["sweep"] == "restart"
    db.set_config.assert_called_with(
        MaintenanceService.BACKFILL_CURSOR_KEY,
        MaintenanceService.BACKFILL_CURSOR_START,
    )


def test_backfill_skips_when_queue_full():
    """队列满 → 不推进游标（避免把没翻的标题空推过去）。"""
    db = MagicMock()
    db.get_config.return_value = "2026-01-22"
    svc = _svc(db)
    svc.scraper.pending_translation_queue_size.return_value = 99999

    result = svc.run_translation_backfill_once(10)

    assert result["skipped"] == "queue_full"
    db.set_config.assert_not_called()


def test_backfill_skips_without_local_llm():
    db = MagicMock()
    svc = _svc(db)
    svc.scraper.ollama_translator = None

    result = svc.run_translation_backfill_once(10)

    assert result["skipped"] == "local_llm_not_configured"


@pytest.mark.parametrize("bad", [None, "", "not-a-date", "2026"])
def test_cursor_never_crashes_on_garbage(bad):
    date, title = MaintenanceService.parse_backfill_cursor(bad)
    assert isinstance(date, str) and date
    assert isinstance(title, str)
