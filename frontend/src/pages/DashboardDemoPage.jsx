import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { Activity, Database, Download, Film, HardDrive, Heart, RefreshCw, Server, Star, Zap } from 'lucide-react';
import { getApiErrorMessage, movieApi } from '../api';
import './DashboardDemo.css';

const emptyDashboard = {
    core: null,
    qb: null,
    jellyfin: null,
    subscriptions: null,
    tasks: null,
};

const DashboardDemoPage = () => {
    const [dashboard, setDashboard] = useState(emptyDashboard);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');

    const loadDashboard = async () => {
        setLoading(true);
        setError('');
        try {
            const requests = [
                { key: 'core', label: '核心统计', promise: movieApi.getDashboardStats() },
                { key: 'qb', label: 'qB 状态', promise: movieApi.getQBDashboard() },
                { key: 'jellyfin', label: 'Jellyfin 状态', promise: movieApi.getJellyfinStats() },
                { key: 'subscriptions', label: '订阅统计', promise: movieApi.getSubscriptionStats() },
                { key: 'tasks', label: '后台任务', promise: movieApi.getTaskStatus() },
            ];
            const results = await Promise.allSettled(requests.map(item => item.promise));
            const nextDashboard = { ...emptyDashboard };
            const errors = [];

            results.forEach((result, index) => {
                const { key, label } = requests[index];
                const parsed = getDashboardDataResult(result, `${label}加载失败`);
                nextDashboard[key] = parsed.data;
                if (parsed.error) errors.push(`${label}: ${parsed.error}`);
            });

            setDashboard(nextDashboard);
            if (errors.length) {
                setError(`部分数据加载失败：${errors.join('；')}`);
            }
        } catch (e) {
            setError(getApiErrorMessage(e, '数据加载失败'));
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        loadDashboard();
    }, []);

    const core = dashboard.core || {};
    const qb = dashboard.qb || {};
    const jellyfin = dashboard.jellyfin || {};
    const queueStats = qb.queue_stats || core.queue_stats || {};
    const qTasks = qb.tasks || {};
    const subscriptionSummary = useMemo(() => summarizeSubscriptions(dashboard.subscriptions), [dashboard.subscriptions]);
    const taskSummary = useMemo(() => summarizeTasks(dashboard.tasks), [dashboard.tasks]);
    const trend = Array.isArray(core.download_trend) ? core.download_trend : [];
    const maxTrend = Math.max(1, ...trend.map(item => Number(item.count) || 0));
    const watchedPercentage = getPercent(core.watched_count, core.movies_count);
    const jellyfinFavoriteRate = getPercent(jellyfin.favorites_count, jellyfin.total_items);
    const qbjav = qTasks.jav || {};
    const qbpt = qTasks.pt || {};

    return (
        <div className="dashboard-demo-container">
            <div className="dashboard-heading">
                <div>
                    <h1 className="dashboard-title">数据看板</h1>
                    <p className="dashboard-subtitle">BusTerminal Operations Dashboard</p>
                </div>
                <button type="button" className="dashboard-refresh-btn" onClick={loadDashboard} disabled={loading}>
                    <RefreshCw size={16} /> {loading ? '刷新中' : '刷新'}
                </button>
            </div>

            {error && <div className="dashboard-error">{error}</div>}

            <div className="dashboard-grid-top">
                <div className="dash-card library-card">
                    <div className="card-header">
                        <div className="icon-wrapper purple"><Film size={20} /></div>
                        <div>
                            <h3>媒体库</h3>
                            <div className="sub-text">Database</div>
                        </div>
                    </div>
                    <div className="card-body">
                        <div className="big-number">{formatNumber(core.movies_count)} <span className="unit">部</span></div>
                        <div className="progress-section">
                            <div className="progress-labels">
                                <span>已阅 {formatNumber(core.watched_count)}</span>
                                <span>{watchedPercentage}%</span>
                            </div>
                            <div className="progress-bar-bg">
                                <div className="progress-bar-fill" style={{ width: `${watchedPercentage}%` }} />
                            </div>
                        </div>
                    </div>
                </div>

                <div className="dash-card actress-card">
                    <div className="card-header">
                        <div className="icon-wrapper pink"><Star size={20} /></div>
                        <div>
                            <h3>演员订阅</h3>
                            <div className="sub-text">Subscription Graph</div>
                        </div>
                    </div>
                    <div className="card-body">
                        <div className="stat-row">
                            <span className="label">总收录</span>
                            <span className="value">{formatNumber(core.actresses_count)}</span>
                        </div>
                        <div className="stat-row highlight">
                            <span className="label">已订阅</span>
                            <span className="value">{formatNumber(core.subscribed_actresses_count ?? core.followed_actresses_count)}</span>
                        </div>
                        <div className="stat-row">
                            <span className="label">有 Jellyfin 元数据</span>
                            <span className="value">{formatNumber(jellyfin.actresses_with_metadata)}</span>
                        </div>
                    </div>
                </div>

                <div className="dash-card health-card">
                    <div className="card-header">
                        <div className="icon-wrapper blue"><Zap size={20} /></div>
                        <div>
                            <h3>系统状态</h3>
                            <div className="sub-text">Service Health</div>
                        </div>
                    </div>
                    <div className="card-body service-list">
                        <ServiceRow name="qBittorrent" connected={qb.connected ?? core.qb_status?.connected} meta={qb.disk?.free_display || core.qb_status?.message || '未知'} />
                        <ServiceRow name="Jellyfin" connected={jellyfin.connected || core.jellyfin_status?.connected} meta={jellyfin.server_info?.version || core.jellyfin_status?.message || '未知'} />
                        <ServiceRow
                            name="后台任务"
                            connected={taskSummary.error === 0}
                            meta={`${taskSummary.running} 运行 / ${taskSummary.retrying} 重试 / ${taskSummary.error} 异常`}
                        />
                    </div>
                </div>
            </div>

            <div className="dashboard-grid-bottom">
                <div className="dash-card download-card">
                    <div className="card-header-simple">
                        <h3>下载中心</h3>
                        <span className="badge">
                            {queueStats.pending || 0} 待处理 / {queueStats.waiting_external_service || 0} 等待服务
                        </span>
                    </div>
                    <div className="download-stats-row">
                        <Metric value={core.total_downloads} label="历史下载" icon={Download} />
                        <Metric value={core.subscriptions_count} label="订阅" icon={Activity} />
                        <Metric value={queueStats.waiting_external_service || 0} label="等待服务" />
                        <Metric value={queueStats.failed || 0} label="失败" danger />
                    </div>
                    <div className="chart-placeholder">
                        {trend.map((item) => (
                            <div key={item.date} className="chart-bar-col">
                                <div className="chart-bar" style={{ height: `${Math.max(4, (Number(item.count) || 0) / maxTrend * 90)}px` }}>
                                    <span className="tooltip">{item.count}</span>
                                </div>
                                <span className="chart-label">{formatTrendDate(item.date)}</span>
                            </div>
                        ))}
                    </div>
                </div>

                <div className="dash-card actions-card">
                    <div className="card-header-simple"><h3>实时下载</h3></div>
                    <div className="dashboard-kv-list">
                        <KvRow label="下载速度" value={qb.transfer?.dl_speed_display || '-'} />
                        <KvRow label="上传速度" value={qb.transfer?.up_speed_display || '-'} />
                        <KvRow label="JAV 任务" value={`${qbjav.downloading_count || 0}/${qbjav.total_count || 0}`} />
                        <KvRow label="PT 任务" value={`${qbpt.downloading_count || 0}/${qbpt.total_count || 0}`} />
                    </div>
                    <Link to="/downloads" className="dashboard-link-btn">进入下载管理</Link>
                </div>
            </div>

            <div className="dashboard-grid-bottom dashboard-grid-secondary">
                <div className="dash-card">
                    <div className="card-header">
                        <div className="icon-wrapper yellow"><Server size={20} /></div>
                        <div>
                            <h3>Jellyfin 生命周期</h3>
                            <div className="sub-text">Viewing Signals</div>
                        </div>
                    </div>
                    <div className="dashboard-metrics-grid">
                        <Metric value={jellyfin.total_items} label="媒体项" icon={Database} />
                        <Metric value={jellyfin.watched_count} label="已观看" icon={Film} />
                        <Metric value={jellyfin.favorites_count} label="收藏" icon={Heart} />
                        <Metric value={`${jellyfinFavoriteRate}%`} label="收藏率" />
                    </div>
                </div>

                <div className="dash-card">
                    <div className="card-header">
                        <div className="icon-wrapper green"><HardDrive size={20} /></div>
                        <div>
                            <h3>存储与命中</h3>
                            <div className="sub-text">Storage & Subscriptions</div>
                        </div>
                    </div>
                    <div className="dashboard-kv-list">
                        <KvRow label="qB 剩余空间" value={qb.disk?.free_display || '-'} />
                        <KvRow label="估算已用" value={qb.disk?.estimated_used_display || '-'} />
                        <KvRow label="订阅命中" value={subscriptionSummary.totalHits} />
                        <KvRow label="活跃来源" value={subscriptionSummary.activeSources} />
                    </div>
                </div>
            </div>
        </div>
    );
};

function ServiceRow({ name, connected, meta }) {
    return (
        <div className="service-item">
            <div className="service-info">
                <span className={`status-dot ${connected ? 'green' : 'red'}`} />
                <span className="service-name">{name}</span>
            </div>
            <div className="service-meta">{meta}</div>
        </div>
    );
}

function Metric({ value, label, icon: Icon, danger = false }) {
    return (
        <div className="d-stat">
            <div className={`d-val ${danger ? 'text-red' : ''}`}>
                {Icon && <Icon size={16} />} {formatNumber(value)}
            </div>
            <div className="d-lbl">{label}</div>
        </div>
    );
}

function KvRow({ label, value }) {
    return (
        <div className="dashboard-kv-row">
            <span>{label}</span>
            <strong>{value}</strong>
        </div>
    );
}

function getDashboardDataResult(result, fallback) {
    if (result.status !== 'fulfilled') {
        return { data: null, error: getApiErrorMessage(result.reason, fallback) };
    }
    if (!result.value?.success) {
        return { data: null, error: getApiErrorMessage(result.value, fallback) };
    }
    return { data: result.value.data || result.value, error: '' };
}

function summarizeSubscriptions(stats) {
    if (!stats || typeof stats !== 'object') return { totalHits: 0, activeSources: 0 };
    const values = Object.values(stats);
    return {
        totalHits: values.reduce((sum, item) => sum + Number(item?.count || 0), 0),
        activeSources: values.filter(item => Number(item?.count || 0) > 0).length,
    };
}

function summarizeTasks(tasks) {
    if (!tasks || typeof tasks !== 'object') return { running: 0, retrying: 0, error: 0 };
    const values = Array.isArray(tasks.tasks) ? tasks.tasks : [];
    const externalQueue = tasks.external_queue?.available ? tasks.external_queue : {};
    const metadataQueue = tasks.metadata_queue?.available ? tasks.metadata_queue : {};
    return {
        running: values.filter(item => item?.status === 'running').length,
        retrying: values.filter(item => Number(item?.consecutive_error_count || 0) > 0).length
            + Number(externalQueue.waiting || 0)
            + Number(metadataQueue.waiting || 0),
        error: values.filter(item => item?.status === 'failed').length
            + Number(externalQueue.needs_attention || 0)
            + Number(metadataQueue.needs_attention || 0),
    };
}

function getPercent(value, total) {
    const numericTotal = Number(total) || 0;
    if (numericTotal <= 0) return 0;
    return Math.round(((Number(value) || 0) / numericTotal) * 100);
}

function formatNumber(value) {
    if (value === undefined || value === null || value === '') return '-';
    if (typeof value === 'string' && value.endsWith('%')) return value;
    const number = Number(value);
    if (!Number.isFinite(number)) return value;
    return number.toLocaleString();
}

function formatTrendDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return `${date.getMonth() + 1}/${date.getDate()}`;
}

export default DashboardDemoPage;
