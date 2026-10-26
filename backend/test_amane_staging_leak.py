"""投递暂存目录泄漏检测（2026-10-17：Jellyfin 把 _incoming 里的广告片当影片入库）。"""
from amane_service import find_staging_leaks, staging_leak_check

DELIVERY = "/hp/_incoming"


def test_find_staging_leaks_matches_children_only():
    items = [
        {"Id": "1", "Name": "社 區 最 新 情 報", "Path": "/hp/_incoming/MFCW-065/社 區 最 新 情 報.mp4"},
        {"Id": "2", "Name": "JUR-880", "Path": "/hp/マドンナ(Madonna)/JUR-880/JUR-880.mp4"},
        # 前缀相近但不能算命中
        {"Id": "3", "Name": "_incoming2", "Path": "/hp/_incoming2/X/X.mp4"},
        # 暂存目录自身（没有下级路径）也不算命中
        {"Id": "4", "Name": "_incoming", "Path": "/hp/_incoming"},
    ]

    leaks = find_staging_leaks(items, DELIVERY)

    assert [leak["id"] for leak in leaks] == ["1"]
    assert leaks[0]["name"] == "社 區 最 新 情 報"


def test_find_staging_leaks_ignores_items_without_path():
    items = [{"Id": "1", "Name": "电视直播", "Path": None}, {"Id": "2", "Name": "x"}]

    assert find_staging_leaks(items, DELIVERY) == []


def test_find_staging_leaks_handles_empty_delivery_folder():
    items = [{"Id": "1", "Name": "x", "Path": "/hp/_incoming/X/x.mp4"}]

    assert find_staging_leaks(items, "") == []
    assert find_staging_leaks(items, "   ") == []


def test_find_staging_leaks_tolerates_trailing_slash():
    items = [{"Id": "1", "Name": "x", "Path": "/hp/_incoming/X/x.mp4"}]

    assert len(find_staging_leaks(items, "/hp/_incoming/")) == 1


def test_staging_leak_check_ok_when_clean():
    check = staging_leak_check([{"Id": "1", "Name": "a", "Path": "/hp/S1/a/a.mp4"}], DELIVERY)

    assert check["status"] == "ok"
    assert check["hint"] == ""


def test_staging_leak_check_warns_with_ignore_hint():
    items = [
        {"Id": "1", "Name": "美女直播大秀 uun25.com", "Path": "/hp/_incoming/SSNI-676_FHD_CH/x.mp4"},
        {"Id": "2", "Name": "台 妹 子 線 上 現 場 直 播", "Path": "/hp/_incoming/MFCW-065/y.mp4"},
    ]

    check = staging_leak_check(items, DELIVERY)

    assert check["status"] == "warn"
    assert "2 条" in check["detail"]
    assert ".ignore" in check["hint"]
    assert "内容必须为空" in check["hint"]
