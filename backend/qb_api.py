"""
qBittorrent API 封装模块

提供登录、种子管理、文件优先级设置、做种限制、标签管理等功能。
所有函数均返回统一类型，便于调用方处理。
"""
import requests
import logging
import re
from typing import List, Dict, Optional, Union
from typing_extensions import TypedDict

from net_policy import internal_proxy_enabled

logger = logging.getLogger(__name__)


def redact_qb_response_text(value: str) -> str:
    text = str(value or "")
    text = re.sub(
        r"(?i)(api[_-]?key|token|secret|password|passwd|pwd|authorization)\s*[:=]\s*[^,\s]+",
        r"\1=***",
        text,
    )
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 ***", text)
    return text[:120]


def exception_type(exc: Exception) -> str:
    return exc.__class__.__name__


def _dict_value(value) -> Dict:
    return value if isinstance(value, dict) else {}


def _dict_items(value) -> List[Dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _join_values(value, separator: str) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple, set)):
        return separator.join(
            text
            for text in (str(item).strip() for item in value if item is not None)
            if text
        )
    if value is None:
        return ""
    return str(value).strip()


def _has_target(value: str) -> bool:
    return bool(str(value or "").strip())


class TorrentInfo(TypedDict, total=False):
    """种子信息类型"""
    hash: str
    name: str
    state: str
    tags: str
    total_size: int
    progress: float
    category: str


class TorrentFile(TypedDict, total=False):
    """种子文件信息类型"""
    index: int
    """种子信息类型"""
    hash: str
    name: str
    state: str
    tags: str
    total_size: int
    progress: float
    category: str


class TorrentFile(TypedDict, total=False):
    """种子文件信息类型"""
    index: int
    name: str
    size: int
    priority: int


class ServerState(TypedDict, total=False):
    """服务器状态类型"""
    free_space_on_disk: int
    dl_info_speed: int
    up_info_speed: int
    alltime_dl: int
    alltime_ul: int


def login(base_url: str, username: str, password: str, timeout: int = 5, verify: bool = True) -> Optional[requests.Session]:
    """
    登录 qBittorrent 并返回 Session 对象
    """
    if not verify:
        requests.packages.urllib3.disable_warnings(  # type: ignore[attr-defined]
            requests.packages.urllib3.exceptions.InsecureRequestWarning  # type: ignore[attr-defined]
        )
        if not getattr(login, "_ssl_warning_logged", False):
            logger.warning("QB SSL verification is disabled by configuration.")
            login._ssl_warning_logged = True

    session = requests.Session()
    session.verify = verify
    # qBittorrent 是内网服务：本机配了系统代理时不能让它走代理，否则本机/NAS 地址会被代理劫持
    session.trust_env = internal_proxy_enabled()
    # 自动处理 base_url 末尾的 /
    base_url = base_url.rstrip("/")
    try:
        resp = session.post(
            f"{base_url}/api/v2/auth/login",
            data={'username': username, 'password': password},
            timeout=timeout
        )
        if resp.status_code == 200 and "Ok." in resp.text:
            return session
            
        # [FIX] Strict check: If text is 'Fails.', it's a failure even if status is 200
        if "Fails." in resp.text:
            logger.warning("QB 登录被拒绝: %s", redact_qb_response_text(resp.text))
            return None

        # Fallback for other versions that might return 200 but different text
        if resp.ok and len(resp.text) < 100: # Simple heuristic
             # Maybe check for SID cookie?
             if 'SID' in session.cookies:
                 return session
        
        logger.warning("QB 登录响应未确认: Status=%s, Text=%s", resp.status_code, redact_qb_response_text(resp.text))
        return None
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError) as e:
        logger.error("连接 QB 失败 (网络/超时): %s", exception_type(e))
        return None
    except Exception as e:
        logger.error("连接 QB 失败: %s", exception_type(e))
        return None

def get_app_version(session: requests.Session, base_url: str) -> Optional[str]:
    """获取 QB 版本号，用于测试连接"""
    try:
        resp = session.get(f"{base_url}/api/v2/app/version", timeout=5)
        if resp.status_code in (401, 403):
            logger.warning("QB 会话失效 (status=%s)", resp.status_code)
            return None
        if resp.ok:
            version = resp.text.strip()
            return version[1:] if version.lower().startswith("v") else version
        return None
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError):
        raise
    except Exception:
        return None

def get_torrents(session: requests.Session, base_url: str, category: Optional[str] = None, tag: Optional[str] = None) -> List[TorrentInfo]:
    """获取种子列表"""
    params = {'filter': 'all'}
    if category:
        params['category'] = category
    if tag:
        params['tag'] = tag
        
    try:
        resp = session.get(f"{base_url}/api/v2/torrents/info", params=params, timeout=5)
        if resp.ok:
            return _dict_items(resp.json())
        return []
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError):
        raise
    except Exception as e:
        logger.error("获取种子列表失败: %s", exception_type(e))
        return []

def add_magnet(session: requests.Session, base_url: str, magnet: str, save_path: Optional[str] = None, category: Optional[str] = None, tags: Optional[List[str]] = None) -> bool:
    """添加磁力链接"""
    data = {'urls': magnet}
    if save_path:
        data['savepath'] = save_path
    if category:
        data['category'] = category
    if tags:
        tags_text = _join_values(tags, ',')
        if tags_text:
            data['tags'] = tags_text
        
    try:
        resp = session.post(f"{base_url}/api/v2/torrents/add", data=data, timeout=5)
        return resp.ok or "Ok" in resp.text
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError):
        raise
    except Exception as e:
        logger.error("添加磁力失败: %s", exception_type(e))
        return False

def get_torrent_files(session: requests.Session, base_url: str, torrent_hash: str) -> List[TorrentFile]:
    """获取种子文件列表"""
    try:
        resp = session.get(f"{base_url}/api/v2/torrents/files", params={'hash': torrent_hash}, timeout=5)
        if resp.ok:
            return _dict_items(resp.json())
        return []
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError):
        raise
    except Exception as e:
        logger.error("获取文件列表失败: %s", exception_type(e))
        return []

def set_file_priority(session: requests.Session, base_url: str, torrent_hash: str, file_ids: List[int], priority: int) -> bool:
    """设置文件优先级 (0: 不下载, 1: 正常, 6/7: 高/最高)"""
    ids_str = _join_values(file_ids, '|')
    if not _has_target(torrent_hash) or not _has_target(ids_str):
        return False
    try:
        resp = session.post(
            f"{base_url}/api/v2/torrents/filePrio", 
            data={'hash': torrent_hash, 'id': ids_str, 'priority': priority},
            timeout=5
        )
        return resp.ok
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError):
        raise
    except Exception as e:
        logger.error("设置文件优先级失败: %s", exception_type(e))
        return False

def set_share_limits(session: requests.Session, base_url: str, hashes: List[str], ratio_limit: float = -2, seeding_time_limit: int = -2) -> bool:
    """设置做种限制. -2 表示全局默认, -1 无限制"""
    hashes_str = _join_values(hashes, '|')
    if not _has_target(hashes_str):
        return False
    data = {
        'hashes': hashes_str,
        'ratioLimit': ratio_limit,
        'seedingTimeLimit': seeding_time_limit
    }
    try:
        resp = session.post(f"{base_url}/api/v2/torrents/setShareLimits", data=data, timeout=5)
        return resp.ok
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError):
        raise
    except Exception as e:
        logger.error("设置做种限制失败: %s", exception_type(e))
        return False

def add_tags(session: requests.Session, base_url: str, hashes: List[str], tags: str) -> bool:
    """添加标签"""
    hashes_str = _join_values(hashes, '|')
    if not _has_target(hashes_str) or not _has_target(tags):
        return False
    data = {'hashes': hashes_str, 'tags': tags}
    try:
        resp = session.post(f"{base_url}/api/v2/torrents/addTags", data=data, timeout=5)
        return resp.ok
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError):
        raise
    except Exception as e:
        logger.error("添加标签失败: %s", exception_type(e))
        return False

def remove_tags(session: requests.Session, base_url: str, hashes: List[str], tags: str) -> bool:
    """移除标签"""
    hashes_str = _join_values(hashes, '|')
    if not _has_target(hashes_str) or not _has_target(tags):
        return False
    data = {'hashes': hashes_str, 'tags': tags}
    try:
        resp = session.post(f"{base_url}/api/v2/torrents/removeTags", data=data, timeout=5)
        return resp.ok
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError):
        raise
    except Exception as e:
        logger.error("移除标签失败: %s", exception_type(e))
        return False

def get_server_state(session: requests.Session, base_url: str) -> ServerState:
    """获取服务器状态 (含磁盘空间)"""
    try:
        resp = session.get(f"{base_url}/api/v2/sync/maindata", timeout=5)
        if resp.ok:
            data = _dict_value(resp.json())
            return _dict_value(data.get('server_state'))
        return {}
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError):
        raise
    except Exception as e:
        logger.error("获取服务器状态失败: %s", exception_type(e))
        return {}
    
def pause_torrents(session: requests.Session, base_url: str, hashes: Union[str, List[str]] = "all") -> bool:
    """暂停任务"""
    hashes_str = _join_values(hashes, '|')
    if not _has_target(hashes_str):
        return False
    data = {'hashes': hashes_str}
    try:
        resp = session.post(f"{base_url}/api/v2/torrents/pause", data=data, timeout=5)
        return resp.ok
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError):
        raise
    except Exception as e:
        logger.error("暂停任务失败: %s", exception_type(e))
        return False

def resume_torrents(session: requests.Session, base_url: str, hashes: Union[str, List[str]] = "all") -> bool:
    """恢复任务"""
    hashes_str = _join_values(hashes, '|')
    if not _has_target(hashes_str):
        return False
    data = {'hashes': hashes_str}
    try:
        resp = session.post(f"{base_url}/api/v2/torrents/resume", data=data, timeout=5)
        return resp.ok
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError):
        raise
    except Exception as e:
        logger.error("恢复任务失败: %s", exception_type(e))
        return False

def set_torrent_location(session: requests.Session, base_url: str, hashes: List[str], location: str) -> bool:
    """设置下载位置 (移动文件)"""
    hashes_str = _join_values(hashes, '|')
    if not _has_target(hashes_str) or not _has_target(location):
        return False
    data = {'hashes': hashes_str, 'location': location}
    try:
        resp = session.post(f"{base_url}/api/v2/torrents/setLocation", data=data, timeout=5)
        return resp.ok
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError):
        raise
    except Exception as e:
        logger.error("设置下载位置失败: %s", exception_type(e))
        return False

def delete_torrents(session: requests.Session, base_url: str, hashes: List[str], delete_files: bool = False) -> bool:
    """删除任务"""
    hashes_str = _join_values(hashes, '|')
    if not _has_target(hashes_str):
        return False
    data = {'hashes': hashes_str, 'deleteFiles': 'true' if delete_files else 'false'}
    try:
        resp = session.post(f"{base_url}/api/v2/torrents/delete", data=data, timeout=5)
        return resp.ok
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError):
        raise
    except Exception as e:
        logger.error("删除任务失败: %s", exception_type(e))
        return False

def get_transfer_info(session: requests.Session, base_url: str) -> Dict:
    """获取全局传输信息（下载/上传速度、累计量等）"""
    try:
        resp = session.get(f"{base_url}/api/v2/transfer/info", timeout=5)
        if resp.ok:
            return _dict_value(resp.json())
        return {}
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError):
        raise
    except Exception as e:
        logger.error("获取传输信息失败: %s", exception_type(e))
        return {}

def get_main_data(session: requests.Session, base_url: str) -> Dict:
    """获取主数据（包含server_state和full_update标志）"""
    try:
        resp = session.get(f"{base_url}/api/v2/sync/maindata", params={'rid': 0}, timeout=5)
        if resp.ok:
            return _dict_value(resp.json())
        return {}
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError):
        raise
    except Exception as e:
        logger.error("获取主数据失败: %s", exception_type(e))
        return {}
