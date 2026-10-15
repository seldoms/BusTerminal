// 在卡片进入视口之前，提前把封面图拉进浏览器 HTTP 缓存，
// 这样滚动到位时图片几乎瞬时显示，解决"图片很慢/没预加载"的体感问题。
// 按 URL 去重并限制单次预加载数量，避免一次性把图片代理/远端压垮。

const preloadedUrls = new Set();
const MAX_PRELOAD_PER_CALL = 24;

// ⚠️ 并发闸门：浏览器对同域并发只有约 6 个连接。
// 早期实现是 `new Image()` 无上限地同时发起（一次翻页就灌 30 张），
// 超出的全部排在浏览器队列里 —— 表现就是"滚一下卡 2.5 秒"，
// 而实测 movies 接口本身只用 113ms。改成排队按序放行，
// 让正在显示的卡片优先拿到图。
const MAX_CONCURRENT = 6;

let active = 0;
const waiting = [];

function pump() {
    while (active < MAX_CONCURRENT && waiting.length > 0) {
        const url = waiting.shift();
        active += 1;
        const img = new Image();
        img.decoding = 'async';
        const done = () => {
            active -= 1;
            pump();
        };
        img.onload = done;
        img.onerror = done;
        img.src = url;
    }
}

export function preloadImages(urls) {
    if (!Array.isArray(urls)) return;
    let queued = 0;
    for (const raw of urls) {
        if (!raw || preloadedUrls.has(raw)) continue;
        if (queued >= MAX_PRELOAD_PER_CALL) break;
        preloadedUrls.add(raw);
        queued += 1;
        waiting.push(raw);
    }
    pump();
}

// 页面切换时清空待加载队列：旧页面的图还没轮到就没意义了，
// 留着只会占着并发额度，让新页面第一屏的图排队。
export function resetPreloadQueue() {
    waiting.length = 0;
}