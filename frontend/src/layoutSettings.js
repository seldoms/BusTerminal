import { useCallback, useEffect, useState } from 'react';

export const CARD_WIDTH_MIN = 120;
export const CARD_WIDTH_DEFAULT = 160;
export const CARD_WIDTH_MAX = 260;

export const LAYOUT_SETTINGS_STORAGE_KEY = 'bus-terminal-layout-settings';

const LEGACY_CARD_WIDTH_KEY = 'layout-card-width';
const LEGACY_FULL_WIDTH_KEY = 'layout-full-width';
const LEGACY_LAYOUT_MODE_KEY = 'layout-mode';
const OLDER_CARD_WIDTH_KEY = 'card-min-width';
const OLDER_FULL_WIDTH_KEY = 'is-full-width';

const LAYOUT_MODES = ['compact', 'default', 'poster'];
const subscribers = new Set();

export const normalizeCardWidth = (value) => {
    const parsed = parseInt(value, 10);
    if (!Number.isFinite(parsed)) return CARD_WIDTH_DEFAULT;
    return Math.min(CARD_WIDTH_MAX, Math.max(CARD_WIDTH_MIN, parsed));
};

export const getGridGap = (cardWidth) => {
    if (cardWidth <= 150) return 12;
    if (cardWidth >= 220) return 24;
    if (cardWidth >= 190) return 20;
    return 16;
};

export const getLayoutMode = (cardWidth) => {
    if (cardWidth >= 220) return 'poster';
    if (cardWidth <= 150) return 'compact';
    return 'default';
};

export const getContentMaxWidth = (cardWidth) => {
    const minW = 1200;
    const maxW = 1920;
    const ratio = (cardWidth - CARD_WIDTH_MIN) / (CARD_WIDTH_MAX - CARD_WIDTH_MIN);
    // 滑块向左=封面小、内容区占屏比大（最宽 1920）；向右=封面大、内容区占屏比小（最窄 1200）
    const calculated = Math.round(maxW - ratio * (maxW - minW));
    return `${calculated}px`;
};

export const getNavContentMaxWidth = (cardWidth) => {
    return getContentMaxWidth(cardWidth);
};

export const getContentPadding = (cardWidth) => {
    const ratio = (cardWidth - CARD_WIDTH_MIN) / (CARD_WIDTH_MAX - CARD_WIDTH_MIN);
    const padding = Math.round(16 + ratio * 16); // 16px ~ 32px
    return `${padding}px`;
};

const getStorage = () => {
    if (typeof window === 'undefined') return null;
    try {
        return window.localStorage || null;
    } catch {
        return null;
    }
};

const getStorageItem = (storage, key) => {
    if (!storage) return null;
    try {
        return storage.getItem(key);
    } catch {
        return null;
    }
};

const canUseDocument = () => (
    typeof document !== 'undefined'
    && !!document.documentElement
);

const canUseWindowEvents = () => (
    typeof window !== 'undefined'
    && typeof window.addEventListener === 'function'
    && typeof window.removeEventListener === 'function'
);

const normalizeLayoutMode = (layoutMode, cardWidth) => (
    LAYOUT_MODES.includes(layoutMode) ? layoutMode : getLayoutMode(cardWidth)
);

export const normalizeLayoutBoolean = (value) => {
    if (typeof value === 'boolean') return value;
    if (typeof value === 'number') return value !== 0;
    const text = String(value ?? '').trim().toLowerCase();
    if (['1', 'true', 'yes', 'on'].includes(text)) return true;
    if (['0', 'false', 'no', 'off', ''].includes(text)) return false;
    return false;
};

export const normalizeLayoutSettings = (settings = {}) => {
    const cardWidth = normalizeCardWidth(settings.cardWidth ?? settings.cardSize);
    const layoutMode = normalizeLayoutMode(settings.layoutMode ?? settings.mode, cardWidth);

    return { cardWidth, layoutMode };
};

const readJsonSettings = () => {
    const storage = getStorage();
    if (!storage) return null;

    try {
        const raw = getStorageItem(storage, LAYOUT_SETTINGS_STORAGE_KEY);
        return raw ? JSON.parse(raw) : null;
    } catch {
        return null;
    }
};

const readLegacySettings = () => {
    const storage = getStorage();
    if (!storage) return {};

    return {
        cardWidth: getStorageItem(storage, LEGACY_CARD_WIDTH_KEY) || getStorageItem(storage, OLDER_CARD_WIDTH_KEY),
        layoutMode: getStorageItem(storage, LEGACY_LAYOUT_MODE_KEY),
    };
};

export const readLayoutSettings = () => (
    normalizeLayoutSettings(readJsonSettings() || readLegacySettings())
);

const persistLayoutSettings = (settings) => {
    const storage = getStorage();
    if (!storage) return;

    try {
        storage.setItem(LAYOUT_SETTINGS_STORAGE_KEY, JSON.stringify(settings));
        [
            LEGACY_CARD_WIDTH_KEY,
            LEGACY_FULL_WIDTH_KEY,
            LEGACY_LAYOUT_MODE_KEY,
            OLDER_CARD_WIDTH_KEY,
            OLDER_FULL_WIDTH_KEY,
        ].forEach(key => storage.removeItem(key));
    } catch {
        // Storage may be unavailable in private/restricted browser contexts.
    }
};

const emitLayoutSettings = (settings) => {
    subscribers.forEach(listener => listener(settings));
};

export const subscribeLayoutSettings = (listener) => {
    subscribers.add(listener);
    return () => subscribers.delete(listener);
};

export const applyLayoutSettings = (settings, options = {}) => {
    const { persist = true, notify = true, preview = false } = options;
    const next = normalizeLayoutSettings(settings);

    if (canUseDocument()) {
        const root = document.documentElement;
        if (root.style?.setProperty) {
            root.style.setProperty('--card-width', `${next.cardWidth}px`);
            root.style.setProperty('--card-min-width', `${next.cardWidth}px`);
            root.style.setProperty('--grid-gap', `${getGridGap(next.cardWidth)}px`);
            if (!preview) {
                root.style.setProperty('--content-max-width', getContentMaxWidth(next.cardWidth));
                root.style.setProperty('--nav-content-width', getNavContentMaxWidth(next.cardWidth));
                root.style.setProperty('--content-padding', getContentPadding(next.cardWidth));
                root.style.setProperty('--poster-radius', next.cardWidth >= 220 ? '10px' : '8px');
                root.style.setProperty('--container', 'var(--content-max-width)');
                root.style.setProperty('--page-gutter', 'var(--content-padding)');
            }
        }

        if (!preview) {
            root.classList?.remove('layout-full-width');
            root.classList?.remove('layout-mode-compact', 'layout-mode-default', 'layout-mode-poster');
            root.classList?.add(`layout-mode-${next.layoutMode}`);
            if (root.dataset) {
                root.dataset.layoutMode = next.layoutMode;
            }
        }
    }

    if (persist) persistLayoutSettings(next);
    if (notify) emitLayoutSettings(next);

    return next;
};

export const updateLayoutSettings = (partialSettings, options = {}) => {
    const current = readLayoutSettings();
    const merged = { ...current, ...partialSettings };

    if (
        (Object.prototype.hasOwnProperty.call(partialSettings, 'cardWidth')
            || Object.prototype.hasOwnProperty.call(partialSettings, 'cardSize'))
        && !Object.prototype.hasOwnProperty.call(partialSettings, 'layoutMode')
        && !Object.prototype.hasOwnProperty.call(partialSettings, 'mode')
    ) {
        merged.layoutMode = getLayoutMode(partialSettings.cardWidth ?? partialSettings.cardSize);
    }

    return applyLayoutSettings(merged, options);
};

export const useLayoutSettings = () => {
    const [settings, setSettings] = useState(() => readLayoutSettings());

    useEffect(() => {
        setSettings(applyLayoutSettings(readLayoutSettings(), { persist: true, notify: false }));

        const unsubscribe = subscribeLayoutSettings(setSettings);
        const handleStorage = (event) => {
            if (event.key === LAYOUT_SETTINGS_STORAGE_KEY) {
                setSettings(applyLayoutSettings(readLayoutSettings(), { persist: false, notify: false }));
            }
        };

        if (!canUseWindowEvents()) {
            return unsubscribe;
        }

        window.addEventListener('storage', handleStorage);
        return () => {
            unsubscribe();
            window.removeEventListener('storage', handleStorage);
        };
    }, []);

    const setLayoutSettings = useCallback((partialSettings, options) => (
        updateLayoutSettings(partialSettings, options)
    ), []);

    return [settings, setLayoutSettings];
};
