import React, { useState, useEffect, useCallback } from 'react';
import { Link } from 'react-router-dom';
import {
    Film,
    Server,
    RefreshCw,
    Eye,
    Heart,
    Sliders,
    AlertCircle,
    Activity,
} from 'lucide-react';
import { Chart as ChartJS, ArcElement, BarElement, CategoryScale, LinearScale, Tooltip, Legend } from 'chart.js';
import { Doughnut } from 'react-chartjs-2';
import { logSafeError, movieApi } from '../api';
import ContentContainer from '../components/ContentContainer';
import MovieCard from '../components/MovieCard';
import MovieInfiniteList from '../components/MovieInfiniteList';
import './LibraryPage.css';

ChartJS.register(ArcElement, BarElement, CategoryScale, LinearScale, Tooltip, Legend);

const SORT_OPTIONS = [
    { value: 'DateCreated', label: '入库时间' },
    { value: 'PremiereDate', label: '发行日期' },
    { value: 'SortName', label: '名称' },
    { value: 'CommunityRating', label: '评分' },
    { value: 'PlayCount', label: '播放次数' },
];

const BEHAVIOR_BUCKETS = [
    { key: 'watched_not_favorite', label: '已看未收藏', desc: '清理候选' },
    { key: 'watched_and_favorite', label: '已看且收藏', desc: '强偏好样本' },
    { key: 'favorite_unwatched', label: '收藏未看', desc: '待观看偏好' },
];

// 把 Jellyfin 条目规整成 MovieCard 需要的结构
const normalizeItem = (item) => ({
    id: item.movie_id || item.id || `JF-${item.movie_id || Math.random().toString(36).slice(2)}`,
    title: item.title,
    original_title: item.original_title,
    cover: item.cover,
    date: item.premiere_date ? item.premiere_date.slice(0, 10) : '',
    actress: item.actress,
    ...item
});

export default function LibraryPage() {
    const [activeTab, setActiveTab] = useState('assets'); // 'assets' | 'insights'
    const [libraries, setLibraries] = useState([]);
    const [selectedLibId, setSelectedLibId] = useState('');
    const [total, setTotal] = useState(0);
    const [loading, setLoading] = useState(false);
    const [stats, setStats] = useState(null);
    const [categoryDistribution, setCategoryDistribution] = useState([]);
    const [behavior, setBehavior] = useState(null);
    const [behaviorLoading, setBehaviorLoading] = useState(false);
    const [selectedBucket, setSelectedBucket] = useState('watched_not_favorite');

    // 离线/未配置状态
    const [isOffline, setIsOffline] = useState(false);
    const [refreshTrigger, setRefreshTrigger] = useState(0);

    // 筛选参数
    const [filters, setFilters] = useState({
        watched: '',
        liked: '',
        sort_by: 'DateCreated',
        sort_order: 'Descending',
        query: '',
    });

    // 1. 初始化加载 Jellyfin 基础状态与媒体库
    const initData = useCallback(async () => {
        setLoading(true);
        setIsOffline(false);
        try {
            const [libRes, statRes] = await Promise.allSettled([
                movieApi.getJfLibraries(),
                movieApi.getJellyfinStats(),
            ]);

            if (libRes.status === 'fulfilled' && libRes.value?.success) {
                const libList = Array.isArray(libRes.value.data) ? libRes.value.data : [];
                const prefId = libRes.value.preferred_library_id;
                setLibraries(libList);
                if (libList.length > 0) {
                    const defaultId = (prefId && libList.some(l => l.id === prefId)) ? prefId : libList[0].id;
                    setSelectedLibId(defaultId);
                }
            } else {
                setIsOffline(true);
            }

            if (statRes.status === 'fulfilled' && statRes.value?.success) {
                setStats(statRes.value.data);
            }
            movieApi.getLibraryCategoryDistribution()
                .then((categoryRes) => {
                    if (categoryRes?.success) {
                        setCategoryDistribution(Array.isArray(categoryRes.data) ? categoryRes.data : []);
                    }
                })
                .catch((err) => logSafeError('加载黄片数据库分类统计失败:', err));
        } catch (err) {
            logSafeError('初始化 Jellyfin 媒体库失败:', err);
            setIsOffline(true);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        initData();
    }, [initData]);

    // 3. 加载行为洞察
    const loadBehavior = useCallback(async () => {
        setBehaviorLoading(true);
        try {
            const res = await movieApi.getJellyfinBehaviorInsights({ scan_limit: 500 });
            if (res && res.success) {
                setBehavior(res.data);
            }
        } catch (e) {
            logSafeError('加载行为洞察失败:', e);
        } finally {
            setBehaviorLoading(false);
        }
    }, []);

    useEffect(() => {
        if (activeTab === 'insights') {
            loadBehavior();
        }
    }, [loadBehavior, activeTab]);

    // 取数回调：交给统一的 MovieInfiniteList 处理无限滚动 + 图片预加载 + 后台预取
    const fetchPage = useCallback((pageNum, limit) => {
        return movieApi.getJfLibraryItems({
            library_id: selectedLibId,
            sort_by: filters.sort_by,
            sort_order: filters.sort_order,
            limit,
            offset: (pageNum - 1) * limit,
        });
    }, [selectedLibId, filters.sort_by, filters.sort_order]);

    const getHasMore = useCallback((response, loaded) => {
        return loaded < (response?.total || 0);
    }, []);

    const renderItem = useCallback((item) => (
        <MovieCard movie={normalizeItem(item)} showPlayButton />
    ), []);

    const handleRefresh = () => {
        initData();
        setRefreshTrigger(t => t + 1);
    };

    const reloadKey = `${selectedLibId}|${filters.sort_by}|${filters.sort_order}`;

    const categoryChartData = {
        labels: categoryDistribution.map((item) => item.name),
        datasets: [{
            data: categoryDistribution.map((item) => Number(item.count) || 0),
            backgroundColor: ['#f5b841', '#60a5fa', '#10b981', '#f97316', '#a78bfa', '#38bdf8', '#f472b6', '#94a3b8', '#475569', '#334155'],
            borderColor: '#1b1f28',
            borderWidth: 3,
        }],
    };

    const behaviorChartData = {
        labels: BEHAVIOR_BUCKETS.map((bucket) => bucket.label),
        datasets: [{
            data: BEHAVIOR_BUCKETS.map((bucket) => Number(behavior?.buckets?.[bucket.key]?.count) || 0),
            backgroundColor: ['#60a5fa', '#10b981', '#f5b841'],
            borderColor: '#1b1f28',
            borderWidth: 3,
        }],
    };

    const chartOptions = {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: { labels: { color: '#b8bfcc', boxWidth: 12, padding: 14, font: { size: 12 } } },
            tooltip: { backgroundColor: '#171a21', titleColor: '#f4f6fb', bodyColor: '#b8bfcc' },
        },
        scales: {
            x: { ticks: { color: '#7d8596', maxRotation: 45, minRotation: 0 }, grid: { display: false } },
            y: { beginAtZero: true, ticks: { color: '#7d8596', precision: 0 }, grid: { color: 'rgba(255,255,255,0.07)' } },
        },
    };

    const doughnutOptions = {
        ...chartOptions,
        cutout: '68%',
        plugins: { ...chartOptions.plugins, legend: { ...chartOptions.plugins.legend, position: 'bottom' } },
        scales: undefined,
    };

    return (
        <div className="library-page fade-in">
            <ContentContainer>
                {/* 页头与 Tab 切换 */}
                <div className="library-header">
                    <div className="header-left">
                        <div className="sub-header-nav">
                            <button
                                className={`sub-nav-btn ${activeTab === 'assets' ? 'active' : ''}`}
                                onClick={() => setActiveTab('assets')}
                            >
                                <Film size={16} /> 媒体资产 ({total})
                            </button>
                            <button
                                className={`sub-nav-btn ${activeTab === 'insights' ? 'active' : ''}`}
                                onClick={() => setActiveTab('insights')}
                            >
                                <Activity size={16} /> 同步与行为洞察
                            </button>
                        </div>
                    </div>

                    <div className="header-right">
                        <Link to="/settings" className="btn btn-secondary btn-sm">
                            <Sliders size={14} /> 服务配置
                        </Link>
                        <button className="btn btn-primary btn-sm" onClick={handleRefresh} disabled={loading}>
                            <RefreshCw size={14} className={loading ? 'spin' : ''} /> 刷新
                        </button>
                    </div>
                </div>

                {/* 离线/未连接 Banner 引导 */}
                {isOffline && (
                    <div className="jellyfin-offline-banner card">
                        <AlertCircle size={20} className="warning-icon" />
                        <div className="banner-content">
                            <h4>Jellyfin 服务未连接或暂未配置</h4>
                            <p>系统无法通讯至远程 Jellyfin 媒体服务器，但不影响正常浏览本地已入库资产。</p>
                        </div>
                        <Link to="/settings" className="btn btn-primary btn-sm">
                            前往配置中心连接
                        </Link>
                    </div>
                )}

                {/* Tab 1: 媒体资产 */}
                {activeTab === 'assets' && (
                    <div className="tab-content assets-tab">
                        <div className="assets-filter-bar card">
                            <div className="filter-group">
                                <label>库分类:</label>
                                <select
                                    value={selectedLibId}
                                    onChange={(e) => setSelectedLibId(e.target.value)}
                                    disabled={libraries.length === 0}
                                >
                                    {libraries.length === 0 && <option value="">无可用媒体库</option>}
                                    {libraries.map((lib) => (
                                        <option key={lib.id} value={lib.id}>
                                            {lib.name}
                                        </option>
                                    ))}
                                </select>
                            </div>

                            <div className="filter-group">
                                <label>排序:</label>
                                <select
                                    value={filters.sort_by}
                                    onChange={(e) => setFilters({ ...filters, sort_by: e.target.value })}
                                >
                                    {SORT_OPTIONS.map((opt) => (
                                        <option key={opt.value} value={opt.value}>
                                            {opt.label}
                                        </option>
                                    ))}
                                </select>
                            </div>
                        </div>

                        <MovieInfiniteList
                            key={reloadKey}
                            fetchPage={fetchPage}
                            getHasMore={getHasMore}
                            renderItem={renderItem}
                            gridClassName="poster-grid"
                            refreshTrigger={refreshTrigger}
                            cacheKey={`library:${reloadKey}`}
                            onPageLoaded={(response) => setTotal(response?.total || 0)}
                        />
                    </div>
                )}

                {/* Tab 2: 同步与行为洞察 */}
                {activeTab === 'insights' && (
                    <div className="tab-content insights-tab">
                        <div className="stats-overview-grid">
                            <div className="stat-card card">
                                <Server size={20} className="stat-icon" />
                                <div className="stat-body">
                                <span className="stat-val">{stats?.total_movies ?? stats?.total_items ?? 0}</span>
                                    <span className="stat-lbl">媒体库总影片数</span>
                                </div>
                            </div>
                            <div className="stat-card card">
                                <Eye size={20} className="stat-icon" />
                                <div className="stat-body">
                                <span className="stat-val">{stats?.watched_movies ?? stats?.watched_count ?? 0}</span>
                                    <span className="stat-lbl">已观影数量</span>
                                </div>
                            </div>
                            <div className="stat-card card">
                                <Heart size={20} className="stat-icon" />
                                <div className="stat-body">
                                <span className="stat-val">{stats?.favorite_movies ?? stats?.favorites_count ?? 0}</span>
                                    <span className="stat-lbl">收藏影片数</span>
                                </div>
                            </div>
                        </div>

                        <div className="library-insights-grid">
                            <section className="library-chart-card card">
                                <div className="library-chart-header">
                                    <div>
                                    <h3>黄片数据库分类占比</h3>
                                    <p>本地影片标签分布</p>
                                    </div>
                                    <Server size={18} />
                                </div>
                                <div className="library-chart-canvas library-chart-canvas-wide">
                                    <Doughnut data={categoryChartData} options={doughnutOptions} />
                                </div>
                            </section>
                            <section className="library-chart-card card">
                                <div className="library-chart-header">
                                    <div>
                                        <h3>观影偏好</h3>
                                        <p>收藏与观看行为分布</p>
                                    </div>
                                    <Heart size={18} />
                                </div>
                                <div className="library-chart-canvas library-chart-canvas-doughnut">
                                    <Doughnut data={behaviorChartData} options={doughnutOptions} />
                                </div>
                            </section>
                        </div>

                        <div className="behavior-card card">
                            <div className="card-header">
                                <h3><Eye size={18} /> 观影偏好与行为洞察</h3>
                                <button className="btn btn-secondary btn-sm" onClick={loadBehavior} disabled={behaviorLoading}>
                                    刷新洞察
                                </button>
                            </div>

                            {behaviorLoading ? (
                                <div className="loading-state"><div className="spinner-sm" /> 正在计算偏好...</div>
                            ) : behavior ? (
                                <div className="behavior-buckets-grid">
                                    {BEHAVIOR_BUCKETS.map((b) => {
                                        const count = behavior.buckets?.[b.key]?.count || 0;
                                        return (
                                            <div
                                                className={`bucket-item ${selectedBucket === b.key ? 'active' : ''}`}
                                                key={b.key}
                                                onClick={() => setSelectedBucket(b.key)}
                                            >
                                                <span className="bucket-label">{b.label}</span>
                                                <span className="bucket-count">{count}</span>
                                                <span className="bucket-desc">{b.desc}</span>
                                            </div>
                                        );
                                    })}
                                </div>
                            ) : (
                                <p className="hint-text">暂无行为洞察数据，请确保已关联 Jellyfin 账号</p>
                            )}
                        </div>
                    </div>
                )}
            </ContentContainer>
        </div>
    );
}
