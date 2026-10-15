// 列表路由级缓存：内存 LRU + localStorage 持久化，支持刷新页面后秒开并恢复滚动位置。
// key 由调用方按页面查询参数拼出，value 为 { items, page, hasMore, scrollY, timestamp }。

const MAX_ENTRIES = 20;
const TTL_MS = 5 * 60 * 1000;
const STORAGE_KEY = 'bus-terminal:list-cache:v1';

const cache = new Map();
let persistTimer = null;

const restoreCache = () => {
    if (typeof window === 'undefined') return;
    try {
        const stored = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || '[]');
        if (!Array.isArray(stored)) return;
        stored.slice(-MAX_ENTRIES).forEach(([key, entry]) => {
            if (!key || !entry || Date.now() - entry.timestamp > TTL_MS) return;
            cache.set(key, entry);
        });
    } catch {
        // 缓存损坏或存储不可用时，继续使用当前会话内存缓存。
    }
};

const persistCache = () => {
    if (typeof window === 'undefined') return;
    persistTimer = null;
    try {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify([...cache.entries()]));
    } catch {
        // localStorage 配额不足时不影响列表正常加载。
    }
};

restoreCache();
if (typeof window !== 'undefined') {
    window.addEventListener('pagehide', () => {
        if (persistTimer) clearTimeout(persistTimer);
        persistCache();
    });
}

export function getListCache(key) {
    if (!key) return null;
    const entry = cache.get(key);
    if (!entry) return null;
    if (Date.now() - entry.timestamp > TTL_MS) {
        cache.delete(key);
        return null;
    }
    // LRU：命中后挪到 Map 末尾，刷新最近使用顺序
    cache.delete(key);
    cache.set(key, entry);
    return entry;
}

export function setListCache(key, value) {
    if (!key || !value) return;
    if (cache.has(key)) cache.delete(key);
    cache.set(key, { ...value, timestamp: Date.now() });
    // 容量上限：超出时清掉最早（最久未用）的 key，防止内存膨胀
    while (cache.size > MAX_ENTRIES) {
        cache.delete(cache.keys().next().value);
    }
    if (!persistTimer) persistTimer = setTimeout(persistCache, 500);
}

// 路由切换到新页面时回顶用；App.jsx 路由变化时调用（由页面侧接线，本模块不感知路由）。
export function scrollToTop() {
    window.scrollTo(0, 0);
}
