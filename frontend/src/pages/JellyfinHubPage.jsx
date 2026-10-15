import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { getApiErrorMessage, getProxyImageUrl, logSafeError, movieApi } from '../api';
import { useToast } from '../components/Toast';
import {
    CheckCircle,
    Eye,
    Film,
    Heart,
    Library,
    RefreshCw,
    Server,
    Trash2,
    Users,
    XCircle,
    Zap,
} from 'lucide-react';
import './JellyfinHubPage.css';

const BEHAVIOR_BUCKET_META = {
    watched_not_favorite: {
        label: '已看未收藏',
        desc: '清理候选',
    },
    watched_and_favorite: {
        label: '已看且收藏',
        desc: '强偏好样本',
    },
    favorite_unwatched: {
        label: '收藏未看',
        desc: '待观看偏好',
    },
    strong_preference: {
        label: '高偏好',
        desc: '收藏且多次播放',
    },
};

const BEHAVIOR_BUCKET_KEYS = Object.keys(BEHAVIOR_BUCKET_META);

function JellyfinHubPage() {
    const { showToast } = useToast();
    const [stats, setStats] = useState(null);
    const [behavior, setBehavior] = useState(null);
    const [automationCandidates, setAutomationCandidates] = useState([]);
    const [loading, setLoading] = useState(true);
    const [behaviorLoading, setBehaviorLoading] = useState(false);
    const [automationLoading, setAutomationLoading] = useState(false);
    const [candidateActionId, setCandidateActionId] = useState(null);
    const [approveAllBusy, setApproveAllBusy] = useState(false);
    const [syncing, setSyncing] = useState(false);
    const [fetchError, setFetchError] = useState(false);
    const [selectedBucket, setSelectedBucket] = useState('watched_not_favorite');
    const initRef = useRef(false);
    const statsRequestSeqRef = useRef(0);
    const behaviorRequestSeqRef = useRef(0);
    const automationRequestSeqRef = useRef(0);

    const fetchStats = useCallback(async () => {
        const requestSeq = ++statsRequestSeqRef.current;
        const isStaleRequest = () => requestSeq !== statsRequestSeqRef.current;
        setLoading(true);
        setFetchError(false);
        try {
            const res = await movieApi.getJellyfinStats();
            if (isStaleRequest()) return;
            if (res.success) {
                setStats(res.data);
            } else {
                setStats(null);
                setFetchError(true);
                showToast(getApiErrorMessage(res, 'Jellyfin 状态加载失败'), 'error');
            }
        } catch (e) {
            if (isStaleRequest()) return;
            logSafeError('Jellyfin stats failed:', e);
            setStats(null);
            setFetchError(true);
            showToast('Jellyfin 状态加载失败: ' + getApiErrorMessage(e), 'error');
        } finally {
            if (!isStaleRequest()) setLoading(false);
        }
    }, [showToast]);

    const fetchBehavior = useCallback(async () => {
        const requestSeq = ++behaviorRequestSeqRef.current;
        const isStaleRequest = () => requestSeq !== behaviorRequestSeqRef.current;
        setBehaviorLoading(true);
        try {
            const res = await movieApi.getJellyfinBehaviorInsights({
                scan_limit: 1000,
                sample_limit: 5,
                strong_preference_min_plays: 2,
            });
            if (isStaleRequest()) return;
            if (res.success) {
                setBehavior(res.data);
            } else {
                setBehavior(null);
                showToast(getApiErrorMessage(res, '行为洞察加载失败'), 'error');
            }
        } catch (e) {
            if (isStaleRequest()) return;
            logSafeError('Jellyfin behavior failed:', e);
            setBehavior(null);
            showToast('行为洞察加载失败: ' + getApiErrorMessage(e), 'error');
        } finally {
            if (!isStaleRequest()) setBehaviorLoading(false);
        }
    }, [showToast]);

    const fetchAutomationCandidates = useCallback(async () => {
        const requestSeq = ++automationRequestSeqRef.current;
        const isStaleRequest = () => requestSeq !== automationRequestSeqRef.current;
        setAutomationLoading(true);
        try {
            const res = await movieApi.getAutomationCandidates({
                status: 'candidate',
                candidate_type: 'favorite_actor',
                limit: 20,
            });
            if (isStaleRequest()) return;
            if (res.success) {
                setAutomationCandidates(Array.isArray(res.data) ? res.data : []);
            } else {
                setAutomationCandidates([]);
                showToast(getApiErrorMessage(res, '订阅候选加载失败'), 'error');
            }
        } catch (e) {
            if (isStaleRequest()) return;
            logSafeError('Automation candidates failed:', e);
            setAutomationCandidates([]);
            showToast('订阅候选加载失败: ' + getApiErrorMessage(e), 'error');
        } finally {
            if (!isStaleRequest()) setAutomationLoading(false);
        }
    }, [showToast]);

    useEffect(() => {
        if (initRef.current) return;
        initRef.current = true;
        fetchStats();
        fetchBehavior();
        fetchAutomationCandidates();
        return () => {
            initRef.current = false;
        };
    }, [fetchAutomationCandidates, fetchBehavior, fetchStats]);

    const handleSync = async () => {
        setSyncing(true);
        try {
            const res = await movieApi.syncJellyfinStatus();
            if (res.success) {
                const report = res.report || res.data || {};
                const syncedSubscriptions = Number(report.favorite_people_subscribed || 0)
                    + Number(report.auto_subscribed || 0);
                if (syncedSubscriptions > 0) {
                    await confirmSubscriptionsVisible([]);
                    showToast(`已同步 ${syncedSubscriptions} 个 Jellyfin 订阅`, 'success');
                } else {
                    showToast(res.message || 'Jellyfin 同步完成', 'success');
                }
                fetchStats();
                fetchAutomationCandidates();
            } else {
                showToast(getApiErrorMessage(res, '同步失败'), 'error');
            }
        } catch (e) {
            showToast('同步失败: ' + getApiErrorMessage(e), 'error');
        } finally {
            setSyncing(false);
        }
    };

    const confirmSubscriptionsVisible = async (expectedUrls = []) => {
        try {
            const res = await movieApi.getSubscriptions();
            if (!res?.success) return false;
            fetchStats();
            const groups = expectedUrls
                .map(item => Array.isArray(item) ? item.filter(Boolean) : [item].filter(Boolean))
                .filter(group => group.length > 0);
            if (groups.length === 0) return true;
            const visibleUrls = new Set((res.data || []).map(item => item.url).filter(Boolean));
            return groups.every(group => group.some(url => visibleUrls.has(url)));
        } catch (e) {
            logSafeError('Subscription visibility refresh failed:', e);
            return false;
        }
    };

    const handleCandidateDecision = async (candidate, decision) => {
        const isApprove = decision === 'approve';
        if (isApprove) {
            setCandidateActionId(candidate.id);
            try {
                const res = await movieApi.approveAutomationCandidate(candidate.id, {
                    actor: 'user',
                    reason: '用户批准订阅候选',
                });
                if (res.success) {
                    const application = res.application || {};
                    setAutomationCandidates(prev => prev.filter(item => item.id !== candidate.id));
                    const subscriptionsVisible = application.subscription_visible
                        || await confirmSubscriptionsVisible([application.subscription_urls || application.subscription_url]);
                    if (subscriptionsVisible) {
                        showToast('已加入订阅', 'success');
                    } else if (application.subscribed) {
                        showToast('订阅已保存，但订阅页同步确认失败', 'warning');
                    } else {
                        showToast('订阅已处理，请稍后刷新订阅页确认', 'warning');
                    }
                    fetchAutomationCandidates();
                    fetchStats();
                } else {
                    showToast(getApiErrorMessage(res, '候选处理失败'), 'error');
                }
            } catch (e) {
                showToast('候选处理失败: ' + getApiErrorMessage(e), 'error');
            } finally {
                setCandidateActionId(null);
            }
            return;
        }
        const reason = window.prompt(isApprove ? '请输入批准原因' : '请输入拒绝原因');
        if (!reason || reason.trim().length < 2) {
            showToast('需要填写原因', 'warning');
            return;
        }

        setCandidateActionId(candidate.id);
        try {
            const payload = { actor: 'user', reason: reason.trim() };
            const res = isApprove
                ? await movieApi.approveAutomationCandidate(candidate.id, payload)
                : await movieApi.rejectAutomationCandidate(candidate.id, payload);
            if (res.success) {
                showToast(isApprove ? '订阅候选已批准' : '订阅候选已拒绝', 'success');
                fetchAutomationCandidates();
                fetchStats();
            } else {
                showToast(getApiErrorMessage(res, '候选处理失败'), 'error');
            }
        } catch (e) {
            showToast('候选处理失败: ' + getApiErrorMessage(e), 'error');
        } finally {
            setCandidateActionId(null);
        }
    };

    const handleApproveAllCandidates = async () => {
        if (automationCandidates.length === 0) {
            showToast('暂无可订阅的候选', 'info');
            return;
        }

        setApproveAllBusy(true);
        try {
            const res = await movieApi.approveAllAutomationCandidates({
                actor: 'user',
                reason: '用户一键批准全部订阅候选',
            });
            if (res.success) {
                const report = res.report || {};
                const subscribedCount = report.subscribed || 0;
                const failedCount = report.failed || 0;
                const subscriptionsVisible = subscribedCount > 0
                    ? await confirmSubscriptionsVisible(report.subscription_url_groups || report.subscription_urls || [])
                    : false;
                if (failedCount > 0) {
                    showToast(`已订阅 ${subscribedCount} 位，${failedCount} 位失败`, subscribedCount > 0 ? 'warning' : 'error');
                } else if (subscribedCount > 0 && !subscriptionsVisible) {
                    showToast(`已订阅 ${subscribedCount} 位，但订阅页同步确认失败`, 'warning');
                } else {
                    showToast(res.message || `已订阅 ${subscribedCount} 位`, 'success');
                }
                fetchAutomationCandidates();
                fetchStats();
            } else {
                showToast(getApiErrorMessage(res, '全部订阅失败'), 'error');
            }
        } catch (e) {
            showToast('全部订阅失败: ' + getApiErrorMessage(e), 'error');
        } finally {
            setApproveAllBusy(false);
        }
    };

    const handleRefreshLibrary = async () => {
        try {
            const res = await movieApi.triggerJellyfinRefresh();
            showToast(res.success ? (res.message || '刷新已触发') : getApiErrorMessage(res, '刷新失败'), res.success ? 'success' : 'error');
        } catch (e) {
            showToast('刷新失败: ' + getApiErrorMessage(e), 'error');
        }
    };

    const formatTime = (ts) => {
        if (!ts) return '从未';
        const d = new Date(ts * 1000);
        const now = new Date();
        const diff = Math.floor((now - d) / 1000);
        if (diff < 60) return '刚刚';
        if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
        if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
        return d.toLocaleDateString('zh-CN');
    };

    const renderBucketIcon = (bucket) => {
        if (bucket === 'watched_not_favorite') return <Trash2 size={16} />;
        if (bucket === 'watched_and_favorite') return <Heart size={16} />;
        if (bucket === 'favorite_unwatched') return <Eye size={16} />;
        return <CheckCircle size={16} />;
    };

    const selectedBucketData = behavior?.buckets?.[selectedBucket];

    if (loading) {
        return (
            <div className="jellyfin-hub-page">
                <div className="container" style={{ textAlign: 'center', padding: '100px 0' }}>
                    <div className="spinner" />
                    <p style={{ marginTop: 20, color: 'var(--text-secondary)' }}>加载 Jellyfin 数据...</p>
                </div>
            </div>
        );
    }

    if (fetchError) {
        return (
            <div className="jellyfin-hub-page">
                <div className="container" style={{ textAlign: 'center', padding: '100px 0' }}>
                    <Server size={48} style={{ color: 'var(--text-muted)' }} />
                    <p style={{ marginTop: 16, color: 'var(--text-muted)' }}>无法获取 Jellyfin 数据</p>
                    <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 16 }}>
                        请确认 Jellyfin 服务已启动，且 API Key 配置正确
                    </p>
                    <button className="btn btn-primary" onClick={fetchStats}>
                        <RefreshCw size={16} /> 重试
                    </button>
                </div>
            </div>
        );
    }

    return (
        <div className="jellyfin-hub-page fade-in">
            <div className="container">
                <div className="hub-header">
                    <div>
                        <h1 className="hub-title">Jellyfin</h1>
                    </div>
                    <div className="hub-actions">
                        <button className="btn btn-secondary" onClick={fetchStats} disabled={loading}>
                            <RefreshCw size={16} /> 刷新
                        </button>
                    </div>
                </div>

                <div className="hub-connection-card">
                    <div className="conn-status">
                        <span className={`conn-dot ${stats?.connected ? 'online' : 'offline'}`} />
                        <span className="conn-label">{stats?.connected ? '已连接' : '未连接'}</span>
                    </div>
                    {stats?.connected && stats.server_info && (
                        <>
                            <div className="conn-divider" />
                            <div className="conn-info">
                                <span>{stats.server_info.server_name}</span>
                                <span className="conn-meta">v{stats.server_info.version}</span>
                            </div>
                        </>
                    )}
                    {!stats?.connected && (
                        <span className="conn-hint">请在设置中配置 Jellyfin 服务地址和 API Key</span>
                    )}
                    <button className="hub-connection-refresh btn btn-secondary" onClick={fetchStats} disabled={loading}>
                        <RefreshCw size={16} /> 刷新
                    </button>
                </div>

                <div className="hub-stats-grid">
                    <div className="hub-stat-card">
                        <div className="hub-stat-icon film-icon"><Film size={20} /></div>
                        <div className="hub-stat-body">
                            <span className="hub-stat-value">{(stats?.total_items || 0).toLocaleString()}</span>
                            <span className="hub-stat-label">媒体库影片</span>
                        </div>
                    </div>
                    <div className="hub-stat-card">
                        <div className="hub-stat-icon heart-icon"><Heart size={20} /></div>
                        <div className="hub-stat-body">
                            <span className="hub-stat-value">{(stats?.favorites_count || 0).toLocaleString()}</span>
                            <span className="hub-stat-label">Jellyfin 收藏</span>
                        </div>
                    </div>
                    <div className="hub-stat-card">
                        <div className="hub-stat-icon eye-icon"><Eye size={20} /></div>
                        <div className="hub-stat-body">
                            <span className="hub-stat-value">{(stats?.watched_count || 0).toLocaleString()}</span>
                            <span className="hub-stat-label">Jellyfin 已看</span>
                        </div>
                    </div>
                    <div className="hub-stat-card">
                        <div className="hub-stat-icon user-icon"><Users size={20} /></div>
                        <div className="hub-stat-body">
                            <span className="hub-stat-value">{stats?.total_subscribed_actresses ?? stats?.total_followed_actresses ?? 0}</span>
                            <span className="hub-stat-label">
                                已订阅演员
                                <span className="stat-sub">（{stats?.actresses_with_metadata || 0} 有元数据）</span>
                            </span>
                        </div>
                    </div>
                </div>

                <div className="hub-card behavior-card">
                    <div className="behavior-header">
                        <h3 className="hub-card-title behavior-title"><Eye size={18} /> 行为洞察</h3>
                        <button className="btn btn-secondary" onClick={fetchBehavior} disabled={behaviorLoading || !stats?.connected}>
                            <RefreshCw size={16} /> 刷新
                        </button>
                    </div>
                    {behaviorLoading ? (
                        <div className="behavior-loading">
                            <div className="spinner-sm" />
                            <span>正在扫描 Jellyfin 行为...</span>
                        </div>
                    ) : behavior ? (
                        <>
                            <div className="behavior-scan-meta">
                                <span>已扫描 {(behavior.scanned || 0).toLocaleString()} / {(behavior.total_available || 0).toLocaleString()} 部</span>
                                {behavior.is_truncated && <span className="behavior-warning">结果已截断</span>}
                            </div>
                            <div className="behavior-buckets">
                                {BEHAVIOR_BUCKET_KEYS.map((bucket) => {
                                    const meta = BEHAVIOR_BUCKET_META[bucket];
                                    const count = behavior.buckets?.[bucket]?.count || 0;
                                    return (
                                        <button
                                            key={bucket}
                                            className={`behavior-bucket ${selectedBucket === bucket ? 'active' : ''}`}
                                            onClick={() => setSelectedBucket(bucket)}
                                        >
                                            <span className="behavior-bucket-icon">{renderBucketIcon(bucket)}</span>
                                            <span className="behavior-bucket-body">
                                                <span className="behavior-bucket-label">{meta.label}</span>
                                                <span className="behavior-bucket-desc">{meta.desc}</span>
                                            </span>
                                            <span className="behavior-bucket-count">{count.toLocaleString()}</span>
                                        </button>
                                    );
                                })}
                            </div>
                            <div className="behavior-sample-list">
                                {(selectedBucketData?.sample || []).length > 0 ? (
                                    selectedBucketData.sample.map((movie) => (
                                        <div className="behavior-sample-item" key={`${selectedBucket}-${movie.jellyfin_id}`}>
                                            <div className={`behavior-sample-cover ${movie.cover ? '' : 'is-empty'}`}>
                                                {movie.cover ? (
                                                    <img
                                                        src={getProxyImageUrl(movie.cover, { w: 240 })}
                                                        alt={movie.title || movie.movie_id || 'Jellyfin movie cover'}
                                                        loading="lazy"
                                                        decoding="async"
                                                        onError={(event) => {
                                                            event.currentTarget.style.display = 'none';
                                                            event.currentTarget.parentElement?.classList.add('is-empty');
                                                        }}
                                                    />
                                                ) : (
                                                    <span>无封面</span>
                                                )}
                                            </div>
                                            <div className="behavior-sample-title" title={movie.title}>
                                                {movie.title || movie.movie_id}
                                            </div>
                                            <div className="behavior-sample-meta">
                                                <span>{movie.movie_id}</span>
                                                <span>播放 {movie.play_count || 0} 次</span>
                                                {movie.cleanup_priority && <span className="behavior-warning">清理候选</span>}
                                            </div>
                                        </div>
                                    ))
                                ) : (
                                    <div className="hub-empty">当前分组暂无样本</div>
                                )}
                            </div>
                        </>
                    ) : (
                        <div className="hub-empty">暂无行为洞察数据</div>
                    )}
                </div>

                <div className="hub-card automation-card">
                    <div className="automation-header">
                        <h3 className="hub-card-title automation-title"><Users size={18} /> 订阅候选</h3>
                        <div className="automation-toolbar">
                            <Link to="/settings" className="btn btn-secondary btn-sm" title="前往配置中心修改自动化策略与同步设置">
                                配置中心
                            </Link>
                            <button
                                className="btn btn-primary"
                                onClick={handleApproveAllCandidates}
                                disabled={approveAllBusy || automationLoading || automationCandidates.length === 0}
                                title="批准当前所有候选，并加入订阅"
                            >
                                {approveAllBusy ? <div className="spinner-sm" /> : <CheckCircle size={16} />}
                                全部订阅
                            </button>
                            <button className="btn btn-secondary" onClick={fetchAutomationCandidates} disabled={automationLoading}>
                                <RefreshCw size={16} /> 刷新
                            </button>
                        </div>
                    </div>
                    {automationLoading ? (
                        <div className="automation-loading">
                            <div className="spinner-sm" />
                            <span>正在加载订阅候选...</span>
                        </div>
                    ) : automationCandidates.length > 0 ? (
                        <div className="automation-list">
                            {automationCandidates.map((candidate) => {
                                const payload = candidate.payload || {};
                                const profile = candidate.subject_profile || {};
                                const avatar = profile.avatar;
                                const busy = candidateActionId === candidate.id;
                                return (
                                    <div className="automation-item" key={candidate.id}>
                                        <div className="automation-avatar">
                                            {avatar ? (
                                                <img
                                                    src={getProxyImageUrl(avatar, { w: 160 })}
                                                    alt={candidate.subject_name || profile.name || candidate.subject_id}
                                                    loading="lazy"
                                                    decoding="async"
                                                />
                                            ) : (
                                                <Users size={18} />
                                            )}
                                        </div>
                                        <div className="automation-main">
                                            <div className="automation-name" title={candidate.subject_name}>
                                                {candidate.subject_name || candidate.subject_id}
                                            </div>
                                            <div className="automation-meta">
                                                <span>{candidate.source_title || payload.movie_title || candidate.source_movie_id || '未知来源'}</span>
                                                <span>订阅演员</span>
                                                {payload.already_followed && <span>已订阅</span>}
                                                {payload.subscription_exists && <span>已有订阅</span>}
                                            </div>
                                            <div className="automation-reason">
                                                {candidate.reason || '收藏影片反推订阅'}
                                            </div>
                                        </div>
                                        <div className="automation-actions">
                                            <button
                                                className="btn btn-primary"
                                                onClick={() => handleCandidateDecision(candidate, 'approve')}
                                                disabled={busy}
                                                title="批准该候选并加入订阅"
                                            >
                                                <CheckCircle size={14} /> 订阅
                                            </button>
                                            <button
                                                className="btn btn-secondary"
                                                onClick={() => handleCandidateDecision(candidate, 'reject')}
                                                disabled={busy}
                                            >
                                                <XCircle size={14} /> 拒绝
                                            </button>
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                    ) : (
                        <div className="hub-empty">暂无待审核订阅</div>
                    )}
                </div>

                <div className="hub-main-grid">
                    <div className="hub-left">
                        <div className="hub-card">
                            <h3 className="hub-card-title"><Library size={18} /> 媒体库概览</h3>
                            {stats?.libraries && stats.libraries.length > 0 ? (
                                <div className="lib-list">
                                    {stats.libraries.map((lib) => (
                                        <div key={lib.id} className="lib-item">
                                            <div className="lib-item-left">
                                                <span className={`lib-type-badge lib-type-${lib.type}`}>
                                                    {lib.type === 'movies' ? '电影' : lib.type === 'tvshows' ? '剧集' : '其他'}
                                                </span>
                                                <span className="lib-name">{lib.name}</span>
                                            </div>
                                            <span className="lib-count">{lib.item_count.toLocaleString()} 部</span>
                                        </div>
                                    ))}
                                </div>
                            ) : (
                                <div className="hub-empty">暂无媒体库数据</div>
                            )}
                        </div>

                        <div className="hub-card">
                            <h3 className="hub-card-title"><RefreshCw size={18} /> 状态同步</h3>
                            <div className="sync-info">
                                <div className="sync-row">
                                    <span className="sync-label">上次同步</span>
                                    <span className="sync-value">{formatTime(stats?.last_sync_time)}</span>
                                </div>
                                <div className="sync-row">
                                    <span className="sync-label">自动清理</span>
                                    <span className={`sync-value ${stats?.cleanup?.enabled ? 'text-success' : 'text-muted'}`}>
                                        {stats?.cleanup?.enabled ? '已开启' : '未开启'}
                                    </span>
                                </div>
                                {stats?.cleanup?.last_run && (
                                    <div className="sync-row">
                                        <span className="sync-label">上次清理</span>
                                        <span className="sync-value">{new Date(stats.cleanup.last_run).toLocaleString('zh-CN')}</span>
                                    </div>
                                )}
                            </div>
                            <button
                                className="btn btn-primary hub-btn-full"
                                onClick={handleSync}
                                disabled={syncing || !stats?.connected}
                            >
                                {syncing ? (
                                    <><div className="spinner-sm" /> 同步中...</>
                                ) : (
                                    <><RefreshCw size={16} /> 立即同步收藏与观看状态</>
                                )}
                            </button>
                        </div>
                    </div>

                    <div className="hub-right">
                        <div className="hub-card">
                            <h3 className="hub-card-title"><Zap size={18} /> 快捷操作</h3>
                            <div className="quick-actions">
                                <button
                                    className="action-item"
                                    onClick={handleRefreshLibrary}
                                    disabled={!stats?.connected}
                                >
                                    <div className="action-icon refresh-icon"><RefreshCw size={20} /></div>
                                    <div className="action-body">
                                        <span className="action-title">刷新媒体库</span>
                                        <span className="action-desc">通知 Jellyfin 重新扫描</span>
                                    </div>
                                </button>

                                <button
                                    className="action-item"
                                    onClick={async () => {
                                        try {
                                            const res = await movieApi.syncAllActressesJellyfin();
                                            if (res.success) {
                                                showToast(res.message || '后台任务已启动', 'success');
                                            } else {
                                                showToast(getApiErrorMessage(res, '启动失败'), 'error');
                                            }
                                        } catch (e) {
                                            showToast('启动失败: ' + getApiErrorMessage(e), 'error');
                                        }
                                    }}
                                    disabled={!stats?.connected}
                                >
                                    <div className="action-icon user-icon"><Users size={20} /></div>
                                    <div className="action-body">
                                        <span className="action-title">补全演员元数据</span>
                                        <span className="action-desc">同步头像和简介</span>
                                    </div>
                                </button>
                            </div>
                        </div>

                        <div className="hub-card">
                            <h3 className="hub-card-title"><CheckCircle size={18} /> 联动状态</h3>
                            <div className="feature-list">
                                <div className="feature-item">
                                    <CheckCircle size={14} className="feature-check" />
                                    <div>
                                        <strong>收藏反馈</strong>
                                        <p>收藏影片会进入偏好候选审核。</p>
                                    </div>
                                </div>
                                <div className="feature-item">
                                    <CheckCircle size={14} className="feature-check" />
                                    <div>
                                        <strong>观看反馈</strong>
                                        <p>已看未收藏影片会进入清理候选。</p>
                                    </div>
                                </div>
                                <div className="feature-item">
                                    <CheckCircle size={14} className="feature-check" />
                                    <div>
                                        <strong>自动下载</strong>
                                        <p>所有自动入队都经过统一许可检查。</p>
                                    </div>
                                </div>
                                <div className="feature-item">
                                    <CheckCircle size={14} className="feature-check" />
                                    <div>
                                        <strong>删除保护</strong>
                                        <p>清理任务默认只生成候选，不会直接删除。</p>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}

export default JellyfinHubPage;

