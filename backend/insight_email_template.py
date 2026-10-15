"""观看提醒邮件的 HTML 模板（含内联 SVG 图表）。

为什么图表用**内联 SVG**而不是 PNG 图片：
- 邮件客户端默认屏蔽远程图片，`<img src="外链">` 必然显示不出来；
- 内联 SVG 在主流客户端（QQ/163/Gmail/Outlook 2016+）可正常渲染，体积也只有几 KB。

同时**必须**配一份文字表格 —— Outlook 与部分移动端对 SVG 支持不稳，
纯图表邮件在那些客户端里会变成一片空白。文字摘要保证任何客户端都有信息。
"""
import html
import math
from typing import Any, Dict, List, Tuple

from insight_stats_service import MAX_PAGINATE

# 与前端品牌色一致（index.css --brand 等），深色邮件底
INK = "#f3f4f6"
MUTED = "#9ca3af"
ACCENT = "#f5b941"
PALETTE = ["#f5b941", "#60a5fa", "#f472b6", "#34d399", "#a78bfa", "#fb923c", "#22d3ee"]


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _pie_slices(rows: List[Dict[str, Any]]) -> List[str]:
    """把占比换算成 SVG 圆弧扇形。rows 需已按 count 降序。"""
    total = sum(r["count"] for r in rows) or 1
    out: List[str] = []
    angle = -90.0  # 从 12 点方向起画
    for index, row in enumerate(rows):
        fraction = row["count"] / total
        sweep = fraction * 360.0
        end = angle + sweep
        color = PALETTE[index % len(PALETTE)]

        # 整圆用两个半圆画，否则起止点重合时 SVG 画不出整圆
        if sweep >= 359.99:
            out.append(f'<circle cx="90" cy="90" r="60" fill="none" stroke="{color}" stroke-width="34"/>')
        else:
            x1 = 90 + 60 * math.cos(math.radians(angle))
            y1 = 90 + 60 * math.sin(math.radians(angle))
            x2 = 90 + 60 * math.cos(math.radians(end))
            y2 = 90 + 60 * math.sin(math.radians(end))
            large = 1 if sweep > 180 else 0
            out.append(
                f'<path d="M 90 90 L {x1:.2f} {y1:.2f} '
                f'A 60 60 0 {large} 1 {x2:.2f} {y2:.2f} Z" fill="{color}" stroke="#141821" stroke-width="1"/>'
            )
        angle = end
    return out


def _legend(rows: List[Dict[str, Any]]) -> str:
    total = sum(r["count"] for r in rows) or 1
    cells = []
    for index, row in enumerate(rows[:6]):
        color = PALETTE[index % len(PALETTE)]
        percent = row["count"] / total * 100
        cells.append(
            f'<tr>'
            f'<td style="padding:4px 8px 4px 0;">'
            f'<span style="display:inline-block;width:10px;height:10px;background:{color};'
            f'border-radius:2px;margin-right:6px;"></span>'
            f'<span style="color:{INK};font-size:13px;">{_esc(row["kind"])}</span></td>'
            f'<td style="padding:4px 0;color:{MUTED};font-size:13px;text-align:right;">'
            f'{row["count"]} 部 · {percent:.1f}%</td></tr>'
        )
    return "".join(cells)


def _bar_chart(rows: List[Dict[str, Any]]) -> str:
    """播放次数横向条形图。纯 div 宽度实现，兼容性最好。"""
    if not rows:
        return '<div style="color:' + MUTED + ';font-size:13px;">暂无播放记录</div>'
    top = max((r["play_count"] for r in rows), default=1) or 1
    bars = []
    for row in rows:
        width = max(2, int(row["play_count"] / top * 100))
        bars.append(
            f'<tr><td style="padding:3px 8px 3px 0;color:{INK};font-size:12px;'
            f'white-space:nowrap;overflow:hidden;max-width:150px;">{_esc(row["title"])}</td>'
            f'<td style="padding:3px 0;width:55%;">'
            f'<div style="background:{ACCENT};height:12px;width:{width}%;border-radius:2px;"></div></td>'
            f'<td style="padding:3px 0 3px 8px;color:{MUTED};font-size:12px;">{row["play_count"]} 次</td></tr>'
        )
    return f'<table style="width:100%;border-collapse:collapse;">{"".join(bars)}</table>'


def _progress_ring(percent: float) -> str:
    """观看进度环。

    占比过低时（正常状态就是 1~3%）四舍五入会变成 0%，环上什么也看不见，
    读者会以为数据坏了。所以低于 1% 时至少画一个可见的小弧，并把数字
    用 `< 1%` 表达，不谎报 0%。
    """
    radius = 52
    circumference = 2 * math.pi * radius
    shown = 0.0 if percent <= 0 else max(percent, 0.012)  # 保底画一小段，肉眼可见
    filled = circumference * min(shown, 1.0)
    label = "0%" if percent <= 0 else ("&lt; 1%" if percent < 0.005 else f"{percent * 100:.0f}%")
    return (
        f'<svg width="140" height="140" viewBox="0 0 140 140" role="img" aria-label="观看进度">'
        f'<circle cx="70" cy="70" r="{radius}" fill="none" stroke="#2a3140" stroke-width="14"/>'
        f'<circle cx="70" cy="70" r="{radius}" fill="none" stroke="{ACCENT}" stroke-width="14"'
        f' stroke-linecap="round" stroke-dasharray="{filled:.1f} {circumference:.1f}"'
        f' transform="rotate(-90 70 70)"/>'
        f'<text x="70" y="66" text-anchor="middle" fill="{INK}" font-size="24" font-weight="700">'
        f'{label}</text>'
        f'<text x="70" y="88" text-anchor="middle" fill="{MUTED}" font-size="12">已看完</text>'
        f"</svg>"
    )


def build_watching_digest_email(stats: Dict[str, Any]) -> Tuple[str, str]:
    """生成（主题, HTML 正文）。

    文案里每个数字都来自 stats，**没有任何硬编码**。
    """
    idle = stats.get("idle_days_actual")
    threshold = stats.get("idle_days_threshold") or 10
    watched = stats.get("watched_total") or 0
    unwatched = stats.get("unwatched_total") or 0
    total = watched + unwatched
    percent = (watched / total) if total else 0
    eta_human = stats.get("eta_human") or "-"
    minutes_per_item = stats.get("minutes_per_item") or 0
    last_played = stats.get("last_played_at") or "记录中查不到"
    kinds = stats.get("kind_breakdown") or []
    newest = stats.get("newest_unwatched") or []
    top_played = stats.get("play_count_top") or []

    if idle and idle > 0:
        headline = f"您已经 {idle} 天没看片了"
    else:
        headline = "片单里还堆着不少没看过的内容"

    subject = f"【BusTerminal】{headline}，待看 {unwatched} 部"

    # 片单类型分布的饼图（数据为空时不画，避免一张空饼）
    if kinds:
        pie = (
            '<div style="text-align:center;margin:8px 0 4px;">'
            f'<svg width="180" height="180" viewBox="0 0 180 180" role="img" aria-label="片单类型分布">'
            f'{"".join(_pie_slices(kinds))}</svg>'
            f'<div style="color:{MUTED};font-size:12px;margin-top:2px;">待看片单类型分布</div></div>'
        )
    else:
        pie = f'<div style="color:{MUTED};font-size:13px;text-align:center;padding:18px;">片单分类数据暂不可用</div>'

    newest_rows = "".join(
        f'<li style="padding:3px 0;color:{INK};font-size:13px;">{_esc(t)}</li>' for t in newest
    ) or '<li style="color:#9ca3af;font-size:13px;">暂无可展示的新入库条目</li>'

    degraded_note = ""
    if stats.get("degraded"):
        reasons = "、".join(stats.get("degraded_reasons") or [])
        degraded_note = (
            f'<div style="background:rgba(245,158,11,0.12);border:1px solid rgba(245,158,11,0.4);'
            f'border-radius:6px;padding:10px 12px;margin:14px 0;color:{ACCENT};font-size:12px;line-height:1.6;">'
            f"⚠️ 部分数据未能读取（{_esc(reasons)}），本次统计不完整，请勿据此判断观看进度。</div>"
        )
    elif stats.get("truncated"):
        # 命中分页上限时计数偏低，必须说明，不能让被截断的数字当成真值
        degraded_note = (
            f'<div style="background:rgba(245,158,11,0.12);border:1px solid rgba(245,158,11,0.4);'
            f'border-radius:6px;padding:10px 12px;margin:14px 0;color:{ACCENT};font-size:12px;line-height:1.6;">'
            f"⚠️ 片单数量超过单次统计上限（{_esc(MAX_PAGINATE)} 条），上述数字为<b>下限</b>，"
            f"实际待看数量更多，看完所需时间也会更长。</div>"
        )

    body = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#0f1319;">
<div style="max-width:640px;margin:0 auto;padding:24px 18px;background:#0f1319;
            font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;">

  <div style="color:{ACCENT};font-size:12px;letter-spacing:2px;margin-bottom:6px;">BUSTERMINAL · 观看提醒</div>
  <h1 style="margin:0 0 10px;color:{INK};font-size:22px;line-height:1.4;">{headline}</h1>
  <p style="margin:0 0 4px;color:{MUTED};font-size:14px;line-height:1.8;">
    在您没看片的这段时间，系统还是照常为您服务：<strong style="color:{INK};">下载了 {unwatched} 部待看内容</strong>。
  </p>
  <p style="margin:0;color:{MUTED};font-size:14px;line-height:1.8;">
    距离上次观看（{_esc(last_played)}）已经 {idle if idle and idle > 0 else '—'} 天，
    按当前观看习惯（每部约 {minutes_per_item} 分钟），
    剩余片单全部看完还需要 <strong style="color:{ACCENT};">{eta_human}</strong>。
    不必急着看完剩下的部分，挑真正想看的就好，也请注意休息、保重身体。
  </p>

  {degraded_note}

  <div style="display:block;background:#141821;border:1px solid #232936;border-radius:10px;
              padding:18px;margin:18px 0;">
    <div style="color:{INK};font-size:15px;font-weight:600;margin-bottom:12px;">观看进度</div>
    <table style="width:100%;border-collapse:collapse;">
      <tr>
        <td style="text-align:center;width:140px;">{_progress_ring(percent)}</td>
        <td style="padding-left:12px;color:{MUTED};font-size:13px;line-height:2;">
          已看完 <span style="color:{INK};font-weight:700;">{watched}</span> 部<br/>
          待看 <span style="color:{ACCENT};font-weight:700;">{unwatched}</span> 部<br/>
          预计看完还需 <span style="color:{INK};font-weight:700;">{eta_human}</span>
        </td>
      </tr>
    </table>
  </div>

  <div style="background:#141821;border:1px solid #232936;border-radius:10px;padding:18px;margin:0 0 18px;">
    <div style="color:{INK};font-size:15px;font-weight:600;margin-bottom:8px;">待看片单类型分布</div>
    {pie}
    <table style="width:100%;border-collapse:collapse;margin-top:10px;">{_legend(kinds)}</table>
  </div>

  <div style="background:#141821;border:1px solid #232936;border-radius:10px;padding:18px;margin:0 0 18px;">
    <div style="color:{INK};font-size:15px;font-weight:600;margin-bottom:8px;">反复观看最多的片源</div>
    {_bar_chart(top_played)}
  </div>

  <div style="background:#141821;border:1px solid #232936;border-radius:10px;padding:18px;margin:0 0 18px;">
    <div style="color:{INK};font-size:15px;font-weight:600;margin-bottom:8px;">新入库（还没看）</div>
    <ul style="margin:0;padding-left:18px;">{newest_rows}</ul>
  </div>

  <div style="color:{MUTED};font-size:11px;line-height:1.7;border-top:1px solid #232936;padding-top:12px;">
    统计时间 {_esc(stats.get("generated_at"))} · 数据来源 Jellyfin 媒体库<br/>
    观看速度为估算值（{_esc(stats.get("pace_note") or "按经验值估算")}），仅供参考。
    本邮件由 BusTerminal 自动发送，可在 设置 → 消息与通知 中关闭或调整频率。
  </div>
</div>
</body></html>"""
    return subject, body