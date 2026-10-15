import React from 'react';

export default function SettingsNetworkTab({ sysConfig, handleSysChange }) {
    return (
        <div className="settings-tab-pane">
            <div className="settings-card">
                <div className="card-body">
                    <div className="form-group">
                        <label className="control-label">系统代理 (System Proxy)</label>
                        <input
                            type="text"
                            className="settings-input"
                            value={sysConfig.system_proxy || ''}
                            onChange={(e) => handleSysChange('system_proxy', e.target.value)}
                            placeholder={sysConfig.system_proxy_configured ? '已配置 (留空保持原代理)' : 'http://127.0.0.1:7890'}
                        />
                        <div className="control-help-text">支持 HTTP / SOCKS5 格式，访问国外元数据节点时建议配置</div>
                    </div>

                    <div className="toggle-control-row">
                        <div>
                            <span className="toggle-label">启用域名自动轮询与防封锁 (Domain Rotation)</span>
                            <span className="toggle-help">当当前域名失效时，系统自动测试并切换至可用备用域名节点</span>
                        </div>
                        <label className="settings-switch">
                            <input
                                type="checkbox"
                                checked={!!sysConfig.enable_domain_rotation}
                                onChange={(e) => handleSysChange('enable_domain_rotation', e.target.checked)}
                            />
                            <span className="switch-slider"></span>
                        </label>
                    </div>

                    {!sysConfig.enable_domain_rotation && (
                        <div className="form-group" style={{ marginTop: 16 }}>
                            <label className="control-label">固定节点域名 (Fixed Domain)</label>
                            <input
                                type="text"
                                className="settings-input"
                                value={sysConfig.fixed_domain || ''}
                                onChange={(e) => handleSysChange('fixed_domain', e.target.value)}
                                placeholder="https://www.javbus.com"
                            />
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
