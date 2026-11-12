"""外挂字幕抓取（以 Subtitle Cat 为主力源）。

为什么是"抓站"而不是"自建 ASR"：
    2026-11-03 实测，139 的 16G 显存被翻译用的两个 ollama 模型占掉 12G，
    faster-whisper large-v3 即使 int8 也加载不上（CUDA OOM）；NAS 只有 N6005
    四核核显，CPU 跑 large-v3 的 RTF 是小时级。而按番号抓现成字幕是毫秒级、
    零算力，且命中率已实测：17 个已入库番号命中 12 个（70%），其中 7 个有中文。

职责边界：
    - 只做「按番号搜 → 下 SRT → 校验 → 落盘到影片同目录」，不改动媒体文件。
    - 落盘命名遵循 Jellyfin 外挂字幕规范 `<视频名>.<语言>.srt`，Jellyfin 扫库即识别。
    - 下载失败、命中不到、校验不过一律返回结构化结果，不抛异常。
"""

from __future__ import annotations

import random
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import requests

SUBTITLECAT_BASE = "https://subtitlecat.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 20
RETRY_ATTEMPTS = 3
TRANSIENT_STATUS_CODES = frozenset({429, 502, 503, 504})
# 站点是个人维护的免费站，请求间隔刻意留大，避免把对方打挂也避免被 Cloudflare 拉黑。
REQUEST_INTERVAL_SECONDS = 1.2

# 语言优先级：简体 > 繁体 > 任意中文标记。日文原版只在明确允许时取。
CHINESE_LANG_PREFERENCE = ("zh-CN", "zh-cn", "chs", "zh", "zh-TW", "zh-tw", "cht")
JAPANESE_LANG_PREFERENCE = ("ja", "jp")

SUBTITLE_SUFFIXES = (".srt", ".ass", ".ssa", ".sub", ".vtt")

_SEARCH_ITEM_RE = re.compile(r'href="subs/(\d+)/([^"]+)\.html"')
_SRT_LINK_RE = re.compile(r'href="([^"]+\.srt)"')
# 语言标记在文件名末尾，形如 `-zh-CN.srt` / `-en.srt`。
# 不能按最后一个连字符切：`zh-CN` 自带连字符，切完只剩 `CN`。
_SRT_LANG_RE = re.compile(r"-([A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?)\.srt$", re.IGNORECASE)
_SRT_TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)
_PLAIN_CODE_RE = re.compile(r"^(\d*[A-Za-z]+)(\d+)$")


def code_to_search_term(code_norm: str) -> str:
    """把库内的归一化番号还原成站点惯用的带连字符写法。

    `code_norm` 是去掉分隔符的小写/大写混合串（SNOS392），站点搜索用 SNOS-392。
    已经是带连字符的原样返回，拆不出字母+数字两段也原样返回。
    """
    text = str(code_norm or "").strip()
    if not text:
        return ""
    if "-" in text:
        return text.upper()
    matched = _PLAIN_CODE_RE.match(text)
    if not matched:
        return text.upper()
    return f"{matched.group(1)}-{matched.group(2)}".upper()


def _strip_separators(value: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", str(value or "").lower())


def name_matches_code(name: str, code_norm: str) -> bool:
    """结果文件名是否确实属于这部片。

    站点搜索是模糊匹配，会带出大量无关片；必须用番号做二次校验，
    否则会把别的片的字幕挂到自己片子上，比没有字幕更糟。
    """
    key = _strip_separators(code_norm)
    if not key:
        return False
    return key in _strip_separators(name)


def parse_search_results(html_text: str) -> List[Dict[str, str]]:
    """解析搜索结果表格里的条目（链接是相对路径，无前导斜杠）。"""
    items: List[Dict[str, str]] = []
    seen = set()
    for sub_id, raw_name in _SEARCH_ITEM_RE.findall(html_text or ""):
        name = urllib.parse.unquote(raw_name)
        if name in seen:
            continue
        seen.add(name)
        items.append(
            {
                "id": sub_id,
                "name": name,
                "url": f"{SUBTITLECAT_BASE}/subs/{sub_id}/{raw_name}.html",
            }
        )
    return items


def pick_candidate(
    items: Sequence[Dict[str, str]],
    code_norm: str,
    prefer_languages: Sequence[str] = CHINESE_LANG_PREFERENCE,
) -> Optional[Dict[str, str]]:
    """在结果里挑最合适的一条：番号必须对得上，语言按优先级。"""
    matched = [item for item in items if name_matches_code(item.get("name", ""), code_norm)]
    if not matched:
        return None
    for lang in prefer_languages:
        for item in matched:
            if lang.lower() in item["name"].lower():
                return item
    # 没有明确语言标记时，取站点排序最前的一条（站点按相关度/下载量排）。
    return matched[0]


def parse_language_links(detail_html: str, detail_url: str) -> Dict[str, str]:
    """详情页会列出该字幕的所有语言转换版，返回 {语言标记: 绝对直链}。

    文件名形如 `IPZZ-937.zh-cn(by transub)-zh-CN.srt`，后缀即语言。
    """
    links: Dict[str, str] = {}
    for raw in _SRT_LINK_RE.findall(detail_html or ""):
        matched = _SRT_LANG_RE.search(raw)
        if not matched:
            continue
        lang = matched.group(1)
        absolute = urllib.parse.urljoin(detail_url, urllib.parse.quote(raw))
        links.setdefault(lang, absolute)
    return links


def choose_language_link(links: Dict[str, str], prefer_languages: Sequence[str]) -> Optional[str]:
    for want in prefer_languages:
        for lang, url in links.items():
            if lang.lower() == want.lower():
                return url
    for want in prefer_languages:
        for lang, url in links.items():
            if want.lower() in lang.lower():
                return url
    return None


def parse_srt(text: str) -> List[Dict[str, Any]]:
    """把 SRT 拆成条目，解析不出时间的段落直接丢弃。"""
    cues: List[Dict[str, Any]] = []
    blocks = re.split(r"\r?\n\r?\n+", (text or "").replace("\ufeff", "").strip())
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        time_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue
        matched = _SRT_TIME_RE.search(lines[time_index])
        if not matched:
            continue
        start = (
            int(matched.group(1)) * 3600
            + int(matched.group(2)) * 60
            + int(matched.group(3))
            + int(matched.group(4)) / 1000
        )
        end = (
            int(matched.group(5)) * 3600
            + int(matched.group(6)) * 60
            + int(matched.group(7))
            + int(matched.group(8)) / 1000
        )
        body = " ".join(lines[time_index + 1 :]).strip()
        if not body:
            continue
        cues.append({"start": round(start, 3), "end": round(end, 3), "text": body})
    return cues


def validate_srt(
    text: str,
    *,
    min_cues: int = 20,
    video_duration_seconds: Optional[float] = None,
    max_duration_drift_ratio: float = 0.25,
) -> Dict[str, Any]:
    """字幕能不能用。

    除了「有内容」，还拿末条时间轴和视频时长比一把：抓错版本最常见的结果不是
    空文件，而是时间轴整体偏移或只覆盖前半段，光看条数发现不了。
    """
    cues = parse_srt(text)
    if not cues:
        return {"ok": False, "reason": "empty_srt", "cues": 0, "last_end": 0.0}
    if len(cues) < min_cues:
        return {"ok": False, "reason": "too_few_cues", "cues": len(cues), "last_end": 0.0}
    last_end = max(cue["end"] for cue in cues)
    if last_end <= 0:
        return {"ok": False, "reason": "invalid_timeline", "cues": len(cues), "last_end": last_end}
    if video_duration_seconds and video_duration_seconds > 0:
        drift = abs(last_end - video_duration_seconds) / video_duration_seconds
        # 只拦「明显不够长」和「明显超长」，片尾留白属于正常现象，不按 25% 卡死。
        if last_end < video_duration_seconds * (1 - max_duration_drift_ratio):
            return {
                "ok": False,
                "reason": "timeline_too_short",
                "cues": len(cues),
                "last_end": last_end,
                "drift": round(drift, 3),
            }
    return {"ok": True, "reason": "ok", "cues": len(cues), "last_end": last_end}


# Jellyfin 只认 ISO 639-1/639-2 的语言后缀，`zh-CN` 这种带地区的标签会被
# 当成"未知"甚至整条忽略（2026-11-03 生产实测：`.zh-CN.srt` 不出字幕轨，
# 改名 `.zh.srt` 立刻识别成外挂 SUBRIP）。所以统一收成两字母。
JELLYFIN_LANG_SUFFIX = {
    "zh-cn": "zh",
    "zh-tw": "zh",
    "zh-hans": "zh",
    "zh-hant": "zh",
    "zh": "zh",
    "chi": "zh",
    "zho": "zh",
    "ja": "ja",
    "jpn": "ja",
    "en": "en",
    "eng": "en",
}


def jellyfin_language_suffix(language: str) -> str:
    key = str(language or "").strip().lower()
    return JELLYFIN_LANG_SUFFIX.get(key, key or "und")


def subtitle_target_path(video_path: str, language: str) -> Path:
    """Jellyfin 认的外挂字幕名：`<视频同名>.<语言>.srt`。"""
    video = Path(video_path)
    suffix = jellyfin_language_suffix(language)
    return video.with_name(f"{video.stem}.{suffix}.srt")


def existing_subtitle(video_path: str) -> Optional[Path]:
    """同目录里已有任何字幕就不再抓，避免覆盖人工字幕。"""
    video = Path(video_path)
    if not video.parent.exists():
        return None
    for candidate in video.parent.iterdir():
        if candidate.suffix.lower() not in SUBTITLE_SUFFIXES:
            continue
        if candidate.stem == video.stem or candidate.stem.startswith(f"{video.stem}."):
            return candidate
    return None


def language_from_subtitle_path(subtitle_path: str) -> Optional[str]:
    """从 `<视频名>.<语言>.srt` 里读出语言标记，读不到返回 None。

    片源自带的字幕通常没有语言段（`影片.srt`），读不出来就留空；我们自己下的
    一定带（`影片.zh.srt`），读出来才能把「已有字幕」记成哪国语言。
    """
    stem = Path(str(subtitle_path or "")).stem
    if "." not in stem:
        return None
    tag = stem.rsplit(".", 1)[-1]
    return tag if re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z]{2,4})?", tag) else None


def db_record_for_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """抓取结果 → `local_files` 里该记的 (status, language, source)。

    `skipped` / `subtitle_exists` 意思是「影片同目录已经有字幕了」（片源自带，
    或者更早下载的）。它是**有字幕**，不是没字幕 —— 记成 skipped 会让覆盖率
    统计一直把这批片算进「还缺字幕」，2026-11-03 实测 138 部里就有 6 部是这种。
    """
    status = result.get("status") or "error"
    language = result.get("language")
    source = result.get("source")
    if result.get("reason") == "subtitle_exists":
        status = "ok"
        source = source or "existing"
        language = language or language_from_subtitle_path(result.get("subtitle") or "")
    return {"status": status, "language": language, "source": source}


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    )
    return session


def _reset_connections(session: requests.Session) -> None:
    """丢掉已断开的 keep-alive 连接。

    SSLEOFError 多半是复用了服务端已经关掉的连接：不换连接的话，重试多少次
    都在同一条死连接上撞。清连接池保留 cookie，比新建 session 温和。
    """
    for adapter in list(getattr(session, "adapters", {}).values()):
        pool = getattr(adapter, "poolmanager", None)
        try:
            if pool is not None:
                pool.clear()
            else:
                adapter.close()
        except Exception:
            pass


def _get(session: requests.Session, url: str, *, timeout: int = DEFAULT_TIMEOUT) -> str:
    """GET 并返回文本，连接层错误重试。

    subtitlecat 前面是 Cloudflare，实测会随机抛 SSLEOFError
    （`UNEXPECTED_EOF_WHILE_READING`，同一个 URL 有时候通、有时候断），
    而 403 是出口 IP 被封，重试没有意义 —— 所以只重试连接层与 429/5xx。
    """
    attempt = 0
    while True:
        try:
            response = session.get(url, timeout=timeout)
            if response.status_code in TRANSIENT_STATUS_CODES and attempt < RETRY_ATTEMPTS:
                raise requests.HTTPError(
                    f"transient {response.status_code}", response=response
                )
            response.raise_for_status()
            if not response.encoding or response.encoding.lower() == "iso-8859-1":
                response.encoding = response.apparent_encoding or "utf-8"
            return response.text
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and status not in TRANSIENT_STATUS_CODES:
                raise
            if attempt >= RETRY_ATTEMPTS:
                raise
            attempt += 1
            _reset_connections(session)
            time.sleep(1.5 * attempt + random.uniform(0, 1.0))


def fetch_subtitle(
    code_norm: str,
    video_path: str,
    *,
    prefer_languages: Sequence[str] = CHINESE_LANG_PREFERENCE,
    video_duration_seconds: Optional[float] = None,
    overwrite: bool = False,
    session: Optional[requests.Session] = None,
    sleep_seconds: float = REQUEST_INTERVAL_SECONDS,
) -> Dict[str, Any]:
    """主入口：给番号和视频路径，尽量把一条能用的外挂字幕落到盘上。

    返回结构固定，便于 API 与后台批量共用：
        {"status": ok|skipped|not_found|invalid|error, ...}
    """
    video = Path(video_path)
    result: Dict[str, Any] = {
        "code": code_norm,
        "video": str(video),
        "status": "error",
        "reason": "",
    }

    if not video.exists():
        # 单独记 `path_missing`：`skipped` 的语义是「跳过、但这条没问题」，会被
        # 覆盖率统计和重试查询同时放过，于是这类记录永远卡在中间（2026-11-04 实测
        # 库里有 6 条指向已被清走的 /hp/_incoming 路径）。给它独立状态才能被识别、
        # 计数、清理，而不是假装没事。
        result.update(status="path_missing", reason="video_missing")
        return result

    if not overwrite:
        existing = existing_subtitle(str(video))
        if existing is not None:
            result.update(status="skipped", reason="subtitle_exists", subtitle=str(existing))
            return result

    query = code_to_search_term(code_norm)
    if not query:
        result.update(status="not_found", reason="empty_code")
        return result
    result["query"] = query

    owns_session = session is None
    session = session or build_session()
    try:
        search_url = f"{SUBTITLECAT_BASE}/index.php?search={urllib.parse.quote(query)}"
        search_html = _get(session, search_url)
        if sleep_seconds:
            time.sleep(sleep_seconds)

        items = parse_search_results(search_html)
        result["candidates"] = len(items)
        picked = pick_candidate(items, code_norm, prefer_languages)
        if picked is None:
            result.update(status="not_found", reason="no_code_match")
            return result
        result["matched"] = picked["name"]

        detail_html = _get(session, picked["url"])
        if sleep_seconds:
            time.sleep(sleep_seconds)

        links = parse_language_links(detail_html, picked["url"])
        result["languages"] = sorted(links.keys())[:12]
        target_url = choose_language_link(links, prefer_languages)
        if target_url is None:
            result.update(status="not_found", reason="no_preferred_language")
            return result
        language = next(
            (lang for lang, url in links.items() if url == target_url), prefer_languages[0]
        )
        result["language"] = language

        subtitle_text = _get(session, target_url)
        verdict = validate_srt(
            subtitle_text,
            video_duration_seconds=video_duration_seconds,
        )
        result.update(cues=verdict.get("cues"), last_end=verdict.get("last_end"))
        if not verdict["ok"]:
            result.update(status="invalid", reason=verdict["reason"])
            return result

        destination = subtitle_target_path(str(video), language)
        # 按字节写，避免 Windows 上 write_text 把行尾换成 CRLF。
        destination.write_bytes(subtitle_text.encode("utf-8"))
        result.update(
            status="ok",
            reason="downloaded",
            subtitle=str(destination),
            source="subtitlecat",
            bytes=len(subtitle_text.encode("utf-8")),
        )
        return result
    except requests.RequestException as exc:
        result.update(status="error", reason=f"http_error:{type(exc).__name__}")
        return result
    except OSError as exc:
        result.update(status="error", reason=f"io_error:{type(exc).__name__}")
        return result
    finally:
        if owns_session:
            session.close()
