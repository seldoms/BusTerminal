import React, { useCallback, useEffect, useState } from 'react';
import { Bell } from 'lucide-react';
import { Link } from 'react-router-dom';
import { getApiErrorMessage, logSafeError, movieApi } from '../api';
import { useToast } from '../components/Toast';
import { cancelListPrefetch, scheduleListPrefetch } from '../utils/listPrefetch';
import './NavigationPage.css';

// 分类矩阵按 有码/无码 缓存（模块级，会话内有效）：切标签、回退页面不重复请求
const genresCache = {};

function NavigationPage() {
    const [genres, setGenres] = useState({});
    const [isUncensored, setIsUncensored] = useState(false);
    const [loading, setLoading] = useState(true);
    const [subscribingUrl, setSubscribingUrl] = useState(null);
    const [startDate, setStartDate] = useState(new Date().toISOString().split('T')[0]);
    const [excludeTags, setExcludeTags] = useState('VR');
    const { showToast } = useToast();
    const categoryType = isUncensored ? 'uncensored' : 'censored';
    const genreItems = Object.entries(genres).flatMap(([groupName, items]) =>
        (Array.isArray(items) ? items : []).map((item, idx) => ({
            ...item,
            key: `${groupName}-${idx}-${item.url}`
        }))
    );

    const loadGenres = useCallback(async () => {
        const genreType = isUncensored ? 'uncensored' : 'censored';
        if (genresCache[genreType]) {
            setGenres(genresCache[genreType]);
            setLoading(false);
            return;
        }
        setLoading(true);
        try {
            const response = await movieApi.getGenres(genreType);
            if (response.success) {
                const data = response.data && typeof response.data === 'object' ? response.data : {};
                genresCache[genreType] = data;
                setGenres(data);
            } else {
                setGenres({});
                showToast(getApiErrorMessage(response, '加载分类失败'), 'error');
            }
        } catch (error) {
            logSafeError('Failed to load genres:', error);
            setGenres({});
            showToast(getApiErrorMessage(error, '加载分类失败'), 'error');
        } finally {
            setLoading(false);
        }
    }, [isUncensored, showToast]);

    useEffect(() => {
        loadGenres();
    }, [loadGenres]);

    const handleSubscribe = async (genre) => {
        try {
            const response = await movieApi.addSubscription({
                url: genre.url,
                name: genre.name,
                category_type: isUncensored ? 'uncensored' : 'censored',
                start_date: startDate,
                exclude_tags: excludeTags
            });
            if (response.success) {
                showToast(`订阅成功: ${genre.name}\n规避标签: ${excludeTags || '无'}\n将自动下载 ${startDate} 之后的影片`, 'success');
                setSubscribingUrl(null);
            } else {
                showToast(getApiErrorMessage(response, '订阅失败'), 'error');
            }
        } catch (error) {
            showToast('订阅失败: ' + getApiErrorMessage(error), 'error');
        }
    };

    return (
        <div className="navigation-page fade-in">
            <div className="nav-container">
                <div className="pill-tabs">
                    <button
                        className={`pill-tab${!isUncensored ? ' active' : ''}`}
                        onClick={() => setIsUncensored(false)}
                    >
                        有码星球
                    </button>
                    <button
                        className={`pill-tab${isUncensored ? ' active' : ''}`}
                        onClick={() => setIsUncensored(true)}
                    >
                        无码宇宙
                    </button>
                </div>

                {loading ? (
                    <div className="loading-state">
                        <div>正在加载分类矩阵...</div>
                    </div>
                ) : (
                    <div className="genre-group">
                        <div className="genre-links">
                            {genreItems.map((item) => {
                                // 悬停即预热该分类列表：点开时命中缓存零请求直接出图
                                const listCacheKey = `category:${item.url}`;
                                const prefetchGenreList = () => scheduleListPrefetch(
                                    listCacheKey,
                                    () => movieApi.getMoviesFast(item.url, 1, 30, false),
                                );
                                return (
                                <div key={item.key} className="genre-item">
                                    <Link
                                        to={`/category/${categoryType}?url=${encodeURIComponent(item.url)}&name=${encodeURIComponent(item.name)}`}
                                        className="genre-link"
                                        onMouseEnter={prefetchGenreList}
                                        onFocus={prefetchGenreList}
                                        onMouseLeave={() => cancelListPrefetch(listCacheKey)}
                                    >
                                        {item.name}
                                    </Link>
                                    <button
                                        className="sub-btn"
                                        onClick={() => setSubscribingUrl(subscribingUrl === item.url ? null : item.url)}
                                        title="订阅自动下载"
                                    >
                                        <Bell size={16} />
                                    </button>

                                    {subscribingUrl === item.url && (
                                        <div className="sub-popover">
                                            <div className="sub-field">
                                                <label>起降日期:</label>
                                                <input
                                                    type="date"
                                                    value={startDate}
                                                    onChange={(e) => setStartDate(e.target.value)}
                                                />
                                            </div>
                                            <div className="sub-field">
                                                <label>规避标签 (逗号分隔):</label>
                                                <input
                                                    type="text"
                                                    value={excludeTags}
                                                    onChange={(e) => setExcludeTags(e.target.value)}
                                                    placeholder="如: VR, 动画"
                                                />
                                            </div>
                                            <button className="confirm-sub-btn" onClick={() => handleSubscribe(item)}>确认订阅</button>
                                        </div>
                                    )}
                                </div>
                                );
                            })}
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}

export default NavigationPage;
