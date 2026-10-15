import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
    BarChart3, Film, UserRound, PlayCircle, HardDrive,
    Download, AlertTriangle, Clock, TrendingUp, Trophy, Layers, Loader2, Server
} from 'lucide-react';
import {
    Chart as ChartJS, ArcElement, BarElement, LineElement, PointElement,
    CategoryScale, LinearScale, Tooltip, Legend, Filler
} from 'chart.js';
import { Bar, Doughnut, Line } from 'react-chartjs-2';
import { getBiOverview, getApiErrorMessage, logSafeError } from '../api';
import './BIDashboardPage.css';

ChartJS.register(
    ArcElement, BarElement, LineElement, PointElement,
    CategoryScale, LinearScale, Tooltip, Legend, Filler
);

// 主题感知的配色：与 index.css 的 --brand 系列对齐
const PALETTE = ['#f5b941', '#60a5fa', '#f472b6', '#34d399', '#a78bfa', '#fb923c', '#22d3ee', '#f87171'];
const GRID = 'rgba(148, 163, 184, 0.16)';
const TICK = '#94a3b8';

// ---------- 数字滚动动画 ----------
function useCountUp(target, duration = 1100) {
    const [value, setValue] = useState(0);
    const rafRef = useRef(null);
    const fromRef = useRef(0);

    useEffect(() => {
        const to = Number(target);
        if (!Number.isFinite(to)) return undefined;
        const from = fromRef.current;
        if (from === to) return undefined;
        const start = performance.now();
        const tick = (now) => {
            const p = Math.min((now - start) / duration, 1);
            // easeOutCubic：起步快、收尾稳，读数不跳
            const eased = 1 - Math.pow(1 - p, 3);
            setValue(from + (to - from) * eased);
            if (p < 1) {
                rafRef.current = requestAnimationFrame(tick);
            } else {
                fromRef.current = to;
            }
        };
        rafRef.current = requestAnimationFrame(tick);
        return () => { if (rafRef.current) cancelAnimationFrame(rafRef.current); };
    }, [target, duration]);

    return value;
}

function KpiCard({ icon: Icon, label, value, suffix, decimals = 0, accent, hint, delay = 0 }) {
    const animated = useCountUp(Number(value) || 0);
    const shown = decimals > 0 ? animated.toFixed(decimals) : Math.round(animated).toLocaleString();
    return (
        <div className="bi-kpi" style={{ '--bi-accent': accent, animationDelay: `${delay}ms` }}>
            <div className="bi-kpi-icon"><Icon size={18} /></div>
            <div className="bi-kpi-body">
                <div className="bi-kpi-label">{label}</div>
                <div className="bi-kpi-value">
                    {shown}
                    {suffix && <span className="bi-kpi-suffix">{suffix}</span>}
                </div>
                {hint && <div className="bi-kpi-hint">{hint}</div>}
            </div>
        </div>
    );
}

function Panel({ title, icon: Icon, children, delay = 0, extra }) {
    return (
        <section className="bi-panel" style={{ animationDelay: `${delay}ms` }}>
            <header className="bi-panel-head">
                <span className="bi-panel-title"><Icon size={15} />{title}</span>
                {extra}
            </header>
            <div className="bi-panel-body">{children}</div>
        </section>
    );
}

function Empty({ text = '暂无可用数据' }) {
    return <div className="bi-empty">{text}</div>;
}

function formatBytes(bytes) {
    const n = Number(bytes) || 0;
    if (n <= 0) return 0;
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
    return { value: n / Math.pow(1024, i), unit: units[i] };
}

function formatHours(hours) {
    const h = Number(hours) || 0;
    if (h <= 0) return { value: 0, unit: '小时' };
    if (h < 24) return { value: h, unit: '小时' };
    const days = h / 24;
    if (days < 365) return { value: days, unit: '天' };
    return { value: days / 365, unit: '年' };
}

export default function BIDashboardPage() {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');

    // 页面只读快照，刷新由导航栏的刷新按钮负责；
    // 需要强制重算时走 /api/bi/overview?refresh=true（或设置页）。
    const load = useCallback(async () => {
        setLoading(true);
        try {
            const res = await getBiOverview(false);
            if (res?.success) {
                setData(res.data || null);
                setError('');
            } else {
                setError(res?.message || '数据加载失败');
            }
        } catch (err) {
            logSafeError('BI overview failed', err);
            setError(getApiErrorMessage(err, '数据加载失败'));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { load(); }, [load]);

    const lib = data?.library || {};
    const cast = data?.cast || {};
    const play = data?.playback || {};
    const down = data?.downloads || {};
    const store = data?.storage || {};
    const runtime = data?.runtime || {};

    // ---- 图表配置 ----
    const genreChart = useMemo(() => {
        const rows = (lib.top_genres || []).slice(0, 8);
        if (!rows.length) return null;
        return {
            labels: rows.map((r) => r.name),
            datasets: [{
                label: '出现次数',
                data: rows.map((r) => r.count),
                backgroundColor: rows.map((_, i) => PALETTE[i % PALETTE.length]),
                borderWidth: 0,
                borderRadius: 4,
            }],
        };
    }, [lib.top_genres]);

    const trendChart = useMemo(() => {
        const rows = play.daily_trend || [];
        if (!rows.length) return null;
        return {
            labels: rows.map((r) => r.date.slice(5)),
            datasets: [{
                label: '播放次数',
                data: rows.map((r) => r.plays),
                borderColor: PALETTE[0],
                backgroundColor: 'rgba(245, 185, 65, 0.16)',
                fill: true,
                tension: 0.34,
                pointRadius: 3,
                pointHoverRadius: 6,
                pointBackgroundColor: PALETTE[0],
                borderWidth: 2,
            }],
        };
    }, [play.daily_trend]);

    const topPlayedChart = useMemo(() => {
        const rows = (play.top_played || []).slice(0, 8);
        if (!rows.length) return null;
        return {
            labels: rows.map((r) => (r.title || '').slice(0, 14)),
            datasets: [{
                label: '播放次数',
                data: rows.map((r) => r.play_count),
                backgroundColor: PALETTE[1],
                borderRadius: 4,
                borderWidth: 0,
            }],
        };
    }, [play.top_played]);

    const statusChart = useMemo(() => {
        const rows = (down.by_status || []).filter((r) => r.count > 0);
        if (!rows.length) return null;
        return {
            labels: rows.map((r) => r.status),
            datasets: [{
                data: rows.map((r) => r.count),
                backgroundColor: rows.map((_, i) => PALETTE[i % PALETTE.length]),
                borderWidth: 0,
            }],
        };
    }, [down.by_status]);

    const storeGB = (store.library_bytes || 0) / 1073741824;
    const uptime = formatHours(runtime.container_uptime_hours);
    const watchTime = formatHours(play.total_watch_hours);

    if (loading) {
        return (
            <div className="bi-page">
                <div className="bi-loading"><Loader2 className="spin" size={30} /><span>正在汇总运营数据...</span></div>
            </div>
        );
    }

    return (
        <div className="bi-page">
            <header className="bi-header">
                <h1><BarChart3 size={22} /> 数据看板</h1>
            </header>

            {error && (
                <div className="bi-alert error"><AlertTriangle size={15} /><span>{error}</span></div>
            )}
            {(data?.warnings || []).length > 0 && (
                <div className="bi-alert warn">
                    <AlertTriangle size={15} />
                    <span>部分指标暂不可用：{data.warnings.join('；')}</span>
                </div>
            )}

            {/* ===== 第一排：总览指标 ===== */}
            <div className="bi-kpi-grid">
                <KpiCard icon={Film} label="影片库条目" value={lib.movie_total} accent={PALETTE[0]}
                    hint={`类型 ${lib.genre_total_kinds || 0} 种`} delay={0} />
                <KpiCard icon={Server} label="已入库可播放" value={store.library_items} accent={PALETTE[1]}
                    hint="Jellyfin 媒体库" delay={60} />
                <KpiCard icon={UserRound} label="关注演员" value={cast.subscribed} accent={PALETTE[2]}
                    hint={`演员库 ${(cast.actress_total || 0).toLocaleString()} 位`} delay={120} />
                <KpiCard icon={PlayCircle} label="累计播放" value={play.total_plays} accent={PALETTE[3]}
                    hint={`${play.played_movies || 0} 部片`} delay={180} />
                <KpiCard icon={Clock} label="累计观看时长" value={watchTime.value} decimals={1}
                    suffix={watchTime.unit} accent={PALETTE[4]} delay={240} />
                <KpiCard icon={HardDrive} label="媒体库存量" value={storeGB} decimals={1} suffix="GB"
                    accent={PALETTE[5]} hint={store.sampled ? `按抽样均体积估算` : ''} delay={300} />
                <KpiCard icon={Download} label="下载任务" value={down.queue_total} accent={PALETTE[6]}
                    hint={`已提交 ${down.submitted || 0} · 完成记录 ${down.completed || 0}`} delay={360} />
                <KpiCard icon={Layers} label="连续运行" value={uptime.value} decimals={1}
                    suffix={uptime.unit} accent={PALETTE[7]} delay={420} />
            </div>

            {/* ===== 第二排：类型与趋势 ===== */}
            <div className="bi-grid-2">
                <Panel title="影片类型分布 Top 8" icon={Layers} delay={120}
                    extra={lib.top_genres?.[0]?.sampled ? (
                        <span className="bi-tag">抽样 {lib.top_genres[0].sampled_rows} 条</span>
                    ) : null}>
                    {genreChart ? (
                        <div className="bi-chart-box">
                            <Bar data={genreChart} options={{
                                indexAxis: 'y',
                                responsive: true,
                                maintainAspectRatio: false,
                                animation: { duration: 900, easing: 'easeOutQuart' },
                                plugins: { legend: { display: false } },
                                scales: {
                                    x: { grid: { color: GRID }, ticks: { color: TICK } },
                                    y: { grid: { display: false }, ticks: { color: TICK, font: { size: 11 } } },
                                },
                            }} />
                        </div>
                    ) : <Empty text="类型分布暂不可用" />}
                </Panel>

                <Panel title="每日播放情况" icon={TrendingUp} delay={180}>
                    {trendChart ? (
                        <>
                            <div className="bi-chart-box">
                                <Line data={trendChart} options={{
                                    responsive: true,
                                    maintainAspectRatio: false,
                                    animation: { duration: 1000, easing: 'easeOutQuart' },
                                    plugins: { legend: { display: false } },
                                    scales: {
                                        x: { grid: { color: GRID }, ticks: { color: TICK, maxRotation: 0, autoSkipPadding: 12 } },
                                        y: { grid: { color: GRID }, ticks: { color: TICK, precision: 0 }, beginAtZero: true },
                                    },
                                }} />
                            </div>
                            {play.trend_note && <p className="bi-note">{play.trend_note}</p>}
                        </>
                    ) : <Empty text="暂无播放记录" />}
                </Panel>
            </div>

            {/* ===== 第三排：播放榜与下载 ===== */}
            <div className="bi-grid-2">
                <Panel title="播放次数最多" icon={Trophy} delay={240}>
                    {topPlayedChart ? (
                        <div className="bi-chart-box">
                            <Bar data={topPlayedChart} options={{
                                indexAxis: 'y',
                                responsive: true,
                                maintainAspectRatio: false,
                                animation: { duration: 900, easing: 'easeOutQuart' },
                                plugins: { legend: { display: false } },
                                scales: {
                                    x: { grid: { color: GRID }, ticks: { color: TICK, precision: 0 }, beginAtZero: true },
                                    y: { grid: { display: false }, ticks: { color: TICK, font: { size: 11 } } },
                                },
                            }} />
                        </div>
                    ) : <Empty text="暂无播放记录" />}
                </Panel>

                <Panel title="下载任务状态" icon={Download} delay={300}>
                    {statusChart ? (
                        <>
                            <div className="bi-chart-box bi-chart-doughnut">
                                <Doughnut data={statusChart} options={{
                                    responsive: true,
                                    maintainAspectRatio: false,
                                    cutout: '58%',
                                    animation: { animateRotate: true, duration: 1100, easing: 'easeOutQuart' },
                                    plugins: { legend: { position: 'right', labels: { color: TICK, boxWidth: 12, font: { size: 11 } } } },
                                }} />
                            </div>
                            <div className="bi-legend-grid">
                                {(down.by_status || []).map((row, i) => (
                                    <div key={row.status} className="bi-legend-item">
                                        <span className="bi-dot" style={{ background: PALETTE[i % PALETTE.length] }} />
                                        <span className="bi-legend-label">{row.status}</span>
                                        <span className="bi-legend-value">{row.count}</span>
                                    </div>
                                ))}
                            </div>
                        </>
                    ) : <Empty text="暂无下载任务" />}
                </Panel>
            </div>

            {/* ===== 第四排：明细 ===== */}
            <div className="bi-grid-2">
                <Panel title="受关注演员 Top 10" icon={UserRound} delay={360}>
                    {(cast.top_actresses || []).length ? (
                        <ol className="bi-rank-list">
                            {cast.top_actresses.map((row, i) => (
                                <li key={`${row.name}-${i}`} style={{ animationDelay: `${i * 40}ms` }}>
                                    <span className={`bi-rank-no ${i < 3 ? 'top' : ''}`}>{i + 1}</span>
                                    <span className="bi-rank-name">{row.name}</span>
                                    <span className="bi-rank-value">{row.movies} 部</span>
                                </li>
                            ))}
                        </ol>
                    ) : <Empty text="暂无演员数据" />}
                </Panel>

                <Panel title="最近入库（尚未观看）" icon={Film} delay={420}>
                    {(lib.recent_unwatched || []).length ? (
                        <ul className="bi-simple-list">
                            {lib.recent_unwatched.slice(0, 8).map((t, i) => (
                                <li key={`${t}-${i}`} style={{ animationDelay: `${i * 40}ms` }}>{t}</li>
                            ))}
                        </ul>
                    ) : <Empty text="暂无待看条目" />}
                </Panel>
            </div>
        </div>
    );
}