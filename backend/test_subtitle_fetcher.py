"""subtitle_fetcher 的单元测试。

夹具用 2026-11-03 从 subtitlecat.com 真实抓回的片段，避免用"看起来像"的假数据
把解析器养成对真实结构不敏感。
"""

from pathlib import Path

import pytest
import requests

import subtitle_fetcher as sf

from subtitle_fetcher import (
    CHINESE_LANG_PREFERENCE,
    choose_language_link,
    code_to_search_term,
    db_record_for_result,
    existing_subtitle,
    fetch_subtitle,
    language_from_subtitle_path,
    name_matches_code,
    parse_language_links,
    parse_search_results,
    parse_srt,
    pick_candidate,
    subtitle_target_path,
    validate_srt,
)


SEARCH_HTML = """
<tbody>
<tr>
<td><a href="subs/1710/IPZZ-937.zh-cn%28by%20transub%29.html">IPZZ-937.zh-cn(by transub)</a> (translated from Chinese)</td>
<td class="sub-table__stars"><span title="Rated good by users">&#128077;</span></td>
<td class="sub-table__metric"><span class="sub-table__metric-value">63 KB</span></td>
</tr>
<tr>
<td><a href="subs/1757/IPZZ-937.en.html">IPZZ-937.en</a> (translated from English)</td>
</tr>
<tr>
<td><a href="subs/9999/OTHER-111.zh-cn.html">OTHER-111.zh-cn</a></td>
</tr>
</tbody>
"""

DETAIL_HTML = """
<a href="/subs/1710/IPZZ-937.zh-cn(by transub)-zh-CN.srt">zh-CN</a>
<a href="/subs/1710/IPZZ-937.zh-cn(by transub)-zh-TW.srt">zh-TW</a>
<a href="/subs/1710/IPZZ-937.zh-cn(by transub)-en.srt">en</a>
<a href="/css/avsubtitles.css?n=108">not a subtitle</a>
"""

SRT_TEXT = """\ufeff1
00:00:00,000 --> 00:00:02,000
字幕组声明

2
00:00:50,960 --> 00:00:54,465
和她约会，真是太棒了。

3
00:01:02,650 --> 00:01:04,210
我说姐姐啊

4
00:01:04,510 --> 00:01:05,290
您没事吧？
"""


@pytest.mark.parametrize(
    "code,expected",
    [
        ("SNOS392", "SNOS-392"),
        ("START647", "START-647"),
        ("300MIUM1292", "300MIUM-1292"),
        ("SGSR40606", "SGSR-40606"),
        ("IPZZ-937", "IPZZ-937"),
        ("", ""),
    ],
)
def test_code_to_search_term(code, expected):
    assert code_to_search_term(code) == expected


@pytest.mark.parametrize(
    "name,code,expected",
    [
        ("IPZZ-937.zh-cn(by transub)", "IPZZ937", True),
        ("[SubtitleTools.com] URE-140_Mosaic", "URE140", True),
        ("SIVR-525.foo", "SIVR525", True),
        ("OTHER-111.zh-cn", "IPZZ937", False),
        ("", "IPZZ937", False),
        ("anything", "", False),
    ],
)
def test_name_matches_code(name, code, expected):
    assert name_matches_code(name, code) is expected


def test_parse_search_results_extracts_relative_links():
    items = parse_search_results(SEARCH_HTML)
    assert len(items) == 3
    assert items[0]["id"] == "1710"
    assert items[0]["name"] == "IPZZ-937.zh-cn(by transub)"
    assert items[0]["url"].startswith("https://subtitlecat.com/subs/1710/")


def test_pick_candidate_requires_code_match_and_prefers_chinese():
    items = parse_search_results(SEARCH_HTML)
    picked = pick_candidate(items, "IPZZ937")
    assert picked is not None
    assert "zh-cn" in picked["name"].lower()


def test_pick_candidate_returns_none_when_no_code_match():
    items = parse_search_results(SEARCH_HTML)
    assert pick_candidate(items, "ABCD123") is None


def test_parse_language_links_builds_absolute_urls():
    links = parse_language_links(DETAIL_HTML, "https://subtitlecat.com/subs/1710/x.html")
    assert set(links) == {"zh-CN", "zh-TW", "en"}
    assert links["zh-CN"].startswith("https://subtitlecat.com/subs/1710/")
    assert "%28" in links["zh-CN"] or "(" not in links["zh-CN"]


def test_choose_language_link_prefers_simplified():
    links = parse_language_links(DETAIL_HTML, "https://subtitlecat.com/subs/1710/x.html")
    assert choose_language_link(links, CHINESE_LANG_PREFERENCE).endswith("-zh-CN.srt")


def test_choose_language_link_returns_none_when_absent():
    links = {"en": "https://x/en.srt"}
    assert choose_language_link(links, ("zh-CN", "zh-TW")) is None


def test_parse_srt_skips_blocks_without_timeline():
    cues = parse_srt(SRT_TEXT)
    assert len(cues) == 4
    assert cues[1]["text"] == "和她约会，真是太棒了。"
    assert cues[1]["start"] == pytest.approx(50.96)


def test_validate_srt_flags_empty_and_short():
    assert validate_srt("")["reason"] == "empty_srt"
    assert validate_srt(SRT_TEXT)["reason"] == "too_few_cues"


def test_validate_srt_accepts_when_enough_cues():
    body = "".join(
        f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\n第{i}句\n\n" for i in range(1, 30)
    )
    verdict = validate_srt(body)
    assert verdict["ok"] is True
    assert verdict["cues"] == 29


def test_validate_srt_rejects_timeline_that_stops_early():
    body = "".join(
        f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\n第{i}句\n\n" for i in range(1, 30)
    )
    verdict = validate_srt(body, video_duration_seconds=7200)
    assert verdict["ok"] is False
    assert verdict["reason"] == "timeline_too_short"


def test_subtitle_target_path_uses_jellyfin_convention(tmp_path):
    video = tmp_path / "ABC-123 标题.mp4"
    target = subtitle_target_path(str(video), "zh-CN")
    # zh-CN 不是 Jellyfin 认的后缀，必须收成 zh，否则整条字幕轨不出现。
    assert target.name == "ABC-123 标题.zh.srt"


def test_existing_subtitle_finds_sibling_language_file(tmp_path):
    video = tmp_path / "ABC-123.mp4"
    video.write_bytes(b"")
    assert existing_subtitle(str(video)) is None
    (tmp_path / "ABC-123.zh-CN.srt").write_text("x", encoding="utf-8")
    assert existing_subtitle(str(video)).name == "ABC-123.zh-CN.srt"


def test_existing_subtitle_ignores_unrelated_files(tmp_path):
    video = tmp_path / "ABC-123.mp4"
    video.write_bytes(b"")
    (tmp_path / "ABC-123.nfo").write_text("x", encoding="utf-8")
    (tmp_path / "OTHER-999.zh-CN.srt").write_text("x", encoding="utf-8")
    assert existing_subtitle(str(video)) is None


class _FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("boom")


class _FakeSession:
    """按 URL 关键词返回不同响应，避免依赖网络。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self.requested = []

    def get(self, url, timeout=None):
        self.requested.append(url)
        # 按声明顺序匹配，调用方负责把更具体的标记写在前面
        # （字幕直链的 URL 前缀和详情页相同，只能靠顺序区分）。
        for marker, payload in self.mapping:
            if marker in url:
                return _FakeResponse(payload)
        return _FakeResponse("", status_code=404)

    def close(self):
        pass


def _enough_srt(prefix="第"):
    return "".join(
        f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\n{prefix}{i}句\n\n" for i in range(1, 30)
    )


def test_fetch_subtitle_writes_srt_and_reports_ok(tmp_path):
    video = tmp_path / "IPZZ-937.mp4"
    video.write_bytes(b"")
    session = _FakeSession(
        [
            ("-zh-CN.srt", _enough_srt()),
            ("index.php", SEARCH_HTML),
            ("subs/1710/IPZZ-937.zh-cn", DETAIL_HTML),
        ]
    )
    result = fetch_subtitle("IPZZ937", str(video), session=session, sleep_seconds=0)
    assert result["status"] == "ok"
    assert result["language"] == "zh-CN"
    assert result["source"] == "subtitlecat"
    written = Path(result["subtitle"])
    assert written.exists()
    assert written.name == "IPZZ-937.zh.srt"


def test_fetch_subtitle_skips_when_subtitle_already_present(tmp_path):
    video = tmp_path / "IPZZ-937.mp4"
    video.write_bytes(b"")
    (tmp_path / "IPZZ-937.zh-CN.srt").write_text("x", encoding="utf-8")
    result = fetch_subtitle("IPZZ937", str(video), sleep_seconds=0)
    assert result["status"] == "skipped"
    assert result["reason"] == "subtitle_exists"


def test_fetch_subtitle_reports_not_found_without_touching_disk(tmp_path):
    video = tmp_path / "ABCD-123.mp4"
    video.write_bytes(b"")
    session = _FakeSession([("index.php", SEARCH_HTML)])
    result = fetch_subtitle("ABCD123", str(video), session=session, sleep_seconds=0)
    assert result["status"] == "not_found"
    assert result["reason"] == "no_code_match"
    assert list(tmp_path.glob("*.srt")) == []


def test_fetch_subtitle_marks_invalid_and_keeps_disk_clean(tmp_path):
    video = tmp_path / "IPZZ-937.mp4"
    video.write_bytes(b"")
    session = _FakeSession(
        [
            ("-zh-CN.srt", SRT_TEXT),
            ("index.php", SEARCH_HTML),
            ("subs/1710/IPZZ-937.zh-cn", DETAIL_HTML),
        ]
    )
    result = fetch_subtitle("IPZZ937", str(video), session=session, sleep_seconds=0)
    assert result["status"] == "invalid"
    assert result["reason"] == "too_few_cues"
    assert list(tmp_path.glob("*.srt")) == []


def test_language_from_subtitle_path_reads_two_letter_tag():
    assert language_from_subtitle_path("/hp/A/IPZZ-985/IPZZ-985.zh.srt") == "zh"
    assert language_from_subtitle_path("/hp/A/IPZZ-985/IPZZ-985.zh-CN.srt") == "zh-CN"


def test_language_from_subtitle_path_returns_none_for_release_subtitle():
    """片源自带的 `影片.srt` 没有语言段，不能瞎猜。"""
    assert language_from_subtitle_path("/hp/A/IPZZ-985/IPZZ-985.srt") is None
    assert language_from_subtitle_path("") is None


def test_db_record_marks_existing_subtitle_as_ok():
    """同目录已有字幕 = 有字幕。记成 skipped 会让覆盖率永远低估（KI-060）。"""
    result = {
        "status": "skipped",
        "reason": "subtitle_exists",
        "subtitle": "/hp/A/JUR-733/JUR-733.zh.srt",
    }
    record = db_record_for_result(result)
    assert record == {"status": "ok", "language": "zh", "source": "existing"}


def test_db_record_keeps_skipped_for_missing_video():
    """影片文件不在了要单独记 `path_missing`，不能混进 `skipped`。

    `skipped` 既不算 ok 也不在重试查询里 → 这类记录永远卡中间（KI-062）。
    """
    result = {"status": "path_missing", "reason": "video_missing"}
    record = db_record_for_result(result)
    assert record["status"] == "path_missing"
    assert record["source"] is None


def test_db_record_passes_through_downloaded_result():
    result = {
        "status": "ok",
        "reason": "downloaded",
        "language": "zh-CN",
        "source": "subtitlecat",
        "subtitle": "/hp/A/IPZZ-985/IPZZ-985.zh.srt",
    }
    assert db_record_for_result(result) == {
        "status": "ok",
        "language": "zh-CN",
        "source": "subtitlecat",
    }


class _StatusResponse:
    """只实现 _get 会用到的那几个属性。"""
    def __init__(self, status_code, text="<html>ok</html>"):
        self.status_code = status_code
        self.text = text
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


def test_get_retries_transient_ssl_eof(monkeypatch):
    """subtitlecat 前面的 Cloudflare 会随机断 TLS，同一 URL 有时通有时断。"""
    monkeypatch.setattr("subtitle_fetcher.time.sleep", lambda _seconds: None)
    calls = []

    class _Flaky:
        def get(self, url, timeout=None):
            calls.append(url)
            if len(calls) == 1:
                raise requests.exceptions.SSLError("UNEXPECTED_EOF_WHILE_READING")
            return _StatusResponse(200)

    assert sf._get(_Flaky(), "https://subtitlecat.com/index.php?search=X") == "<html>ok</html>"
    assert len(calls) == 2


def test_get_does_not_retry_when_blocked(monkeypatch):
    """403 是出口 IP 被封，重试只会更糟。"""
    monkeypatch.setattr("subtitle_fetcher.time.sleep", lambda _seconds: None)
    calls = []

    class _Blocked:
        def get(self, url, timeout=None):
            calls.append(url)
            return _StatusResponse(403)

    with pytest.raises(requests.HTTPError):
        sf._get(_Blocked(), "https://subtitlecat.com/x")
    assert len(calls) == 1


def test_get_resets_connections_before_retry(monkeypatch):
    """重试必须换新连接：复用已断开的 keep-alive 会在同一条死连接上反复失败。"""
    monkeypatch.setattr("subtitle_fetcher.time.sleep", lambda _seconds: None)
    resets = []
    monkeypatch.setattr("subtitle_fetcher._reset_connections", lambda session: resets.append(1))
    calls = []

    class _Flaky:
        def get(self, url, timeout=None):
            calls.append(url)
            if len(calls) == 1:
                raise requests.exceptions.SSLError("UNEXPECTED_EOF_WHILE_READING")
            return _StatusResponse(200)

    sf._get(_Flaky(), "https://subtitlecat.com/x")

    assert len(calls) == 2
    assert resets == [1]


def test_jellyfin_language_suffix_collapses_regional_tags():
    assert sf.jellyfin_language_suffix("zh-CN") == "zh"
    assert sf.jellyfin_language_suffix("zh-TW") == "zh"
    assert sf.jellyfin_language_suffix("chi") == "zh"
    assert sf.jellyfin_language_suffix("en") == "en"
    assert sf.jellyfin_language_suffix("ja") == "ja"
