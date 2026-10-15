import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { RefreshCw } from 'lucide-react';
import { useToast } from '../components/Toast';
import { getApiErrorMessage, movieApi } from '../api';
import MovieInfiniteList from '../components/MovieInfiniteList';
import ContentContainer from '../components/ContentContainer';
import './CategoryPage.css';

function CategoryPage() {
    const [searchParams] = useSearchParams();
    const url = searchParams.get('url');
    const { showToast } = useToast();
    const [refreshing, setRefreshing] = useState(false);
    const [refreshTrigger, setRefreshTrigger] = useState(0);
    const [contextMenu, setContextMenu] = useState({ visible: false, x: 0, y: 0 });

    const handleRefresh = async () => {
        if (!url) return;
        setContextMenu(prev => ({ ...prev, visible: false }));
        setRefreshing(true);

        try {
            const res = await movieApi.refreshCategory(url);
            if (res.success) {
                showToast(`刷新成功，新增 ${res.count} 部影片`, 'success');
                setRefreshTrigger(prev => prev + 1);
            } else {
                showToast(getApiErrorMessage(res, '刷新失败'), 'error');
            }
        } catch (error) {
            showToast(`刷新失败: ${getApiErrorMessage(error)}`, 'error');
        } finally {
            setRefreshing(false);
        }
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

    return (
        <div className="category-page fade-in" onContextMenu={handleContextMenu}>
            <ContentContainer className="poster-content-container">
                {url ? (
                    <MovieInfiniteList
                        key={url}
                        fetchUrl={url}
                        refreshTrigger={refreshTrigger}
                        cacheKey={`category:${url}`}
                    />
                ) : (
                    <div className="empty-state">
                        <p>无效的分类链接</p>
                    </div>
                )}
            </ContentContainer>
            {contextMenu.visible && (
                <div
                    className="page-context-menu"
                    style={{ left: contextMenu.x, top: contextMenu.y }}
                    role="menu"
                    onClick={(event) => event.stopPropagation()}
                >
                    <button type="button" onClick={handleRefresh} disabled={!url || refreshing}>
                        <RefreshCw size={14} className={refreshing ? 'spin' : ''} />
                        {refreshing ? '正在同步...' : '刷新最新'}
                    </button>
                </div>
            )}
        </div>
    );
}

export default CategoryPage;
