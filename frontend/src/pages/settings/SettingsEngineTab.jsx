import React from 'react';
import { Activity, CheckCircle2, AlertCircle } from 'lucide-react';

export default function SettingsEngineTab({
    qbConfig,
    handleQBChange,
    handleSaveQB,
    savingQB,
    handleTestQB,
    testingQB,
    qbStatus,
    sysConfig,
    handleSysChange
}) {
    return (
        <div className="settings-tab-pane">
            {/* qBittorrent 连接参数 */}
            <div className="settings-card">
                <div className="card-body">
                    <form onSubmit={handleSaveQB} className="settings-form">
                        <div className="form-row">
                            <div className="form-group flex-2">
                                <label className="control-label">服务器 API 地址</label>
                                <input
                                    type="text"
                                    className="settings-input"
                                    value={qbConfig.host || ''}
                                    onChange={(e) => handleQBChange('host', e.target.value)}
                                    placeholder="http://127.0.0.1:8080"
                                />
                            </div>
                            <div className="form-group flex-1">
                                {/* 后端 QBConfig.port 是必填项，之前表单里根本没有这个输入框，
                                    保存时提交 {url, username} 缺 host/port -> 422 校验失败，
                                    表现为「下载客户端怎么都保存不了」。 */}
                                <label className="control-label">端口</label>
                                <input
                                    type="number"
                                    className="settings-input"
                                    value={qbConfig.port ?? ''}
                                    onChange={(e) => handleQBChange('port', Number(e.target.value) || 0)}
                                    placeholder="8080"
                                />
                            </div>
                            <div className="form-group flex-1">
                                <label className="control-label">用户名</label>
                                <input
                                    type="text"
                                    className="settings-input"
                                    value={qbConfig.username || ''}
                                    onChange={(e) => handleQBChange('username', e.target.value)}
                                />
                            </div>
                            <div className="form-group flex-1">
                                <label className="control-label">密码</label>
                                <input
                                    type="password"
                                    className="settings-input"
                                    value={qbConfig.password || ''}
                                    onChange={(e) => handleQBChange('password', e.target.value)}
                                    placeholder={qbConfig.password_configured ? '已配置 (留空保留原密码)' : '输入密码'}
                                />
                            </div>
                        </div>

                        <div className="form-actions-row">
                            <button type="submit" className="btn btn-primary" disabled={savingQB}>
                                {savingQB ? '保存中...' : '保存下载器配置'}
                            </button>
                            <button type="button" className="btn btn-secondary" onClick={handleTestQB} disabled={testingQB}>
                                <Activity size={15} /> {testingQB ? '测试中...' : '测试连接'}
                            </button>

                            {qbStatus !== null && (
                                <div className={`status-pill ${qbStatus.ok ? 'success' : 'error'}`}>
                                    {qbStatus.ok ? (
                                        <>
                                            <CheckCircle2 size={14} /> 可用 (v{qbStatus.version || '-'})
                                        </>
                                    ) : (
                                        <>
                                            <AlertCircle size={14} /> {qbStatus.message || '连接失败'}
                                        </>
                                    )}
                                </div>
                            )}
                        </div>
                    </form>
                </div>
            </div>

            {/* 下载与熔断策略 */}
            <div className="settings-card" style={{ marginTop: 16 }}>
                <div className="card-body">
                    <div className="setting-control-group">
                        <div className="control-label-row">
                            <label className="control-label">磁盘剩余容限熔断阈值</label>
                            <span className="control-value-badge">{sysConfig.disk_threshold_gb || 50} GB</span>
                        </div>
                        <div className="slider-wrapper">
                            <input
                                type="range"
                                min={10}
                                max={2000}
                                step={10}
                                value={sysConfig.disk_threshold_gb || 50}
                                onChange={(e) => handleSysChange('disk_threshold_gb', Number(e.target.value))}
                                className="settings-slider"
                            />
                        </div>
                        <div className="control-help-text">当目标存储可用磁盘低于此阈值时，自动暂停新下载任务。</div>
                    </div>

                    <div className="setting-control-group">
                        <div className="control-label-row">
                            <label className="control-label">最大做种时间限制</label>
                            <span className="control-value-badge">{sysConfig.seeding_time_limit_min || 1440} 分钟</span>
                        </div>
                        <div className="slider-wrapper">
                            <input
                                type="range"
                                min={0}
                                max={4320}
                                step={30}
                                value={sysConfig.seeding_time_limit_min || 1440}
                                onChange={(e) => handleSysChange('seeding_time_limit_min', Number(e.target.value))}
                                className="settings-slider"
                            />
                        </div>
                    </div>

                    <div className="form-group">
                        <label className="control-label">本地完成投放路径 (Delivery Folder)</label>
                        <input
                            type="text"
                            className="settings-input"
                            value={sysConfig.delivery_folder || ''}
                            onChange={(e) => handleSysChange('delivery_folder', e.target.value)}
                            placeholder="/downloads/completed"
                        />
                    </div>

                    <div className="toggle-control-row">
                        <div>
                            <span className="toggle-label">Jable 热门影片自动抓取下载</span>
                            <span className="toggle-help">开启后系统将自动分析流行榜单并推送热门种子</span>
                        </div>
                        <label className="settings-switch">
                            <input
                                type="checkbox"
                                checked={!!sysConfig.jable_hot_auto_download}
                                onChange={(e) => handleSysChange('jable_hot_auto_download', e.target.checked)}
                            />
                            <span className="switch-slider"></span>
                        </label>
                    </div>
                </div>
            </div>
        </div>
    );
}
