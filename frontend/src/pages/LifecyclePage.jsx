import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, ArrowRight, ChevronLeft, ChevronRight, History, RefreshCw, Search } from 'lucide-react';
import { Link } from 'react-router-dom';
import { getApiErrorMessage, logSafeError, movieApi } from '../api';
import ContentContainer from '../components/ContentContainer';
import './LifecyclePage.css';

const PAGE_SIZE = 50;

const DIMENSIONS = [
    { value: 'all', label: '全部维度', statuses: [] },
    { value: 'discovery_status', label: '发现', statuses: ['unknown', 'discovered'] },
    { value: 'decision_status', label: '决策', statuses: ['pending', 'candidate', 'approved', 'rejected', 'blocked'] },
    { value: 'download_status', label: '下载', statuses: ['unknown', 'queued', 'waiting_external_service', 'downloaded', 'delivered', 'blocked', 'failed'] },
    { value: 'organize_status', label: '整理', statuses: ['pending', 'organizing', 'completed', 'failed', 'blocked'] },
    { value: 'jellyfin_status', label: 'Jellyfin', statuses: ['unknown', 'indexed', 'watched', 'unwatched', 'favorite', 'not_favorite'] },
    { value: 'cleanup_status', label: '清理', statuses: ['none', 'candidate', 'approved', 'rejected', 'completed'] },
];

const STATUS_FIELDS = DIMENSIONS.filter(item => item.value !== 'all');

function LifecyclePage() {
    const [movies, setMovies] = useState([]);
    const [events, setEvents] = useState([]);
    const [selectedMovieId, setSelectedMovieId] = useState('');
    const [dimension, setDimension] = useState('all');
    const [status, setStatus] = useState('all');
    const [searchInput, setSearchInput] = useState('');
    const [movieQuery, setMovieQuery] = useState('');
    const [offset, setOffset] = useState(0);
    const [listLoading, setListLoading] = useState(true);
    const [eventsLoading, setEventsLoading] = useState(false);
    const [listError, setListError] = useState('');
    const [eventsError, setEventsError] = useState('');
    const listRequestRef = useRef(0);
    const eventsRequestRef = useRef(0);
    const eventsMovieIdRef = useRef('');

    const selectedDimension = useMemo(
        () => DIMENSIONS.find(item => item.value === dimension) || DIMENSIONS[0],
        [dimension],
    );

    const loadMovies = useCallback(async () => {
        const requestSeq = ++listRequestRef.current;
        setListLoading(true);
        setListError('');
        const params = { limit: PAGE_SIZE, offset };
        if (movieQuery) params.movie_id = movieQuery;
        if (dimension !== 'all' && status !== 'all') params[dimension] = status;

        try {
            const response = await movieApi.getLifecycleMovies(params);
            if (requestSeq !== listRequestRef.current) return;
            if (!response?.success) throw new Error(getApiErrorMessage(response, '生命周期加载失败'));
            const rows = Array.isArray(response.data) ? response.data : [];
            setMovies(rows);
            setSelectedMovieId(current => (
                rows.some(item => item.movie_id === current) ? current : (rows[0]?.movie_id || '')
            ));
            if (rows.length === 0) setEvents([]);
        } catch (error) {
            if (requestSeq !== listRequestRef.current) return;
            logSafeError('Lifecycle list load failed:', error);
            setListError(getApiErrorMessage(error, '生命周期加载失败'));
        } finally {
            if (requestSeq === listRequestRef.current) setListLoading(false);
        }
    }, [dimension, movieQuery, offset, status]);

    const loadEvents = useCallback(async () => {
        if (!selectedMovieId) {
            eventsMovieIdRef.current = '';
            setEvents([]);
            return;
        }
        const requestSeq = ++eventsRequestRef.current;
        if (eventsMovieIdRef.current !== selectedMovieId) {
            setEvents([]);
        }
        setEventsLoading(true);
        setEventsError('');
        try {
            const response = await movieApi.getLifecycleEvents(selectedMovieId, 200);
            if (requestSeq !== eventsRequestRef.current) return;
            if (!response?.success) throw new Error(getApiErrorMessage(response, '事件时间线加载失败'));
            eventsMovieIdRef.current = selectedMovieId;
            setEvents(Array.isArray(response.data) ? response.data : []);
        } catch (error) {
            if (requestSeq !== eventsRequestRef.current) return;
            logSafeError('Lifecycle events load failed:', error);
            setEventsError(getApiErrorMessage(error, '事件时间线加载失败'));
        } finally {
            if (requestSeq === eventsRequestRef.current) setEventsLoading(false);
        }
    }, [selectedMovieId]);

    useEffect(() => {
        loadMovies();
    }, [loadMovies]);

    useEffect(() => {
        loadEvents();
    }, [loadEvents]);

    const submitSearch = (event) => {
        event.preventDefault();
        setMovieQuery(searchInput.trim());
        setOffset(0);
    };

    const changeDimension = (event) => {
        setDimension(event.target.value);
        setStatus('all');
        setOffset(0);
    };

    const selectedMovie = movies.find(item => item.movie_id === selectedMovieId);
    const visibleEvents = eventsMovieIdRef.current === selectedMovieId ? events : [];

    return (
        <div className="lifecycle-page">
            <ContentContainer className="lifecycle-container">
                <header className="lifecycle-header">
                    <div>
                        <h1>生命周期</h1>
                        <span>{movies.length} 项</span>
                    </div>
                    <div className="lifecycle-header-actions">
                        <Link to="/tasks" className="lifecycle-link-button">
                            <History size={16} /> 任务中心
                        </Link>
                        <button
                            type="button"
                            className="lifecycle-icon-button"
                            onClick={() => { loadMovies(); loadEvents(); }}
                            disabled={listLoading || eventsLoading}
                            title="刷新生命周期"
                            aria-label="刷新生命周期"
                        >
                            <RefreshCw size={17} className={listLoading || eventsLoading ? 'is-spinning' : ''} />
                        </button>
                    </div>
                </header>

                <div className="lifecycle-toolbar">
                    <form className="lifecycle-search" onSubmit={submitSearch}>
                        <input
                            value={searchInput}
                            onChange={event => setSearchInput(event.target.value)}
                            placeholder="影片 ID"
                            aria-label="搜索影片 ID"
                        />
                        <button type="submit" title="搜索" aria-label="搜索"><Search size={16} /></button>
                    </form>
                    <select value={dimension} onChange={changeDimension} aria-label="状态维度">
                        {DIMENSIONS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
                    </select>
                    <select
                        value={status}
                        onChange={event => { setStatus(event.target.value); setOffset(0); }}
                        disabled={dimension === 'all'}
                        aria-label="状态值"
                    >
                        <option value="all">全部状态</option>
                        {selectedDimension.statuses.map(value => (
                            <option key={value} value={value}>{statusLabel(value)}</option>
                        ))}
                    </select>
                </div>

                {listLoading && movies.length > 0 && <div className="lifecycle-banner">正在刷新，保留当前结果</div>}
                {listError && (
                    <div className="lifecycle-banner lifecycle-error">
                        {listError}<button type="button" onClick={loadMovies}>重试</button>
                    </div>
                )}

                <div className="lifecycle-workspace">
                    <section className="lifecycle-movies" aria-label="影片生命周期列表">
                        <div className="lifecycle-list">
                            {movies.map(movie => (
                                <MovieLifecycleRow
                                    key={movie.movie_id}
                                    movie={movie}
                                    selected={movie.movie_id === selectedMovieId}
                                    onSelect={setSelectedMovieId}
                                />
                            ))}
                            {!listLoading && movies.length === 0 && !listError && (
                                <div className="lifecycle-empty">没有符合条件的生命周期记录</div>
                            )}
                        </div>
                        <div className="lifecycle-pagination">
                            <button
                                type="button"
                                className="lifecycle-icon-button"
                                onClick={() => setOffset(value => Math.max(0, value - PAGE_SIZE))}
                                disabled={offset === 0 || listLoading}
                                title="上一页"
                                aria-label="上一页"
                            ><ChevronLeft size={17} /></button>
                            <span>第 {Math.floor(offset / PAGE_SIZE) + 1} 页</span>
                            <button
                                type="button"
                                className="lifecycle-icon-button"
                                onClick={() => setOffset(value => value + PAGE_SIZE)}
                                disabled={movies.length < PAGE_SIZE || listLoading}
                                title="下一页"
                                aria-label="下一页"
                            ><ChevronRight size={17} /></button>
                        </div>
                    </section>

                    <section className="lifecycle-events" aria-label="生命周期事件时间线">
                        <div className="lifecycle-events-heading">
                            <div>
                                <h2>{selectedMovie?.title || selectedMovieId || '事件时间线'}</h2>
                                {selectedMovieId && <span>{selectedMovieId}</span>}
                            </div>
                            <span>{visibleEvents.length} 条事件</span>
                        </div>
                        {eventsError && (
                            <div className="lifecycle-banner lifecycle-error">
                                {eventsError}<button type="button" onClick={loadEvents}>重试</button>
                            </div>
                        )}
                        <div className="lifecycle-timeline">
                            {visibleEvents.map(event => <LifecycleEvent key={event.id} event={event} />)}
                            {!eventsLoading && selectedMovieId && visibleEvents.length === 0 && !eventsError && (
                                <div className="lifecycle-empty">该影片还没有事件记录</div>
                            )}
                            {!selectedMovieId && <div className="lifecycle-empty">请选择一条生命周期记录</div>}
                        </div>
                    </section>
                </div>
            </ContentContainer>
        </div>
    );
}

function MovieLifecycleRow({ movie, selected, onSelect }) {
    return (
        <button
            type="button"
            className={`lifecycle-movie-row${selected ? ' selected' : ''}`}
            onClick={() => onSelect(movie.movie_id)}
        >
            <div className="lifecycle-movie-title">
                <strong>{movie.title || movie.movie_id}</strong>
                <span>{movie.movie_id}</span>
            </div>
            <div className="lifecycle-status-grid">
                {STATUS_FIELDS.map(field => (
                    <span key={field.value}>
                        <small>{field.label}</small>
                        <b className={`tone-${statusTone(movie[field.value])}`}>{statusLabel(movie[field.value])}</b>
                    </span>
                ))}
            </div>
            <div className="lifecycle-row-footer">
                <span>{eventTypeLabel(movie.last_event)}</span>
                <time>{formatDate(movie.updated_at)}</time>
            </div>
        </button>
    );
}

function LifecycleEvent({ event }) {
    const transitions = getTransitions(event);
    const failed = event.execution_result === 'failed' || Boolean(event.error_summary);
    return (
        <article className={`lifecycle-event${failed ? ' failed' : ''}`}>
            <div className="lifecycle-event-dot">{failed && <AlertTriangle size={12} />}</div>
            <div className="lifecycle-event-heading">
                <h3>{eventTypeLabel(event.event_type)}</h3>
                <time>{formatDate(event.created_at)}</time>
            </div>
            {transitions.length > 0 && (
                <div className="lifecycle-transitions">
                    {transitions.map(transition => (
                        <div key={transition.dimension}>
                            <span>{dimensionLabel(transition.dimension)}</span>
                            <b className={`tone-${statusTone(transition.oldStatus)}`}>{statusLabel(transition.oldStatus)}</b>
                            <ArrowRight size={13} />
                            <b className={`tone-${statusTone(transition.newStatus)}`}>{statusLabel(transition.newStatus)}</b>
                        </div>
                    ))}
                </div>
            )}
            <div className="lifecycle-event-meta">
                {event.source && <span>来源 {event.source}</span>}
                {event.execution_result && <span>结果 {resultLabel(event.execution_result)}</span>}
            </div>
            {event.reason && <p>{event.reason}</p>}
            {event.error_summary && <p className="lifecycle-event-error">{event.error_summary}</p>}
        </article>
    );
}

function getTransitions(event) {
    const dimensions = String(event.status_dimension || '').split(',').filter(Boolean);
    if (dimensions.length === 0) return [];
    const oldMap = parseStatusObject(event.old_status);
    const newMap = parseStatusObject(event.new_status);
    return dimensions.map(dimension => ({
        dimension,
        oldStatus: dimensions.length === 1 ? event.old_status : oldMap[dimension],
        newStatus: dimensions.length === 1 ? event.new_status : newMap[dimension],
    }));
}

function parseStatusObject(value) {
    if (!value || typeof value !== 'string') return {};
    try {
        const parsed = JSON.parse(value);
        return parsed && typeof parsed === 'object' ? parsed : {};
    } catch {
        return {};
    }
}

function dimensionLabel(value) {
    return DIMENSIONS.find(item => item.value === value)?.label || value || '状态';
}

function statusLabel(value) {
    const labels = {
        unknown: '未知', none: '无', pending: '待处理', discovered: '已发现', candidate: '候选',
        approved: '已批准', rejected: '已拒绝', blocked: '已阻止', queued: '已排队',
        waiting_external_service: '等待外部服务', downloaded: '已下载', delivered: '已投放',
        failed: '失败', organizing: '整理中', completed: '已完成', indexed: '已入库',
        watched: '已观看', unwatched: '未观看', favorite: '已收藏', not_favorite: '未收藏',
    };
    return labels[value] || value || '-';
}

function statusTone(value) {
    if (['failed', 'blocked', 'rejected'].includes(value)) return 'danger';
    if (['discovered', 'approved', 'downloaded', 'delivered', 'completed', 'indexed', 'watched', 'favorite'].includes(value)) return 'success';
    if (['candidate', 'queued', 'waiting_external_service', 'organizing', 'pending'].includes(value)) return 'warning';
    return 'muted';
}

function eventTypeLabel(value) {
    const labels = {
        content_discovered: '发现内容',
        download_candidate_created: '生成下载候选',
        download_candidate_approved: '批准下载候选',
        download_candidate_rejected: '拒绝下载候选',
        download_queued: '进入下载队列',
        qb_waiting_external_service: '等待 qBittorrent',
        qb_download_completed: '下载完成',
        organize_started: '开始整理',
        organize_completed: '整理完成',
        organize_failed: '整理失败',
        jellyfin_indexed: 'Jellyfin 入库',
        cleanup_candidate_created: '生成清理候选',
    };
    return labels[value] || String(value || '事件').replaceAll('_', ' ');
}

function resultLabel(value) {
    return { success: '成功', failed: '失败', recorded: '已记录', blocked: '已阻止' }[value] || value;
}

function formatDate(value) {
    if (value === undefined || value === null || value === '') return '-';
    const numeric = Number(value);
    const date = Number.isFinite(numeric)
        ? new Date(numeric < 1e12 ? numeric * 1000 : numeric)
        : new Date(value);
    if (Number.isNaN(date.getTime())) return '-';
    return date.toLocaleString('zh-CN', { hour12: false });
}

export default LifecyclePage;
