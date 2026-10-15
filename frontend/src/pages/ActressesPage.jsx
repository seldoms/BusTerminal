import React, { useCallback, useEffect, useState } from 'react';
import { ArrowDownWideNarrow, Heart, Leaf } from 'lucide-react';
import { getApiErrorMessage, logSafeError, movieApi } from '../api';
import { getActressAvatarSrc } from '../utils/actressDisplay';
import ActressCard from '../components/ActressCard';
import MovieInfiniteList from '../components/MovieInfiniteList';
import { useToast } from '../components/Toast';
import './ActressesPage.css';

const PAGE_SIZE = 30;
const getSubscriptionFilterLabel = (subscriptionState) => {
    if (subscriptionState === 'subscribed') return '已订阅';
    if (subscriptionState === 'unsubscribed') return '未订阅';
    return '全部订阅';
};

function ActressesPage() {
    const [subscriptionState, setSubscriptionState] = useState('all');
    const [sortByNew, setSortByNew] = useState(false);
    const [sortByCup, setSortByCup] = useState(false);
    const { showToast } = useToast();

    // 取数回调：交给统一的 MovieInfiniteList 处理无限滚动 + 头像预加载 + 后台预取
    const fetchPage = useCallback((pageNum, limit) => {
        return movieApi.getActresses({
            page: pageNum,
            limit,
            subscription_state: subscriptionState,
            sort_by_new: sortByNew,
            sort_by_cup: sortByCup,
        });
    }, [subscriptionState, sortByNew, sortByCup]);

    const getImageUrl = useCallback((actress) => getActressAvatarSrc(actress?.avatar), []);

    const handleToggleSubscription = useCallback(async (actressId) => {
        try {
            const response = await movieApi.subscribeActress(actressId);
            if (!response.success) {
                showToast(getApiErrorMessage(response, '订阅操作失败'), 'error');
                return;
            }
            const rawSubscribed = response.is_subscribed ?? response.is_followed;
            const isSubscribed = rawSubscribed === true || rawSubscribed === 1 || rawSubscribed === '1';
            showToast(isSubscribed ? '订阅成功' : '已取消订阅', 'success');
        } catch (error) {
            showToast(getApiErrorMessage(error, '订阅操作失败'), 'error');
        }
    }, [showToast]);

    const renderItem = useCallback((actress) => (
        <ActressCard actress={actress} onToggleSubscription={handleToggleSubscription} />
    ), [handleToggleSubscription]);

    useEffect(() => {
        return () => {
            movieApi.markActressesViewed().catch(error => logSafeError('Failed to mark actresses viewed:', error));
        };
    }, []);

    const toggleSubscriptionFilter = useCallback(() => {
        setSubscriptionState(prev => (prev === 'subscribed' ? 'unsubscribed' : 'subscribed'));
    }, []);

    const reloadKey = `${subscriptionState}|${sortByNew}|${sortByCup}`;

    return (
        <div className="actresses-page container fade-in">
            <header className="page-header">
                <div className="header-actions-new">
                    <div className="pill-tabs" aria-label="女优筛选">
                        <button
                            type="button"
                            className={`pill-tab${subscriptionState !== 'all' ? ' active' : ''}`}
                            onClick={toggleSubscriptionFilter}
                            title={subscriptionState === 'subscribed' ? '当前显示已订阅女优，点击切换到未订阅' : '点击显示已订阅女优'}
                        >
                            <Heart size={14} />
                            {getSubscriptionFilterLabel(subscriptionState)}
                        </button>
                        <button
                            type="button"
                            className={`pill-tab${sortByNew ? ' active' : ''}`}
                            onClick={() => setSortByNew(prev => !prev)}
                        >
                            <Leaf size={14} />新茶优先
                        </button>
                        <button
                            type="button"
                            className={`pill-tab${sortByCup ? ' active' : ''}`}
                            onClick={() => setSortByCup(prev => !prev)}
                        >
                            <ArrowDownWideNarrow size={14} />罩杯优先
                        </button>
                    </div>
                </div>
            </header>

            <MovieInfiniteList
                key={reloadKey}
                fetchPage={fetchPage}
                renderItem={renderItem}
                getImageUrl={getImageUrl}
                initialLimit={PAGE_SIZE}
                gridClassName="actress-grid"
                cacheKey={`actresses:${reloadKey}`}
            />
        </div>
    );
}

export default ActressesPage;
