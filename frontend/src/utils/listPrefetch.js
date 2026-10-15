// 列表预取：鼠标悬停/键盘聚焦链接时，提前把该列表拉回来并写入 listCache。
// 这样真正点开时命中缓存零请求直接渲染（后端缓存同时被暖热，4 小时内复访也快）。
// 预取失败静默忽略：真实点击时仍会走正常请求。

import { getListCache, setListCache } from './listCache';

// 悬停意图判定：停留一小会儿才发请求，避免鼠标扫过整片链接时打出成串请求
const HOVER_DELAY_MS = 120;
// 同时最多两个预取，防止连续划过把后端抓取压满
const MAX_CONCURRENT = 2;

const timers = new Map();
const fetched = new Set();
let inflight = 0;

const runPrefetch = async (key, fetcher) => {
    if (fetched.has(key) || inflight >= MAX_CONCURRENT) return;
    inflight += 1;
    try {
        const response = await fetcher();
        if (response?.success && Array.isArray(response.data) && response.data.length > 0) {
            setListCache(key, {
                items: response.data,
                page: 1,
                hasMore: true,
                scrollY: 0,
            });
            fetched.add(key);
        }
    } catch {
        // 预取是纯优化，失败不影响正常点击
    } finally {
        inflight -= 1;
    }
};

export function scheduleListPrefetch(cacheKey, fetcher) {
    if (!cacheKey || typeof fetcher !== 'function' || timers.has(cacheKey) || fetched.has(cacheKey)) {
        return;
    }
    if (getListCache(cacheKey)) {
        fetched.add(cacheKey);
        return;
    }
    timers.set(cacheKey, setTimeout(() => {
        timers.delete(cacheKey);
        runPrefetch(cacheKey, fetcher);
    }, HOVER_DELAY_MS));
}

export function cancelListPrefetch(cacheKey) {
    const timer = timers.get(cacheKey);
    if (timer) {
        clearTimeout(timer);
        timers.delete(cacheKey);
    }
}
