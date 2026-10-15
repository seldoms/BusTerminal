import React from 'react';

export default function SettingsNotifyTab({ sysConfig, handleSysChange }) {
    return (
        <div className="settings-tab-pane">
            <div className="settings-card">
                <div className="card-body">
                    <div className="toggle-control-row">
                        <div>
                            <span className="toggle-label">启用邮件通知 (Email Alerts)</span>
                            <span className="toggle-help">当遇到重要事件或自动下载成功时发送总结邮件</span>
                        </div>
                        <label className="settings-switch">
                            <input
                                type="checkbox"
                                checked={!!sysConfig.email_enabled}
                                onChange={(e) => handleSysChange('email_enabled', e.target.checked)}
                            />
                            <span className="switch-slider"></span>
                        </label>
                    </div>

                    {sysConfig.email_enabled && (
                        <div className="nested-form-group">
                            <div className="form-row">
                                <div className="form-group flex-2">
                                    <label className="control-label">SMTP 服务器地址</label>
                                    <input
                                        type="text"
                                        className="settings-input"
                                        value={sysConfig.email_host || ''}
                                        onChange={(e) => handleSysChange('email_host', e.target.value)}
                                        placeholder="smtp.qq.com"
                                    />
                                </div>
                                <div className="form-group flex-1">
                                    <label className="control-label">端口 (Port)</label>
                                    <input
                                        type="text"
                                        className="settings-input"
                                        value={sysConfig.email_port || ''}
                                        onChange={(e) => handleSysChange('email_port', e.target.value)}
                                        placeholder="465"
                                    />
                                </div>
                            </div>

                            <div className="form-row">
                                <div className="form-group flex-1">
                                    <label className="control-label">发件人邮箱</label>
                                    <input
                                        type="text"
                                        className="settings-input"
                                        value={sysConfig.email_user || ''}
                                        onChange={(e) => handleSysChange('email_user', e.target.value)}
                                    />
                                </div>
                                <div className="form-group flex-1">
                                    <label className="control-label">授权码 / 密码</label>
                                    <input
                                        type="password"
                                        className="settings-input"
                                        value={sysConfig.email_pass || ''}
                                        onChange={(e) => handleSysChange('email_pass', e.target.value)}
                                        placeholder={sysConfig.email_pass_configured ? '已配置 (留空保留原密码)' : ''}
                                    />
                                </div>
                            </div>

                            <div className="form-group">
                                <label className="control-label">接收通知的邮箱 (To Email)</label>
                                <input
                                    type="text"
                                    className="settings-input"
                                    value={sysConfig.email_to || ''}
                                    onChange={(e) => handleSysChange('email_to', e.target.value)}
                                />
                            </div>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
