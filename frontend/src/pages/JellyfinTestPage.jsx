import React, { useState, useEffect, useCallback, useRef } from 'react';
import { FlaskConical, Sparkles, CheckCircle, AlertTriangle, RefreshCw, Pause, XCircle, Trash2, Calendar, Star, Eye, Heart } from 'lucide-react';
import { getApiErrorMessage, getProxyImageUrl, logSafeError, movieApi } from '../api';
import { useToast } from '../components/Toast';
import './JellyfinTestPage.css';

function JellyfinTestPage() {
    const [selectedItems, setSelectedItems] = useState([]);
    const [taskId, setTaskId] = useState(null);
    const [progress, setProgress] = useState(null);
    const [isDeleting, setIsDeleting] = useState(false);
    const [libraries, setLibraries] = useState([]);
    const [movies, setMovies] = useState([]);
    const [loading, setLoading] = useState(false);
    const [loadError, setLoadError] = useState('');
    const { showToast } = useToast();
    const movieRequestSeqRef = useRef(0);

    const loadLibraries = useCallback(async () => {
        try {
            const data = await movieApi.getJfLibraries();
            if (data.success) {
                setLibraries(Array.isArray(data.data) ? data.data : []);
            } else {
                setLibraries([]);
                showToast('媒体库加载失败: ' + getApiErrorMessage(data), 'error');
            }
        } catch (error) {
            logSafeError('加载媒体库失败:', error);
            setLibraries([]);
            showToast('媒体库加载失败: ' + getApiErrorMessage(error), 'error');
        }
    }, [showToast]);

    // 加载媒体库列表
    useEffect(() => {
        loadLibraries();
    }, [loadLibraries]);

    // 加载影片列表
    const loadMovies = useCallback(async (libraryId = null) => {
        const requestSeq = ++movieRequestSeqRef.current;
        const isStaleRequest = () => requestSeq !== movieRequestSeqRef.current;
        setLoading(true);
        setLoadError('');
        setSelectedItems([]);
        try {
            const data = await movieApi.getLibrary({
                ...(libraryId ? { library_id: libraryId } : {}),
                limit: 20,
            });

            if (isStaleRequest()) return;
            if (data.success) {
                setMovies(Array.isArray(data.data) ? data.data : []);
            } else {
                setMovies([]);
                setLoadError(getApiErrorMessage(data, '影片加载失败'));
            }
        } catch (error) {
            if (isStaleRequest()) return;
            logSafeError('加载影片失败:', error);
            setMovies([]);
            setLoadError(getApiErrorMessage(error, '影片加载失败'));
        } finally {
            if (!isStaleRequest()) setLoading(false);
        }
    }, []);

    // 切换选中状态
    const toggleSelect = (movie) => {
        setSelectedItems(prev => {
            const exists = prev.find(item => item.jellyfin_id === movie.jellyfin_id);
            if (exists) {
                return prev.filter(item => item.jellyfin_id !== movie.jellyfin_id);
            } else {
                return [...prev, movie];
            }
        });
    };

    // 全选/取消全选
    const toggleSelectAll = () => {
        if (selectedItems.length === movies.length) {
            setSelectedItems([]);
        } else {
            setSelectedItems([...movies]);
        }
    };

    // 开始删除
    const startDeletion = async () => {
        if (selectedItems.length === 0) {
            showToast('请先选择要删除的影片', 'warning');
            return;
        }

        if (!confirm(`确定要删除 ${selectedItems.length} 个影片吗？`)) {
            return;
        }

        const confirmDelete = window.prompt('输入 DELETE_JELLYFIN_ITEMS 才会继续提交删除请求');
        if (confirmDelete !== 'DELETE_JELLYFIN_ITEMS') {
            showToast('确认短语不匹配，已取消删除', 'warning');
            return;
        }
        const reason = window.prompt('请输入删除原因');
        if (!reason || reason.trim().length < 4) {
            showToast('删除原因太短，已取消删除', 'warning');
            return;
        }

        setIsDeleting(true);

        try {
            const data = await movieApi.deleteJfItemsAsync({
                item_ids: selectedItems.map(item => item.jellyfin_id),
                confirm: confirmDelete,
                reason: reason.trim()
            });

            if (data.success) {
                setTaskId(data.task_id);
                showToast('删除任务已创建', 'success');
            } else {
                showToast('创建删除任务失败: ' + getApiErrorMessage(data), 'error');
                setIsDeleting(false);
            }
        } catch (error) {
            logSafeError('删除失败:', error);
            showToast('删除失败: ' + getApiErrorMessage(error), 'error');
            setIsDeleting(false);
        }
    };

    // 轮询进度
    useEffect(() => {
        if (!taskId) return;

        const interval = setInterval(async () => {
            try {
                const data = await movieApi.getDeleteProgress(taskId);

                if (data.success) {
                    setProgress(data);

                    // 检查是否完成
                    if (['completed', 'cancelled', 'error'].includes(data.status)) {
                        clearInterval(interval);
                        setIsDeleting(false);

                        if (data.status === 'completed') {
                            showToast(`删除完成：成功 ${data.completed}，失败 ${data.failed}`, data.failed ? 'warning' : 'success');
                            // 刷新列表
                            loadMovies();
                            setSelectedItems([]);
                        } else if (data.status === 'cancelled') {
                            showToast('删除任务已取消', 'warning');
                        } else if (data.status === 'error') {
                            showToast('删除任务执行失败: ' + getApiErrorMessage(data), 'error');
                        }
                    }
                } else {
                    clearInterval(interval);
                    setIsDeleting(false);
                    showToast('查询删除进度失败: ' + getApiErrorMessage(data), 'error');
                }
            } catch (error) {
                logSafeError('查询进度失败:', error);
                clearInterval(interval);
                setIsDeleting(false);
                showToast('查询删除进度失败: ' + getApiErrorMessage(error), 'error');
            }
        }, 1000);

        return () => clearInterval(interval);
    }, [loadMovies, showToast, taskId]);

    // 取消删除
    const cancelDeletion = async () => {
        if (!taskId) return;

        if (!confirm('确定要取消删除吗？')) return;

        try {
            const data = await movieApi.cancelDeleteTask(taskId);

            if (data.success) {
                showToast('删除任务已取消', 'success');
            } else {
                showToast('取消失败: ' + getApiErrorMessage(data), 'error');
            }
        } catch (error) {
            logSafeError('取消失败:', error);
            showToast('取消失败: ' + getApiErrorMessage(error), 'error');
        }
    };

    return (
        <div className="jellyfin-test-page">
            <div className="page-header">
                <h1><FlaskConical size={18} style={{marginRight: 4, verticalAlign: 'text-bottom'}} />Jellyfin API 测试页面</h1>
                <p>测试异步批量删除和封面缓存功能</p>
            </div>

            {/* 媒体库选择 */}
            <div className="library-selector">
                <h3>选择媒体库</h3>
                <div className="library-buttons">
                    <button onClick={() => loadMovies()} className="btn-primary">
                        加载所有影片
                    </button>
                    {libraries.map(lib => (
                        <button
                            key={lib.id}
                            onClick={() => loadMovies(lib.id)}
                            className="btn-secondary"
                        >
                            {lib.name}
                        </button>
                    ))}
                </div>
            </div>

            {/* 操作栏 */}
            {movies.length > 0 && (
                <div className="action-bar">
                    <div className="selection-info">
                        <label>
                            <input
                                type="checkbox"
                                checked={selectedItems.length === movies.length}
                                onChange={toggleSelectAll}
                            />
                            全选 ({selectedItems.length}/{movies.length})
                        </label>
                    </div>

                    <button
                        onClick={startDeletion}
                        disabled={isDeleting || selectedItems.length === 0}
                        className="btn-danger"
                    >
                        <Trash2 size={16} style={{marginRight: 4}} />删除选中项 ({selectedItems.length})
                    </button>
                </div>
            )}

            {/* 进度面板 */}
            {progress && (
                <div className="progress-panel">
                    <h3>删除进度</h3>

                    <div className="progress-bar-container">
                        <div
                            className="progress-bar-fill"
                            style={{ width: `${progress.progress}%` }}
                        >
                            <span className="progress-text">
                                {progress.progress.toFixed(1)}%
                            </span>
                        </div>
                    </div>

                    <div className="progress-stats">
                        <div className="stat-item">
                            <span className="stat-label">状态:</span>
                            <span className={`stat-value status-${progress.status}`}>
                                {progress.status === 'running' ? <><RefreshCw size={14} style={{marginRight: 4}} />进行中</> :
                                 progress.status === 'completed' ? <><CheckCircle size={14} style={{marginRight: 4}} />已完成</> :
                                 progress.status === 'cancelled' ? <><AlertTriangle size={14} style={{marginRight: 4}} />已取消</> :
                                 <><XCircle size={14} style={{marginRight: 4}} />错误</>}
                            </span>
                        </div>
                        <div className="stat-item">
                            <span className="stat-label">总计:</span>
                            <span className="stat-value">{progress.total}</span>
                        </div>
                        <div className="stat-item success">
                            <span className="stat-label">成功:</span>
                            <span className="stat-value">{progress.completed}</span>
                        </div>
                        <div className="stat-item failed">
                            <span className="stat-label">失败:</span>
                            <span className="stat-value">{progress.failed}</span>
                        </div>
                        {progress.elapsed && (
                            <div className="stat-item">
                                <span className="stat-label">已耗时:</span>
                                <span className="stat-value">{progress.elapsed.toFixed(1)}秒</span>
                            </div>
                        )}
                        {progress.eta && (
                            <div className="stat-item">
                                <span className="stat-label">预计剩余:</span>
                                <span className="stat-value">{Math.ceil(progress.eta)}秒</span>
                            </div>
                        )}
                    </div>

                    {progress.status === 'running' && (
                        <button onClick={cancelDeletion} className="btn-warning">
                            <Pause size={16} style={{marginRight: 4}} />取消删除
                        </button>
                    )}

                    {progress.results && progress.results.length > 0 && (
                        <div className="recent-results">
                            <h4>最近删除记录:</h4>
                            <ul>
                                {progress.results.slice(-5).reverse().map((result, idx) => (
                                    <li key={idx} className={result.success ? 'success' : 'failed'}>
                                        {result.success ? <CheckCircle size={14} style={{marginRight: 4}} /> : <XCircle size={14} style={{marginRight: 4}} />}
                                        {result.id}
                                        {result.error && ` - ${result.error}`}
                                    </li>
                                ))}
                            </ul>
                        </div>
                    )}
                </div>
            )}

            {/* 影片列表 */}
            <div className="movies-grid">
                {loading ? (
                    <div className="loading">加载中...</div>
                ) : loadError ? (
                    <div className="empty-state">
                        <p>{loadError}</p>
                    </div>
                ) : movies.length === 0 ? (
                    <div className="empty-state">
                        <p>请先选择媒体库加载影片</p>
                    </div>
                ) : (
                    movies.map(movie => {
                        const isSelected = selectedItems.some(
                            item => item.jellyfin_id === movie.jellyfin_id
                        );

                        return (
                            <div
                                key={movie.jellyfin_id}
                                className={`movie-card ${isSelected ? 'selected' : ''}`}
                                onClick={() => toggleSelect(movie)}
                            >
                                <div className="movie-cover">
                                    <img
                                        src={getProxyImageUrl(movie.cover, { w: 360 })}
                                        alt={movie.title}
                                        loading="lazy"
                                        decoding="async"
                                    />
                                    {isSelected && (
                                        <div className="selected-badge">✓</div>
                                    )}
                                </div>
                                <div className="movie-info">
                                    <h4 className="movie-title">{movie.title}</h4>
                                    <div className="movie-meta">
                                        {movie.year && <span><Calendar size={14} style={{marginRight: 4}} />{movie.year}</span>}
                                        {movie.rating && <span><Star size={14} style={{marginRight: 4}} />{movie.rating}</span>}
                                    </div>
                                    <div className="movie-status">
                                        {movie.is_watched && <span className="badge"><Eye size={14} style={{marginRight: 4}} />已看</span>}
                                        {movie.is_favorite && <span className="badge"><Heart size={14} style={{marginRight: 4}} />收藏</span>}
                                    </div>
                                </div>
                            </div>
                        );
                    })
                )}
            </div>

            {/* 功能说明 */}
            <div className="feature-info">
                <h3><Sparkles size={16} style={{marginRight: 4, verticalAlign: 'text-bottom'}} />新功能测试</h3>
                <ul>
                    <li><CheckCircle size={14} style={{marginRight: 4}} /> <strong>异步批量删除</strong>: 选择多个影片，点击删除按钮</li>
                    <li><CheckCircle size={14} style={{marginRight: 4}} /> <strong>实时进度显示</strong>: 查看删除进度、成功/失败数量、预计剩余时间</li>
                    <li><CheckCircle size={14} style={{marginRight: 4}} /> <strong>取消操作</strong>: 删除过程中可随时取消</li>
                    <li><CheckCircle size={14} style={{marginRight: 4}} /> <strong>封面缓存</strong>: 封面图片自动缓存，二次加载速度提升 10 倍</li>
                    <li><CheckCircle size={14} style={{marginRight: 4}} /> <strong>缓存指示</strong>: 打开浏览器开发者工具查看响应头 X-Cache (HIT/MISS)</li>
                </ul>
            </div>
        </div>
    );
}

export default JellyfinTestPage;
