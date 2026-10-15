import React, { useEffect, useState } from 'react';
import { RefreshCw, Trash2, RefreshCcw, BarChart3 } from 'lucide-react';
import { getBiOverview, saveBiSettings, logSafeError } from '../../api';

const CACHE_CATEGORY_LABELS = {
    remote_images: '远程图片',
    jellyfin_images: 'Jellyfin 图片',
    thumbnails: '缩略图',
    failure_markers: '失败标记',
    discovery_feeds: '发现列表',
    movie_details: '影片详情',
    navigation: '导航分类',
    translations: '翻译结果',
};

function formatCacheBytes(bytes) {
    if (bytes == null || isNaN(bytes)) return '-';
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

export default function SettingsCacheTab({
    sysConfig,
    handleSysChange,
    cacheStats,
    fetchCacheStats,
    cacheLoading,
    handleCacheCleanup,
    cacheCleaning,
    cacheInvalidationCategory,
    setCacheInvalidationCategory,
    handleCacheInvalidation,
    cacheInvalidating,
    handleCacheWarmup,
    cacheWarming
}) {
    const structuredMetrics = cacheStats?.structured?.metrics || {};

    // 数据看板的汇总时间属于运维信息，放在设置里；看板页面上不重复展示
    const [biMeta, setBiMeta] = useState(null);
    const [biRecalculating, setBiRecalculating] = useState(false);
    const [biRefreshMode, setBiRefreshMode] = useState('daily');
    const [biModeSaving, setBiModeSaving] = useState(false);

    useEffect(() => {
        let cancelled = false;
        getBiOverview(false)
            .then((res) => {
                if (cancelled) return;
                setBiMeta({
                    refreshed_at: res?.data?.meta?.refreshed_at || null,
                    slots: res?.data?.meta?.slots || [],
                });
                // 频率以后端返回的为准，避免界面显示与实际调度不一致
                if (res?.data?.refresh_mode) setBiRefreshMode(res.data.refresh_mode);
            })
            .catch(() => { /* 快照元信息拿不到不影响设置页其他内容 */ });
        return () => { cancelled = true; };
    }, []);

    const applyBiMeta = (payload) => {
        setBiMeta({
            refreshed_at: payload?.meta?.refreshed_at || null,
            slots: payload?.meta?.slots || payload?.refresh_slots || [],
        });
        if (payload?.refresh_mode) setBiRefreshMode(payload.refresh_mode);
    };

    const handleBiRecalculate = async () => {
        setBiRecalculating(true);
        try {
            const res = await getBiOverview(true);
            applyBiMeta(res?.data);
        } catch (err) {
            logSafeError('BI recalculate failed', err);
        } finally {
            setBiRecalculating(false);
        }
    };

    const handleBiModeChange = async (next) => {
        const prev = biRefreshMode;
        setBiRefreshMode(next);           // 先动界面，后端失败再回滚
        setBiModeSaving(true);
        try {
            const res = await saveBiSettings({ refresh_mode: next });
            if (res?.success === false) {
                setBiRefreshMode(prev);
                return;
            }
            applyBiMeta(res?.data?.snapshot);
        } catch (err) {
            logSafeError('BI refresh mode save failed', err);
            setBiRefreshMode(prev);
        } finally {
            setBiModeSaving(false);
        }
    };

    return (
        <div className="settings-tab-pane">
            <div className="settings-card">
                <div className="card-body">
                    {/* 磁盘缓存上限 Slider */}
                    <div className="setting-control-group">
                        <div className="control-label-row">
                            <label className="control-label">磁盘缓存上限</label>
                            <span className="control-value-badge">{sysConfig.cache_size_limit_gb || 10} GB</span>
                        </div>
                        <div className="slider-wrapper">
                            <input
                                type="range"
                                min={1}
                                max={100}
                                step={1}
                                value={sysConfig.cache_size_limit_gb || 10}
                                onChange={(e) => handleSysChange('cache_size_limit_gb', Number(e.target.value))}
                                className="settings-slider"
                            />
                        </div>
                    </div>

                    {/* Usage Progress Bar */}
                    <div className="cache-progress-card">
                        <div className="progress-info-row">
                            <span className="progress-title">磁盘已用存储</span>
                            <span className="progress-value">
                                {formatCacheBytes(cacheStats?.total_size_bytes)} / {cacheStats?.limit_gb ?? sysConfig.cache_size_limit_gb} GB ({Math.round((cacheStats?.usage_ratio || 0) * 100)}%)
                            </span>
                        </div>
                        <div className="progress-track">
                            <div
                                className="progress-bar"
                                style={{ width: `${Math.min((cacheStats?.usage_ratio || 0) * 100, 100)}%` }}
                            />
                        </div>
                    </div>

                    {/* Metrics Grid */}
                    <div className="cache-metrics-grid">
                        <div className="metric-box">
                            <span className="metric-num">{cacheStats?.images?.total_count ?? '-'}</span>
                            <span className="metric-label">磁盘文件数</span>
                        </div>
                        <div className="metric-box">
                            <span className="metric-num">{cacheStats?.structured?.total_entries?.toLocaleString() ?? '-'}</span>
                            <span className="metric-label">结构化条目</span>
                        </div>
                        <div className="metric-box">
                            <span className="metric-num">
                                {cacheStats?.images?.hit_rate_recent == null ? '-' : `${Math.round(cacheStats.images.hit_rate_recent * 100)}%`}
                            </span>
                            <span className="metric-label">图片命中率</span>
                        </div>
                        <div className="metric-box">
                            <span className="metric-num">
                                {structuredMetrics.hit_rate == null ? '-' : `${Math.round(structuredMetrics.hit_rate * 100)}%`}
                            </span>
                            <span className="metric-label">结构化命中率</span>
                        </div>
                    </div>

                    {/* Category Details */}
                    <h4 className="section-subheading">分类存储明细</h4>
                    <div className="category-occupancy-list">
                        {Object.entries(cacheStats?.images?.categories || {}).map(([key, item]) => (
                            <div className="occupancy-item" key={key}>
                                <span className="item-name">{CACHE_CATEGORY_LABELS[key] || key}</span>
                                <span className="item-detail">{formatCacheBytes(item.bytes)} · {item.count} 个</span>
                            </div>
                        ))}
                    </div>

                    {/* Actions Row */}
                    <div className="invalidation-row">
                        <label className="control-label">选择要清除的失效分类：</label>
                        <select
                            value={cacheInvalidationCategory}
                            onChange={(e) => setCacheInvalidationCategory(e.target.value)}
                            className="settings-select"
                        >
                            {Object.entries(CACHE_CATEGORY_LABELS).map(([val, label]) => (
                                <option key={val} value={val}>{label}</option>
                            ))}
                        </select>
                    </div>

                    <div className="actions-button-group">
                        <button type="button" className="btn btn-secondary" onClick={fetchCacheStats} disabled={cacheLoading}>
                            <RefreshCw size={15} /> {cacheLoading ? '刷新中...' : '刷新统计'}
                        </button>
                        <button type="button" className="btn btn-secondary" onClick={handleCacheCleanup} disabled={cacheCleaning}>
                            <Trash2 size={15} /> {cacheCleaning ? '整理中...' : '整理到安全水位'}
                        </button>
                        <button type="button" className="btn btn-secondary" onClick={handleCacheInvalidation} disabled={cacheInvalidating}>
                            <RefreshCcw size={15} /> {cacheInvalidating ? '失效中...' : '使所选缓存失效'}
                        </button>
                        <button type="button" className="btn btn-secondary" onClick={handleCacheWarmup} disabled={cacheWarming}>
                            <RefreshCw size={15} /> {cacheWarming ? '提交中...' : '预热常用图片'}
                        </button>
                    </div>
                </div>
            </div>

            {/* 数据看板快照：刷新时间与频率属于运维信息，放在设置里而不是看板页面上 */}
            <div className="settings-card" style={{ marginTop: 16 }}>
                <div className="card-body">
                    <h4 className="section-subheading">数据看板汇总</h4>
                    <div className="bi-snapshot-row">
                        <div className="bi-snapshot-item">
                            <span className="metric-label">上次汇总</span>
                            <span className="metric-num small">{biMeta?.refreshed_at || '尚未生成'}</span>
                        </div>
                        <div className="bi-snapshot-item">
                            <label className="metric-label" htmlFor="bi-refresh-mode">汇总频率</label>
                            <select
                                id="bi-refresh-mode"
                                className="settings-input"
                                style={{ minWidth: 200 }}
                                value={biRefreshMode}
                                onChange={(e) => handleBiModeChange(e.target.value)}
                                disabled={biModeSaving}
                            >
                                <option value="daily">每天一次（11:30）</option>
                                <option value="twice">每天两次（11:30 / 18:00）</option>
                                <option value="realtime">实时（每次打开都重算）</option>
                            </select>
                        </div>
                        <button
                            type="button"
                            className="btn btn-secondary"
                            onClick={handleBiRecalculate}
                            disabled={biRecalculating}
                        >
                            <BarChart3 size={15} /> {biRecalculating ? '重算中...' : '立即重算'}
                        </button>
                    </div>
                    <p className="control-help-text" style={{ marginTop: 10 }}>
                        汇总要扫全库并分页读取媒体库，约需十秒，所以默认每天只跑一次。
                        看板读的是快照，**打开页面不会触发重算**，因此永远是秒开。
                        换频率后会立刻重算一次，避免等到下一个时刻。
                    </p>
                </div>
            </div>
        </div>
    );
}
