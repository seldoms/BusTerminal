"""gfriends 演员头像同步。

数据源: https://github.com/gfriends/gfriends —— 公开的演员头像库, 索引文件
``Filetree.json`` 给出 ``(文件名 -> Content/{folder}/{file})`` 映射 (约 3.4 万个名字)。

本模块只做一件事: 把 gfriends 的头像 URL 按演员名回填到 ``actresses.avatar``。
**不下载图片** —— 图片继续由 ``/api/proxy/image`` 按需抓取并本地缓存, 避免十万张图爆盘。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

import config
from network_security_utils import build_system_proxy_config

logger = logging.getLogger(__name__)

GFRIENDS_REPO = "https://github.com/gfriends/gfriends"
GFRIENDS_RAW_BASE = "https://raw.githubusercontent.com/gfriends/gfriends/master"
GFRIENDS_INDEX_URL = f"{GFRIENDS_RAW_BASE}/Filetree.json"

INDEX_CACHE_FILENAME = "gfriends_index.json"
INDEX_TTL_SECONDS = 7 * 24 * 3600
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG")

_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
_SPACE_RE = re.compile(r"\s+")
_ALIAS_SPLIT_RE = re.compile(r"[,，/|;；]")

_SYNC_LOCK = threading.Lock()


def normalize_actress_name(name: object) -> str:
    """去掉括号备注（如 ``森沢かな（飯岡かなこ）`` → ``森沢かな``）与所有空白。"""
    if not name:
        return ""
    return _SPACE_RE.sub("", _PAREN_RE.sub("", str(name))).strip()


def build_index(filetree: Dict) -> Dict[str, str]:
    """Filetree.json → ``{规范化演员名: 头像 URL}``。

    同名保留文件夹字母序靠前者（与 amane 同一取舍: 倾向非 ``z-*`` 低质源）。
    """
    content = (filetree or {}).get("Content")
    if not isinstance(content, dict):
        return {}
    index: Dict[str, str] = {}
    for folder in sorted(content.keys()):
        files = content[folder]
        if not isinstance(files, dict):
            continue
        encoded_folder = quote(str(folder), safe="")
        for filename, target in files.items():
            base = str(filename)
            for ext in IMAGE_EXTENSIONS:
                if base.endswith(ext):
                    base = base[: -len(ext)]
                    break
            key = normalize_actress_name(base)
            if not key or key in index:
                continue
            rel = str(target).split("?", 1)[0] if target else str(filename)
            index[key] = f"{GFRIENDS_RAW_BASE}/Content/{encoded_folder}/{quote(rel, safe='')}"
    return index


class GfriendsAvatarSyncer:
    """拉取 gfriends 索引, 给缺头像的演员回填 avatar URL。"""

    def __init__(
        self,
        db,
        data_dir: Optional[str] = None,
        *,
        fetch_index: Optional[Callable[[], Dict]] = None,
    ):
        self.db = db
        self.data_dir = data_dir or config.DATA_DIR
        self._fetch_index = fetch_index
        self._index: Optional[Dict[str, str]] = None

    @property
    def cache_path(self) -> str:
        return os.path.join(self.data_dir, INDEX_CACHE_FILENAME)

    def _download_index(self) -> Dict:
        proxy = build_system_proxy_config(self.db.get_config("system_proxy") or config.SYSTEM_PROXY)
        response = requests.get(
            GFRIENDS_INDEX_URL,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            proxies=proxy,
            timeout=(5.0, 60.0),
        )
        response.raise_for_status()
        return response.json()

    def load_index(self, force: bool = False) -> Dict[str, str]:
        """优先读本地缓存（TTL 内）；否则重新拉取并原子写缓存。"""
        if self._index is not None and not force:
            return self._index

        path = self.cache_path
        if not force and os.path.exists(path):
            try:
                if time.time() - os.path.getmtime(path) < INDEX_TTL_SECONDS:
                    with open(path, "r", encoding="utf-8") as handle:
                        cached = json.load(handle)
                    if isinstance(cached, dict) and cached:
                        self._index = cached
                        return cached
            except (OSError, ValueError):
                logger.warning("gfriends 索引缓存不可读，将重新拉取")

        raw = self._fetch_index() if self._fetch_index else self._download_index()
        index = build_index(raw)
        if index:
            try:
                os.makedirs(self.data_dir, exist_ok=True)
                tmp_path = f"{path}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as handle:
                    json.dump(index, handle, ensure_ascii=False)
                os.replace(tmp_path, path)
            except OSError:
                logger.warning("gfriends 索引缓存写入失败: %s", path)
        self._index = index
        return index

    def sync(
        self,
        *,
        dry_run: bool = False,
        limit: Optional[int] = None,
        randomize: bool = False,
    ) -> Dict[str, object]:
        """扫描缺头像的演员并回填。``dry_run`` 只统计不落库。

        ``limit`` 是**扫描**上限而非写入上限；实测 gfriends 索引对候选的命中率只有
        2~15%，所以调用方通常要传一个远大于期望写入量的 limit。
        ``randomize=True`` 打散候选顺序，避开「永远只扫存储顺序前 N 行」的饥饿。
        """
        with _SYNC_LOCK:
            index = self.load_index()
            candidates = self.db.get_actresses_missing_avatar(
                limit=limit, randomize=randomize
            )

            updates: List[Tuple[str, str]] = []
            samples: List[Dict[str, str]] = []
            for row in candidates:
                url = None
                matched_name = ""
                for name in self._candidate_names(row):
                    url = index.get(normalize_actress_name(name))
                    if url:
                        matched_name = name
                        break
                if not url:
                    continue
                updates.append((row["id"], url))
                if len(samples) < 5:
                    samples.append({"id": row["id"], "name": row.get("name") or matched_name, "avatar": url})

            applied = 0
            if updates and not dry_run:
                applied = self.db.update_actress_avatars(updates)

            return {
                "index_size": len(index),
                "candidates": len(candidates),
                "matched": len(updates),
                "applied": applied,
                "dry_run": dry_run,
                "samples": samples,
            }

    @staticmethod
    def _candidate_names(row: Dict) -> List[str]:
        """候选名 = 主名 + 逗号分隔的别名。"""
        names = [row.get("name") or ""]
        aliases = row.get("alias_names") or ""
        if isinstance(aliases, str):
            names.extend(part for part in _ALIAS_SPLIT_RE.split(aliases) if part.strip())
        elif isinstance(aliases, (list, tuple)):
            names.extend(str(part) for part in aliases)
        return [name for name in names if name]
