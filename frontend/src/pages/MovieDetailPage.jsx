import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useParams, useNavigate, Link, useSearchParams } from 'react-router-dom';
import { getApiErrorMessage, getProxyImageUrl, getSafeMagnetUrl, logSafeError, movieApi } from '../api';
import { useToast } from '../components/Toast';
import { getActressDetailPath } from '../utils/actressDisplay';
import './MovieDetailPage.css';

const safeDecodeURIComponent = (value) => {
    try {
        return decodeURIComponent(value);
    } catch {
        return '';
    }
};

const getGenreCategoryLink = (id, name) => {
    const rawGenreId = String(id || '').trim();
    const genreId = rawGenreId.replace(/^\/?(?:uncensored\/)?genre\//, '');
    const genreName = String(name || '').trim();
    if (!genreId) {
        return `/search?q=${encodeURIComponent(genreName)}`;
    }
    const genrePath = rawGenreId.startsWith('/uncensored/genre/') || rawGenreId.startsWith('uncensored/genre/')
        ? `/uncensored/genre/${genreId}`
        : `/genre/${genreId}`;
    return `/category/${encodeURIComponent(genreId)}?name=${encodeURIComponent(genreName || genreId)}&url=${encodeURIComponent(genrePath)}`;
};

function MovieDetailPage() {
    const params = useParams();
    const [searchParams] = useSearchParams();
    const navigate = useNavigate();
    const { showToast } = useToast();

    // 所有 Hooks 必须在顶层无条件执行
    const rawUrl = params['*'] || '';
    const [loading, setLoading] = useState(true);
    const [detail, setDetail] = useState(null);
    const [downloading, setDownloading] = useState(false);
    const [preference, setPreference] = useState('1080P');
    const [selectedMagnet, setSelectedMagnet] = useState(null);
    const detailRequestSeqRef = useRef(0);
    const magnetRequestSeqRef = useRef(0);

    // 条件逻辑移至 Hooks 之后
    const movieUrl = rawUrl ? safeDecodeURIComponent(rawUrl) : '';
    const routeMovieId = searchParams.get('movie_id') || '';
    const routeTitle = searchParams.get('title') || '';
    const routeHints = useMemo(() => ({
        movie_id: routeMovieId || undefined,
        title: routeTitle || undefined,
    }), [routeMovieId, routeTitle]);

    // [FIX] 使用 useCallback 包装函数,避免 useEffect 依赖警告
    const loadMovieDetail = useCallback(async () => {
        const requestSeq = ++detailRequestSeqRef.current;
        magnetRequestSeqRef.current += 1;
        const isStaleRequest = () => requestSeq !== detailRequestSeqRef.current;
        setLoading(true);
        setDetail(null);
        setSelectedMagnet(null);
        if (!movieUrl) {
            setLoading(false);
            return;
        }
        try {
            const response = await movieApi.getMovieDetail(movieUrl, routeHints);
            if (isStaleRequest()) return;
            if (response.success && response.data && response.data.title) {
                setDetail(response.data);
            } else {
                setDetail(null);
                showToast(getApiErrorMessage(response, '影片信息加载失败'), 'error');
            }
        } catch (error) {
            if (isStaleRequest()) return;
            logSafeError('Failed to load movie detail:', error);
            setDetail(null);
            setSelectedMagnet(null);
            // [FIX] 增强错误处理
            if (error.response) {
                if (error.response.status === 404) {
                    showToast('影片不存在或已被删除', 'error');
                } else if (error.response.status >= 500) {
                    showToast('服务器错误,请稍后重试', 'error');
                } else {
                    showToast(`加载失败: ${getApiErrorMessage(error, '未知错误')}`, 'error');
                }
            } else if (error.request) {
                showToast('网络连接失败,请检查网络', 'error');
            } else {
                showToast('影片信息抓取失败,请检查网络或 URL 是否正确', 'error');
            }
        } finally {
            if (!isStaleRequest()) setLoading(false);
        }
    }, [movieUrl, routeHints, showToast]);

    const previewMagnetSelection = useCallback(async () => {
        const requestSeq = ++magnetRequestSeqRef.current;
        const isStaleRequest = () => requestSeq !== magnetRequestSeqRef.current;
        try {
            const detailInfo = detail?.info || {};
            const response = await movieApi.selectMagnet(movieUrl, preference, {
                movie_id: routeMovieId || detailInfo['识别码'] || detailInfo['識別碼'] || detailInfo['番号'] || undefined,
                title: routeTitle || detail?.original_title || detail?.title || undefined,
            });
            if (isStaleRequest()) return;
            if (response.success) {
                setSelectedMagnet(response.data || null);
            } else {
                setSelectedMagnet(null);
            }
        } catch (error) {
            if (isStaleRequest()) return;
            logSafeError('Failed to preview magnet:', error);
            setSelectedMagnet(null);
        }
    }, [detail, movieUrl, preference, routeMovieId, routeTitle]);

    useEffect(() => {
        loadMovieDetail();
    }, [loadMovieDetail]);

    // 磁力始终异步补：详情本身走缓存秒开，磁力可能还没抓过，交给 selectMagnet 按需补全
    useEffect(() => {
        if (detail) {
            previewMagnetSelection();
        }
    }, [detail, preference, previewMagnetSelection]);

    const handleDownload = async () => {
        if (!selectedMagnet) return;

        setDownloading(true);
        try {
            const response = await movieApi.downloadMovie({
                movie_url: movieUrl,
                movie_id: routeMovieId || displayMovieId,
                title: routeTitle || detail.original_title || detail.title,
                preference: preference,
            });

            if (response && response.success) {
                if (response.pending) {
                    showToast(`暂无磁链，已加入待重试: ${response.movie_id || displayMovieId}`, 'info');
                    return;
                }
                showToast(`下载任务已添加！\n${selectedMagnet.resolution} | ${selectedMagnet.size}`, 'success');
                navigate('/downloads');
            } else {
                showToast(getApiErrorMessage(response, '下载任务添加失败'), 'error');
            }
        } catch (error) {
            logSafeError('Download error:', error);
            showToast('下载失败: ' + getApiErrorMessage(error), 'error');
        } finally {
            setDownloading(false);
        }
    };

    if (loading) {
        return (
            <div className="movie-detail-page">
                <div className="container">
                    <div className="detail-loading" role="status" aria-live="polite">
                        <div className="spinner" aria-hidden="true"></div>
                        <p>正在加载影片详情...</p>
                    </div>
                </div>
            </div>
        );
    }

    if (!detail) {
        return (
            <div className="movie-detail-page">
                <div className="container">
                    <div className="detail-error">
                        <p>电影信息加载失败</p>
                        <div className="detail-error-actions">
                            <button className="btn btn-primary" onClick={loadMovieDetail}>重试</button>
                            <button className="btn btn-secondary" onClick={() => navigate(-1)}>返回</button>
                        </div>
                    </div>
                </div>
            </div>
        );
    }

    const detailInfo = detail.info || {};
    const displayMovieId = routeMovieId || detailInfo['识别码'] || detailInfo['識別碼'] || detailInfo['番号'] || '未知';

    return (
        <div className="movie-detail-page fade-in">
            <div className="container">
                <button className="back-btn btn btn-secondary" onClick={() => navigate(-1)}>
                    <BackIcon /> 返回
                </button>

                <div className="detail-header-top">
                    <h1 className="detail-title">{displayMovieId} {detail.title}</h1>
                </div>

                <div className="detail-content">
                    <div className="detail-main-info">
                        <div className="detail-poster-section">
                            {detail.cover && <img src={getProxyImageUrl(detail.cover, { w: 720 })} alt={detail.title} loading="lazy" decoding="async" />}
                        </div>

                        <div className="detail-metadata-section">
                            <div className="detail-info-list">
                                {Object.entries(detailInfo).map(([key, value]) => (
                                    <div key={key} className="info-row">
                                        <span className="info-label">{key}:</span>
                                        <span className="info-value">{value}</span>
                                    </div>
                                ))}
                            </div>

                            {detail.stars && detail.stars.length > 0 && (
                                <div className="detail-stars-section">
                                    <span className="info-label">出演人物:</span>
                                    <div className="stars-list">
                                        {detail.stars.map((star, index) => (
                                            <Link
                                                key={star.id || index}
                                                to={star.id
                                                    ? getActressDetailPath(star.id)
                                                    : `/search?q=${encodeURIComponent(star.name || '')}`}
                                                className="star-tag"
                                            >
                                                {star.name}
                                            </Link>
                                        ))}
                                    </div>
                                </div>
                            )}

                            {detail.genres && detail.genres.length > 0 && (
                                <div className="detail-genres-section">
                                    <span className="info-label">类型:</span>
                                    <div className="genres-list">
                                        {detail.genres.map((genre, index) => {
                                            // 兼容旧数据的字符串格式和新数据的对象格式
                                            const isObject = typeof genre === 'object' && genre !== null;
                                            const name = isObject ? genre.name : genre;
                                            const id = isObject ? genre.id : null;
                                            const linkTo = getGenreCategoryLink(id, name);

                                            return (
                                                <Link
                                                    key={id || index}
                                                    to={linkTo}
                                                    className="genre-tag"
                                                >
                                                    {name}
                                                </Link>
                                            );
                                        })}
                                    </div>
                                </div>
                            )}
                        </div>
                    </div>



                    <div className="detail-interaction-section">

                        {detail.magnets && detail.magnets.length > 0 && (
                            <div className="magnets-section">
                                <h2 className="section-title">下载选项</h2>

                                <div className="preference-selector">
                                    <label>分辨率偏好:</label>
                                    <div className="preference-buttons">
                                        {['4K', '1080P', '720P', '480P'].map((res) => (
                                            <button
                                                key={res}
                                                className={`btn ${preference === res ? 'btn-primary' : 'btn-secondary'}`}
                                                onClick={() => setPreference(res)}
                                            >
                                                {res}
                                            </button>
                                        ))}
                                    </div>
                                </div>

                                {selectedMagnet && (
                                    <div className="selected-magnet card">
                                        <h3>智能推荐</h3>
                                        <div className="magnet-details">
                                            <p><strong>标题:</strong> {selectedMagnet.title}</p>
                                            <p><strong>分辨率:</strong> <span className="badge">{selectedMagnet.resolution || '未知'}</span></p>
                                            <p><strong>字幕:</strong> <span className={`badge ${selectedMagnet.has_subtitle ? 'badge-success' : ''}`}>
                                                {selectedMagnet.has_subtitle ? '有字幕' : '无字幕'}
                                            </span></p>
                                            <p><strong>大小:</strong> {selectedMagnet.size}</p>
                                        </div>
                                        <button
                                            className="btn btn-primary download-btn"
                                            onClick={handleDownload}
                                            disabled={downloading}
                                        >
                                            <DownloadIcon />
                                            {downloading ? '添加中...' : '立即下载'}
                                        </button>
                                    </div>
                                )}

                                <div className="all-magnets">
                                    <h3>磁力连结投稿 ⊕</h3>
                                    <div className="magnets-table-container">
                                        <table className="magnets-table">
                                            <thead>
                                                <tr>
                                                    <th>磁力名称 ⬇</th>
                                                    <th>檔案大小</th>
                                                    <th>分享日期</th>
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {detail.magnets.map((magnet, index) => {
                                                    const safeMagnet = getSafeMagnetUrl(magnet.magnet);
                                                    return (
                                                        <tr key={index}>
                                                            <td className="magnet-name-cell">
                                                                <div className="magnet-name-wrapper">
                                                                    {magnet.resolution && (
                                                                        <span className="quality-badge">高清</span>
                                                                    )}
                                                                    {safeMagnet ? (
                                                                        <a
                                                                            href={safeMagnet}
                                                                            className="magnet-link"
                                                                            title={magnet.title}
                                                                        >
                                                                            {magnet.title}
                                                                        </a>
                                                                    ) : (
                                                                        <span className="magnet-link disabled" title="磁力链接格式异常">
                                                                            {magnet.title}
                                                                        </span>
                                                                    )}
                                                                    {magnet.has_subtitle && (
                                                                        <span className="subtitle-badge">字幕</span>
                                                                    )}
                                                                </div>
                                                                <div className="magnet-actions-inline">
                                                                        <button
                                                                            className="action-btn copy-btn"
                                                                            disabled={!safeMagnet}
                                                                        onClick={async () => {
                                                                            try {
                                                                                await navigator.clipboard.writeText(safeMagnet);
                                                                                showToast('磁力链接已复制', 'success');
                                                                            } catch (err) {
                                                                                logSafeError('Copy magnet failed:', err);
                                                                                showToast('复制失败: ' + getApiErrorMessage(err), 'error');
                                                                            }
                                                                        }}
                                                                        title="复制磁力链接"
                                                                    >
                                                                        <CopyIcon />
                                                                    </button>
                                                                    <button
                                                                        className="action-btn download-btn-small"
                                                                        disabled={!safeMagnet}
                                                                        onClick={async () => {
                                                                            try {
                                                                                const response = await movieApi.downloadMagnet({ magnet_url: safeMagnet });
                                                                                if (response.success) {
                                                                                    showToast('已添加到下载队列', 'success');
                                                                                } else {
                                                                                    showToast(getApiErrorMessage(response, '添加失败'), 'error');
                                                                                }
                                                                            } catch (err) {
                                                                                showToast('添加失败: ' + getApiErrorMessage(err), 'error');
                                                                            }
                                                                        }}
                                                                        title="下载"
                                                                    >
                                                                        <DownloadIcon />
                                                                    </button>
                                                                </div>
                                                            </td>
                                                            <td>{magnet.size}</td>
                                                            <td>{magnet.date_str}</td>
                                                        </tr>
                                                    );
                                                })}
                                            </tbody>
                                        </table>
                                    </div>
                                </div>
                            </div>
                        )}
                    </div>
                </div>
            </div>
        </div>
    );
}

const BackIcon = () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <line x1="19" y1="12" x2="5" y2="12"></line>
        <polyline points="12 19 5 12 12 5"></polyline>
    </svg>
);

const DownloadIcon = () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
        <polyline points="7 10 12 15 17 10"></polyline>
        <line x1="12" y1="15" x2="12" y2="3"></line>
    </svg>
);

const CopyIcon = () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
    </svg>
);

export default MovieDetailPage;
