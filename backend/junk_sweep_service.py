"""400MB 以下碎片的**索引层**清理：amane media_files + Jellyfin 条目。

文件本身的物理删除由栈内 ``junk_sweeper`` 容器负责（它挂了 ``/hp``，直扫磁盘）；
本服务负责让「已经建成条目」的垃圾立刻从两个索引里消失 —— 否则删了文件，
下一次扫描/刷新又会把它建回来，而且用户在这期间一直能看到广告片。

判定口径全部来自 ``media_policy``（阈值 = JUNK_MIN_VIDEO_MB，默认 400）。
体积未知（0/None）的条目一律不动：宁可漏一次，也不能把没读到 size 的正片删了。

对外只有一个方法 ``sweep()``，report 结构固定，方便接口/日志/单测共用。
"""
from typing import Any, Dict, List, Optional

import media_policy


def pick_undersized_media(items: List[Dict[str, Any]], min_bytes: int) -> List[Dict[str, Any]]:
    """从 amane media 列表里挑出体积不足的视频条目。"""
    picked: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        size = media_policy.parse_size(item.get("size"))
        path = item.get("path") or item.get("number") or ""
        if not media_policy.is_undersized_video(path, size, min_bytes=min_bytes):
            continue
        picked.append({
            "id": item.get("id"),
            "number": item.get("number"),
            "size": size,
            "path": path,
            "status": item.get("status"),
        })
    return picked


def jellyfin_item_size(item: Dict[str, Any]) -> int:
    """Jellyfin 条目的体积：优先 Size，退化到 MediaSources[0].Size。"""
    size = media_policy.parse_size(item.get("Size"))
    if size:
        return size
    sources = item.get("MediaSources")
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict):
                continue
            size = media_policy.parse_size(source.get("Size"))
            if size:
                return size
    return 0


def pick_undersized_jellyfin_items(items: List[Dict[str, Any]], min_bytes: int) -> List[Dict[str, Any]]:
    """从 Jellyfin 库条目里挑出体积不足的视频。"""
    picked: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("Id") or "").strip()
        if not item_id:
            continue
        size = jellyfin_item_size(item)
        path = str(item.get("Path") or "")
        # Path 拿不到时按「条目本身就是视频」处理（IncludeItemTypes=Movie 拉回来的）
        if path and not media_policy.is_undersized_video(path, size, min_bytes=min_bytes):
            continue
        if not path and not (0 < size < int(min_bytes)):
            continue
        picked.append({
            "id": item_id,
            "name": str(item.get("Name") or "")[:80],
            "size": size,
            "path": path,
        })
    return picked


class JunkSweepService:
    def __init__(
        self,
        *,
        amane_client: Any = None,
        jellyfin_client: Any = None,
        logger: Any = None,
        max_deletes_per_run: int = 200,
        sample_limit: int = 10,
    ) -> None:
        self._amane_client = amane_client
        self.jellyfin_client = jellyfin_client
        self.logger = logger
        self.max_deletes_per_run = max(1, int(max_deletes_per_run))
        self.sample_limit = max(0, int(sample_limit))
        if self.logger is None:
            import logging

            self.logger = logging.getLogger(__name__)

    # ---------- 内部 ----------

    def _threshold(self, min_bytes: Optional[int]) -> int:
        return int(min_bytes) if min_bytes else media_policy.min_video_bytes()

    def _client(self) -> Any:
        return self._amane_client

    def _list_all_media(self, client: Any, page_size: int = 200) -> List[Dict[str, Any]]:
        collected: List[Dict[str, Any]] = []
        offset = 0
        # amane 库现在三千条量级；上限只是防「total 一直涨」把循环拖死
        for _ in range(500):
            page = client.list_media(limit=page_size, offset=offset)
            items = page.get("items") or []
            if not items:
                break
            collected.extend(items)
            offset += len(items)
            total = page.get("total")
            if isinstance(total, int) and offset >= total:
                break
        return collected

    # ---------- amane ----------

    def sweep_amane(self, *, min_bytes: Optional[int] = None, dry_run: bool = True) -> Dict[str, Any]:
        threshold = self._threshold(min_bytes)
        client = self._client()
        report: Dict[str, Any] = {
            "scanned": 0, "matched": 0, "deleted": 0, "failed": 0,
            "dry_run": bool(dry_run), "samples": [], "errors": [],
        }
        if client is None:
            report["skipped"] = "amane_not_configured"
            return report

        try:
            items = self._list_all_media(client)
        except Exception as exc:
            # amane 没配置/不可用时整轮跳过，不能把巡检循环打挂
            report["skipped"] = f"amane_unavailable:{type(exc).__name__}"
            return report
        report["scanned"] = len(items)
        targets = pick_undersized_media(items, threshold)
        report["matched"] = len(targets)
        report["samples"] = targets[: self.sample_limit]

        for target in targets[: self.max_deletes_per_run]:
            if dry_run:
                continue
            try:
                client.delete_media(int(target["id"]))
                report["deleted"] += 1
            except Exception as exc:  # 单条失败不影响整轮
                report["failed"] += 1
                if len(report["errors"]) < self.sample_limit:
                    report["errors"].append(
                        f"{target.get('id')}:{type(exc).__name__}"
                    )
        if report["deleted"]:
            self.logger.warning(
                "junk sweep (amane): deleted %s media entries below %sMB",
                report["deleted"], threshold // (1024 * 1024),
            )
        return report

    # ---------- Jellyfin ----------

    def sweep_jellyfin(self, *, min_bytes: Optional[int] = None, dry_run: bool = True) -> Dict[str, Any]:
        threshold = self._threshold(min_bytes)
        report: Dict[str, Any] = {
            "scanned": 0, "matched": 0, "deleted": 0, "failed": 0,
            "dry_run": bool(dry_run), "samples": [], "errors": [],
        }
        client = self.jellyfin_client
        if client is None:
            report["skipped"] = "jellyfin_not_configured"
            return report

        try:
            items = client.list_library_items_raw(fields="Size,Path")
        except Exception as exc:
            report["skipped"] = f"jellyfin_unavailable:{type(exc).__name__}"
            return report
        report["scanned"] = len(items)
        targets = pick_undersized_jellyfin_items(items, threshold)
        report["matched"] = len(targets)
        report["samples"] = targets[: self.sample_limit]

        for target in targets[: self.max_deletes_per_run]:
            if dry_run:
                continue
            try:
                result = client.delete_items([target["id"]])
                if (result or {}).get("success"):
                    report["deleted"] += 1
                else:
                    report["failed"] += 1
            except Exception as exc:
                report["failed"] += 1
                if len(report["errors"]) < self.sample_limit:
                    report["errors"].append(f"{target.get('id')}:{type(exc).__name__}")
        if report["deleted"]:
            self.logger.warning(
                "junk sweep (jellyfin): deleted %s items below %sMB",
                report["deleted"], threshold // (1024 * 1024),
            )
        return report

    # ---------- 入口 ----------

    def sweep(self, *, dry_run: bool = True, min_bytes: Optional[int] = None) -> Dict[str, Any]:
        threshold = self._threshold(min_bytes)
        amane = self.sweep_amane(min_bytes=threshold, dry_run=dry_run)
        jellyfin = self.sweep_jellyfin(min_bytes=threshold, dry_run=dry_run)
        return {
            "success": True,
            "min_size_mb": threshold // (1024 * 1024),
            "dry_run": bool(dry_run),
            "deleted": int(amane.get("deleted") or 0) + int(jellyfin.get("deleted") or 0),
            "matched": int(amane.get("matched") or 0) + int(jellyfin.get("matched") or 0),
            "amane": amane,
            "jellyfin": jellyfin,
        }
