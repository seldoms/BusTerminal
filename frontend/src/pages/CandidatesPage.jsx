import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
    CheckCircle,
    ChevronLeft,
    ChevronRight,
    Download,
    Film,
    RefreshCw,
    UserRoundPlus,
    Trash2,
    XCircle,
} from 'lucide-react';
import { Link } from 'react-router-dom';
import { getApiErrorMessage, getProxyImageUrl, logSafeError, movieApi } from '../api';
import ContentContainer from '../components/ContentContainer';
import { useToast } from '../components/Toast';
import './CandidatesPage.css';

const PAGE_SIZE = 50;

const STATUS_OPTIONS = [
    { value: 'candidate', label: '待审核' },
    { value: 'approved', label: '已批准' },
    { value: 'rejected', label: '已拒绝' },
    { value: 'all', label: '全部' },
];

const TYPE_OPTIONS = [
    { value: 'all', label: '全部类型' },
    { value: 'subscription_download', label: '下载候选' },
    { value: 'favorite_actor', label: '订阅候选' },
];

const TYPE_META = {
    subscription_download: {
        label: '下载候选',
        action: '进入本地队列',
        icon: Download,
    },
    favorite_actor: {
        label: '订阅候选',
        action: '加入订阅',
        icon: UserRoundPlus,
    },
};

function CandidatesPage() {
    const [candidates, setCandidates] = useState([]);
    const [status, setStatus] = useState('candidate');
    const [candidateType, setCandidateType] = useState('all');
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
            const response = await movieApi.getAutomationCandidates({
                status: status === 'all' ? '' : status,
                candidate_type: candidateType === 'all' ? '' : candidateType,
                limit: PAGE_SIZE,
                offset,
            });
            if (requestSeq !== requestSeqRef.current) return;
            if (!response.success) {
                throw new Error(getApiErrorMessage(response, '候选加载失败'));
            }
            setCandidates(Array.isArray(response.data) ? response.data : []);
        } catch (loadError) {
            if (requestSeq !== requestSeqRef.current) return;
            logSafeError('Candidate center load failed:', loadError);
            setError(getApiErrorMessage(loadError, '候选加载失败'));
        } finally {
            if (requestSeq === requestSeqRef.current) setLoading(false);
        }
    }, [candidateType, offset, status]);

    useEffect(() => {
        loadCandidates();
    }, [loadCandidates]);

    const counts = useMemo(() => ({
        total: candidates.length,
        downloads: candidates.filter(item => item.candidate_type === 'subscription_download').length,
        subscriptions: candidates.filter(item => item.candidate_type === 'favorite_actor').length,
    }), [candidates]);

    const selectStatus = (value) => {
        setStatus(value);
        setOffset(0);
        setDecision(null);
    };

    const selectType = (value) => {
        setCandidateType(value);
        setOffset(0);
        setDecision(null);
    };

    const openDecision = (candidate, action) => {
        setDecision({
            candidate,
            action,
            reason: action === 'approve' ? '用户在候选中心批准' : '',
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
                ? await movieApi.approveAutomationCandidate(decision.candidate.id, payload)
                : await movieApi.rejectAutomationCandidate(decision.candidate.id, payload);
            if (!response.success) {
                showToast(getApiErrorMessage(response, '候选处理失败'), 'error');
                return;
            }

            if (decision.action === 'approve') {
                const queued = response.application?.action === 'queue_download';
                showToast(queued ? '已进入本地下载队列' : '候选已批准', 'success');
            } else {
                showToast('候选已拒绝', 'success');
            }
            setDecision(null);
            await loadCandidates();
        } catch (actionError) {
            logSafeError('Candidate decision failed:', actionError);
            showToast(getApiErrorMessage(actionError, '候选处理失败'), 'error');
        } finally {
            setSaving(false);
        }
    };

    const page = Math.floor(offset / PAGE_SIZE) + 1;
    const hasNext = candidates.length === PAGE_SIZE;

    return (
        <div className="candidates-page">
            <ContentContainer className="candidates-container">
                <header className="candidates-header">
                    <div>
                        <h1>候选中心</h1>
                        <div className="candidate-counts" aria-label="当前页候选统计">
                            <span>{counts.total} 条</span>
                            <span><Download size={14} /> {counts.downloads}</span>
                            <span><UserRoundPlus size={14} /> {counts.subscriptions}</span>
                        </div>
                    </div>
                    <div className="candidate-header-actions">
                        <Link to="/cleanup" className="candidate-link-button">
                            <Trash2 size={16} /> 清理候选
                        </Link>
                        <button
                            type="button"
                            className="candidate-icon-button"
                            onClick={loadCandidates}
                            disabled={loading}
                            title="刷新候选"
                            aria-label="刷新候选"
                        >
                            <RefreshCw size={18} className={loading ? 'is-spinning' : ''} />
                        </button>
                    </div>
                </header>

                <div className="candidate-filters">
                    <div className="candidate-segments" aria-label="候选状态">
                        {STATUS_OPTIONS.map(option => (
                            <button
                                type="button"
                                key={option.value}
                                className={status === option.value ? 'active' : ''}
                                onClick={() => selectStatus(option.value)}
                            >
                                {option.label}
                            </button>
                        ))}
                    </div>
                    <div className="candidate-segments" aria-label="候选类型">
                        {TYPE_OPTIONS.map(option => (
                            <button
                                type="button"
                                key={option.value}
                                className={candidateType === option.value ? 'active' : ''}
                                onClick={() => selectType(option.value)}
                            >
                                {option.label}
                            </button>
                        ))}
                    </div>
                </div>

                {error && (
                    <div className="candidate-banner candidate-error">
                        <span>{error}</span>
                        <button type="button" onClick={loadCandidates}>重试</button>
                    </div>
                )}
                {loading && candidates.length > 0 && (
                    <div className="candidate-banner">正在刷新...</div>
                )}

                <div className="candidate-list" aria-busy={loading}>
                    {candidates.map(candidate => (
                        <CandidateRow
                            key={candidate.id}
                            candidate={candidate}
                            busy={saving && decision?.candidate?.id === candidate.id}
                            decision={decision?.candidate?.id === candidate.id ? decision : null}
                            onDecision={openDecision}
                            onReasonChange={(reason) => setDecision(current => ({ ...current, reason }))}
                            onSubmit={submitDecision}
                            onCancel={() => setDecision(null)}
                        />
                    ))}
                    {!loading && !error && candidates.length === 0 && (
                        <div className="candidate-empty">当前筛选条件下没有候选</div>
                    )}
                    {loading && candidates.length === 0 && (
                        <div className="candidate-empty">正在加载候选...</div>
                    )}
                </div>

                <footer className="candidate-pagination">
                    <button
                        type="button"
                        className="candidate-icon-button"
                        disabled={offset === 0 || loading}
                        onClick={() => setOffset(value => Math.max(0, value - PAGE_SIZE))}
                        title="上一页"
                        aria-label="上一页"
                    >
                        <ChevronLeft size={18} />
                    </button>
                    <span>第 {page} 页</span>
                    <button
                        type="button"
                        className="candidate-icon-button"
                        disabled={!hasNext || loading}
                        onClick={() => setOffset(value => value + PAGE_SIZE)}
                        title="下一页"
                        aria-label="下一页"
                    >
                        <ChevronRight size={18} />
                    </button>
                </footer>
            </ContentContainer>
        </div>
    );
}

function CandidateRow({ candidate, busy, decision, onDecision, onReasonChange, onSubmit, onCancel }) {
    const meta = TYPE_META[candidate.candidate_type] || {
        label: candidate.candidate_type || '其他候选',
        action: '批准',
        icon: Film,
    };
    const Icon = meta.icon;
    const payload = candidate.payload && typeof candidate.payload === 'object' ? candidate.payload : {};
    const rules = Array.isArray(payload.matched_rules) ? payload.matched_rules : [];
    const avatar = candidate.subject_profile?.avatar;
    const isPending = candidate.status === 'candidate';

    return (
        <article className="candidate-row">
            <div className={`candidate-visual ${avatar ? 'has-image' : ''}`}>
                {avatar ? (
                    <img
                        src={getProxyImageUrl(avatar, { w: 160 })}
                        alt=""
                        loading="lazy"
                        decoding="async"
                    />
                ) : (
                    <Icon size={20} />
                )}
            </div>

            <div className="candidate-body">
                <div className="candidate-title-row">
                    <h2>{candidate.subject_name || candidate.source_title || candidate.subject_id}</h2>
                    <span className={`candidate-status status-${candidate.status}`}>{statusLabel(candidate.status)}</span>
                </div>
                <div className="candidate-meta">
                    <span>{meta.label}</span>
                    <span>{candidate.source || '未知来源'}</span>
                    {candidate.source_movie_id && <span>{candidate.source_movie_id}</span>}
                    <span>置信度 {formatConfidence(candidate.confidence)}</span>
                </div>
                <p className="candidate-reason">{candidate.reason || '未提供原因'}</p>
                {rules.length > 0 && (
                    <div className="candidate-rules">
                        {rules.map(rule => <span key={rule}>{rule}</span>)}
                    </div>
                )}

                {decision && (
                    <div className="candidate-decision-form">
                        <input
                            type="text"
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
                        <button type="button" className="candidate-command primary" onClick={onSubmit} disabled={busy}>
                            <CheckCircle size={15} /> 确认
                        </button>
                        <button type="button" className="candidate-icon-button" onClick={onCancel} disabled={busy} title="取消" aria-label="取消">
                            <XCircle size={17} />
                        </button>
                    </div>
                )}
            </div>

            {isPending && !decision && (
                <div className="candidate-actions">
                    <button
                        type="button"
                        className="candidate-command primary"
                        onClick={() => onDecision(candidate, 'approve')}
                    >
                        <CheckCircle size={15} /> {meta.action}
                    </button>
                    <button
                        type="button"
                        className="candidate-command"
                        onClick={() => onDecision(candidate, 'reject')}
                    >
                        <XCircle size={15} /> 拒绝
                    </button>
                </div>
            )}
        </article>
    );
}

function statusLabel(status) {
    return {
        candidate: '待审核',
        approved: '已批准',
        rejected: '已拒绝',
    }[status] || status || '未知';
}

function formatConfidence(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return '-';
    return `${Math.round(numeric * 100)}%`;
}

export default CandidatesPage;
