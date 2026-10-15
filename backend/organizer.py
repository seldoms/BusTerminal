
import os
import shutil
import logging
import hashlib
import ipaddress
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Dict, Optional
from urllib.parse import urlparse, urlunsplit
import socket
from database import db, display_value_for_log
from network_security_utils import is_fake_proxy_ip_address
import config

logger = logging.getLogger(__name__)
MAX_ACTOR_THUMB_BYTES = 5 * 1024 * 1024
ALLOW_UNSAFE_PATH_OUTPUT = "BUS_TERMINAL_SHOW_UNSAFE_PATHS"
SAFE_ACTOR_THUMB_CONTENT_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_url_for_log(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if not parsed.scheme or not parsed.netloc:
        return "<url>"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def safe_path_for_log(path: str) -> str:
    text = "" if path is None else str(path)
    if os.getenv(ALLOW_UNSAFE_PATH_OUTPUT, "").lower() in {"1", "true", "yes", "on"}:
        return text
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"<path len={len(text)} sha256={digest}>"


def exception_type(exc: Exception) -> str:
    return type(exc).__name__


def is_safe_remote_image_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False

    host = parsed.hostname.rstrip(".").lower()

    def is_blocked_ip(address: str) -> bool:
        # Clash/mihomo fake-ip 段(198.18.0.0/15)不是真实目的地,
        # 实际连接由 TUN/代理远端解析,不参与 SSRF 私网判定。
        if is_fake_proxy_ip_address(address):
            return False
        ip = ipaddress.ip_address(address)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )

    try:
        return not is_blocked_ip(host)
    except ValueError:
        try:
            resolved = socket.getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror:
            return False
        return all(not is_blocked_ip(item[4][0]) for item in resolved)


def sanitize_path_component(value: Optional[str], fallback: str = "Unknown", max_length: int = 120) -> str:
    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text or text in {".", ".."}:
        text = fallback
    if text.upper() in WINDOWS_RESERVED_NAMES:
        text = f"_{text}"
    return text[:max_length].rstrip(" .") or fallback


def _legacy_organizer_enabled() -> bool:
    """环境变量逃生舱：运维确需临时启用本地整理时使用（默认关闭）。"""
    raw = os.getenv("BUS_TERMINAL_LEGACY_ORGANIZER", "")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


class Organizer:
    """
    负责媒体库内容的整理、NFO生成及文件链接。

    ⚠️ 方案 C（2026-10-10）下这些职责已整体交给 amane（计划 §2.4 元数据归属收口）：
    BusTerminal 不碰文件，NFO/海报/改名/整理全部由 amane 单独执笔，避免"两个作者
    写同一批文件"互相覆盖。生产路径没有调用点，这里默认只保留逻辑供受控场景与测试：
    写文件的方法在拿到 `allow_file_writes=True`（或环境变量 BUS_TERMINAL_LEGACY_ORGANIZER=1）
    之前一律不落盘。
    """
    def __init__(self, allow_file_writes: bool = False):
        # 从配置或数据库获取映射路径 (待后续接入配置页)
        self.library_root = os.getenv("JELLYFIN_LIBRARY_PATH", "C:/Jellyfin/Media")
        self.allow_file_writes = bool(allow_file_writes) or _legacy_organizer_enabled()

    def _writes_blocked(self, what: str) -> bool:
        if self.allow_file_writes:
            return False
        logger.warning(
            "Organizer.%s 已停用：元数据与文件整理归 amane（计划 §2.4），"
            "这里是空操作。确需使用请显式 allow_file_writes=True",
            what,
        )
        return True

    def _library_root_abs(self) -> str:
        return os.path.abspath(self.library_root)

    def _ensure_within_library(self, path: str) -> str:
        root = self._library_root_abs()
        target = os.path.abspath(path)
        if os.path.commonpath([os.path.normcase(root), os.path.normcase(target)]) != os.path.normcase(root):
            raise ValueError("path_outside_library_root")
        return target

    def _safe_library_path(self, *parts: str) -> str:
        root = self._library_root_abs()
        target = os.path.abspath(os.path.join(root, *parts))
        return self._ensure_within_library(target)

    def ensure_dir(self, path: str):
        target = self._ensure_within_library(path)
        os.makedirs(target, exist_ok=True)

    def generate_nfo(self, movie_data: Dict, save_dir: str):
        """生成符合 Jellyfin/Kodi 标准的 NFO 文件（生产已停用，见类注记）"""
        if self._writes_blocked("generate_nfo"):
            return None
        try:
            save_dir = self._ensure_within_library(save_dir)
            root = ET.Element("movie")
            
            # 基本字段
            ET.SubElement(root, "title").text = movie_data.get("title")
            ET.SubElement(root, "originaltitle").text = movie_data.get("original_title")
            ET.SubElement(root, "uniqueid", type="num").text = movie_data.get("id")
            ET.SubElement(root, "id").text = movie_data.get("id")
            ET.SubElement(root, "premiered").text = movie_data.get("release_date")
            ET.SubElement(root, "releasedate").text = movie_data.get("release_date")
            ET.SubElement(root, "year").text = movie_data.get("release_date", "")[:4] if movie_data.get("release_date") else ""
            
            # 详情
            detail = movie_data.get("detail_json", {})
            if isinstance(detail, str):
                import json
                try: detail = json.loads(detail)
                except Exception: detail = {}
            
            ET.SubElement(root, "plot").text = detail.get("description", "")
            
            # 演员
            for actor in detail.get("actresses", []):
                actor_node = ET.SubElement(root, "actor")
                ET.SubElement(actor_node, "name").text = actor.get("name")
                ET.SubElement(actor_node, "thumb").text = actor.get("avatar")
            
            # 分类
            for genre in detail.get("genres", []):
                ET.SubElement(root, "genre").text = genre.get("name") if isinstance(genre, dict) else genre

            tree = ET.ElementTree(root)
            safe_movie_id = sanitize_path_component(movie_data.get("id"), fallback="movie", max_length=80)
            nfo_path = os.path.join(save_dir, f"{safe_movie_id}.nfo")
            
            # 格式化并保存
            from xml.dom import minidom
            xml_str = ET.tostring(root, encoding='utf-8')
            pretty_xml = minidom.parseString(xml_str).toprettyxml(indent="  ")
            
            with open(nfo_path, "w", encoding="utf-8") as f:
                f.write(pretty_xml)
            
            logger.info("NFO generated for %s: %s", display_value_for_log(movie_data.get('id')), safe_path_for_log(nfo_path))
            return nfo_path
        except Exception as e:
            logger.error("Error generating NFO for %s: %s", display_value_for_log(movie_data.get('id')), exception_type(e))
            return None

    def create_symlink_for_movie(self, movie_id: str, source_file: str):
        """
        为影片创建硬链接或复制文件。
        目录结构: [LibraryRoot]/[Actor]/[ID] Title/[ID].mp4

        优先使用硬链接（同分区），如果失败则回退到复制文件（跨分区）。
        生产已停用，见类注记。
        """
        if self._writes_blocked("create_symlink_for_movie"):
            return False
        movie = db.get_movie(movie_id)
        if not movie:
            logger.warning("Symlink failed: Movie %s not found in DB", display_value_for_log(movie_id))
            db.record_lifecycle_event(
                movie_id,
                "organize_failed",
                organize_status="failed",
                source="organizer",
                reason="movie_not_found",
                execution_result="failed",
                error_summary="movie_not_found",
            )
            return False

        if not db.is_managed_file_path(source_file):
            logger.warning("Organizer blocked unmanaged source path: %s", safe_path_for_log(source_file))
            db.record_lifecycle_event(
                movie_id,
                "organize_blocked",
                title=movie.get("title"),
                organize_status="blocked",
                source="organizer",
                reason="source_outside_managed_roots",
                execution_result="blocked",
                error_summary="source_outside_managed_roots",
            )
            return False

        db.record_lifecycle_event(
            movie_id,
            "organize_started",
            title=movie.get("title"),
            organize_status="organizing",
            source="organizer",
            reason="managed source accepted",
            execution_result="started",
        )

        detail = movie.get("detail_json", {})
        if isinstance(detail, str):
            import json
            try: detail = json.loads(detail)
            except Exception: detail = {}
        
        # 确定主演员（用于分类）
        actors = detail.get("actresses", [])
        actor_name = actors[0].get("name", "Unknown") if actors else "Unknown"
        
        # 构造目标路径
        actor_folder = sanitize_path_component(actor_name)
        safe_movie_id = sanitize_path_component(movie_id, fallback="movie", max_length=80)
        folder_name = sanitize_path_component(f"{safe_movie_id} {movie.get('title')}", fallback=safe_movie_id)
        target_dir = self._safe_library_path(actor_folder, folder_name)
        self.ensure_dir(target_dir)

        ext = sanitize_path_component(os.path.splitext(source_file)[1], fallback=".mp4", max_length=16)
        if not ext.startswith("."):
            ext = f".{ext}"
        link_path = os.path.join(target_dir, f"{safe_movie_id}{ext}")

        try:
            if os.path.exists(link_path):
                os.remove(link_path)
            
            # 尝试使用硬链接，如果失败（跨分区）则回退到复制
            try:
                os.link(source_file, link_path)
                logger.info("Hard link created: %s -> %s", safe_path_for_log(link_path), safe_path_for_log(source_file))
            except OSError as link_error:
                # 硬链接失败（通常是跨分区错误），回退到复制
                logger.warning(
                    "Hard link failed (%s), falling back to copy: %s",
                    exception_type(link_error),
                    safe_path_for_log(source_file),
                )
                import shutil
                shutil.copy2(source_file, link_path)
                logger.info("File copied: %s <- %s", safe_path_for_log(link_path), safe_path_for_log(source_file))
            
            # 生成配套 NFO
            self.generate_nfo(movie, target_dir)

            # 下载女优头像到 .actors 目录 (Jellyfin 离线图片支持)
            self.download_actor_thumbs(actors, target_dir)
            
            # 保存到数据库 mapping
            file_size = os.path.getsize(source_file)
            if not db.save_local_file(movie_id, source_file, link_path, file_size):
                raise RuntimeError("local_file_save_failed")

            db.record_lifecycle_event(
                movie_id,
                "organize_completed",
                title=movie.get("title"),
                organize_status="completed",
                source="organizer",
                reason="media linked into library",
                execution_result="success",
            )

            return True
        except Exception as e:
            logger.error("Symlink/Copy creation failed: %s", exception_type(e))
            db.record_lifecycle_event(
                movie_id,
                "organize_failed",
                title=movie.get("title"),
                organize_status="failed",
                source="organizer",
                reason="organize_operation_failed",
                execution_result="failed",
                error_summary=exception_type(e),
            )
            return False

    def download_actor_thumbs(self, actors: list, target_dir: str):
        """下载演员头像到本地 .actors 文件夹（生产已停用，见类注记）"""
        if self._writes_blocked("download_actor_thumbs"):
            return None
        import requests
        target_dir = self._ensure_within_library(target_dir)
        actors_dir = os.path.join(target_dir, ".actors")
        try:
            if actors and not os.path.exists(actors_dir):
                self.ensure_dir(actors_dir)
            
            for actor in actors:
                name = actor.get("name")
                thumb_url = actor.get("avatar")
                if name and thumb_url:
                    if not is_safe_remote_image_url(thumb_url):
                        continue

                    safe_name = sanitize_path_component(name)
                    thumb_path = os.path.join(actors_dir, f"{safe_name}.jpg")
                    if not os.path.exists(thumb_path):
                        try:
                            try:
                                resp = requests.get(thumb_url, timeout=10, verify=True, stream=True, allow_redirects=False)
                            except requests.exceptions.SSLError:
                                if not getattr(config, "SCRAPER_ALLOW_INSECURE_IMAGE_FETCH", False):
                                    raise
                                resp = requests.get(thumb_url, timeout=10, verify=False, stream=True, allow_redirects=False)
                            if resp.status_code == 200:
                                content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                                if content_type not in SAFE_ACTOR_THUMB_CONTENT_TYPES:
                                    continue
                                total = 0
                                with open(thumb_path, "wb") as f:
                                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                                        if not chunk:
                                            continue
                                        total += len(chunk)
                                        if total > MAX_ACTOR_THUMB_BYTES:
                                            raise ValueError("actor_thumbnail_too_large")
                                        f.write(chunk)
                        except Exception as exc:
                            if os.path.exists(thumb_path):
                                try:
                                    os.remove(thumb_path)
                                except OSError:
                                    pass
                            logger.debug("Failed to download actor thumbnail %s: %s", safe_url_for_log(thumb_url), exception_type(exc))
        except Exception as e:
            logger.warning("Failed to download actor thumbs: %s", exception_type(e))

organizer = Organizer()
