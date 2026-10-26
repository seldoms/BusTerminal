import React, { useState, useEffect, useCallback } from 'react';
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
    Ban
} from 'lucide-react';
import './SettingsPage.css';
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

const CATEGORIES = [
    { id: 'layout', label: '界面与布局', icon: Sliders, desc: '卡片尺寸与页面宽度联动' },
    { id: 'cache', label: '缓存与性能', icon: Database, desc: '磁盘已用与图表数据清理' },
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

    // 系统配置
    const [sysConfig, setSysConfig] = useState({});
    const [loadingSys, setLoadingSys] = useState(true);
    const [savingSys, setSavingSys] = useState(false);

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
            if (data) setSysConfig(data);
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

    // Handler: 修改系统 State
    const handleSysChange = (key, value) => {
        setSysConfig(prev => ({ ...prev, [key]: value }));
    };

    // Handler: 保存系统配置
    const handleSaveSys = async () => {
        setSavingSys(true);
        try {
            const res = await saveSystemSettings(sysConfig);
            if (res && res.success !== false) {
                showToast('系统设置已成功保存！', 'success');
                // 回读校验：后端可能做了归一化（如剥掉主机末尾斜杠），
                // 用回读值覆盖本地状态，界面上立刻就是真实生效值。
                const saved = res?.data;
                if (saved && typeof saved === 'object') {
                    setSysConfig(prev => ({ ...prev, ...saved }));
                }
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
                <RefreshCw className="spin" size={32} style={{ color: 'var(--brand)', marginBottom: 12 }} />
                <p style={{ color: 'var(--text-secondary)' }}>正在加载系统设置中心...</p>
            </div>
        );
    }

    return (
        <div className="container settings-hub-container">
            {/* Main Dual Column Layout */}
            <div className="settings-dual-layout">
                {/* Left Sidebar Tab Navigation */}
                <div className="settings-sidebar-nav">
                    {CATEGORIES.map((cat) => {
                        const IconComponent = cat.icon;
                        const isActive = activeTab === cat.id;
                        return (
                            <button
                                key={cat.id}
                                className={`nav-item-btn ${isActive ? 'active' : ''}`}
                                onClick={() => setActiveTab(cat.id)}
                                type="button"
                            >
                                <IconComponent size={18} />
                                <span>{cat.label}</span>
                            </button>
                        );
                    })}

                    <div className="sidebar-save-wrapper">
                        <button
                            type="button"
                            className="btn btn-primary sidebar-save-btn"
                            onClick={handleSaveSys}
                            disabled={savingSys}
                        >
                            <Save size={15} />
                            <span>{savingSys ? '保存中...' : '保存全部设置'}</span>
                        </button>
                    </div>
                </div>

                {/* Right Content Pane */}
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
        </div>
    );
}
