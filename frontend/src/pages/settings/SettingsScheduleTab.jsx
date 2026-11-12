import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Clock, RotateCcw, Save } from 'lucide-react';
import { getScrapeSchedule, saveScrapeSchedule } from '../../api';
import { useToast } from '../../components/Toast';

// 秒 → 「4 小时」这种人话，用于「实际生效」提示和错值排查。
const formatDuration = (seconds) => {
    const total = Number(seconds) || 0;
    if (total <= 0) return '不限间隔';
    if (total % 86400 === 0) return `${total / 86400} 天`;
    if (total % 3600 === 0) return `${total / 3600} 小时`;
    if (total % 60 === 0) return `${total / 60} 分钟`;
    return `${total} 秒`;
};

// 前端把「值 + 单位」换算成秒，用来提示实际节奏；后端保存时会再校验一遍。
const toSeconds = (value, unit) => {
    const factor = unit === 'day' ? 86400 : unit === 'hour' ? 3600 : unit === 'minute' ? 60 : 1;
    return (Number(value) || 0) * factor;
};

export default function SettingsScheduleTab() {
    const { showToast } = useToast();
    const [items, setItems] = useState([]);
    const [timesDraft, setTimesDraft] = useState({});
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);

    const loadSchedule = useCallback(async () => {
        setLoading(true);
        try {
            const data = await getScrapeSchedule();
            const list = Array.isArray(data?.items) ? data.items : [];
            setItems(list);
            setTimesDraft(
                Object.fromEntries(
                    list.filter((item) => item.kind === 'times')
                        .map((item) => [item.key, (item.times || []).join(', ')])
                )
            );
        } catch (err) {
            console.error('加载抓取节奏失败:', err);
            showToast('加载抓取节奏失败', 'error');
        } finally {
            setLoading(false);
        }
    }, [showToast]);

    useEffect(() => {
        loadSchedule();
    }, [loadSchedule]);

    const updateItem = (key, patch) => {
        setItems((prev) => prev.map((item) => (item.key === key ? { ...item, ...patch } : item)));
    };

    const payload = useMemo(() => {
        const body = {};
        items.forEach((item) => {
            if (item.kind === 'times') {
                const raw = timesDraft[item.key] ?? (item.times || []).join(', ');
                body[item.key] = {
                    times: raw.split(/[,，\s]+/).map((part) => part.trim()).filter(Boolean),
                };
                return;
            }
            body[item.key] = { value: Number(item.value), unit: item.unit };
        });
        return body;
    }, [items, timesDraft]);

    const handleSave = async () => {
        setSaving(true);
        try {
            const data = await saveScrapeSchedule(payload);
            const list = Array.isArray(data?.items) ? data.items : [];
            if (list.length) {
                setItems(list);
                setTimesDraft(
                    Object.fromEntries(
                        list.filter((item) => item.kind === 'times')
                            .map((item) => [item.key, (item.times || []).join(', ')])
                    )
                );
            }
            showToast('抓取节奏已保存，下一轮循环起生效', 'success');
        } catch (err) {
            const detail = err?.response?.data?.errors;
            showToast(
                Array.isArray(detail) && detail.length ? `保存失败：${detail.join(', ')}` : '保存抓取节奏失败',
                'error'
            );
        } finally {
            setSaving(false);
        }
    };

    if (loading) {
        return (
            <div className="settings-tab-pane">
                <div className="settings-card">
                    <div className="card-body">正在读取抓取节奏…</div>
                </div>
            </div>
        );
    }

    return (
        <div className="settings-tab-pane">
            <div className="settings-card">
                <div className="card-body">
                    <div className="schedule-header">
                        <div>
                            <span className="toggle-label">
                                <Clock size={15} style={{ verticalAlign: '-2px', marginRight: 6 }} />
                                抓取与刷新频率
                            </span>
                            <span className="toggle-help">
                                这些是后台自动任务的节奏，保存后下一轮循环立即按新值走，不需要重启。
                                带「随机」的项表示实际间隔在 0 ~ 该值之间随机，避免固定节奏被站点识别。
                            </span>
                        </div>
                        <div className="schedule-actions">
                            <button type="button" className="btn btn-secondary" onClick={loadSchedule} disabled={saving}>
                                <RotateCcw size={14} />
                                <span>重新读取</span>
                            </button>
                            <button type="button" className="btn btn-primary" onClick={handleSave} disabled={saving}>
                                <Save size={14} />
                                <span>{saving ? '保存中…' : '保存节奏'}</span>
                            </button>
                        </div>
                    </div>

                    <div className="schedule-list">
                        {items.map((item) => (
                            <div className="schedule-row" key={item.key}>
                                <div className="schedule-meta">
                                    <span className="control-label">{item.label}</span>
                                    <span className="schedule-help">{item.description}</span>
                                </div>
                                <div className="schedule-control">
                                    {item.kind === 'times' ? (
                                        <input
                                            type="text"
                                            className="settings-input schedule-times-input"
                                            value={timesDraft[item.key] ?? ''}
                                            onChange={(e) =>
                                                setTimesDraft((prev) => ({ ...prev, [item.key]: e.target.value }))
                                            }
                                            placeholder="08:00, 18:00"
                                        />
                                    ) : (
                                        <div className="schedule-interval">
                                            <input
                                                type="number"
                                                min="0"
                                                step="any"
                                                className="settings-input schedule-number-input"
                                                value={item.value}
                                                onChange={(e) => updateItem(item.key, { value: e.target.value })}
                                            />
                                            <select
                                                className="settings-input schedule-unit-select"
                                                value={item.unit}
                                                onChange={(e) => updateItem(item.key, { unit: e.target.value })}
                                            >
                                                {(item.units || []).map((unit) => (
                                                    <option key={unit.value} value={unit.value}>
                                                        {unit.label}
                                                    </option>
                                                ))}
                                            </select>
                                            <span className="schedule-effective">
                                                {item.key.startsWith('subscription_') || item.key === 'subtitle_backfill'
                                                    ? `每次 0 ~ ${formatDuration(toSeconds(item.value, item.unit))} 随机`
                                                    : `每 ${formatDuration(toSeconds(item.value, item.unit))} 一次`}
                                            </span>
                                        </div>
                                    )}
                                    {item.kind === 'times' && (
                                        <span className="schedule-effective">
                                            每天 {(timesDraft[item.key] || '').split(/[,，\s]+/).filter(Boolean).length || 0} 次
                                        </span>
                                    )}
                                </div>
                            </div>
                        ))}
                    </div>

                    <p className="schedule-footnote">
                        单位按建议值给：首页/榜单、订阅整轮用小时，订阅与字幕间隔用分钟，元数据与头像用天。
                        想更保守就把数值调大，想更快就调小；站点抓到限制会被自动熔断。
                    </p>
                </div>
            </div>
        </div>
    );
}
