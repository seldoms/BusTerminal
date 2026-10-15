import React, { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import { Link } from 'react-router-dom';
import { Activity, Clock, Download, Upload, Pause, Play, AlertTriangle, Search, CheckCircle, XCircle, Package, Tag, RefreshCw } from 'lucide-react';
import { Chart as ChartJS, ArcElement, BarElement, CategoryScale, LinearScale, Tooltip, Legend } from 'chart.js';
import { Bar, Doughnut } from 'react-chartjs-2';
import { getApiErrorMessage, logSafeError, movieApi, redactText } from '../api';
import { useToast } from '../components/Toast';
import './DownloadsPage.css';

ChartJS.register(ArcElement, BarElement, CategoryScale, LinearScale, Tooltip, Legend);

const QB_CONTROL_ALL_CONFIRMATION = 'CONTROL_ALL_QB_TORRENTS';
const QB_TORRENTS_FETCH_LIMIT = 5000;
const QUEUE_PAGE_SIZE = 10;

// 本地下载队列的状态元数据：中文标签 + 徽章配色
const QUEUE_STATUS_META = {
    pending: { label: '待提交', badge: 'secondary' },
    waiting_external_service: { label: '等待外部服务', badge: 'warning' },
    submitted: { label: '已提交', badge: 'success' },
    failed: { label: '失败', badge: 'error' },
    duplicate_skipped: { label: '重复跳过', badge: 'secondary' },
    cancelled: { label: '已取消', badge: 'secondary' },
};

// 状态筛选下拉可选项（空字符串 = 全部）
const QUEUE_STATUS_FILTERS = [
    { value: '', label: '全部状态' },
    { value: 'pending', label: '待提交' },
    { value: 'waiting_external_service', label: '等待外部服务' },
    { value: 'submitted', label: '已提交' },
    { value: 'failed', label: '失败' },
    { value: 'duplicate_skipped', label: '重复跳过' },
    { value: 'cancelled', label: '已取消' },
];

// 每个动作对应的接口、文案与确认提示
const QUEUE_ACTION_META = {
    retry: {
        label: '立即重试',
        hint: '将下次重试时间设为现在，交由调度器在下一轮拾取。',
        request: (id) => movieApi.retryDownloadTask(id),
    },
    recover: {
        label: '恢复',
        hint: '把失败/已取消的任务重置为待提交，重试计数归零。',
        request: (id) => movieApi.recoverDownloadTask(id),
    },
    cancel: {
        label: '取消',
        hint: '取消该任务并释放去重占位。',
        request: (id) => movieApi.cancelDownloadTask(id),
    },
};

// 后端返回的错误码 -> 中文提示（需与 database.py 的 reason 保持一致）
const QUEUE_ERROR_MESSAGES = {
    task_not_found: '任务不存在或已被移除',
    invalid_status_filter: '无效的状态筛选',
    invalid_state_for_retry: '当前状态不支持立即重试（或已达最大重试次数，请用「恢复」）',
    invalid_state_for_recover: '当前状态不支持恢复',
    recover_conflict_active_exists: '已存在同一影片的活跃任务，无法恢复',
    cannot_cancel_in_state: '当前状态不支持取消',
};

// 依据状态计算可用动作按钮
const getQueueActions = (status, retryCount) => {
    switch (status) {
        case 'waiting_external_service':
            return ['retry', 'cancel'];
        case 'pending':
            return ['cancel'];
        case 'failed':
            // 失败且未达上限可重试；无论是否达上限都可恢复/取消
            return (Number(retryCount) || 0) < 5
                ? ['retry', 'recover', 'cancel']
                : ['recover', 'cancel'];
        case 'cancelled':
            return ['recover'];
        default:
            return [];
    }
};

const formatQueueTime = (value) => {
    if (!value) return '-';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString();
};

function DownloadsPage() {
    const [torrents, setTorrents] = useState([]);
    const [stats, setStats] = useState(null);
    const [loading, setLoading] = useState(true);
    const [filterJav, setFilterJav] = useState(true);
    const [pageSize, setPageSize] = useState(10);
    const [currentPage, setCurrentPage] = useState(1);
    const [sortMode, setSortMode] = useState('progress_desc');
    const [selectedHashes, setSelectedHashes] = useState(new Set());
    const [controlling, setControlling] = useState(false);
    const [filteringFiles, setFilteringFiles] = useState(false);
    const [loadError, setLoadError] = useState('');
    const [queueItems, setQueueItems] = useState([]);
    const [queueByStatus, setQueueByStatus] = useState(null);
    const [queueTotal, setQueueTotal] = useState(0);
    const [queueStatusFilter, setQueueStatusFilter] = useState('');
    const [queuePage, setQueuePage] = useState(1);
    const [queueActionId, setQueueActionId] = useState('');
    const { showToast } = useToast();
    const dataRequestSeqRef = useRef(0);

    const fetchAllTorrents = useCallback(async () => {
        let offset = 0;
        let total = 0;
        const allTorrents = [];
        let hasMore = true;

        while (hasMore) {
            const res = await movieApi.getTorrents({
                limit: QB_TORRENTS_FETCH_LIMIT,
                offset,
            });
            if (!res.success) return res;

            const chunk = Array.isArray(res.data) ? res.data : [];
            total = Number(res.total ?? chunk.length) || chunk.length;
            allTorrents.push(...chunk);

            if (chunk.length === 0 || allTorrents.length >= total) {
                return { ...res, data: allTorrents, total };
            }
            offset += chunk.length;
            hasMore = offset < total;
        }
        return { success: true, data: allTorrents, total };
    }, []);

    const isFetchingRef = React.useRef(false);

    const loadData = useCallback(async () => {
        if (isFetchingRef.current) return false;
        isFetchingRef.current = true;
        const requestSeq = ++dataRequestSeqRef.current;
        const isStaleRequest = () => requestSeq !== dataRequestSeqRef.current;
        try {
            setLoadError('');
            // 并行请求看板数据、种子列表和本地队列
            const [dashboardRes, torrentsRes, queueRes] = await Promise.all([
                movieApi.getQBDashboard(),
                fetchAllTorrents(),
                movieApi.getDownloadQueue({
                    status: queueStatusFilter || undefined,
                    limit: QUEUE_PAGE_SIZE,
                    offset: (queuePage - 1) * QUEUE_PAGE_SIZE,
                })
            ]);
            if (isStaleRequest()) return false;

            if (dashboardRes.success && dashboardRes.data && typeof dashboardRes.data === 'object') {
                setStats(dashboardRes.data);
            } else {
                setStats(null);
            }

            if (queueRes.success && queueRes.data && typeof queueRes.data === 'object') {
                setQueueItems(Array.isArray(queueRes.data.items) ? queueRes.data.items : []);
                setQueueByStatus(queueRes.data.by_status || null);
                setQueueTotal(Number(queueRes.data.total) || 0);
            } else {
                setQueueItems([]);
                setQueueByStatus(null);
                setQueueTotal(0);
            }

            if (torrentsRes.success) {
                const torrentList = Array.isArray(torrentsRes.data) ? torrentsRes.data : [];
                setTorrents(torrentList);
                const liveHashes = new Set(torrentList.map(t => t.hash));
                setSelectedHashes(prev => new Set([...prev].filter(hash => liveHashes.has(hash))));
            } else {
                setTorrents([]);
                setSelectedHashes(new Set());
                setLoadError(getApiErrorMessage(torrentsRes, '下载任务加载失败'));
            }
            return torrentsRes.success;
        } catch (error) {
            if (isStaleRequest()) return false;
            logSafeError('Failed to load data:', error);
            setStats(null);
            setTorrents([]);
            setSelectedHashes(new Set());
            setLoadError(getApiErrorMessage(error, '下载任务加载失败'));
            return false;
        } finally {
            isFetchingRef.current = false;
            if (!isStaleRequest()) setLoading(false);
        }
    }, [fetchAllTorrents, queuePage, queueStatusFilter]);

    useEffect(() => {
        let cancelled = false;
        let refreshTimer;
        const refresh = async () => {
            const qbOnline = await loadData();
            if (!cancelled) {
                refreshTimer = window.setTimeout(refresh, qbOnline ? 8000 : 30000);
            }
        };

        refresh();
        return () => {
            cancelled = true;
            window.clearTimeout(refreshTimer);
        };
    }, [loadData]);

    // 磁盘颜色逻辑
    const getDiskColorClass = (percent) => {
        if (percent > 90) return 'danger';
        if (percent > 80) return 'warning';
        return '';
    };

    // 过滤逻辑
    const filteredTorrents = useMemo(() => {
        return filterJav
            ? torrents.filter(t => t.tags && t.tags.toLowerCase().includes('jav'))
            : torrents;
    }, [filterJav, torrents]);

    const displayedTorrents = useMemo(() => {
        const getAddedTime = (torrent) => Number(torrent.added_on || torrent.addedOn || torrent.added_at || 0);
        return [...filteredTorrents].sort((a, b) => {
            if (sortMode.startsWith('added')) {
                const diff = getAddedTime(b) - getAddedTime(a);
                if (diff !== 0) return sortMode === 'added_desc' ? diff : -diff;
            } else {
                const diff = (Number(b.progress) || 0) - (Number(a.progress) || 0);
                if (diff !== 0) return sortMode === 'progress_desc' ? diff : -diff;
            }
            return String(a.name || '').localeCompare(String(b.name || ''));
        });
    }, [filteredTorrents, sortMode]);
    const displayedHashes = useMemo(
        () => displayedTorrents.map(t => t.hash).filter(Boolean),
        [displayedTorrents]
    );
    const totalPages = Math.max(1, Math.ceil(displayedTorrents.length / pageSize));
    const pageStart = (currentPage - 1) * pageSize;
    const pagedTorrents = displayedTorrents.slice(pageStart, pageStart + pageSize);
    const selectedVisibleCount = displayedHashes.filter(hash => selectedHashes.has(hash)).length;
    const allVisibleSelected = displayedHashes.length > 0 && selectedVisibleCount === displayedHashes.length;
    const someSelected = selectedHashes.size > 0;
    const queueTotalPages = Math.max(1, Math.ceil(queueTotal / QUEUE_PAGE_SIZE));

    useEffect(() => {
        setCurrentPage(1);
    }, [filterJav, pageSize, sortMode]);

    useEffect(() => {
        setQueuePage(1);
    }, [queueStatusFilter]);

    useEffect(() => {
        setCurrentPage(page => Math.min(Math.max(page, 1), totalPages));
    }, [totalPages]);

    useEffect(() => {
        setQueuePage(page => Math.min(Math.max(page, 1), queueTotalPages));
    }, [queueTotalPages]);

    const diskStats = stats?.disk || {};
    const transferStats = stats?.transfer || {};
    const usedPercent = Number(diskStats.used_percent) || 0;

    const torrentChartData = useMemo(() => {
        const counts = { downloading: 0, completed: 0, paused: 0, other: 0 };
        torrents.forEach((torrent) => {
            const state = String(torrent.state || '').toLowerCase();
            const progress = Number(torrent.progress) || 0;
            if (progress >= 1) counts.completed += 1;
            else if (state.includes('paused')) counts.paused += 1;
            else if (state.includes('dl') || state.includes('downloading') || state.includes('meta')) counts.downloading += 1;
            else counts.other += 1;
        });
        return {
            labels: ['下载中', '已完成', '已暂停', '其他'],
            datasets: [{
                data: Object.values(counts),
                backgroundColor: ['#f5b841', '#10b981', '#64748b', '#475569'],
                borderColor: '#1b1f28',
                borderWidth: 3,
                hoverOffset: 5,
            }],
        };
    }, [torrents]);

    const progressChartData = useMemo(() => {
        const buckets = [0, 0, 0, 0, 0];
        torrents.forEach((torrent) => {
            const progress = Math.min(Math.max(Number(torrent.progress) || 0, 0), 1);
            const bucket = progress >= 1 ? 4 : Math.min(Math.floor(progress * 4), 3);
            buckets[bucket] += 1;
        });
        return {
            labels: ['0-25%', '25-50%', '50-75%', '75-99%', '100%'],
            datasets: [{
                label: '任务数',
                data: buckets,
                backgroundColor: ['#64748b', '#60a5fa', '#38bdf8', '#f59e0b', '#10b981'],
                borderRadius: 5,
                borderSkipped: false,
                maxBarThickness: 34,
            }],
        };
    }, [torrents]);

    const chartOptions = useMemo(() => ({
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: {
                labels: { color: '#b8bfcc', boxWidth: 12, padding: 14, font: { size: 12 } },
            },
            tooltip: {
                backgroundColor: '#171a21',
                titleColor: '#f4f6fb',
                bodyColor: '#b8bfcc',
                borderColor: '#2c3240',
                borderWidth: 1,
            },
        },
        scales: {
            x: { ticks: { color: '#7d8596' }, grid: { display: false } },
            y: { beginAtZero: true, ticks: { color: '#7d8596', precision: 0 }, grid: { color: 'rgba(255,255,255,0.07)' } },
        },
    }), []);

    const doughnutOptions = useMemo(() => ({
        ...chartOptions,
        cutout: '68%',
        plugins: { ...chartOptions.plugins, legend: { ...chartOptions.plugins.legend, position: 'bottom' } },
        scales: undefined,
    }), [chartOptions]);

    const toggleTorrent = (hash) => {
        setSelectedHashes(prev => {
            const next = new Set(prev);
            next.has(hash) ? next.delete(hash) : next.add(hash);
            return next;
        });
    };

    const toggleSelectVisible = () => {
        setSelectedHashes(prev => {
            const next = new Set(prev);
            if (allVisibleSelected) {
                displayedHashes.forEach(hash => next.delete(hash));
            } else {
                displayedHashes.forEach(hash => next.add(hash));
            }
            return next;
        });
    };

    const clearSelection = () => setSelectedHashes(new Set());

    const controlSelected = async (action) => {
        if (!someSelected || controlling) return;
        setControlling(true);
        try {
            const hashes = [...selectedHashes];
            const res = await movieApi.controlTorrents({ hashes, action });
            if (res.success) {
                showToast(`${action === 'pause' ? '暂停' : '继续'} ${res.count} 个任务成功`, 'success');
                clearSelection();
                await loadData();
            } else {
                showToast(getApiErrorMessage(res, '任务操作失败'), 'error');
            }
        } catch (error) {
            showToast('任务操作请求失败: ' + getApiErrorMessage(error), 'error');
        } finally {
            setControlling(false);
        }
    };

    const controlAll = async (action) => {
        if (controlling) return;
        if (!window.confirm(`确认${action === 'pause' ? '暂停' : '继续'}全部任务？`)) {
            return;
        }

        setControlling(true);
        try {
            const res = await movieApi.controlTorrents({
                hashes: [],
                action,
                all: true,
                confirm: QB_CONTROL_ALL_CONFIRMATION
            });
            if (res.success) {
                showToast(`${action === 'pause' ? '已暂停全部任务' : '已继续全部任务'}`, 'success');
                clearSelection();
                await loadData();
            } else {
                showToast(getApiErrorMessage(res, '任务操作失败'), 'error');
            }
        } catch (error) {
            showToast('任务操作请求失败: ' + getApiErrorMessage(error), 'error');
        } finally {
            setControlling(false);
        }
    };

    const filterJavFiles = async () => {
        if (filteringFiles) return;
        setFilteringFiles(true);
        try {
            const res = await movieApi.filterJavTorrentFiles();
            if (res.success) {
                const data = res.data || {};
                showToast(
                    `已过滤 ${data.filtered || 0} 个任务，小文件阈值 ${data.min_file_size_mb || 400}MB`,
                    'success'
                );
                await loadData();
            } else {
                showToast(getApiErrorMessage(res, '过滤小文件失败'), 'error');
            }
        } catch (error) {
            showToast('过滤小文件请求失败: ' + getApiErrorMessage(error), 'error');
        } finally {
            setFilteringFiles(false);
        }
    };

    const runQueueAction = async (action, item) => {
        if (queueActionId) return;
        const meta = QUEUE_ACTION_META[action];
        if (!meta) return;
        const label = item.title || `#${item.id}`;
        if (!window.confirm(`确认对任务「${label}」执行「${meta.label}」？\n${meta.hint}`)) {
            return;
        }
        setQueueActionId(item.id);
        try {
            const res = await meta.request(item.id);
            if (res.success) {
                showToast(`${meta.label}成功：${label}`, 'success');
                await loadData();
            } else {
                const reason = QUEUE_ERROR_MESSAGES[res.message] || res.message || `${meta.label}失败`;
                showToast(reason, 'error');
            }
        } catch (error) {
            showToast(`${meta.label}请求失败: ` + getApiErrorMessage(error), 'error');
        } finally {
            setQueueActionId('');
        }
    };

    return (
        <div className="downloads-page fade-in">
            <div className="container">
                <div className="page-header">
                    <div className="filter-toggle">
                        <label>
                            <input
                                type="checkbox"
                                checked={filterJav}
                                onChange={e => setFilterJav(e.target.checked)}
                            />
                            只显示 JAV 任务
                        </label>
                    </div>
                </div>

                <DownloadActivityTrend torrents={filteredTorrents} />

                {/* 批量操作 */}
                <div className="system-settings-card card">
                    <div className="global-qb-toolbar">
                        <div className="global-qb-title">
                            <h3>批量操作</h3>
                        </div>
                        <div className="global-qb-actions">
                            <button
                                type="button"
                                className="batch-action-btn"
                                disabled={controlling}
                                onClick={() => controlAll('pause')}
                            >
                                <Pause size={14} /> 全部暂停
                            </button>
                            <button
                                type="button"
                                className="batch-action-btn primary"
                                disabled={controlling}
                                onClick={() => controlAll('resume')}
                            >
                                <Play size={14} /> 全部继续
                            </button>
                            <button
                                type="button"
                                className="batch-action-btn"
                                disabled={filteringFiles}
                                onClick={filterJavFiles}
                                title="JAV 任务只保留大于 400MB 的最大视频文件"
                            >
                                <Package size={14} /> {filteringFiles ? '过滤中...' : '过滤小文件'}
                            </button>
                            <Link to="/settings" className="btn btn-secondary btn-sm" title="修改磁盘阈值与做种限制">
                                配置中心
                            </Link>
                        </div>
                    </div>
                </div>

                {/* 看板区域 */}
                {stats && (
                    <div className="dashboard-grid">
                        {/* 磁盘卡片 */}
                        <div className="stat-card card">
                            <div className="stat-header">
                                <h3>磁盘空间</h3>
                                <span className="stat-value">{diskStats.free_display || 'N/A'} 可用</span>
                            </div>
                            <div className="progress-bar-bg">
                                <div
                                    className={`progress-bar-fill ${getDiskColorClass(usedPercent)}`}
                                    style={{ width: `${Math.min(Math.max(usedPercent, 0), 100)}%` }}
                                ></div>
                            </div>
                            <div className="stat-sub">
                                总共: {diskStats.estimated_total_display || diskStats.total_display || 'N/A'} | 已用: {usedPercent}%
                            </div>
                        </div>

                        {/* 传输卡片 */}
                        <div className="stat-card card">
                            <div className="stat-header">
                                <h3>实时传输</h3>
                                <div className="speed-group">
                                    <span className="speed-dl">↓ {transferStats.dl_speed_display || '0 B/s'}</span>
                                    <span className="speed-ul">↑ {transferStats.up_speed_display || '0 B/s'}</span>
                                </div>
                            </div>
                            <div className="stat-sub">
                                总下载: {transferStats.alltime_dl_display || '0 B'}
                            </div>
                        </div>

                        {/* 队列卡片 */}
                        <div className="stat-card card">
                            <div className="stat-header">
                                <h3>下载队列</h3>
                                <div className="queue-badges">
                                    <span className="badge badge-secondary" title="待提交">
                                        ⏳ {stats.queue_stats?.pending || 0}
                                    </span>
                                    <span className="badge badge-secondary" title="等待外部服务恢复">
                                        <RefreshCw size={14} style={{marginRight: 4}} />{stats.queue_stats?.waiting_external_service || 0}
                                    </span>
                                    <span className="badge badge-success" title="已提交">
                                        <CheckCircle size={14} style={{marginRight: 4}} />{stats.queue_stats?.submitted || 0}
                                    </span>
                                    <span className="badge badge-error" title="失败">
                                        <XCircle size={14} style={{marginRight: 4}} />{stats.queue_stats?.failed || 0}
                                    </span>
                                </div>
                            </div>
                            <div className="stat-sub">
                                本地队列托管中
                            </div>
                        </div>
                    </div>
                )}

                <section className="download-charts" aria-label="下载任务图表">
                    <div className="download-chart-card card">
                        <div className="download-chart-header">
                            <div>
                                <h2>任务状态</h2>
                                <p>当前 qBittorrent 任务分布</p>
                            </div>
                            <Activity size={18} />
                        </div>
                        <div className="download-chart-canvas download-chart-canvas-doughnut">
                            <Doughnut data={torrentChartData} options={doughnutOptions} />
                        </div>
                    </div>
                    <div className="download-chart-card card">
                        <div className="download-chart-header">
                            <div>
                                <h2>进度分布</h2>
                                <p>按完成比例统计任务数量</p>
                            </div>
                            <Download size={18} />
                        </div>
                        <div className="download-chart-canvas">
                            <Bar data={progressChartData} options={chartOptions} />
                        </div>
                    </div>
                </section>

                {/* 本地下载队列（逐条管理） */}
                <div className="local-queue-section card">
                    <div className="local-queue-header">
                        <div>
                            <h2 className="section-title">
                                本地下载队列 ({queueTotal})
                            </h2>
                            <p className="local-queue-sub">
                                托管在本地数据库的待提交/重试/失败任务，可逐条重试、恢复或取消。
                            </p>
                        </div>
                        <div className="local-queue-controls">
                            {queueByStatus && (
                                <div className="local-queue-badges">
                                    {Object.entries(queueByStatus)
                                        .filter(([, count]) => Number(count) > 0)
                                        .map(([status, count]) => {
                                            const meta = QUEUE_STATUS_META[status] || { label: status, badge: 'secondary' };
                                            return (
                                                <span key={status} className={`badge badge-${meta.badge}`} title={meta.label}>
                                                    {meta.label} {count}
                                                </span>
                                            );
                                        })}
                                </div>
                            )}
                            <label className="local-queue-filter">
                                状态
                                <select
                                    value={queueStatusFilter}
                                    onChange={event => setQueueStatusFilter(event.target.value)}
                                >
                                    {QUEUE_STATUS_FILTERS.map(option => (
                                        <option key={option.value || 'all'} value={option.value}>
                                            {option.label}
                                        </option>
                                    ))}
                                </select>
                            </label>
                        </div>
                    </div>

                    {queueItems.length === 0 ? (
                        <div className="local-queue-empty">当前筛选下暂无队列任务</div>
                    ) : (
                        <div className="local-queue-table-wrap">
                            <table className="local-queue-table">
                                <thead>
                                    <tr>
                                        <th>番号</th>
                                        <th>标题</th>
                                        <th>状态</th>
                                        <th>重试</th>
                                        <th>下次重试</th>
                                        <th>保存位置</th>
                                        <th>错误摘要</th>
                                        <th>操作</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {queueItems.map(item => {
                                        const meta = QUEUE_STATUS_META[item.status] || { label: item.status, badge: 'secondary' };
                                        const actions = getQueueActions(item.status, item.retry_count);
                                        const busy = queueActionId === item.id;
                                        return (
                                            <tr key={item.id}>
                                                <td className="queue-cell-code">{item.movie_id || '-'}</td>
                                                <td className="queue-cell-title" title={item.title || ''}>
                                                    {item.title || '未命名'}
                                                </td>
                                                <td>
                                                    <span className={`badge badge-${meta.badge}`}>{meta.label}</span>
                                                </td>
                                                <td className="queue-cell-num">{item.retry_count ?? 0}</td>
                                                <td className="queue-cell-time">{formatQueueTime(item.next_retry_at)}</td>
                                                <td className="queue-cell-path" title={item.save_path_display || ''}>
                                                    {item.save_path_display || '-'}
                                                </td>
                                                <td className="queue-cell-error" title={item.error_message ? redactText(item.error_message) : ''}>
                                                    {item.error_message ? redactText(item.error_message) : '-'}
                                                </td>
                                                <td className="queue-cell-actions">
                                                    {actions.length === 0 ? (
                                                        <span className="queue-no-action">-</span>
                                                    ) : (
                                                        actions.map(action => (
                                                            <button
                                                                key={action}
                                                                type="button"
                                                                className={`queue-action-btn queue-action-${action}`}
                                                                disabled={busy}
                                                                onClick={() => runQueueAction(action, item)}
                                                            >
                                                                {QUEUE_ACTION_META[action].label}
                                                            </button>
                                                        ))
                                                    )}
                                                </td>
                                            </tr>
                                        );
                                    })}
                                </tbody>
                            </table>
                        </div>
                    )}
                    <div className="local-queue-pagination">
                        <span>
                            第 {queuePage} / {queueTotalPages} 页，显示 {queueItems.length === 0 ? 0 : (queuePage - 1) * QUEUE_PAGE_SIZE + 1}
                            -{Math.min(queuePage * QUEUE_PAGE_SIZE, queueTotal)} 项，共 {queueTotal} 项
                        </span>
                        <div className="local-queue-pagination-controls">
                            <button
                                type="button"
                                className="page-btn"
                                disabled={queuePage <= 1}
                                onClick={() => setQueuePage(page => Math.max(1, page - 1))}
                            >
                                上一页
                            </button>
                            <button
                                type="button"
                                className="page-btn"
                                disabled={queuePage >= queueTotalPages}
                                onClick={() => setQueuePage(page => Math.min(queueTotalPages, page + 1))}
                            >
                                下一页
                            </button>
                        </div>
                    </div>
                </div>

                {/* 列表区域 */}
                <div className="torrents-section">
                    <h2 className="section-title">
                        任务列表 ({displayedTorrents.length})
                    </h2>

                    <div className={`torrent-batch-bar card ${someSelected ? 'has-selection' : ''}`}>
                        <label className="torrent-select-all">
                            <input
                                type="checkbox"
                                checked={allVisibleSelected}
                                disabled={displayedHashes.length === 0}
                                onChange={toggleSelectVisible}
                            />
                            <span>
                                {allVisibleSelected
                                    ? `取消全部 ${displayedHashes.length} 项`
                                    : `全选全部 ${displayedHashes.length} 项`}
                            </span>
                        </label>
                        <div className="torrent-batch-actions">
                            <span className="selected-count">
                                已选 {selectedHashes.size} 项
                                {selectedVisibleCount !== selectedHashes.size ? `，当前筛选内 ${selectedVisibleCount} 项` : ''}
                            </span>
                            <button
                                type="button"
                                className="batch-action-btn"
                                disabled={!someSelected || controlling}
                                onClick={() => controlSelected('pause')}
                            >
                                <Pause size={14} /> 暂停
                            </button>
                            <button
                                type="button"
                                className="batch-action-btn primary"
                                disabled={!someSelected || controlling}
                                onClick={() => controlSelected('resume')}
                            >
                                <Play size={14} /> 继续
                            </button>
                            <button
                                type="button"
                                className="batch-action-btn ghost"
                                disabled={!someSelected || controlling}
                                onClick={clearSelection}
                            >
                                清空
                            </button>
                        </div>
                    </div>
                    <div className="torrent-pagination card">
                        <div className="pagination-summary">
                            第 {currentPage} / {totalPages} 页，
                            显示 {displayedTorrents.length === 0 ? 0 : pageStart + 1}
                            -{Math.min(pageStart + pageSize, displayedTorrents.length)}
                            项，共 {displayedTorrents.length} 项
                        </div>
                        <div className="pagination-controls">
                            <label>
                                排序
                                <select
                                    value={sortMode}
                                    onChange={event => setSortMode(event.target.value)}
                                >
                                    <option value="progress_desc">进度高到低</option>
                                    <option value="progress_asc">进度低到高</option>
                                    <option value="added_desc">添加时间新到旧</option>
                                    <option value="added_asc">添加时间旧到新</option>
                                </select>
                            </label>
                            <label>
                                每页
                                <select
                                    value={pageSize}
                                    onChange={event => setPageSize(Number(event.target.value))}
                                >
                                    {[10, 20, 50, 100].map(size => (
                                        <option key={size} value={size}>{size}</option>
                                    ))}
                                </select>
                            </label>
                            <button
                                type="button"
                                className="page-btn"
                                disabled={currentPage <= 1}
                                onClick={() => setCurrentPage(page => Math.max(1, page - 1))}
                            >
                                上一页
                            </button>
                            <button
                                type="button"
                                className="page-btn"
                                disabled={currentPage >= totalPages}
                                onClick={() => setCurrentPage(page => Math.min(totalPages, page + 1))}
                            >
                                下一页
                            </button>
                        </div>
                    </div>

                    {loading && !stats ? (
                        <div className="torrents-loading">加载中...</div>
                    ) : loadError ? (
                        <div className="empty-state card">
                            <p>{loadError}</p>
                        </div>
                    ) : displayedTorrents.length === 0 ? (
                        <div className="empty-state card">
                            <p>暂无下载任务</p>
                        </div>
                    ) : (
                        <div className="torrents-list">
                            {pagedTorrents.map((torrent, index) => (
                                <TorrentCard
                                    key={torrent.hash || `${torrent.name}-${index}`}
                                    torrent={torrent}
                                    selected={selectedHashes.has(torrent.hash)}
                                    onToggle={() => toggleTorrent(torrent.hash)}
                                />
                            ))}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}

function DownloadActivityTrend({ torrents }) {
    const trend = useMemo(() => {
        const bucketCount = 15;
        const dayMs = 24 * 60 * 60 * 1000;
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        const start = today.getTime() - (bucketCount - 1) * dayMs;
        const end = today.getTime() + dayMs;
        const buckets = Array.from({ length: bucketCount }, (_, index) => ({
            count: 0,
            start: start + index * dayMs
        }));

        torrents.forEach(torrent => {
            const seconds = Number(torrent.added_on || torrent.addedOn || torrent.added_at || 0);
            if (!seconds) return;
            const time = seconds * 1000;
            if (time < start || time >= end) return;
            const date = new Date(time);
            date.setHours(0, 0, 0, 0);
            const index = Math.floor((date.getTime() - start) / dayMs);
            if (index < 0 || index >= bucketCount) return;
            buckets[index].count += 1;
        });

        const activeBuckets = buckets.filter(bucket => bucket.count > 0);
        const max = Math.max(1, ...activeBuckets.map(bucket => bucket.count));
        return activeBuckets.map(bucket => ({
            ...bucket,
            height: Math.max(6, Math.round((bucket.count / max) * 54))
        }));
    }, [torrents]);

    const totalRecent = trend.reduce((sum, bucket) => sum + bucket.count, 0);
    const activeCount = torrents.filter(torrent => {
        const state = String(torrent.state || '').toLowerCase();
        return state.includes('dl') || state.includes('download') || state.includes('meta') || Number(torrent.dlspeed || 0) > 0;
    }).length;

    const formatDay = (timestamp) => {
        const date = new Date(timestamp);
        return `${date.getMonth() + 1}/${date.getDate()}`;
    };

    return (
        <div className="download-activity-card card">
            <div className="activity-header">
                <div>
                    <h3><Activity size={16} /> 最近下载新增趋势</h3>
                    <p>最近 15 天每天新增任务数量</p>
                </div>
                <div className="activity-metrics">
                    <span><Clock size={14} />15 天新增 {totalRecent}</span>
                    <span>活跃 {activeCount}</span>
                </div>
            </div>
            <div className="activity-chart" aria-label="最近下载新增趋势">
                {trend.length > 0 ? trend.map(bucket => (
                    <div className="activity-bar-wrap" key={bucket.start} title={`${formatDay(bucket.start)} 新增 ${bucket.count} 个任务`}>
                        <strong className="activity-count">{bucket.count}</strong>
                        <div className="activity-bar has-data" style={{ height: `${bucket.height}px` }} />
                        <span>{formatDay(bucket.start)}</span>
                    </div>
                )) : (
                    <div className="activity-empty">最近 15 天暂无新增下载任务</div>
                )}
            </div>
        </div>
    );
}

function TorrentCard({ torrent, selected, onToggle }) {
    const getStateText = (state) => {
        const stateTextMap = {
            'downloading': <><Download size={14} style={{marginRight: 4}} />下载中</>,
            'uploading': <><Upload size={14} style={{marginRight: 4}} />做种中</>,
            'stalledUP': <><Upload size={14} style={{marginRight: 4}} />等待上传</>,
            'forcedUP': <><Upload size={14} style={{marginRight: 4}} />强制做种</>,
            'pausedDL': <><Pause size={14} style={{marginRight: 4}} />已暂停</>,
            'pausedUP': <><Pause size={14} style={{marginRight: 4}} />做种暂停</>,
            'queuedDL': '⏳ 排队中',
            'queuedUP': '⏳ 做种排队',
            'stalledDL': <><AlertTriangle size={14} style={{marginRight: 4}} />停滞</>,
            'checkingDL': <><Search size={14} style={{marginRight: 4}} />校验中</>,
            'checkingUP': <><Search size={14} style={{marginRight: 4}} />做种校验</>,
            'metaDL': <><Search size={14} style={{marginRight: 4}} />获取元数据</>,
            'allocating': <><Package size={14} style={{marginRight: 4}} />分配空间</>,
            'moving': <><Package size={14} style={{marginRight: 4}} />移动中</>,
            'missingFiles': <><AlertTriangle size={14} style={{marginRight: 4}} />文件缺失</>,
            'completed': <><CheckCircle size={14} style={{marginRight: 4}} />已完成</>,
            'error': <><XCircle size={14} style={{marginRight: 4}} />错误</>,
        };
        return stateTextMap[state] || state;
    };

    const getStateColor = (state) => {
        if (!state) return 'secondary';
        if (state.includes('error') || state === 'missingFiles') return 'error';
        if (state.includes('paused') || state.includes('stalled')) return 'warning';
        if (state.includes('DL') || state.includes('download')) return 'primary';
        if (state.includes('UP') || state.includes('upload') || state === 'completed') return 'success';
        return 'secondary';
    };

    const formatBytes = (bytes) => {
        const value = Number(bytes);
        if (!Number.isFinite(value) || value <= 0) return '0 B';
        const k = 1024;
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.min(Math.floor(Math.log(value) / Math.log(k)), units.length - 1);
        return (value / Math.pow(k, i)).toFixed(2) + ' ' + units[i];
    };

    const formatSpeed = (bytes) => formatBytes(bytes) + '/s';
    const progressValue = Math.min(Math.max(Number(torrent.progress) || 0, 0), 1);
    const progress = (progressValue * 100).toFixed(1);
    const addedSeconds = Number(torrent.added_on || torrent.addedOn || torrent.added_at || 0);
    const addedText = addedSeconds ? new Date(addedSeconds * 1000).toLocaleString() : '未知';
    const torrentName = torrent.name || '未命名任务';

    return (
        <div className={`torrent-card card ${selected ? 'selected' : ''}`}>
            <div className="torrent-header">
                <label className="torrent-select-box" onClick={(event) => event.stopPropagation()}>
                    <input
                        type="checkbox"
                        checked={selected}
                        onChange={onToggle}
                        aria-label={`选择 ${torrentName}`}
                    />
                </label>
                <h3 className="torrent-name" title={torrentName}>{torrentName}</h3>
                <span className={`badge badge-${getStateColor(torrent.state)}`}>
                    {getStateText(torrent.state)}
                </span>
            </div>

            <div className="progress-container">
                <div className="progress-bar-bg">
                    <div className="progress-bar-fill" style={{ width: `${progress}%` }}></div>
                </div>
                <span className="progress-text">{progress}%</span>
            </div>

            <div className="torrent-meta">
                <span><Package size={14} style={{marginRight: 4}} />{formatBytes(torrent.size)}</span>
                <span className="meta-dl">↓ {formatSpeed(torrent.dlspeed)}</span>
                <span className="meta-ul">↑ {formatSpeed(torrent.upspeed)}</span>
                <span><Clock size={14} style={{marginRight: 4}} />{addedText}</span>
                <span title="Tags"><Tag size={14} style={{marginRight: 4}} />{torrent.tags || '-'}</span>
            </div>
        </div>
    );
}

export default DownloadsPage;
