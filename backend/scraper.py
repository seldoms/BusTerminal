import re
import requests
import hashlib
from bs4 import BeautifulSoup
from typing import Any, List, Dict, Optional
from deep_translator import GoogleTranslator
import logging

import logging
import time
import random
import threading
from collections import deque
from datetime import datetime, timedelta
from urllib.parse import urlparse
from database import db, is_bad_translation_text, strip_translation_control_tokens
import config # Import config module
from domain_router import DomainRouter
from ollama_translator import OllamaTranslator
from translation_guard import TitleGuard
import wikipediaapi
from jellyfin_api import jellyfin_client
try:
    from opencc import OpenCC
    _TRADITIONAL_TO_SIMPLIFIED = OpenCC('t2s')
except Exception:
    _TRADITIONAL_TO_SIMPLIFIED = None

# 禁用 SSL 警告，避免日志混乱（HTTPS 请求使用 verify=False 时会产生警告）
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 禁用 bs4.dammit 编码警告，避免日志混乱
logging.getLogger("bs4.dammit").setLevel(logging.ERROR)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 日文假名：标题里出现它，就说明这条还是原文、需要翻译
JAPANESE_KANA_PATTERN = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")


def _ollama_batch_size(db, config_module):
    """翻译批量大小：优先读 DB 配置 `ollama_batch_size`，回落环境变量默认值。

    需要能调成 1：Hunyuan-MT / Hy-MT 这类专用翻译模型不认换行批量，一次给多条
    它会当成一整条理解（实测 hunyuan-mt-7b：6 条进、1 行出，还反问「请提供要翻译的文本」）。
    逐条并不慢 —— 7B 单条约 0.31~0.37s，103 万条约 4 天。
    """
    try:
        raw = db.get_config('ollama_batch_size')
    except Exception:  # noqa: BLE001
        raw = None
    if raw is not None and str(raw).strip():
        try:
            return max(1, min(int(str(raw).strip()), 30))
        except (TypeError, ValueError):
            pass
    return config_module.OLLAMA_BATCH_SIZE


def normalize_simplified_title(value: str, fallback: str = "") -> str:
    """Normalize translated titles and reject upstream error pages returned as text."""
    text = strip_translation_control_tokens(value)
    if is_bad_translation_text(text):
        text = str(fallback or "").strip()
    if _TRADITIONAL_TO_SIMPLIFIED and text:
        try:
            text = _TRADITIONAL_TO_SIMPLIFIED.convert(text)
        except Exception:
            pass
    return text


def sanitize_movie_title_for_output(movie: Dict) -> Dict:
    """Avoid returning polluted upstream error pages as movie titles."""
    if not isinstance(movie, dict):
        return movie

    safe_title = normalize_simplified_title(movie.get("title"), "")
    if not safe_title:
        safe_title = str(movie.get("original_title") or movie.get("id") or "").strip()

    if safe_title and safe_title != movie.get("title"):
        movie = dict(movie)
        movie["title"] = safe_title
    return movie


def sanitize_movie_list_titles(movies: List[Dict]) -> List[Dict]:
    if not isinstance(movies, list):
        return movies
    return [sanitize_movie_title_for_output(movie) for movie in movies]


def is_suspicious_actress_name(name: str) -> bool:
    if not name:
        return False

    normalized = name.strip()
    if len(normalized) > 40:
        return True

    if re.search(r'\b[A-Z]{2,6}-?\d{2,5}\b', normalized, re.IGNORECASE):
        return True

    if re.search(r'\d{4}-\d{2}-\d{2}', normalized):
        return True

    separators = sum(normalized.count(ch) for ch in [':', '|', '/', '\\'])
    return separators >= 2

# BASE_URL removed, using dynamic self.base_url
# Full Chrome-like headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}



class BaiduTranslator:
    def __init__(self, app_id, secret_key):
        self.app_id = app_id
        self.secret_key = secret_key
        self.api_url = "http://api.fanyi.baidu.com/api/trans/vip/translate"

    def translate_text(self, text, from_lang="auto", to_lang="zh"):
        if not text:
            return ""
        # 随机盐值
        salt = str(random.randint(32768, 65536))
        # 签名生成
        sign_str = self.app_id + text + salt + self.secret_key
        sign = hashlib.md5(sign_str.encode()).hexdigest()
        
        params = {
            "q": text,
            "from": from_lang,
            "to": to_lang,
            "appid": self.app_id,
            "salt": salt,
            "sign": sign
        }
        
        try:
            resp = requests.get(self.api_url, params=params, timeout=5)
            result = resp.json()
            if "trans_result" in result:
                return "\n".join([item["dst"] for item in result["trans_result"]])
            else:
                # 返回错误详情供上层处理
                return {"error": result.get("error_code", "unknown"), "msg": result.get("error_msg", "")}
        except Exception as e:
            logger.error(f"Baidu Request Failed: {e}")
            return {"error": "request_failed", "msg": str(e)}


# Default anti-censorship domains
DOMAINS = [
    "https://www.javbus.com",
    "https://www.busdmm.cyou",
    "https://www.busfan.cyou",
    "https://www.javbus.cyou",
    "https://www.seedmm.cyou",
    "https://www.seejav.bond"
]

class JavBusScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.all_domains = list(DOMAINS)
        
        # Merge extra domains from env
        extra = config.get_extra_domains()
        if extra:
            for d in extra:
                if d not in self.all_domains:
                     self.all_domains.append(d)
            logger.info(f"Loaded {len(extra)} extra domains from environment: {extra}")
        self.domain_router = DomainRouter(
            db=db,
            source_key="javbus",
            default_domains=self.all_domains,
            logger=logger,
        )
            
        self.base_url = self.all_domains[0] # Default
        self.proxies = None
        self._translation_lock = threading.Lock()
        self._google_disabled_until = 0.0
        self._google_cooldown_log_at = 0.0
        # 翻译队列：一个片子一个请求，页面打开时插队到队首，历史回填排到队尾
        self._translation_queue = deque()
        self._translation_queue_lock = threading.Lock()
        self._translation_queue_event = threading.Event()
        self._translation_worker: Optional[threading.Thread] = None
        # 已入队标题的去重表（10 分钟窗口）
        self._queued_titles: Dict[str, float] = {}
        
        # Initialize Wikipedia API (Japanese and English)
        self.wiki_ja = wikipediaapi.Wikipedia(user_agent='BusTerminal/1.0 (https://github.com)', language='ja')
        self.wiki_en = wikipediaapi.Wikipedia(user_agent='BusTerminal/1.0 (https://github.com)', language='en')
        
        # Load initial config
        self.reload_config()
        
    def reload_config(self):

        """Reload configuration from database for Proxy and Domains"""
        # 1. Load Proxy
        proxy_str = db.get_config('system_proxy') or config.SYSTEM_PROXY
        if proxy_str and proxy_str.strip():
            self.proxies = {
                "http": proxy_str,
                "https": proxy_str
            }
            self.session.proxies.update(self.proxies)
            logger.info(f"System Proxy set to: {proxy_str}")
        else:
            self.proxies = {}
            self.session.proxies.clear()
            logger.info("System Proxy disabled")
            
        # 2. Check Domain Strategy
        enable_rotation = str(db.get_config('enable_domain_rotation')).lower() != 'false' # Default True
        fixed_domain = db.get_config('fixed_domain')
        
        if enable_rotation:
             # Try to detect new mirrors from current working domain before checking all
             self.detect_official_mirrors()
             logger.info("Domain Rotation ENABLED. Finding best domain...")
             self.base_url = self.find_best_domain()
        else:
             if fixed_domain and fixed_domain.startswith('http'):
                 self.base_url = fixed_domain.rstrip('/')
                 logger.info(f"Domain Rotation DISABLED. Using fixed domain: {self.base_url}")
             else:
                 self.base_url = self.all_domains[0]
                 logger.warning("Domain Rotation DISABLED but no valid fixed domain set. Using default.")

        # 3. Reload Translator (Existing logic)
        self._reload_translator_logic()
        
        # 4. Set Cookies for current domain
        self._set_cookies()

    def _set_cookies(self):
        cookie_names = ["existmag", "age_verified"]
        # Extract domain from base_url for cookie setting
        from urllib.parse import urlparse
        try:
            domain = urlparse(self.base_url).netloc
            # Also set for root domain if possible, but mainly for current host
            self.session.cookies.set("existmag", "all", domain=domain, path="/")
            self.session.cookies.set("age_verified", "mag", domain=domain, path="/")
        except:
            pass

    def find_best_domain(self):
        """Iterate through domains to find the FASTEST available one"""
        candidates = self.domain_router.candidates()
        best_domain = candidates[0] if candidates else self.all_domains[0]
        best_time = float('inf')
        
        for domain in candidates:
            try:
                start = time.time()
                # Use HEAD request for speed, apply proxy if set
                resp = self.session.head(domain, proxies=self.proxies, timeout=5)
                if resp.status_code < 400:
                    elapsed = time.time() - start
                    self.domain_router.record_success(domain, elapsed * 1000)
                    logger.info(f"Domain {domain} is reachable ({elapsed:.2f}s)")
                    if elapsed < best_time:
                        best_time = elapsed
                        best_domain = domain
                        # Optimization: If very fast (<1s), just pick it
                        if elapsed < 1.0: 
                            break
            except Exception as e:
                self.domain_router.record_failure(domain, type(e).__name__)
                logger.warning(f"Domain {domain} failed: {type(e).__name__}")
                
        logger.info(f"Selected Best Domain: {best_domain}")
        return best_domain

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
                    encoding = response.apparent_encoding
                except (UnicodeDecodeError, LookupError):
                    pass
            
            return BeautifulSoup(content, 'html.parser')
        except Exception as e:
            logger.warning(f"HTML 解析编码异常: {e}")
            return BeautifulSoup(response.content, 'html.parser', from_encoding='utf-8')

    def detect_official_mirrors(self):
        """
        尝试从当前可用域名的首页解析官方发布的“防屏蔽地址”
        并将其加入到待选列表
        """
        try:
            # 尝试访问当前 base_url 或 默认域名获取首页
            target_url = self.base_url if self.base_url.startswith('http') else self.all_domains[0]
            logger.info(f"Checking for official mirrors on {target_url}...")
            
            resp = self.session.get(target_url, timeout=10, proxies=self.proxies)
            if resp.status_code != 200:
                logger.warning(f"Failed to fetch homepage for mirror check: {resp.status_code}")
                return

            soup = self.parse_html(resp)
            new_domains = []
            
            # 策略1：根据用户提供的路径查找 (/html/body/div[4]/div/div[2])
            # body 下的 div[4] -> index 3
            try:
                body_divs = [d for d in soup.body.contents if d.name == 'div']
                if len(body_divs) >= 4:
                    target = body_divs[3].find_all('div', recursive=False)
                    if target:
                        info_divs = target[0].find_all('div', recursive=False)
                        if len(info_divs) >= 2:
                            links = info_divs[1].find_all('a')
                            for a in links:
                                href = a.get('href')
                                if href and href.startswith('http'):
                                    new_domains.append(href.rstrip('/'))
            except Exception as e:
                logger.debug(f"Path lookup for mirrors failed: {e}")
            
            # 策略2：全局搜索包含“防屏蔽”或“永久域名”的文本块
            if not new_domains:
                keyword_tags = soup.find_all(lambda t: t.name in ['p', 'div'] and ('防屏蔽' in t.text or '永久域名' in t.text))
                for tag in keyword_tags:
                    links = tag.find_all('a')
                    for a in links:
                        href = a.get('href')
                        if href and href.startswith('http'):
                             new_domains.append(href.rstrip('/'))

            # 去重并添加
            count = 0
            for d in new_domains:
                # 简单过滤非域名链接
                if 'javbus' not in d and 'seed' not in d and 'seejav' not in d and 'bus' not in d:
                     continue
                
                if d not in self.all_domains:
                    self.all_domains.append(d)
                    count += 1

            if new_domains:
                self.domain_router.add_domains(new_domains, "official_page")
            
            if count > 0:
                logger.info(f"Detected {count} new official mirrors: {new_domains}")
            else:
                logger.info("No new mirrors detected.")
                
        except Exception as e:
            logger.warning(f"Error checking for mirrors: {e}")

    def _reload_translator_logic(self):
        # Determine provider from database config or env
        provider = db.get_config('translation_provider') or config.TRANSLATION_PROVIDER or 'google_free'
        self.provider = provider

        # 本地大模型（内网 Ollama）：翻译的首选路径，provider 选中时才启用
        self.ollama_translator = None
        if provider == 'ollama_local':
            base_url = db.get_config('ollama_base_url') or config.OLLAMA_BASE_URL
            model = db.get_config('ollama_model') or config.OLLAMA_MODEL
            if base_url and model:
                guard = None
                if getattr(config, 'TRANSLATION_GUARD_ENABLED', True):
                    try:
                        names = db.get_actress_names_for_translation_guard()
                        guard = TitleGuard(names)
                        logger.info(
                            "标题翻译占位保护已启用（演员名 %s 条）", len(names) if names else 0
                        )
                    except Exception as exc:  # noqa: BLE001
                        # 保护器取不到名字表不应让翻译整体不可用，退回无保护即可
                        logger.warning(
                            "加载演员名失败，本次翻译不带占位保护: %s",
                            type(exc).__name__,
                        )
                self.ollama_translator = OllamaTranslator(
                    base_url,
                    model,
                    timeout=config.OLLAMA_TIMEOUT,
                    # 批量大小可经 DB 覆盖：Hunyuan-MT / Hy-MT 这类**专用翻译模型不认换行批量**，
                    # 一次给多条它会当成一整条来理解，反过来问"请提供要翻译的文本"
                    # （2026-10-11 在 hunyuan-mt-7b 上实测：6 条进、1 行出）。
                    # 逐条仍不慢——7B 单条约 0.31~0.37s，103 万条约 4 天。
                    batch_size=_ollama_batch_size(db, config),
                    guard=guard,
                    strict_quality_gate=getattr(config, 'TRANSLATION_STRICT_QUALITY_GATE', False),
                )
                logger.info(f"Using local Ollama Translator ({model} @ {base_url})")
            else:
                logger.warning("本地大模型翻译缺少 OLLAMA_BASE_URL / OLLAMA_MODEL，回退其它翻译源")

        self.translator = None
        if provider == 'baidu_dev':
            app_id = db.get_config('baidu_app_id') or config.BAIDU_APP_ID
            secret = db.get_config('baidu_secret') or config.BAIDU_SECRET
            if app_id and secret:
                self.translator = BaiduTranslator(app_id, secret)
                self._baidu_disabled_until = 0 
                logger.info(f"Using Baidu Translator ({app_id})")
        
        if not self.translator:
            if provider == 'baidu_dev':
                 logger.warning("Baidu config missing, falling back to Google Free")
            try:
                self.translator = GoogleTranslator(source='auto', target='zh-CN', proxies=self.proxies) # Support proxy for google
                self.has_baidu = False
                logger.info("Using Google Translator (Free)")
            except Exception as e:
                logger.error(f"Failed to init Google Translator: {e}")
                self.translator = None
                self.has_baidu = False

        if provider == 'baidu_dev' and self.translator:
            self.baidu_translator = self.translator
            self.has_baidu = True
        else:
            self.baidu_translator = None
            self.has_baidu = False

    def _translate_via_google_endpoint(self, texts: List[str]) -> Dict[str, str]:
        """
        免费 Google 网页端点批量翻译（免 Key）。

        deep_translator 走的是 translate.google.com/m 端点，实测单条请求也会整体
        触发 TooManyRequests；这里换成 translate.googleapis.com 的 gtx 接口，
        用「一个 q + 换行」一次拿回整批译文，返回 {原文: 译文}（对齐不上时返回空）。
        """
        if not texts:
            return {}
        params = [("client", "gtx"), ("sl", "auto"), ("tl", "zh-CN"), ("dt", "t")]
        params.append(("q", "\n".join(texts)))
        response = self.session.get(
            "https://translate.googleapis.com/translate_a/single",
            params=params,
            timeout=10,
        )
        response.raise_for_status()
        segments = response.json()[0] or []
        combined = "".join(segment[0] for segment in segments if segment and segment[0])
        lines = combined.split("\n")
        if len(lines) != len(texts):
            logger.warning("Google 端点译文行数不匹配: 原文 %s 行 / 译文 %s 行", len(texts), len(lines))
            return {}
        return {
            original: line.strip()
            for original, line in zip(texts, lines)
            if line.strip()
        }

    def translate_to_chinese(self, text: str) -> str:
        """
        翻译文本为简体中文
        
        降级策略：百度翻译(快) → Google翻译(备用) → 返回原文(兜底)
        所有结果都会缓存到数据库
        """
        try:
            if not text or not text.strip():
                return text
            
            # 1. 检查数据库缓存
            cached = db.get_translation(text)
            if cached:
                return cached
            
            # fallback 到单次翻译逻辑（此处简化，直接调用 batch）
            results = self.batch_translate_to_chinese([text])
            return results[0] if results else text
            
        except Exception as e:
            logger.warning(f"翻译过程异常 ({text[:20]}...): {e}")
            return text

    def batch_translate_to_chinese(
        self,
        texts: List[str],
        *,
        wait_for_lock: bool = True,
    ) -> List[str]:
        """
        批量翻译文本列表，显著减少 API 请求频率
        """
        if not texts:
            return []
            
        # 1. 初始化结果并找出需要翻译的文本
        results = [None] * len(texts)
        pending_indices_by_text = {}
        to_translate_texts = []
        
        for i, text in enumerate(texts):
            if not text or not text.strip() or text.isdigit():
                results[i] = text
                continue
            
            cached = db.get_translation(text)
            if cached:
                results[i] = normalize_simplified_title(cached, text)
            else:
                pending_indices_by_text.setdefault(text, []).append(i)
                if len(pending_indices_by_text[text]) == 1:
                    to_translate_texts.append(text)
                
        if not to_translate_texts:
            return results

        lock_acquired = (
            self._translation_lock.acquire()
            if wait_for_lock
            else self._translation_lock.acquire(blocking=False)
        )
        if not lock_acquired:
            for indices in pending_indices_by_text.values():
                for index in indices:
                    results[index] = texts[index]
            return results
            
        try:
            # 启动预热、滚动翻译和其它后台任务共享同一个翻译器。
            # 加锁后重新查一次缓存，避免并发请求把同一批标题重复打到上游。
            cached_again = db.get_translations_batch(to_translate_texts)
            for original, cached in cached_again.items():
                normalized = normalize_simplified_title(cached, original)
                if normalized:
                    for index in pending_indices_by_text.pop(original, []):
                        results[index] = normalized

            to_translate_texts = [
                text for text in to_translate_texts if text in pending_indices_by_text
            ]
            if not to_translate_texts:
                return results

            translated_dict = {}

            # 本地大模型优先：Ollama 并发为 1，按批串行提交，每批翻完立刻落库，
            # 避免中途超时导致整批结果全丢。
            if getattr(self, 'ollama_translator', None) and to_translate_texts:
                llm_target_count = len(to_translate_texts)
                batch_size = self.ollama_translator.batch_size
                for start in range(0, len(to_translate_texts), batch_size):
                    chunk = to_translate_texts[start:start + batch_size]
                    try:
                        chunk_translations = self.ollama_translator.translate_batch(chunk)
                    except Exception as e:
                        logger.warning(
                            "本地大模型翻译失败: %s，回退其它翻译源",
                            type(e).__name__,
                        )
                        break
                    if not chunk_translations:
                        logger.warning(
                            "本地大模型未给出可用译文（%s 条），改用兜底翻译源",
                            len(chunk),
                        )
                    for original, translated in chunk_translations.items():
                        normalized = normalize_simplified_title(translated, original)
                        # 原文照抄（模型没翻出来）不入库，留给后续再试
                        if not normalized or normalized == original or is_bad_translation_text(normalized):
                            continue
                        translated_dict[original] = normalized
                        db.save_translation(original, normalized)
                        db.update_movie_titles_by_translation(original, normalized)
                        for index in pending_indices_by_text.pop(original, []):
                            results[index] = normalized

                to_translate_texts = [
                    text for text in to_translate_texts if text in pending_indices_by_text
                ]
                logger.info(
                    "本地大模型翻译完成: 成功 %s/%s 个新项",
                    len(translated_dict),
                    llm_target_count,
                )
                if not to_translate_texts:
                    return results
            
            # 尝试通过百度翻译批量接口
            # 检查百度翻译是否被临时禁用
            baidu_disabled_until = getattr(self, '_baidu_disabled_until', 0)
            current_time = time.time()
            
            if self.has_baidu and current_time >= baidu_disabled_until:
                try:
                    # 将列表合并为多行文本，百度翻译对 \n 支持很好，能保持对应关系
                    combined_text = "\n".join(to_translate_texts)
                    
                    # 针对 54003 (QPS限制) 增加简单重试
                    translated_combined = None
                    for attempt in range(2):
                        res = self.baidu_translator.translate_text(combined_text, to_lang='zh')
                        
                        if isinstance(res, str):
                            translated_combined = res
                            break
                        elif isinstance(res, dict):
                            error_code = str(res.get('error', ''))
                            if error_code == '54003': # QPS Limit
                                logger.warning(f"百度翻译并发受限 (54003)，重试 {attempt+1}/2...")
                                time.sleep(1.2)
                                continue
                            elif error_code == '54004': # 余额不足
                                self._baidu_disabled_until = time.time() + 43200 # 12小时
                                logger.error("百度翻译余额不足 (54004)，临时禁用12小时")
                                break
                            else:
                                logger.warning(f"百度翻译非预期错误 ({error_code}): {res.get('msg')}")
                                break
                    
                    if translated_combined:
                        lines = translated_combined.split('\n')
                        if len(lines) == len(to_translate_texts):
                            for orig, trans in zip(to_translate_texts, lines):
                                translated_dict[orig] = trans.strip()
                            logger.info(f"批量百度翻译成功: {len(to_translate_texts)} 个项")
                except Exception as e:
                    logger.warning(f"批量百度翻译逻辑执行失败: {e}，切换到 Google 翻译")
            elif self.has_baidu and current_time < baidu_disabled_until:
                remaining_minutes = int((baidu_disabled_until - current_time) / 60)
                logger.debug(f"百度翻译仍在禁用期（剩余 {remaining_minutes} 分钟），使用 Google 翻译")

            # 3. 处理未完成的翻译 (Google 兜底)
            remaining_texts = [t for t in to_translate_texts if t not in translated_dict]
            # 3.1 先用免 Key 网页端点整批翻译：一次请求覆盖本批全部标题，
            #     deep_translator 的端点被限流时这条路径仍可用（其自身冷却状态不影响这里）
            if remaining_texts:
                try:
                    translated_dict.update(self._translate_via_google_endpoint(remaining_texts))
                except Exception as e:
                    logger.warning("Google 网页端点批量翻译失败: %s", type(e).__name__)
                remaining_texts = [t for t in remaining_texts if t not in translated_dict]

            if remaining_texts:
                now = time.time()
                if now < self._google_disabled_until:
                    remaining_texts = []
                else:
                    try:
                        # [FIX] 如果当前主翻译器是 BaiduTranslator，创建临时 GoogleTranslator 作为后备
                        google_translator = None
                        # 检查 self.translator 是否是我们要的 GoogleTranslator 实例
                        # 注意：需要从 deep_translator 导入 GoogleTranslator 类进行判断
                        from deep_translator import GoogleTranslator as DeepGoogleTranslator
                        if isinstance(self.translator, DeepGoogleTranslator):
                            google_translator = self.translator
                        else:
                            # 当前不是 Google 翻译器（可能是 BaiduTranslator），创建 Google 翻译器作为后备
                            try:
                                google_translator = GoogleTranslator(source='auto', target='zh-CN', proxies=self.proxies)
                                logger.info(f"百度翻译未完成 {len(remaining_texts)} 个文本，使用 Google 翻译作为后备")
                            except Exception as e:
                                logger.error(f"创建 Google 翻译器失败: {e}")

                        if google_translator:
                            # 使用 translate_batch 批量翻译
                            batch_size = 20
                            for i in range(0, len(remaining_texts), batch_size):
                                batch = remaining_texts[i:i + batch_size]
                                try:
                                    # 增加随机延迟，避免短时间多次调用
                                    time.sleep(random.uniform(1.0, 2.0))

                                    batch_results = google_translator.translate_batch(batch)

                                    if batch_results and len(batch_results) == len(batch):
                                        for orig, trans in zip(batch, batch_results):
                                            if trans:
                                                translated_dict[orig] = trans.strip()
                                    else:
                                        logger.warning(f"Google 批量翻译返回数量不匹配: input={len(batch)}, output={len(batch_results) if batch_results else 0}")
                                except Exception as e:
                                    error_text = f"{type(e).__name__}: {e}".lower()
                                    rate_limited = any(
                                        marker in error_text
                                        for marker in ("too many requests", "429", "rate limit", "ratelimit")
                                    )
                                    if rate_limited:
                                        self._google_disabled_until = time.time() + 600
                                        if time.time() - self._google_cooldown_log_at >= 60:
                                            logger.warning(
                                                "Google 翻译触发限流，暂停 10 分钟并返回原文，避免继续阻塞业务"
                                            )
                                            self._google_cooldown_log_at = time.time()
                                        break
                                    logger.warning("Google 批量翻译分块失败: %s", type(e).__name__)
                        else:
                            logger.warning(f"无可用的翻译器处理剩余 {len(remaining_texts)} 个文本")

                    except Exception as e:
                        logger.warning("Google 翻译逻辑整体失败: %s", type(e).__name__)

            # 4. 汇总结果并存入缓存
            translated_count = 0
            for orig, indices in pending_indices_by_text.items():
                raw_trans = translated_dict.get(orig)
                trans = normalize_simplified_title(raw_trans, orig)
                if trans:
                    if raw_trans and trans != orig and not is_bad_translation_text(raw_trans):
                        db.save_translation(orig, trans)
                        # [NEW] 联动更新：确保数据库中所有相关电影标题立即变中文
                        db.update_movie_titles_by_translation(orig, trans)
                        translated_count += 1
                    for index in indices:
                        results[index] = trans
                else:
                    fallback = normalize_simplified_title(orig)
                    for index in indices:
                        results[index] = fallback
            
            logger.info(f"批量翻译完成: 成功 {translated_count}/{len(to_translate_texts)} 个新项")
                    
        except Exception as e:
            logger.error(f"批量翻译逻辑出错: {e}")
            # 填充剩余空缺
            for indices in pending_indices_by_text.values():
                for index in indices:
                    if results[index] is None:
                        results[index] = texts[index]
        finally:
            self._translation_lock.release()
                
        return results

    def filter_processed_movies(self, movies: List[Dict]) -> tuple:
        """
        页面只展示处理完的数据：展示标题还是日文的条目，先看翻译库里有没有现成译文，
        有就换成中文展示（列表缓存是 4 小时的旧快照，不在这里补就跟不上新翻译），
        没有就这次不下发，并插队翻译，前端刷新即可看到。

        判据是**展示用的 title** 是否还含假名，而不是 title 与 original_title 是否相同：
        源站列表里 title 是短标题、original_title 是带番号日期的完整标题，两者不同不代表已翻译。

        返回 (可展示条目, 本页待翻译条数)。
        """
        movie_list = list(movies or [])
        sources = {}
        for movie in movie_list:
            display_title = str(movie.get("title") or "").strip()
            if display_title and JAPANESE_KANA_PATTERN.search(display_title):
                source_text = str(
                    movie.get("original_title") or movie.get("title_original") or display_title
                ).strip()
                if source_text:
                    sources[source_text] = None

        if not sources:
            return movie_list, 0

        known = db.get_translations_batch(list(sources.keys()))
        processed: List[Dict] = []
        pending: List[str] = []
        for movie in movie_list:
            display_title = str(movie.get("title") or "").strip()
            source_text = str(
                movie.get("original_title") or movie.get("title_original") or display_title
            ).strip()
            translated = normalize_simplified_title(known.get(source_text), "") if source_text else ""
            if not display_title or not JAPANESE_KANA_PATTERN.search(display_title):
                processed.append(movie)
            elif translated and not JAPANESE_KANA_PATTERN.search(translated):
                # 库里已有不含假名的中文译文：直接用它展示（列表缓存是旧快照，这里补上）
                processed.append({**movie, "title": translated})
            elif source_text and source_text not in known:
                # 还没翻过：这次不下发，排进翻译队列，翻好刷新即可看到
                pending.append(source_text)
            # 已有译文但仍含假名（模型保留了假名艺人名等）：不下发也不重复翻译，避免白耗显卡

        if pending:
            self.queue_title_translations(pending)
        return processed, len(pending)

    def queue_title_translations(self, titles: List[str], priority: bool = True) -> int:
        """
        把一个片子排进翻译队列（一个片子一个请求，翻完立刻落库）。

        priority=True：页面刚打开，插到队首先翻；已经在队列里的也会被**提到队首**
        （否则页面要排在几千条历史积压后面）。历史回填用 priority=False 排到队尾。
        同一标题 10 分钟内不重复入队，避免反复刷新页面把同一个片子堆很多次。
        """
        now = time.time()
        added: List[str] = []
        promoted: List[str] = []
        with self._translation_queue_lock:
            self._queued_titles = {
                title: queued_at
                for title, queued_at in self._queued_titles.items()
                if now - queued_at < 600
            }
            for title in titles:
                if not title:
                    continue
                if title in self._queued_titles:
                    # 已排过：只做提权，**不刷新**去重时间戳。
                    # 否则回填每轮都查回同一批还没翻完的标题，时间戳被一直续期，
                    # 这批标题永远不再入队、游标也卡死不动。
                    if priority:
                        promoted.append(title)
                    continue
                self._queued_titles[title] = now
                added.append(title)

            for title in promoted:
                try:
                    self._translation_queue.remove(title)
                except ValueError:
                    # 已经被工作线程取走正在翻译，保持原样即可
                    continue

            fresh = added + promoted
            if fresh:
                if priority:
                    self._translation_queue.extendleft(reversed(fresh))
                else:
                    self._translation_queue.extend(fresh)

            if not self._translation_worker or not self._translation_worker.is_alive():
                self._translation_worker = threading.Thread(
                    target=self._translation_worker_loop,
                    name="title-translation-queue",
                    daemon=True,
                )
                self._translation_worker.start()

        self._translation_queue_event.set()
        return len(fresh)

    def pending_translation_queue_size(self) -> int:
        with self._translation_queue_lock:
            return len(self._translation_queue)

    def _translation_worker_loop(self) -> None:
        """翻译队列工作线程：一次取一个片子、发一次请求、落一次库。"""
        attempts: Dict[str, int] = {}
        while True:
            title = None
            with self._translation_queue_lock:
                if self._translation_queue:
                    title = self._translation_queue.popleft()

            if title is None:
                self._translation_queue_event.wait(15)
                self._translation_queue_event.clear()
                continue

            try:
                self.batch_translate_to_chinese([title])
                attempts.pop(title, None)
            except Exception as e:
                # 失败不能把片子丢掉（丢了就再也翻不出来）：重排回队列稍后重试，
                # 连续失败 3 次才放弃，避免一条坏数据把队列卡住。
                count = attempts.get(title, 0) + 1
                attempts[title] = count
                logger.warning("翻译队列执行失败(%s/3): %s", count, type(e).__name__)
                if count < 3:
                    time.sleep(2)
                    with self._translation_queue_lock:
                        self._translation_queue.append(title)
                    self._translation_queue_event.set()
                else:
                    attempts.pop(title, None)

    def get_navigation(self, force_refresh: bool = False) -> Dict[str, List[Dict]]:
        """
        获取导航栏分类
        优先从缓存返回，若强制刷新或缓存不存在则抓取。
        """
        cache_key = "navigation_data"
        cached_data = db.get_cache(cache_key)
        
        # [NEW] 模式：非强制刷新且有缓存，直接秒开返回
        if not force_refresh and cached_data:
            logger.debug("从缓存返回导航数据 (秒开)")
            return cached_data

        try:
            logger.info("正在从源站抓取最新导航数据...")
            # 探测最佳域名并设置
            self.find_best_domain()
            
            response = self.session.get(self.base_url, timeout=15)
            response.raise_for_status()
            soup = self.parse_html(response)
            
            navigation = {
                "censored": [],
                "uncensored": []
            }
            
            # 查找导航菜单
            nav_menu = soup.find('div', id='navbar')
            if not nav_menu:
                logger.warning("未找到导航菜单")
                if cached_data: return cached_data
                return navigation
            
            # 提取分类链接
            links_to_process = []
            for link in nav_menu.find_all('a'):
                href = link.get('href', '')
                text = link.get_text(strip=True)
                if href and text:
                    links_to_process.append({"href": href, "text": text})

            if links_to_process:
                # 批量翻译
                texts_to_translate = [item["text"] for item in links_to_process]
                translated_names = self.batch_translate_to_chinese(texts_to_translate)

                for i, item in enumerate(links_to_process):
                    href = item["href"]
                    text = item["text"]
                    translated_name = translated_names[i]
                    
                    full_url = href if href.startswith('http') else self.base_url + href
                    nav_item = {
                        "name": translated_name,
                        "original_name": text,
                        "url": full_url
                    }
                    
                    if 'uncensored' in href.lower():
                        navigation["uncensored"].append(nav_item)
                    else:
                        navigation["censored"].append(nav_item)
            
            # 补充额外入口 (确保核心分类不丢失)
            extra_items_censored = [
                {"name": "高清", "original_name": "HD", "url": f"{self.base_url}/genre/hd"},
                {"name": "字幕", "original_name": "Sub", "url": f"{self.base_url}/genre/sub"},
                {"name": "VR", "original_name": "VR", "url": f"{self.base_url}/genre/vr"},
                {"name": "歐美", "original_name": "Western", "url": f"{self.base_url}/genre/ou"},
                {"name": "4K", "original_name": "4K", "url": f"{self.base_url}/genre/4k"}
            ]
            for item in extra_items_censored:
                if not any(n['url'] == item['url'] for n in navigation["censored"]):
                    navigation["censored"].append(item)
            
            navigation["uncensored"].extend([
                {"name": "高清", "original_name": "HD", "url": f"{self.base_url}/uncensored/genre/hd"},
                {"name": "字幕", "original_name": "Sub", "url": f"{self.base_url}/uncensored/genre/sub"}
            ])
            
            # 存入缓存
            if navigation["censored"] or navigation["uncensored"]:
                db.set_cache(cache_key, navigation)
                logger.info("已更新导航数据缓存")
                return navigation
                
            if cached_data: return cached_data
            return navigation
            
        except Exception as e:
            logger.error(f"抓取导航时出错: {e}")
            if cached_data:
                logger.info("抓取失败，返回过期缓存数据。")
                return cached_data
            return {"censored": [], "uncensored": []}

    def get_genres(self, is_uncensored: bool = False, force_refresh: bool = False) -> Dict[str, List[Dict[str, str]]]:
        """
        获取详细分类标签（主题、角色、服装等）
        
        策略：秒开 + 后台异步翻译
        1. 优先返回 cache_key 中的完整结构。
        2. 如果缓存缺失，从源站抓取结构（HTML解析），并仅使用本地已有的中文进行翻译。
        3. 发起异步任务去翻译缺失的项，翻译完成后更新缓存。
        """
        cache_key = f"genres_{'uncensored' if is_uncensored else 'censored'}"
        cached_data = db.get_cache(cache_key)
        
        # [NEW] 模式：非强制刷新且有缓存，直接秒开返回
        if not force_refresh and cached_data:
            logger.debug(f"从缓存返回 {'无码' if is_uncensored else '有码'} 的分类标签 (秒开)")
            return cached_data

        base_url = f"{self.base_url}/uncensored/genre" if is_uncensored else f"{self.base_url}/genre"
        
        try:
            logger.info(f"正在从源站抓取最新 {'无码' if is_uncensored else '有码'} 分类结构...")
            # 探测最佳域名 (HEAD 请求，很快)
            self.find_best_domain()
            
            response = self.session.get(base_url, timeout=10) # 结构抓取设短超时
            response.raise_for_status()
            soup = self.parse_html(response)
            
            genres = {}
            
            # 1. 解析结构
            headers = soup.find_all('h4')
            texts_to_check = []
            structure = [] 
            
            for h in headers:
                if 'modal-title' in h.get('class', []):
                    continue
                header_text = h.get_text(strip=True)
                texts_to_check.append(header_text)
                
                group_items = []
                genre_box = h.find_next_sibling('div', class_='genre-box')
                if genre_box:
                    links = genre_box.find_all('a')
                    for a in links:
                        item_text = a.get_text(strip=True)
                        item_url = a.get('href', '')
                        if not item_url.startswith('http'):
                            item_url = self.base_url + (item_url if item_url.startswith('/') else '/' + item_url)
                        
                        texts_to_check.append(item_text)
                        group_items.append({"text": item_text, "url": item_url})
                
                if group_items:
                    structure.append({"header": header_text, "items": group_items})

            if not structure:
                logger.warning(f"无法从页面解析出分类结构: {base_url}")
                if cached_data: return cached_data
                return {}

            # 2. 秒开策略：仅使用本地数据库已有的翻译，不调用 API
            unique_texts = [t for t in list(set(texts_to_check)) if t]
            # [FIX] 使用数据库查询而非 batch_translate_to_chinese (后者会调用 API)
            translate_map = db.get_translations_batch(unique_texts)

            # 3. 组装初步结果
            missing_texts = []
            for group in structure:
                trans_header = translate_map.get(group["header"], group["header"])
                if group["header"] not in translate_map:
                    missing_texts.append(group["header"])
                    
                final_items = []
                for item in group["items"]:
                    trans_name = translate_map.get(item["text"], item["text"])
                    if item["text"] not in translate_map:
                        missing_texts.append(item["text"])
                        
                    final_items.append({
                        "name": trans_name,
                        "original_name": item["text"],
                        "url": item["url"]
                    })
                genres[trans_header] = final_items
            
            # 4. 后台静默翻译（如果有缺失项）
            if missing_texts:
                logger.info(f"发现 {len(missing_texts)} 个分类项缺失翻译，已触发后台批量翻译任务...")
                import threading
                def background_translate():
                    try:
                        # 真正的 API 翻译调用
                        self.batch_translate_to_chinese(missing_texts)
                        # 翻译完成后再次生成完整的 genres 并更新缓存
                        full_translate_map = db.get_translations_batch(unique_texts)
                        updated_genres = {}
                        for group in structure:
                            t_header = full_translate_map.get(group["header"], group["header"])
                            f_items = []
                            for it in group["items"]:
                                f_items.append({
                                    "name": full_translate_map.get(it["text"], it["text"]),
                                    "original_name": it["text"],
                                    "url": it["url"]
                                })
                            updated_genres[t_header] = f_items
                        db.set_cache(cache_key, updated_genres)
                        logger.info(f"异步分类翻译完成，已更新缓存: {cache_key}")
                    except Exception as e:
                        logger.error(f"异步分类翻译失败: {e}")

                thread = threading.Thread(target=background_translate)
                thread.daemon = True
                thread.start()
            else:
                # 即使没有缺失，也更新下 updated_at
                db.set_cache(cache_key, genres)

            return genres
            
        except Exception as e:
            logger.error(f"获取分类标签结构时出错: {e}")
            if cached_data:
                logger.info("抓取失败，返回本地缓存数据。")
                return cached_data
            return {}

    def get_movies(self, url: str, page: int = 1, limit: int = 30, force_refresh: bool = False, perform_translate: bool = True, translate_mode: str = "full") -> List[Dict]:
        """
        获取电影列表
        支持自定义每页数量（通过请求多个源页面实现）
        
        Args:
            translate_mode: 翻译模式
                - "full": 完整翻译（调用API）
                - "cache_only": 仅使用缓存翻译
                - "none": 不翻译（等同于 perform_translate=False）
        """
        from urllib.parse import urlparse

        # Cache identity is source + path, never the currently selected domain.
        if url and not url.startswith('http'):
            url = self.base_url.rstrip('/') + ("/" if not url.startswith('/') else "") + url
        parsed_input = urlparse(url)
        request_path = parsed_input.path.rstrip('/') or ''
        request_query = f"?{parsed_input.query}" if parsed_input.query else ""
        cache_key = self.get_movies_cache_key(
            f"{request_path}{request_query}",
            page,
            limit,
        )
        legacy_cache_keys = [
            f"movies_list_{url.rstrip('/').replace(':', '_').replace('/', '_')}_p{page}_l{limit}"
        ]
        for candidate in self.domain_router.candidates():
            legacy_url = f"{candidate}{request_path}{request_query}".rstrip('/')
            legacy_key = f"movies_list_{legacy_url.replace(':', '_').replace('/', '_')}_p{page}_l{limit}"
            if legacy_key not in legacy_cache_keys:
                legacy_cache_keys.append(legacy_key)
        
        # [NEW] 缓存优先策略
        if not force_refresh:
            cache_full = db.get_cache_full(cache_key)
            cache_key_used = cache_key
            if not cache_full:
                for legacy_key in legacy_cache_keys:
                    cache_full = db.get_cache_full(legacy_key)
                    if cache_full:
                        cache_key_used = legacy_key
                        break
            if cache_full:
                cached_data = cache_full["value"]
                updated_at = cache_full["updated_at"]
                now = time.time()
                
                # 如果缓存还在 4 小时内，直接返回
                if now - updated_at < 14400: # 4 * 3600
                    logger.debug(f"[Cache Hit] 从缓存返回 {request_path} 第 {page} 页 (有效期内)")
                    if cache_key_used != cache_key:
                        db.set_cache(cache_key, cached_data)
                    return sanitize_movie_list_titles(cached_data)
                
                # 如果缓存超过 4 小时，返回缓存的同时，在后台触发一次静默刷新
                # 注意：只对第一页触发后台同步，避免深度挖掘压力
                logger.info(f"[Cache Stale] 返回过期缓存并触发后台同步: {request_path}")
                if page == 1:
                    import threading
                    # 为了防止递归，后台刷新时必须设置 force_refresh=True
                    # 使用 lambda 捕获 self 并调用方法
                    threading.Thread(
                        target=lambda: self.refresh_category(f"{request_path}{request_query}", max_pages=1),
                        daemon=True
                    ).start()
                
                return sanitize_movie_list_titles(cached_data)
        try:
            movies = []
            source_page_size = 30  # 估计 JavBus 每页平均电影数
            
            num_pages_to_fetch = (limit + source_page_size - 1) // source_page_size
            start_source_page = (page - 1) * num_pages_to_fetch + 1
            
            from concurrent.futures import ThreadPoolExecutor, as_completed
            
            def fetch_for_domain(domain):
                page_urls = []
                for i in range(num_pages_to_fetch):
                    current_source_page = start_source_page + i
                    if current_source_page == 1:
                        page_url = f"{domain}{request_path}{request_query}"
                    elif request_path in ("", "/uncensored"):
                        page_url = f"{domain}{request_path}/page/{current_source_page}"
                    else:
                        page_url = f"{domain}{request_path}/{current_source_page}"
                    page_urls.append((current_source_page, page_url))

                def fetch_single_page(page_item):
                    current_source_page, p_url = page_item
                    try:
                        res = self.session.get(p_url, timeout=10)
                        if res.status_code == 404 and current_source_page > 1:
                            return []
                        res.raise_for_status()
                        lowered = res.text.lower()
                        challenge_markers = (
                            "just a moment",
                            "cf-chl-",
                            "verify you are human",
                            "captcha",
                        )
                        if any(marker in lowered for marker in challenge_markers):
                            raise RuntimeError("challenge_page")
                        boxes = self.parse_html(res).find_all('a', class_='movie-box')
                        if current_source_page == 1 and not boxes:
                            raise RuntimeError("empty_movie_page")
                        return boxes
                    except Exception as e:
                        logger.warning("抓取页面失败 [%s]: %s", domain, type(e).__name__)
                        raise

                movie_boxes = []
                with ThreadPoolExecutor(max_workers=min(len(page_urls), 5)) as executor:
                    futures = [executor.submit(fetch_single_page, item) for item in page_urls]
                    for future in as_completed(futures):
                        movie_boxes.extend(future.result())
                return movie_boxes

            active_domain = None
            movie_boxes = []
            for domain in self.domain_router.candidates():
                try:
                    started_at = time.time()
                    movie_boxes = fetch_for_domain(domain)
                    active_domain = domain
                    self.domain_router.record_success(domain, (time.time() - started_at) * 1000)
                    self.base_url = domain
                    break
                except Exception as e:
                    self.domain_router.record_failure(domain, type(e).__name__)
                    logger.warning("Source domain failed, switching [%s]: %s", domain, type(e).__name__)

            # 所有候选域名都抓失败时（active_domain 为空）与"源站真的没有这一页"必须区分：
            # 抓取失败抛异常，让接口回 success=False；否则前端会拿到空数组判定到底，
            # 表现为"无限滚动卡住"这种极难定位的现象。
            if not active_domain:
                raise RuntimeError("all_source_domains_failed")
            if not movie_boxes:
                return []

            # 1. 解析所有盒子内容（不包含翻译）
            raw_movies = []
            for box in movie_boxes:
                try:
                    img = box.find('img')
                    cover = img.get('src', '') if img else ''
                    if cover and cover.startswith('/'):
                        cover = active_domain + cover
                    
                    # 解析日期信息，获取电影 ID 和发布日期
                    dates = box.find_all('date')
                    movie_id = dates[0].get_text(strip=True) if dates else ''
                    release_date = dates[1].get_text(strip=True) if len(dates) >= 2 else ''
                    # 兼容其他结构（如缺少第二个 <date>）
                    if not release_date:
                        # 尝试从其他标签或正则获取 YYYY-MM-DD
                        import re
                        m = re.search(r"\d{4}-\d{2}-\d{2}", box.get_text())
                        release_date = m.group(0) if m else ''
                    
                    title_elem = box.find('span')
                    title = title_elem.get_text(strip=True) if title_elem else ''
                    
                    # [FILTER] 过滤未上映影片 (发布日期 > 今天)
                    if release_date:
                        try:
                            movie_dt = datetime.strptime(release_date, '%Y-%m-%d')
                            if movie_dt > datetime.now():
                                logger.info(f"跳过未上映影片: {movie_id} ({release_date})")
                                continue
                        except Exception:
                            pass  # 日期格式解析失败则不过滤

                    detail_url = box.get('href', '')
                    if detail_url and detail_url.startswith('/'):
                        detail_url = active_domain + detail_url
                    
                    raw_movies.append({
                        "id": movie_id,
                        "original_title": title,
                        "cover": cover,
                        "detail_url": detail_url,
                        "date": release_date
                    })
                except:
                    continue

            if not raw_movies:
                return []

            # 2. 根据翻译模式处理标题
            if translate_mode == "none" or not perform_translate:
                # 模式 A: 不翻译。直接返回原文，但不覆盖数据库中的 title (如果有的话)
                movies = []
                for m in raw_movies:
                    # 获取数据库中已有的翻译
                    existing = db.get_movie(m["id"])
                    existing_title = normalize_simplified_title(existing.get("title"), "") if existing else ""
                    if existing_title and existing_title != m["original_title"]:
                        m["title"] = existing_title
                    else:
                        m["title"] = m["original_title"]
                    db.save_movie(m)
                    movies.append(m)
            elif translate_mode == "cache_only":
                # 模式 B: 仅使用缓存。
                titles_to_check = [m["original_title"] for m in raw_movies]
                cached_translations = db.get_translations_batch(titles_to_check)
                
                movies = []
                for m in raw_movies:
                    # 优先级：缓存翻译 > 数据库现有标题 > 原文
                    translated = normalize_simplified_title(cached_translations.get(m["original_title"]), "")
                    if translated and translated != m["original_title"]:
                        m["title"] = translated
                    else:
                        existing = db.get_movie(m["id"])
                        existing_title = normalize_simplified_title(existing.get("title"), "") if existing else ""
                        if existing_title and existing_title != m["original_title"]:
                            m["title"] = existing_title
                        else:
                            m["title"] = m["original_title"]
                    
                    db.save_movie(m)
                    movies.append(m)
                
                logger.info(f"缓存翻译命中: {len(cached_translations)}/{len(titles_to_check)}")
            else:  # translate_mode == "full"
                # 模式 C: 完整翻译。
                titles_to_translate = [m["original_title"] for m in raw_movies]
                translated_titles = self.batch_translate_to_chinese(titles_to_translate)
                
                movies = []
                for i, m in enumerate(raw_movies):
                    m["title"] = translated_titles[i]
                    db.save_movie(m)
                    movies.append(m)

            
            # [NEW] 按发布时间倒序排序,最新的影片在最上面
            if movies:
                # 按日期降序排序 (最新的在前)
                # 对于没有日期或日期无效的影片,排在最后
                movies = sorted(
                    movies, 
                    key=lambda m: m.get('date', '') or '', 
                    reverse=True
                )
                db.set_cache(cache_key, movies)
            
            logger.info(f"返回第 {page} 页的 {len(movies)} 部电影 (翻译模式: {translate_mode})")
            return movies
            
        except Exception as e:
            logger.error(f"获取电影列表出错: {e}")
            return []

    def get_movies_cache_key(self, path: str, page: int = 1, limit: int = 30) -> str:
        normalized_path = str(path or "").strip()
        if normalized_path.startswith("http"):
            parsed = urlparse(normalized_path)
            normalized_path = parsed.path or ""
            if parsed.query:
                normalized_path += f"?{parsed.query}"
        if not normalized_path.startswith("/"):
            normalized_path = "/" + normalized_path
        token = normalized_path.replace("?", "__").replace("&", "_").replace("=", "-")
        token = re.sub(r"[^a-zA-Z0-9_./-]", "_", token).replace("/", "_")
        return f"movies_list_v2_javbus_{token}_p{int(page)}_l{int(limit)}"

    def get_movie_detail(self, url: str, perform_translate: bool = True) -> Dict:
        """
        获取电影详情和磁力链接
        """
        from urllib.parse import urlparse
        try:
            parsed_input = urlparse(url)
            request_path = parsed_input.path or "/"
            if parsed_input.query:
                request_path += f"?{parsed_input.query}"
            response = None
            active_url = url
            domain_prefix = f"{parsed_input.scheme}://{parsed_input.netloc}"

            for candidate in self.domain_router.candidates():
                candidate_url = f"{candidate}{request_path}"
                try:
                    started_at = time.time()
                    candidate_response = self.session.get(candidate_url, timeout=15)
                    candidate_response.raise_for_status()
                    lowered = candidate_response.text.lower()
                    if any(
                        marker in lowered
                        for marker in ("just a moment", "cf-chl-", "verify you are human", "captcha")
                    ):
                        raise RuntimeError("challenge_page")
                    response = candidate_response
                    active_url = candidate_url
                    domain_prefix = candidate
                    self.domain_router.record_success(
                        candidate,
                        (time.time() - started_at) * 1000,
                    )
                    self.base_url = candidate
                    break
                except Exception as exc:
                    self.domain_router.record_failure(candidate, type(exc).__name__)
                    logger.warning(
                        "详情页域名失败，切换备用域名 [%s]: %s",
                        candidate,
                        type(exc).__name__,
                    )

            if response is None:
                # 所有候选域名都取不到（最常见的断网形态）：退回库里的结构化详情，
                # 否则离线时明明有缓存也会返回空详情。
                cached_detail = self._cached_movie_detail(url)
                return cached_detail or {"magnets": [], "info": {}, "title": "", "original_title": ""}

            url = active_url
            
            # 检查年龄验证 (Simple Modal)
            if 'id="ageVerify"' in response.text or 'name="Submit"' in response.text:
                 logger.info("检测到年龄验证弹窗，尝试通过 POST 绕过...")
                 try:
                     self.session.post(url, data={"Submit": "確認"}, timeout=10)
                     # POST 后页面通常会重定向或直接返回内容，再次 GET 确认
                     response = self.session.get(url, timeout=15)
                 except Exception as e:
                     logger.warning(f"年龄验证绕过失败: {e}")

            if "driver-verify" in response.url:
                logger.warning("重定向到了复杂的驾驶员验证页面，可能无法自动绕过。")
            
            response.raise_for_status()
            soup = self.parse_html(response)
            
            detail = {
                "magnets": [],
                "info": {}
            }
            
            # 提取标题
            title_elem = soup.find('h3')
            title = title_elem.get_text(strip=True) if title_elem else ''
            detail['title'] = self.translate_to_chinese(title) if perform_translate else title
            detail['original_title'] = title
            
            # 提取封面
            cover_elem = soup.select_one('.bigImage img')
            if cover_elem:
                cover_url = cover_elem.get('src', '')
                if cover_url and cover_url.startswith('/'):
                    cover_url = self.base_url + cover_url
                detail['cover'] = cover_url
            else:
                detail['cover'] = ''
 
            # 提取演员 (Stars)
            # 兼容多种页面结构: .star-show (旧版) 和 .avatar-box (新版)
            detail['stars'] = []
            
            # 策略 1: .avatar-box (常见于 newer JavBus design)
            avatar_boxes = soup.select('.avatar-box')
            if avatar_boxes:
                for box in avatar_boxes:
                    star_name = box.get_text(strip=True)
                    img = box.find('img')
                    if not star_name and img:
                        star_name = img.get('title') or img.get('alt')
                    
                    link = box.parent if box.parent.name == 'a' else box.find('a')
                    star_url = link.get('href') if link else ''
                    star_id = star_url.strip('/').split('/')[-1] if star_url else ''
                    
                    if star_name:
                        detail['stars'].append({
                            "name": self.translate_to_chinese(star_name) if perform_translate else star_name,
                            "id": star_id,
                            "url": star_url if star_url.startswith('http') else domain_prefix + (star_url if star_url.startswith('/') else '/' + star_url)
                        })

            # 策略 2: .star-show (fallback)
            if not detail['stars']:
                star_divs = soup.select('.star-show')
                if star_divs:
                    for star_div in star_divs:
                        # 优先获取纯文本 (通常在标签下)
                        star_name = star_div.get_text(strip=True)
                        
                        # 如果文本为空，尝试获取图片的 title 或 alt
                        if not star_name:
                            img = star_div.find('img')
                            if img:
                                star_name = img.get('title') or img.get('alt') or ''
                        
                        # 依然为空，尝试链接的 title
                        star_link = star_div.find('a')
                        if not star_name and star_link:
                             star_name = star_link.get('title') or ''
                             
                        star_url = star_link.get('href', '') if star_link else ''
                        star_id = star_url.strip('/').split('/')[-1] if star_url else ''
                        
                        if star_name: # 只添加有名字的
                            detail['stars'].append({
                                "name": self.translate_to_chinese(star_name) if perform_translate else star_name,
                                "id": star_id,
                                "url": star_url if star_url.startswith('http') else domain_prefix + (star_url if star_url.startswith('/') else '/' + star_url)
                            })
            
            # 策略 3: 基于文本查找 (最粗暴的兜底)
            # 有些页面结构混乱，直接找 "演員" / "出演" 后面的链接
            if not detail['stars']:
                 try:
                     # 查找包含特定文本的 header
                     keywords = ["演員", "出演", "出演者", "Cast", "Stars"]
                     star_header = soup.find(lambda t: t.name in ["span", "p", "div"] and any(k in t.text for k in keywords) and len(t.text) < 20)
                     
                     if star_header:
                         # 1. 尝试在父级段落中找链接
                         parent = star_header.parent
                         if parent:
                             links = parent.find_all('a')
                             
                             # 如果父级没找到，或者是顶级元素，尝试查找后续兄弟元素
                             if not links or len(links) <= 1: # <=1 是防止只有 header 自己的链接
                                 next_sibling = parent.find_next_sibling()
                                 if next_sibling:
                                     links = next_sibling.find_all('a')
                             
                             for a in links:
                                 name = a.get_text(strip=True)
                                 href = a.get('href', '')
                                 if name and href and ('/star/' in href or '/uncensored/star/' in href):
                                     detail['stars'].append({
                                         "name": self.translate_to_chinese(name) if perform_translate else name,
                                         "id": href.strip('/').split('/')[-1],
                                         "url": href if href.startswith('http') else domain_prefix + href
                                     })
                 except Exception as e:
                     logger.warning(f"Strategy 3 actor extraction failed: {e}")

            # 去重
            seen_stars = set()
            unique_stars = []
            for s in detail['stars']:
                if s['id'] not in seen_stars:
                    seen_stars.add(s['id'])
                    unique_stars.append(s)
            detail['stars'] = unique_stars

            # 提取基本信息
            info_div = soup.find('div', class_='info')
            if info_div:
                for p in info_div.find_all('p'):
                    # 尝试寻找 header span (更精确)
                    header = p.find('span', class_='header')
                    if header:
                        key = header.get_text(strip=True).rstrip(':')
                        # Value 是 header 之后的所有内容
                        # 移除 header 自身，获取剩余 text
                        # 但不能直接 modify p，所以复制 logic
                        value_parts = []
                        for sibling in header.next_siblings:
                            if hasattr(sibling, 'get_text'):
                                value_parts.append(sibling.get_text(strip=True))
                            elif isinstance(sibling, str):
                                value_parts.append(sibling.strip())
                        value = " ".join([v for v in value_parts if v])
                    else:
                        # Fallback: 简单的文本分割
                        text = p.get_text(strip=True)
                        if ':' in text:
                            key, value = text.split(':', 1)
                        else:
                            continue

                    key = key.strip()
                    if not key: continue

                    # 翻译键名 (如 "識別碼", "發行日期", "導演", "製作商", "發行商", "系列")
                    translated_key = self.translate_to_chinese(key) if perform_translate else key
                    
                    # 对于演员/制作商等值，如果不是中文也尝试翻译
                    translated_value = value.strip()
                    
                    # 如果包含日文假名，尝试翻译 (针对 "メーカー" (Manufacturer), "レーベル" (Label) 等值的翻译)
                    # 注意: 系列、制作商 通常是链接，前面已经提取了文本
                    if perform_translate and re.search(r'[\u3040-\u309f\u30a0-\u30ff]', translated_value):
                         translated_value = self.translate_to_chinese(translated_value)
                         
                    detail['info'][translated_key] = translated_value
            
            # 提取类型标签 (Genres)
            # 提取类型标签 (Genres)
            detail['genres'] = []
            # 策略 1: 标准 CSS Selector
            genre_elements = soup.select('.genre a, p.header + p a[href*="/genre/"]')
            
            # 策略 2: 基于文本的 Header 查找 (针对不规范页面)
            if not genre_elements:
                 try:
                     keywords = ["類別", "类别", "Genre", "Category"]
                     genre_header = soup.find(lambda t: t.name in ["span", "p"] and any(k in t.text for k in keywords) and len(t.text) < 20)
                     if genre_header:
                         parent = genre_header.parent
                         if parent:
                             # 查找父元素下所有指向 /genre/ 的链接
                             links = parent.find_all('a', href=re.compile(r'/genre/'))
                             # 如果父元素没找到，找兄弟元素
                             if not links:
                                 next_p = parent.find_next_sibling('p')
                                 if next_p:
                                     links = next_p.find_all('a', href=re.compile(r'/genre/'))
                             
                             if links:
                                 genre_elements = links
                 except:
                     pass

            if genre_elements:
                genres_list = []
                genre_texts = [] # 仅用于批量翻译
                
                for g in genre_elements:
                    text = g.get_text(strip=True)
                    href = g.get('href', '')
                    # 提取 ID (例如 /genre/4g -> 4g)
                    g_id = href.strip('/').split('/')[-1] if href else ''
                    
                    if text and g_id:
                         genres_list.append({"name": text, "id": g_id})
                         genre_texts.append(text)
                
                # 批量翻译
                if perform_translate and genre_texts:
                    translated_texts = self.batch_translate_to_chinese(genre_texts)
                    # 将翻译结果回填
                    for i, g_obj in enumerate(genres_list):
                        if i < len(translated_texts):
                            g_obj['name'] = translated_texts[i]
                
                # 去重 (按 ID)
                seen_ids = set()
                unique_genres = []
                for g in genres_list:
                    if g['id'] not in seen_ids:
                        seen_ids.add(g['id'])
                        unique_genres.append(g)
                        
                detail['genres'] = unique_genres # 返回对象列表
            else:
                 detail['genres'] = []
            
            # 提取磁力链接 - JavBus 的磁力链接通过 Ajax 加载，需要特定的 gid 和 uc 变量
            try:
                # 尝试多种策略寻找 gid
                gid_match = re.search(r'var gid = (\d+);', response.text)
                if not gid_match:
                    gid_match = re.search(r'gid\s*=\s*(\d+)', response.text)
                
                uc_match = re.search(r'var uc = (\d+);', response.text)
                img_match = re.search(r'var img = [\'"](.+?)[\'"];', response.text)
                
                if gid_match:
                    gid = gid_match.group(1)
                    uc = uc_match.group(1) if uc_match else "0"
                    img = img_match.group(1) if img_match else ""
                    
                    # 关键：增加 floor 模拟及 Referer，这是 Ajax 校验的核心
                    # 使用 domain_prefix 而非 self.base_url，确保 AJAX 域名一致
                    ajax_url = f"{domain_prefix}/ajax/uncledatoolsbyajax.php?gid={gid}&lang=zh&img={img}&uc={uc}&floor={random.randint(100, 999)}"
                    ajax_headers = {
                        "Referer": url,
                        "X-Requested-With": "XMLHttpRequest"
                    }
                    
                    # 发起 AJAX 请求
                    ajax_response = self.session.get(ajax_url, headers=ajax_headers, timeout=10)
                    ajax_soup = self.parse_html(ajax_response)
                    logger.info(f"成功获取磁力 AJAX，长度: {len(ajax_response.text)}")
                else:
                    logger.warning(f"无法在页面中识别出 gid: {url}")
                    ajax_soup = BeautifulSoup("", 'html.parser')
                
                # 解析磁力链接表格
                for tr in ajax_soup.find_all('tr'):
                    tds = tr.find_all('td')
                    if len(tds) >= 3:
                        link_td = tds[0]
                        size_td = tds[1]
                        date_td = tds[2]
                        
                        magnet_link = link_td.find('a')
                        if magnet_link:
                            magnet_url = magnet_link.get('href', '')
                            m_title = magnet_link.get_text(strip=True)
                            size = size_td.get_text(strip=True)
                            date = date_td.get_text(strip=True)
                            
                            # 解析磁力链接信息
                            magnet_info = self._parse_magnet_info(m_title, size, magnet_url)
                            magnet_info['date'] = date
                            
                            detail['magnets'].append(magnet_info)
                
                # detail['magnets'] = magnets # already done in loop
            except Exception as e:
                logger.error(f"提取磁力链接解析过程中出错: {e}")

            # 剧照功能已删除

            # 保存详情到数据库 (更新现有电影)
            movie_id = url.split('/')[-1]
            db_movie = {
                "id": movie_id,
                "detail": detail
            }
            if detail.get('title'):
                db_movie['title'] = detail['title']
            if detail.get('cover'):
                db_movie['cover'] = detail['cover']
                
            db.save_movie(db_movie)
            
            return detail
        
        except Exception as e:
            logger.error(f"捕获电影详情出错: {e}")
            # 如果网络失败，尝试从数据库获取
            return self._cached_movie_detail(url) or {"magnets": [], "info": {}}

    @staticmethod
    def _cached_movie_detail(url: str) -> Optional[Dict]:
        """网络取不到详情时，退回库里已经存过的结构化详情（movies.detail）。"""
        cached = db.get_movie(url.split('/')[-1])
        if cached and cached.get('detail'):
            return cached['detail']
        return None

    @staticmethod
    def strip_title_code_prefix(title: Any, code: Any) -> str:
        """去掉标题前面粘着的番号（"ABC-123 标题" -> "标题"）。

        详情页 h3 拿到的整串是「番号 + 标题」，而翻译缓存是按**纯标题**存的键，
        不剥掉就永远查不中已翻好的译文（详情页因此一直显示日文原文）。
        """
        text = str(title or "").strip()
        prefix = str(code or "").strip()
        if not text or not prefix:
            return text
        if text.upper().startswith(prefix.upper()):
            rest = text[len(prefix):].lstrip()
            if rest:
                return rest
        return text

    @staticmethod
    def adapt_r18_cached_detail(detail: Dict, movie: Optional[Dict] = None) -> Dict:
        """把 r18 源的缓存详情补成前端能渲染的形状。

        全库 99.5% 的片子是 r18 源，它们的 detail_json 是另一套结构
        （title_ja / gallery / tags / maker），不映射的话详情页除标题外一片空白。
        """
        if detail.get("source") != "r18" and not detail.get("r18_content_id"):
            return detail

        adapted = dict(detail)

        info = adapted.get("info")
        if not isinstance(info, dict) or not info:
            maker = adapted.get("maker") or {}
            label = adapted.get("label") or {}
            series = adapted.get("series") or {}
            runtime = str(adapted.get("runtime_mins") or "").strip()
            built = {
                "识别码": str(adapted.get("code") or adapted.get("dvd_id") or "").strip(),
                "时长": f"{runtime}分钟" if runtime else "",
                "制作商": str(maker.get("name_ja") or "").strip(),
                "发行商": str(label.get("name_ja") or "").strip(),
                "系列": str(series.get("name_ja") or "").strip(),
            }
            adapted["info"] = {key: value for key, value in built.items() if value}

        if not adapted.get("cover"):
            gallery = adapted.get("gallery") or {}
            cover = (
                gallery.get("full_first")
                or gallery.get("thumb_first")
                or gallery.get("full_last")
                or gallery.get("thumb_last")
                or (movie or {}).get("cover")
            )
            if cover:
                adapted["cover"] = cover

        if not adapted.get("genres") and adapted.get("tags"):
            adapted["genres"] = adapted["tags"]

        # r18 不提供磁力，给个空列表，前端就不会显示下载区块
        if not adapted.get("magnets"):
            adapted["magnets"] = []

        return adapted

    def get_movie_detail_cached_first(self, url: str, movie_id: Optional[str] = None) -> Optional[Dict]:
        """详情页优先渲染库里已缓存的结构化详情（detail_json，全库 99.6% 覆盖）。

        标题走和列表页 cache_only 一样的三级降级：已有译文 > 库里标题 > 原文。
        缓存里没有译文就把原文排进翻译队列（高优先级），下次打开就是中文。
        库里没有这部片子才返回 None，由调用方回退实时抓取。
        """
        code = str(movie_id or "").strip() or str(url or "").rstrip("/").split("/")[-1].strip()
        if not code:
            return None

        movie = db.get_movie(code)
        if not movie:
            return None

        detail = movie.get("detail")
        if not detail:
            return None

        detail = self.adapt_r18_cached_detail(dict(detail), movie)
        raw = str(detail.get("original_title") or detail.get("title") or detail.get("title_ja") or "").strip()
        # ⚠️ 有些条目只有 title_en（英文名），上面三个字段全空时 raw 会是空串，
        # 最终 detail["title"] 也变空 —— 页面标题直接消失。必须回退到可用字段，
        # 宁可显示英文也不要显示空白。
        if not raw:
            raw = str(detail.get("title_en") or detail.get("title_ja") or "").strip()
        pure = self.strip_title_code_prefix(raw, code)
        if not pure:
            pure = code

        cached_translation = normalize_simplified_title(db.get_translation(pure), "") if pure else ""
        if self._is_usable_translation(cached_translation, pure):
            display = cached_translation
            source = "cache"
        else:
            library_title = self.strip_title_code_prefix(
                normalize_simplified_title(movie.get("title"), ""), code
            )
            if self._is_usable_translation(library_title, pure):
                display = library_title
                source = "library"
            else:
                display = pure
                source = "pending"
                # 没译文就排进翻译队列，别让用户一直看原文
                if pure and re.search(r"[\u3040-\u30ff]", pure):
                    try:
                        self.queue_title_translations([pure], priority=True)
                    except Exception as exc:
                        logger.warning("详情标题入队翻译失败: %s", type(exc).__name__)

        detail["title"] = display
        detail["original_title"] = pure
        detail["translation_source"] = source
        return detail

    @staticmethod
    def _is_usable_translation(candidate: str, original: str) -> bool:
        """判断候选标题能否当作"中文标题"使用。

        早期只判断「候选 != 原文」，于是**英文标题也被当成译文接受**
        （实测 `translation_source=library` 却显示英文，页面看着像没继承中文名）。
        这里补一条硬条件：必须含 CJK 字符，否则英文/数字标题不算译文。
        """
        text = str(candidate or "").strip()
        if not text:
            return False
        if not re.search(r"[\u4e00-\u9fff\u3400-\u4dbf]", text):
            return False
        return text != str(original or "").strip()

    def _parse_magnet_info(self, title: str, size: str, magnet_url: str) -> Dict:
        """
        解析磁力链接信息，提取分辨率、字幕等
        """
        info = {
            "title": title,
            "size": size,
            "magnet": magnet_url,
            "resolution": None,
            "has_subtitle": False,
            "size_bytes": self._parse_size_to_bytes(size)
        }
        
        # 检测分辨率
        title_upper = title.upper()
        if '4K' in title_upper or '2160P' in title_upper:
            info['resolution'] = '4K'
        elif '1080P' in title_upper or 'FHD' in title_upper:
            info['resolution'] = '1080P'
        elif '720P' in title_upper or 'HD' in title_upper:
            info['resolution'] = '720P'
        elif '480P' in title_upper or 'SD' in title_upper:
            info['resolution'] = '480P'
        
        # 检测字幕
        subtitle_keywords = ['字幕', 'SUB', 'subtitle', '中文', 'CH', 'CHS']
        for keyword in subtitle_keywords:
            if keyword.upper() in title_upper:
                info['has_subtitle'] = True
                break
        
        return info

    def _parse_size_to_bytes(self, size_str: str) -> int:
        """将文件大小字符串转换为字节数"""
        try:
            size_str = size_str.upper().strip()
            
            # 提取数字和单位
            match = re.match(r'([\d.]+)\s*([KMGT]?B)', size_str)
            if not match:
                return 0
            
            number = float(match.group(1))
            unit = match.group(2)
            
            multipliers = {
                'B': 1,
                'KB': 1024,
                'MB': 1024 ** 2,
                'GB': 1024 ** 3,
                'TB': 1024 ** 4
            }
            
            return int(number * multipliers.get(unit, 1))
        except:
            return 0

    def select_best_magnet(self, magnets: List[Dict], preference: str = '1080P') -> Optional[Dict]:
        """
        根据用户偏好智能选择最佳磁力链接
        
        优先级：
        1. 匹配用户偏好的分辨率
        2. 同分辨率下优先选择有字幕的
        3. 无字幕时选择文件最小的
        """
        if not magnets:
            return None
        
        # 按分辨率分组
        resolution_groups = {}
        for magnet in magnets:
            res = magnet.get('resolution', 'Unknown')
            if res not in resolution_groups:
                resolution_groups[res] = []
            resolution_groups[res].append(magnet)
        
        # 获取目标分辨率组
        target_group = resolution_groups.get(preference, [])
        
        # 如果没有找到偏好分辨率，尝试降级
        if not target_group:
            fallback_order = ['1080P', '720P', '4K', '480P']
            for res in fallback_order:
                if res in resolution_groups:
                    return self.select_best_magnet(magnets, preference=res)
            
            # 最后选文件最大的（通常质量最高）
            return max(magnets, key=lambda x: x.get('size_bytes', 0))

        # 在目标组中选择
        # 优先选择有字幕的
        with_subtitle = [m for m in target_group if m.get('has_subtitle')]
        if with_subtitle:
            # 有字幕的情况下，选择文件最大的 (优先画质)
            return max(with_subtitle, key=lambda x: x.get('size_bytes', 0))
        
        # 没有字幕，选择文件最大的
        return max(target_group, key=lambda x: x.get('size_bytes', 0))

    def backfill_actress_from_jellyfin(self, actress_name: str) -> bool:
        """
        尝试从 Jellyfin 补全女优信息（头像、简介等）
        """
        try:
            person = jellyfin_client.get_person_info(actress_name)
            if not person:
                # 尝试通过搜索匹配（应对别名情况）
                results = jellyfin_client.search_people(actress_name)
                if results:
                    # 匹配第一个名字相同的
                    target = next((item for item in results if item.get("Name") == actress_name), None)
                    if not target: target = results[0] # 保底选第一个
                    person = jellyfin_client.get_person_info(target.get("Name"))

            if person:
                # 更新到数据库
                # 注意：这里需要先知道 actress_id 才能准确存入。
                # 通常我们可以通过姓名查找已存在于库中的女优。
                conn = db.get_connection()
                try:
                    cursor = conn.cursor()
                    sql = "SELECT id FROM actresses WHERE name = %s" if db.is_postgres else "SELECT id FROM actresses WHERE name = ?"
                    cursor.execute(sql, (actress_name,))
                    row = cursor.fetchone()
                    if row:
                        a_id = row["id"]
                        data = {
                            "id": a_id,
                            "name": actress_name,
                            "bio": person.get("bio"),
                            "avatar": person.get("avatar") # 如果原图由于域名封锁不可用，Jellyfin 的图是极佳替代
                        }
                        return db.save_actress(data)
                finally:
                    db._release_connection(conn)
            return False
        except Exception as e:
            logger.error(f"Jellyfin Backfill Error for {actress_name}: {e}")
            return False

    def get_actress_info(self, actress_id: str) -> Optional[Dict]:
        """
        抓取单个女优的详情页信息，并尝试从 Jellyfin 补全
        """
        try:
            url = f"{self.base_url}/star/{actress_id}"
            logger.info(f"正在抓取女优详情: {url}")
            
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            soup = self.parse_html(response)
            
            # 解析头像和基本属性
            photo_info = soup.find('div', class_='photo-info')
            if not photo_info:
                # 尝试无码结构
                url = f"{self.base_url}/uncensored/star/{actress_id}"
                response = self.session.get(url, timeout=15)
                soup = self.parse_html(response)
                photo_info = soup.find('div', class_='photo-info')
                
            if not photo_info:
                logger.warning(f"无法找到女优详情容器: {actress_id}")
                return None
                
            img_elem = photo_info.find('img')
            avatar = img_elem.get('src', '') if img_elem else ''
            if avatar and avatar.startswith('/'):
                avatar = self.base_url + avatar
                
            name_elem = photo_info.find('span', class_='pb10')
            name = name_elem.get_text(strip=True) if name_elem else ''
            
            if is_suspicious_actress_name(name):
                logger.warning(f"跳过异常女优详情: {name[:40]}...")
                return None
            
            props = {}
            for p in photo_info.find_all('p'):
                text = p.get_text(strip=True)
                if ':' in text:
                    k, v = text.split(':', 1)
                    props[k.strip()] = v.strip()
            
            birth_date = props.get('生日', '')
            if birth_date and '0000' in birth_date:
                birth_date = ''

            actress_data = {
                "id": actress_id,
                "name": name,
                "avatar": avatar,
                "birth_date": birth_date,
                "height": props.get('身高', ''),
                "cup": props.get('罩杯', ''),
                "bust": props.get('胸圍', ''),
                "waist": props.get('腰圍', ''),
                "hip": props.get('臀圍', ''),
                "hometown": props.get('出生地', ''),
                "hobby": props.get('愛好', ''),
                "page_url": url
            }
            
            # [NEW] 尝试从 Jellyfin 补全 Bio 和更好的图
            try:
                person = jellyfin_client.get_person_info(name)
                if person:
                    actress_data["bio"] = person.get("bio")
                    if person.get("avatar"):
                        actress_data["avatar"] = person.get("avatar")
            except: pass

            db.save_actress(actress_data)
            return actress_data
            
        except Exception as e:
            logger.error(f"抓取女优 {actress_id} 详情失败: {e}")
            return None

    def get_actress_list(self, url: str, perform_translate: bool = False) -> List[Dict]:
        """
        获取女优列表
        """
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            soup = self.parse_html(response)
            
            actresses = []
            waterfall = soup.find('div', id='waterfall')
            if not waterfall:
                return []
            
            items = waterfall.find_all('div', class_='item')
            raw_actresses = []
            for item in items:
                try:
                    img_elem = item.find('img')
                    avatar = img_elem.get('src', '') if img_elem else ''
                    if avatar and avatar.startswith('/'):
                        avatar = self.base_url + avatar
                        
                    name_elem = item.find('span')
                    name = name_elem.get_text(strip=True) if name_elem else ''
                    
                    link_elem = item.find('a', class_='avatar-box')
                    page_url = link_elem.get('href', '') if link_elem else ''
                    if page_url and not page_url.startswith('http'):
                        page_url = self.base_url + page_url
                    
                    actress_id = page_url.strip('/').split('/')[-1] if page_url else ''
                    
                    # 获取详细属性 (在头像下面的列表里)
                    props = {}
                    p_elems = item.find_all('p')
                    for p in p_elems:
                        text = p.get_text(strip=True)
                        if ':' in text:
                            k, v = text.split(':', 1)
                            props[k.strip()] = v.strip()
                    
                    # 格式化生日
                    birth_date = props.get('生日', '')
                    if birth_date and '0000' in birth_date:
                        birth_date = '' # 无效日期

                    raw_actresses.append({
                        "id": actress_id,
                        "name": name,
                        "avatar": avatar,
                        "birth_date": birth_date,
                        "height": props.get('身高', ''),
                        "cup": props.get('罩杯', ''),
                        "bust": props.get('胸圍', ''),
                        "waist": props.get('腰圍', ''),
                        "hip": props.get('臀圍', ''),
                        "hometown": props.get('出生地', ''),
                        "hobby": props.get('愛好', ''),
                        "page_url": page_url
                    })
                except Exception as e:
                    logger.warning(f"解析单个女优项失败: {e}")
                    continue
            
            # [NEW] 批量翻译姓名 (如果在后台抓取，提前翻译好存入缓存)
            if perform_translate and raw_actresses:
                names = [a['name'] for a in raw_actresses]
                translated_names = self.batch_translate_to_chinese(names)
                for i, a in enumerate(raw_actresses):
                    a['name'] = translated_names[i]
            
            # [FILTER] 过滤掉姓名异常超长的项 (双保险)
            raw_actresses = [a for a in raw_actresses if a['name'] and len(a['name']) <= 10]

            # 保存到数据库
            for a in raw_actresses:
                db.save_actress(a)
                actresses.append(a)
                    
            return actresses
        except Exception as e:
            logger.error(f"获取女优列表出错: {e}")
            return []
        except Exception as e:
            logger.error(f"获取女优列表出错: {e}")
            return []

    def get_image(self, url: str):
        """
        获取图片内容，用于代理
        """
        try:
            # 检查是否有 Referer，必须带斜杠
            headers = self.session.headers.copy()
            # 尝试根据图片 URL 构造 Referer，或者是 BaseURL
            headers['Referer'] = self.base_url + '/'
            
            # 默认校验 SSL；仅当显式开启 SCRAPER_ALLOW_INSECURE_IMAGE_FETCH 时，
            # 才在证书错误时降级为不校验（与 organizer.py 的图床策略一致）。
            try:
                response = self.session.get(url, stream=True, timeout=20, headers=headers, verify=True)
            except requests.exceptions.SSLError:
                if not getattr(config, "SCRAPER_ALLOW_INSECURE_IMAGE_FETCH", False):
                    raise
                response = self.session.get(url, stream=True, timeout=20, headers=headers, verify=False)
            response.raise_for_status()
            return response
        except Exception as e:
            logger.error(f"获取图片 {url} 时出错: {e}")
            return None

    def crawl_recent_movies(self, days: int = 60):
        """
        爬取最近 N 天的所有电影
        """
        logger.info(f"开始抓取最近 {days} 天的电影...")
        current_page = 1
        cutoff_date = datetime.now() - timedelta(days=days)
        reached_cutoff = False
        
        total_scraped = 0
        
        while not reached_cutoff:
            logger.info(f"正在抓取第 {current_page} 页...")
            
            # 限流：随机等待
            time.sleep(random.uniform(2, 5))
            
            movies = self.get_movies(self.base_url, current_page)
            if not movies:
                break
                
            for movie in movies:
                date_str = movie.get('date', '')
                try:
                    # 日期格式通常为 YYYY-MM-DD
                    if date_str:
                        m_date = datetime.strptime(date_str, "%Y-%m-%d")
                        if m_date < cutoff_date:
                            reached_cutoff = True
                            # 继续完成本页但之后停止
                except ValueError:
                    continue
            
            total_scraped += len(movies)
            current_page += 1
            
            # 安全中断
            if current_page > 100: 
                break
                
        logger.info(f"抓取任务结束。共抓取 {total_scraped} 部电影。")

    def refresh_category(self, url: str, max_pages: int = 5) -> int:
        """
        刷新指定分类/URL的影片
        策略：逐页抓取，直到遇到已缓存的数据（即该页所有影片都在库中），或达到 max_pages
        """
        try:
            # 确保 URL 是完整路径
            if not url.startswith('http'):
                url = self.base_url + (url if url.startswith('/') else '/' + url)
                
            logger.info(f"[Category Refresh] 开始刷新分类: {url} (Max {max_pages} pages)")
            
            total_new = 0
            for page in range(1, max_pages + 1):
                # 强制从源站抓取，不使用缓存
                movies = self.get_movies(url, page=page, limit=30, force_refresh=True)
                
                if not movies:
                    logger.info(f"[Category Refresh] 第 {page} 页无数据，停止刷新")
                    break
                
                new_in_page = 0
                for m in movies:
                    m_id = m.get('id')
                    if not m_id: continue
                    
                    # 检查是否存在
                    if not db.get_movie(m_id):
                        db.save_movie(m)
                        new_in_page += 1
                
                total_new += new_in_page
                logger.info(f"[Category Refresh] {url} - 第 {page} 页: 发现 {len(movies)} 部, 新增 {new_in_page} 部")
                
                # 策略：如果本页没有任何一部新片，说明已经追上进度了，停止翻页
                # (仅在非第一页时生效，防止第一页即全满但用户想确认的情况？不，第一页全满也说明不用翻了)
                if new_in_page == 0:
                    logger.info("[Category Refresh] 命中缓存断点（本页无新片），停止后续抓取")
                    break
                    
                # 简单的防风控延迟
                time.sleep(random.uniform(1.5, 3.0))
                
            return total_new
            
        except Exception as e:
            logger.error(f"[Category Refresh] 刷新分类失败 {url}: {e}")
            return 0
    
    def refresh_homepage(self) -> int:
        """
        刷新首页，并行抓取前 3 页，仅保存数据库中不存在的新影片。
        如果发现数据库中已存在的影片，说明已追上进度（但在并发情况下仍会抓完设定页数）。
        """
        try:
            logger.info("[Homepage Refresh] 开始刷新首页 (并行抓取前 3 页)...")
            from concurrent.futures import ThreadPoolExecutor, as_completed
            
            # 并行抓取前 3 页
            pages_to_scrape = [1, 2, 3]
            all_movies = []
            
            with ThreadPoolExecutor(max_workers=3) as executor:
                # 提交任务
                future_to_page = {
                    executor.submit(self.get_movies, self.base_url, page=p, limit=30, force_refresh=True, perform_translate=False): p 
                    for p in pages_to_scrape
                }
                
                for future in as_completed(future_to_page):
                    p = future_to_page[future]
                    try:
                        movies = future.result()
                        if movies:
                            all_movies.extend(movies)
                            logger.info(f"[Homepage Refresh] 第 {p} 页抓取完成，获 {len(movies)} 部影片")
                    except Exception as e:
                        logger.error(f"[Homepage Refresh] 第 {p} 页抓取失败: {e}")
            
            new_count = 0
            # 倒序处理（理论上页码大的更旧，但并行后顺序不保，无所谓，ID去重即可）
            # 其实应该按日期排序？这里主要为了存入新片。
            
            for movie in all_movies:
                movie_id = movie.get('id')
                if not movie_id:
                    continue
                
                # Check if exists
                existing = db.get_movie(movie_id)
                if not existing:
                    # New movie
                    db.save_movie(movie)
                    new_count += 1
                    logger.info(f"[Homepage Refresh] 发现新影片: {movie_id}")
            
            logger.info(f"[Homepage Refresh] 刷新完成，共处理 {len(all_movies)} 部数据，新增 {new_count} 部影片")
            return new_count
            
        except Exception as e:
            logger.error(f"[Homepage Refresh] 刷新首页时出错: {e}")
            return 0

    def _parse_wiki_infobox(self, url: str) -> Dict[str, str]:
        """
        [NEW] 直接抓取 Wiki HTML 页面并解析 Infobox 表格
        因为 Wikipedia API 不返回这些模板数据。
        """
        info = {}
        try:
            logger.info(f"正在从 Wiki HTML 解析详细属性: {url}")
            # Requests page
            # 走 self.session（header 已在 session 上，且 reload_config 把 system_proxy
            # 写在 session.proxies 里）。用裸 requests.get 会绕过配置的出网代理。
            resp = self.session.get(url, timeout=10)
            if resp.status_code != 200:
                return info
            
            soup = self.parse_html(resp)
            # Infobox class usually "infobox"
            infobox = soup.find('table', class_='infobox')
            if not infobox:
                return info
            
            # Iterate rows
            rows = infobox.find_all('tr')
            for row in rows:
                th = row.find('th')
                td = row.find('td')
                if not th or not td:
                    continue
                
                key = th.get_text(strip=True)
                val = td.get_text(strip=True)
                
                # Parse logic based on keys (Japanese Wiki)
                if '生年月日' in key:
                    # Filter date 19xx年x月x日
                    match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', val)
                    if not match: match = re.search(r'(\d{4})-(\d{1,2})-(\d{1,2})', val)
                    if match:
                        info['birth_date'] = f"{match.group(1)}-{match.group(2).zfill(2)}-{match.group(3).zfill(2)}"
                        
                elif '身長' in key:
                    # 159 cm ...
                    match = re.search(r'(\d{2,3})[\s]*cm', val)
                    if match:
                        info['height'] = match.group(1)
                        
                elif 'スリーサイズ' in key or 'サイズ' in key:
                    # 83 - 60 - 85 cm
                    nums = re.findall(r'(\d{2,3})', val)
                    if len(nums) >= 3:
                        info['bust'] = nums[0]
                        info['waist'] = nums[1]
                        info['hip'] = nums[2]
                        
                elif 'ブラサイズ' in key or 'カップ' in key:
                    # C or Cカップ
                    match = re.search(r'([A-Z])', val.upper())
                    if match:
                        info['cup'] = match.group(1)
                        
                elif '血液型' in key and not info.get('blood_type'):
                     # A型
                     match = re.search(r'([ABO]+)', val.upper())
                     if match:
                         info['blood_type'] = match.group(1)

            logger.info(f"Wiki 详细属性解析成功: {info}")
            return info
        except Exception as e:
            logger.error(f"解析 Wiki Infobox 失败: {e}")
            return info

    def get_actress_bio_from_wikipedia(self, actress_name: str) -> Dict[str, str]:
        """
        从维基百科获取演员简介和详细信息
        """
        try:
            # 1. 清理名字
            # 移除括号及其内容 (例如: "波多野结衣 (AV女优)")
            cleaned_name = re.sub(r'\s*\(.*?\)', '', actress_name).strip()
            # 移除所有非中日英文及数字的特殊符号，但保留空格
            cleaned_name = re.sub(r'[^\w\s\u4e00-\u9fa5\u3040-\u309f\u30a0-\u30ff]', '', cleaned_name)
            
            if not cleaned_name:
                return {}

            logger.info(f"正在从维基百科搜索演员: {cleaned_name}")
            
            # Helper to process page result
            def process_page(page_obj):
                data = {
                    "bio": "",
                    "wiki_url": page_obj.fullurl,
                    "alias": page_obj.title if page_obj.title != actress_name else "",
                    "extra_info": {}
                }
                
                # 1. Summary + Translation
                summary = page_obj.summary[:1000] if page_obj.summary else ""
                if summary:
                    data["bio"] = self.translate_to_chinese(summary)
                
                # 2. [NEW] Parse HTML Infobox for stats
                data["extra_info"] = self._parse_wiki_infobox(page_obj.fullurl)
                
                return data

            # 2. 核心搜索策略
            search_names = [cleaned_name]
            
            # 优先尝试日文维基 (对 AV 女优最全)
            for name_variant in search_names:
                page_ja = self.wiki_ja.page(name_variant)
                if page_ja.exists():
                    return process_page(page_ja)
                
                # 中文名转日文名重试
                if re.search(r'[\u4e00-\u9fa5]', name_variant):
                    try:
                        from deep_translator import GoogleTranslator
                        ja_name = GoogleTranslator(source='auto', target='ja').translate(name_variant)
                        if ja_name and ja_name != name_variant:
                            logger.info(f"尝试将中文名 [{name_variant}] 转换为日文名 [{ja_name}] 再次搜索...")
                            page_ja_retry = self.wiki_ja.page(ja_name)
                            if page_ja_retry.exists():
                                return process_page(page_ja_retry)
                    except Exception as te:
                        logger.debug(f"Back-translation failed: {te}")

            # 3. 尝试英文维基
            page_en = self.wiki_en.page(cleaned_name)
            if page_en.exists():
                # English page logic (infobox format might differ but method handles it roughly)
                return process_page(page_en)
            
            logger.info(f"未在维基百科找到演员 {cleaned_name} 的信息")
            return {}
            
        except Exception as e:
            logger.error(f"从维基百科获取演员信息时出错: {e}")
            return {}


# 单例实例
scraper = JavBusScraper()
