import React, { useMemo, useState, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { getApiErrorMessage, getProxyImageUrl, getSelectedMagnetFromResponse, logSafeError, movieApi } from '../api';
import { useToast } from './Toast';
import { useMovieDrawerDispatch } from './MovieDrawerContext';
import { useLibraryItemId } from '../utils/libraryPresence';
import './MovieCard.css';

const getMovieDetailRoute = (movie, fallbackUrl) => {
    const detailUrl = fallbackUrl || movie.detail_url || '';
    const params = new URLSearchParams();
    if (movie.id) params.set('movie_id', movie.id);
    if (movie.original_title || movie.title) params.set('title', movie.original_title || movie.title);
    const suffix = params.toString();
    return `/movie/${encodeURIComponent(detailUrl)}${suffix ? `?${suffix}` : ''}`;
};

const DownloadIcon = () => (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
        <polyline points="7 10 12 15 17 10"></polyline>
        <line x1="12" y1="15" x2="12" y2="3"></line>
    </svg>
);

const PlayIcon = () => (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" style={{ marginLeft: 2 }}>
        <polygon points="6 3 20 12 6 21 6 3"></polygon>
    </svg>
);

const escapeRegExp = (value) => String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

const getPosterTitle = (movie) => {
    const title = String(movie.title_zh || movie.title_cn || movie.title || movie.original_title || '未知影片').trim();
    if (title.toLowerCase().startsWith('error 500') || title.includes('There was an error')) return '未知影片';
    const code = String(movie.id || movie.movie_id || '').trim();
    if (!code) return title;

    // Upstream JavBus titles often end with "番号/日期"; keep that metadata
    // out of the title line while preserving the raw fields for actions/details.
    const escapedCode = escapeRegExp(code);
    const suffixPattern = new RegExp(`\\s*${escapedCode}(?:[\\/_-].*)?$`, 'i');
    const withoutSuffix = title.replace(suffixPattern, '').trim() || title;
    const leadingCodePattern = /^(?:\[[^\]]+\]\s*)?[A-Z]{2,8}-\d{2,5}\s*[:：|-]?\s*/i;
    return withoutSuffix.replace(leadingCodePattern, '').trim() || withoutSuffix;
};

function MovieCard({ movie, eagerImage = false, showPlayButton = false }) {
    const { showToast } = useToast();
    const navigate = useNavigate();
    const cardRef = useRef(null);
    const [imageLoaded, setImageLoaded] = useState(false);
    const [imageFailed, setImageFailed] = useState(false);
    const [imageRetryCount, setImageRetryCount] = useState(0);
    const imageRetryTimerRef = useRef(null);
    const [playMenuOpen, setPlayMenuOpen] = useState(false);
    const { openMovieDrawer } = useMovieDrawerDispatch();
    const longPressTimerRef = useRef(null);
    const longPressFiredRef = useRef(false);

    const openDrawer = () => {
        openMovieDrawer({
            code: movie.id || movie.movie_id,
            title: movie.original_title || movie.title,
            url: movie.detail_url,
            cover: movie.cover,
        });
    };

    const handleContextMenu = (event) => {
        // 卡片右键打开详情抽屉，不冒泡到页面级右键菜单
        event.preventDefault();
        event.stopPropagation();
        openDrawer();
    };

    const handleTouchStart = () => {
        longPressFiredRef.current = false;
        longPressTimerRef.current = setTimeout(() => {
            longPressFiredRef.current = true;
            openDrawer();
        }, 500);
    };

    const cancelLongPress = () => {
        if (longPressTimerRef.current) {
            clearTimeout(longPressTimerRef.current);
            longPressTimerRef.current = null;
        }
    };

    // 已入库的片子给"播放"，没入库的才给"下载"。
    // 媒体库页自带 jellyfin_id，其余页面查共享索引判断（索引加载完会自动重渲染）。
    const indexedJellyfinId = useLibraryItemId(movie.id || movie.movie_id, !showPlayButton && !movie.jellyfin_id);
    const playableId = movie.jellyfin_id || indexedJellyfinId || null;
    const canDownload = !playableId && !showPlayButton && Boolean(movie.detail_url || movie.id);

    const isJable = movie.source === 'jable';
    const hasCover = Boolean(movie.cover) && !imageFailed;
    const posterSeed = String(movie.id || movie.title || movie.original_title || 'BT');
    const posterStyle = useMemo(() => {
        let hash = 0;
        for (let index = 0; index < posterSeed.length; index += 1) {
            hash = posterSeed.charCodeAt(index) + ((hash << 5) - hash);
        }
        return {
            '--poster-hue-a': 185 + (Math.abs(hash) % 170),
            '--poster-hue-b': 165 + (Math.abs(hash * 37) % 170),
        };
    }, [posterSeed]);

    const handlePlay = async (event, player = 'mpv') => {
        event.preventDefault();
        event.stopPropagation();
        setPlayMenuOpen(false);

        const targetId = playableId;
        if (!targetId) {
            showToast('未检测到有效播放源', 'error');
            return;
        }

        showToast(player === 'mpv' ? '正在联动 Jellyfin MPV 播放器...' : '正在打开浏览器播放...', 'info');
        try {
            const res = player === 'mpv'
                ? await movieApi.playJellyfinMpv(targetId)
                : await movieApi.playJellyfinBrowser(targetId);
            if (res && res.success) {
                if (player === 'browser' && res.stream_url) {
                    window.open(res.stream_url, '_blank', 'noopener,noreferrer');
                } else if (res.mpv_launched) {
                    showToast(res.message || '已成功唤起本地 MPV 播放器！', 'success');
                } else {
                    showToast('未检测到运行中的 Jellyfin MPV Shim，请先启动本地播放器', 'warning');
                }
            } else {
                showToast(getApiErrorMessage(res, '唤起 MPV 播放失败'), 'error');
            }
        } catch (err) {
            logSafeError('MPV Play error:', err);
            showToast(player === 'mpv' ? '唤起 MPV 播放器失败' : '打开浏览器播放失败', 'error');
        }
    };

    const handleDownload = async (event) => {
        event.preventDefault();
        event.stopPropagation();

        try {
            let downloadUrl = movie.detail_url;

            if (movie.source === 'jable' && movie.id) {
                downloadUrl = `https://www.javbus.com/${movie.id}`;
                showToast(`正在搜索番号 ${movie.id}...`, 'info');
            }

            const response = await movieApi.downloadMovie({
                movie_url: downloadUrl,
                movie_id: movie.id,
                title: movie.original_title || movie.title,
                preference: '1080P',
            });

            if (response.success) {
                if (response.pending) {
                    showToast(`暂无磁链，已加入待重试: ${movie.id || movie.title}`, 'info');
                    return;
                }
                const selectedMagnet = getSelectedMagnetFromResponse(response);
                showToast(`已添加下载: ${movie.title}\n大小: ${selectedMagnet?.size || '未知'}`, 'success');
            } else {
                showToast(getApiErrorMessage(response, '添加下载失败'), 'error');
            }
        } catch (error) {
            logSafeError('Download error:', error);
            showToast(`下载失败: ${getApiErrorMessage(error, '未知错误')}`, 'error');
        }
    };

    const handleMouseMove = (event) => {
        if (window.innerWidth <= 768) return;
        if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
        if (!cardRef.current) return;

        const card = event.currentTarget;
        const box = card.getBoundingClientRect();
        const x = event.clientX - box.left;
        const y = event.clientY - box.top;
        const centerX = box.width / 2;
        const centerY = box.height / 2;
        const rotateX = ((y - centerY) / centerY) * -12;
        const rotateY = ((x - centerX) / centerX) * 12;

        cardRef.current.style.transform = `perspective(1000px) rotateX(${rotateX.toFixed(2)}deg) rotateY(${rotateY.toFixed(2)}deg) scale3d(1.02, 1.02, 1.02)`;
    };

    const handleMouseLeave = () => {
        if (cardRef.current) {
            cardRef.current.style.transform = 'perspective(1000px) rotateX(0deg) rotateY(0deg) scale3d(1, 1, 1)';
        }
    };

    const displayTitle = getPosterTitle(movie);
    const rawCoverUrl = movie.cover;
    const coverUrl = rawCoverUrl ? getProxyImageUrl(rawCoverUrl, { w: 360 }) : null;
    // 封面加载失败时自动重试（换 URL 参数绕过失败请求），避免后端抖动后要手动刷新才出图
    const coverSrc = coverUrl && imageRetryCount > 0 ? `${coverUrl}&_retry=${imageRetryCount}` : coverUrl;

    const handleImageError = () => {
        if (imageRetryCount < 2) {
            if (imageRetryTimerRef.current) clearTimeout(imageRetryTimerRef.current);
            imageRetryTimerRef.current = setTimeout(() => {
                setImageRetryCount((count) => count + 1);
            }, 3000 * (imageRetryCount + 1));
            return;
        }
        setImageFailed(true);
    };

    useEffect(() => () => {
        if (imageRetryTimerRef.current) clearTimeout(imageRetryTimerRef.current);
    }, []);

    const handleCardClick = () => {
        if (longPressFiredRef.current) {
            longPressFiredRef.current = false;
            return;
        }
        const detailRoute = getMovieDetailRoute(movie);
        navigate(detailRoute);
    };

    return (
        <div
            ref={cardRef}
            className="movie-card-container"
            onClick={handleCardClick}
            onContextMenu={handleContextMenu}
            onTouchStart={handleTouchStart}
            onTouchEnd={cancelLongPress}
            onTouchMove={cancelLongPress}
            onMouseMove={handleMouseMove}
            onMouseLeave={handleMouseLeave}
        >
            <div className="movie-card-poster-wrapper" style={posterStyle}>
                {!hasCover && (
                    <div className="movie-card-poster-fallback">
                        <span className="movie-card-fallback-badge">
                            {movie.id || 'BT'}
                        </span>
                        <span className="movie-card-fallback-title">
                            {displayTitle}
                        </span>
                    </div>
                )}

                {coverUrl && !imageFailed && (
                    <img
                        src={coverSrc}
                        alt={displayTitle}
                        className={`movie-card-poster-img ${imageLoaded ? 'is-loaded' : ''}`}
                        loading={eagerImage ? 'eager' : 'lazy'}
                        decoding="async"
                        ref={(el) => {
                            if (el) {
                                try {
                                    el.fetchPriority = eagerImage ? 'high' : 'low';
                                } catch (err) {
                                    // 旧浏览器不支持 fetchPriority，忽略即可
                                }
                            }
                        }}
                        onLoad={() => setImageLoaded(true)}
                        onError={handleImageError}
                    />
                )}

                {movie.date && (
                    <span className="movie-card-poster-date">
                        {movie.date}
                    </span>
                )}

                <div className="movie-card-overlay">
                    {isJable && (
                        <span className="movie-card-jable-tag">
                            Jable
                        </span>
                    )}

                    <div className="movie-card-overlay-actions">
                        {playableId && (
                            <div className="movie-card-play-menu">
                                <button
                                    className="movie-card-quick-play"
                                    onClick={(event) => {
                                        event.preventDefault();
                                        event.stopPropagation();
                                        setPlayMenuOpen((open) => !open);
                                    }}
                                    title="选择播放方式"
                                    aria-label="选择播放方式"
                                    aria-expanded={playMenuOpen}
                                    type="button"
                                >
                                    <PlayIcon />
                                </button>
                                {playMenuOpen && (
                                    <div className="movie-card-play-options" role="menu">
                                        <button type="button" role="menuitem" onClick={(event) => handlePlay(event, 'mpv')}>
                                            本地 MPV
                                        </button>
                                        <button type="button" role="menuitem" onClick={(event) => handlePlay(event, 'browser')}>
                                            浏览器播放
                                        </button>
                                    </div>
                                )}
                            </div>
                        )}
                        {canDownload && (
                            <button
                                className="movie-card-quick-download"
                                onClick={handleDownload}
                                title={displayTitle}
                                aria-label={`下载 ${displayTitle}`}
                                type="button"
                            >
                                <DownloadIcon />
                            </button>
                        )}
                    </div>
                </div>
            </div>

            <div className="movie-card-info-content">
                <h3 className="movie-card-title" title={displayTitle}>
                    {displayTitle}
                </h3>

                <div className="movie-card-sub-meta">
                    {movie.actress && (
                        <span className="movie-card-actress" title={movie.actress}>
                            {movie.actress}
                        </span>
                    )}
                </div>
            </div>
        </div>
    );
}

export default React.memo(MovieCard);
