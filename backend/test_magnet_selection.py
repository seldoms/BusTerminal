"""选种逻辑的回归测试（2026-11-11 AVOP-069 事故）。

背景：AVOP-069 的 JavBus 详情页给出 20 个候选磁力。旧实现按
「分辨率 → 有字幕 → 体积」选中 `AVOP-069.HD高清`（3.92GB / 标 720P），
而这个种子在 tracker 上**只有 1 个做种** → qB 永远停在 `metaDL`，
下载根本没开始。同号里做种最多的 `AVOP-069.mp4`（12 个种）却没被选中。

本文件锁三件事：
1. 分辨率解析：`HD` 不能再靠裸子串撞（`FullHD` 是 1080P，不是 720P）。
2. 未标注分辨率的候选不能被「有标注组」压制。
3. 首选是死种时，改用确实有做种的候选；查不到做种数时保持原行为。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import magnet_availability
from scraper import JavBusScraper


def _magnet(title, size_bytes=1024, magnet=None, resolution=None, subtitle=False):
    return {
        "title": title,
        "size": f"{size_bytes / (1024 ** 3):.2f}GB",
        "size_bytes": size_bytes,
        "magnet": magnet or ("magnet:?xt=urn:btih:" + ("a" * 40) + "&dn=x"),
        "resolution": resolution,
        "has_subtitle": subtitle,
    }


# ---------------- 分辨率解析 ----------------

def test_fullhd_is_not_720p():
    """`FullHD` 含 'HD' 子串，裸子串匹配会把它判成 720P。"""
    scraper = JavBusScraper.__new__(JavBusScraper)
    info = scraper._parse_magnet_info("ABC-123 FullHD", "2.00GB", "magnet:?xt=urn:btih:" + "a" * 40)
    assert info["resolution"] == "1080P", info


def test_hd_alone_is_720p():
    scraper = JavBusScraper.__new__(JavBusScraper)
    info = scraper._parse_magnet_info("ABC-123 HD", "2.00GB", "magnet:?xt=urn:btih:" + "a" * 40)
    assert info["resolution"] == "720P", info


def test_explicit_1080p_wins_over_hd_substring():
    scraper = JavBusScraper.__new__(JavBusScraper)
    info = scraper._parse_magnet_info("ABC-123 1080P", "2.00GB", "magnet:?xt=urn:btih:" + "a" * 40)
    assert info["resolution"] == "1080P", info


def test_4k_and_2160p():
    scraper = JavBusScraper.__new__(JavBusScraper)
    for title in ("ABC-123 4K", "ABC-123 2160P", "ABC-123 UHD"):
        info = scraper._parse_magnet_info(title, "8.00GB", "magnet:?xt=urn:btih:" + "a" * 40)
        assert info["resolution"] == "4K", (title, info)


def test_chinese_gaoqing_is_recognised_not_null():
    """中文「高清」必须被识别 —— 否则整片候选全落 Unknown 组。"""
    scraper = JavBusScraper.__new__(JavBusScraper)
    info = scraper._parse_magnet_info("ABC-123.mkv高清", "4.96GB", "magnet:?xt=urn:btih:" + "a" * 40)
    assert info["resolution"] is not None, info


def test_chaoqing_outranks_gaoqing():
    scraper = JavBusScraper.__new__(JavBusScraper)
    info = scraper._parse_magnet_info("ABC-123 超清", "4.96GB", "magnet:?xt=urn:btih:" + "a" * 40)
    assert info["resolution"] == "4K", info


def test_unlabelled_title_has_no_resolution():
    scraper = JavBusScraper.__new__(JavBusScraper)
    info = scraper._parse_magnet_info("ABC-123.mp4", "1.95GB", "magnet:?xt=urn:btih:" + "a" * 40)
    assert info["resolution"] is None, info


def test_subtitle_detection_keeps_working():
    scraper = JavBusScraper.__new__(JavBusScraper)
    info = scraper._parse_magnet_info("ABC-123-中文字幕", "1.78GB", "magnet:?xt=urn:btih:" + "a" * 40)
    assert info["has_subtitle"] is True, info


# ---------------- 选种：未标注组不能被压制 ----------------

def test_unlabelled_bigger_candidate_is_not_suppressed():
    """AVOP-069 实况：标 720P 的 3.92GB vs 未标注的 4.96GB，
    旧实现会选前者（分组命中就锁定），应该按体积选后者。"""
    magnets = [
        _magnet("AVOP-069.HD高清", 3_920_000_000, resolution="720P"),
        _magnet("AVOP-069.mkv高清", 4_960_000_000, resolution=None),
    ]
    best = JavBusScraper.select_best_magnet(JavBusScraper.__new__(JavBusScraper), magnets, "1080P")
    assert best["title"] == "AVOP-069.mkv高清", best


def test_all_unlabelled_picks_largest():
    magnets = [
        _magnet("ABC.mp4", 1_950_000_000),
        _magnet("ABC.mkv", 4_960_000_000),
    ]
    best = JavBusScraper.select_best_magnet(JavBusScraper.__new__(JavBusScraper), magnets, "1080P")
    assert best["title"] == "ABC.mkv", best


def test_explicit_1080p_group_still_preferred():
    magnets = [
        _magnet("ABC 1080P", 2_000_000_000, resolution="1080P"),
        _magnet("ABC 4K", 8_000_000_000, resolution="4K"),
    ]
    best = JavBusScraper.select_best_magnet(JavBusScraper.__new__(JavBusScraper), magnets, "1080P")
    assert best["title"] == "ABC 1080P", best


def test_subtitle_wins_within_same_group():
    magnets = [
        _magnet("ABC 1080P", 2_000_000_000, resolution="1080P"),
        _magnet("ABC 1080P 中文字幕", 1_800_000_000, resolution="1080P", subtitle=True),
    ]
    best = JavBusScraper.select_best_magnet(JavBusScraper.__new__(JavBusScraper), magnets, "1080P")
    assert best["has_subtitle"] is True, best


# ---------------- 做种评估 ----------------

def test_extract_info_hash():
    assert magnet_availability.extract_info_hash(
        "magnet:?xt=urn:btih:D6DC21035A596BFB9F7350E8EDE17326111F7F97&dn=x"
    ) == "D6DC21035A596BFB9F7350E8EDE17326111F7F97"
    assert magnet_availability.extract_info_hash("not a magnet") is None
    assert magnet_availability.extract_info_hash("") is None


def test_infohash_to_bytes_supports_hex_and_base32():
    raw = magnet_availability._infohash_to_bytes("a" * 40)
    assert raw is not None and len(raw) == 20
    base32_hash = "A" * 32
    raw32 = magnet_availability._infohash_to_bytes(base32_hash)
    assert raw32 is not None and len(raw32) == 20
    assert magnet_availability._infohash_to_bytes("zz") is None


def _stub_seeds(monkeypatch, counts):
    """按 infohash 返回做种数；count_seeds 与 count_seeds_batch 都要打桩。"""
    def _one(magnet, opener=None):
        return counts.get(magnet_availability.extract_info_hash(magnet))

    def _batch(magnets, opener=None):
        return {
            index: counts.get(magnet_availability.extract_info_hash(m.get("magnet")))
            for index, m in enumerate(magnets)
        }

    monkeypatch.setattr(magnet_availability, "count_seeds", _one)
    monkeypatch.setattr(magnet_availability, "count_seeds_batch", _batch)


def test_pick_alive_replaces_dead_preferred(monkeypatch):
    """首选 0 个种 → 换成有做种的候选。"""
    dead = _magnet("AVOP-069.HD高清", 3_920_000_000, magnet="magnet:?xt=urn:btih:" + "d" * 40)
    alive = _magnet("AVOP-069.mp4", 1_950_000_000, magnet="magnet:?xt=urn:btih:" + "a" * 40)
    _stub_seeds(monkeypatch, {"d" * 40: 0, "a" * 40: 12})
    chosen = magnet_availability.pick_alive_candidate([dead, alive], dead)
    assert chosen["title"] == "AVOP-069.mp4", chosen


def test_pick_alive_replaces_single_seed_preferred(monkeypatch):
    """1 个种同样不可靠 —— 实测卡死 metaDL 的正是这个。

    这是 v2.19 上线后的生产实测修正：只把「0 个种」当死种不够，
    1 个种的种子做种者一离线就没了，tracker 的 complete 还会被僵尸 announce 虚报。
    """
    thin = _magnet("AVOP-069.HD高清", 3_920_000_000, magnet="magnet:?xt=urn:btih:" + "d" * 40)
    alive = _magnet("AVOP-069.mp4", 1_950_000_000, magnet="magnet:?xt=urn:btih:" + "a" * 40)
    _stub_seeds(monkeypatch, {"d" * 40: 1, "a" * 40: 12})
    chosen = magnet_availability.pick_alive_candidate([thin, alive], thin)
    assert chosen["title"] == "AVOP-069.mp4", chosen


def test_pick_alive_keeps_preferred_when_it_has_seeds(monkeypatch):
    """做种数达到可靠阈值 → 直接用首选，不去折腾其余候选。"""
    preferred = _magnet("good", 2_000_000_000, magnet="magnet:?xt=urn:btih:" + "a" * 40)
    other = _magnet("other", 9_000_000_000, magnet="magnet:?xt=urn:btih:" + "b" * 40)
    _stub_seeds(monkeypatch, {"a" * 40: 3, "b" * 40: 20})
    chosen = magnet_availability.pick_alive_candidate([preferred, other], preferred)
    assert chosen["title"] == "good", chosen


def test_pick_alive_keeps_preferred_when_unknown(monkeypatch):
    """查不到做种数 = 未知 ≠ 死种，不能因此乱换。"""
    preferred = _magnet("unknown", 2_000_000_000, magnet="magnet:?xt=urn:btih:" + "a" * 40)
    other = _magnet("other", 9_000_000_000, magnet="magnet:?xt=urn:btih:" + "b" * 40)
    _stub_seeds(monkeypatch, {"a" * 40: None, "b" * 40: None})
    chosen = magnet_availability.pick_alive_candidate([preferred, other], preferred)
    assert chosen["title"] == "unknown", chosen


def test_pick_alive_keeps_preferred_when_all_dead(monkeypatch):
    preferred = _magnet("dead1", 2_000_000_000, magnet="magnet:?xt=urn:btih:" + "a" * 40)
    other = _magnet("dead2", 9_000_000_000, magnet="magnet:?xt=urn:btih:" + "b" * 40)
    _stub_seeds(monkeypatch, {"a" * 40: 0, "b" * 40: 0})
    chosen = magnet_availability.pick_alive_candidate([preferred, other], preferred)
    assert chosen["title"] == "dead1", chosen


def test_pick_alive_keeps_preferred_when_no_candidate_is_better(monkeypatch):
    """其余候选做种数不严格优于首选 → 不换（首选已是同档最好的）。"""
    preferred = _magnet("thin", 2_000_000_000, magnet="magnet:?xt=urn:btih:" + "a" * 40)
    other = _magnet("same", 9_000_000_000, magnet="magnet:?xt=urn:btih:" + "b" * 40)
    _stub_seeds(monkeypatch, {"a" * 40: 1, "b" * 40: 1})
    chosen = magnet_availability.pick_alive_candidate([preferred, other], preferred)
    assert chosen["title"] == "thin", chosen


def test_pick_alive_picks_the_most_seeded_candidate(monkeypatch):
    """多个候选都有种时，选做种最多的那个（不是体积最大的）。"""
    preferred = _magnet("preferred", 5_000_000_000, magnet="magnet:?xt=urn:btih:" + "d" * 40)
    mid = _magnet("mid", 3_000_000_000, magnet="magnet:?xt=urn:btih:" + "b" * 40)
    best = _magnet("best", 1_950_000_000, magnet="magnet:?xt=urn:btih:" + "a" * 40)
    _stub_seeds(monkeypatch, {"d" * 40: 1, "b" * 40: 5, "a" * 40: 12})
    chosen = magnet_availability.pick_alive_candidate([preferred, mid, best], preferred)
    assert chosen["title"] == "best", chosen


def test_count_seeds_batch_returns_max_across_trackers(monkeypatch):
    """批量查询：同一候选多个 tracker 取最大值；查不到记 None。"""
    magnets = [
        _magnet("a", magnet="magnet:?xt=urn:btih:" + "a" * 40),
        _magnet("b", magnet="magnet:?xt=urn:btih:" + "b" * 40),
    ]

    def _fake_scrape(infohash, endpoint, opener):
        table = {
            ("a" * 40, "http://tracker.opentrackr.org:1337/scrape"): 6,
            ("a" * 40, "http://tracker.dler.org:80/scrape"): 2,
            ("b" * 40, "http://tracker.opentrackr.org:1337/scrape"): 0,
        }
        return table.get((infohash, endpoint))

    monkeypatch.setattr(magnet_availability, "_scrape_one", _fake_scrape)
    counts = magnet_availability.count_seeds_batch(magnets)
    assert counts == {0: 6, 1: 0}, counts


def test_annotate_seed_counts_fills_every_candidate(monkeypatch):
    magnets = [
        _magnet("a", magnet="magnet:?xt=urn:btih:" + "a" * 40),
        _magnet("b", magnet="magnet:?xt=urn:btih:" + "b" * 40),
    ]

    def _fake_batch(items, opener=None):
        return {0: 7, 1: None}

    monkeypatch.setattr(magnet_availability, "count_seeds_batch", _fake_batch)
    result = magnet_availability.annotate_seed_counts(magnets)
    assert result[0]["seed_count"] == 7
    assert result[1]["seed_count"] is None


def test_select_alive_magnet_end_to_end(monkeypatch):
    """scraper.select_alive_magnet：死种首选被替换。"""
    scraper = JavBusScraper.__new__(JavBusScraper)
    dead = _magnet("AVOP-069.HD高清", 3_920_000_000, magnet="magnet:?xt=urn:btih:" + "d" * 40)
    alive = _magnet("AVOP-069.mp4", 1_950_000_000, magnet="magnet:?xt=urn:btih:" + "a" * 40)
    _stub_seeds(monkeypatch, {"d" * 40: 0, "a" * 40: 12})
    chosen = scraper.select_alive_magnet([dead, alive], "1080P")
    assert chosen["title"] == "AVOP-069.mp4", chosen


def test_select_alive_magnet_survives_scraper_error(monkeypatch):
    """评估挂掉时必须退回原选择，不能把下载搞挂。"""
    scraper = JavBusScraper.__new__(JavBusScraper)
    only = _magnet("only", 2_000_000_000)

    def _boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(magnet_availability, "pick_alive_candidate", _boom)
    chosen = scraper.select_alive_magnet([only], "1080P")
    assert chosen["title"] == "only", chosen


def test_select_alive_magnet_can_be_disabled():
    scraper = JavBusScraper.__new__(JavBusScraper)
    only = _magnet("only", 2_000_000_000)
    chosen = scraper.select_alive_magnet([only], "1080P", check_seeds=False)
    assert chosen["title"] == "only", chosen
