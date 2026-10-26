import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Ban, RefreshCw, Save, Sparkles } from 'lucide-react';
import { getApiErrorMessage, logSafeError, movieApi } from '../../api';
import { useToast } from '../../components/Toast';

/**
 * 全局关键词屏蔽。
 *
 * 这套词是「全链路闸门」：命中后既不出现在任何展示页面（首页 / 发现 / Jable /
 * 搜索 / 生命周期 / 详情），也不进下载队列（手动、订阅、自动化同一个闸门）。
 * 匹配口径由后端统一：标题 / 番号 / 演员名，去分隔符 + 忽略大小写做子串匹配，
 * 所以填 `vr` 能同时挡住 `VR`、`vr-`、`ＶＲ`、`HD VR`。
 */
export default function SettingsBlockKeywordsTab() {
    const { showToast } = useToast();
    const [savedKeywords, setSavedKeywords] = useState([]);
    const [draft, setDraft] = useState('');
    const [recommended, setRecommended] = useState([]);
    const [maxKeywords, setMaxKeywords] = useState(100);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [previewing, setPreviewing] = useState(false);
    const [preview, setPreview] = useState(null);

    const loadKeywords = useCallback(async () => {
        setLoading(true);
        try {
            const data = await movieApi.getBlockKeywords();
            const list = Array.isArray(data?.keywords) ? data.keywords : [];
            setSavedKeywords(list);
            setDraft(list.join(', '));
            setRecommended(Array.isArray(data?.defaults) ? data.defaults : []);
            if (data?.max_keywords) setMaxKeywords(data.max_keywords);
        } catch (err) {
            logSafeError('加载屏蔽词失败:', err);
            showToast(getApiErrorMessage(err, '加载屏蔽词失败'), 'error');
        } finally {
            setLoading(false);
        }
    }, [showToast]);

    useEffect(() => {
        loadKeywords();
    }, [loadKeywords]);

    // 输入框里已经写了什么就展示什么（不依赖保存），让用户随时看清最终生效词
    const draftKeywords = useMemo(() => {
        const parts = String(draft || '')
            .split(/[\r\n,，;；、|]+/)
            .map((item) => item.trim())
            .filter(Boolean);
        return Array.from(new Set(parts));
    }, [draft]);

    const dirty = draftKeywords.join('|') !== savedKeywords.join('|');

    const handleSave = async () => {
        setSaving(true);
        try {
            const res = await movieApi.saveBlockKeywords({ block_keywords: draft });
            if (res?.success) {
                const list = Array.isArray(res.data?.keywords) ? res.data.keywords : [];
                setSavedKeywords(list);
                setDraft(list.join(', '));
                setPreview(null);
                showToast(
                    list.length > 0 ? `屏蔽词已生效（${list.length} 个）` : '已清空屏蔽词，不再拦截',
                    'success',
                );
            } else {
                showToast(getApiErrorMessage(res, '保存失败'), 'error');
            }
        } catch (err) {
            logSafeError('保存屏蔽词失败:', err);
            showToast(getApiErrorMessage(err, '保存失败'), 'error');
        } finally {
            setSaving(false);
        }
    };

    const handlePreview = async () => {
        setPreviewing(true);
        try {
            const res = await movieApi.previewBlockKeywords({ block_keywords: draft, limit: 20 });
            if (res?.success) {
                setPreview({
                    keywords: Array.isArray(res.data?.keywords) ? res.data.keywords : [],
                    items: Array.isArray(res.data?.items) ? res.data.items : [],
                });
            } else {
                showToast(getApiErrorMessage(res, '试算失败'), 'error');
            }
        } catch (err) {
            logSafeError('试算屏蔽词失败:', err);
            showToast(getApiErrorMessage(err, '试算失败'), 'error');
        } finally {
            setPreviewing(false);
        }
    };

    const handleFillRecommended = () => {
        const merged = Array.from(new Set([...draftKeywords, ...recommended])).slice(0, maxKeywords);
        setDraft(merged.join(', '));
        setPreview(null);
    };

    return (
        <div className="settings-tab-pane">
            <div className="settings-card">
                <div className="card-body">
                    <div className="control-label-row">
                        <label className="control-label">关键词屏蔽（全局）</label>
                        <span className="control-value-badge">
                            {loading ? '读取中…' : `生效中 ${savedKeywords.length} 个`}
                        </span>
                    </div>

                    <div className="control-help-text" style={{ marginBottom: 14, lineHeight: 1.8 }}>
                        命中的条目在<strong>展示</strong>与<strong>下载</strong>两条链路上都会被拦掉：
                        不进首页 / 发现 / Jable / 搜索 / 生命周期列表，点详情也会被挡，手动下载、订阅与自动化同样拒收。
                        匹配范围是标题、番号、演员名，去分隔符并忽略大小写 —— 填 <code>vr</code> 即可同时挡住
                        <code>VR</code>、<code>vr-</code>、<code>ＶＲ</code>、<code>HD VR</code>。
                        用逗号或换行分隔，最多 {maxKeywords} 个。
                    </div>

                    <div className="form-group">
                        <label className="control-label">屏蔽词列表</label>
                        <textarea
                            className="settings-input"
                            rows={6}
                            value={draft}
                            onChange={(event) => {
                                setDraft(event.target.value);
                                setPreview(null);
                            }}
                            placeholder={'vr\n换个片名或番号，同样会被拦掉'}
                            spellCheck={false}
                            style={{ resize: 'vertical', fontFamily: 'inherit', lineHeight: 1.7 }}
                        />
                        <div className="control-help-text">
                            {draftKeywords.length > 0
                                ? `将生效 ${draftKeywords.length} 个：${draftKeywords.join('、')}`
                                : '当前为空 —— 不会拦截任何内容'}
                            {dirty && <span style={{ marginLeft: 8 }}>（有未保存的改动）</span>}
                        </div>
                    </div>

                    <div className="actions-button-group">
                        <button
                            type="button"
                            className="btn btn-primary"
                            onClick={handleSave}
                            disabled={saving || loading || !dirty}
                        >
                            <Save size={15} /> {saving ? '保存中...' : '保存并生效'}
                        </button>
                        <button
                            type="button"
                            className="btn btn-secondary"
                            onClick={handlePreview}
                            disabled={previewing || loading || draftKeywords.length === 0}
                        >
                            <RefreshCw size={15} className={previewing ? 'spin' : ''} />
                            {previewing ? '试算中...' : '试算影响范围'}
                        </button>
                        {recommended.some((word) => !draftKeywords.includes(word)) && (
                            <button type="button" className="btn btn-secondary" onClick={handleFillRecommended}>
                                <Sparkles size={15} /> 填入推荐词
                            </button>
                        )}
                        {savedKeywords.length > 0 && (
                            <button
                                type="button"
                                className="btn btn-secondary"
                                onClick={() => {
                                    setDraft('');
                                    setPreview(null);
                                }}
                            >
                                <Ban size={15} /> 清空输入
                            </button>
                        )}
                    </div>

                    {preview && (
                        <>
                            <h4 className="section-subheading">试算结果（未保存，仅预览）</h4>
                            <div className="control-help-text" style={{ marginBottom: 10 }}>
                                按 {preview.keywords.join('、') || '（空）'} 匹配，本地库最近条目里命中{' '}
                                <strong>{preview.items.length}</strong> 条
                                {preview.items.length >= 20 && '（只显示前 20 条）'}。
                            </div>
                            {preview.items.length === 0 ? (
                                <div className="control-help-text">最近条目里没有命中，可以考虑换个更精确的词。</div>
                            ) : (
                                <div
                                    style={{
                                        display: 'flex',
                                        flexDirection: 'column',
                                        gap: 6,
                                        maxHeight: 320,
                                        overflowY: 'auto',
                                    }}
                                >
                                    {preview.items.map((item, index) => (
                                        <div
                                            key={`${item.id}-${index}`}
                                            style={{
                                                display: 'flex',
                                                gap: 10,
                                                alignItems: 'baseline',
                                                padding: '6px 10px',
                                                borderRadius: 8,
                                                border: '1px solid var(--border-base)',
                                                background: 'var(--bg-elevated, rgba(255,255,255,0.03))',
                                                fontSize: 13,
                                            }}
                                        >
                                            <span style={{ color: 'var(--text-muted)', minWidth: 84 }}>{item.id || '-'}</span>
                                            <span style={{ flex: 1, color: 'var(--text-main)' }}>
                                                {item.title || item.original_title || '-'}
                                            </span>
                                            <span style={{ color: 'var(--primary)', fontSize: 12 }}>命中 {item.keyword}</span>
                                        </div>
                                    ))}
                                </div>
                            )}
                        </>
                    )}
                </div>
            </div>
        </div>
    );
}
