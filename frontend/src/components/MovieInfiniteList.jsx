import React, { useCallback, useEffect, useRef, useState } from 'react';
import { getApiErrorMessage, getProxyImageUrl, logSafeError, movieApi } from '../api';
import MovieCard from './MovieCard';
import CardGrid from './CardGrid';
import { preloadImages } from '../utils/imagePreload';
import { getListCache, setListCache } from '../utils/listCache';
import './MovieInfiniteList.css';

const MovieCardSkeleton = () => (
    <div className="skeleton-card">
        <div className="skeleton-poster"></div>
        <div className="skeleton-info">
            <div className="skeleton-text"></div>
            <div className="skeleton-text short"></div>
        </div>
    </div>
);

const resolveKey = (item, index) => item?.id || item?.detail_url || item?.url || index;

// 列表 DOM 上限。无限滚动如果只追加不回收，DOM 会线性膨胀
// （实测滚 5 屏就到 6568 个节点），越滚越慢、最终浏览器变卡。
// 超过上限时裁掉最前面的条目，只保留最近这些 —— 用户本来就在往下看，
// 回滚到很前面的概率极低，真要回去点导航重新进即可。
const MAX_RENDERED_ITEMS = 600;

// 返回 { items, dropped } —— dropped 是被裁掉的条目数，调用方据此回退页码，
// 否则下次请求会从原页号继续，出现「第 N 页之后又出现第 N 页内容」的重复。
function trimToWindow(items) {
    if (items.length <= MAX_RENDERED_ITEMS) return { items, dropped: 0 };
    const next = items.slice(items.length - MAX_RENDERED_ITEMS);
    return { items: next, dropped: items.length - next.length };
}

// 连续这么多页都没有可展示条目就认为到底了（避免一路翻页打爆源站）
const MAX_EMPTY_PAGES = 5;
// 哨兵长期可见时最多自动补几页；再往下要等用户真的滚动（别一口气抓几十页）
const MAX_AUTO_FILL_PAGES = 3;
// 用户输入后这段时间内视为"人还在操作"，不再限制补页
const USER_INPUT_WINDOW_MS = 3000;
// 两次自动补页之间至少隔这么久：防止一屏没满时把源站连抓几十页
const MIN_AUTO_LOAD_INTERVAL_MS = 800;
// 自动补页状态放模块级：列表组件会因为"路由缓存恢复"等原因重挂载，
// 放组件内每次重挂载都会重置额度，等于没有限制。
const autoFillState = {
    budget: MAX_AUTO_FILL_PAGES,
    lastUserInputAt: 0,
    lastAutoLoadAt: 0,
};

function MovieInfiniteList({
    // 兼容旧用法：基于 /api/movies?url= 的分页；新用法优先 fetchPage 回调
    fetchUrl,
    fetchPage,
    // 自定义卡片渲染（默认 MovieCard），用于女优/搜索等列表
    renderItem,
    // 从列表项取封面/头像 URL（默认 movie.cover）
    getImageUrl,
    gridClassName = 'movies-grid',
    initialLimit = 30,
    // 提前约一屏触发加载
    rootMargin = '1000px',
    refreshTrigger = 0,
    lazyTranslate = true,
    // 自定义“是否到底”判断，用于后端返回 total 的场景（如媒体库）
    getHasMore,
    // 每页响应回调（页面可据此更新 cache_status 等附属 UI）
    onPageLoaded,
    // 可选：路由级缓存 key（由页面按查询参数拼接）；命中缓存则秒开并恢复滚动位置
    cacheKey,
}) {
    const [movies, setMovies] = useState([]);
    const [page, setPage] = useState(1);
    const [hasMore, setHasMore] = useState(true);
    const [loading, setLoading] = useState(false);
    const [errorMessage, setErrorMessage] = useState('');
    // 本次拿到这批数据的时间/来源，以及本页还有多少条在等翻译
    const [loadedAt, setLoadedAt] = useState(null);
    const [pendingTranslationCount, setPendingTranslationCount] = useState(0);
    const [reloadToken, setReloadToken] = useState(0);
    // 每次加载结束自增，用于触发"哨兵仍可见就继续续页"的判断
    const [loadTick, setLoadTick] = useState(0);

    // 用 ref 镜像 state，供 IntersectionObserver 回调读取最新值，避免闭包过期
    const loadingRef = useRef(false);
    const hasMoreRef = useRef(true);
    const pageRef = useRef(1);
    const bufferRef = useRef(null);
    const loadedPagesRef = useRef(new Set());
    const requestSeqRef = useRef(0);
    const shouldForceRef = useRef(false);
    const skipCacheRef = useRef(false);
    // 哨兵当前是否可见：IntersectionObserver 只在“可见性变化”时回调，
    // 哨兵长期停在视口里就不会再回调，需要靠它判断要不要主动续页
    const sentinelVisibleRef = useRef(false);
    // 连续多少页条目都被过滤光（防止一路翻到底打爆源站）
    const emptyPageStreakRef = useRef(0);
    const sentinelRef = useRef(null);
    // 用 ref 持有 fetchPage，避免内联箭头导致重置 effect 每次触发
    const fetchPageRef = useRef(fetchPage);
    fetchPageRef.current = fetchPage;
    // movies / cacheKey 的 ref 镜像，供卸载时写缓存读取最新值
    const moviesRef = useRef([]);
    const cacheKeyRef = useRef(cacheKey);
    cacheKeyRef.current = cacheKey;

    useEffect(() => { hasMoreRef.current = hasMore; }, [hasMore]);
    useEffect(() => { pageRef.current = page; }, [page]);
    useEffect(() => { moviesRef.current = movies; }, [movies]);

    // 自动补页额度：没有用户输入时最多补几页就停；用户一有输入（滚轮/触摸/按键/点击）
    // 就放开限制。不能用 scroll 事件判断：浏览器恢复滚动位置、内容变高都会自己触发它。
    const hasRecentUserInput = useCallback(
        () => Date.now() - autoFillState.lastUserInputAt < USER_INPUT_WINDOW_MS,
        [],
    );
    // 哨兵可见本身就证明有人在看这一屏，这时续页不该被"输入窗口"拦死。
    // 否则会出现"滚到底 → 停留超 3 秒 → 再滚也加载不出来"的断链
    // （IntersectionObserver 只在进出视口时回调，哨兵停在视口内就不再触发）。
    const canAutoFill = useCallback(
        () => (
            // 用户正在输入（滚轮/触摸/按键/点击，3 秒内）时不受节流约束：
            // 快速滚动间隔往往小于 MIN_AUTO_LOAD_INTERVAL_MS，若一律节流，
            // 用户滑得越快越加载不出来；等滑完了又不再有事件源（哨兵已在视口内、
            // 页面往往也没到底），于是永久停在当前页。实测快速滚动止于 page5、慢速可到 page25。
            hasRecentUserInput()
            || (
                Date.now() - autoFillState.lastAutoLoadAt >= MIN_AUTO_LOAD_INTERVAL_MS
                && (autoFillState.budget > 0 || sentinelVisibleRef.current)
            )
        ),
        [hasRecentUserInput],
    );
    const markAutoFill = useCallback(() => {
        autoFillState.lastAutoLoadAt = Date.now();
        if (!hasRecentUserInput() && autoFillState.budget > 0) {
            autoFillState.budget -= 1;
        }
    }, [hasRecentUserInput]);

    useEffect(() => {
        const markInput = () => { autoFillState.lastUserInputAt = Date.now(); };
        const events = ['wheel', 'touchmove', 'keydown', 'pointerdown'];
        events.forEach((name) => window.addEventListener(name, markInput, { passive: true }));
        return () => events.forEach((name) => window.removeEventListener(name, markInput));
    }, []);

    // 列表加载过程中持续写缓存，刷新页面或切换榜单时也能立即恢复。
    useEffect(() => {
        if (!cacheKey || movies.length === 0) return;
        setListCache(cacheKey, {
            items: movies,
            page,
            hasMore,
            scrollY: window.scrollY,
            loadedAt: loadedAt || Date.now(),
            pendingTranslationCount,
        });
    }, [cacheKey, movies, page, hasMore, loadedAt, pendingTranslationCount]);

    // 卸载时把最终列表状态 + 滚动位置写进路由级缓存（空列表不写，避免缓存错误态）
    useEffect(() => () => {
        if (!cacheKeyRef.current || moviesRef.current.length === 0) return;
        setListCache(cacheKeyRef.current, {
            items: moviesRef.current,
            page: pageRef.current,
            hasMore: hasMoreRef.current,
            scrollY: window.scrollY,
        });
    }, []);

    // 解析取数函数：优先 fetchPage 回调，否则兼容旧 fetchUrl
    const resolveFetchPage = useCallback((pageNum, limit, forceRefresh) => {
        if (fetchPageRef.current) return fetchPageRef.current(pageNum, limit, forceRefresh);
        const apiCall = lazyTranslate ? movieApi.getMoviesFast : movieApi.getMovies;
        return apiCall(fetchUrl, pageNum, limit, forceRefresh);
    }, [fetchUrl, lazyTranslate]);

    const prefetchPage = useCallback((pageNum, requestSeq) => {
        if (bufferRef.current || !hasMoreRef.current) return;
        const entry = { page: pageNum, requestSeq, movies: [], response: null };
        bufferRef.current = entry;
        entry.promise = resolveFetchPage(pageNum, initialLimit, false)
            .then((response) => {
                if (requestSeq !== requestSeqRef.current) return entry;
                entry.response = response;
                entry.movies = Array.isArray(response?.data) ? response.data : [];
                if (response?.success && entry.movies.length > 0) {
                    const imageUrlGetter = getImageUrl || (m => m.cover);
                    preloadImages(entry.movies.slice(0, 8).map(
                        m => getProxyImageUrl(imageUrlGetter(m), { w: 360 }),
                    ).filter(Boolean));
                    // 预取成功即尝试显示，避免这一页卡在缓冲区里把列表钉死
                    consumePrefetchedRef.current?.();
                }
                return entry;
            })
            .catch((error) => {
                entry.error = error;
                return entry;
            });
    }, [resolveFetchPage, initialLimit, getImageUrl]);

    const loadPage = useCallback(async (pageNum, requestSeq) => {
        if (loadingRef.current || loadedPagesRef.current.has(pageNum)) return;
        loadedPagesRef.current.add(pageNum);
        loadingRef.current = true;
        if (pageNum === 1) setLoading(true);

        try {
            const forceRefresh = pageNum === 1 && shouldForceRef.current;
            if (forceRefresh) shouldForceRef.current = false;

            const buffered = bufferRef.current?.page === pageNum ? bufferRef.current : null;
            const result = buffered ? await buffered.promise : null;
            if (result?.error) throw result.error;
            const response = buffered ? result.response : await resolveFetchPage(pageNum, initialLimit, forceRefresh);
            if (requestSeq !== requestSeqRef.current) return;
            const responseMovies = buffered ? result.movies : (Array.isArray(response?.data) ? response.data : []);
            if (buffered && bufferRef.current === buffered) bufferRef.current = null;
            if (onPageLoaded) onPageLoaded(response, pageNum);

            if (pageNum === 1 && !buffered) {
                setLoadedAt(Date.now());
                setPendingTranslationCount(Number(response?.pending_translation_count) || 0);
            }

            if (response?.success && responseMovies.length > 0) {
                emptyPageStreakRef.current = 0;
                setMovies(prev => {
                    const existingIds = new Set(prev.map(m => resolveKey(m)));
                    const merged = [...prev, ...responseMovies.filter(m => !existingIds.has(resolveKey(m)))];
                    const trimmed = trimToWindow(merged);
                    // 裁掉了前面的条目，页码要同步回退，否则下次请求从原页号继续会拉回重复内容
                    if (trimmed.dropped > 0) {
                        const pagesDropped = Math.max(1, Math.round(trimmed.dropped / Math.max(initialLimit, 1)));
                        const newPage = Math.max(1, pageNum - pagesDropped);
                        pageRef.current = newPage;
                        setPage(newPage);
                    }
                    return trimmed.items;
                });
                // 页码只在没发生裁剪时在这里推进。裁剪时（dropped > 0）
                // 上面的回调已经把它回退了，这里再赋值会把回退覆盖掉。
                if (moviesRef.current.length <= MAX_RENDERED_ITEMS) {
                    setPage(pageNum);
                    pageRef.current = pageNum;
                }

                // 提前把本页封面/头像拉进本地图片缓存，滚动到位即瞬时显示
                const imageUrlGetter = getImageUrl || (m => m.cover);
                preloadImages(
                    responseMovies.map(m => getProxyImageUrl(imageUrlGetter(m), { w: 360 })).filter(Boolean),
                );

                // 是否已到底：优先用页面自定义判断，否则按“本页未装满”判定
                const reachedEnd = getHasMore
                    ? !getHasMore(response, moviesRef.current.length + responseMovies.length)
                    // Most upstream feeds use a fixed, smaller page size (Jable returns 24
                    // for a requested limit of 30), so a short page is not an end marker.
                    : responseMovies.length === 0;

                if (reachedEnd) {
                    setHasMore(false);
                    hasMoreRef.current = false;
                } else {
                    prefetchPage(pageNum + 1, requestSeq);
                }
                return;
            }

            if (response?.success) {
                // 本页条目被"只展示已处理数据"过滤光了：继续往后翻，别误判成到底了
                const pendingOnPage = Number(response?.pending_translation_count) || 0;
                if (pendingOnPage > 0 && emptyPageStreakRef.current < MAX_EMPTY_PAGES) {
                    emptyPageStreakRef.current += 1;
                    prefetchPage(pageNum + 1, requestSeq);
                    return;
                }
                setHasMore(false);
                hasMoreRef.current = false;
            } else {
                setErrorMessage(getApiErrorMessage(response, '列表加载失败'));
                setHasMore(false);
                hasMoreRef.current = false;
                loadedPagesRef.current.delete(pageNum);
            }
        } catch (error) {
            if (requestSeq !== requestSeqRef.current) return;
            logSafeError('Failed to load list:', error);
            setErrorMessage(getApiErrorMessage(error, '列表加载失败'));
            setHasMore(false);
            hasMoreRef.current = false;
            if (bufferRef.current?.page === pageNum) bufferRef.current = null;
            loadedPagesRef.current.delete(pageNum);
        } finally {
            if (requestSeq === requestSeqRef.current) {
                loadingRef.current = false;
                if (pageNum === 1) setLoading(false);
                // 每次加载结束都打一个 tick：让"哨兵仍可见就续页"的副作用能重新评估
                // （loading 状态只有第 1 页会变，光靠它第 2 页以后就不再续了）
                setLoadTick(tick => tick + 1);
            }
        }
    }, [resolveFetchPage, initialLimit, getHasMore, onPageLoaded, prefetchPage, getImageUrl]);

    useEffect(() => {
        requestSeqRef.current += 1;
        loadedPagesRef.current.clear();
        bufferRef.current = null;
        loadingRef.current = false;
        setErrorMessage('');
        if (refreshTrigger > 0) {
            shouldForceRef.current = true;
        }
        // 命中路由级缓存：直接渲染缓存数据、恢复 page/hasMore 与滚动位置，不重新请求
        const cached = !skipCacheRef.current && cacheKey && refreshTrigger === 0
            ? getListCache(cacheKey)
            : null;
        skipCacheRef.current = false;
        if (cached) {
            setMovies(cached.items);
            setPage(cached.page);
            setHasMore(cached.hasMore);
            pageRef.current = cached.page;
            hasMoreRef.current = cached.hasMore;
            moviesRef.current = cached.items;
            setLoadedAt(cached.loadedAt || cached.timestamp || Date.now());
            setPendingTranslationCount(Number(cached.pendingTranslationCount) || 0);
            // 已缓存的页标记为已加载，避免无限滚动重复请求
            for (let pageNum = 1; pageNum <= cached.page; pageNum += 1) {
                loadedPagesRef.current.add(pageNum);
            }
            // 等首屏渲染完再恢复滚动位置
            requestAnimationFrame(() => {
                requestAnimationFrame(() => window.scrollTo(0, cached.scrollY || 0));
            });
            if (cached.hasMore) prefetchPage(cached.page + 1, requestSeqRef.current);
            return;
        }
        setMovies([]);
        setPage(1);
        setHasMore(true);
        pageRef.current = 1;
        hasMoreRef.current = true;
        moviesRef.current = [];
        loadPage(1, requestSeqRef.current);
        // fetchUrl / refreshTrigger / reloadToken 变化时重置；fetchPage 通过 ref 持有，无需加入依赖
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [fetchUrl, refreshTrigger, cacheKey, reloadToken]);

    // 手动刷新：跳过前端缓存，重新向后端要一次（后端命中 4 小时缓存，很快），
    // 用来把刚翻好的条目显示出来，不去强制回源抓取。
    const handleReload = useCallback(() => {
        skipCacheRef.current = true;
        shouldForceRef.current = false;
        setReloadToken(token => token + 1);
    }, []);

    const loadMore = useCallback(() => {
        loadPage(pageRef.current + 1, requestSeqRef.current);
    }, [loadPage]);

    // 预取回来的那一页若没被消费，就一直压在 bufferRef 里不显示。而缓冲区同一时刻只能存一页，
    // 于是它卡住后续所有预取：页面此时通常已经"滚到底"，scroll 不再产生事件、
    // IntersectionObserver 也因为哨兵没进出视口而不再回调 —— 列表就永久停在第 N 页。
    // （实测：分段慢滚能到 19 页，一次性快滚稳定停在第 4 页。）
    // 所以预取成功的那一刻就自己判断要不要显示，不等下一次事件。
    const consumePrefetchedRef = useRef(null);
    consumePrefetchedRef.current = () => {
        if (loadingRef.current) return;
        if (!bufferRef.current || !hasMoreRef.current) return;
        if (!sentinelVisibleRef.current) return;
        if (!canAutoFill()) return;
        markAutoFill();
        loadMore();
    };

    const retryLoad = useCallback(() => {
        setHasMore(true);
        hasMoreRef.current = true;
        setErrorMessage('');
        loadPage(pageRef.current + 1, requestSeqRef.current);
    }, [loadPage]);

    // 固定哨兵节点观察：不随 movies/loading 变化而反复断开重建，避免观察器失效
    useEffect(() => {
        const el = sentinelRef.current;
        if (!el) return;
        const observer = new IntersectionObserver(entries => {
            // 提前约一屏触发加载，配合后台预取下一页，划动时内容始终“已准备好”
            const visible = entries[0].isIntersecting;
            sentinelVisibleRef.current = visible;
            if (visible && hasMoreRef.current && !loadingRef.current && canAutoFill()) {
                markAutoFill();
                loadMore();
            }
        }, {
            rootMargin,
        });
        observer.observe(el);
        return () => observer.disconnect();
    }, [loadMore, rootMargin, canAutoFill, markAutoFill]);

    // 每次加载结束（loadTick 变化）时补一刀：哨兵仍然可见就继续续页。
    // 否则 IntersectionObserver 不重复回调，预取好的下一页会一直压在缓冲里不显示。
    // 额度制：没人滚动时只补几页，避免后台标签一口气抓几十页。
    useEffect(() => {
        if (loading) return;
        if (!sentinelVisibleRef.current || !hasMoreRef.current) return;
        if (!canAutoFill()) return;
        markAutoFill();
        loadMore();
    }, [loading, loadTick, loadMore, canAutoFill, markAutoFill]);

    // scroll 兜底：哨兵停在视口内不再移动时，IntersectionObserver 不会再次回调，
    // 上面的 loadTick 兜底也等不到下一次加载 —— 用户"滚到底 + 停留超过输入窗口 + 再滚"
    // 就彻底断链了。滚动时若哨兵已在视口内，主动续一页。
    useEffect(() => {
        let ticking = false;
        const onScroll = () => {
            if (ticking) return;
            ticking = true;
            requestAnimationFrame(() => {
                ticking = false;
                if (loadingRef.current) return;
                if (!sentinelVisibleRef.current || !hasMoreRef.current) return;
                if (!canAutoFill()) return;
                markAutoFill();
                loadMore();
            });
        };
        window.addEventListener('scroll', onScroll, { passive: true });
        return () => window.removeEventListener('scroll', onScroll);
    }, [loadMore, canAutoFill, markAutoFill]);

    return (
        <div className="movie-infinite-list" role="feed" aria-busy={loading}>
            {/* 数据更新时间 + 页内刷新按钮已移除：
                导航栏已有刷新（局部刷新），更新时间属运维信息应放设置页。
                这条横幅还在时会在页签与列表之间留出一行空白。 */}

            <CardGrid className={gridClassName}>
                {loading && movies.length === 0 && [...Array(12)].map((_, index) => (
                    <MovieCardSkeleton key={index} />
                ))}
                {movies.map((item, index) => (
                    <div
                        key={resolveKey(item, index)}
                        className="movie-item-wrapper"
                    >
                        {renderItem ? renderItem(item, index) : <MovieCard movie={item} eagerImage={index < 12} />}
                    </div>
                ))}
            </CardGrid>

            <div ref={sentinelRef} className="infinite-sentinel" aria-hidden="true" />

            {loading && movies.length > 0 && (
                <div className="loading-indicator">
                    <div className="spinner"></div>
                </div>
            )}

            {errorMessage && !loading && (
                <div className="end-of-list">
                    <p>{errorMessage}</p>
                    <button type="button" className="btn btn-secondary" onClick={retryLoad}>
                        重试
                    </button>
                </div>
            )}

            {!errorMessage && !hasMore && movies.length > 0 && (
                <div className="end-of-list">
                    <p>已经到底了</p>
                </div>
            )}

            {!errorMessage && !loading && movies.length === 0 && pendingTranslationCount > 0 && (
                <div className="end-of-list">
                    <p>本页内容正在翻译，稍后点“刷新”查看</p>
                </div>
            )}

            {!errorMessage && !loading && movies.length === 0 && !hasMore && pendingTranslationCount === 0 && (
                <div className="end-of-list">
                    <p>暂无内容</p>
                </div>
            )}
        </div>
    );
}

export default MovieInfiniteList;
