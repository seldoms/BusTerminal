import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
    AlertTriangle,
    CheckCircle2,
    Clock3,
    Database,
    PlayCircle,
    RefreshCw,
    RotateCcw,
    ServerOff,
} from 'lucide-react';
import { getApiErrorMessage, logSafeError, movieApi } from '../api';
import ContentContainer from '../components/ContentContainer';
import './TasksPage.css';

const STATUS_OPTIONS = [
    { value: 'all', label: '全部' },
    { value: 'waiting_external_service', label: '等待重试' },
    { value: 'needs_attention', label: '需处理' },
    { value: 'completed', label: '已完成' },
];

function TasksPage() {
    const [taskStatus, setTaskStatus] = useState({ tasks: [] });
    const [externalTasks, setExternalTasks] = useState([]);
    const [status, setStatus] = useState('all');
    const [loading, setLoading] = useState(true);
    const [errors, setErrors] = useState({});
    const requestSeqRef = useRef(0);

    const loadTasks = useCallback(async () => {
        const requestSeq = ++requestSeqRef.current;
        setLoading(true);
        setErrors({});
        const params = status === 'all' ? { limit: 100 } : { status, limit: 100 };
        const [statusResult, externalResult] = await Promise.allSettled([
            movieApi.getTaskStatus(),
            movieApi.getExternalTasks(params),
        ]);
        if (requestSeq !== requestSeqRef.current) return;

        const nextErrors = {};
        if (statusResult.status === 'fulfilled' && statusResult.value?.success) {
            setTaskStatus(statusResult.value.data || { tasks: [] });
        } else {
            const reason = statusResult.status === 'rejected' ? statusResult.reason : statusResult.value;
            logSafeError('Task registry load failed:', reason);
            nextErrors.registry = getApiErrorMessage(reason, '后台任务状态加载失败');
        }

        if (externalResult.status === 'fulfilled' && externalResult.value?.success) {
            setExternalTasks(Array.isArray(externalResult.value.data) ? externalResult.value.data : []);
        } else {
            const reason = externalResult.status === 'rejected' ? externalResult.reason : externalResult.value;
            logSafeError('External task queue load failed:', reason);
            nextErrors.external = getApiErrorMessage(reason, '持久化任务加载失败');
        }
        setErrors(nextErrors);
        setLoading(false);
    }, [status]);

    useEffect(() => {
        loadTasks();
    }, [loadTasks]);

    const summary = useMemo(() => {
        const registryTasks = Array.isArray(taskStatus.tasks) ? taskStatus.tasks : [];
        const externalQueue = taskStatus.external_queue?.available ? taskStatus.external_queue : {};
        const metadataQueue = taskStatus.metadata_queue?.available ? taskStatus.metadata_queue : {};
        return {
            running: registryTasks.filter(task => task.status === 'running').length,
            waiting: Number(externalQueue.waiting || 0)
                + Number(metadataQueue.waiting || 0)
                + registryTasks.filter(task => Number(task.consecutive_error_count || 0) > 0).length,
            attention: Number(externalQueue.needs_attention || 0)
                + Number(metadataQueue.needs_attention || 0)
                + registryTasks.filter(task => task.status === 'failed').length,
            completed: Number(externalQueue.by_status?.completed || 0)
                + Number(metadataQueue.completed || 0),
        };
    }, [taskStatus]);

    const registryTasks = Array.isArray(taskStatus.tasks) ? taskStatus.tasks : [];

    return (
        <div className="tasks-page">
            <ContentContainer className="tasks-container">
                <header className="tasks-header">
                    <div>
                        <h1>任务中心</h1>
                        <p>后台任务与离线服务重试状态</p>
                    </div>
                    <div className="tasks-header-actions">
                        <button
                            type="button"
                            className="task-icon-button"
                            onClick={loadTasks}
                            disabled={loading}
                            title="刷新任务状态"
                            aria-label="刷新任务状态"
                        >
                            <RefreshCw size={17} className={loading ? 'is-spinning' : ''} />
                        </button>
                    </div>
                </header>

                <section className="task-summary" aria-label="任务统计">
                    <SummaryItem icon={PlayCircle} label="运行中" value={summary.running} />
                    <SummaryItem icon={RotateCcw} label="等待重试" value={summary.waiting} />
                    <SummaryItem icon={AlertTriangle} label="需处理" value={summary.attention} danger />
                    <SummaryItem icon={CheckCircle2} label="已完成" value={summary.completed} />
                </section>

                {loading && (registryTasks.length > 0 || externalTasks.length > 0) && (
                    <div className="task-banner">正在刷新，保留当前结果</div>
                )}
                {(errors.registry || errors.external) && (
                    <div className="task-banner task-error">
                        {[errors.registry, errors.external].filter(Boolean).join('；')}
                        <button type="button" onClick={loadTasks}>重试</button>
                    </div>
                )}

                <section className="task-section">
                    <div className="task-section-heading">
                        <div>
                            <h2>运行任务</h2>
                            <span>{registryTasks.length} 项</span>
                        </div>
                    </div>
                    <div className="task-list">
                        {registryTasks.map(task => <RegistryTaskRow key={task.name} task={task} />)}
                        {!loading && registryTasks.length === 0 && !errors.registry && (
                            <div className="task-empty">当前没有已注册的后台任务</div>
                        )}
                    </div>
                </section>

                <section className="task-section">
                    <div className="task-section-heading task-external-heading">
                        <div>
                            <h2>持久化重试队列</h2>
                            <span>{externalTasks.length} 项</span>
                        </div>
                        <div className="task-segments" aria-label="持久化任务状态">
                            {STATUS_OPTIONS.map(option => (
                                <button
                                    key={option.value}
                                    type="button"
                                    className={status === option.value ? 'active' : ''}
                                    onClick={() => setStatus(option.value)}
                                >
                                    {option.label}
                                </button>
                            ))}
                        </div>
                    </div>
                    <div className="task-list">
                        {externalTasks.map(task => <ExternalTaskRow key={task.id} task={task} />)}
                        {!loading && externalTasks.length === 0 && !errors.external && (
                            <div className="task-empty">当前筛选条件下没有持久化任务</div>
                        )}
                    </div>
                </section>
            </ContentContainer>
        </div>
    );
}

function SummaryItem({ icon: Icon, label, value, danger = false }) {
    return (
        <div className={`task-summary-item${danger ? ' danger' : ''}`}>
            <Icon size={17} />
            <strong>{Number(value || 0).toLocaleString()}</strong>
            <span>{label}</span>
        </div>
    );
}

function RegistryTaskRow({ task }) {
    const retryCount = Number(task.consecutive_error_count || 0);
    const recoveredCount = Number(task.last_recovery?.retry_count || 0);
    return (
        <article className="task-row">
            <div className={`task-state-icon status-${task.status || 'registered'}`}>
                {task.status === 'failed' ? <AlertTriangle size={18} /> : <Database size={18} />}
            </div>
            <div className="task-body">
                <div className="task-title-row">
                    <h3>{task.description || task.name}</h3>
                    <span className={`task-status status-${task.status || 'registered'}`}>
                        {statusLabel(task.status)}
                    </span>
                </div>
                <div className="task-meta">
                    <span>{task.category || 'background'}</span>
                    {task.interval && <span>{task.interval}</span>}
                    {task.last_success_at && <span>最近成功 {formatDate(task.last_success_at)}</span>}
                </div>
                {retryCount > 0 && (
                    <p className="task-error-text">
                        连续失败 {retryCount} 次
                        {task.last_error?.message ? `：${task.last_error.message}` : '，后台仍会按计划重试'}
                    </p>
                )}
                {recoveredCount > 0 && retryCount === 0 && (
                    <p className="task-recovery">最近一次在重试 {recoveredCount} 次后恢复</p>
                )}
            </div>
        </article>
    );
}

function ExternalTaskRow({ task }) {
    const retryCount = Number(task.retry_count || 0);
    const completedAfterRetry = task.status === 'completed' && retryCount > 0;
    return (
        <article className="task-row">
            <div className={`task-state-icon status-${task.status || 'pending'}`}>
                {task.status === 'needs_attention' ? <AlertTriangle size={18} /> : <ServerOff size={18} />}
            </div>
            <div className="task-body">
                <div className="task-title-row">
                    <h3>{taskTypeLabel(task.task_type)}</h3>
                    <span className={`task-status status-${task.status || 'pending'}`}>
                        {statusLabel(task.status)}
                    </span>
                </div>
                <div className="task-meta">
                    {task.source && <span>{task.source}</span>}
                    <span>尝试 {retryCount}/{Number(task.max_retries || 0)}</span>
                    <span>更新于 {formatDate(task.updated_at)}</span>
                </div>
                {task.last_error && task.status !== 'completed' && (
                    <p className="task-error-text">最近错误：{task.last_error}</p>
                )}
                {task.next_retry_at && (
                    <p className="task-next-retry"><Clock3 size={14} /> 下次重试 {formatDate(task.next_retry_at)}</p>
                )}
                {completedAfterRetry && (
                    <p className="task-recovery">已在重试 {retryCount} 次后成功</p>
                )}
                {task.status === 'completed' && retryCount === 0 && (
                    <p className="task-recovery">一次执行成功</p>
                )}
            </div>
        </article>
    );
}

function taskTypeLabel(value) {
    const labels = {
        jellyfin_automation_sync: 'Jellyfin 自动化同步',
        actress_metadata: '演员元数据补全',
    };
    return labels[value] || String(value || '外部任务').replaceAll('_', ' ');
}

function statusLabel(value) {
    const labels = {
        registered: '已注册',
        running: '运行中',
        stopped: '已停止',
        failed: '失败',
        pending: '排队中',
        processing: '处理中',
        waiting_external_service: '等待外部服务',
        needs_attention: '需人工处理',
        completed: '已完成',
    };
    return labels[value] || value || '未知';
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

export default TasksPage;
