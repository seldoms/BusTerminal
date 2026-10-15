import React, { useState, useEffect, useCallback } from 'react';
import { X, Download, CheckCircle2, Clock, ExternalLink, HardDrive, Film } from 'lucide-react';
import { useMovieDrawer } from './MovieDrawerContext';
import { movieApi, getProxyImageUrl } from '../api';
import { useToast } from './Toast';
import './MovieContextDrawer.css';

export default function MovieContextDrawer() {
    const { isOpen, drawerData, closeMovieDrawer } = useMovieDrawer();
    const { showToast } = useToast();

    // 内部查询的三方状态
    const [loadingStatus, setLoadingStatus] = useState(false);
    const [jellyfinStatus, setJellyfinStatus] = useState(null); // { inLibrary: boolean, item: object }
    const [qbStatus, setQbStatus] = useState(null); // { isDownloading: boolean, progress: number, speed: string }
    const [downloadingAction, setDownloadingAction] = useState(false);

    // 解析番号 (Code)
    const extractCode = (title = '', url = '') => {
        const codeMatch = (title || url).match(/([A-Z0-9]{2,8}[-_][0-9]{3,5})/i);
        return codeMatch ? codeMatch[1].toUpperCase() : null;
    };

    const movieCode = drawerData ? (drawerData.code || extractCode(drawerData.title, drawerData.url)) : null;

    // 当抽屉打开时，并发异步检测 Jellyfin 和 qBittorrent 状态
    const checkStatuses = useCallback(async () => {
        if (!drawerData) return;
        setLoadingStatus(true);
        setJellyfinStatus(null);
        setQbStatus(null);

        try {
            // 1. 查询 Jellyfin 库
            if (movieCode) {
                const jfRes = await movieApi.getLibrary({ q: movieCode, limit: 1 });
                const jfItems = Array.isArray(jfRes?.data) ? jfRes.data : (jfRes?.data?.items || jfRes?.items || []);
                if (jfItems.length > 0) {
                    setJellyfinStatus({ inLibrary: true, item: jfItems[0] });
                } else {
                    setJellyfinStatus({ inLibrary: false });
                }
            }

            // 2. 查询 qB / 本地下载队列
            const queueRes = await movieApi.getDownloadQueue({ limit: 50 });
            const queueItems = queueRes?.data?.items || queueRes?.items || [];
            if (queueItems.length > 0) {
                const activeTask = queueItems.find(t =>
                    (movieCode && t.title?.toUpperCase().includes(movieCode)) ||
                    (drawerData.title && t.title?.includes(drawerData.title))
                );
                if (activeTask) {
                    setQbStatus({
                        isDownloading: true,
                        status: activeTask.status,
                        progress: activeTask.progress || 0,
                        speed: activeTask.speed || '0 KB/s'
                    });
                } else {
                    setQbStatus({ isDownloading: false });
                }
            }
        } catch (err) {
            console.error('检查影片关联状态失败:', err);
        } finally {
            setLoadingStatus(false);
        }
    }, [drawerData, movieCode]);

    useEffect(() => {
        if (isOpen && drawerData) {
            checkStatuses();
        }
    }, [isOpen, drawerData, checkStatuses]);

    // 键盘 Esc 关闭监听
    useEffect(() => {
        const handleKeyDown = (e) => {
            if (e.key === 'Escape' && isOpen) {
                closeMovieDrawer();
            }
        };
        window.addEventListener('keydown', handleKeyDown);
        return () => window.removeEventListener('keydown', handleKeyDown);
    }, [isOpen, closeMovieDrawer]);

    if (!drawerData) return null;

    const posterUrl = drawerData.cover || drawerData.poster || drawerData.img;
    const resolvedPoster = posterUrl ? getProxyImageUrl(posterUrl, { w: 360 }) : '';

    // 一键极速下载
    const handleQuickDownload = async () => {
        setDownloadingAction(true);
        try {
            const res = await movieApi.downloadMovie({
                movie_url: drawerData.url,
                movie_id: movieCode,
                title: drawerData.title,
                preference: '1080P'
            });
            if (res && res.success !== false) {
                showToast('极速下载任务已成功提交！', 'success');
                checkStatuses(); // 重新加载状态
            } else {
                showToast(res?.message || '极速下载提交失败', 'error');
            }
        } catch (err) {
            showToast('下载请求发生异常', 'error');
        } finally {
            setDownloadingAction(false);
        }
    };

    return (
        <>
            {/* Backdrop Blur Overlay */}
            <div
                className={`drawer-overlay ${isOpen ? 'active' : ''}`}
                onClick={closeMovieDrawer}
            />

            {/* Slide-over Drawer Panel */}
            <div className={`drawer-panel ${isOpen ? 'active' : ''}`}>
                {/* Header Banner */}
                <div className="drawer-banner-header">
                    <button
                        className="drawer-close-btn"
                        onClick={closeMovieDrawer}
                        type="button"
                        aria-label="Close"
                    >
                        <X size={18} />
                    </button>

                    <div className="drawer-media-meta">
                        {resolvedPoster ? (
                            <img
                                src={resolvedPoster}
                                alt={drawerData.title}
                                className="drawer-poster-thumb"
                                onError={(e) => { e.target.style.display = 'none'; }}
                            />
                        ) : (
                            <div className="drawer-poster-thumb" style={{ background: 'var(--surface-2)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                                <Film size={24} style={{ color: 'var(--text-muted)' }} />
                            </div>
                        )}

                        <div className="drawer-meta-info">
                            <div>
                                {movieCode && <span className="drawer-code-badge">{movieCode}</span>}
                                <h2 className="drawer-title" title={drawerData.title}>
                                    {drawerData.title || '未知影片'}
                                </h2>
                            </div>
                            <div className="drawer-meta-tags">
                                {drawerData.date && <span>📅 {drawerData.date}</span>}
                                {drawerData.actress && <span>👩 {drawerData.actress}</span>}
                            </div>
                        </div>
                    </div>
                </div>

                {/* Body Content */}
                <div className="drawer-body">
                    {/* 三方状态矩阵卡片 (Status Matrix) */}
                    <div className="status-matrix-card">
                        <div className="matrix-title">
                            <HardDrive size={14} /> 实时关联服务诊断
                        </div>

                        {/* Jellyfin 关联 */}
                        <div className="status-row">
                            <span className="status-label">
                                <Film size={15} style={{ color: 'var(--brand)' }} />
                                Jellyfin 本地影音库
                            </span>
                            {loadingStatus ? (
                                <span className="status-badge not-found">检测中...</span>
                            ) : jellyfinStatus?.inLibrary ? (
                                <span className="status-badge in-library">
                                    <CheckCircle2 size={13} /> 已入库
                                </span>
                            ) : (
                                <span className="status-badge not-found">未在库</span>
                            )}
                        </div>

                        {/* qBittorrent 关联 */}
                        <div className="status-row">
                            <span className="status-label">
                                <Download size={15} style={{ color: 'var(--brand)' }} />
                                qBittorrent 下载任务
                            </span>
                            {loadingStatus ? (
                                <span className="status-badge not-found">检测中...</span>
                            ) : qbStatus?.isDownloading ? (
                                <span className="status-badge downloading">
                                    <Clock size={13} /> {qbStatus.status || '下载中'} ({Math.round(qbStatus.progress * 100)}%)
                                </span>
                            ) : (
                                <span className="status-badge not-found">无活动任务</span>
                            )}
                        </div>
                    </div>
                </div>

                {/* Footer Action Bar */}
                <div className="drawer-actions-footer">
                    <div className="action-btn-row">
                        <button
                            type="button"
                            className="btn btn-primary flex-btn"
                            onClick={handleQuickDownload}
                            disabled={downloadingAction || qbStatus?.isDownloading}
                        >
                            <Download size={16} />
                            {downloadingAction ? '提交中...' : qbStatus?.isDownloading ? '已在下载队列中' : '一键推送下载'}
                        </button>
                    </div>
                    {drawerData.url && (
                        <a
                            href={drawerData.url.startsWith('http') ? drawerData.url : `/movie/${encodeURIComponent(drawerData.url)}`}
                            className="btn btn-secondary"
                            style={{ textAlign: 'center', justifyContent: 'center', textDecoration: 'none' }}
                        >
                            <ExternalLink size={15} /> 查看完整详情页
                        </a>
                    )}
                </div>
            </div>
        </>
    );
}

