import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Ban } from 'lucide-react';
import { getApiErrorMessage, getProxyImageUrl, logSafeError, movieApi } from '../api';
import { useToast } from '../components/Toast';
import CardGrid from '../components/CardGrid';
import ContentContainer from '../components/ContentContainer';
import './SubscriptionsPage.css';

// 假名是日文独有的标志：名字里没有假名就说明它已经是中文，送进翻译器拿不到译文，
// 只会拿到一句「该内容无法识别为有效影视标题。」之类的拒答文案。
const JAPANESE_KANA_PATTERN = /[\u3040-\u30ff]/;

const ACTRESS_SUBSCRIPTION_CATEGORIES = new Set([
    'star',
    'actress',
    'actor',
    'person',
    'performer',
    'favorite_actor',
    'jellyfin',
    'jellyfin_actor',
    'jellyfin_person',
]);

function stripLegacySubscriptionPrefix(name = '') {
    return String(name || '')
        .replace(/^订阅[:：]\s*/, '')
        .replace(/^关注[:：]\s*/, '')
        .trim();
}

function isActressSubscription(sub = {}) {
    const category = String(sub.category_type || '').toLowerCase();
    const url = String(sub.url || '').toLowerCase();
    return ACTRESS_SUBSCRIPTION_CATEGORIES.has(category)
        || url.includes('/star/')
        || url.startsWith('jf_actor:')
        || url.startsWith('jf_person:')
        || url.startsWith('jellyfin:actress:')
        || url.startsWith('jellyfin:actor:')
        || url.startsWith('jellyfin:person:');
}

function getSubscriptionDisplayBaseName(sub = {}) {
    return stripLegacySubscriptionPrefix(sub.name)
        .split(/[（(【[]/)[0]
        .trim();
}

function getSubscriptionStatsScore(sub = {}, stats = {}) {
    const item = stats[sub.url] || {};
    const tasks = Array.isArray(item.tasks) ? item.tasks : [];
    const decorativeCovers = Array.isArray(item.decorative_covers) ? item.decorative_covers : [];
    const coverCount = [
        ...tasks.map(task => task?.cover),
        ...decorativeCovers.map(cover => (typeof cover === 'string' ? cover : cover?.cover)),
    ].filter(cover => cover && String(cover).trim()).length;
    const taskCount = Number(item.count || 0);
    const createdAt = Number.isFinite(Number(sub.created_at)) ? Number(sub.created_at) : 0;
    return { coverCount, taskCount, createdAt };
}

function sortSubscriptionsForDisplay(subscriptions = [], stats = {}) {
    return [...subscriptions].sort((left, right) => {
        const leftScore = getSubscriptionStatsScore(left, stats);
        const rightScore = getSubscriptionStatsScore(right, stats);
        return rightScore.coverCount - leftScore.coverCount
            || rightScore.taskCount - leftScore.taskCount
            || rightScore.createdAt - leftScore.createdAt;
    });
}

function SubscriptionsPage() {
    const [subscriptions, setSubscriptions] = useState([]);
    const [stats, setStats] = useState({});
    const [loading, setLoading] = useState(true);
    const [expandedCards, setExpandedCards] = useState({});
    const [translatedNames, setTranslatedNames] = useState({});
    const { showToast } = useToast();
    const loadRequestSeqRef = useRef(0);

    const fetchTranslations = useCallback(async (subs) => {
        const toTranslate = subs
            .filter(isActressSubscription)
            .map((sub) => {
                const name = stripLegacySubscriptionPrefix(sub.name);
                if (name.match(/[（(]([^）)]+)[）)]/)) return null;
                const baseName = name.split(/[（(【[]/)[0].trim();
                if (!baseName || !JAPANESE_KANA_PATTERN.test(baseName)) return null;
                return baseName;
            })
            .filter(Boolean);

        if (toTranslate.length === 0) return;
        try {
            const res = await movieApi.batchTranslate(toTranslate);
            if (res.success) {
                setTranslatedNames(prev => ({ ...prev, ...res.data }));
            }
        } catch (e) {
            logSafeError('Translation error:', e);
        }
    }, []);

    const loadData = useCallback(async () => {
        const requestSeq = ++loadRequestSeqRef.current;
        const isStaleRequest = () => requestSeq !== loadRequestSeqRef.current;
        setLoading(true);
        try {
            const subRes = await movieApi.getSubscriptions({ sync: false });
            if (isStaleRequest()) return;
            if (subRes.success) {
                const nextSubscriptions = Array.isArray(subRes.data) ? subRes.data : [];
                setSubscriptions(nextSubscriptions);
                fetchTranslations(nextSubscriptions);
                const jellyfinCreated = Number(subRes.jellyfin_sync?.created || 0);
                const repaired = Number(subRes.repair?.repaired || 0);
                if (jellyfinCreated > 0 || repaired > 0) {
                    showToast(`已同步 ${jellyfinCreated + repaired} 个 Jellyfin 订阅`, 'success');
                }
            } else {
                setSubscriptions([]);
                setStats({});
                showToast(getApiErrorMessage(subRes, '订阅数据加载失败'), 'error');
                return;
            }

            movieApi.getSubscriptionStats()
                .then(statRes => {
                    if (isStaleRequest()) return;
                    if (statRes?.success) {
                        setStats(statRes.data || {});
                        return;
                    }
                    setStats({});
                    showToast('订阅列表已加载，统计数据暂时不可用', 'warning');
                })
                .catch(error => {
                    if (isStaleRequest()) return;
                    logSafeError('订阅统计加载失败:', error);
                    setStats({});
                    showToast('订阅列表已加载，统计数据暂时不可用', 'warning');
                });
        } catch (error) {
            if (isStaleRequest()) return;
            logSafeError('订阅数据加载失败:', error);
            setSubscriptions([]);
            setStats({});
            showToast('订阅数据加载失败：' + getApiErrorMessage(error), 'error');
        } finally {
            if (!isStaleRequest()) setLoading(false);
        }
    }, [fetchTranslations, showToast]);

    useEffect(() => {
        loadData();
    }, [loadData]);

    const handleDelete = async (url) => {
        if (!window.confirm('确定要取消这个订阅吗？')) return;
        try {
            const response = await movieApi.deleteSubscription(url);
            if (response.success) {
                showToast('订阅已取消', 'success');
                loadData();
            } else {
                showToast(getApiErrorMessage(response, '取消失败'), 'error');
            }
        } catch (e) {
            showToast('取消失败: ' + getApiErrorMessage(e), 'error');
        }
    };

    const toggleDetails = (url) => {
        setExpandedCards(prev => ({ ...prev, [url]: !prev[url] }));
    };

    const totalSubs = subscriptions.length;
    const totalDownloads = Object.values(stats).reduce((acc, curr) => acc + (curr.count || 0), 0);
    const activeTasks = Object.values(stats).reduce((acc, curr) => acc + (curr.tasks ? curr.tasks.length : 0), 0);
    const actressSubsCount = subscriptions.filter(isActressSubscription).length;

    const groupedSubs = useMemo(() => ({
        actresses: sortSubscriptionsForDisplay(
            subscriptions.filter(isActressSubscription),
            stats,
        ),
        others: sortSubscriptionsForDisplay(
            subscriptions.filter(sub => !isActressSubscription(sub)),
            stats,
        ),
    }), [stats, subscriptions]);

    return (
        <div className="subscriptions-page">
            <ContentContainer className="subscriptions-container">
                <div className="subscriptions-content">
                    <div className="stats-bar">
                        <div className="stat-card">
                            <div className="stat-number">{actressSubsCount}</div>
                            <div className="stat-label">演员订阅</div>
                        </div>
                        <div className="stat-card">
                            <div className="stat-number">{totalDownloads}</div>
                            <div className="stat-label">累计捕获</div>
                        </div>
                        <div className="stat-card">
                            <div className="stat-number">{activeTasks}</div>
                            <div className="stat-label">近期任务</div>
                        </div>
                        <div className="stat-card">
                            <div className="stat-number">{totalSubs}</div>
                            <div className="stat-label">总订阅源</div>
                        </div>
                    </div>

                    {groupedSubs.actresses.length > 0 && (
                        <div className="subscription-group">
                            <h2 className="group-title">演员订阅</h2>
                            <CardGrid className="actress-grid" variant="wide">
                                {groupedSubs.actresses.map(sub => (
                                    <SubscriptionCard
                                        key={sub.url}
                                        sub={sub}
                                        stats={stats[sub.url]}
                                        expanded={expandedCards[sub.url]}
                                        translatedName={translatedNames[getSubscriptionDisplayBaseName(sub)]}
                                        onDelete={() => handleDelete(sub.url)}
                                        onToggle={() => toggleDetails(sub.url)}
                                    />
                                ))}
                            </CardGrid>
                        </div>
                    )}

                    {groupedSubs.others.length > 0 && (
                        <div className="subscription-group">
                            <h2 className="group-title">分类/番号订阅</h2>
                            <CardGrid className="actress-grid" variant="wide">
                                {groupedSubs.others.map(sub => (
                                    <SubscriptionCard
                                        key={sub.url}
                                        sub={sub}
                                        stats={stats[sub.url]}
                                        expanded={expandedCards[sub.url]}
                                        translatedName={translatedNames[getSubscriptionDisplayBaseName(sub)]}
                                        onDelete={() => handleDelete(sub.url)}
                                        onToggle={() => toggleDetails(sub.url)}
                                    />
                                ))}
                            </CardGrid>
                        </div>
                    )}

                    {loading && subscriptions.length === 0 && (
                        <div className="subscription-group">
                            <div className="empty-state">正在加载订阅...</div>
                        </div>
                    )}

                    {!loading && subscriptions.length === 0 && (
                        <div className="subscription-group">
                            <div className="empty-state">暂无订阅</div>
                        </div>
                    )}
                </div>
            </ContentContainer>
        </div>
    );
}

function SubscriptionCard({ sub, stats, expanded, translatedName, onDelete, onToggle }) {
    const taskCount = stats?.count || 0;
    const taskList = React.useMemo(() => stats?.tasks || [], [stats]);
    const recentCount = taskList.length;
    const [selectedCovers, setSelectedCovers] = React.useState([]);
    const [loadedCovers, setLoadedCovers] = React.useState(new Set());

    React.useEffect(() => {
        const decorativeCovers = Array.isArray(stats?.decorative_covers) ? stats.decorative_covers : [];
        const allCovers = [
            ...taskList.map(task => task?.cover),
            ...decorativeCovers.map(item => (typeof item === 'string' ? item : item?.cover)),
        ]
            .filter(cover => cover && String(cover).trim())
            .filter((cover, index, list) => list.indexOf(cover) === index);

        // ⚠️ 只有展开时才拉封面图。之前 234 张订阅卡各拉 5 张 = 上千张图并发请求，
        // 把首屏拖到几秒；列表概览根本用不上这些图。
        if (!expanded) {
            setSelectedCovers([]);
            return;
        }

        if (allCovers.length === 0) {
            // 列表接口不再批量算装饰封面（那要 15 秒），展开时按需单独取一次
            let cancelled = false;
            movieApi.getSubscriptionCovers(sub.url).then((res) => {
                if (cancelled || !res?.success) return;
                const extra = (res.data || [])
                    .map(item => (typeof item === 'string' ? item : item?.cover))
                    .filter(cover => cover && String(cover).trim());
                const merged = [...extra, ...allCovers]
                    .filter((cover, index, list) => list.indexOf(cover) === index)
                    .slice(0, 5);
                setSelectedCovers(merged);
                setLoadedCovers(new Set());
            }).catch(() => { /* 封面取不到不影响看详情 */ });
            return () => { cancelled = true; };
        }

        setSelectedCovers(allCovers.slice(0, Math.min(5, allCovers.length)));
        setLoadedCovers(new Set());
        return undefined;
    }, [stats, taskList, expanded, sub.url]);

    const handleImageLoad = (url) => {
        setLoadedCovers(prev => new Set([...prev, url]));
    };

    const handleImageError = (url) => {
        setSelectedCovers(prev => prev.filter(item => item !== url));
    };

    const progressWidth = recentCount > 0 ? `${Math.min((recentCount / 10) * 100, 100)}%` : '0%';
    let displayName = stripLegacySubscriptionPrefix(sub.name);

    if (translatedName) {
        displayName = translatedName;
    } else {
        const chineseMatch = displayName.match(/[（(]([^）)]+)[）)]/);
        displayName = chineseMatch?.[1] || displayName.split(/[（(【[]/)[0].trim();
    }

    const nameChars = Array.from(displayName);
    const visibleName = nameChars.length > 8 ? `${nameChars.slice(0, 8).join('')}…` : displayName;

    return (
        <div className="actress-card">
            {selectedCovers.length > 0 && (
                <div className="fan-gallery">
                    {selectedCovers.map((url, index) => (
                        <div key={`${url}-${index}`} className="fan-card">
                            <img
                                src={getProxyImageUrl(url, { w: 240 })}
                                alt=""
                                loading="lazy"
                                decoding="async"
                                onLoad={() => handleImageLoad(url)}
                                onError={() => handleImageError(url)}
                                style={{
                                    width: '100%',
                                    height: '100%',
                                    objectFit: 'cover',
                                    opacity: loadedCovers.has(url) ? 1 : 0,
                                    transition: 'opacity 0.3s ease',
                                }}
                            />
                        </div>
                    ))}
                </div>
            )}

            <div className="actress-header">
                <div className="actress-name" title={displayName}>{visibleName}</div>
                <div className={`status-indicator ${taskCount > 0 ? 'status-active' : 'status-refreshing'}`} />
            </div>

            <div className="download-info">
                <div className="download-item">
                    <div className="download-number">{taskCount}</div>
                    <div className="download-label">累计下载</div>
                </div>
                <div className="download-item">
                    <div className="download-number">{recentCount}</div>
                    <div className="download-label">近期命中</div>
                </div>
            </div>

            <div className="progress-bar">
                <div className="progress-fill" style={{ width: progressWidth }} />
            </div>

            {sub.exclude_tags && (
                <div className="card-tags">
                    {sub.exclude_tags.split(/,|，/).filter(Boolean).map((tag, index) => (
                        <span key={`${tag}-${index}`} className="mini-tag">
                            <Ban size={12} style={{ marginRight: 4 }} />
                            {tag.trim()}
                        </span>
                    ))}
                </div>
            )}

            {expanded && (
                <div className="details-overlay">
                    <strong>最近任务记录</strong>
                    {taskList.length === 0 ? (
                        <div style={{ padding: '10px', textAlign: 'center' }}>暂无记录</div>
                    ) : (
                        [...taskList].reverse().slice(0, 10).map((task, index) => (
                            <div key={`${task.id || task.title}-${index}`} className={`detail-item ${task.status}`}>
                                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', marginRight: '10px' }}>
                                    {task.title}
                                </span>
                                <span style={{ fontSize: '10px' }}>
                                    {task.status === 'submitted'
                                        ? '已提交'
                                        : task.status === 'waiting_external_service'
                                            ? `等待服务（已重试 ${task.retry_count || 0} 次）`
                                            : '排队中'}
                                </span>
                            </div>
                        ))
                    )}
                </div>
            )}

            <div className="card-actions">
                <button className="btn btn-primary" onClick={onToggle}>
                    {expanded ? '收起详情' : '查看详情'}
                </button>
                <button className="btn btn-danger" onClick={onDelete}>
                    取消订阅
                </button>
            </div>
        </div>
    );
}

export default SubscriptionsPage;
