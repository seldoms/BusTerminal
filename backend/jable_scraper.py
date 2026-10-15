import re
import requests
import logging
import time
import threading
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
from database import db, display_value_for_log
from scrapling_adapter import fetch_html_with_scrapling
from movie_normalization import normalize_movie_list

logging.getLogger("bs4.dammit").setLevel(logging.ERROR)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def exception_type(exc: Exception) -> str:
    return type(exc).__name__


# Jable.tv Headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

class JableScraper:
    """Jable.tv 视频抓取器"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.base_url = "https://jable.tv"
        self.proxies = None
        self._refresh_lock = threading.Lock()
        self._refreshing_keys = set()

    def _schedule_background_refresh(
        self,
        *,
        page: int,
        limit: int,
        sort_by: str,
        translate: bool,
        cache_key: str,
    ) -> None:
        with self._refresh_lock:
            if cache_key in self._refreshing_keys:
                return
            self._refreshing_keys.add(cache_key)

        def refresh():
            try:
                self.get_hot_movies(
                    page=page,
                    limit=limit,
                    sort_by=sort_by,
                    force_refresh=True,
                    translate=translate,
                )
            except Exception as exc:
                logger.warning(
                    "Jable 后台刷新失败 [%s]: %s",
                    display_value_for_log(cache_key),
                    exception_type(exc),
                )
            finally:
                with self._refresh_lock:
                    self._refreshing_keys.discard(cache_key)

        threading.Thread(
            target=refresh,
            name=f"jable_refresh_{page}_{sort_by}",
            daemon=True,
        ).start()
        
    def reload_config(self):
        """从数据库加载代理配置"""
        import config
        proxy_str = db.get_config('system_proxy') or config.SYSTEM_PROXY
        if proxy_str and proxy_str.strip():
            self.proxies = {
                "http": proxy_str,
                "https": proxy_str
            }
            self.session.proxies.update(self.proxies)
            logger.info("Jable Scraper: proxy enabled %s", display_value_for_log(proxy_str))
        else:
            self.proxies = {}
            self.session.proxies.clear()

    def parse_html(self, response):
        """
        解析 HTML 响应，自动处理编码问题，避免 bs4 警告
        """
        try:
            content = response.content
            encoding = response.encoding or 'utf-8'
            
            if response.apparent_encoding and response.apparent_encoding != encoding:
                try:
                    content = response.content.decode(response.apparent_encoding)
                except (UnicodeDecodeError, LookupError):
                    pass
            
            return BeautifulSoup(content, 'html.parser')
        except Exception as e:
            logger.warning("Jable HTML 解析编码异常: %s", exception_type(e))
            return BeautifulSoup(response.content, 'html.parser')
    
    def get_hot_movies(self, page: int = 1, limit: int = 30, sort_by: str = "video_viewed", force_refresh: bool = False, translate: bool = False) -> List[Dict]:
        """
        获取热门影片列表
        
        Args:
            page: 页码
            limit: 每页数量
            sort_by: 排序方式
                - video_viewed: 所有时间观看数（默认）
                - video_viewed_month: 本月热门
                - video_viewed_week: 本周热门
                - video_viewed_today: 今日热门
                - post_date: 发布日期
            force_refresh: 是否强制刷新缓存
            translate: 是否翻译标题
            
        Returns:
            影片列表
        """
        sort_by = {
            "daily": "video_viewed",
            "weekly": "video_viewed_week",
            "monthly": "video_viewed_month",
        }.get(sort_by, sort_by)
        cache_key = f"jable_hot_{sort_by}_p{page}_l{limit}"
        if translate:
            cache_key += "_trans"

        def normalize_cached_titles(items):
            if not translate:
                return normalize_movie_list(items)
            try:
                from scraper import normalize_simplified_title
            except Exception:
                return normalize_movie_list(items)
            normalized = normalize_movie_list(items)
            for item in normalized:
                item["title"] = normalize_simplified_title(item.get("title"), item.get("title"))
            return normalized
        
        # 缓存策略
        cache_full = None
        if not force_refresh:
            cache_full = db.get_cache_full(cache_key)
            if cache_full:
                cached_data = cache_full["value"]
                updated_at = cache_full["updated_at"]
                now = time.time()
                
                # 4小时内直接返回缓存
                if now - updated_at < 14400:
                    logger.debug("[Jable Cache Hit] 返回 %s 缓存数据", display_value_for_log(cache_key))
                    return normalize_cached_titles(cached_data)

                # 过期缓存也先返回，后台刷新，避免页面请求同步等待多个备用站点。
                if not force_refresh:
                    self._schedule_background_refresh(
                        page=page,
                        limit=limit,
                        sort_by=sort_by,
                        translate=translate,
                        cache_key=cache_key,
                    )
                    logger.info(
                        "[Jable Cache Stale] 返回旧缓存并后台刷新: %s",
                        display_value_for_log(cache_key),
                    )
                    return normalize_cached_titles(cached_data)
        
        try:
            # 构建 API URL
            timestamp = int(time.time() * 1000)
            api_url = f"{self.base_url}/hot/?mode=async&function=get_block&block_id=list_videos_common_videos_list&sort_by={sort_by}&from={page}&_={timestamp}"
            
            logger.info("正在抓取 Jable 热门影片: page=%s, sort_by=%s, translate=%s", page, sort_by, translate)
            
            response = self.session.get(api_url, timeout=15)
            response.raise_for_status()
            
            # 解析 HTML
            soup = self.parse_html(response)
            
            movies = []
            video_items = soup.find_all('div', class_='video-img-box')
            
            logger.info("找到 %s 个视频项", len(video_items))
            
            for item in video_items[:limit]:
                try:
                    movie = self._parse_video_item(item)
                    if movie:
                        movies.append(movie)
                except Exception as e:
                    logger.error("解析视频项失败: %s", exception_type(e))
                    continue
            
            # 如果需要翻译且有影片
            movies = normalize_movie_list(movies)
            if translate and movies:
                try:
                    from scraper import scraper as javbus_scraper
                    titles = [m['title'] for m in movies]
                    # 使用现有的批量翻译功能
                    translated_titles = javbus_scraper.batch_translate_to_chinese(titles)
                    
                    for i, movie in enumerate(movies):
                        if i < len(translated_titles):
                            movie['title_original'] = movie['title']
                            movie['title'] = javbus_scraper.normalize_simplified_title(
                                translated_titles[i], movie['title']
                            )
                except Exception as e:
                    logger.error("翻译 Jable 标题时出错: %s", exception_type(e))
            
            # 存入缓存
            if movies:
                db.set_cache(cache_key, movies)
                logger.info("已缓存 %s 个 Jable 视频 (Key: %s)", len(movies), display_value_for_log(cache_key))
            
            return movies
            
        except Exception as e:
            logger.warning("抓取 Jable 热门影片失败: %s", exception_type(e))
            fallback_api_url = f"https://fs1.app/hot/?mode=async&function=get_block&block_id=list_videos_common_videos_list&sort_by={sort_by}&from={page}&_={int(time.time() * 1000)}"
            try:
                response = self.session.get(fallback_api_url, timeout=15)
                response.raise_for_status()
                soup = self.parse_html(response)
                movies = []
                for item in soup.find_all('div', class_='video-img-box')[:limit]:
                    movie = self._parse_video_item(item)
                    if movie:
                        movie['detail_url'] = movie.get('detail_url', '').replace(self.base_url, "https://fs1.app")
                        movie['cover'] = movie.get('cover', '').replace(self.base_url, "https://fs1.app")
                        movies.append(movie)
                movies = normalize_movie_list(movies)
                if movies:
                    db.set_cache(cache_key, movies)
                    return normalize_cached_titles(movies)
            except Exception as fallback_error:
                logger.warning("抓取 Jable 备用源失败: %s", exception_type(fallback_error))

            content, status, _ = fetch_html_with_scrapling(fallback_api_url, timeout=15, proxies=self.proxies)
            if not content or status != 200:
                if cache_full:
                    return normalize_cached_titles(cache_full["value"])
                return []
            soup = BeautifulSoup(content, 'html.parser')
            movies = []
            for item in soup.find_all('div', class_='video-img-box')[:limit]:
                movie = self._parse_video_item(item)
                if movie:
                    movie['detail_url'] = movie.get('detail_url', '').replace(self.base_url, "https://fs1.app")
                    movie['cover'] = movie.get('cover', '').replace(self.base_url, "https://fs1.app")
                    movies.append(movie)
            movies = normalize_movie_list(movies)
            if movies:
                db.set_cache(cache_key, movies)
            return normalize_cached_titles(movies)
    
    def _parse_video_item(self, item) -> Optional[Dict]:
        """解析单个视频项"""
        try:
            # 查找父容器（包含 detail 信息）
            parent = item.parent
            if not parent:
                return None
            
            # 提取链接（在 img-box 内的 a 标签）
            link = item.find('a')
            if not link:
                return None
            
            video_url = link.get('href', '')
            
            # 提取视频 ID (从 URL 中提取)
            video_id = ''
            if video_url:
                match = re.search(r'/videos/([^/]+)/?', video_url)
                if match:
                    video_id = match.group(1).upper()
            
            # 提取封面
            cover = ''
            img = link.find('img')
            if img:
                cover = img.get('data-src') or img.get('src', '')
                if cover and not cover.startswith('http'):
                    cover = self.base_url + cover
            
            # 提取时长（在 absolute-bottom-right 内）
            duration = ''
            duration_elem = item.find('div', class_='absolute-bottom-right')
            if duration_elem:
                label = duration_elem.find('span', class_='label')
                if label:
                    duration = label.get_text(strip=True)
            
            # 提取标题（在 detail > h6.title > a 中）
            title = ''
            detail_div = parent.find('div', class_='detail')
            if detail_div:
                title_h6 = detail_div.find('h6', class_='title')
                if title_h6:
                    title_link = title_h6.find('a')
                    if title_link:
                        title = title_link.get_text(strip=True)
            
            # 提取观看数（在 detail > p.sub-title 中的第一个数字）
            views = 0
            if detail_div:
                sub_title = detail_div.find('p', class_='sub-title')
                if sub_title:
                    # 获取文本并提取第一个数字
                    text = sub_title.get_text(strip=True)
                    # 移除空格，提取第一个数字
                    numbers = re.findall(r'[\d\s]+', text)
                    if numbers:
                        views_text = numbers[0].replace(' ', '').replace(',', '')
                        try:
                            views = int(views_text)
                        except:
                            pass
            
            # 完整 URL
            if video_url and not video_url.startswith('http'):
                video_url = self.base_url + video_url
            
            return {
                'id': video_id,
                'title': title,
                'cover': cover,
                'detail_url': video_url,
                'duration': duration,
                'views': views,
                'source': 'jable'
            }
            
        except Exception as e:
            logger.error("解析视频项时出错: %s", exception_type(e))
            return None
    
    def get_movie_detail(self, url: str) -> Dict:
        """
        获取影片详情
        
        Args:
            url: 影片详情页 URL
            
        Returns:
            影片详情字典
        """
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            
            soup = self.parse_html(response)
            
            detail = {
                'url': url,
                'description': '',
                'tags': [],
                'actresses': [],
            }
            
            # 提取描述
            desc_elem = soup.find('div', class_='video-description')
            if desc_elem:
                detail['description'] = desc_elem.get_text(strip=True)
            
            # 提取标签
            tag_elems = soup.find_all('a', class_='tag')
            for tag in tag_elems:
                tag_text = tag.get_text(strip=True)
                if tag_text:
                    detail['tags'].append(tag_text)
            
            # 提取演员
            actress_elems = soup.find_all('a', class_='model')
            for actress in actress_elems:
                actress_name = actress.get_text(strip=True)
                if actress_name:
                    detail['actresses'].append(actress_name)
            
            return detail
            
        except Exception as e:
            logger.error("获取 Jable 影片详情失败: %s", exception_type(e))
            return {}

# 创建全局实例
jable_scraper = JableScraper()
