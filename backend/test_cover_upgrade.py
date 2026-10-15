"""封面升级规则测试。

核心保护一条：**只升不降**。
`save_movie` 的 UPSERT 是 `cover = EXCLUDED.cover`，若这里放行降级，
一次抓取失败就会把高清封面覆盖成模糊图 —— 而且很难被发现。
"""
import pytest

from cover_upgrade_service import (
    COVER_SOURCE_PRIORITY,
    CoverUpgradeService,
    cover_tier,
    extract_javdb_cover,
    is_javdb_cover,
    should_replace_cover,
)

JDB = "https://c0.jdbstatic.com/covers/5n/5navDM.jpg"
DMM = "https://pics.dmm.co.jp/digital/video/118abp00123/118abp00123pl.jpg"
JAVBUS = "https://pics.javbus.me/pics/cover/600x400_ABP-123.jpg"


# ------------------------------------------------------------------ 分档


def test_javdb_outranks_dmm_and_javbus():
    # 实测：JavDB 800×538/142KB，DMM 590×800/19KB
    assert cover_tier(JDB) < cover_tier(DMM) < cover_tier(JAVBUS)


def test_unknown_source_is_lowest():
    assert cover_tier("https://example.com/a.jpg") >= len(COVER_SOURCE_PRIORITY)
    assert cover_tier("") >= len(COVER_SOURCE_PRIORITY)


def test_is_javdb_cover():
    assert is_javdb_cover(JDB) is True
    assert is_javdb_cover(DMM) is False
    assert is_javdb_cover("") is False


# ------------------------------------------------------------------ 只升不降


@pytest.mark.parametrize(
    "existing,candidate,expected",
    [
        # 升级路径
        (DMM, JDB, True),
        (JAVBUS, JDB, True),
        ("", JDB, True),
        ("", DMM, True),
        # 降级路径必须拒绝 —— 这是本模块最重要的一条
        (JDB, DMM, False),
        (JDB, JAVBUS, False),
        (DMM, JAVBUS, False),
        # 同档位不重复覆盖（避免反复请求）
        (JDB, JDB, False),
        # 空候选永远不覆盖
        (JDB, "", False),
        ("", "", False),
    ],
)
def test_should_replace_cover_only_upgrades(existing, candidate, expected):
    assert should_replace_cover(existing, candidate) is expected


def test_failed_lookup_never_downgrades_existing():
    """回归：查不到高清图时必须保留原图，否则越维护画质越差。"""
    for candidate in ("", None, DMM, JAVBUS):
        assert should_replace_cover(JDB, candidate) is False


# ------------------------------------------------------------------ 抽取


def test_extract_javdb_cover_from_text():
    assert extract_javdb_cover(f'前缀 {JDB} 后缀') == JDB
    assert extract_javdb_cover(f'{{"cover": "{JDB}"}}') == JDB


def test_extract_returns_empty_when_absent():
    assert extract_javdb_cover(f"cover={DMM}") == ""
    assert extract_javdb_cover("") == ""


# ------------------------------------------------------------------ 服务


class _Log:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass


def test_pick_upgrades_when_better():
    svc = CoverUpgradeService(db=None, logger_=_Log())
    final, replaced = svc.pick_from_source(existing=DMM, source_cover=JDB)
    assert final == JDB and replaced is True


def test_pick_keeps_existing_when_worse():
    svc = CoverUpgradeService(db=None, logger_=_Log())
    final, replaced = svc.pick_from_source(existing=JDB, source_cover=DMM)
    assert final == JDB and replaced is False


def test_merge_from_any_text_upgrades():
    svc = CoverUpgradeService(db=None, logger_=_Log())
    final, replaced = svc.merge_from_any_text(existing=DMM, text=f'cover={JDB}')
    assert final == JDB and replaced is True


def test_merge_ignores_non_javdb_text():
    svc = CoverUpgradeService(db=None, logger_=_Log())
    final, replaced = svc.merge_from_any_text(existing=JDB, text=f"cover={DMM}")
    assert final == JDB and replaced is False


def test_needs_upgrade():
    svc = CoverUpgradeService(db=None, logger_=_Log())
    assert svc.needs_upgrade(DMM) is True
    assert svc.needs_upgrade("") is True
    assert svc.needs_upgrade(JDB) is False