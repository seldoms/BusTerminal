import React, { useCallback, useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useToast } from '../components/Toast';
import MovieCard from '../components/MovieCard';
import CardGrid from '../components/CardGrid';
import { getApiErrorMessage, getProxyImageUrl, getSafeExternalUrl, logSafeError, movieApi } from '../api';
import { preloadImages } from '../utils/imagePreload';
import './ActressDetailPage.css';

function ActressDetailPage() {
    const { actressId } = useParams();
    const navigate = useNavigate();
    const { showToast } = useToast();
    const [loading, setLoading] = useState(true);
    const [actress, setActress] = useState(null);
    const [movies, setMovies] = useState([]);
    const [bioExpanded, setBioExpanded] = useState(false);
    const [error, setError] = useState('');

    const legacyAliasMarker = ' [曾用名: ';
    const displayName = actress?.name?.includes(legacyAliasMarker)
        ? actress.name.split(legacyAliasMarker)[0]
        : actress?.name;
    const aliasText = actress?.names?.length
        ? actress.names
            .map(item => item.name)
            .filter(name => name && name !== displayName)
            .join(' / ')
        : actress?.name?.includes(legacyAliasMarker)
            ? actress.name.split(legacyAliasMarker)[1].replace(']', '')
            : '';
    const safeWikiUrl = getSafeExternalUrl(actress?.wiki_url);

    const loadActressDetail = useCallback(async () => {
        setLoading(true);
        setError('');
        setActress(null);
        setMovies([]);

        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 15000);

        try {
            const data = await movieApi.getActressDetail(actressId, {
                signal: controller.signal
            });

            if (data.success) {
                setActress(data.data?.actress || null);
                setMovies(Array.isArray(data.data?.movies) ? data.data.movies : []);
            } else {
                const message = getApiErrorMessage(data, '加载女优信息失败');
                setActress(null);
                setMovies([]);
                setError(message);
                showToast(message, 'error');
            }
        } catch (error) {
            logSafeError('Failed to load actress detail:', error);
            setActress(null);
            setMovies([]);
            const message = error.name === 'AbortError' || error.code === 'ERR_CANCELED'
                ? '加载女优信息超时，请稍后重试'
                : getApiErrorMessage(error, '加载女优信息失败');
            setError(message);
            showToast(message, 'error');
        } finally {
            clearTimeout(timeoutId);
            setTimeout(() => setLoading(false), 300);
        }
    }, [actressId, showToast]);

    useEffect(() => {
        loadActressDetail();
    }, [loadActressDetail]);

    // 头像与作品封面提前预取，滚动到位即瞬时显示
    useEffect(() => {
        if (actress?.avatar) {
            preloadImages([getProxyImageUrl(actress.avatar, { w: 420 })]);
        }
    }, [actress]);

    useEffect(() => {
        if (movies.length > 0) {
            preloadImages(movies.map(m => getProxyImageUrl(m.cover, { w: 360 })).filter(Boolean));
        }
    }, [movies]);

    if (loading) {
        return (
            <div className="actress-detail-page">
                <div className="container">
                    <div className="loading-container">
                        <div className="loading-spinner"></div>
                        <div className="loading-text">正在加载女优信息...</div>
                    </div>
                </div>
            </div>
        );
    }

    if (!actress) {
        return (
            <div className="actress-detail-page">
                <div className="container">
                    <div className="actress-detail-error">
                        <p>{error || '女优信息不存在'}</p>
                        <div className="actress-detail-error-actions">
                            <button className="btn btn-primary" onClick={loadActressDetail}>重试</button>
                            <button className="btn btn-secondary" onClick={() => navigate(-1)}>返回</button>
                        </div>
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div className="actress-detail-page fade-in">
            <div className="container">
                <button className="back-btn btn btn-secondary" onClick={() => navigate(-1)}>
                    <BackIcon /> 返回
                </button>

                <div className="actress-header-modern">
                    <div className="actress-profile-card">
                        <div className="profile-left">
                            <div className="actress-avatar-modern">
                                {actress.avatar && (
                                    <img
                                        src={getProxyImageUrl(actress.avatar, { w: 420 })}
                                        loading="lazy"
                                        decoding="async"
                                        alt={displayName || actress.name}
                                    />
                                )}
                            </div>
                            {actress.cup && (
                                <div className="actress-cup-badge">
                                    {String(actress.cup).replace(/cup/gi, '')} Cup
                                </div>
                            )}
                        </div>

                        <div className="profile-right">
                            <div className="actress-name-section">
                                <h1 className="actress-name-modern">{displayName}</h1>
                                {aliasText && (
                                    <div className="actress-alias" title={aliasText}>
                                        曾用名 {aliasText}
                                    </div>
                                )}
                            </div>

                            <div className="actress-quick-stats">
                                {actress.age && (
                                    <div className="quick-stat-item">
                                        <span className="stat-label">年龄</span>
                                        <span className="stat-value">{actress.age}</span>
                                    </div>
                                )}
                                {actress.height && (
                                    <div className="quick-stat-item">
                                        <span className="stat-label">身高</span>
                                        <span className="stat-value">{actress.height}</span>
                                    </div>
                                )}
                                {actress.birth_date && (
                                    <div className="quick-stat-item">
                                        <span className="stat-label">生日</span>
                                        <span className="stat-value">{actress.birth_date}</span>
                                    </div>
                                )}
                            </div>

                            <div className="actress-details-section">
                                <button
                                    className="details-toggle-btn"
                                    onClick={() => setBioExpanded(!bioExpanded)}
                                >
                                    <span>{bioExpanded ? '收起详细信息' : '查看详细信息'}</span>
                                    <svg
                                        className={`toggle-icon ${bioExpanded ? 'expanded' : ''}`}
                                        width="20"
                                        height="20"
                                        viewBox="0 0 24 24"
                                        fill="none"
                                        stroke="currentColor"
                                        strokeWidth="2"
                                    >
                                        <polyline points="6 9 12 15 18 9"></polyline>
                                    </svg>
                                </button>

                                <div className={`details-content ${bioExpanded ? 'expanded' : ''}`}>
                                    <div className="details-grid">
                                        {actress.bust && (
                                            <div className="detail-item">
                                                <span className="detail-label">三围</span>
                                                <span className="detail-value">{actress.bust}/{actress.waist}/{actress.hip}</span>
                                            </div>
                                        )}
                                        {actress.hometown && (
                                            <div className="detail-item">
                                                <span className="detail-label">出生地</span>
                                                <span className="detail-value">{actress.hometown}</span>
                                            </div>
                                        )}
                                    </div>

                                    {actress.bio && (
                                        <div className="bio-section">
                                            <h3 className="bio-title">个人简介</h3>
                                            <p className="bio-text">{actress.bio}</p>
                                        </div>
                                    )}

                                    {safeWikiUrl && (
                                        <a
                                            href={safeWikiUrl}
                                            target="_blank"
                                            rel="noopener noreferrer"
                                            className="wiki-link-modern"
                                        >
                                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                                <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
                                                <polyline points="15 3 21 3 21 9"></polyline>
                                                <line x1="10" y1="14" x2="21" y2="3"></line>
                                            </svg>
                                            查看完整维基百科资料
                                        </a>
                                    )}
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <div className="actress-movies-section">
                    <h2 className="section-title">参演作品 ({movies.length})</h2>

                    {movies.length > 0 ? (
                        <CardGrid className="movies-grid">
                            {movies.map(movie => (
                                <MovieCard key={movie.id} movie={movie} />
                            ))}
                        </CardGrid>
                    ) : (
                        <p className="no-movies">暂无参演作品</p>
                    )}
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

export default ActressDetailPage;
