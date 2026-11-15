import React, { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { getApiErrorMessage, logSafeError, movieApi } from '../../api';
import SecretInput from './SecretInput';

const formatNumber = (value) => (value == null ? '-' : Number(value).toLocaleString('zh-CN'));

const STAGE_LABELS = {
    queue: '入队', request: '发送', response: '返回',
    write: '写入', skip: '拦下', error: '失败',
};

export default function SettingsTranslateTab({ sysConfig, handleSysChange }) {
    const [status, setStatus] = useState(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [logEntries, setLogEntries] = useState([]);
    const [logCapacity, setLogCapacity] = useState(200);
    const [logLoading, setLogLoading] = useState(false);
    const [logError, setLogError] = useState('');
    const [autoRefresh, setAutoRefresh] = useState(true);

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

    const loadLog = useCallback(async () => {
        setLogLoading(true);
        try {
            const response = await movieApi.getTranslationLog(50);
            if (response?.success && Array.isArray(response.data)) {
                setLogEntries(response.data);
                setLogCapacity(response.capacity || 200);
                setLogError('');
            } else {
                setLogError(getApiErrorMessage(response, '读取调用明细失败'));
            }
        } catch (err) {
            logSafeError('Translation log failed:', err);
            setLogError(getApiErrorMessage(err, '读取调用明细失败'));
        } finally {
            setLogLoading(false);
        }
    }, []);

    useEffect(() => {
        loadLog();
        if (!autoRefresh) return undefined;
        const timer = setInterval(loadLog, 10000);
        return () => clearInterval(timer);
    }, [loadLog, autoRefresh]);

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
                                <label
                                    className="control-label"
                                    title="默认翻译方式为 Google 免费翻译（免Key）；若切换为本地 Ollama，建议使用 Qwen2.5-14B-Instruct-Abliterated-GGUF:latest 这类 14B Abliterated 模型；不要用 Qwen3.6-35B-A3B 系列，速度虽快但实测会删减、漏翻。"
                                >模型名</label>
                                <input
                                    type="text"
                                    className="settings-input"
                                    value={sysConfig.ollama_model || ''}
                                    onChange={(e) => handleSysChange('ollama_model', e.target.value)}
                                    placeholder="Qwen2.5-14B-Instruct-Abliterated-GGUF:latest"
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
                                <SecretInput
                                    className="settings-input"
                                    value={sysConfig.baidu_secret || ''}
                                    onChange={(e) => handleSysChange('baidu_secret', e.target.value)}
                                    placeholder={sysConfig.baidu_secret_configured ? '已配置 (留空保留原有密钥)' : '输入 Secret'}
                                />
                            </div>
                        </div>
                    )}

                    <h4 className="section-subheading">调用明细</h4>
                    <div className="translation-log">
                        <div className="translation-log__bar">
                            <span className="translation-log__hint">
                                最近 {logCapacity} 条 · 仅存内存，重启清空 · 含发送内容、模型原始返回与实际写库
                            </span>
                            <label className="translation-log__auto">
                                <input
                                    type="checkbox"
                                    checked={autoRefresh}
                                    onChange={(e) => setAutoRefresh(e.target.checked)}
                                />
                                自动刷新
                            </label>
                            <button type="button" className="btn btn-secondary" onClick={loadLog} disabled={logLoading}>
                                <RefreshCw size={14} className={logLoading ? 'spin' : ''} /> 刷新
                            </button>
                        </div>
                        {logError && <div className="translation-log__error">{logError}</div>}
                        <div className="translation-log__list">
                            {logEntries.length === 0 ? (
                                <div className="translation-log__empty">
                                    暂无调用记录；打开含日文标题的列表页或详情页会触发增量翻译。
                                </div>
                            ) : logEntries.map((entry) => (
                                <div className="translation-log__entry" key={entry.id}>
                                    <div className="translation-log__head">
                                        <span className={`translation-log__stage translation-log__stage--${entry.stage}`}>
                                            {STAGE_LABELS[entry.stage] || entry.stage}
                                        </span>
                                        <span className="translation-log__ts">
                                            {new Date(entry.ts * 1000).toLocaleString('zh-CN', { hour12: false })}
                                        </span>
                                        {entry.source && <span>{entry.source}{entry.model ? ` · ${entry.model}` : ''}</span>}
                                        {entry.duration_ms != null && <span>{entry.duration_ms} ms</span>}
                                        {entry.note && <span className="translation-log__note">{entry.note}</span>}
                                    </div>
                                    {entry.request && (
                                        <div className="translation-log__row"><span>发送</span><pre>{entry.request}</pre></div>
                                    )}
                                    {entry.response && (
                                        <div className="translation-log__row"><span>返回</span><pre>{entry.response}</pre></div>
                                    )}
                                    {entry.written?.length > 0 && (
                                        <div className="translation-log__row">
                                            <span>写入</span>
                                            <pre>{entry.written.map((item) => `${item.original}  →  ${item.translated}`).join('\n')}</pre>
                                        </div>
                                    )}
                                </div>
                            ))}
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}
