import React, { useCallback, useEffect, useState } from 'react';
import { ShieldAlert, RefreshCw, Loader2 } from 'lucide-react';
import { getJellyfinLibraries, getApiErrorMessage, logSafeError } from '../../api';

// 与后端 jellyfin_delete_guard 的确认短语保持一致
const DELETE_ENABLE_PHRASE = 'DELETE_JELLYFIN_ITEMS';

export default function SettingsJellyfinTab({ sysConfig, handleSysChange }) {
    const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
    const [deleteConfirmText, setDeleteConfirmText] = useState('');
    // 媒体库列表：地址与 Key 配好后从 Jellyfin 拉，让用户勾选而不是手填库名
    const [libraries, setLibraries] = useState(null);
    const [libsLoading, setLibsLoading] = useState(false);
    const [libsError, setLibsError] = useState('');

    const host = String(sysConfig.jellyfin_host || '').trim();
    // 密钥不回显，只有用户新填了才当"已填"；已配置过但没重填时后端仍用库里那份
    const keyTyped = String(sysConfig.jellyfin_api_key || '').trim();
    const canFetch = !!host && (keyTyped || !!sysConfig.jellyfin_api_key_configured);

    const fetchLibraries = useCallback(async () => {
        if (!canFetch) return;
        setLibsLoading(true);
        setLibsError('');
        try {
            const res = await getJellyfinLibraries();
            const list = Array.isArray(res?.data) ? res.data : null;
            if (list && list.length > 0) {
                setLibraries(list);
            } else {
                setLibraries([]);
                // 服务不可达与配置错误是两种情况，别一律说成地址填错
                setLibsError(
                    res?.success === false
                        ? 'Jellyfin 连不上，请确认服务在运行、地址可达'
                        : '没读到媒体库列表（服务可达但返回为空）'
                );
            }
        } catch (err) {
            logSafeError('Jellyfin libraries failed:', err);
            setLibraries([]);
            setLibsError(getApiErrorMessage(err, '读取媒体库失败'));
        } finally {
            setLibsLoading(false);
        }
    }, [canFetch]);

    // 地址/密钥齐了就把已配置的库回填一次，省得用户手点
    useEffect(() => {
        if (canFetch && libraries === null) fetchLibraries();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [canFetch]);

    const pickLibrary = (name) => handleSysChange('jellyfin_library_name', name || '');

    const handleDeleteToggle = (checked) => {
        if (!checked) {
            handleSysChange('jellyfin_delete_enabled', false);
            setDeleteConfirmOpen(false);
            setDeleteConfirmText('');
            return;
        }
        // 开启危险开关前必须输入确认短语
        setDeleteConfirmOpen(true);
        setDeleteConfirmText('');
    };

    const confirmEnableDelete = () => {
        if (deleteConfirmText.trim() !== DELETE_ENABLE_PHRASE) return;
        handleSysChange('jellyfin_delete_enabled', true);
        setDeleteConfirmOpen(false);
        setDeleteConfirmText('');
    };

    return (
        <div className="settings-tab-pane">
            <div className="settings-card">
                <div className="card-body">
                    <div className="jellyfin-compact-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '20px' }}>
                        {/* 左列：连接配置与自动化开关 */}
                        <div className="jellyfin-col-left">
                            <div className="form-group">
                                <label className="control-label">Jellyfin 主机地址</label>
                                <input
                                    type="text"
                                    className="settings-input"
                                    value={sysConfig.jellyfin_host || ''}
                                    onChange={(e) => handleSysChange('jellyfin_host', e.target.value)}
                                    placeholder="http://127.0.0.1:8096"
                                />
                            </div>

                            <div className="form-row" style={{ marginTop: 10 }}>
                                <div className="form-group flex-1">
                                    <label className="control-label">API Key 密钥</label>
                                    <input
                                        type="password"
                                        className="settings-input"
                                        value={sysConfig.jellyfin_api_key || ''}
                                        onChange={(e) => handleSysChange('jellyfin_api_key', e.target.value)}
                                        placeholder={sysConfig.jellyfin_api_key_configured ? '已配置 (留空保留原Key；改主机时需重填)' : '输入 API Key'}
                                    />
                                </div>
                                <div className="form-group flex-1">
                                <label className="control-label">监控媒体库</label>
                                {canFetch ? (
                                    <>
                                        <select
                                            className="settings-input"
                                            value={sysConfig.jellyfin_library_name || ''}
                                            onChange={(e) => pickLibrary(e.target.value)}
                                        >
                                            <option value="">（未选择）</option>
                                            {(libraries || []).map(lib => (
                                                <option key={lib.id || lib.name} value={lib.name}>
                                                    {lib.name}
                                                    {lib.type ? ` · ${lib.type}` : ''}
                                                </option>
                                            ))}
                                            {/* 拉取失败或库里都没有时，仍允许手填，避免把用户卡死 */}
                                            {(!libraries || libraries.length === 0) && (
                                                <option value={sysConfig.jellyfin_library_name || ''}>
                                                    {sysConfig.jellyfin_library_name || '手动填写'}
                                                </option>
                                            )}
                                        </select>
                                        <div className="jellyfin-library-actions">
                                            <button
                                                type="button"
                                                className="btn btn-secondary btn-sm"
                                                onClick={fetchLibraries}
                                                disabled={libsLoading}
                                            >
                                                {libsLoading
                                                    ? <><Loader2 size={13} className="spin" /> 读取中...</>
                                                    : <><RefreshCw size={13} /> 重新读取</>}
                                            </button>
                                            {libraries && libraries.length > 0 && (
                                                <span className="control-hint">
                                                    共 {libraries.length} 个库，请选择要监控的那个
                                                </span>
                                            )}
                                        </div>
                                        {libsError && <div className="control-hint error">{libsError}</div>}
                                    </>
                                ) : (
                                    <>
                                        <input
                                            type="text"
                                            className="settings-input"
                                            value={sysConfig.jellyfin_library_name || ''}
                                            onChange={(e) => pickLibrary(e.target.value)}
                                            placeholder="先填上方地址与 API Key，再从这里选择"
                                        />
                                        <div className="control-hint">填好地址与 API Key 后自动读取，无需手填库名</div>
                                    </>
                                )}
                            </div>
                            </div>

                            <div className="toggle-group-grid" style={{ marginTop: 12 }}>
                                <div className="toggle-control-row">
                                    <div><span className="toggle-label">自动关注演员更新</span></div>
                                    <label className="settings-switch">
                                        <input
                                            type="checkbox"
                                            checked={!!sysConfig.auto_follow_enabled}
                                            onChange={(e) => handleSysChange('auto_follow_enabled', e.target.checked)}
                                        />
                                        <span className="switch-slider"></span>
                                    </label>
                                </div>

                                <div className="toggle-control-row">
                                    <div><span className="toggle-label">自动创建新剧集订阅</span></div>
                                    <label className="settings-switch">
                                        <input
                                            type="checkbox"
                                            checked={!!sysConfig.auto_subscribe_enabled}
                                            onChange={(e) => handleSysChange('auto_subscribe_enabled', e.target.checked)}
                                        />
                                        <span className="switch-slider"></span>
                                    </label>
                                </div>

                                <div className="toggle-control-row">
                                    <div><span className="toggle-label">自动批准高分种子下载</span></div>
                                    <label className="settings-switch">
                                        <input
                                            type="checkbox"
                                            checked={!!sysConfig.auto_download_enabled}
                                            onChange={(e) => handleSysChange('auto_download_enabled', e.target.checked)}
                                        />
                                        <span className="switch-slider"></span>
                                    </label>
                                </div>
                            </div>
                        </div>

                        {/* 右列：模式选择、观影清理与硬核删除开关 */}
                        <div className="jellyfin-col-right">
                            <div className="form-group">
                                <label className="control-label">自动化运行模式</label>
                                <select
                                    className="settings-select"
                                    value={sysConfig.automation_mode || 'off'}
                                    onChange={(e) => handleSysChange('automation_mode', e.target.value)}
                                >
                                    <option value="off">关闭全自动 (仅手动作业)</option>
                                    <option value="suggest">仅生成建议 (推送候选项由人工决定)</option>
                                    <option value="auto_approve_low_risk">自动批准低风险操作 (推荐)</option>
                                </select>
                            </div>

                            <div className="toggle-control-row" style={{ marginTop: 10 }}>
                                <div><span className="toggle-label">开启已观影自动清理计划</span></div>
                                <label className="settings-switch">
                                    <input
                                        type="checkbox"
                                        checked={!!sysConfig.cleanup_enabled}
                                        onChange={(e) => handleSysChange('cleanup_enabled', e.target.checked)}
                                    />
                                    <span className="switch-slider"></span>
                                </label>
                            </div>

                            <div className="form-row" style={{ marginTop: 8 }}>
                                <div className="setting-control-group flex-1" style={{ marginBottom: 0 }}>
                                    <div className="control-label-row">
                                        <label className="control-label">清理进度阈值</label>
                                        <span className="control-value-badge">{sysConfig.cleanup_progress_threshold || 90}%</span>
                                    </div>
                                    <div className="slider-wrapper">
                                        <input
                                            type="range"
                                            min={50}
                                            max={100}
                                            step={5}
                                            value={sysConfig.cleanup_progress_threshold || 90}
                                            onChange={(e) => handleSysChange('cleanup_progress_threshold', Number(e.target.value))}
                                            className="settings-slider"
                                        />
                                    </div>
                                </div>

                                <div className="setting-control-group flex-1" style={{ marginBottom: 0 }}>
                                    <div className="control-label-row">
                                        <label className="control-label">定时清理时间</label>
                                        <span className="control-value-badge">凌晨 {sysConfig.cleanup_run_hour || 3}:00 点</span>
                                    </div>
                                    <div className="slider-wrapper">
                                        <input
                                            type="range"
                                            min={0}
                                            max={23}
                                            step={1}
                                            value={sysConfig.cleanup_run_hour || 3}
                                            onChange={(e) => handleSysChange('cleanup_run_hour', Number(e.target.value))}
                                            className="settings-slider"
                                        />
                                    </div>
                                </div>
                            </div>

                            <div className="danger-zone-box" style={{ marginTop: 12, padding: '10px 12px' }}>
                                <div className="danger-header" style={{ marginBottom: 6 }}>
                                    <ShieldAlert size={16} />
                                    <span>高级控制：允许直接通过接口彻底删除 Jellyfin 磁盘文件</span>
                                </div>
                                <div className="toggle-control-row" style={{ border: 'none', padding: 0 }}>
                                    <span className="toggle-help" style={{ fontSize: 12 }}>危险操作，开启需输入确认短语</span>
                                    <label className="settings-switch">
                                        <input
                                            type="checkbox"
                                            checked={!!sysConfig.jellyfin_delete_enabled}
                                            onChange={(e) => handleDeleteToggle(e.target.checked)}
                                        />
                                        <span className="switch-slider danger"></span>
                                    </label>
                                </div>
                                {deleteConfirmOpen && !sysConfig.jellyfin_delete_enabled && (
                                    <div style={{ marginTop: 10, padding: '10px 12px', border: '1px solid var(--error)', borderRadius: 'var(--radius)', background: 'rgba(239, 68, 68, 0.08)' }}>
                                        <p style={{ fontSize: 12, color: 'var(--text-soft)', marginBottom: 8 }}>
                                            开启后可通过接口彻底删除 Jellyfin 磁盘文件，不可恢复。请输入 <strong style={{ color: 'var(--error)' }}>{DELETE_ENABLE_PHRASE}</strong> 确认：
                                        </p>
                                        <div style={{ display: 'flex', gap: 8 }}>
                                            <input
                                                type="text"
                                                className="settings-input"
                                                style={{ flex: 1 }}
                                                value={deleteConfirmText}
                                                onChange={(e) => setDeleteConfirmText(e.target.value)}
                                                onKeyDown={(e) => {
                                                    if (e.key === 'Enter') confirmEnableDelete();
                                                    if (e.key === 'Escape') { setDeleteConfirmOpen(false); setDeleteConfirmText(''); }
                                                }}
                                                placeholder={DELETE_ENABLE_PHRASE}
                                                autoFocus
                                            />
                                            <button
                                                type="button"
                                                className="btn"
                                                disabled={deleteConfirmText.trim() !== DELETE_ENABLE_PHRASE}
                                                onClick={confirmEnableDelete}
                                            >
                                                确认开启
                                            </button>
                                        </div>
                                    </div>
                                )}
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}
