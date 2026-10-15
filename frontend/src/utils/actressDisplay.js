import { getProxyImageUrl } from '../api';

const UNKNOWN_VALUES = new Set([
    '0',
    '0.0',
    '-',
    '--',
    'null',
    'none',
    'undefined',
    'unknown',
    '0000-00-00',
]);

export const fallbackAvatar = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="320" height="320" viewBox="0 0 320 320"%3E%3Crect width="320" height="320" fill="%2320242d"/%3E%3Ccircle cx="160" cy="124" r="54" fill="%237d8596"/%3E%3Cpath d="M70 284c12-58 52-92 90-92s78 34 90 92" fill="%237d8596"/%3E%3C/svg%3E';

export const isKnownValue = (value) => {
    const text = String(value ?? '').trim();
    if (!text) return false;
    return !UNKNOWN_VALUES.has(text.toLowerCase());
};

export const formatPlainValue = (value) => (isKnownValue(value) ? String(value).trim() : '');

const hasKana = (value) => /[\u3040-\u30ff]/.test(String(value || ''));
const hasCjk = (value) => /[\u3400-\u9fff]/.test(String(value || ''));

const splitAliases = (aliases) => String(aliases || '')
    .split(/[,，、/|]/)
    .map(item => item.trim())
    .filter(Boolean);

export const getActressName = (actress) => String(actress?.name || actress?.id || 'Unknown').trim();

export const getActressDisplayName = (actress) => {
    const name = getActressName(actress);
    const baseName = name
        .replace(/\s*\[曾用名:.*?\]\s*$/, '')
        .split(/[（(【[]/)[0]
        .trim() || name;

    if (!hasKana(baseName)) return baseName;

    const knownNames = [
        ...splitAliases(actress?.aliases),
        ...(Array.isArray(actress?.names) ? actress.names.map(item => item?.name) : []),
    ];
    const chineseName = knownNames
        .map(value => String(value || '').trim())
        .find(value => value && value !== baseName && hasCjk(value) && !hasKana(value));

    return chineseName || baseName;
};

export const getCompactActressName = (actress, maxLength = 5) => {
    const displayName = getActressDisplayName(actress);
    const chars = Array.from(displayName);
    return chars.length > maxLength ? chars.slice(0, maxLength).join('') : displayName;
};

export const getActressNameTitle = (actress) => {
    const name = getActressName(actress);
    const displayName = getActressDisplayName(actress);
    return name === displayName ? undefined : name;
};

export const getActressDetailPath = (actressId) => `/actress/${encodeURIComponent(String(actressId ?? ''))}`;

export const getActressAvatarSrc = (avatar, width = 320) => {
    const value = String(avatar || '').trim();
    if (!value) return fallbackAvatar;
    return getProxyImageUrl(value, { w: width }) || fallbackAvatar;
};

export const getHeightLabel = (height) => {
    const value = formatPlainValue(height);
    if (!value) return '';
    return /cm$/i.test(value) ? value : `${value}cm`;
};

export const formatCupLabel = (cup) => {
    const label = formatPlainValue(cup).replace(/cup|杯/gi, '').trim();
    return isKnownValue(label) ? label : '';
};

export const getAgeBadge = (actress) => {
    const explicitAge = Number.parseInt(formatPlainValue(actress?.age), 10);
    if (Number.isFinite(explicitAge) && explicitAge > 0 && explicitAge < 120) {
        return { label: `${explicitAge}岁`, title: `年龄: ${explicitAge}岁` };
    }

    const birthDate = formatPlainValue(actress?.birth_date);
    const match = birthDate.match(/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})/);
    if (!match) return null;

    const year = Number(match[1]);
    const month = Number(match[2]);
    const day = Number(match[3]);
    const today = new Date();
    if (!Number.isInteger(year) || year < 1900 || year > today.getFullYear()) return null;

    let age = today.getFullYear() - year;
    const birthdayPassed = today.getMonth() + 1 > month || (today.getMonth() + 1 === month && today.getDate() >= day);
    if (!birthdayPassed) age -= 1;
    if (!Number.isFinite(age) || age <= 0 || age >= 120) return null;
    return { label: `${age}岁`, title: `出生日期: ${birthDate}` };
};

export const estimateCupFromBust = (bust) => {
    const raw = String(bust || '').match(/\d+(?:\.\d+)?/);
    if (!raw) return '';

    let bustCm = Number(raw[0]);
    if (!Number.isFinite(bustCm) || bustCm <= 0) return '';
    if (bustCm < 60) bustCm *= 2.54;

    if (bustCm < 80) return '估A';
    if (bustCm < 83) return '估B';
    if (bustCm < 86) return '估C';
    if (bustCm < 89) return '估D';
    if (bustCm < 92) return '估E';
    if (bustCm < 95) return '估F';
    return '估G';
};

export const getCupBadge = (actress) => {
    const confirmed = formatCupLabel(actress?.cup);
    if (confirmed) {
        return { label: confirmed, title: `罩杯: ${confirmed}`, estimated: false };
    }

    const estimated = estimateCupFromBust(actress?.bust);
    if (!estimated) return null;

    return {
        label: estimated,
        title: `估算罩杯: ${estimated.replace('估', '')}，依据胸围 ${actress?.bust}`,
        estimated: true,
    };
};
