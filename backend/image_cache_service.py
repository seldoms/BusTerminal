import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import tempfile
from urllib.parse import urljoin
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

import config
from database import db, parse_int_value
from network_security_utils import (
    build_system_proxy_config,
    is_blocked_remote_ip,
    is_fake_proxy_ip_address,
)
from organizer import is_safe_remote_image_url

try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover - Pillow may be absent in some local envs
    Image = None
    ImageOps = None


logger = logging.getLogger(__name__)

SAFE_REMOTE_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}
SAFE_REMOTE_IMAGE_EXTENSIONS = set(SAFE_REMOTE_IMAGE_TYPES.values())
CACHE_CLEANUP_IMAGE_SUFFIXES = SAFE_REMOTE_IMAGE_EXTENSIONS | {".jpg", ".jpeg"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
FAILURE_TTL_SECONDS = config.IMAGE_FAILURE_TTL_SECONDS
DEFAULT_THUMBNAIL_QUALITY = 82
IMAGE_CACHE_WARMUP_LIMIT = 80
# 需要随预热一起生成的缩略图宽度：360 供列表卡片、720 供影片详情页封面。
# 详情页封面就是 720，如果只预热原图，首次打开详情页仍要走一次上游大图请求。
IMAGE_CACHE_WARMUP_THUMBNAIL_WIDTHS = (360, 720)
IMAGE_CACHE_STATS_TTL_SECONDS = 300
IMAGE_CACHE_STATS_KEY = "image_cache_stats_snapshot_v1"

# 已实测下架 / 连不上的图床域名。库里有历史遗留的封面指向它们
# （seejav.cyou 122 条，javbus.cyou / seedmm.cyou 等合计约 800 条，占 0.05%）。
# 这些域名要么 DNS 不解析、要么直接丢弃连接，逐个去试要花掉整个连接+读取超时，
# 用户体感就是「图片一直转圈」。这里直接快速失败：返回占位图，
# 并照样写失败标记，短期不会反复重试。
# ⚠️ 只是加速失败，不改变数据 —— 真要彻底清掉需另行清理 movies.cover。
DEAD_IMAGE_HOSTS = frozenset({
    "www.seejav.cyou",
    "seejav.cyou",
    "www.javbus.cyou",
    "javbus.cyou",
    "www.seedmm.cyou",
    "seedmm.cyou",
    "www.cdnbus.cyou",
    "cdnbus.cyou",
    "n1.1024cdn.sx",
    "1024cdn.sx",
})


def is_dead_image_host(image_url: str) -> bool:
    """判断是否已确认失效的图床域名。"""
    try:
        host = (urlparse(image_url).hostname or "").lower()
    except Exception:
        return False
    return host in DEAD_IMAGE_HOSTS


@dataclass
class CachedImage:
    path: Optional[str] = None
    content: Optional[bytes] = None
    media_type: str = "image/jpeg"
    cache_status: str = "MISS"
    source_path: Optional[str] = None


class ImageCacheService:
    def __init__(self, cache_root: str):
        self.cache_root = Path(cache_root)
        self.proxy_dir = self.cache_root / "proxy"
        self.failure_dir = self.cache_root / "proxy_failures"
        self.recent_events = deque(maxlen=1000)
        self._fetch_locks: Dict[str, threading.Lock] = {}
        self._fetch_lock_refs: Dict[str, int] = {}
        self._fetch_locks_guard = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stats_cache: Optional[Dict[str, Any]] = None
        self._stats_cache_at = 0.0
        self._stats_refreshing = False
        root_digest = hashlib.sha256(str(self.cache_root.resolve()).encode("utf-8")).hexdigest()[:16]
        self._stats_cache_key = f"{IMAGE_CACHE_STATS_KEY}_{root_digest}"
        try:
            snapshot = db.get_cache(self._stats_cache_key)
            if isinstance(snapshot, dict) and "total_size_bytes" in snapshot:
                self._stats_cache = snapshot
                self._stats_cache_at = time.time()
        except Exception:
            logger.warning("Image cache stats snapshot could not be loaded")

    def normalize_url(self, image_url: Any) -> Optional[str]:
        value = str(image_url or "").strip()
        if not value:
            return None
        if value.startswith("//"):
            value = f"https:{value}"

        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None

        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").rstrip(".").lower()
        if not host:
            return None
        netloc = host
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"

        query = parsed.query
        if query:
            try:
                query = urlencode(sorted(parse_qsl(query, keep_blank_values=True)), doseq=True)
            except Exception:
                query = parsed.query

        return urlunparse((scheme, netloc, parsed.path or "/", "", query, ""))

    def rejection_reason(self, image_url: str) -> Optional[str]:
        parsed = urlparse(image_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return "Only http/https image URLs are supported"

        host = parsed.hostname or ""
        normalized_host = host.rstrip(".").lower()
        if normalized_host in {"localhost", "127.0.0.1", "::1"} or normalized_host.endswith(".localhost"):
            return "Local image proxy targets are not allowed"

        if not is_safe_remote_image_url(image_url):
            return "Private image proxy targets are not allowed"

        # 若 host 本身为显式 IP 地址，前面的统一校验已经覆盖；这里保留分支只是为了清晰。
        try:
            ip = ipaddress.ip_address(normalized_host)
            if is_blocked_remote_ip(str(ip)):
                return "Private image proxy targets are not allowed"
        except ValueError:
            pass

        return None

    def get_cache_keys(self, image_url: Any) -> List[str]:
        normalized = self.normalize_url(image_url)
        candidates = [normalized, str(image_url or "").strip()]
        keys = []
        for candidate in candidates:
            if not candidate:
                continue
            for digest in (
                hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
                hashlib.md5(candidate.encode("utf-8")).hexdigest(),
            ):
                if digest not in keys:
                    keys.append(digest)
        return keys

    def find_cached_original(self, image_url: Any) -> Optional[Path]:
        # 缓存文件名固定为 {sha256/md5}{已知图片后缀}，直接按路径探测，
        # 避免 glob 全目录扫描（大缓存目录 + 高并发下每个请求都要扫好几遍）。
        cache_roots = [self.proxy_dir, self.cache_root]
        for cache_root in cache_roots:
            if not cache_root.exists():
                continue
            for cache_key in self.get_cache_keys(image_url):
                for suffix in SAFE_REMOTE_IMAGE_EXTENSIONS:
                    item = cache_root / f"{cache_key}{suffix}"
                    if self.is_safe_cache_file(item, cache_root, SAFE_REMOTE_IMAGE_EXTENSIONS):
                        return item
        return None

    def get_proxy_image(self, raw_url: Any, width: Optional[int] = None) -> Optional[CachedImage]:
        image_url = self.normalize_url(raw_url)
        if not image_url or self.rejection_reason(image_url):
            return None

        if self.is_failure_fresh(image_url):
            self._record_event("failure")
            return None

        width = self.normalize_width(width)
        cached_original = self.find_cached_original(image_url)
        if cached_original:
            os.utime(cached_original, None)
            self._record_event("hit")
            if width:
                thumbnail = self.get_or_create_thumbnail(cached_original, image_url, width)
                if thumbnail:
                    return CachedImage(
                        path=str(thumbnail),
                        media_type="image/jpeg",
                        cache_status="HIT",
                        source_path=str(cached_original),
                    )
            return CachedImage(
                path=str(cached_original),
                media_type=mimetypes.guess_type(str(cached_original))[0] or "image/jpeg",
                cache_status="HIT",
            )

        if self.is_failure_fresh(image_url):
            self._record_event("failure")
            return None

        fetched = self.fetch_and_cache(image_url)
        if not fetched:
            self.mark_failure(image_url, "fetch_failed")
            self._record_event("failure")
            return None

        if width and fetched.path:
            thumbnail = self.get_or_create_thumbnail(Path(fetched.path), image_url, width)
            if thumbnail:
                fetched = CachedImage(
                    path=str(thumbnail),
                    media_type="image/jpeg",
                    cache_status="MISS",
                    source_path=fetched.path,
                )
        self._record_event("hit" if fetched.cache_status == "HIT" else "miss")
        return fetched

    def cache_remote_url(self, raw_url: Any) -> Optional[str]:
        image_url = self.normalize_url(raw_url)
        if not image_url or self.rejection_reason(image_url):
            return None

        cached_file = self.find_cached_original(image_url)
        if cached_file:
            os.utime(cached_file, None)
            self._record_event("hit")
            return str(cached_file)

        if self.is_failure_fresh(image_url):
            self._record_event("failure")
            return None

        fetched = self.fetch_and_cache(image_url)
        if fetched and fetched.path:
            self._record_event("hit" if fetched.cache_status == "HIT" else "miss")
            return fetched.path

        self.mark_failure(image_url, "fetch_failed")
        self._record_event("failure")
        return None

    def fetch_and_cache(self, image_url: str) -> Optional[CachedImage]:
        cache_key = hashlib.sha256(image_url.encode("utf-8")).hexdigest()
        with self._fetch_locks_guard:
            fetch_lock = self._fetch_locks.setdefault(cache_key, threading.Lock())
            self._fetch_lock_refs[cache_key] = self._fetch_lock_refs.get(cache_key, 0) + 1

        try:
            with fetch_lock:
                cached_file = self.find_cached_original(image_url)
                if cached_file:
                    os.utime(cached_file, None)
                    return CachedImage(
                        path=str(cached_file),
                        media_type=mimetypes.guess_type(str(cached_file))[0] or "image/jpeg",
                        cache_status="HIT",
                    )
                if self.is_failure_fresh(image_url):
                    return None
                return self._fetch_and_cache_unlocked(image_url, cache_key)
        finally:
            with self._fetch_locks_guard:
                remaining = self._fetch_lock_refs.get(cache_key, 1) - 1
                if remaining <= 0:
                    self._fetch_lock_refs.pop(cache_key, None)
                    self._fetch_locks.pop(cache_key, None)
                else:
                    self._fetch_lock_refs[cache_key] = remaining

    def _fetch_and_cache_unlocked(self, image_url: str, cache_key: str) -> Optional[CachedImage]:
        self.proxy_dir.mkdir(parents=True, exist_ok=True)

        # 已确认失效的图床域名：不去试连接，直接放弃。
        # 否则每张这样的封面都要先耗掉整个超时（实测 seejav.cyou 直连 16 秒无响应）。
        if is_dead_image_host(image_url):
            self.mark_failure(image_url, "dead_host")
            self._record_event("failure")
            return None

        current_url = image_url
        proxy_config = self.get_system_proxy_config()
        for _redirect_depth in range(5):
            if self.rejection_reason(current_url):
                return None

            response = None
            try:
                parsed_url = urlparse(current_url)
                origin = f"{parsed_url.scheme}://{parsed_url.netloc}/"
                response = requests.get(
                    current_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                        "Referer": origin,
                    },
                    allow_redirects=False,
                    proxies=proxy_config,
                    stream=True,
                    # ⚠️ 连接 4s / 读取 6s。早期是 (5.0, 20.0)，读取给到 20 秒：
                    # 库里存着一批**已下架域名**的封面（seejav.cyou 122 条、
                    # javbus.cyou/seedmm.cyou 等合计约 800 条，实测 seejav.cyou
                    # 直连 16 秒不返回），第一次访问每张要干等十几秒，
                    # 用户体感就是「图片加载慢」。图片本来就该是秒回的，
                    # 慢的源站等它也没意义 —— 失败会被标记，短时间不再重试。
                    timeout=(4.0, 6.0),
                )
            except requests.exceptions.RequestException as exc:
                logger.warning("Remote image fetch failed: %s", exc.__class__.__name__)
                return None

            try:
                if 300 <= response.status_code < 400:
                    location = response.headers.get("location") or response.headers.get("Location")
                    if not location:
                        return None
                    next_url = urljoin(current_url, location)
                    if not next_url or self.rejection_reason(next_url):
                        return None
                    current_url = next_url
                    continue

                content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                if response.status_code != 200 or content_type not in SAFE_REMOTE_IMAGE_TYPES:
                    return None

                content_length = parse_int_value(response.headers.get("content-length"), 0, minimum=0)
                if content_length > MAX_IMAGE_BYTES:
                    return None

                chunks = []
                total = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        return None
                    chunks.append(chunk)

                content = b"".join(chunks)
                ext = SAFE_REMOTE_IMAGE_TYPES[content_type]
                cache_path = self.proxy_dir / f"{cache_key}{ext}"
                if self.write_cache_bytes_if_safe(cache_path, self.proxy_dir, content):
                    return CachedImage(path=str(cache_path), media_type=content_type, cache_status="MISS")
                return CachedImage(content=content, media_type=content_type, cache_status="MISS")
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()

        return None

    def get_or_create_thumbnail(self, original_path: Path, image_url: str, width: int) -> Optional[Path]:
        if Image is None or ImageOps is None:
            return None
        if width <= 0:
            return None

        thumb_dir = self.proxy_dir / f"thumb_{width}"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        cache_key = hashlib.sha256(image_url.encode("utf-8")).hexdigest()
        thumb_path = thumb_dir / f"{cache_key}.jpg"
        if self.is_safe_cache_file(thumb_path, thumb_dir, {".jpg", ".jpeg"}):
            os.utime(thumb_path, None)
            return thumb_path

        try:
            with Image.open(original_path) as image:
                image = ImageOps.exif_transpose(image)
                # Jable/JavDB 的“封面”是整张封套（背面在左、正面在右），
                # 横版且宽高比 ≥1.35 时裁掉左侧，只保留右侧 2:3 的封面正面
                if image.width > image.height and image.width / image.height >= 1.35:
                    front_width = round(image.height * 2 / 3)
                    image = image.crop((image.width - front_width, 0, image.width, image.height))
                image.thumbnail((width, max(width * 4, width)), Image.Resampling.LANCZOS)
                if image.mode not in ("RGB", "L"):
                    background = Image.new("RGB", image.size, (15, 17, 21))
                    if image.mode in ("RGBA", "LA"):
                        background.paste(image, mask=image.getchannel("A"))
                    else:
                        background.paste(image.convert("RGB"))
                    image = background
                else:
                    image = image.convert("RGB")
                image.save(thumb_path, format="JPEG", quality=DEFAULT_THUMBNAIL_QUALITY, optimize=True)
            return thumb_path if self.is_safe_cache_file(thumb_path, thumb_dir, {".jpg", ".jpeg"}) else None
        except Exception as exc:
            logger.debug("Thumbnail generation skipped: %s", exc.__class__.__name__)
            return None

    def collect_remote_image_urls(self, items: Any, limit: int = IMAGE_CACHE_WARMUP_LIMIT) -> List[str]:
        if not isinstance(items, list):
            return []

        image_urls = []
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("cover", "avatar", "image", "poster", "thumb", "thumbnail"):
                image_url = self.normalize_url(item.get(key))
                if image_url and image_url not in image_urls:
                    image_urls.append(image_url)
                    if len(image_urls) >= limit:
                        return image_urls
        return image_urls

    def warm_remote_thumbnails(self, original_path: Path, image_url: str) -> int:
        # 原图预热后顺手把详情页/卡片要用的缩略图尺寸也生成出来，
        # 避免用户第一次点开影片详情时才去上游抓 720 大图。
        created = 0
        for width in IMAGE_CACHE_WARMUP_THUMBNAIL_WIDTHS:
            thumb_dir = self.proxy_dir / f"thumb_{width}"
            thumb_path = thumb_dir / f"{hashlib.sha256(image_url.encode('utf-8')).hexdigest()}.jpg"
            if self.is_safe_cache_file(thumb_path, thumb_dir, {".jpg", ".jpeg"}):
                continue
            if self.get_or_create_thumbnail(original_path, image_url, width):
                created += 1
        return created

    def warm_remote_cache(self, image_urls: List[str], stop_event=None) -> Dict[str, Any]:
        summary = {
            "total": len(image_urls[:IMAGE_CACHE_WARMUP_LIMIT]),
            "hit": 0,
            "cached": 0,
            "failed": 0,
            "thumbs": 0,
        }
        for image_url in image_urls[:IMAGE_CACHE_WARMUP_LIMIT]:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                normalized = self.normalize_url(image_url)
                if not normalized:
                    summary["failed"] += 1
                    continue
                was_cached = self.find_cached_original(normalized) is not None
                original_path = self.cache_remote_url(normalized)
                if original_path:
                    summary["hit" if was_cached else "cached"] += 1
                    summary["thumbs"] += self.warm_remote_thumbnails(Path(original_path), normalized)
                else:
                    summary["failed"] += 1
            except Exception as exc:
                logger.warning("Image cache warmup item failed: %s", exc.__class__.__name__)
                summary["failed"] += 1
            if stop_event is not None:
                stop_event.wait(0.05)
        return summary

    def get_stats(self, force_refresh: bool = False) -> Dict[str, Any]:
        with self._stats_lock:
            if self._stats_cache is not None and not force_refresh:
                age_seconds = time.time() - self._stats_cache_at
                if age_seconds >= IMAGE_CACHE_STATS_TTL_SECONDS and not self._stats_refreshing:
                    self._stats_refreshing = True
                    threading.Thread(target=self._refresh_stats, daemon=True).start()
                result = dict(self._stats_cache)
                result["stale"] = age_seconds >= IMAGE_CACHE_STATS_TTL_SECONDS
                result["refreshing"] = self._stats_refreshing
                return result

        total_size = 0
        total_count = 0
        legacy_count = 0
        proxy_count = 0
        thumbnail_count = 0
        failure_count = 0
        category_bytes = {
            "remote_images": 0,
            "jellyfin_images": 0,
            "thumbnails": 0,
            "failure_markers": 0,
            "other": 0,
        }
        category_counts = {key: 0 for key in category_bytes}
        if self.cache_root.exists():
            for root, _dirs, filenames in os.walk(self.cache_root):
                for filename in filenames:
                    item = Path(root) / filename
                    if item.is_symlink():
                        continue
                    try:
                        size = item.stat().st_size
                        total_size += size
                        total_count += 1
                        relative_parts = item.relative_to(self.cache_root).parts
                        if relative_parts and relative_parts[0] == "jellyfin":
                            category = "jellyfin_images"
                        elif relative_parts and relative_parts[0] == "proxy_failures":
                            category = "failure_markers"
                        elif any(part.startswith("thumb_") for part in relative_parts):
                            category = "thumbnails"
                        elif not relative_parts or relative_parts[0] == "proxy" or len(relative_parts) == 1:
                            category = "remote_images"
                        else:
                            category = "other"
                        category_bytes[category] += size
                        category_counts[category] += 1
                        suffix = item.suffix.lower()
                        if len(relative_parts) == 1 and suffix in CACHE_CLEANUP_IMAGE_SUFFIXES:
                            legacy_count += 1
                        elif len(relative_parts) == 2 and relative_parts[0] == "proxy" and suffix in CACHE_CLEANUP_IMAGE_SUFFIXES:
                            proxy_count += 1
                        elif category == "thumbnails" and suffix in {".jpg", ".jpeg"}:
                            thumbnail_count += 1
                        elif category == "failure_markers" and suffix == ".json":
                            failure_count += 1
                    except OSError:
                        pass

        events = list(self.recent_events)
        hits = events.count("hit")
        misses = events.count("miss")
        attempts = hits + misses
        result = {
            "legacy_count": legacy_count,
            "proxy_count": proxy_count,
            "thumbnail_count": thumbnail_count,
            "failure_count": failure_count,
            "total_size_mb": round(total_size / 1024 / 1024, 2),
            "total_size_bytes": total_size,
            "total_count": total_count,
            "hit_recent": hits,
            "miss_recent": misses,
            "failure_recent": events.count("failure"),
            "hit_rate_recent": round(hits / attempts, 4) if attempts else None,
            "refreshing_count": self.refreshing_count,
            "categories": {
                key: {
                    "bytes": category_bytes[key],
                    "size_mb": round(category_bytes[key] / 1024 / 1024, 2),
                    "count": category_counts[key],
                }
                for key in category_bytes
            },
            "pillow_available": Image is not None,
        }
        with self._stats_lock:
            self._stats_cache = result
            self._stats_cache_at = time.time()
            self._stats_refreshing = False
        try:
            db.set_cache(self._stats_cache_key, result)
        except Exception:
            logger.warning("Image cache stats snapshot could not be saved")
        return {**result, "stale": False, "refreshing": False}

    def _refresh_stats(self) -> None:
        try:
            self.get_stats(force_refresh=True)
        except Exception as exc:
            logger.warning("Image cache stats refresh failed: %s", exc.__class__.__name__)
            with self._stats_lock:
                self._stats_refreshing = False

    def invalidate_category(self, category: str) -> Dict[str, Any]:
        allowed_categories = {
            "remote_images",
            "jellyfin_images",
            "thumbnails",
            "failure_markers",
            "all",
        }
        if category not in allowed_categories:
            raise ValueError("unsupported_cache_category")

        candidates: List[tuple[Path, Path, set]] = []
        if category in {"remote_images", "all"}:
            candidates.extend(
                (path, self.cache_root, SAFE_REMOTE_IMAGE_EXTENSIONS)
                for path in self._direct_image_files(self.cache_root)
            )
            candidates.extend(
                (path, self.proxy_dir, SAFE_REMOTE_IMAGE_EXTENSIONS)
                for path in self._direct_image_files(self.proxy_dir)
            )
        if category in {"jellyfin_images", "all"}:
            jellyfin_dir = self.cache_root / "jellyfin"
            if jellyfin_dir.exists():
                candidates.extend(
                    (path, jellyfin_dir, {".jpg", ".jpeg"})
                    for path in jellyfin_dir.rglob("*")
                    if path.is_file()
                )
        if category in {"thumbnails", "all"} and self.proxy_dir.exists():
            for thumb_dir in self.proxy_dir.glob("thumb_*"):
                if thumb_dir.is_dir():
                    candidates.extend(
                        (path, thumb_dir, {".jpg", ".jpeg"})
                        for path in self._direct_image_files(thumb_dir)
                    )
        if category in {"failure_markers", "all"} and self.failure_dir.exists():
            candidates.extend(
                (path, self.failure_dir, {".json"})
                for path in self.failure_dir.glob("*.json")
            )

        deleted_count = 0
        freed_bytes = 0
        seen = set()
        for path, root, allowed_suffixes in candidates:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in seen or not self.is_safe_cache_file(path, root, allowed_suffixes):
                continue
            seen.add(resolved)
            try:
                size = path.stat().st_size
                path.unlink()
                deleted_count += 1
                freed_bytes += size
            except OSError:
                continue

        return {
            "category": category,
            "deleted_count": deleted_count,
            "freed_bytes": freed_bytes,
            "freed_mb": round(freed_bytes / 1024 / 1024, 2),
        }

    @property
    def refreshing_count(self) -> int:
        with self._fetch_locks_guard:
            return sum(1 for lock in self._fetch_locks.values() if lock.locked())

    def is_failure_fresh(self, image_url: str) -> bool:
        marker = self._failure_marker_path(image_url)
        if not marker.exists():
            return False
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            created_at = float(payload.get("created_at") or 0)
        except Exception:
            return False
        if time.time() - created_at < FAILURE_TTL_SECONDS:
            return True
        try:
            marker.unlink()
        except OSError:
            pass
        return False

    def mark_failure(self, image_url: str, reason: str) -> None:
        self.failure_dir.mkdir(parents=True, exist_ok=True)
        marker = self._failure_marker_path(image_url)
        payload = {"created_at": time.time(), "reason": reason}
        try:
            marker.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass

    def normalize_width(self, width: Optional[int]) -> Optional[int]:
        if width in (None, "", 0):
            return None
        try:
            parsed = int(width)
        except (TypeError, ValueError):
            return None
        if parsed < 32:
            return None
        return min(parsed, 1200)

    def get_system_proxy_config(self) -> Optional[Dict[str, str]]:
        return build_system_proxy_config(db.get_config("system_proxy") or config.SYSTEM_PROXY)

    def is_safe_cache_file(self, path: Any, root: Any, allowed_suffixes: Optional[set] = None) -> bool:
        try:
            candidate = Path(path)
            root_path = Path(root).resolve()
            if allowed_suffixes and candidate.suffix.lower() not in allowed_suffixes:
                return False
            if candidate.is_symlink() or not candidate.is_file():
                return False
            candidate.resolve().relative_to(root_path)
            return True
        except Exception:
            return False

    def write_cache_bytes_if_safe(self, path: Any, root: Any, content: bytes) -> bool:
        temporary_path = None
        try:
            target = Path(path)
            root_path = Path(root).resolve()
            target.parent.resolve().relative_to(root_path)
            if target.exists() and (target.is_symlink() or not target.is_file()):
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
            return True
        except Exception as exc:
            logger.debug("Skipped unsafe cache write: %s", exc.__class__.__name__)
            return False
        finally:
            if temporary_path and temporary_path.exists():
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

    def _failure_marker_path(self, image_url: str) -> Path:
        cache_key = hashlib.sha256(image_url.encode("utf-8")).hexdigest()
        return self.failure_dir / f"{cache_key}.json"

    def _direct_image_files(self, directory: Path) -> List[Path]:
        if not directory.exists():
            return []
        return [
            item for item in directory.iterdir()
            if item.is_file() and item.suffix.lower() in CACHE_CLEANUP_IMAGE_SUFFIXES
        ]

    def _record_event(self, event_type: str) -> None:
        self.recent_events.append(event_type)

    @staticmethod
    def _is_fake_proxy_ip_address(address: str) -> bool:
        return is_fake_proxy_ip_address(address)

    def _is_blocked_remote_ip(self, address: str, *, allow_fake_proxy_ip: bool = False) -> bool:
        return is_blocked_remote_ip(address, allow_fake_proxy_ip=allow_fake_proxy_ip)


image_cache_service = ImageCacheService(config.COVER_CACHE_DIR)
