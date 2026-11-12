import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Moon, Plus, Save, Trash2, Sunrise } from 'lucide-react';
import { getApiErrorMessage, logSafeError, movieApi } from '../../api';
import { useToast } from '../../components/Toast';

/**
 * 半夜静默模式。
 *
 * 在用户指定的**多个**时间段内暂停 qBittorrent 的上传/下载，主要是为了夜里别让
 * 硬盘和风扇一直响。qB 自带的调度只能配一组 from/to，配不了多个时间段，所以
 * 窗口判定放在后端（qb_quiet_hours.py），这里只负责编辑与状态展示。
 *
 * 两个容易踩的边界，后端已经处理，界面上也要说清楚：
 *   · 只恢复「本功能自己暂停的」种子 —— 你手动暂停的不会被误放出来；
 *   · 静默期间你手动恢复的种子不会被重新按回去。
 */

// ISO 星期：1=周一 … 7=周日；展示按中文习惯从周一开始
const DAYS = [
    { value: 1, label: '一' },
    { value: 2, label: '二' },
    { value: 3, label: '三' },
    { value: 4, label: '四' },
    { value: 5, label: '五' },
    { value: 6, label: '六' },
    { value: 7, label: '日' },
];
const ALL_DAYS = DAYS.map((d) => d.value);

const EMPTY_WINDOW = { start: '23:30', end: '07:00', days: ALL_DAYS };

const timeText = (value) => {
    const text = String(value || '').trim();
    return /^\d{1,2}:\d{2}$/.test(text) ? text.padStart(5, '0') : text;
};

// 后端返回 ISO 时间戳，这里只关心「几点几分」；跨天的补上日期
const clockText = (iso) => {
    if (!iso) return '';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '';
    const hhmm = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
    const today = new Date();
    if (date.toDateString() === today.toDateString()) return hhmm;
    const tomorrow = new Date(today.getTime() + 86400000);
    if (date.toDateString() === tomorrow.toDateString()) return `明天 ${hhmm}`;
    return `${date.getMonth() + 1}/${date.getDate()} ${hhmm}`;
};

const daysText = (days) => {
    const list = Array.isArray(days) ? days : [];
    if (list.length === 7) return '每天';
    if (list.length === 0) return '未选';
    return list
        .slice()
        .sort((a, b) => a - b)
        .map((d) => (DAYS.find((item) => item.value === d) || {}).label || '')
        .join('、');
};

const windowText = (w) => `${timeText(w.start)} – ${timeText(w.end)} · ${daysText(w.days)}`;

export default function QbQuietHoursCard() {
    const { showToast } = useToast();
    const [saved, setSaved] = useState({ enabled: false, windows: [] });
    const [draft, setDraft] = useState({ enabled: false, windows: [] });
    const [state, setState] = useState({});
    const [limits, setLimits] = useState({ max_windows: 8, min_window_minutes: 10 });
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [resuming, setResuming] = useState(false);

    const applyPayload = useCallback((data) => {
        const next = {
            enabled: !!data?.enabled,
            windows: Array.isArray(data?.windows) ? data.windows : [],
        };
        setSaved(next);
        setDraft(next);
        setState(data?.state || {});
        setLimits({
            max_windows: data?.max_windows || 8,
            min_window_minutes: data?.min_window_minutes || 10,
        });
    }, []);

    const load = useCallback(async (silent = false) => {
        if (!silent) setLoading(true);
        try {
            const data = await movieApi.getQBQuietHours();
            if (silent) {
                // 只刷新运行状态，别把用户正在编辑的草稿冲掉
                setState(data?.state || {});
            } else {
                applyPayload(data);
            }
        } catch (err) {
            if (!silent) {
                logSafeError('加载静默模式配置失败:', err);
                showToast(getApiErrorMessage(err, '加载静默模式配置失败'), 'error');
            }
        } finally {
            if (!silent) setLoading(false);
        }
    }, [applyPayload, showToast]);

    useEffect(() => {
        load();
    }, [load]);

    // 状态条每 30 秒对一次表，不然「静默中」要等用户切页才更新
    useEffect(() => {
        const timer = setInterval(() => load(true), 30000);
        return () => clearInterval(timer);
    }, [load]);

    const dirty = useMemo(
        () => JSON.stringify(draft) !== JSON.stringify(saved),
        [draft, saved],
    );

    const updateWindow = (index, patch) => {
        setDraft((prev) => {
            const windows = prev.windows.map((w, i) => (i === index ? { ...w, ...patch } : w));
            return { ...prev, windows };
        });
    };

    const toggleDay = (index, day) => {
        setDraft((prev) => {
            const windows = prev.windows.map((w, i) => {
                if (i !== index) return w;
                const days = Array.isArray(w.days) ? w.days : [];
                const next = days.includes(day) ? days.filter((d) => d !== day) : [...days, day];
                // 一个都不选等于「不生效」，这里退回每天，避免用户以为已经关了
                return { ...w, days: next.length ? next.sort((a, b) => a - b) : ALL_DAYS };
            });
            return { ...prev, windows };
        });
    };

    const addWindow = () => {
        if (draft.windows.length >= limits.max_windows) {
            showToast(`最多 ${limits.max_windows} 个时间段`, 'error');
            return;
        }
        setDraft((prev) => ({ ...prev, windows: [...prev.windows, { ...EMPTY_WINDOW }] }));
    };

    const removeWindow = (index) => {
        setDraft((prev) => ({ ...prev, windows: prev.windows.filter((_, i) => i !== index) }));
    };

    const handleSave = async () => {
        const bad = draft.windows.find((w) => !w.start || !w.end || timeText(w.start) === timeText(w.end));
        if (bad) {
            showToast(`${windowText(bad)} —— 开始和结束不能相同`, 'error');
            return;
        }
        setSaving(true);
        try {
            const res = await movieApi.saveQBQuietHours({
                enabled: draft.enabled,
                windows: draft.windows.map((w) => ({
                    start: timeText(w.start),
                    end: timeText(w.end),
                    days: w.days,
                })),
            });
            if (res?.success) {
                applyPayload(res.data);
                showToast(
                    draft.enabled && draft.windows.length
                        ? `静默模式已生效（${draft.windows.length} 个时间段）`
                        : '静默模式已保存（未启用）',
                    'success',
                );
                load(true);
            } else {
                showToast(getApiErrorMessage(res, '保存失败'), 'error');
            }
        } catch (err) {
            logSafeError('保存静默模式失败:', err);
            showToast(getApiErrorMessage(err, '保存失败'), 'error');
        } finally {
            setSaving(false);
        }
    };

    const handleResumeNow = async () => {
        setResuming(true);
        try {
            const res = await movieApi.resumeQBQuietHours();
            if (res?.success) {
                showToast(`已恢复 ${res.data?.resumed ?? 0} 个任务`, 'success');
                load(true);
            } else {
                showToast(getApiErrorMessage(res, '恢复失败，稍后重试'), 'error');
            }
        } catch (err) {
            logSafeError('解除静默失败:', err);
            showToast(getApiErrorMessage(err, '恢复失败'), 'error');
        } finally {
            setResuming(false);
        }
    };

    const activeNow = !!state.active;
    const pausedCount = Number(state.paused_count || 0);
    const nextClock = clockText(state.next_change_at);

    const statusLine = () => {
        if (loading) return '读取中…';
        if (!saved.enabled) return '未启用';
        if (activeNow) {
            return `静默中 · 已暂停 ${pausedCount} 个任务${nextClock ? `，${nextClock} 自动恢复` : ''}`;
        }
        if (!saved.windows.length) return '已启用，但没有有效时间段';
        return `当前不在时间段内${nextClock ? ` · ${nextClock} 开始静默` : ''}`;
    };

    return (
        <div className="settings-card" style={{ marginTop: 16 }}>
            <div className="card-body">
                <div className="control-label-row">
                    <label className="control-label">
                        <Moon size={15} style={{ verticalAlign: '-2px', marginRight: 6 }} />
                        半夜静默模式
                    </label>
                    <span className="control-value-badge">{statusLine()}</span>
                </div>

                <div className="control-help-text" style={{ marginBottom: 14, lineHeight: 1.8 }}>
                    在下列时间段内<b>暂停 qBittorrent 的上传与下载</b>，出时间段自动恢复，用来压夜里的硬盘与风扇噪音。
                    可以加多个时间段（最多 {limits.max_windows} 个），结束时间早于开始时间表示跨零点。
                    <br />
                    只恢复「静默模式自己暂停的」任务 —— 你手动暂停的不会被放出来；静默期间你手动恢复的任务，也不会被重新按回去。
                    <br />
                    注意：暂停期间<b>做种计时也会停</b>，有 H&amp;R 要求的私有站请把时间段设短一些，或者干脆别开。
                </div>

                <div className="toggle-control-row" style={{ marginBottom: 14 }}>
                    <div>
                        <span className="toggle-label">启用静默模式</span>
                        <span className="toggle-help">
                            {draft.enabled
                                ? '到点自动暂停，到点自动恢复'
                                : '关闭状态下不会暂停任何任务；已暂停的会在下一轮巡检恢复'}
                        </span>
                    </div>
                    <label className="settings-switch">
                        <input
                            type="checkbox"
                            checked={!!draft.enabled}
                            onChange={(e) => setDraft((prev) => ({ ...prev, enabled: e.target.checked }))}
                        />
                        <span className="switch-slider"></span>
                    </label>
                </div>

                {draft.windows.length === 0 && (
                    <div className="control-help-text" style={{ marginBottom: 12 }}>
                        还没有时间段。加一个「23:30 – 07:00 · 每天」就是最常见的整夜静默。
                    </div>
                )}

                {draft.windows.map((w, index) => (
                    <div className="quiet-hours-row" key={index}>
                        <div className="quiet-hours-times">
                            <input
                                type="time"
                                className="settings-input quiet-hours-time"
                                value={timeText(w.start)}
                                onChange={(e) => updateWindow(index, { start: e.target.value })}
                                aria-label="开始时间"
                            />
                            <span className="quiet-hours-dash">→</span>
                            <input
                                type="time"
                                className="settings-input quiet-hours-time"
                                value={timeText(w.end)}
                                onChange={(e) => updateWindow(index, { end: e.target.value })}
                                aria-label="结束时间"
                            />
                        </div>
                        <div className="quiet-hours-days">
                            {DAYS.map((day) => (
                                <button
                                    type="button"
                                    key={day.value}
                                    className={`day-chip ${
                                        (w.days || []).includes(day.value) ? 'active' : ''
                                    }`}
                                    onClick={() => toggleDay(index, day.value)}
                                    title={`周${day.label}`}
                                >
                                    {day.label}
                                </button>
                            ))}
                        </div>
                        <button
                            type="button"
                            className="btn btn-secondary quiet-hours-remove"
                            onClick={() => removeWindow(index)}
                            title="删除这个时间段"
                        >
                            <Trash2 size={14} />
                        </button>
                    </div>
                ))}

                <div className="actions-button-group" style={{ marginTop: 14 }}>
                    <button
                        type="button"
                        className="btn btn-secondary"
                        onClick={addWindow}
                        disabled={loading || draft.windows.length >= limits.max_windows}
                    >
                        <Plus size={15} /> 添加时间段
                    </button>
                    <button
                        type="button"
                        className="btn btn-primary"
                        onClick={handleSave}
                        disabled={saving || loading || !dirty}
                    >
                        <Save size={15} /> {saving ? '保存中...' : '保存并生效'}
                    </button>
                    {(activeNow || pausedCount > 0) && (
                        <button
                            type="button"
                            className="btn btn-secondary"
                            onClick={handleResumeNow}
                            disabled={resuming}
                        >
                            <Sunrise size={15} /> {resuming ? '恢复中...' : '立即解除静默'}
                        </button>
                    )}
                    {dirty && <span className="control-help-text">（有未保存的改动）</span>}
                </div>

                {saved.windows.length > 0 && (
                    <div className="control-help-text" style={{ marginTop: 12 }}>
                        当前生效：{saved.windows.map(windowText).join('；')}
                        {state.last_change && ` ｜ 上次变更 ${clockText(state.last_change)}`}
                    </div>
                )}
            </div>
        </div>
    );
}
