import axios from 'axios';
import { mapApiErrorMessage } from './utils/errorMessages.js';

const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '::1']);
const SENSITIVE_TEXT_PATTERNS = [
    /(["']?(?:api[_-]?key|token|secret|password|passwd|pwd|authorization|x-emby-token|x-mediabrowser-token|jellyfin_api_key|baidu_secret|email_pass|system_proxy|qb_password)["']?\s*[:=]\s*)(["']?)([^,\s"'\]}]+)(["']?)/gi,
    /([?&](?:api[_-]?key|token|secret|password|passwd|pwd)=)[^&\s]+/gi,
    /\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+/gi,
    /\b[A-Za-z]:[\\/][^\s"',;<>]+/g,
    /(?:\/mnt|\/media|\/volume\d*|\/home|\/Users|\/app\/data|\/app\/downloads)[^\s"',;<>]*/g,
];

export const redactText = (value) => {
    let text = value == null ? '' : String(value);
    text = text.replace(/(["']?authorization["']?\s*[:=]\s*)(["']?)(?:Bearer|Basic)\s+([^,\s"'\]}]+)(["']?)/gi, '$1$2***$4');
    text = text.replace(/(["']?(?:api[_-]?key|token|secret|password|passwd|pwd|authorization|x-emby-token|x-mediabrowser-token|jellyfin_api_key|baidu_secret|email_pass|system_proxy|qb_password)["']?\s*[:=]\s*)(["'])(.*?)(\2)/gi, '$1$2***$4');
    text = text.replace(SENSITIVE_TEXT_PATTERNS[0], '$1$2***$4');
    text = text.replace(SENSITIVE_TEXT_PATTERNS[1], '$1***');
    text = text.replace(SENSITIVE_TEXT_PATTERNS[2], '$1 ***');
    text = text.replace(SENSITIVE_TEXT_PATTERNS[3], '<path redacted>');
    text = text.replace(SENSITIVE_TEXT_PATTERNS[4], '<path redacted>');
    return text;
};

const isSensitiveKey = (key) =>
    /api[_-]?key|token|secret|password|passwd|pwd|authorization|x-emby-token|x-mediabrowser-token|jellyfin_api_key|baidu_secret|email_pass|system_proxy|qb_password/i.test(String(key));

const redactValue = (value, seen = new WeakSet()) => {
    if (value == null) return value;
    if (typeof value === 'string') return redactText(value);
    if (typeof value !== 'object') return value;

    if (seen.has(value)) return '[Circular]';
    seen.add(value);

    if (Array.isArray(value)) {
        return value.map((item) => redactValue(item, seen));
    }

    return Object.fromEntries(
        Object.entries(value).map(([key, nestedValue]) => [
            key,
            isSensitiveKey(key) ? '***' : redactValue(nestedValue, seen),
        ])
    );
};

export const getSafeApiErrorSummary = (error) => {
    const responseData = error?.response?.data;
    const serverMessage = responseData?.detail || responseData?.message || error?.message;
    return {
        status: error?.response?.status,
        method: error?.config?.method?.toUpperCase(),
        url: redactText(error?.config?.url),
        message: redactText(serverMessage),
    };
};

export const logSafeError = (label, error) => {
    console.error(label, getSafeApiErrorSummary(error));
};

const sanitizeApiError = (error) => {
    if (!error || typeof error !== 'object') return error;

    if (error.config) {
        error.config = {
            ...error.config,
            url: redactText(error.config.url),
            baseURL: redactText(error.config.baseURL),
            headers: redactValue(error.config.headers),
            params: redactValue(error.config.params),
            data: redactValue(error.config.data),
        };
    }

    if (error.response?.config) {
        error.response.config = redactValue(error.response.config);
    }

    if (error.response?.data) {
        error.response.data = redactValue(error.response.data);
    }

    if (error.message) {
        error.message = redactText(error.message);
    }

    return error;
};

const normalizeApiBaseUrl = (rawUrl) => {
    if (!rawUrl || typeof window === 'undefined') return rawUrl || '';

    try {
        const apiUrl = new URL(rawUrl);
        const pageHost = window.location.hostname;

        if (LOCAL_HOSTS.has(apiUrl.hostname) && LOCAL_HOSTS.has(pageHost)) {
            return '';
        }
    } catch {
        return rawUrl;
    }

    return rawUrl;
};

export const API_BASE_URL = normalizeApiBaseUrl(import.meta.env?.VITE_API_URL || '');
const normalizedApiBase = API_BASE_URL === '/' ? '' : API_BASE_URL.replace(/\/$/, '');
const encodePathSegment = (value) => encodeURIComponent(String(value ?? ''));

export const getProxyImageUrl = (url, options = {}) => {
    if (!url) return '';
    const value = String(url).trim();
    if (!value) return '';
    const width = Number.parseInt(options.w ?? options.width ?? '', 10);
    const widthParam = Number.isFinite(width) && width > 0 ? `&w=${encodeURIComponent(width)}` : '';
    if (/^data:image\/(?:png|jpe?g|webp|gif|avif);base64,/i.test(value)) {
        return value;
    }
    if (value.startsWith('/api/')) {
        return `${normalizedApiBase}${value}`;
    }
    if (value.startsWith('/assets/')) {
        return value;
    }
    if (value.startsWith('//')) {
        return `${normalizedApiBase}/api/proxy/image?url=${encodeURIComponent(`https:${value}`)}${widthParam}`;
    }
    if (/^https?:\/\//i.test(value)) {
        return `${normalizedApiBase}/api/proxy/image?url=${encodeURIComponent(value)}${widthParam}`;
    }
    return '';
};

export const getSafeExternalUrl = (url, allowedProtocols = ['http:', 'https:']) => {
    if (!url) return '';
    try {
        const parsed = new URL(String(url).trim());
        return allowedProtocols.includes(parsed.protocol) ? parsed.href : '';
    } catch {
        return '';
    }
};

export const getSafeMagnetUrl = (url) => {
    const value = String(url || '').trim();
    if (!/^magnet:\?/i.test(value)) return '';
    if (!/xt=urn:btih:/i.test(value)) return '';
    return value;
};

export const getSelectedMagnetFromResponse = (response) =>
    response?.selected_magnet || response?.magnet || null;

export const getApiErrorMessage = (error, fallback = '请求失败') => {
    const responseData = error?.response?.data;
    // detail 可能是字符串、422 校验错误数组；非 JSON 响应时 data 本身是字符串
    // 字符串响应体只接受短文本，HTML 错误页之类的交给 axios message 兜底
    const textData = typeof responseData === 'string'
        && responseData.length <= 200
        && !/[<>]/.test(responseData)
        ? responseData
        : null;
    const rawMessage = Array.isArray(responseData?.detail)
        ? responseData.detail
        : (responseData?.detail
            || responseData?.message
            || textData
            || error?.detail
            || error?.message
            || null);
    return redactText(mapApiErrorMessage(rawMessage, fallback));
};

const AUTH_TOKEN_KEY = 'bt_auth_token';

export const getAuthToken = () => {
    try {
        return window.localStorage.getItem(AUTH_TOKEN_KEY) || '';
    } catch {
        return '';
    }
};

export const setAuthToken = (token) => {
    try {
        if (token) {
            window.localStorage.setItem(AUTH_TOKEN_KEY, token);
        } else {
            window.localStorage.removeItem(AUTH_TOKEN_KEY);
        }
    } catch {
        // 存储不可用时仅本次会话内无 token
    }
};

export const clearAuthToken = () => setAuthToken('');

const isAuthEndpoint = (url) => /\/api\/auth\/(login|setup|status)/.test(String(url || ''));

const api = axios.create({
    baseURL: API_BASE_URL,
    timeout: 30000,
    headers: {
        'Content-Type': 'application/json',
    },
});

// 请求拦截器：注入登录 token
api.interceptors.request.use(
    (config) => {
        const token = getAuthToken();
        if (token) {
            config.headers = config.headers || {};
            config.headers.Authorization = `Bearer ${token}`;
        }
        return config;
    },
    (error) => {
        return Promise.reject(error);
    }
);

// 响应拦截器
api.interceptors.response.use(
    (response) => {
        return response.data;
    },
    (error) => {
        // 受保护接口返回 401：清 token 并跳登录页（auth 接口自身的 401 不处理，避免循环）
        if (
            error?.response?.status === 401
            && !isAuthEndpoint(error?.config?.url)
            && typeof window !== 'undefined'
            && window.location.pathname !== '/login'
        ) {
            clearAuthToken();
            window.location.assign('/login');
        }
        const safeError = sanitizeApiError(error);
        console.error('API Error:', getSafeApiErrorSummary(safeError));
        return Promise.reject(safeError);
    }
);

const TRANSLATION_BATCH_SIZE = 500;
const TRANSLATION_TEXT_MAX_LENGTH = 300;
// 本地大模型翻译每条标题约 2 秒（内网 qwen3.8，Ollama 并发 1），整页可能上百秒，
// 翻译是后台任务、不阻塞渲染，这里给足超时时间。
const TRANSLATION_REQUEST_TIMEOUT_MS = 300000;

const batchTranslateTexts = async (texts) => {
    if (!Array.isArray(texts)) {
        return api.post('/api/translate/batch', { texts });
    }

    const cleaned = [];
    const seen = new Set();
    texts.forEach((item) => {
        const text = String(item || '').trim();
        if (!text || text.length > TRANSLATION_TEXT_MAX_LENGTH || seen.has(text)) return;
        seen.add(text);
        cleaned.push(text);
    });

    if (cleaned.length === 0) {
        return { success: true, data: {} };
    }

    const merged = {};
    for (let index = 0; index < cleaned.length; index += TRANSLATION_BATCH_SIZE) {
        const chunk = cleaned.slice(index, index + TRANSLATION_BATCH_SIZE);
        const response = await api.post(
            '/api/translate/batch',
            { texts: chunk },
            { timeout: TRANSLATION_REQUEST_TIMEOUT_MS },
        );
        if (!response?.success) return response;
        Object.assign(merged, response.data || {});
    }
    return { success: true, data: merged };
};

const normalizeSubscriptionStatsResponse = (response) => {
    if (response?.success === true) {
        return {
            ...response,
            data: response.data && typeof response.data === 'object' && !Array.isArray(response.data)
                ? response.data
                : {},
        };
    }
    if (response?.success === false) {
        return response;
    }
    if (response && typeof response === 'object' && !Array.isArray(response)) {
        return { success: true, data: response };
    }
    return { success: false, message: 'invalid_subscription_stats_response', data: {} };
};

// 鉴权接口
export const authApi = {
    getStatus: () => api.get('/api/auth/status'),
    login: (username, password) => api.post('/api/auth/login', { username, password }),
    setup: (username, password) => api.post('/api/auth/setup', { username, password }),
};

// 首次部署引导接口
export const onboardingApi = {
    // includeSettings 只在引导页需要回填表单时才开。
    // 启动门（SetupGate）每个页面都调这个接口，不带配置才不会拖慢全站首屏。
    getStatus: (includeSettings = false) =>
        api.get('/api/onboarding/status', { params: { include_settings: includeSettings } }),
    apply: (payload) => api.post('/api/onboarding/apply', payload),
    testTarget: (target, payload = {}) => api.post(`/api/onboarding/test/${target}`, payload),
    checkDatabase: (expected_url = '') => api.post('/api/onboarding/database/check', { expected_url }),
    previewBlockKeywords: (block_keywords, limit = 20) =>
        api.post('/api/onboarding/block-keywords/preview', { block_keywords, limit }),
    reset: () => api.post('/api/onboarding/reset'),
};

// API 方法
export const movieApi = {
    // 获取导航分类
    getNavigation: () => api.get('/api/navigation'),

    // 获取详细分类标签 (Mega Menu)
    getGenres: (type = 'censored') => api.get('/api/genres', { params: { type } }),

    // 获取电影列表
    getMovies: (url, page = 1, limit = 30, force = false) =>
        api.get('/api/movies', { params: { url, page, limit, force_refresh: force } }),

    // 获取电影列表（仅缓存翻译模式，快速返回）
    getMoviesFast: (url, page = 1, limit = 30, force = false) =>
        api.get('/api/movies', { params: { url, page, limit, translate_mode: 'cache_only', force_refresh: force } }),
    getDiscoverySources: () => api.get('/api/discovery/sources'),
    getDiscoveryMovies: (params) => api.get('/api/discovery/movies', { params }),
    getJableHot: (params) => api.get('/api/jable/hot', { params }),

    // 批量翻译文本
    batchTranslate: batchTranslateTexts,

    // 获取电影详情
    getMovieDetail: (url, hints = {}) => api.get('/api/detail', { params: { url, ...hints } }),
    getActressDetail: (actressId, config = {}) =>
        api.get(`/api/actress/${encodePathSegment(actressId)}`, config),

    // 预览磁力链接选择
    selectMagnet: (url, preference = '1080P', hints = {}) =>
        api.get('/api/magnets/select', { params: { url, preference, ...hints } }),

    // 下载电影 (智能选择)
    downloadMovie: (data) => api.post('/api/download', data),

    // 下载特定磁力链接
    downloadMagnet: (data) => api.post('/api/download/magnet', data),

    // 获取 qBittorrent 状态
    getQBStatus: () => api.get('/api/qb/status'),

    // 获取 qBittorrent 配置
    getQBConfig: () => api.get('/api/qb/config').then(r => r?.data ?? {}),

    // 获取 qBittorrent 看板数据 (包含磁盘、速度、队列)
    getQBDashboard: () => api.get('/api/qb/dashboard'),

    // 本地下载队列：逐条列表 (分页/状态过滤/统计)
    getDownloadQueue: (params = {}) => api.get('/api/download/queue', { params }),

    // 本地下载队列：立即重试
    retryDownloadTask: (taskId) => api.post(`/api/download/queue/${taskId}/retry`),

    // 本地下载队列：恢复到 pending
    recoverDownloadTask: (taskId) => api.post(`/api/download/queue/${taskId}/recover`),

    // 本地下载队列：取消
    cancelDownloadTask: (taskId) => api.post(`/api/download/queue/${taskId}/cancel`),

    // 更新 qBittorrent 配置
    updateQBConfig: (config) => api.post('/api/qb/config', config),

    // 获取下载列表
    getTorrents: (paramsOrCategory) => {
        const params = typeof paramsOrCategory === 'object'
            ? paramsOrCategory
            : { category: paramsOrCategory };
        return api.get('/api/qb/torrents', { params });
    },
    controlTorrents: (data) => api.post('/api/qb/torrents/control', data),
    filterJavTorrentFiles: () => api.post('/api/qb/torrents/filter-jav-files'),

    // 搜索电影 (本地数据库)
    searchMovies: (query, limit = 50, offset = 0) =>
        api.get('/api/search', { params: { q: query, limit, offset } }),
    // 搜索外网 JavBus，供全局搜索在本地结果后补充
    searchExternalMovies: (query, page = 1, limit = 50, force = false) =>
        api.get('/api/movies', {
            params: {
                url: `/search/${encodeURIComponent(query)}`,
                page,
                limit,
                force_refresh: force,
                translate_mode: 'cache_only',
            },
        }),

    // --- 订阅管理 ---
    // 获取所有订阅
    getSubscriptions: (params = {}) => api.get('/api/subscriptions', { params }),

    // 添加订阅 (包含规避标签)
    addSubscription: (data) => api.post('/api/subscriptions', data),

    // 删除订阅
    deleteSubscription: (url) => api.delete('/api/subscriptions', { params: { url } }),

    // 手动刷新订阅
    refreshSubscription: (url) => api.post('/api/subscriptions/refresh', null, { params: { url } }),

    // 手动刷新首页 (添加 5分钟超时)
    refreshHomepage: () => api.post('/api/homepage/refresh', null, { timeout: 300000 }),

    // 手动刷新分类
    refreshCategory: (url) => api.post('/api/category/refresh', null, { params: { url }, timeout: 300000 }),



    // --- 系统配置 ---
    // 响应拦截器只剥到 response.data，后端仍是 {success, data} 信封。
    // 这里统一取 data，否则调用方拿到的会是 {success, data:{...}}，
    // 读 sysConfig.jellyfin_host 之类全是 undefined —— 表现为「保存后刷新就没了」。
    getSystemConfig: () => api.get('/api/config/system').then(r => r?.data ?? {}),
    updateSystemConfig: (data) => api.post('/api/config/system', data),

    // --- 全局关键词屏蔽（展示 + 下载双链路）---
    getBlockKeywords: () => api.get('/api/block-keywords').then(r => r?.data ?? {}),
    saveBlockKeywords: (data) => api.post('/api/block-keywords', data),
    // 纯内存试算：返回会被挡掉的样本，不写库
    previewBlockKeywords: (data) => api.post('/api/onboarding/block-keywords/preview', data),

    getCacheStats: () => api.get('/api/cache/stats').then(r => r?.data ?? {}),
    // 翻译进度总览（已翻/待翻/队列/速率/游标）
    getTranslationStatus: () => api.get('/api/translation/status'),
    cleanupCache: () => api.post('/api/cache/cleanup'),
    invalidateCache: (data) => api.post('/api/cache/invalidate', data),
    warmupCache: (data = { scope: 'recent', limit: 80 }) => api.post('/api/cache/warmup', data),

    // 获取实时运行日志
    getLogs: () => api.get('/api/logs'),

    // --- 女优管理 ---
    getActresses: (params) => api.get('/api/actresses', { params }),
    subscribeActress: (actress_id) => api.post('/api/actresses/subscribe', { actress_id }),
    markActressesViewed: () => api.post('/api/actresses/mark_viewed'),
    syncActressJellyfin: (actress_id) => api.post(`/api/actresses/${encodePathSegment(actress_id)}/sync_jellyfin`),
    syncAllActressesJellyfin: () => api.post('/api/actresses/sync_all_jellyfin'),
    syncJellyfinStatus: () => api.post('/api/jellyfin/sync_status'),

    // 获取订阅触发的下载任务统计
    // 列表用：不带装饰封面（那一步要 15 秒，卡片展开时才单独取）
    getSubscriptionStats: async () => normalizeSubscriptionStatsResponse(await api.get('/api/subscriptions/stats')),
    // 展开单张卡片时才取该订阅的装饰封面
    getSubscriptionCovers: async (url) => {
        const res = await api.get('/api/subscriptions/covers', { params: { url } });
        return { success: res?.success !== false, data: Array.isArray(res?.data) ? res.data : [] };
    },

    // --- 统计数据看板 ---
    getDashboardStats: () => api.get('/api/stats/dashboard'),

    // --- 媒体库管理 (以 Jellyfin 为数据源) ---
    getJfLibraries: () => api.get('/api/jellyfin/libraries'),
    getLibrary: (params) => api.get('/api/jellyfin/library', { params }),
    getJfLibraryItems: (params) => api.get('/api/jellyfin/library', { params }),
    getJellyfinBehaviorInsights: (params) => api.get('/api/jellyfin/behavior/insights', { params }),
    getJellyfinBehaviorMovies: (params) => api.get('/api/jellyfin/behavior/movies', { params }),
    getAutomationCandidates: (params) => api.get('/api/automation/candidates', { params }),
    getAutomationSettings: () => api.get('/api/automation/settings'),
    updateAutomationSettings: (data) => api.post('/api/automation/settings', data),
    approveAllAutomationCandidates: (data) => api.post('/api/automation/candidates/approve_all', data),
    approveAutomationCandidate: (candidateId, data) => api.post(`/api/automation/candidates/${encodePathSegment(candidateId)}/approve`, data),
    rejectAutomationCandidate: (candidateId, data) => api.post(`/api/automation/candidates/${encodePathSegment(candidateId)}/reject`, data),
    deleteJfItems: (data) => api.post('/api/jellyfin/items/delete', data),
    deleteJfItemsAsync: (data) => api.post('/api/jellyfin/items/delete_async', data),
    getDeleteProgress: (taskId) => api.get(`/api/jellyfin/delete_progress/${encodePathSegment(taskId)}`),
    cancelDeleteTask: (taskId) => api.delete(`/api/jellyfin/delete_task/${encodePathSegment(taskId)}`),
    favoriteActorSync: (data) => api.post('/api/jellyfin/favorite_actor', data),
    getJellyfinStats: () => api.get('/api/jellyfin/stats'),
    // 媒体库列表：后端把列表直接摊在信封外（{success, data:[...], preferred_library_id}），
    // 所以这里返回整个信封，由调用方取 res.data。
    getJellyfinLibraries: () => api.get('/api/jellyfin/libraries'),
    getLibraryCategoryDistribution: (limit = 8) =>
        api.get('/api/library/category-distribution', { params: { limit } }),
    // 已入库索引（番号 -> Jellyfin 条目 ID），卡片据此显示"播放"还是"下载"
    getLibraryIndex: (refresh = false) => api.get('/api/library/index', { params: { refresh } }),
    triggerJellyfinRefresh: () => api.post('/api/scrape/jellyfin-refresh'),
    playJellyfinMpv: (itemId) => api.post(`/api/jellyfin/play/${encodePathSegment(itemId)}`, null, { params: { player: 'mpv' } }),
    playJellyfinBrowser: (itemId) => api.post(`/api/jellyfin/play/${encodePathSegment(itemId)}`, null, { params: { player: 'browser' } }),

    // --- 健康检查 ---
    healthCheck: () => api.get('/api/health/check'),
    getTaskStatus: () => api.get('/api/tasks/status'),
    getExternalTasks: (params = {}) => api.get('/api/tasks/external', { params }),
    getLifecycleMovies: (params = {}) => api.get('/api/lifecycle/movies', { params }),
    getLifecycleEvents: (movieId, limit = 100) => api.get(
        `/api/lifecycle/movies/${encodePathSegment(movieId)}/events`,
        { params: { limit } },
    ),
    getCleanupCandidates: (params = {}) => api.get('/api/cleanup/candidates', { params }),
    approveCleanupCandidate: (candidateId, data) => api.post(
        `/api/cleanup/candidates/${encodePathSegment(candidateId)}/approve`,
        data,
    ),
    rejectCleanupCandidate: (candidateId, data) => api.post(
        `/api/cleanup/candidates/${encodePathSegment(candidateId)}/reject`,
        data,
    ),

    // --- amane 媒体栈对接（方案 C：BusTerminal 不碰文件，靠 API 驱动 amane） ---
    getAmaneStatus: () => api.get('/api/amane/status'),
    getAmaneConfig: () => api.get('/api/amane/config'),
    saveAmaneConfig: (data) => api.post('/api/amane/config', data),
    getAmaneLibraries: (force = false) => api.get('/api/amane/libraries', { params: { force } }),
    getAmaneInventory: (force = false) => api.get('/api/amane/inventory', { params: { force } }),
    getAmaneSelfcheck: () => api.get('/api/amane/selfcheck'),
    runAmaneIngest: (data) => api.post('/api/amane/ingest', data),
    confirmAmaneVisibility: (data) => api.post('/api/amane/ingest/visibility', data),
    checkAmanePresence: (movieId, number) => api.get('/api/amane/presence', {
        params: { movie_id: movieId, ...(number ? { number } : {}) },
    }),
    getAmaneIngestConfig: () => api.get('/api/amane/ingest/config'),
    saveAmaneIngestConfig: (data) => api.post('/api/amane/ingest/config', data),

    // --- BI 看板（默认读快照，refresh=true 强制现算） ---
    getBiOverview: (refresh = false) => api.get('/api/bi/overview', { params: { refresh } }),
};

export const getSystemSettings = movieApi.getSystemConfig;
export const saveSystemSettings = movieApi.updateSystemConfig;
export const getQBConfig = movieApi.getQBConfig;
export const saveQBConfig = movieApi.updateQBConfig;
export const testQBConnection = movieApi.getQBStatus;
export const getCacheStats = movieApi.getCacheStats;
export const cleanCache = movieApi.cleanupCache;
export const invalidateCache = movieApi.invalidateCache;
export const warmupCache = movieApi.warmupCache;

// --- amane 媒体栈 ---
export const getAmaneStatus = movieApi.getAmaneStatus;
export const getAmaneConfig = movieApi.getAmaneConfig;
export const saveAmaneConfig = movieApi.saveAmaneConfig;
export const getAmaneLibraries = movieApi.getAmaneLibraries;
export const getAmaneInventory = movieApi.getAmaneInventory;
export const getAmaneSelfcheck = movieApi.getAmaneSelfcheck;
export const runAmaneIngest = movieApi.runAmaneIngest;
export const confirmAmaneVisibility = movieApi.confirmAmaneVisibility;
export const checkAmanePresence = movieApi.checkAmanePresence;
export const getAmaneIngestConfig = movieApi.getAmaneIngestConfig;
export const saveAmaneIngestConfig = movieApi.saveAmaneIngestConfig;
export const getLifecycleMovies = movieApi.getLifecycleMovies;

// --- BI 看板 ---
export const getBiOverview = movieApi.getBiOverview;
// 看板汇总频率：daily / twice / realtime。保存后端会立刻重算一次
export const saveBiSettings = (data) => api.post('/api/bi/settings', data);
// 媒体库列表：后端把数组摊在信封外（{success, data:[...], preferred_library_id}），
// 所以这里返回整个信封，由调用方取 res.data
export const getJellyfinLibraries = movieApi.getJellyfinLibraries;

export default api;
