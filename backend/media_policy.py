"""媒体文件体积策略：**400MB 以下的视频一律当碎片/广告**。

这条策略原来只在 qB 选文件（download_service）里硬编码，于是同一个阈值在
其他环节要么各写一套、要么完全没有：amane 只过滤 100MB、Jellyfin 一份都不过滤，
结果种子自带的 10~20MB 广告 mp4 还是会被扫描/建条目。现在统一到这里，四个环节
共用一套判定：

  ① 下载选文件   download_service.filter_jav_torrent_files  → 只下 ≥ 阈值的那一个
  ② 下载后清理   lifecycle_manager.clean_ads_post_download  → 小视频当垃圾删
  ③ 文件层巡检   栈内 junk_sweeper 容器                     → 直扫 /hp 物理删除
  ④ 索引层巡检   junk_sweep_service                         → 清 amane / Jellyfin 条目

阈值只有一个旋钮：环境变量 ``JUNK_MIN_VIDEO_MB``（默认 400），
bus_terminal 与 junk_sweeper 容器都读它，改一处两边一致。
"""
import os
from typing import Any, Optional

DEFAULT_MIN_VIDEO_MB = 400
MIN_PLAYABLE_VIDEO_BYTES = DEFAULT_MIN_VIDEO_MB * 1024 * 1024

# 与 download_service 历史口径保持一致（多出来的 .m4v/.webm 只用于清理侧识别，
# 不影响下载选片逻辑——那边用的是本项目常量导入后的同一集合）
VIDEO_FILE_EXTS = {
    ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv", ".rmvb", ".ts", ".m2ts",
}

# qB 未完成标记：这些文件不是成品，不能按体积判垃圾
INCOMPLETE_SUFFIXES = (".!qb", ".part", ".parts", ".downloading", ".tmp")


def _int_env(name: str, default: int) -> int:
    try:
        value = int(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def min_video_mb(default_mb: int = DEFAULT_MIN_VIDEO_MB) -> int:
    """阈值（MB）。只认 JUNK_MIN_VIDEO_MB，避免多个旋钮互相打架。"""
    return _int_env("JUNK_MIN_VIDEO_MB", default_mb)


def min_video_bytes(default_mb: int = DEFAULT_MIN_VIDEO_MB) -> int:
    return min_video_mb(default_mb) * 1024 * 1024


def parse_size(value: Any) -> int:
    """把 size 字段抹成非负 int。None / 脏值一律 0（= 未知，不参与判定）。"""
    try:
        size = int(value)
    except (TypeError, ValueError):
        return 0
    return size if size > 0 else 0


def file_ext(name: Any) -> str:
    text = str(name or "").strip().lower()
    if not text:
        return ""
    dot = text.rfind(".")
    return text[dot:] if dot >= 0 else ""


def is_video_filename(name: Any) -> bool:
    return file_ext(name) in VIDEO_FILE_EXTS


def is_incomplete_filename(name: Any) -> bool:
    lowered = str(name or "").strip().lower()
    return bool(lowered) and lowered.endswith(INCOMPLETE_SUFFIXES)


def is_undersized_video(
    name: Any,
    size: Any,
    *,
    min_bytes: Optional[int] = None,
) -> bool:
    """是不是「体积不足的影片碎片」。

    三条同时成立才算：是视频扩展名、体积已知且 > 0、体积小于阈值。
    **体积未知（0/None）一律不判**——宁可漏一次，也不能把没读到 size 的
    正片当垃圾删掉。下载中的 .!qB/.part 也不算。
    """
    threshold = int(min_bytes) if min_bytes else MIN_PLAYABLE_VIDEO_BYTES
    if is_incomplete_filename(name) or not is_video_filename(name):
        return False
    value = parse_size(size)
    return 0 < value < threshold
