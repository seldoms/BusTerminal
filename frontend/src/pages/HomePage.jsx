import React, { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { useToast } from '../components/Toast';
import { getApiErrorMessage, logSafeError, movieApi } from '../api';
import MovieInfiniteList from '../components/MovieInfiniteList';
import ContentContainer from '../components/ContentContainer';
import './HomePage.css';

function HomePage() {
    const [refreshTrigger, setRefreshTrigger] = useState(0);
    const [refreshing, setRefreshing] = useState(false);
    const [contextMenu, setContextMenu] = useState({ visible: false, x: 0, y: 0 });
    const { showToast } = useToast();

    const handleRefresh = useCallback(async () => {
        if (refreshing) return;
        setContextMenu(prev => ({ ...prev, visible: false }));
        setRefreshing(true);

        try {
            const response = await movieApi.refreshHomepage();
            if (response.success) {
                showToast(response.message || '刷新成功', 'success');
                setRefreshTrigger(prev => prev + 1);
            } else {
                showToast(getApiErrorMessage(response, '刷新失败'), 'error');
            }
        } catch (error) {
            logSafeError('刷新失败:', error);
            showToast(`刷新失败: ${getApiErrorMessage(error, '未知错误')}`, 'error');
        } finally {
            setRefreshing(false);
        }
    }, [refreshing, showToast]);

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

    useEffect(() => {
        const handleGlobalRefresh = () => {
            handleRefresh();
        };
        window.addEventListener('bus-terminal:refresh-home', handleGlobalRefresh);
        return () => window.removeEventListener('bus-terminal:refresh-home', handleGlobalRefresh);
    }, [handleRefresh]);

    return (
        <div className="home-page fade-in" onContextMenu={handleContextMenu}>
            <ContentContainer className="poster-content-container">
                <MovieInfiniteList
                    fetchUrl="https://www.javbus.com"
                    refreshTrigger={refreshTrigger}
                    lazyTranslate={true}
                    cacheKey="home:javbus"
                />
            </ContentContainer>
            {contextMenu.visible && (
                <div
                    className="page-context-menu"
                    style={{ left: contextMenu.x, top: contextMenu.y }}
                    role="menu"
                    onClick={(event) => event.stopPropagation()}
                >
                    <button type="button" onClick={handleRefresh} disabled={refreshing}>
                        <RefreshCw size={14} className={refreshing ? 'spin' : ''} />
                        {refreshing ? '正在同步...' : '刷新最新'}
                    </button>
                </div>
            )}
        </div>
    );
}

export default HomePage;
