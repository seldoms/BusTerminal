import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
    Rocket, Database, HardDrive, Server, Filter,
    CheckCircle2, XCircle, Loader2, Save, PlayCircle, AlertTriangle, Eye, Sparkles
} from 'lucide-react';
import { onboardingApi, getApiErrorMessage, logSafeError } from '../api';
import { useToast } from '../components/Toast';
import {
    CAPABILITY_DOMAINS,
    STEP_CAPABILITIES,
    STEP_STEP_HINTS,
} from './onboardingCapabilities';
import './OnboardingPage.css';

const STEPS = [
    { id: 'overview', label: '能做什么', icon: Sparkles },
    { id: 'services', label: '接口与授权', icon: Server },
    { id: 'download', label: '下载习惯', icon: HardDrive },
    { id: 'filter', label: '屏蔽关键词', icon: Filter },
    { id: 'cache', label: '缓存策略', icon: Database },
    { id: 'database', label: '数据库接入', icon: Database },
    { id: 'jellyfin', label: '自动刮削补齐', icon: PlayCircle },
];

const TRANSLATION_PROVIDERS = [
    { value: 'google_free', label: 'Google 免费翻译（无需授权）' },
    { value: 'ollama_local', label: '本地大模型 Ollama（推荐，无额度限制）' },
    { value: 'baidu_dev', label: '百度翻译开放平台（需 AppID/Secret）' },
];

const RESOLUTIONS = ['1080P', '4K', '720P'];

const EMPTY_FORM = {
    // qBittorrent（嵌套结构，后端按 qb_config 存）
    qb_config: { host: '', port: 8080, username: '', password: '' },
    // Jellyfin
    jellyfin_host: '',
    jellyfin_api_key: '',
    jellyfin_user_id: '',
    jellyfin_library_name: '',
    // amane
    amane_base_url: '',
    amane_token: '',
    amane_library_id: '',
    amane_scan_path: '',
    // 翻译
    translation_provider: 'google_free',
    baidu_app_id: '',
    baidu_secret: '',
    ollama_base_url: '',
    ollama_model: '',
    system_proxy: '',
    // 下载习惯
    disk_threshold_gb: 800,
    seeding_time_limit_min: 1440,
    delivery_folder: '',
    auto_download_daily_limit: 3,
    preferred_resolution: '1080P',
    auto_download_enabled: false,
    auto_subscribe_enabled: false,
    jable_hot_auto_download: false,
    automation_mode: 'suggest',
    // 屏蔽词（逗号/换行分隔）
    block_keywords_text: '',
    // 缓存
    cache_size_limit_gb: 10,
    movie_detail_cache_ttl_hours: 6,
    discovery_cache_ttl_hours: 6,
    // 入库闭环
    amane_ingest_enabled: true,
    amane_organize_enabled: true,
    amane_ingest_verify_visible: true,
    amane_dedupe_enabled: true,
    // 数据库
    expected_database_url: '',
};

function Field({ label, hint, children, required }) {
    return (
        <div className="ob-field">
            <label className="ob-label">
                {label}
                {required && <span className="ob-required">*</span>}
            </label>
            {children}
            {hint && <div className="ob-hint">{hint}</div>}
        </div>
    );
}

function ToggleRow({ label, help, checked, onChange }) {
    return (
        <div className="ob-toggle-row">
            <div className="ob-toggle-text">
                <span className="ob-toggle-label">{label}</span>
                {help && <span className="ob-toggle-help">{help}</span>}
            </div>
            <label className="ob-switch">
                <input
                    type="checkbox"
                    checked={!!checked}
                    onChange={(e) => onChange(e.target.checked)}
                />
                <span className="ob-switch-slider"></span>
            </label>
        </div>
    );
}

/** 连通性测试按钮 + 结果展示 */
function ProbeButton({ target, label, payload }) {
    const [state, setState] = useState(null);
    const [busy, setBusy] = useState(false);

    const run = async () => {
        setBusy(true);
        setState(null);
        try {
            const res = await onboardingApi.testTarget(target, payload || {});
            setState(res?.data || { ok: false, message: 'invalid_response' });
        } catch (err) {
            logSafeError(`Onboarding probe ${target} failed`, err);
            setState({ ok: false, message: getApiErrorMessage(err, '请求失败') });
        } finally {
            setBusy(false);
        }
    };

    return (
        <div className="ob-probe">
            <button type="button" className="btn btn-secondary ob-probe-btn" onClick={run} disabled={busy}>
                {busy ? <Loader2 className="spin" size={14} /> : <PlayCircle size={14} />}
                <span>{busy ? '测试中...' : label}</span>
            </button>
            {state && (
                <span className={`ob-probe-result ${state.ok ? 'ok' : 'fail'}`}>
                    {state.ok ? <CheckCircle2 size={14} /> : <XCircle size={14} />}
                    <span>
                        {state.ok
                            ? `可用${state.version ? ` · v${state.version}` : ''}${state.server_name ? ` · ${state.server_name}` : ''}${state.model_present === false ? ' · 但填的模型未安装' : ''}`
                            : (state.message || '连接失败')}
                    </span>
                </span>
            )}
        </div>
    );
}

/**
 * 能力提示条：说明「这一步填的东西能换来什么」，以及是否已经就绪。
 * `requires` 为空表示不依赖任何配置（如屏蔽词、缓存参数），恒为就绪。
 */
function CapabilityList({ stepId, form, configured }) {
    const capability = STEP_CAPABILITIES[stepId];
    if (!capability) return null;

    const hasValue = (key) => {
        if (key === 'qb_host') return !!String(form.qb_config?.host || '').trim();
        if (key === 'qb_username') return !!String(form.qb_config?.username || '').trim();
        // 密钥类不回填明文，所以「已填」= 本次输入非空 **或** 后端说之前已配置过
        if (key === 'amane_token') {
            return !!String(form.amane_token || '').trim() || !!configured.amane_token;
        }
        return !!String(form[key] || '').trim() || !!configured[key];
    };

    return (
        <div className="ob-capabilities">
            <div className="ob-cap-headline">{capability.headline}</div>
            <ul className="ob-cap-list">
                {capability.points.map((point) => {
                    const missing = point.requires.filter((key) => !hasValue(key));
                    const ready = missing.length === 0;
                    return (
                        <li key={point.title} className={`ob-cap-item ${ready ? 'ready' : 'pending'}`}>
                            <div className="ob-cap-item-head">
                                <span className="ob-cap-item-title">{point.title}</span>
                                {point.requires.length > 0 && (
                                    <span className={`ob-cap-badge ${ready ? 'ready' : 'pending'}`}>
                                        {ready ? '已就绪' : '待配置'}
                                    </span>
                                )}
                            </div>
                            <div className="ob-cap-item-detail">{point.detail}</div>
                        </li>
                    );
                })}
            </ul>
            {capability.footnote && <div className="ob-cap-footnote">{capability.footnote}</div>}
            {STEP_STEP_HINTS[stepId] && <div className="ob-step-hint">{STEP_STEP_HINTS[stepId]}</div>}
        </div>
    );
}

/** 首步：系统能力总览 */
function CapabilityOverview() {
    return (
        <section className="ob-section">
            <div className="ob-alert info">
                <Sparkles size={16} />
                <span>
                    BusTerminal 是一套「<strong>发现 → 下载 → 入库 → 播放</strong>」的自动化媒体栈。
                    下面几步配置的目的，就是把这条链路上的各个组件接上。
                </span>
            </div>
            <div className="ob-overview-grid">
                {CAPABILITY_DOMAINS.map((domain) => (
                    <div key={domain.id} className="ob-overview-card">
                        <div className="ob-overview-title">{domain.title}</div>
                        <div className="ob-overview-summary">{domain.summary}</div>
                    </div>
                ))}
            </div>
            <div className="ob-alert warn">
                <AlertTriangle size={16} />
                <span>
                    <strong>分工说明：</strong>BusTerminal 只做决策与编排，
                    下载交给 qBittorrent，落盘整理交给 amane，呈现播放交给 Jellyfin ——
                    这样任何一个组件换掉，都不会牵连其余环节。
                </span>
            </div>
        </section>
    );
}

export default function OnboardingPage() {
    const navigate = useNavigate();
    const { showToast } = useToast();

    const [stepIndex, setStepIndex] = useState(0);
    const [form, setForm] = useState(EMPTY_FORM);
    // 后端不回填密钥明文，只给 *_configured —— 用于「就绪」徽标判断已配置过
    const [configured, setConfigured] = useState({});
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [loadError, setLoadError] = useState('');
    const [alreadyDone, setAlreadyDone] = useState(false);

    // 屏蔽词试算
    const [preview, setPreview] = useState(null);
    const [previewing, setPreviewing] = useState(false);

    // 数据库体检
    const [dbCheck, setDbCheck] = useState(null);
    const [dbChecking, setDbChecking] = useState(false);

    const setField = useCallback((key, value) => {
        setForm((prev) => ({ ...prev, [key]: value }));
    }, []);

    const setQbField = useCallback((key, value) => {
        setForm((prev) => ({ ...prev, qb_config: { ...prev.qb_config, [key]: value } }));
    }, []);

    // ---- 载入已填配置 ----
    useEffect(() => {
        let cancelled = false;
        // 引导页需要回填表单，所以显式要完整设置
        onboardingApi.getStatus(true)
            .then((res) => {
                if (cancelled) return;
                const data = res?.data || {};
                setAlreadyDone(!!data.completed);
                const settings = data.settings || {};
                setConfigured({
                    amane_token: !!settings.amane_token_configured,
                    jellyfin_api_key: !!settings.jellyfin_api_key_configured,
                });
                setForm((prev) => ({
                    ...prev,
                    ...settings,
                    qb_config: {
                        host: settings.qb_config?.host || '',
                        port: settings.qb_config?.port || 8080,
                        username: settings.qb_config?.username || '',
                        // 后端不回传密码明文，留空表示保留原密码
                        password: '',
                    },
                    block_keywords_text: Array.isArray(settings.block_keywords)
                        ? settings.block_keywords.join('\n')
                        : '',
                    expected_database_url: settings.onboarding_database_url || '',
                    // 密钥类不回填明文，密码框一律留空
                    jellyfin_api_key: '',
                    baidu_secret: '',
                    amane_token: '',
                    system_proxy: '',
                }));
                if (settings.onboarding_database_url) setDbCheck(null);
            })
            .catch((err) => {
                if (cancelled) return;
                logSafeError('Onboarding status failed', err);
                setLoadError(getApiErrorMessage(err, '无法读取引导状态'));
            })
            .finally(() => { if (!cancelled) setLoading(false); });
        return () => { cancelled = true; };
    }, []);

    const handleProbeDb = useCallback(async () => {
        setDbChecking(true);
        try {
            const res = await onboardingApi.checkDatabase(form.expected_database_url || '');
            setDbCheck(res?.data || null);
        } catch (err) {
            logSafeError('Onboarding database check failed', err);
            showToast(getApiErrorMessage(err, '数据库体检失败'), 'error');
        } finally {
            setDbChecking(false);
        }
    }, [form.expected_database_url, showToast]);

    const handlePreviewKeywords = useCallback(async () => {
        setPreviewing(true);
        setPreview(null);
        try {
            const res = await onboardingApi.previewBlockKeywords(form.block_keywords_text || '', 20);
            setPreview(res?.data || { keywords: [], items: [] });
        } catch (err) {
            logSafeError('Onboarding keyword preview failed', err);
            showToast(getApiErrorMessage(err, '试算失败'), 'error');
        } finally {
            setPreviewing(false);
        }
    }, [form.block_keywords_text, showToast]);

    const handleSave = useCallback(async (complete = true) => {
        setSaving(true);
        try {
            const payload = {
                qb_config: {
                    host: form.qb_config.host,
                    port: Number(form.qb_config.port) || 8080,
                    username: form.qb_config.username,
                    password: form.qb_config.password,
                },
                jellyfin_host: form.jellyfin_host,
                jellyfin_api_key: form.jellyfin_api_key,
                jellyfin_user_id: form.jellyfin_user_id,
                jellyfin_library_name: form.jellyfin_library_name,
                amane_base_url: form.amane_base_url,
                amane_token: form.amane_token,
                amane_library_id: form.amane_library_id,
                amane_scan_path: form.amane_scan_path,
                translation_provider: form.translation_provider,
                baidu_app_id: form.baidu_app_id,
                baidu_secret: form.baidu_secret,
                ollama_base_url: form.ollama_base_url,
                ollama_model: form.ollama_model,
                system_proxy: form.system_proxy,
                disk_threshold_gb: Number(form.disk_threshold_gb),
                seeding_time_limit_min: Number(form.seeding_time_limit_min),
                delivery_folder: form.delivery_folder,
                auto_download_daily_limit: Number(form.auto_download_daily_limit),
                preferred_resolution: form.preferred_resolution,
                auto_download_enabled: !!form.auto_download_enabled,
                auto_subscribe_enabled: !!form.auto_subscribe_enabled,
                jable_hot_auto_download: !!form.jable_hot_auto_download,
                automation_mode: form.automation_mode,
                block_keywords: form.block_keywords_text,
                cache_size_limit_gb: Number(form.cache_size_limit_gb),
                movie_detail_cache_ttl_hours: Number(form.movie_detail_cache_ttl_hours),
                discovery_cache_ttl_hours: Number(form.discovery_cache_ttl_hours),
                amane_ingest_enabled: !!form.amane_ingest_enabled,
                amane_organize_enabled: !!form.amane_organize_enabled,
                amane_ingest_verify_visible: !!form.amane_ingest_verify_visible,
                amane_dedupe_enabled: !!form.amane_dedupe_enabled,
                onboarding_database_url: form.expected_database_url,
                completed: complete,
            };
            const res = await onboardingApi.apply(payload);
            if (!res?.success) {
                const errors = res?.errors || [];
                showToast(`保存失败：${errors.join(', ') || '参数不合法'}`, 'error');
                return;
            }
            showToast(complete ? '引导已完成，正在进入系统' : '配置已保存', 'success');
            if (complete) {
                navigate('/', { replace: true });
                window.location.reload();
            }
        } catch (err) {
            logSafeError('Onboarding apply failed', err);
            showToast(getApiErrorMessage(err, '保存失败'), 'error');
        } finally {
            setSaving(false);
        }
    }, [form, navigate, showToast]);

    const keywordList = useMemo(
        () => String(form.block_keywords_text || '')
            .split(/[\r\n,，;；、|]+/)
            .map((item) => item.trim())
            .filter(Boolean),
        [form.block_keywords_text]
    );

    if (loading) {
        return (
            <div className="ob-page">
                <div className="ob-loading">
                    <Loader2 className="spin" size={32} />
                    <span>正在读取部署状态...</span>
                </div>
            </div>
        );
    }

    const currentStep = STEPS[stepIndex];

    return (
        <div className="ob-page">
            <header className="ob-header">
                <div className="ob-brand">
                    <Rocket size={26} />
                    <div>
                        <h1>BusTerminal 初始化向导</h1>
                        <p>先看看这套系统能做什么，再把基础配置填完。每一步都可以先测试连通性再保存。</p>
                    </div>
                </div>
                {alreadyDone && (
                    <div className="ob-reentry-badge">
                        <CheckCircle2 size={15} />
                        <span>该部署已完成过引导，这是重新配置</span>
                    </div>
                )}
            </header>

            {loadError && (
                <div className="ob-alert error">
                    <AlertTriangle size={16} />
                    <span>{loadError}</span>
                </div>
            )}

            <nav className="ob-steps">
                {STEPS.map((step, index) => {
                    const Icon = step.icon;
                    return (
                        <button
                            key={step.id}
                            type="button"
                            className={`ob-step ${index === stepIndex ? 'active' : ''} ${index < stepIndex ? 'done' : ''}`}
                            onClick={() => setStepIndex(index)}
                        >
                            <Icon size={16} />
                            <span className="ob-step-label">{step.label}</span>
                            {index < stepIndex && <CheckCircle2 size={14} className="ob-step-check" />}
                        </button>
                    );
                })}
            </nav>

            <main className="ob-panel">
                {/* 步骤名已在顶部步骤条里高亮显示，面板内不再重复一遍标题 */}
                <CapabilityList stepId={currentStep.id} form={form} configured={configured} />

                {/* ---------- 能做什么 ---------- */}
                {currentStep.id === 'overview' && <CapabilityOverview />}

                {/* ---------- 接口与授权 ---------- */}
                {currentStep.id === 'services' && (
                    <>
                        <section className="ob-section">
                            <h3>qBittorrent（下载客户端）</h3>
                            <div className="ob-grid-3">
                                <Field label="地址" hint="可填主机名或完整 URL">
                                    <input
                                        className="ob-input"
                                        value={form.qb_config.host}
                                        onChange={(e) => setQbField('host', e.target.value)}
                                        placeholder="192.168.1.100"
                                    />
                                </Field>
                                <Field label="端口">
                                    <input
                                        className="ob-input"
                                        type="number"
                                        value={form.qb_config.port}
                                        onChange={(e) => setQbField('port', e.target.value)}
                                    />
                                </Field>
                                <Field label="用户名">
                                    <input
                                        className="ob-input"
                                        value={form.qb_config.username}
                                        onChange={(e) => setQbField('username', e.target.value)}
                                    />
                                </Field>
                            </div>
                            <Field label="密码" hint="留空表示保持原密码不变">
                                <input
                                    className="ob-input"
                                    type="password"
                                    value={form.qb_config.password}
                                    onChange={(e) => setQbField('password', e.target.value)}
                                    placeholder="输入密码"
                                />
                            </Field>
                            <ProbeButton target="qb" label="测试 qB 连接" />
                        </section>

                        <section className="ob-section">
                            <h3>Jellyfin（媒体库）</h3>
                            <div className="ob-grid-2">
                                <Field label="服务地址">
                                    <input
                                        className="ob-input"
                                        value={form.jellyfin_host}
                                        onChange={(e) => setField('jellyfin_host', e.target.value)}
                                        placeholder="http://192.168.1.100:8096"
                                    />
                                </Field>
                                <Field label="用户 ID" hint="通常可留空，系统会自动探测">
                                    <input
                                        className="ob-input"
                                        value={form.jellyfin_user_id || ''}
                                        onChange={(e) => setField('jellyfin_user_id', e.target.value)}
                                    />
                                </Field>
                            </div>
                            <Field label="API Key" hint="留空表示保持原 Key 不变">
                                <input
                                    className="ob-input"
                                    type="password"
                                    value={form.jellyfin_api_key}
                                    onChange={(e) => setField('jellyfin_api_key', e.target.value)}
                                />
                            </Field>
                            <ProbeButton target="jellyfin" label="测试 Jellyfin 连接" />
                        </section>

                        <section className="ob-section">
                            <h3>amane（媒体库执行层，负责整理与刮削）</h3>
                            <div className="ob-grid-2">
                                <Field label="服务地址">
                                    <input
                                        className="ob-input"
                                        value={form.amane_base_url}
                                        onChange={(e) => setField('amane_base_url', e.target.value)}
                                        placeholder="http://192.168.1.100:8000"
                                    />
                                </Field>
                                <Field label="访问令牌" hint="留空表示保持原令牌不变">
                                    <input
                                        className="ob-input"
                                        type="password"
                                        value={form.amane_token}
                                        onChange={(e) => setField('amane_token', e.target.value)}
                                    />
                                </Field>
                            </div>
                            <ProbeButton target="amane" label="测试 amane 连接" />
                        </section>

                        <section className="ob-section">
                            <h3>翻译服务</h3>
                            <Field label="翻译来源">
                                <select
                                    className="ob-input"
                                    value={form.translation_provider}
                                    onChange={(e) => setField('translation_provider', e.target.value)}
                                >
                                    {TRANSLATION_PROVIDERS.map((p) => (
                                        <option key={p.value} value={p.value}>{p.label}</option>
                                    ))}
                                </select>
                            </Field>

                            {form.translation_provider === 'ollama_local' && (
                                <div className="ob-grid-2">
                                    <Field label="Ollama 地址">
                                        <input
                                            className="ob-input"
                                            value={form.ollama_base_url || ''}
                                            onChange={(e) => setField('ollama_base_url', e.target.value)}
                                            placeholder="http://127.0.0.1:11434"
                                        />
                                    </Field>
                                    <Field label="模型名称" hint="会检查该模型是否已安装">
                                        <input
                                            className="ob-input"
                                            value={form.ollama_model || ''}
                                            onChange={(e) => setField('ollama_model', e.target.value)}
                                            placeholder="qwen3.8:27b"
                                        />
                                    </Field>
                                </div>
                            )}

                            {form.translation_provider === 'baidu_dev' && (
                                <div className="ob-grid-2">
                                    <Field label="App ID">
                                        <input
                                            className="ob-input"
                                            value={form.baidu_app_id || ''}
                                            onChange={(e) => setField('baidu_app_id', e.target.value)}
                                        />
                                    </Field>
                                    <Field label="Secret" hint="留空表示保持原 Secret 不变">
                                        <input
                                            className="ob-input"
                                            type="password"
                                            value={form.baidu_secret}
                                            onChange={(e) => setField('baidu_secret', e.target.value)}
                                        />
                                    </Field>
                                </div>
                            )}

                            <ProbeButton
                                target="translation"
                                label="测试翻译服务"
                                payload={{
                                    provider: form.translation_provider,
                                    base_url: form.ollama_base_url,
                                    model: form.ollama_model,
                                    baidu_app_id: form.baidu_app_id,
                                    baidu_secret: form.baidu_secret,
                                }}
                            />
                        </section>

                        <section className="ob-section">
                            <h3>出网代理（可选）</h3>
                            <Field label="代理地址" hint="留空表示直连；仅影响外网抓取，不影响内网服务">
                                <input
                                    className="ob-input"
                                    value={form.system_proxy}
                                    onChange={(e) => setField('system_proxy', e.target.value)}
                                    placeholder="http://127.0.0.1:7890"
                                />
                            </Field>
                        </section>
                    </>
                )}

                {/* ---------- 下载习惯 ---------- */}
                {currentStep.id === 'download' && (
                    <>
                        <section className="ob-section">
                            <h3>磁盘与做种</h3>
                            <div className="ob-grid-2">
                                <Field label="磁盘剩余熔断阈值 (GB)" hint="剩余空间低于此值时暂停新下载">
                                    <input
                                        className="ob-input"
                                        type="number"
                                        value={form.disk_threshold_gb}
                                        onChange={(e) => setField('disk_threshold_gb', e.target.value)}
                                    />
                                </Field>
                                <Field label="最长做种时间 (分钟)" hint="1440 = 24 小时；0 = 不限制">
                                    <input
                                        className="ob-input"
                                        type="number"
                                        value={form.seeding_time_limit_min}
                                        onChange={(e) => setField('seeding_time_limit_min', e.target.value)}
                                    />
                                </Field>
                            </div>
                            <Field label="完成投放路径" hint="qB 下载完成后把文件投递到这个目录">
                                <input
                                    className="ob-input"
                                    value={form.delivery_folder || ''}
                                    onChange={(e) => setField('delivery_folder', e.target.value)}
                                    placeholder="/downloads/staging"
                                />
                            </Field>
                        </section>

                        <section className="ob-section">
                            <h3>下载偏好</h3>
                            <div className="ob-grid-2">
                                <Field label="磁力清晰度偏好">
                                    <select
                                        className="ob-input"
                                        value={form.preferred_resolution}
                                        onChange={(e) => setField('preferred_resolution', e.target.value)}
                                    >
                                        {RESOLUTIONS.map((r) => <option key={r} value={r}>{r}</option>)}
                                    </select>
                                </Field>
                                <Field label="自动下载每日上限" hint="-1 表示不限制">
                                    <input
                                        className="ob-input"
                                        type="number"
                                        value={form.auto_download_daily_limit}
                                        onChange={(e) => setField('auto_download_daily_limit', e.target.value)}
                                    />
                                </Field>
                            </div>
                            <Field label="自动化模式" hint="自动化的激进度">
                                <select
                                    className="ob-input"
                                    value={form.automation_mode}
                                    onChange={(e) => setField('automation_mode', e.target.value)}
                                >
                                    <option value="off">关闭：只记录建议，不自动执行</option>
                                    <option value="suggest">建议：给出候选，人工确认</option>
                                    <option value="auto_approve_low_risk">自动放行低风险项</option>
                                </select>
                            </Field>
                            <ToggleRow
                                label="订阅命中后自动下载"
                                help="关闭后订阅只提示不下载"
                                checked={form.auto_download_enabled}
                                onChange={(v) => setField('auto_download_enabled', v)}
                            />
                            <ToggleRow
                                label="自动新增订阅"
                                help="按观看行为自动订阅演员"
                                checked={form.auto_subscribe_enabled}
                                onChange={(v) => setField('auto_subscribe_enabled', v)}
                            />
                            <ToggleRow
                                label="热门榜单自动下载"
                                help="按 Jable 热门榜自动推送种子"
                                checked={form.jable_hot_auto_download}
                                onChange={(v) => setField('jable_hot_auto_download', v)}
                            />
                        </section>
                    </>
                )}

                {/* ---------- 屏蔽关键词 ---------- */}
                {currentStep.id === 'filter' && (
                    <section className="ob-section">
                        <h3>全链路屏蔽关键词</h3>
                        <div className="ob-alert info">
                            <AlertTriangle size={16} />
                            <span>
                                匹配会忽略大小写与分隔符，所以填 <code>vr</code> 能同时挡住
                                <code>VR</code>、<code>ＶＲ</code>、<code>VR-</code>；
                                番号里带 VR 的片子同样会被挡住。
                            </span>
                        </div>
                        <Field label="关键词" hint="每行一个，也可用逗号分隔。作用于标题、番号与演员名">
                            <textarea
                                className="ob-textarea"
                                rows={6}
                                value={form.block_keywords_text}
                                onChange={(e) => setField('block_keywords_text', e.target.value)}
                                placeholder={'vr\n4k-iso\nuncensored-hack'}
                            />
                        </Field>
                        {keywordList.length > 0 && (
                            <div className="ob-chips">
                                {keywordList.map((word) => (
                                    <span key={word} className="ob-chip">{word}</span>
                                ))}
                            </div>
                        )}
                        <div className="ob-inline-actions">
                            <button
                                type="button"
                                className="btn btn-secondary"
                                onClick={handlePreviewKeywords}
                                disabled={previewing || keywordList.length === 0}
                            >
                                {previewing ? <Loader2 className="spin" size={14} /> : <Eye size={14} />}
                                <span>{previewing ? '试算中...' : '试算：看看会挡住什么'}</span>
                            </button>
                        </div>
                        {preview && (
                            <div className="ob-preview">
                                {preview.items?.length ? (
                                    <>
                                        <div className="ob-preview-title">
                                            本地库中已命中 {preview.items.length} 条（最多展示 20 条）：
                                        </div>
                                        <ul className="ob-preview-list">
                                            {preview.items.map((item) => (
                                                <li key={item.id}>
                                                    <span className="ob-preview-title-text">{item.title || item.id}</span>
                                                    <span className="ob-chip small">{item.keyword}</span>
                                                </li>
                                            ))}
                                        </ul>
                                    </>
                                ) : (
                                    <div className="ob-preview-title">本地库中暂未命中任何条目。</div>
                                )}
                            </div>
                        )}
                    </section>
                )}

                {/* ---------- 缓存策略 ---------- */}
                {currentStep.id === 'cache' && (
                    <section className="ob-section">
                        <h3>缓存能用多久 / 占多少空间</h3>
                        <div className="ob-grid-2">
                            <Field label="磁盘缓存上限 (GB)" hint="超过后按最近最少使用清理">
                                <input
                                    className="ob-input"
                                    type="number"
                                    value={form.cache_size_limit_gb}
                                    onChange={(e) => setField('cache_size_limit_gb', e.target.value)}
                                />
                            </Field>
                            <Field label="详情缓存有效期 (小时)" hint="超时的影片详情会重新抓取">
                                <input
                                    className="ob-input"
                                    type="number"
                                    value={form.movie_detail_cache_ttl_hours}
                                    onChange={(e) => setField('movie_detail_cache_ttl_hours', e.target.value)}
                                />
                            </Field>
                            <Field label="发现列表缓存有效期 (小时)">
                                <input
                                    className="ob-input"
                                    type="number"
                                    value={form.discovery_cache_ttl_hours}
                                    onChange={(e) => setField('discovery_cache_ttl_hours', e.target.value)}
                                />
                            </Field>
                        </div>
                    </section>
                )}

                {/* ---------- 数据库接入 ---------- */}
                {currentStep.id === 'database' && (
                    <section className="ob-section">
                        <h3>是否接入现有数据库</h3>
                        <div className="ob-alert warn">
                            <AlertTriangle size={16} />
                            <span>
                                连接串由环境变量 <code>DATABASE_URL</code> 决定，<strong>进程启动时读取，运行期无法切换</strong>。
                                这里可以填写你期望接入的库并做比对；若不一致，需要改环境变量后重启容器。
                            </span>
                        </div>
                        <Field label="期望接入的数据库连接串" hint="填了会与当前实际生效的连接做比对（密码不会回显）">
                            <input
                                className="ob-input"
                                value={form.expected_database_url}
                                onChange={(e) => setField('expected_database_url', e.target.value)}
                                placeholder="postgresql://user:password@host:5433/busterminal"
                            />
                        </Field>
                        <div className="ob-inline-actions">
                            <button
                                type="button"
                                className="btn btn-secondary"
                                onClick={handleProbeDb}
                                disabled={dbChecking}
                            >
                                {dbChecking ? <Loader2 className="spin" size={14} /> : <Database size={14} />}
                                <span>{dbChecking ? '体检中...' : '体检当前数据库'}</span>
                            </button>
                        </div>
                        {dbCheck && (
                            <div className="ob-dbcheck">
                                <div className="ob-dbcheck-row">
                                    <span className="ob-dbcheck-label">当前生效连接</span>
                                    <code>{dbCheck.active_url_masked}</code>
                                </div>
                                <div className="ob-dbcheck-row">
                                    <span className="ob-dbcheck-label">数据库类型</span>
                                    <span>{dbCheck.dialect === 'postgresql' ? 'PostgreSQL' : 'SQLite'}</span>
                                </div>
                                <div className="ob-dbcheck-row">
                                    <span className="ob-dbcheck-label">与期望是否一致</span>
                                    <span className={dbCheck.matches_expected ? 'ok' : 'fail'}>
                                        {dbCheck.matches_expected ? '一致' : '不一致'}
                                    </span>
                                </div>
                                {Object.keys(dbCheck.counts || {}).length > 0 && (
                                    <div className="ob-dbcheck-counts">
                                        {Object.entries(dbCheck.counts).map(([table, count]) => (
                                            <div key={table} className="ob-dbcheck-count">
                                                <span>{table}</span>
                                                <strong>{Number(count).toLocaleString()}</strong>
                                            </div>
                                        ))}
                                    </div>
                                )}
                                {dbCheck.hint && (
                                    <div className="ob-alert warn">{dbCheck.hint}</div>
                                )}
                                {(dbCheck.errors || []).length > 0 && (
                                    <div className="ob-alert error">
                                        <span>部分表统计失败：{dbCheck.errors.join('; ')}</span>
                                    </div>
                                )}
                            </div>
                        )}
                    </section>
                )}

                {/* ---------- Jellyfin 自动刮削补齐 ---------- */}
                {currentStep.id === 'jellyfin' && (
                    <section className="ob-section">
                        <h3>下载完成后的入库闭环</h3>
                        <div className="ob-alert info">
                            <span>
                                这一段由 <code>amane</code> 执行。开启后系统会<strong>实际写文件、改名、移动目录</strong>，
                                建议先留一两个测试任务确认链路无误再全开。
                            </span>
                        </div>
                        <ToggleRow
                            label="启用入库闭环"
                            help="总开关。关闭后下载完成不会自动入库"
                            checked={form.amane_ingest_enabled}
                            onChange={(v) => setField('amane_ingest_enabled', v)}
                        />
                        <ToggleRow
                            label="自动刮削与整理"
                            help="补番号、抓元数据、按 工作室/番号 归档"
                            checked={form.amane_organize_enabled}
                            onChange={(v) => setField('amane_organize_enabled', v)}
                        />
                        <ToggleRow
                            label="入库后校验可见性"
                            help="等 Jellyfin 里真的能看到这部片才算成功"
                            checked={form.amane_ingest_verify_visible}
                            onChange={(v) => setField('amane_ingest_verify_visible', v)}
                        />
                        <ToggleRow
                            label="入库去重"
                            help="避免重复文件反复入库"
                            checked={form.amane_dedupe_enabled}
                            onChange={(v) => setField('amane_dedupe_enabled', v)}
                        />
                        <div className="ob-grid-2">
                            <Field label="目标库 ID" hint="留空则自动选择 watch 模式的库">
                                <input
                                    className="ob-input"
                                    value={form.amane_library_id || ''}
                                    onChange={(e) => setField('amane_library_id', e.target.value)}
                                />
                            </Field>
                            <Field label="扫描子目录" hint="相对库根，如 _incoming；留空用库根">
                                <input
                                    className="ob-input"
                                    value={form.amane_scan_path || ''}
                                    onChange={(e) => setField('amane_scan_path', e.target.value)}
                                    placeholder="_incoming"
                                />
                            </Field>
                        </div>
                        <div className="ob-alert info">
                            <AlertTriangle size={16} />
                            <span>
                                <strong>请先在 Jellyfin 后台建好一个专用媒体库</strong>（系统不会替你建）：
                                内容类型必须是<strong>电影</strong>，路径指向一个专用空目录。
                                不要把这些内容混进你现有的分类库 —— 入库时系统会按「工作室/番号」重排目录，
                                混进已有分类会打乱你原有的组织。已有分类留给手动管理的内容更合适。
                            </span>
                        </div>
                        <Field
                            label="Jellyfin 媒体库名称"
                            hint="填你实际建的库名。库名含「黄片」二字时系统也能自动匹配"
                        >
                            <input
                                className="ob-input"
                                value={form.jellyfin_library_name || ''}
                                onChange={(e) => setField('jellyfin_library_name', e.target.value)}
                                placeholder="黄片"
                            />
                        </Field>
                    </section>
                )}
            </main>

            <footer className="ob-footer">
                <button
                    type="button"
                    className="btn btn-secondary"
                    onClick={() => setStepIndex((i) => Math.max(0, i - 1))}
                    disabled={stepIndex === 0 || saving}
                >
                    上一步
                </button>
                <div className="ob-footer-right">
                    <button
                        type="button"
                        className="btn btn-secondary"
                        onClick={() => handleSave(false)}
                        disabled={saving}
                    >
                        <Save size={15} />
                        <span>{saving ? '保存中...' : '仅保存'}</span>
                    </button>
                    {stepIndex < STEPS.length - 1 ? (
                        <button
                            type="button"
                            className="btn btn-primary"
                            onClick={() => setStepIndex((i) => Math.min(STEPS.length - 1, i + 1))}
                            disabled={saving}
                        >
                            下一步
                        </button>
                    ) : (
                        <button
                            type="button"
                            className="btn btn-primary"
                            onClick={() => handleSave(true)}
                            disabled={saving}
                        >
                            <CheckCircle2 size={15} />
                            <span>{saving ? '提交中...' : '完成并进入系统'}</span>
                        </button>
                    )}
                </div>
            </footer>
        </div>
    );
}