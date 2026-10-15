import React, { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import ContentContainer from '../components/ContentContainer';
import { getApiErrorMessage, logSafeError, movieApi } from '../api';
import MovieInfiniteList from '../components/MovieInfiniteList';
import { cancelListPrefetch, scheduleListPrefetch } from '../utils/listPrefetch';
import './DiscoveryPage.css';

function DiscoveryPage() {
  const [sources, setSources] = useState([]);
  // 默认 JavDB：封面清晰度最高（JavDB 800×538 vs DMM 590×800 / JavBus 600× 失效），
  // 与 bus 侧的「JavDB 优先」封面策略一致。
  const [activeSource, setActiveSource] = useState('javdb');
  const [rankType] = useState('daily');
  const [sourceListError, setSourceListError] = useState('');
  const [refreshTrigger, setRefreshTrigger] = useState(0);
  const [contextMenu, setContextMenu] = useState({ visible: false, x: 0, y: 0 });

  const fetchSources = async () => {
    try {
      const body = await movieApi.getDiscoverySources();
      if (body.success) {
        const list = Array.isArray(body.data) ? body.data : [];
        // JavDB 排第一：封面最清晰，且与 bus 侧封面优先策略一致
        list.sort((a, b) => (a.id === 'javdb' ? -1 : 0) - (b.id === 'javdb' ? -1 : 0));
        setSources(list);
        // 默认源在列表里没有对应项时（后端调整了源），回落到第一个
        if (list.length && !list.some(s => s.id === activeSource)) {
          setActiveSource(list[0].id);
        }
        setSourceListError('');
      } else {
        setSources([]);
        setSourceListError(getApiErrorMessage(body, '发现源列表加载失败'));
      }
    } catch (error) {
      logSafeError('Discovery sources failed:', error);
      setSources([]);
      setSourceListError(getApiErrorMessage(error, '发现源列表加载失败'));
    }
  };

  // 取数回调：交给统一的 MovieInfiniteList 处理无限滚动 + 图片预加载 + 后台预取
  const fetchPage = useCallback((pageNum, limit, forceRefresh) => {
    const params = { source: activeSource, page: pageNum, rank_type: rankType };
    if (forceRefresh) params.force_refresh = true;
    return movieApi.getDiscoveryMovies(params);
  }, [activeSource, rankType]);

  const handleSourceChange = (id) => {
    if (id === activeSource) return;
    setActiveSource(id);
  };

  const handleRefresh = () => {
    setContextMenu(prev => ({ ...prev, visible: false }));
    if (sourceListError) fetchSources();
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

  useEffect(() => {
    fetchSources();
  }, []);

  return (
    <div className="discovery-page fade-in" onContextMenu={handleContextMenu}>
      <ContentContainer className="poster-content-container">
        <section className="toolbar">
          <div className="pill-tabs">
            {sources.map(source => {
              // 悬停即预热该榜单页：切换时命中缓存直接渲染
              const listCacheKey = `discovery:${source.id}:${rankType}`;
              const prefetchRanking = () => scheduleListPrefetch(
                listCacheKey,
                () => movieApi.getDiscoveryMovies({ source: source.id, page: 1, rank_type: rankType }),
              );
              return (
                <button
                  key={source.id}
                  className={`pill-tab${activeSource === source.id ? ' active' : ''}`}
                  onClick={() => handleSourceChange(source.id)}
                  onMouseEnter={prefetchRanking}
                  onFocus={prefetchRanking}
                  onMouseLeave={() => cancelListPrefetch(listCacheKey)}
                >
                  {source.name}
                </button>
              );
            })}
          </div>
          {sourceListError && (
            <div className="cache-state-notice error">
              <span>{sourceListError}</span>
              <button type="button" className="secondary-btn" onClick={fetchSources}>重试</button>
            </div>
          )}
        </section>

        <MovieInfiniteList
          key={activeSource}
          fetchPage={fetchPage}
          refreshTrigger={refreshTrigger}
          cacheKey={`discovery:${activeSource}:${rankType}`}
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

export default DiscoveryPage;
