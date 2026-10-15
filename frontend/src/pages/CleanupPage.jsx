import React, { useCallback, useEffect, useRef, useState } from 'react';
import { CheckCircle, ChevronLeft, ChevronRight, RefreshCw, ShieldCheck, Trash2, XCircle } from 'lucide-react';
import { Link } from 'react-router-dom';
import { getApiErrorMessage, logSafeError, movieApi } from '../api';
import ContentContainer from '../components/ContentContainer';
import { useToast } from '../components/Toast';
import './CleanupPage.css';

const PAGE_SIZE = 50;
const STATUS_OPTIONS = [
    { value: 'candidate', label: '待审核' },
    { value: 'approved', label: '已批准' },
    { value: 'rejected', label: '已拒绝' },
    { value: 'all', label: '全部' },
];

function CleanupPage() {
    const [candidates, setCandidates] = useState([]);
    const [status, setStatus] = useState('candidate');
    const [offset, setOffset] = useState(0);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [decision, setDecision] = useState(null);
    const [saving, setSaving] = useState(false);
    const requestSeqRef = useRef(0);
    const { showToast } = useToast();

    const loadCandidates = useCallback(async () => {
        const requestSeq = ++requestSeqRef.current;
        setLoading(true);
        setError('');
        try {
            const response = await movieApi.getCleanupCandidates({
                status: status === 'all' ? '' : status,
                limit: PAGE_SIZE,
                offset,
            });
            if (requestSeq !== requestSeqRef.current) return;
            if (!response?.success) throw new Error(getApiErrorMessage(response, '清理候选加载失败'));
            setCandidates(Array.isArray(response.data) ? response.data : []);
        } catch (loadError) {
            if (requestSeq !== requestSeqRef.current) return;
            logSafeError('Cleanup candidates load failed:', loadError);
            setError(getApiErrorMessage(loadError, '清理候选加载失败'));
        } finally {
            if (requestSeq === requestSeqRef.current) setLoading(false);
        }
    }, [offset, status]);

    useEffect(() => {
        loadCandidates();
    }, [loadCandidates]);

    const openDecision = (candidate, action) => {
        setDecision({
            candidate,
            action,
            reason: action === 'approve' ? '人工审核通过清理候选' : '',
        });
    };

    const submitDecision = async () => {
        const reason = decision?.reason?.trim();
        if (!decision || reason.length < 2) {
            showToast('请填写处理原因', 'warning');
            return;
        }
        setSaving(true);
        try {
            const payload = { actor: 'user', reason };
            const response = decision.action === 'approve'
                ? await movieApi.approveCleanupCandidate(decision.candidate.id, payload)
                : await movieApi.rejectCleanupCandidate(decision.candidate.id, payload);
            if (!response?.success) {
                showToast(getApiErrorMessage(response, '清理候选处理失败'), 'error');
                return;
            }
            showToast(
                decision.action === 'approve' ? '候选已批准，未执行删除' : '清理候选已拒绝',
                'success',
            );
            setDecision(null);
            await loadCandidates();
        } catch (actionError) {
            logSafeError('Cleanup candidate decision failed:', actionError);
            showToast(getApiErrorMessage(actionError, '清理候选处理失败'), 'error');
        } finally {
            setSaving(false);
        }
    };

    return (
        <div className="cleanup-page">
            <ContentContainer className="cleanup-container">
                <header className="cleanup-header">
                    <div>
                        <h1>清理候选</h1>
                        <span>{candidates.length} 条</span>
                    </div>
                    <div className="cleanup-header-actions">
                        <Link to="/candidates" className="cleanup-link-button">
                            <ShieldCheck size={16} /> 候选中心
                        </Link>
                        <button
                            type="button"
                            className="cleanup-icon-button"
                            onClick={loadCandidates}
                            disabled={loading}
                            title="刷新清理候选"
                            aria-label="刷新清理候选"
                        >
                            <RefreshCw size={17} className={loading ? 'is-spinning' : ''} />
                        </button>
                    </div>
                </header>

                <div className="cleanup-filters" aria-label="清理候选状态">
                    {STATUS_OPTIONS.map(option => (
                        <button
                            key={option.value}
                            type="button"
                            className={status === option.value ? 'active' : ''}
                            onClick={() => { setStatus(option.value); setOffset(0); setDecision(null); }}
                        >
                            {option.label}
                        </button>
                    ))}
                </div>

                {loading && candidates.length > 0 && <div className="cleanup-banner">正在刷新，保留当前结果</div>}
                {error && (
                    <div className="cleanup-banner cleanup-error">
                        {error}<button type="button" onClick={loadCandidates}>重试</button>
                    </div>
                )}

                <div className="cleanup-list">
                    {candidates.map(candidate => (
                        <CleanupCandidateRow
                            key={candidate.id}
                            candidate={candidate}
                            decision={decision?.candidate?.id === candidate.id ? decision : null}
                            busy={saving && decision?.candidate?.id === candidate.id}
                            onDecision={openDecision}
                            onReasonChange={reason => setDecision(current => ({ ...current, reason }))}
                            onSubmit={submitDecision}
                            onCancel={() => setDecision(null)}
                        />
                    ))}
                    {!loading && !error && candidates.length === 0 && (
                        <div className="cleanup-empty">当前筛选条件下没有清理候选</div>
                    )}
                    {loading && candidates.length === 0 && <div className="cleanup-empty">正在加载清理候选</div>}
                </div>

                <footer className="cleanup-pagination">
                    <button
                        type="button"
                        className="cleanup-icon-button"
                        disabled={offset === 0 || loading}
                        onClick={() => setOffset(value => Math.max(0, value - PAGE_SIZE))}
                        title="上一页"
                        aria-label="上一页"
                    ><ChevronLeft size={17} /></button>
                    <span>第 {Math.floor(offset / PAGE_SIZE) + 1} 页</span>
                    <button
                        type="button"
                        className="cleanup-icon-button"
                        disabled={candidates.length < PAGE_SIZE || loading}
                        onClick={() => setOffset(value => value + PAGE_SIZE)}
                        title="下一页"
                        aria-label="下一页"
                    ><ChevronRight size={17} /></button>
                </footer>
            </ContentContainer>
        </div>
    );
}

function CleanupCandidateRow({ candidate, decision, busy, onDecision, onReasonChange, onSubmit, onCancel }) {
    const progress = Math.max(0, Math.min(Number(candidate.progress || 0), 100));
    const isPending = candidate.status === 'candidate';
    return (
        <article className="cleanup-row">
            <div className="cleanup-visual"><Trash2 size={19} /></div>
            <div className="cleanup-body">
                <div className="cleanup-title-row">
                    <h2>{candidate.title || candidate.movie_id || candidate.jellyfin_id}</h2>
                    <span className={`cleanup-status status-${candidate.status}`}>{statusLabel(candidate.status)}</span>
                </div>
                <div className="cleanup-meta">
                    {candidate.movie_id && <span>{candidate.movie_id}</span>}
                    <span>{candidate.source || 'cleanup_scan'}</span>
                    <span>{candidate.is_favorite ? '已收藏' : '未收藏'}</span>
                    <span>发现于 {formatDate(candidate.last_seen_at)}</span>
                </div>
                <div className="cleanup-progress">
                    <span>播放进度 {progress}%</span>
                    <div><i style={{ width: `${progress}%` }} /></div>
                </div>
                <p className="cleanup-reason">{candidate.reason || '未提供候选原因'}</p>
                {candidate.decision_reason && (
                    <p className="cleanup-decision-record">
                        {candidate.decision_actor || 'user'}：{candidate.decision_reason}
                    </p>
                )}
                {decision && (
                    <div className="cleanup-decision-form">
                        <input
                            value={decision.reason}
                            maxLength={500}
                            autoFocus
                            placeholder="处理原因"
                            onChange={event => onReasonChange(event.target.value)}
                            onKeyDown={event => {
                                if (event.key === 'Enter') onSubmit();
                                if (event.key === 'Escape') onCancel();
                            }}
                        />
                        <button type="button" className="cleanup-command primary" onClick={onSubmit} disabled={busy}>
                            <CheckCircle size={15} /> 确认
                        </button>
                        <button type="button" className="cleanup-icon-button" onClick={onCancel} disabled={busy} title="取消" aria-label="取消">
                            <XCircle size={17} />
                        </button>
                    </div>
                )}
            </div>
            {isPending && !decision && (
                <div className="cleanup-actions">
                    <button type="button" className="cleanup-command primary" onClick={() => onDecision(candidate, 'approve')}>
                        <CheckCircle size={15} /> 批准候选
                    </button>
                    <button type="button" className="cleanup-command" onClick={() => onDecision(candidate, 'reject')}>
                        <XCircle size={15} /> 拒绝
                    </button>
                </div>
            )}
        </article>
    );
}

function statusLabel(value) {
    return { candidate: '待审核', approved: '已批准', rejected: '已拒绝', deleted: '已删除' }[value] || value || '未知';
}

function formatDate(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return '-';
    return new Date(numeric * 1000).toLocaleString('zh-CN', { hour12: false });
}

export default CleanupPage;
