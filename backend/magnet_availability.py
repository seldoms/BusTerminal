"""磁力候选的「可用性」评估 —— 按做种数选种。

## 为什么需要它

2026-11-11 实测（AVOP-069）：JavBus 详情页给出 **20 个候选磁力**，
`select_best_magnet()` 按「分辨率 → 有字幕 → 体积」选中了
`AVOP-069.HD高清`（3.92GB / 标 720P）。但这个种子的 tracker 上**只有 1 个做种**，
qB 拿到它之后永远停在 `metaDL`（拿不到元数据、`num_seeds=0`、`progress=0`），
下载**根本没开始**。

而同一个号里做种最多的候选是 `AVOP-069.mp4`（tracker 上 **12 个做种**）——
体积更小（1.95GB）、画质未知，但**当天就能下完**。

也就是说：**「分辨率/体积」和「能不能下下来」是两件事**，
只看前者会选到根本没人生存的死种。

## 做法与代价控制

用 tracker 的 scrape 接口（`/scrape?info_hash=...`）查候选的 `complete`（做种数）。
scrape 是**只读查询**，比 announce 温和得多，单次请求查一个 hash。

代价控制（接口是同步的，不能让选种变成几十秒的等待）：

- **自适应**：先只查首选那一个。它做种充足就**立即返回** ——
  这是绝大多数情况，一次查询就结束。
- 只有当首选**做种不可靠**时，才去查其余候选。
- 候选之间**并行**查（线程池），单 tracker 超时 4 秒。
- 查不到 = **未知**，不是「0 个种」。这两个语义必须分开，
  否则会把「tracker 查不动」误判成「死种」，进而乱换种子。

## 「有做种」不等于「能下下来」

2026-11-11 生产实测（AVOP-069，20 个候选）：

| 做种数 | 候选 | 实际表现 |
|---|---|---|
| 12 | `AVOP-069.mp4` | ✅ 当天 100% 下完 |
| 7 | `AVOP-069-中文字幕` | ✅ 当天 100% 下完 |
| 5 / 3 | `mkv高清` / `高清` | 可用 |
| **1** | **`AVOP-069.HD高清`** | ❌ **卡在 metaDL，一个字节没下** |
| 0 | 其余 10 个 | 死种 |

「1 个种」同样不可用 —— BT 里 1 个种的种子，做种者一离线就彻底没了；
而且 tracker 的 `complete` 基于 announce 统计，**僵尸 announce 会虚报**，
1 个种的种子往往拿不到 metadata peer。实测那 1 个种的候选正是最初卡死的那个。

所以判定不能是「有没有种」，而要看**做种数够不够**：`MIN_RELIABLE_SEEDS = 3`。
首选做种数 ≥ 3 才算可靠；否则去找做种更多的候选。
"""
from __future__ import annotations

import base64
import binascii
import logging
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# 公共 tracker 的 scrape 端点。scrape 是只读的，比 announce 温和得多。
SCRAPE_ENDPOINTS: Sequence[str] = (
    "http://tracker.opentrackr.org:1337/scrape",
    "http://tracker.dler.org:80/scrape",
    "http://1337.abcvg.info:80/scrape",
)

# 单个 tracker 的超时（秒）。scrape 慢不该拖住整个选种。
SCRAPE_TIMEOUT = 4.0

# 首选确认不可靠后，最多再查多少个候选。
MAX_SCRAPE_CANDIDATES = 16

# 展开查询时的并发度（别把 tracker 打太狠）。
SCRAPE_WORKERS = 8

# 做种数达到这个值才算「可靠」。低于它就该去找别的候选。
# 依据见模块 docstring 的实测表：1 个种的种子照样卡在 metaDL。
MIN_RELIABLE_SEEDS = 3


def extract_info_hash(magnet: Any) -> Optional[str]:
    """从磁力链接里抠出 btih（40 位 hex 或 32 位 base32）。"""
    match = re.search(r"urn:btih:([A-Za-z0-9]{32,40})", str(magnet or ""))
    if not match:
        return None
    return match.group(1)


def info_hash_to_hex(value: Any) -> Optional[str]:
    """把 magnet 或裸 btih 统一成 40 位小写 hex。

    40 位 hex 与 32 位 base32 两种形态都认（qB 返回的 hash 也可能是后者）。
    用于把 ``download_queue.magnet`` 与 qB 的 ``torrents/info`` hash 对齐。
    """
    text = extract_info_hash(value)
    if not text:
        candidate = str(value or "").strip()
        if re.fullmatch(r"[A-Za-z0-9]{32,40}", candidate):
            text = candidate
    raw = _infohash_to_bytes(text) if text else None
    return raw.hex() if raw else None


def _infohash_to_bytes(infohash: str) -> Optional[bytes]:
    """把 btih 文本转成 20 字节二进制。40 位 hex 与 32 位 base32 都支持。"""
    text = str(infohash or "")
    try:
        if len(text) == 40:
            return binascii.unhexlify(text)
        if len(text) == 32:
            return base64.b32decode(text.upper())
    except (binascii.Error, ValueError):
        return None
    return None


def _scrape_one(infohash: str, endpoint: str, opener) -> Optional[int]:
    """向一个 tracker 查做种数；查不到返回 None（未知，不是 0）。"""
    raw = _infohash_to_bytes(infohash)
    if raw is None:
        return None
    query = urllib.parse.urlencode({"info_hash": raw})
    request = urllib.request.Request(
        f"{endpoint}?{query}",
        headers={"User-Agent": "Mozilla/5.0 (compatible; BusTerminal/1.0)"},
    )
    try:
        with opener.open(request, timeout=SCRAPE_TIMEOUT) as response:
            body = response.read().decode("utf-8", "replace")
    except Exception:
        return None
    match = re.search(r'complete"?i(\d+)e', body)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def count_seeds(magnet: Any, opener=None) -> Optional[int]:
    """查一个磁力的做种数。多个 tracker 取**最大值**（任一 tracker 有种就行）。

    返回 None = 查不到（未知），**不等于 0 个种**。
    """
    infohash = extract_info_hash(magnet)
    if not infohash:
        return None
    if opener is None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    results = _scrape_parallel(infohash, opener)
    known = [value for value in results if value is not None]
    return max(known) if known else None


def _scrape_parallel(infohash: str, opener) -> List[Optional[int]]:
    """并行查所有 tracker，返回各 tracker 的做种数（None = 查不到）。"""
    if not opener:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with ThreadPoolExecutor(max_workers=len(SCRAPE_ENDPOINTS)) as pool:
            futures = [
                pool.submit(_scrape_one, infohash, endpoint, opener)
                for endpoint in SCRAPE_ENDPOINTS
            ]
            return [future.result() for future in futures]
    except Exception:
        return [_scrape_one(infohash, endpoint, opener) for endpoint in SCRAPE_ENDPOINTS]


def count_seeds_batch(magnets: List[Dict[str, Any]], *, opener=None) -> Dict[int, Optional[int]]:
    """批量查一批候选的做种数，返回 ``{下标: 做种数}``（None = 查不到）。

    一次调用里把「候选 × tracker」全部并发掉，而不是每个候选各开一轮 ——
    实测 opentrackr / dler 都**拒绝多 hash 的 scrape 请求**（HTTP 400
    Invalid Request），所以只能一个 hash 一个请求，但可以全部并发。
    """
    if not magnets:
        return {}
    if opener is None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    tasks = []
    for index, magnet in enumerate(magnets):
        infohash = extract_info_hash(magnet.get("magnet"))
        if not infohash:
            continue
        for endpoint in SCRAPE_ENDPOINTS:
            tasks.append((index, endpoint, infohash))

    results: Dict[int, List[Optional[int]]] = {index: [] for index in range(len(magnets))}
    if not tasks:
        return {index: None for index in range(len(magnets))}

    def _run(task):
        index, endpoint, infohash = task
        return index, _scrape_one(infohash, endpoint, opener)

    try:
        with ThreadPoolExecutor(max_workers=SCRAPE_WORKERS) as pool:
            for index, value in pool.map(_run, tasks):
                results[index].append(value)
    except Exception:
        logger.warning("批量查做种数异常，退回逐个查询", exc_info=True)

    counts: Dict[int, Optional[int]] = {}
    for index in range(len(magnets)):
        known = [value for value in results[index] if value is not None]
        counts[index] = max(known) if known else None
    return counts


def annotate_seed_counts(magnets: List[Dict[str, Any]], *, opener=None) -> List[Dict[str, Any]]:
    """就地给候选补 ``seed_count`` 字段（查不到就是 None）。

    一次批量查完所有候选（并发），不给 tracker 添麻烦也不拖慢接口。
    """
    if not magnets:
        return magnets
    counts = count_seeds_batch(magnets, opener=opener)
    for index, magnet in enumerate(magnets):
        magnet["seed_count"] = counts.get(index)
    return magnets


def pick_alive_candidate(
    magnets: List[Dict[str, Any]],
    preferred: Optional[Dict[str, Any]],
    *,
    opener=None,
) -> Optional[Dict[str, Any]]:
    """在 ``preferred``（按画质/体积选出的那个）之外，挑一个**确实能下下来**的。

    判定口径（依据见模块 docstring 的生产实测表）：

    - 首选做种数 **≥ MIN_RELIABLE_SEEDS(3)** → 直接用它，不折腾。
    - 首选做种数 **查不到**（None）→ 也直接用它（不做「无数据即否定」的强推断）。
    - 首选做种数 **< 3**（含 0，但**1 个种同样不可靠**）→ 批量查其余候选，
      换成做种最多的那个；**做种数严格大于首选**才换，否则保持首选。

    注意「1 个种也要换」：2026-11-11 实测，最初卡死在 metaDL 的那个候选
    正是 **1 个种**（不是 0）。BT 里 1 个种的种子，做种者一离线就彻底没了，
    而且 tracker 的 `complete` 基于 announce 统计、僵尸 announce 会虚报。
    """
    if not magnets or not preferred:
        return preferred
    if opener is None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    preferred_seeds = preferred.get("seed_count")
    if preferred_seeds is None:
        preferred_seeds = count_seeds(preferred.get("magnet"), opener=opener)
        preferred["seed_count"] = preferred_seeds
    if preferred_seeds is None or preferred_seeds >= MIN_RELIABLE_SEEDS:
        return preferred

    # 首选做种不够 → 查其余候选（一次批量并发查完）
    others = [m for m in magnets if m is not preferred]
    others = others[:MAX_SCRAPE_CANDIDATES]
    if not others:
        return preferred

    counts = count_seeds_batch(others, opener=opener)
    for index, magnet in enumerate(others):
        magnet["seed_count"] = counts.get(index)

    best = None
    best_seeds = preferred_seeds
    for index, magnet in enumerate(others):
        count = counts.get(index)
        if count is None:
            continue
        if count > best_seeds:
            best_seeds = count
            best = magnet
    if best is None:
        return preferred

    logger.info(
        "选种：首选 %s 只有 %s 个做种，改用 %s（做种 %s）",
        str(preferred.get("title") or "")[:40],
        preferred_seeds,
        str(best.get("title") or "")[:40],
        best_seeds,
    )
    return best
