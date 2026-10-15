import React, { useCallback, useEffect, useState } from 'react';
import {
    Activity,
    CheckCircle2,
    XCircle,
    AlertTriangle,
    RefreshCw,
    Save,
    KeyRound,
    Server,
} from 'lucide-react';
import {
    getAmaneStatus,
    getAmaneConfig,
    saveAmaneConfig,
    getAmaneSelfcheck,
    getAmaneInventory,
    getAmaneIngestConfig,
    saveAmaneIngestConfig,
    getLifecycleMovies,
    runAmaneIngest,
    confirmAmaneVisibility,
} from '../../api';

const STATUS_ICON = {
    ok: <CheckCircle2 size={15} color="var(--success, #16a34a)" />,
    fail: <XCircle size={15} color="var(--danger, #dc2626)" />,
    warn: <AlertTriangle size={15} color="var(--warning, #d97706)" />,
    skip: <Activity size={15} color="var(--text-muted)" />,
};

function unwrap(resp) {
    // 后端统一 { success, data }
    return (resp && resp.data && resp.data.data !== undefined) ? resp.data.data : (resp && resp.data);
}

async function loadPending() {
    // 闭环里"没跑完/跑挂了"的都算待人工：failed（某阶段任务失败）与 not_found（没被 amane 登记）
    const statuses = ['failed', 'not_found'];
    const batches = await Promise.all(
        statuses.map((s) => getLifecycleMovies({ organize_status: s, limit: 50 })
            .then((r) => (unwrap(r) || []).map((row) => ({ ...row, _pendingKind: s })))
            .catch(() => [])),
    );
    const merged = new Map();
    batches.flat().forEach((row) => {
        if (row.movie_id && !merged.has(row.movie_id)) merged.set(row.movie_id, row);
    });
    return Array.from(merged.values());
}

export default function SettingsMediaStackTab() {
    const [loading, setLoading] = useState(true);
    const [checking, setChecking] = useState(false);
    const [savingToken, setSavingToken] = useState(false);
    const [savingIngest, setSavingIngest] = useState(false);
    const [error, setError] = useState('');

    const [status, setStatus] = useState({});
    const [config, setConfig] = useState({ base_url: '', token_masked: '', configured: false });
    const [selfcheck, setSelfcheck] = useState({ ok: false, checks: [] });
    const [inventory, setInventory] = useState(null);
    const [ingest, setIngest] = useState({});
    const [pendingItems, setPendingItems] = useState([]);
    const [retrying, setRetrying] = useState('');

    const [baseUrl, setBaseUrl] = useState('');
    const [token, setToken] = useState('');

    const load = useCallback(async (withInventory = false) => {
        setChecking(true);
        setError('');
        try {
            const [st, cfg, sc, ic] = await Promise.all([
                getAmaneStatus().catch(() => ({ data: {} })),
                getAmaneConfig().catch(() => ({ data: {} })),
                getAmaneSelfcheck().catch(() => ({ data: { ok: false, checks: [] } })),
                getAmaneIngestConfig().catch(() => ({ data: {} })),
            ]);
            setStatus(unwrap(st) || {});
            const cfgData = unwrap(cfg) || {};
            setConfig(cfgData);
            setBaseUrl(cfgData.base_url || '');
            setSelfcheck(unwrap(sc) || { ok: false, checks: [] });
            setIngest(unwrap(ic) || {});
            try {
                setPendingItems(await loadPending());
            } catch (pendingErr) {
                setPendingItems([]);
            }
            if (withInventory) {
                try {
                    setInventory(unwrap(await getAmaneInventory(true)) || null);
                } catch (invErr) {
                    // 盘点要 token，没有token时失败属于预期
                    setInventory({ error: invErr?.response?.data?.detail || String(invErr) });
                }
            }
        } catch (err) {
            setError(err?.response?.data?.detail || String(err));
        } finally {
            setChecking(false);
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        load();
    }, [load]);

    const handleSaveToken = async () => {
        setSavingToken(true);
        setError('');
        try {
            const payload = {};
            if (baseUrl) payload.base_url = baseUrl;
            if (token) payload.token = token;
            await saveAmaneConfig(payload);
            setToken('');
            await load(true);
        } catch (err) {
            setError(err?.response?.data?.detail || String(err));
        } finally {
            setSavingToken(false);
        }
    };

    const handleSaveIngest = async () => {
        setSavingIngest(true);
        setError('');
        try {
            await saveAmaneIngestConfig({
                enabled: ingest.enabled,
                library_id: ingest.library_id,
                scan_path: ingest.scan_path,
                organize_enabled: ingest.organize_enabled,
                verify_visible: ingest.verify_visible,
                dedupe_enabled: ingest.dedupe_enabled,
            });
            await load();
        } catch (err) {
            setError(err?.response?.data?.detail || String(err));
        } finally {
            setSavingIngest(false);
        }
    };

    const patchIngest = (key, value) => setIngest((prev) => ({ ...prev, [key]: value }));

    const handleRetry = async (movie_id) => {
        setRetrying(movie_id);
        setError('');
        try {
            await runAmaneIngest({ movie_id, number: movie_id, force: true });
            setPendingItems(await loadPending());
        } catch (err) {
            setError(err?.response?.data?.detail || String(err));
        } finally {
            setRetrying('');
        }
    };

    const handleRecheckVisible = async (movie_id) => {
        setRetrying(`${movie_id}#visible`);
        setError('');
        try {
            await confirmAmaneVisibility({ movie_id, number: movie_id });
        } catch (err) {
            setError(err?.response?.data?.detail || String(err));
        } finally {
            setRetrying('');
        }
    };

    return (
        <div className="settings-tab-pane">
            <div className="settings-card">
                <div className="card-header">
                    <div className="header-icon"><Server size={17} /></div>
                    <div>
                        <h3 className="card-title">媒体栈连通性</h3>
                        <p className="card-subtitle">
                            方案 C：BusTerminal 不碰文件，扫描/刮削/整理全部由 amane 执行，这里只做连通性与凭据校验
                        </p>
                    </div>
                    <div style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
                        <button
                            type="button"
                            className="btn btn-secondary"
                            onClick={() => load(true)}
                            disabled={checking}
                        >
                            <RefreshCw size={14} />
                            <span>{checking ? '检测中...' : '重新检测'}</span>
                        </button>
                    </div>
                </div>
                <div className="card-body">
                    {loading && <div className="card-subtitle">加载中...</div>}
                    {error && <div className="status-pill error">{error}</div>}

                    {!loading && (
                        <>
                            <div className="form-row">
                                <div className="form-group flex-1">
                                    <label className="control-label">amane 服务</label>
                                    <div className="settings-input" style={{ opacity: 0.85 }}>
                                        {config.base_url || '(未配置)'}
                                    </div>
                                </div>
                                <div className="form-group flex-1">
                                    <label className="control-label">版本</label>
                                    <div className="settings-input" style={{ opacity: 0.85 }}>
                                        {status.version || '—'}
                                    </div>
                                </div>
                                <div className="form-group flex-1">
                                    <label className="control-label">已索引媒体库</label>
                                    <div className="settings-input" style={{ opacity: 0.85 }}>
                                        {status.library_count ?? '—'}
                                    </div>
                                </div>
                            </div>

                            <div className="setting-control-group">
                                <label className="control-label">
                                    <KeyRound size={13} style={{ marginRight: 6 }} />
                                    API Token
                                </label>
                                <div className="control-label-row">
                                    <span className="card-subtitle">
                                        当前：{config.token_masked || '(未配置，除 /api/health 外全部端点都需要它)'}
                                    </span>
                                </div>
                                <input
                                    type="password"
                                    className="settings-input"
                                    value={token}
                                    onChange={(e) => setToken(e.target.value)}
                                    placeholder="留空表示不修改"
                                    autoComplete="new-password"
                                />
                            </div>

                            <div className="setting-control-group">
                                <label className="control-label">amane 地址</label>
                                <input
                                    type="text"
                                    className="settings-input"
                                    value={baseUrl}
                                    onChange={(e) => setBaseUrl(e.target.value)}
                                    placeholder="http://192.168.1.100:8000"
                                />
                            </div>

                            <button
                                type="button"
                                className="btn btn-primary"
                                onClick={handleSaveToken}
                                disabled={savingToken}
                            >
                                <Save size={14} />
                                <span>{savingToken ? '保存中...' : '保存凭据'}</span>
                            </button>
                        </>
                    )}
                </div>
            </div>

            <div className="settings-card" style={{ marginTop: 16 }}>
                <div className="card-header">
                    <div className="header-icon"><Activity size={17} /></div>
                    <div>
                        <h3 className="card-title">自检结果</h3>
                        <p className="card-subtitle">链路上哪一环没通，逐项给出修复提示</p>
                    </div>
                </div>
                <div className="card-body">
                    {!loading && (selfcheck.checks || []).length === 0 && (
                        <div className="card-subtitle">暂无自检数据，点上方"重新检测"</div>
                    )}
                    {(selfcheck.checks || []).map((check) => (
                        <div className="toggle-control-row" key={check.id || check.name}>
                            <div>
                                <span className="toggle-label">
                                    {STATUS_ICON[check.status] || STATUS_ICON.skip}
                                    {' '}
                                    {check.name}
                                </span>
                                <span className="toggle-help">{check.detail}</span>
                                {check.hint && check.status !== 'ok' && (
                                    <span className="toggle-help">修复建议：{check.hint}</span>
                                )}
                            </div>
                        </div>
                    ))}
                </div>
            </div>

            <div className="settings-card" style={{ marginTop: 16 }}>
                <div className="card-header">
                    <div className="header-icon"><CheckCircle2 size={17} /></div>
                    <div>
                        <h3 className="card-title">入库闭环参数</h3>
                        <p className="card-subtitle">下载完成后由谁扫、要不要整理、是否确认 Jellyfin 可见</p>
                    </div>
                </div>
                <div className="card-body">
                    {[
                        ['enabled', '启用入库闭环', '下载完成后自动触发 amane 入库'],
                        ['organize_enabled', '整理归档', '入库后触发 amane organize（改文件名与目录）'],
                        ['verify_visible', '确认 Jellyfin 可见', '整理后刷新 Jellyfin 并轮询到该片可见'],
                        ['dedupe_enabled', '去重仲裁', '提交下载前查库里是否已有同番号'],
                    ].map(([key, label, desc]) => (
                        <div className="toggle-control-row" key={key}>
                            <div>
                                <span className="toggle-label">{label}</span>
                                <span className="toggle-help">{desc}</span>
                            </div>
                            <label className="settings-switch">
                                <input
                                    type="checkbox"
                                    checked={ingest[key] !== false}
                                    onChange={(e) => patchIngest(key, e.target.checked)}
                                />
                                <span className="switch-slider"></span>
                            </label>
                        </div>
                    ))}

                    <div className="form-row" style={{ marginTop: 12 }}>
                        <div className="form-group flex-1">
                            <label className="control-label">目标库 ID</label>
                            <input
                                type="text"
                                className="settings-input"
                                value={ingest.library_id ?? ''}
                                onChange={(e) => patchIngest('library_id', e.target.value)}
                                placeholder="留空则挑第一个 automation=scrape 的库"
                            />
                        </div>
                        <div className="form-group flex-1">
                            <label className="control-label">扫描子路径</label>
                            <input
                                type="text"
                                className="settings-input"
                                value={ingest.scan_path || ''}
                                onChange={(e) => patchIngest('scan_path', e.target.value)}
                                placeholder="completed"
                            />
                        </div>
                    </div>

                    <button
                        type="button"
                        className="btn btn-primary"
                        onClick={handleSaveIngest}
                        disabled={savingIngest}
                        style={{ marginTop: 8 }}
                    >
                        <Save size={14} />
                        <span>{savingIngest ? '保存中...' : '保存闭环参数'}</span>
                    </button>
                </div>
            </div>

            <div className="settings-card" style={{ marginTop: 16 }}>
                <div className="card-header">
                    <div className="header-icon"><AlertTriangle size={17} /></div>
                    <div>
                        <h3 className="card-title">待人工处理</h3>
                        <p className="card-subtitle">
                            闭环没跑完或跑挂的片。不会静默丢弃，这里能看到原因并重试
                        </p>
                    </div>
                    <div style={{ marginLeft: 'auto' }}>
                        <span className="control-value-badge">{pendingItems.length}</span>
                    </div>
                </div>
                <div className="card-body">
                    {pendingItems.length === 0 && (
                        <div className="card-subtitle">没有待处理项</div>
                    )}
                    {pendingItems.map((item) => (
                        <div className="setting-control-group" key={item.movie_id}>
                            <div className="control-label-row">
                                <span className="toggle-label">{item.movie_id}</span>
                                <span className="control-value-badge">
                                    {item._pendingKind === 'failed' ? '入库失败' : '未被登记'}
                                </span>
                            </div>
                            <span className="toggle-help">
                                {item.last_reason || '（无原因记录）'}
                            </span>
                            <div style={{ display: 'flex', gap: 8, marginTop: 6 }}>
                                <button
                                    type="button"
                                    className="btn btn-secondary"
                                    onClick={() => handleRetry(item.movie_id)}
                                    disabled={!!retrying}
                                >
                                    <RefreshCw size={13} />
                                    <span>{retrying === item.movie_id ? '重试中...' : '重新入库'}</span>
                                </button>
                                <button
                                    type="button"
                                    className="btn btn-secondary"
                                    onClick={() => handleRecheckVisible(item.movie_id)}
                                    disabled={!!retrying}
                                >
                                    <span>
                                        {retrying === `${item.movie_id}#visible` ? '确认中...' : '补确认 Jellyfin 可见'}
                                    </span>
                                </button>
                            </div>
                        </div>
                    ))}
                </div>
            </div>

            {inventory && (
                <div className="settings-card" style={{ marginTop: 16 }}>
                    <div className="card-header">
                        <div className="header-icon"><AlertTriangle size={17} /></div>
                        <div>
                            <h3 className="card-title">库配置盘点</h3>
                            <p className="card-subtitle">按入库要求逐项核对，给出待改项</p>
                        </div>
                    </div>
                    <div className="card-body">
                        {inventory.error && <div className="status-pill error">{inventory.error}</div>}
                        {(inventory.libraries || []).map((lib) => (
                            <div className="setting-control-group" key={lib.id}>
                                <span className="toggle-label">
                                    #{lib.id} {lib.name} — {lib.path}
                                </span>
                                {(lib.suggestions || []).length === 0 && (
                                    <span className="toggle-help">配置符合要求</span>
                                )}
                                {(lib.suggestions || []).map((s) => (
                                    <span className="toggle-help" key={`${lib.id}-${s.field}`}>
                                        {s.severity === 'fail' ? '必须改' : '建议改'}：{s.field} 当前
                                        {JSON.stringify(s.current)} → 建议 {JSON.stringify(s.recommended)}（{s.reason}）
                                    </span>
                                ))}
                            </div>
                        ))}
                    </div>
                </div>
            )}
        </div>
    );
}
