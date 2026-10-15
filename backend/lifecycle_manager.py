
import os
import logging
import hashlib
from pathlib import Path
import qb_api
from database import db, display_value_for_log, parse_int_value
import config
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)
ALLOW_UNSAFE_PATH_OUTPUT = "BUS_TERMINAL_SHOW_UNSAFE_PATHS"

# 广告常见扩展名及关键词
AD_EXTS = ['.url', '.txt', '.exe', '.bat', '.cmd', '.lnk', '.html', '.htm']
AD_KEYWORDS = ['广告', '推广', '最新地址', '官网', 'readme', 'index', 'visit', 'subscribe']

# 保留的媒体相关扩展名
KEEP_EXTS = ['.mp4', '.mkv', '.avi', '.wmv', '.mov', '.flv', '.ts', '.m2ts',  # 视频
             '.nfo',  # 元数据
             '.jpg', '.png', '.jpeg', '.webp', '.bmp',  # 图片
             '.srt', '.ass', '.ssa', '.sub', '.sup', '.idx']  # 字幕


def display_path(path: str) -> str:
    text = str(path or "")
    if os.getenv(ALLOW_UNSAFE_PATH_OUTPUT, "").lower() in {"1", "true", "yes", "on"}:
        return text
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"<path len={len(text)} sha256={digest}>"


def exception_type(exc: Exception) -> str:
    return type(exc).__name__


class LifecycleManager:
    @staticmethod
    def _normalize_path_for_match(path: str) -> Optional[str]:
        text = str(path or "").strip()
        if not text:
            return None
        try:
            return os.path.normcase(str(Path(text).expanduser().resolve()))
        except Exception:
            try:
                return os.path.normcase(os.path.abspath(text))
            except Exception:
                return None

    @classmethod
    def _paths_match_for_qb_torrent(cls, managed_path: str, torrent_path: str) -> bool:
        left = cls._normalize_path_for_match(managed_path)
        right = cls._normalize_path_for_match(torrent_path)
        if not left or not right:
            return False
        if left == right:
            return True
        left_parts = Path(left).parts
        right_parts = Path(right).parts
        if len(left_parts) <= len(right_parts):
            return right_parts[:len(left_parts)] == left_parts
        return left_parts[:len(right_parts)] == right_parts

    @staticmethod
    def _safe_remove_managed_file(path: str, description: str) -> bool:
        if not path or not os.path.exists(path):
            return False
        if not db.is_managed_file_path(path):
            logger.warning("Lifecycle: skipped deleting unmanaged %s path: %s", description, display_path(path))
            return False
        os.remove(path)
        return True

    @staticmethod
    def _safe_remove_empty_managed_dir(path: str, description: str) -> bool:
        if not path or not os.path.isdir(path) or os.listdir(path):
            return False
        if not db.is_managed_file_path(path):
            logger.warning("Lifecycle: skipped deleting unmanaged %s directory: %s", description, display_path(path))
            return False
        os.rmdir(path)
        return True
    
    @staticmethod
    def clean_ads_pre_download(session, base_url, torrent_hash):
        """
        在下载前通过 qB 接口过滤掉广告文件。
        """
        try:
            files = qb_api.get_torrent_files(session, base_url, torrent_hash)
            if not files:
                return
            
            ad_file_ids = []
            video_files = []
            
            for fallback_index, f in enumerate(files):
                if not isinstance(f, dict):
                    continue
                fname = str(f.get('name') or '').lower()
                if not fname:
                    continue
                ext = os.path.splitext(fname)[1]
                
                # 识别视频文件 (借用 main.py 中的典型后缀)
                VIDEO_EXTS = ['.mp4', '.mkv', '.avi', '.wmv', '.mov', '.flv']
                if ext in VIDEO_EXTS:
                    video_files.append(f)
                    continue
                
                # 识别广告文件
                is_ad = False
                if ext in AD_EXTS:
                    is_ad = True
                else:
                    for kw in AD_KEYWORDS:
                        if kw in fname:
                            is_ad = True
                            break
                
                # NFO 文件除外，我们需要它
                if ext == '.nfo':
                    is_ad = False
                    
                if is_ad:
                    try:
                        ad_file_ids.append(int(f.get('index', fallback_index)))
                    except (TypeError, ValueError):
                        ad_file_ids.append(fallback_index)
            
            if ad_file_ids:
                logger.info(
                    "Lifecycle: 发现 %s 个疑似广告文件，已设为禁止下载 (Hash: %s)",
                    len(ad_file_ids),
                    display_value_for_log(torrent_hash),
                )
                qb_api.set_file_priority(session, base_url, torrent_hash, ad_file_ids, 0)
        except Exception as e:
            logger.error("Lifecycle Pre-Clean Error: %s", exception_type(e))

    @staticmethod
    def clean_ads_post_download(directory):
        """
        下载完成后，物理扫描并删除残留的非媒体、非 NFO 文件。
        保留视频、元数据、图片和字幕文件。
        """
        if not os.path.exists(directory):
            return
        if not db.is_managed_file_path(directory):
            logger.warning("Lifecycle: skipped ad cleanup for unmanaged directory: %s", display_path(directory))
            return
            
        deleted_count = 0
        for root, dirs, files in os.walk(directory):
            for f in files:
                fpath = os.path.join(root, f)
                fname = f.lower()
                ext = os.path.splitext(fname)[1]
                
                # 保留所有媒体相关文件（视频、NFO、图片、字幕）
                if ext in KEEP_EXTS:
                    continue
                
                # 识别并删除广告文件
                is_trash = False
                if ext in AD_EXTS:
                    is_trash = True
                else:
                    for kw in AD_KEYWORDS:
                        if kw in fname:
                            is_trash = True
                            break
                
                # 小文件且不是已知扩展名的也大概率是垃圾
                if os.path.getsize(fpath) < 1024 * 1024: # < 1MB
                    is_trash = True
                
                if is_trash:
                    try:
                        if LifecycleManager._safe_remove_managed_file(fpath, "trash file"):
                            deleted_count += 1
                    except Exception as exc:
                        logger.debug("Lifecycle: failed to delete trash file %s: %s", display_path(fpath), exception_type(exc))
        
        if deleted_count > 0:
            logger.info("Lifecycle: 已从目录 %s 物理清理 %s 个残留广告/附属文件", display_path(directory), deleted_count)

    @staticmethod
    def run_auto_delete_gc():
        """将超过保留期的影片登记为清理候选，不执行任何删除。"""
        try:
            retention_days = parse_int_value(db.get_config('retention_days'), 30, minimum=1)
            expired_movies = db.get_lifecycle_expired_movies(retention_days)

            if not expired_movies:
                return {"scanned": 0, "candidates": 0}

            logger.info(
                "Lifecycle GC: 发现 %s 部影片已过期，登记清理候选 (保留期: %s天)",
                len(expired_movies),
                retention_days,
            )

            candidate_count = 0
            for m in expired_movies:
                movie_id = str(m.get("id") or "").strip()
                if not movie_id:
                    continue
                db.upsert_cleanup_candidate(
                    jellyfin_id=f"local:{movie_id}",
                    movie_id=movie_id,
                    title=m.get("title") or "",
                    file_path=m.get("file_path") or "",
                    reason=f"retention_expired:{retention_days}d",
                    source="lifecycle_retention_scan",
                )
                candidate_count += 1
            return {"scanned": len(expired_movies), "candidates": candidate_count}
        except Exception as e:
            logger.error("Lifecycle GC Task Error: %s", exception_type(e))
            return {"scanned": 0, "candidates": 0, "error": exception_type(e)}

    @staticmethod
    def _parse_btih_from_magnet(magnet: str) -> Optional[str]:
        """从磁力链接解析 btih 种子哈希。"""
        import re
        match = re.search(r"urn:btih:([A-Za-z0-9]{32,40})", str(magnet or ""))
        return match.group(1).lower() if match else None

    @staticmethod
    def run_auto_delete_watched(
        session,
        base_url: str,
        delete_jellyfin_item_fn,
        *,
        watch_count_max: int = 1,
        retention_days: int = 30,
        batch_limit: int = 50,
        keep_favorite: bool = True,
        reason_tag: str = "auto_delete",
    ) -> Dict:
        """自动删除已看过的影片：删 Jellyfin 条目 + 磁盘文件 + qBittorrent 种子，
        保留 DB 记录（靠 DB 去重杜绝重复下载）。

        条件：is_watched + watch_count<=N + (keep_favorite 时 not is_liked) + 超过保留期。
        delete_jellyfin_item_fn: Callable[[jellyfin_id], bool]，由调用方注入（避免循环依赖）。
        """
        summary = {
            "reason_tag": reason_tag,
            "scanned": 0,
            "deleted_jellyfin": 0,
            "deleted_files": 0,
            "deleted_torrents": 0,
            "failed": 0,
            "skipped": 0,
            "items": [],
        }
        try:
            targets = db.get_auto_delete_targets(
                watch_count_max=watch_count_max,
                retention_days=retention_days,
                limit=batch_limit,
            )
        except Exception as exc:
            logger.error("Auto-delete: 查询候选失败: %s", exception_type(exc))
            summary["error"] = exception_type(exc)
            return summary

        summary["scanned"] = len(targets)
        if not targets:
            logger.info("Auto-delete: 无符合条件的已看片候选 (reason=%s)", reason_tag)
            return summary

        logger.info(
            "Auto-delete: 开始处理 %s 部已看片 (reason=%s, watch_count<=%s, retention=%sd)",
            len(targets), reason_tag, watch_count_max, retention_days,
        )

        for m in targets:
            movie_id = str(m.get("id") or "").strip()
            jellyfin_id = str(m.get("jellyfin_id") or "").strip()
            file_path = m.get("file_path") or ""
            symlink_path = m.get("symlink_path") or ""
            title = m.get("title") or movie_id

            if not movie_id:
                summary["skipped"] += 1
                continue
            # 没有 jellyfin_id 无法安全删除 Jellyfin 条目，跳过（保留磁盘文件以免孤立）
            if not jellyfin_id:
                summary["skipped"] += 1
                logger.debug("Auto-delete: 跳过 %s（无 jellyfin_id 映射）", display_value_for_log(movie_id))
                continue

            item_result = {"movie_id": movie_id, "jellyfin_id": jellyfin_id, "title": title}
            item_failed = False

            # 1) 删 Jellyfin 媒体条目
            try:
                ok = bool(delete_jellyfin_item_fn(jellyfin_id))
                if ok:
                    summary["deleted_jellyfin"] += 1
                    item_result["jellyfin"] = "deleted"
                else:
                    item_failed = True
                    item_result["jellyfin"] = "failed"
            except Exception as exc:
                item_failed = True
                item_result["jellyfin"] = f"error:{exception_type(exc)}"

            # 2) 删本地磁盘文件 + 符号链接
            try:
                removed_file = LifecycleManager._safe_remove_managed_file(file_path, "auto_delete_video")
                removed_link = False
                if symlink_path and symlink_path != file_path:
                    removed_link = LifecycleManager._safe_remove_managed_file(symlink_path, "auto_delete_symlink")
                if removed_file or removed_link:
                    summary["deleted_files"] += 1
                    item_result["file"] = "deleted"
                else:
                    item_result["file"] = "not_found_or_unmanaged"
            except Exception as exc:
                item_result["file"] = f"error:{exception_type(exc)}"

            # 3) 删 qBittorrent 种子（delete_files=true，避免残留种子重新下载）
            try:
                torrent_deleted = False
                magnet = db.get_magnet_for_movie(movie_id)
                btih = LifecycleManager._parse_btih_from_magnet(magnet) if magnet else None
                if btih and session:
                    qb_api.delete_torrents(session, base_url, [btih], delete_files=True)
                    torrent_deleted = True
                elif session:
                    # 兜底：仅按规范化绝对路径精确/层级匹配 qB 种子，避免子串误删
                    try:
                        torrents = qb_api.get_torrents(session, base_url)
                        match_hashes = []
                        candidate_paths = [file_path, symlink_path]
                        for t in torrents:
                            torrent_path = str(t.get("content_path") or t.get("save_path") or "")
                            if not torrent_path or not any(
                                candidate and LifecycleManager._paths_match_for_qb_torrent(candidate, torrent_path)
                                for candidate in candidate_paths
                            ):
                                continue
                            h = t.get("hash")
                            if h:
                                match_hashes.append(h)
                        if match_hashes:
                            qb_api.delete_torrents(session, base_url, match_hashes, delete_files=True)
                            torrent_deleted = True
                    except Exception:
                        pass
                if torrent_deleted:
                    summary["deleted_torrents"] += 1
                    item_result["torrent"] = "deleted"
                else:
                    item_result["torrent"] = "not_found"
            except Exception as exc:
                item_result["torrent"] = f"error:{exception_type(exc)}"

            # 4) 保留 DB 记录（movies/local_files/download_queue 均不删）→ 去重防止重复下载
            # 5) 审计
            try:
                db.record_automation_event(
                    movie_id,
                    "auto_delete_watched",
                    actor="system",
                    reason=reason_tag,
                    payload=item_result,
                )
            except Exception as exc:
                logger.warning(
                    "Auto-delete: failed to record automation audit for %s (reason=%s): %s",
                    display_value_for_log(movie_id),
                    reason_tag,
                    exception_type(exc),
                )

            if item_failed:
                summary["failed"] += 1
            summary["items"].append(item_result)
            logger.info(
                "Auto-delete: %s | jellyfin=%s file=%s torrent=%s",
                display_value_for_log(movie_id),
                item_result.get("jellyfin"),
                item_result.get("file"),
                item_result.get("torrent"),
            )

        logger.info(
            "Auto-delete 完成 (reason=%s): jellyfin=%s files=%s torrents=%s failed=%s skipped=%s",
            reason_tag,
            summary["deleted_jellyfin"],
            summary["deleted_files"],
            summary["deleted_torrents"],
            summary["failed"],
            summary["skipped"],
        )
        return summary

lifecycle_manager = LifecycleManager()
