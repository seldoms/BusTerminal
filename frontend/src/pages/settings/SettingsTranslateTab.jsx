import React, { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { getApiErrorMessage, logSafeError, movieApi } from '../../api';

const formatNumber = (value) => (value == null ? '-' : Number(value).toLocaleString('zh-CN'));

export default function SettingsTranslateTab({ sysConfig, handleSysChange }) {
    const [status, setStatus] = useState(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');

    const loadStatus = useCallback(async () => {
        setLoading(true);
        try {
            const response = await movieApi.getTranslationStatus();
            if (response?.success && response.data) {
                setStatus(response.data);
                setError('');
            } else {
                setError(getApiErrorMessage(response, '读取翻译进度失败'));
            }
        } catch (err) {
            logSafeError('Translation status failed:', err);
            setError(getApiErrorMessage(err, '读取翻译进度失败'));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        loadStatus();
        const timer = setInterval(loadStatus, 30000);
        return () => clearInterval(timer);
    }, [loadStatus]);

    const byYear = status?.by_year || [];

    return (
        <div className="settings-tab-pane">
            <div className="settings-card">
                <div className="card-body">
                    {/* 翻译进度总览 */}
                    <div className="control-label-row">
                        <label className="control-label">翻译进度</label>
                        <span className="control-value-badge">
                            {status?.provider || '-'}
                            {status?.model ? ` · ${status.model}` : ''}
                        </span>
                    </div>

                    <div className="cache-metrics-grid">
                        <div className="metric-box">
                            <span className="metric-num">{formatNumber(status?.translated_total)}</span>
                            <span className="metric-label">已翻译</span>
                        </div>
                        <div className="metric-box">
                            <span className="metric-num">
                                {status?.pending_total == null ? '统计中…' : formatNumber(status.pending_total)}
                            </span>
                            <span className="metric-label">待翻译（去重）</span>
                        </div>
                        <div className="metric-box">
                            <span className="metric-num">{formatNumber(status?.queue_size)}</span>
                            <span className="metric-label">队列中</span>
                        </div>
                        <div className="metric-box">
                            <span className="metric-num">{status?.rate_per_min ?? '-'}</span>
                            <span className="metric-label">条/分钟（近 10 分钟）</span>
                        </div>
                    </div>

                    <div style={{ marginTop: 10, fontSize: 13, color: 'var(--text-muted)', lineHeight: 1.8 }}>
                        <div>
                            回填游标：<strong>{status?.cursor || '-'}</strong>（按发行日期从新到旧推进）
                            {status?.eta_days != null && <> ｜ 预计剩余 <strong>{status.eta_days}</strong> 天</>}
                            {status?.backfill_enabled === false && <> ｜ 回填已关闭</>}
                        </div>
                        {byYear.length > 0 && (
                            <div>
                                待翻年份：
                                {byYear.map((item) => `${item.year} ${formatNumber(item.pending)}`).join(' ｜ ')}
                            </div>
                        )}
                        {status?.snapshot_at ? (
                            <div>统计时间：{new Date(status.snapshot_at * 1000).toLocaleString('zh-CN', { hour12: false })}（每 30 分钟更新）</div>
                        ) : (
                            <div>正在统计待翻数量（全量去重约需 20~30 秒）…</div>
                        )}
                        {error && <div style={{ color: 'var(--error, #e5484d)' }}>{error}</div>}
                    </div>

                    <div className="actions-button-group">
                        <button type="button" className="btn btn-secondary" onClick={loadStatus} disabled={loading}>
                            <RefreshCw size={15} className={loading ? 'spin' : ''} /> {loading ? '刷新中...' : '刷新进度'}
                        </button>
                    </div>

                    <h4 className="section-subheading">翻译服务</h4>
                    <div className="form-group">
                        <label className="control-label">默认翻译服务提供商</label>
                        <select
                            className="settings-select"
                            value={sysConfig.translation_provider || 'google_free'}
                            onChange={(e) => handleSysChange('translation_provider', e.target.value)}
                        >
                            <option value="google_free">Google 翻译 (免费免Key)</option>
                            <option value="baidu_dev">百度翻译 API (需开发者 AppID/Key)</option>
                            <option value="ollama_local">本地大模型 (内网 Ollama)</option>
                        </select>
                    </div>

                    {sysConfig.translation_provider === 'ollama_local' && (
                        <div className="nested-form-group">
                            <div className="form-group">
                                <label className="control-label">Ollama 地址</label>
                                <input
                                    type="text"
                                    className="settings-input"
                                    value={sysConfig.ollama_base_url || ''}
                                    onChange={(e) => handleSysChange('ollama_base_url', e.target.value)}
                                    placeholder="http://127.0.0.1:11434"
                                />
                            </div>
                            <div className="form-group">
                                <label className="control-label">模型名</label>
                                <input
                                    type="text"
                                    className="settings-input"
                                    value={sysConfig.ollama_model || ''}
                                    onChange={(e) => handleSysChange('ollama_model', e.target.value)}
                                    placeholder="qwen3.8:27b"
                                />
                            </div>
                        </div>
                    )}

                    {sysConfig.translation_provider === 'baidu_dev' && (
                        <div className="nested-form-group">
                            <div className="form-group">
                                <label className="control-label">百度 APP ID</label>
                                <input
                                    type="text"
                                    className="settings-input"
                                    value={sysConfig.baidu_app_id || ''}
                                    onChange={(e) => handleSysChange('baidu_app_id', e.target.value)}
                                />
                            </div>
                            <div className="form-group">
                                <label className="control-label">百度 Secret Key</label>
                                <input
                                    type="password"
                                    className="settings-input"
                                    value={sysConfig.baidu_secret || ''}
                                    onChange={(e) => handleSysChange('baidu_secret', e.target.value)}
                                    placeholder={sysConfig.baidu_secret_configured ? '已配置 (留空保留原有密钥)' : '输入 Secret'}
                                />
                            </div>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
