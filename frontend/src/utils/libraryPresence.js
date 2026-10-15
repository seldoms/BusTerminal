import { useEffect, useReducer } from 'react';
import { logSafeError, movieApi } from '../api';

// 全站共用一份"已入库"索引（番号大写 -> Jellyfin 条目 ID）。
// 列表一屏几十张卡片，逐条问后端会把接口打爆；索引体积只有几千条，拉一次就够。
let index = {};
let loaded = false;
let inflight = null;
const listeners = new Set();

const normalizeCode = (code) => String(code || '').trim().toUpperCase();

const notifyListeners = () => listeners.forEach((listener) => listener());

export function getLibraryItemId(code) {
    const key = normalizeCode(code);
    return key ? index[key] || null : null;
}

export function loadLibraryIndex(force = false) {
    if (inflight) return inflight;
    if (loaded && !force) return Promise.resolve(index);

    inflight = movieApi
        .getLibraryIndex()
        .then((response) => {
            index = (response && response.index) || {};
        })
        .catch((error) => {
            // Jellyfin 不可达时保持空索引：查不到不等于没入库，卡片继续显示下载即可
            index = index || {};
            logSafeError('加载已入库索引失败:', error);
        })
        .finally(() => {
            loaded = true;
            inflight = null;
            notifyListeners();
        });
    return inflight;
}

export function subscribeLibraryIndex(listener) {
    listeners.add(listener);
    loadLibraryIndex();
    return () => listeners.delete(listener);
}

/**
 * 取这部片在 Jellyfin 里的条目 ID；没入库返回 null。
 * enabled 传 false 时不订阅（媒体库页自带 jellyfin_id，不必再查索引）。
 */
export function useLibraryItemId(code, enabled = true) {
    const [, bumpVersion] = useReducer((value) => value + 1, 0);

    useEffect(() => {
        if (!enabled) return undefined;
        return subscribeLibraryIndex(bumpVersion);
    }, [enabled]);

    return enabled ? getLibraryItemId(code) : null;
}
