import React, { useCallback, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { getApiErrorMessage, getProxyImageUrl, getSelectedMagnetFromResponse, movieApi } from '../api';
import { useToast } from '../components/Toast';
import ContentContainer from '../components/ContentContainer';
import MovieInfiniteList from '../components/MovieInfiniteList';
import { useLibraryItemId } from '../utils/libraryPresence';
import './HomePage.css';
import '../components/MovieCard.css';

function SearchPage() {
    const [searchParams] = useSearchParams();
    const query = (searchParams.get('q') || '').trim();

    // 取数回调：交给统一的 MovieInfiniteList 处理无限滚动 + 图片预加载 + 后台预取
    const fetchPage = useCallback(async (pageNum, limit) => {
        const localResponse = await movieApi.searchMovies(query, limit, (pageNum - 1) * limit);
        const localMovies = localResponse?.success && Array.isArray(localResponse.data)
            ? localResponse.data.map(movie => ({ ...movie, search_source: 'local' }))
            : [];
        // 本地已经填满一页时直接展示，避免外网站点的慢响应拖住搜索页。
        if (localMovies.length >= limit) {
            return {
                success: true,
                data: localMovies,
                count: localMovies.length,
            };
        }

        const externalResponse = await movieApi.searchExternalMovies(
            query,
            pageNum,
            Math.max(1, limit - localMovies.length),
        );
        const externalMovies = externalResponse?.success && Array.isArray(externalResponse.data)
            ? externalResponse.data.map(movie => ({ ...movie, search_source: 'external' }))
            : [];
        const seen = new Set();
        const data = [...localMovies, ...externalMovies].filter(movie => {
            const key = String(movie.id || movie.detail_url || movie.title || '');
            if (!key || seen.has(key)) return false;
            seen.add(key);
            return true;
        }).slice(0, limit);
        return {
            success: localResponse?.success || externalResponse?.success,
            data,
            count: data.length,
        };
    }, [query]);

    const renderItem = useCallback((movie) => <SearchMovieCard movie={movie} />, []);

    if (!query) {
        return (
            <div className="search-page fade-in">
                <ContentContainer className="poster-content-container">
                    <h2 className="section-title">搜索</h2>
                    <div className="empty-state">
                        <p>请输入关键词进行搜索。</p>
                    </div>
                </ContentContainer>
            </div>
        );
    }

    return (
        <div className="search-page fade-in">
            <ContentContainer className="poster-content-container">
                <h2 className="section-title">搜索结果: {query}</h2>
                <MovieInfiniteList
                    key={query}
                    fetchPage={fetchPage}
                    renderItem={renderItem}
                    getImageUrl={(m) => m.cover}
                    initialLimit={50}
                    gridClassName="movies-grid"
                    cacheKey={`search:${query}`}
                />
            </ContentContainer>
        </div>
    );
}

function SearchMovieCard({ movie }) {
    const { showToast } = useToast();
    const [imageFailed, setImageFailed] = useState(false);
    const title = movie.title || movie.original_title || movie.id || '未知影片';
    const hasCover = Boolean(movie.cover) && !imageFailed;
    const hasDetail = Boolean(movie.detail_url);
    // 已入库的片子给播放，没入库才给下载
    const playableId = useLibraryItemId(movie.id);
    const imageUrl = getProxyImageUrl(movie.cover, { w: 360 });
    const detailParams = new URLSearchParams();
    if (movie.id) detailParams.set('movie_id', movie.id);
    if (movie.original_title || title) detailParams.set('title', movie.original_title || title);
    const detailQuery = detailParams.toString();
    const detailPath = `/movie/${encodeURIComponent(movie.detail_url)}${detailQuery ? `?${detailQuery}` : ''}`;

    const handleDownload = async (event) => {
        event.preventDefault();
        event.stopPropagation();

        if (!hasDetail) {
            showToast('该搜索结果没有可下载详情链接', 'info');
            return;
        }

        try {
            const response = await movieApi.downloadMovie({
                movie_url: movie.detail_url,
                movie_id: movie.id,
                title,
                preference: '1080P',
            });

            if (response.success) {
                if (response.pending) {
                    showToast(`暂无磁链，已加入待重试: ${movie.id || title}`, 'info');
                    return;
                }
                const selectedMagnet = getSelectedMagnetFromResponse(response);
                showToast(`已添加下载: ${title}\n大小: ${selectedMagnet?.size || '未知'}`, 'success');
            } else {
                showToast(getApiErrorMessage(response, '添加下载失败'), 'error');
            }
        } catch (error) {
            showToast('下载失败: ' + getApiErrorMessage(error), 'error');
        }
    };

    const handlePlay = async (event) => {
        event.preventDefault();
        event.stopPropagation();

        try {
            // 有 mpv-shim 就唤起本地播放器，没有就退回浏览器串流（后端两种都给 stream_url）
            const response = await movieApi.playJellyfinMpv(playableId);
            if (response?.success && response?.mpv_launched) {
                showToast(response.message || '已成功唤起本地 MPV 播放器！', 'success');
                return;
            }
            if (response?.success && response?.stream_url) {
                window.open(response.stream_url, '_blank', 'noopener,noreferrer');
                return;
            }
            showToast(getApiErrorMessage(response, '唤起播放失败'), 'error');
        } catch (error) {
            showToast('播放失败: ' + getApiErrorMessage(error, '未知错误'), 'error');
        }
    };

    const cardContent = (
        <>
            <div className={`movie-poster ${hasCover ? '' : 'poster-missing'}`}>
                {hasCover && (
                    <img
                        src={imageUrl}
                        alt={title}
                        loading="lazy"
                        decoding="async"
                        className="loaded"
                        onError={() => setImageFailed(true)}
                    />
                )}
                {(playableId || hasDetail) && (
                    <div className="movie-overlay">
                        {playableId ? (
                            <button
                                className="play-btn download-btn-overlay"
                                onClick={handlePlay}
                                title="播放（已在本地资源库）"
                                aria-label="播放"
                                type="button"
                            >
                                <svg width="32" height="32" viewBox="0 0 24 24" fill="currentColor">
                                    <polygon points="6 3 20 12 6 21 6 3" />
                                </svg>
                            </button>
                        ) : (
                            <button
                                className="play-btn download-btn-overlay"
                                onClick={handleDownload}
                                title="一键下载 (1080P)"
                                aria-label="一键下载"
                                type="button"
                            >
                                <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                                    <polyline points="7 10 12 15 17 10" />
                                    <line x1="12" y1="15" x2="12" y2="3" />
                                </svg>
                            </button>
                        )}
                    </div>
                )}
            </div>
            <div className="movie-info">
                <h3 className="movie-title" title={title}>{title}</h3>
                {movie.id && <p className="movie-id">{movie.id}</p>}
                {movie.date && <p className="movie-date">{movie.date}</p>}
            </div>
        </>
    );

    if (!hasDetail) {
        return <article className="movie-card card">{cardContent}</article>;
    }

    return (
        <Link to={detailPath} className="movie-card card">
            {cardContent}
        </Link>
    );
}

export default SearchPage;
