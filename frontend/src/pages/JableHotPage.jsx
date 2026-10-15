import React, { useCallback, useEffect, useState } from 'react';
import { Bolt, Calendar, CalendarDays, Clock, Flame, RefreshCw } from 'lucide-react';
import { movieApi } from '../api';
import MovieInfiniteList from '../components/MovieInfiniteList';
import { cancelListPrefetch, scheduleListPrefetch } from '../utils/listPrefetch';
import ContentContainer from '../components/ContentContainer';
import './JableHotPage.css';

function JableHotPage() {
    const [sortBy, setSortBy] = useState('video_viewed');
    const [refreshTrigger, setRefreshTrigger] = useState(0);
    const [contextMenu, setContextMenu] = useState({ visible: false, x: 0, y: 0 });

    // 取数回调：交给统一的 MovieInfiniteList 处理无限滚动 + 图片预加载 + 后台预取
    // translate=false：翻译交给前端后台攒批（服务器同步翻译会让冷启动等上一分多钟）
    const fetchPage = useCallback((pageNum, limit) => {
        return movieApi.getJableHot({
            page: pageNum,
            limit,
            sort_by: sortBy,
            translate: false,
        });
    }, [sortBy]);

    const handleRefresh = () => {
        setContextMenu(prev => ({ ...prev, visible: false }));
        setRefreshTrigger(t => t + 1);
    };

    const handleContextMenu = (event) => {
        event.preventDefault();
        const menuWidth = 160;
        const menuHeight = 44;
        const x = Math.min(event.clientX, window.innerWidth - menuWidth - 8);
        const y = Math.min(event.clientY, window.innerHeight - menuHeight - 8);
        setContextMenu({ visible: true, x: Math.max(8, x), y: Math.max(8, y) });
    };

    useEffect(() => {
        if (!contextMenu.visible) return undefined;

        const closeMenu = () => setContextMenu(prev => ({ ...prev, visible: false }));
        const handleKeyDown = (event) => {
            if (event.key === 'Escape') closeMenu();
        };

        window.addEventListener('click', closeMenu);
        window.addEventListener('scroll', closeMenu, true);
        window.addEventListener('keydown', handleKeyDown);

        return () => {
            window.removeEventListener('click', closeMenu);
            window.removeEventListener('scroll', closeMenu, true);
            window.removeEventListener('keydown', handleKeyDown);
        };
    }, [contextMenu.visible]);

    const handleSortChange = (newSort) => {
        if (newSort !== sortBy) {
            setSortBy(newSort);
        }
    };

    return (
        <div className="jable-hot-page fade-in" onContextMenu={handleContextMenu}>
            <ContentContainer className="poster-content-container">
                <div className="page-header">
                    <div className="header-actions">
                        <div className="pill-tabs">
                            {[
                                { id: 'video_viewed', label: '全部', Icon: Flame },
                                { id: 'video_viewed_month', label: '本月', Icon: Calendar },
                                { id: 'video_viewed_week', label: '本周', Icon: CalendarDays },
                                { id: 'video_viewed_today', label: '今日', Icon: Bolt },
                                { id: 'post_date', label: '最新', Icon: Clock }
                            ].map(btn => {
                                // 悬停即预热该榜单：切换时命中缓存直接渲染
                                const listCacheKey = `jable-hot:${btn.id}`;
                                const prefetchRanking = () => scheduleListPrefetch(
                                    listCacheKey,
                                    () => movieApi.getJableHot({
                                        page: 1,
                                        limit: 30,
                                        sort_by: btn.id,
                                        translate: false,
                                    }),
                                );
                                return (
                                <button
                                    key={btn.id}
                                    className={`pill-tab${sortBy === btn.id ? ' active' : ''}`}
                                    onClick={() => handleSortChange(btn.id)}
                                    onMouseEnter={prefetchRanking}
                                    onFocus={prefetchRanking}
                                    onMouseLeave={() => cancelListPrefetch(listCacheKey)}
                                >
                                    <btn.Icon size={14} />
                                    {btn.label}
                                </button>
                                );
                            })}
                        </div>
                    </div>
                </div>

                <MovieInfiniteList
                    key={sortBy}
                    fetchPage={fetchPage}
                    refreshTrigger={refreshTrigger}
                    cacheKey={`jable-hot:${sortBy}`}
                />
            </ContentContainer>
            {contextMenu.visible && (
                <div
                    className="page-context-menu"
                    style={{ left: contextMenu.x, top: contextMenu.y }}
                    role="menu"
                    onClick={(event) => event.stopPropagation()}
                >
                    <button type="button" onClick={handleRefresh}>
                        <RefreshCw size={14} />
                        刷新当前榜单
                    </button>
                </div>
            )}
        </div>
    );
}

export default JableHotPage;
