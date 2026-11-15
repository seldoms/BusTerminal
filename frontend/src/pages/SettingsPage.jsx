import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import {
    Sliders,
    Database,
    HardDrive,
    Globe,
    Languages,
    Server,
    Bell,
    Save,
    RefreshCw,
    Layers,
    Ban,
    Clock,
    RotateCcw,
    ListChecks
} from 'lucide-react';
import './SettingsPage.css';
import './SettingsPage.unslop.css';
import { useLayoutSettings } from '../layoutSettings';
import { useToast } from '../components/Toast';
import {
    getSystemSettings,
    saveSystemSettings,
    getQBConfig,
    saveQBConfig,
    testQBConnection,
    getCacheStats,
    cleanCache,
    invalidateCache,
    warmupCache
} from '../api';

// Tab 子组件
import SettingsLayoutTab from './settings/SettingsLayoutTab';
import SettingsCacheTab from './settings/SettingsCacheTab';
import SettingsEngineTab from './settings/SettingsEngineTab';
import SettingsNetworkTab from './settings/SettingsNetworkTab';
import SettingsTranslateTab from './settings/SettingsTranslateTab';
import SettingsJellyfinTab from './settings/SettingsJellyfinTab';
import SettingsNotifyTab from './settings/SettingsNotifyTab';
import SettingsMediaStackTab from './settings/SettingsMediaStackTab';
import SettingsBlockKeywordsTab from './settings/SettingsBlockKeywordsTab';
import SettingsScheduleTab from './settings/SettingsScheduleTab';

const CATEGORIES = [
    { id: 'layout', label: '界面与布局', icon: Sliders, desc: '卡片尺寸与页面宽度联动' },
    { id: 'cache', label: '缓存与性能', icon: Database, desc: '磁盘已用与图表数据清理' },
    { id: 'schedule', label: '抓取节奏', icon: Clock, desc: '首页/榜单、订阅、字幕、元数据的频率' },
    { id: 'engine', label: '下载客户端', icon: HardDrive, desc: 'qBittorrent 参数与限速' },
    { id: 'network', label: '网络与代理', icon: Globe, desc: '节点代理与自动域名轮询' },
    { id: 'block', label: '关键词屏蔽', icon: Ban, desc: '全局屏蔽词：展示与下载双链路拦截' },
    { id: 'translate', label: '多语言翻译', icon: Languages, desc: '百度/Google 翻译服务' },
    { id: 'integrate', label: 'Jellyfin 媒体', icon: Server, desc: '媒体库自动同步与清理' },
    { id: 'notify', label: '消息与通知', icon: Bell, desc: 'SMTP 邮件提醒设置' },
    { id: 'mediastack', label: '媒体栈自检', icon: Layers, desc: 'amane 连通性与入库闭环参数' },
];

export default function SettingsPage() {
    const [activeTab, setActiveTab] = useState('layout');
    const [{ cardWidth }, setLayoutSettings] = useLayoutSettings();
    const { showToast } = useToast();
    const hubRef = useRef(null);

    // 系统配置：savedSysConfig 是「已落库」的基线，用来算真实未保存项数
    const [sysConfig, setSysConfig] = useState({});
    const [savedSysConfig, setSavedSysConfig] = useState({});
    const [loadingSys, setLoadingSys] = useState(true);
    const [savingSys, setSavingSys] = useState(false);
    const [showDirtyDetail, setShowDirtyDetail] = useState(false);

    const dirtyKeys = useMemo(() => {
        const keys = new Set([...Object.keys(savedSysConfig), ...Object.keys(sysConfig)]);
        return [...keys]
            .filter((key) => JSON.stringify(savedSysConfig[key]) !== JSON.stringify(sysConfig[key]))
            .sort();
    }, [savedSysConfig, sysConfig]);

    // qBittorrent 配置
    const [qbConfig, setQbConfig] = useState({ url: '', username: '', password: '' });
    const [savingQB, setSavingQB] = useState(false);
    const [testingQB, setTestingQB] = useState(false);
    const [qbStatus, setQbStatus] = useState(null);

    // 缓存统计
    const [cacheStats, setCacheStats] = useState(null);
    const [cacheLoading, setCacheLoading] = useState(false);
    const [cacheCleaning, setCacheCleaning] = useState(false);
    const [cacheInvalidationCategory, setCacheInvalidationCategory] = useState('remote_images');
    const [cacheInvalidating, setCacheInvalidating] = useState(false);
    const [cacheWarming, setCacheWarming] = useState(false);

    // 1. 加载系统配置
    const loadSystemSettings = useCallback(async () => {
        setLoadingSys(true);
        try {
            const data = await getSystemSettings();
            if (data) {
                setSysConfig(data);
                setSavedSysConfig(data);
            }
        } catch (err) {
            console.error('加载系统配置失败:', err);
            showToast('加载系统配置失败', 'error');
        } finally {
            setLoadingSys(false);
        }
    }, [showToast]);

    // 2. 加载 qB 配置
    const loadQBConfig = useCallback(async () => {
        try {
            const data = await getQBConfig();
            if (data) setQbConfig(data);
        } catch (err) {
            console.error('加载 qB 配置失败:', err);
        }
    }, []);

    // 3. 加载 Cache 统计
    const fetchCacheStatsData = useCallback(async () => {
        setCacheLoading(true);
        try {
            const data = await getCacheStats();
            if (data) setCacheStats(data);
        } catch (err) {
            console.error('获取缓存统计失败:', err);
        } finally {
            setCacheLoading(false);
        }
    }, []);

    useEffect(() => {
        loadSystemSettings();
        loadQBConfig();
        fetchCacheStatsData();
    }, [loadSystemSettings, loadQBConfig, fetchCacheStatsData]);

    // 说明文字只留在 tooltip：给 10 个 tab 里所有解释性文本补 title，
    // 视觉层再收成一行 / 两行。用 MutationObserver 覆盖 tab 内部异步渲染的说明。
    useEffect(() => {
        const root = hubRef.current;
        if (!root || typeof MutationObserver === 'undefined') return undefined;
        const selector = [
            '.control-help-text', '.card-subtitle', '.toggle-help', '.schedule-help',
            '.control-hint', '.slider-hint', '.schedule-footnote'
        ].join(',');
        const decorate = (el) => {
            if (!el) return;
            // React 重渲染会重建 className，所以已处理过的节点也要补回收敛类
            if (el.dataset.tipDone) {
                el.classList.add('has-tooltip');
                return;
            }
            el.dataset.tipDone = '1';
            const text = (el.textContent || '').trim();
            if (!text) return;
            if (!el.title) el.title = text;
            el.classList.add('has-tooltip');
        };
        const scan = () => root.querySelectorAll(selector).forEach(decorate);
        scan();
        const observer = new MutationObserver(scan);
        observer.observe(root, { childList: true, subtree: true, characterData: true });
        return () => observer.disconnect();
        // 加载态返回的是另一个 DOM 分支，loadingSys 翻转后 ref 才指向真正的台账容器
    }, [loadingSys]);

    // Handler: 修改系统 State
    const handleSysChange = (key, value) => {
        setSysConfig(prev => ({ ...prev, [key]: value }));
    };

    const handleDiscardSys = () => {
        setSysConfig({ ...savedSysConfig });
        setShowDirtyDetail(false);
        showToast('已放弃未保存的改动');
    };

    // Handler: 保存系统配置
    const handleSaveSys = async () => {
        setSavingSys(true);
        try {
            const res = await saveSystemSettings(sysConfig);
            if (res && res.success !== false) {
                showToast(dirtyKeys.length > 0 ? `已保存 ${dirtyKeys.length} 项设置` : '系统设置已保存', 'success');
                // 回读校验：后端可能做了归一化（如剥掉主机末尾斜杠），
                // 用回读值覆盖本地状态，界面上立刻就是真实生效值。
                const saved = res?.data;
                if (saved && typeof saved === 'object') {
                    setSysConfig(prev => ({ ...prev, ...saved }));
                    setSavedSysConfig(prev => ({ ...prev, ...sysConfig, ...saved }));
                } else {
                    setSavedSysConfig({ ...sysConfig });
                }
                setShowDirtyDetail(false);
                loadSystemSettings();
            } else {
                showToast(res?.message || '保存设置失败', 'error');
            }
        } catch (err) {
            showToast('保存请求异常', 'error');
        } finally {
            setSavingSys(false);
        }
    };

    // Handler: qB 改变与保存
    const handleQBChange = (key, value) => {
        setQbConfig(prev => ({ ...prev, [key]: value }));
    };

    const handleSaveQB = async (e) => {
        if (e) e.preventDefault();
        // 后端 QBConfig 要求 host + port + username 全部齐备，缺一个就是 422。
        // 提交前自己先拦一道，并把 422 的字段说明直接显示给用户，
        // 否则只弹一句「保存失败」，用户完全不知道该填什么。
        const host = String(qbConfig.host || '').trim();
        const port = Number(qbConfig.port) || 0;
        if (!host || !port) {
            showToast(!host ? '请填写服务器 API 地址' : '请填写端口', 'error');
            return;
        }
        setSavingQB(true);
        try {
            const payload = {
                host,
                port,
                username: String(qbConfig.username || 'admin').trim(),
                // 留空表示保留原密码，交给后端判定
                password: String(qbConfig.password || ''),
            };
            const res = await saveQBConfig(payload);
            if (res && res.success !== false) {
                showToast('qBittorrent 连接参数已保存', 'success');
                const saved = res?.data;
                if (saved && typeof saved === 'object') {
                    setQbConfig(prev => ({ ...prev, ...saved }));
                }
                loadQBConfig();
            } else {
                showToast(getApiErrorMessage(res, '保存 qB 配置失败'), 'error');
            }
        } catch (err) {
            logSafeError('Save QB config failed', err);
            // 422 的 detail 里是具体哪个字段不合法，比泛泛的「保存失败」有用得多
            const detail = err?.detail;
            const fieldMsg = Array.isArray(detail)
                ? detail.map(d => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join('；')
                : null;
            showToast(fieldMsg || getApiErrorMessage(err, '保存 qB 配置失败'), 'error');
        } finally {
            setSavingQB(false);
        }
    };

    const handleTestQB = async () => {
        setTestingQB(true);
        setQbStatus(null);
        try {
            // /api/qb/status 是 GET，用的是库里已保存的配置，不接收表单参数。
            const res = await testQBConnection();
            // 后端返回 {success, data:{version, server_state}}，
            // 早期前端按 res.ok / res.version 读，永远取不到 -> 一直显示「连接失败」。
            const ok = !!res?.success && res?.data?.version !== undefined;
            const status = ok
                ? { ok: true, version: res.data.version }
                : { ok: false, message: res?.message || res?.data?.message || '无法访问' };
            setQbStatus(status);
            if (ok) {
                showToast(`连接成功! Version: ${res.data.version || 'OK'}`, 'success');
            } else {
                showToast(`连接失败: ${status.message}`, 'error');
            }
        } catch (err) {
            logSafeError('Test QB connection failed', err);
            setQbStatus({ ok: false, message: getApiErrorMessage(err, '网络连接失败') });
            showToast('测试连接失败', 'error');
        } finally {
            setTestingQB(false);
        }
    };

    // Handler: Cache 各种清理动作
    const handleCacheCleanup = async () => {
        setCacheCleaning(true);
        try {
            const res = await cleanCache();
            if (res) {
                showToast('缓存清理整理成功', 'success');
                fetchCacheStatsData();
            }
        } catch (err) {
            showToast('缓存整理异常', 'error');
        } finally {
            setCacheCleaning(false);
        }
    };

    const handleCacheInvalidation = async () => {
        setCacheInvalidating(true);
        try {
            const res = await invalidateCache(cacheInvalidationCategory);
            if (res) {
                showToast(`分类 [${cacheInvalidationCategory}] 缓存已失效`, 'success');
                fetchCacheStatsData();
            }
        } catch (err) {
            showToast('操作失败', 'error');
        } finally {
            setCacheInvalidating(false);
        }
    };

    const handleCacheWarmup = async () => {
        setCacheWarming(true);
        try {
            const res = await warmupCache();
            if (res) {
                showToast('常用图片预热任务提交成功', 'success');
                fetchCacheStatsData();
            }
        } catch (err) {
            showToast('预热提交失败', 'error');
        } finally {
            setCacheWarming(false);
        }
    };

    if (loadingSys) {
        return (
            <div className="container settings-hub-container" style={{ textAlign: 'center', paddingTop: 60 }}>
                <RefreshCw className="spin" size={20} style={{ color: 'var(--muted)', marginBottom: 12 }} />
                <p style={{ color: 'var(--text-secondary)' }}>正在读取配置台账…</p>
            </div>
        );
    }

    return (
        <div className="container settings-hub-container" ref={hubRef}>
            {/* 台账抬头：数字全部来自真实状态，不写死 */}
            <div className="settings-masthead">
                <div className="settings-masthead__id">
                    <h1 className="settings-masthead__title">设置 · BusTerminal</h1>
                    <span className="settings-masthead__meta">配置台账 / 本地生效</span>
                </div>
                <div className="settings-masthead__figures">
                    <div className="settings-masthead__fig">
                        <span className="settings-masthead__num">{CATEGORIES.length}</span>
                        <span className="settings-masthead__cap">配置分类</span>
                    </div>
                    <div className="settings-masthead__fig">
                        <span className="settings-masthead__num">{Object.keys(sysConfig).length}</span>
                        <span className="settings-masthead__cap">已加载键</span>
                    </div>
                    <div className="settings-masthead__fig is-accent">
                        <span className="settings-masthead__num">{dirtyKeys.length}</span>
                        <span className="settings-masthead__cap">未保存</span>
                    </div>
                </div>
            </div>

            <div className="settings-dual-layout">
                {/* 01–10 索引导航，说明只留在 tooltip */}
                <div className="settings-sidebar-nav">
                    {CATEGORIES.map((cat, index) => {
                        const IconComponent = cat.icon;
                        const isActive = activeTab === cat.id;
                        return (
                            <button
                                key={cat.id}
                                className={`nav-item-btn ${isActive ? 'active' : ''}`}
                                onClick={() => setActiveTab(cat.id)}
                                type="button"
                                title={cat.desc}
                            >
                                <span className="cfg-nav__idx">{String(index + 1).padStart(2, '0')}</span>
                                <IconComponent size={16} strokeWidth={1.5} />
                                <span className="cfg-nav__label">{cat.label}</span>
                            </button>
                        );
                    })}
                    <div className="cfg-nav__rule" />
                    <div className="sidebar-save-wrapper">
                        <button
                            type="button"
                            className="btn sidebar-save-btn"
                            onClick={handleSaveSys}
                            disabled={savingSys}
                        >
                            <Save size={15} strokeWidth={1.5} />
                            <span>{savingSys ? '保存中…' : '保存全部设置'}</span>
                        </button>
                    </div>
                </div>

                <div className="settings-tab-content">
                    {activeTab === 'layout' && (
                        <SettingsLayoutTab
                            cardWidth={cardWidth}
                            onChangeLayout={setLayoutSettings}
                        />
                    )}

                    {activeTab === 'cache' && (
                        <SettingsCacheTab
                            sysConfig={sysConfig}
                            handleSysChange={handleSysChange}
                            cacheStats={cacheStats}
                            fetchCacheStats={fetchCacheStatsData}
                            cacheLoading={cacheLoading}
                            handleCacheCleanup={handleCacheCleanup}
                            cacheCleaning={cacheCleaning}
                            cacheInvalidationCategory={cacheInvalidationCategory}
                            setCacheInvalidationCategory={setCacheInvalidationCategory}
                            handleCacheInvalidation={handleCacheInvalidation}
                            cacheInvalidating={cacheInvalidating}
                            handleCacheWarmup={handleCacheWarmup}
                            cacheWarming={cacheWarming}
                        />
                    )}

                    {activeTab === 'schedule' && <SettingsScheduleTab />}

                    {activeTab === 'engine' && (
                        <SettingsEngineTab
                            qbConfig={qbConfig}
                            handleQBChange={handleQBChange}
                            handleSaveQB={handleSaveQB}
                            savingQB={savingQB}
                            handleTestQB={handleTestQB}
                            testingQB={testingQB}
                            qbStatus={qbStatus}
                            sysConfig={sysConfig}
                            handleSysChange={handleSysChange}
                        />
                    )}

                    {activeTab === 'network' && (
                        <SettingsNetworkTab
                            sysConfig={sysConfig}
                            handleSysChange={handleSysChange}
                        />
                    )}

                    {activeTab === 'block' && <SettingsBlockKeywordsTab />}

                    {activeTab === 'translate' && (
                        <SettingsTranslateTab
                            sysConfig={sysConfig}
                            handleSysChange={handleSysChange}
                        />
                    )}

                    {activeTab === 'integrate' && (
                        <SettingsJellyfinTab
                            sysConfig={sysConfig}
                            handleSysChange={handleSysChange}
                        />
                    )}

                    {activeTab === 'notify' && (
                        <SettingsNotifyTab
                            sysConfig={sysConfig}
                            handleSysChange={handleSysChange}
                        />
                    )}

                    {activeTab === 'mediastack' && (
                        <SettingsMediaStackTab />
                    )}
                </div>
            </div>

            {/* 只在真的有未保存改动时出现；清单是实际会提交的键，不是设计稿数字 */}
            {dirtyKeys.length > 0 && (
                <div className="settings-dirty">
                    {showDirtyDetail && (
                        <div className="settings-dirty-bar__detail">
                            <p className="settings-dirty-bar__note">
                                以下 {dirtyKeys.length} 个键将随本次保存写入配置表，保存后回读校验：
                            </p>
                            <div className="settings-dirty-bar__keys">
                                {dirtyKeys.map((key) => (
                                    <span className="settings-dirty-bar__key" key={key}>{key}</span>
                                ))}
                            </div>
                        </div>
                    )}
                    <div className="settings-dirty-bar">
                        <span className="settings-dirty-bar__count">{dirtyKeys.length}</span>
                        <span className="settings-dirty-bar__text">项未保存</span>
                        <button
                            type="button"
                            className="btn btn-sm"
                            onClick={() => setShowDirtyDetail((v) => !v)}
                        >
                            <ListChecks size={14} strokeWidth={1.5} />
                            {showDirtyDetail ? '收起' : '查看明细'}
                        </button>
                        <span className="settings-dirty-bar__spacer" />
                        <button type="button" className="btn btn-sm" onClick={handleDiscardSys}>
                            <RotateCcw size={14} strokeWidth={1.5} /> 放弃
                        </button>
                        <button
                            type="button"
                            className="btn btn-primary btn-sm"
                            onClick={handleSaveSys}
                            disabled={savingSys}
                        >
                            <Save size={14} strokeWidth={1.5} /> {savingSys ? '保存中…' : '保存改动'}
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
}
