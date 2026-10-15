import os
import time
import hashlib
from datetime import datetime
import logging
import threading
import requests
from typing import Dict, List
from database import db
from number_parsing import extract_number

logger = logging.getLogger(__name__)
ALLOW_UNSAFE_LOG_OUTPUT = "BUS_TERMINAL_SHOW_UNSAFE_LOG_VALUES"


def unsafe_log_output_enabled():
    return os.getenv(ALLOW_UNSAFE_LOG_OUTPUT, "").lower() in {"1", "true", "yes", "on"}


def summarize_value(value) -> str:
    text = "" if value is None else str(value)
    if not text:
        return "<empty>"
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"<redacted len={len(text)} sha256={digest}>"


def display_path(path: str) -> str:
    if unsafe_log_output_enabled():
        return str(path or "")
    return summarize_value(path)


def safe_remove_empty_managed_dir(path: str, description: str) -> bool:
    if not path or not os.path.isdir(path) or os.listdir(path):
        return False
    if not db.is_managed_file_path(path):
        logger.warning("Skipped deleting unmanaged %s directory: %s", description, display_path(path))
        return False
    os.rmdir(path)
    return True


def safe_url_for_log(url: str) -> str:
    if unsafe_log_output_enabled():
        return str(url or "")
    return summarize_value(url)


def movie_label_for_log(value) -> str:
    text = str(value or "")
    if unsafe_log_output_enabled():
        return text
    number = extract_number(text)
    if number:
        return number
    return summarize_value(text)


def exception_type(exc: Exception) -> str:
    return exc.__class__.__name__

def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value, default, minimum=None, maximum=None):
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and parsed < minimum:
        return default
    if maximum is not None and parsed > maximum:
        return default
    return parsed

class JellyfinCleanupTask:
    """
    Jellyfin 媒体库清理任务
    每天定时检查：播放进度 >= 阈值且未收藏的影片，记录为清理候选。
    """
    
    def __init__(self):
        self.jellyfin_url = ""
        self.jellyfin_api_key = ""
        self.library_name = "黄片"
        self.progress_threshold = 80  # 进度阈值 %
        self.cleanup_enabled = False
        self.run_hour = 3
        self.is_running = False
        self._cleanup_thread = None
        self.last_run_time = None
        
    def reload_config(self):
        """从数据库加载配置"""
        self.jellyfin_url = db.get_config('jellyfin_host') or ""
        self.jellyfin_api_key = db.get_config('jellyfin_api_key') or ""
        self.library_name = db.get_config('jellyfin_library_name') or "黄片"
        enabled = db.get_config('cleanup_enabled')
        self.cleanup_enabled = _as_bool(enabled, False)
        
        progress = db.get_config('cleanup_progress_threshold')
        self.progress_threshold = _as_int(progress, self.progress_threshold, 0, 100)

        run_hour = db.get_config('cleanup_run_hour')
        self.run_hour = _as_int(run_hour, self.run_hour, 0, 23)
        
        logger.info(
            "JellyfinCleanup: URL=%s, Library=%s, Enabled=%s, Hour=%s, Threshold=%s%%",
            safe_url_for_log(self.jellyfin_url),
            movie_label_for_log(self.library_name),
            self.cleanup_enabled,
            self.run_hour,
            self.progress_threshold,
        )
    
    def start(self):
        """启动清理任务"""
        if self.is_running:
            logger.warning("JellyfinCleanup already running")
            return
        
        self.is_running = True
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()
        logger.info("JellyfinCleanup started")
    
    def stop(self):
        """停止清理任务"""
        self.is_running = False
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=5)
        logger.info("JellyfinCleanup stopped")
    
    def _cleanup_loop(self):
        """清理候选扫描循环（每小时检查一次）"""
        while self.is_running:
            try:
                self.reload_config()
                now = datetime.now()
                if (
                    self.cleanup_enabled
                    and now.hour == self.run_hour
                    and (self.last_run_time is None or (now - self.last_run_time).days >= 1)
                ):
                    self._run_cleanup()
                    self.last_run_time = now
            except Exception as e:
                logger.error("JellyfinCleanup error: %s", exception_type(e))
            
            time.sleep(3600)  # 每小时检查一次

    def run_registered_loop(self, stop_event, task_registry):
        """Run candidate scans under the application's shared task lifecycle."""
        task_name = "jellyfin_cleanup_candidate_scan"
        self.is_running = True
        try:
            while not stop_event.is_set():
                try:
                    self.reload_config()
                    now = datetime.now()
                    should_run = (
                        self.cleanup_enabled
                        and now.hour == self.run_hour
                        and (self.last_run_time is None or (now - self.last_run_time).days >= 1)
                    )
                    if should_run:
                        summary = self._run_cleanup() or {}
                        self.last_run_time = now
                    else:
                        summary = {
                            "status": "idle",
                            "enabled": self.cleanup_enabled,
                            "run_hour": self.run_hour,
                        }
                    task_registry.mark_success(task_name, summary=summary)
                except Exception as exc:
                    logger.error("JellyfinCleanup error: %s", exception_type(exc))
                    task_registry.mark_error(task_name, exc)
                stop_event.wait(3600)
        finally:
            self.is_running = False
    
    def _run_cleanup(self):
        """扫描并记录清理候选，不执行删除。"""
        logger.info("=" * 50)
        logger.info("开始执行 Jellyfin 清理候选扫描")
        logger.info("=" * 50)
        
        if not self.jellyfin_url or not self.jellyfin_api_key:
            logger.error("Jellyfin 配置未完成，跳过清理")
            return {"status": "skipped", "reason": "jellyfin_not_configured", "candidate_count": 0}
        
        try:
            # 1. 获取媒体库中的所有影片
            movies = self._get_library_movies()
            if not movies:
                logger.info("未找到待清理的影片")
                return {"status": "completed", "scanned_count": 0, "candidate_count": 0, "candidates": []}
            
            # 2. 分析每部影片的播放进度和收藏状态
            candidates = []
            auto_cleanup_enabled = (
                str(db.get_config("automation_mode") or "suggest").strip().lower()
                == "auto_approve_low_risk"
                and _as_bool(db.get_config("auto_cleanup_enabled"), False)
            )
            auto_approved_count = 0
            for movie in movies:
                jellyfin_id = movie.get('Id')
                movie_name = movie.get('Name', 'Unknown')
                
                # 获取播放进度
                progress = self._get_playback_progress(jellyfin_id)
                is_favorite = self._is_favorite(jellyfin_id)
                
                logger.info(
                    "检查: %s | 进度: %s%% | 收藏: %s",
                    movie_label_for_log(movie_name),
                    progress,
                    is_favorite,
                )
                
                # 条件：进度 >= 阈值 且 未收藏
                if progress >= self.progress_threshold and not is_favorite:
                    movie_id = self._extract_movie_id(movie_name)
                    path = movie.get('Path') or (movie.get('Paths', [''])[0] if movie.get('Paths') else '')
                    reason = f"播放进度 {progress}% >= {self.progress_threshold}% 且未收藏"
                    candidate_id = db.upsert_cleanup_candidate(
                        jellyfin_id=jellyfin_id,
                        movie_id=movie_id,
                        title=movie_name,
                        file_path=path,
                        progress=progress,
                        is_favorite=is_favorite,
                        reason=reason,
                    )
                    candidate_status = "candidate"
                    if auto_cleanup_enabled:
                        decision = db.decide_cleanup_candidate(
                            candidate_id,
                            "approved",
                            "system",
                            "low_risk_cleanup_candidate_auto_approved",
                        )
                        if decision and not decision.get("conflict"):
                            candidate_status = "approved"
                            if not decision.get("duplicate"):
                                auto_approved_count += 1
                    candidates.append({
                        'id': candidate_id,
                        'movie_id': movie_id,
                        'title': movie_name,
                        'progress': progress,
                        'reason': reason,
                        'status': candidate_status,
                    })
            
            if candidates:
                logger.info("发现 %s 部影片符合清理候选条件:", len(candidates))
                for item in candidates:
                    logger.info("  - %s (进度: %s%%)", movie_label_for_log(item['title']), item['progress'])
            else:
                logger.info("没有符合条件的清理候选")

            return {
                "status": "completed",
                "scanned_count": len(movies),
                "candidate_count": len(candidates),
                "auto_approved_count": auto_approved_count,
                "threshold": self.progress_threshold,
                "candidates": candidates,
            }
                
        except Exception as e:
            logger.error("清理任务执行失败: %s", exception_type(e))
            return {"status": "error", "error": exception_type(e), "candidate_count": 0}

    def _extract_movie_id(self, name: str) -> str:
        """清理候选项的番号; 未命中返回空串 (不把标题原文当番号)."""
        return extract_number(name or "") or ""
    
    def _get_library_movies(self) -> List[Dict]:
        """获取媒体库中的所有影片"""
        try:
            base_url = self.jellyfin_url.rstrip('/')
            headers = {"X-Emby-Token": self.jellyfin_api_key}
            
            # 获取媒体库列表
            response = requests.get(
                f"{base_url}/emby/Library/VirtualFolders",
                headers=headers,
                timeout=30
            )
            
            if response.status_code != 200:
                logger.error("获取媒体库失败: %s", response.status_code)
                return []
            
            libraries = response.json()
            
            # 找到"黄片"媒体库
            yellow_library = None
            for lib in libraries:
                if lib.get('Name') == self.library_name or '黄片' in str(lib.get('Name', '')):
                    yellow_library = lib
                    break
            
            if not yellow_library:
                logger.warning("未找到媒体库: %s", movie_label_for_log(self.library_name))
                return []
            
            # 获取该库的所有影片
            library_id = yellow_library.get('ItemId') or yellow_library.get('LibraryNameId')
            
            # 查询影片列表
            query_params = {
                "ParentId": library_id,
                "IncludeItemTypes": "Movie",
                "Recursive": "true",
                "Fields": "Path,UserData"
            }
            
            response = requests.get(
                f"{base_url}/emby/Items",
                headers=headers,
                params=query_params,
                timeout=60
            )
            
            if response.status_code == 200:
                data = response.json()
                return data.get('Items', [])
            
            return []
            
        except Exception as e:
            logger.error("获取影片列表失败: %s", exception_type(e))
            return []
    
    def _get_playback_progress(self, movie_id: str) -> int:
        """获取影片播放进度百分比"""
        try:
            base_url = self.jellyfin_url.rstrip('/')
            headers = {"X-Emby-Token": self.jellyfin_api_key}
            
            response = requests.get(
                f"{base_url}/emby/Users/{self._get_user_id()}/Items/{movie_id}",
                headers=headers,
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                user_data = data.get('UserData', {})
                played = user_data.get('Played', False)
                playback_position = user_data.get('PlaybackPositionTicks', 0)
                runtime_ticks = data.get('RunTimeTicks', 1)
                
                if played:
                    return 100
                
                if runtime_ticks > 0:
                    progress = int((playback_position / runtime_ticks) * 100)
                    return min(progress, 99)
            
            return 0
            
        except Exception as e:
            logger.debug("获取播放进度失败: %s", exception_type(e))
            return 0
    
    def _get_user_id(self) -> str:
        """获取 Jellyfin 用户 ID"""
        try:
            base_url = self.jellyfin_url.rstrip('/')
            headers = {"X-Emby-Token": self.jellyfin_api_key}
            
            response = requests.get(
                f"{base_url}/emby/Users",
                headers=headers,
                timeout=10
            )
            
            if response.status_code == 200:
                users = response.json()
                if users:
                    return users[0].get('Id', '')
            
            return ""
            
        except Exception as e:
            logger.error("获取用户ID失败: %s", exception_type(e))
            return ""
    
    def _is_favorite(self, movie_id: str) -> bool:
        """检查影片是否被收藏"""
        try:
            base_url = self.jellyfin_url.rstrip('/')
            headers = {"X-Emby-Token": self.jellyfin_api_key}
            
            response = requests.get(
                f"{base_url}/emby/Users/{self._get_user_id()}/Items/{movie_id}",
                headers=headers,
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                user_data = data.get('UserData', {})
                return user_data.get('IsFavorite', False)
            
            return False
            
        except Exception as e:
            logger.debug("检查收藏状态失败: %s", exception_type(e))
            return False
    
    def _delete_movie(self, movie: Dict):
        """兼容旧调用；清理扫描不得执行删除。"""
        movie_label = movie_label_for_log(movie.get('name') or movie.get('Name') or movie.get('id') or movie.get('Id') or "unknown")
        logger.warning("旧清理任务删除入口已禁用，候选必须经独立审批和删除流程处理: %s", movie_label)
        return False
    
    def _refresh_library(self):
        """刷新媒体库"""
        try:
            base_url = self.jellyfin_url.rstrip('/')
            headers = {"X-Emby-Token": self.jellyfin_api_key}
            
            requests.post(
                f"{base_url}/emby/Library/Refresh",
                headers=headers,
                timeout=30
            )
            logger.info("Jellyfin 媒体库刷新已触发")
            
        except Exception as e:
            logger.error("触发刷新失败: %s", exception_type(e))
    
    def run_now(self) -> Dict:
        """手动执行一次安全的清理候选扫描。"""
        self.reload_config()
        result = self._run_cleanup()
        self.last_run_time = datetime.now()
        result["last_run"] = self.last_run_time.isoformat()
        return result


jellyfin_cleanup = JellyfinCleanupTask()
