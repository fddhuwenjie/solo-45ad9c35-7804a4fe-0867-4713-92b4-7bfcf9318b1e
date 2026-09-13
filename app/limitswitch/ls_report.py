"""限位开关（开/关到位接点）诊断打印报告：HTML + 内联 SVG。

呈现校正后阀位曲线、动作/释放位置窗、两路接点逻辑态、实际采用的边沿
及其判定、异常事件（含原始样本区间）、阈值检查、校准链与版本修订记录。
报告仅标识一致性异常区间，不构成维修结论。
"""

import html
import json

from ..common.calibration_report import render_calibration_section
from .ls_analysis import CONTACT_NAMES, DIRECTION_NAMES, EVENT_NAMES

VERDICT_NAMES = {"ok": "未发现异常", "exceedances": "存在异常区间",
                 "no_conclusion": "证据不足，不得判定"}
KIND_NAMES = {"actuate": "动作", "release": "释放"}


def _polyline(xs, ys, x_of, y_of):
    return " ".join(f"{x_of(x):.1f},{y_of(y):.1f}" for x, y in zip(xs, ys))


def _ls_chart(r, width=920, height=360, pad_l=50, pad_r=16, pad_t=16, pad_b=64):
    c = r["curves"]
    t, pos = c["t"], c["position_pct"]
    t0, t1 = t[0], t[-1]
    v0, v1 = -5.0, 105.0
    lane_h = 14.0

    def x_of(tv):
        return pad_l + (tv - t0) / (t1 - t0) * (width - pad_l - pad_r)

    def y_of(v):
        return pad_t + (v1 - v) / (v1 - v0) * (height - pad_t - pad_b)

    p = [f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
         f'xmlns="http://www.w3.org/2000/svg" font-family="sans-serif" font-size="11">']
    # 动作/释放位置窗
    for logical, colors in (("open", ("#dcfce7", "#dbeafe")),
                            ("closed", ("#fef9c3", "#fce7f3"))):
        spec = (r.get("switch") or {}).get(logical) or {}
        for key, color in (("actuate_window_pct", colors[0]),
                           ("release_window_pct", colors[1])):
            win = spec.get(key)
            if not win:
                continue
            y0, y1v = y_of(win[1]), y_of(win[0])
            p.append(f'<rect x="{pad_l}" y="{y0:.1f}" width="{width - pad_l - pad_r}" '
                     f'height="{max(y1v - y0, 1):.1f}" fill="{color}" opacity="0.6"/>')
    # 异常事件时段
    for ev in r.get("events", []):
        a, b = ev.get("interval_s") or [None, None]
        if a is None:
            continue
        x0, x1 = x_of(a), x_of(b)
        p.append(f'<rect x="{x0:.1f}" y="{pad_t}" width="{max(x1 - x0, 2):.1f}" '
                 f'height="{height - pad_t - pad_b}" fill="#fee2e2" opacity="0.45">'
                 f'<title>{html.escape(EVENT_NAMES.get(ev["kind"], ev["kind"]))} '
                 f'{a}–{b}s</title></rect>')
    for v in range(0, 101, 25):
        y = y_of(v)
        p.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
                 f'stroke="#e5e7eb"/>')
        p.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" '
                 f'fill="#6b7280">{v}%</text>')
    n_ticks = 10
    for k in range(n_ticks + 1):
        tv = t0 + (t1 - t0) * k / n_ticks
        p.append(f'<text x="{x_of(tv):.1f}" y="{height - pad_b + 16}" text-anchor="middle" '
                 f'fill="#6b7280">{tv:.1f}s</text>')
    # 阀位曲线
    p.append(f'<polyline points="{_polyline(t, pos, x_of, y_of)}" fill="none" '
             f'stroke="#059669" stroke-width="1.9"/>')
    # 接点逻辑态泳道（图下方两条）
    lane_y = {"open": height - pad_b + 30, "closed": height - pad_b + 30 + lane_h + 4}
    lane_color = {"open": "#2563eb", "closed": "#d97706"}
    for logical in ("open", "closed"):
        bits = c.get(logical)
        if not bits:
            continue
        yb = lane_y[logical]
        p.append(f'<text x="{pad_l - 6}" y="{yb + lane_h - 3:.1f}" text-anchor="end" '
                 f'fill="{lane_color[logical]}">{CONTACT_NAMES[logical]}</text>')
        p.append(f'<rect x="{pad_l}" y="{yb:.1f}" width="{width - pad_l - pad_r}" '
                 f'height="{lane_h:.1f}" fill="none" stroke="#d1d5db"/>')
        i = 0
        while i < len(bits):
            if bits[i]:
                j = i
                while j + 1 < len(bits) and bits[j + 1]:
                    j += 1
                x0, x1 = x_of(t[i]), x_of(t[j])
                p.append(f'<rect x="{x0:.1f}" y="{yb + 1:.1f}" '
                         f'width="{max(x1 - x0, 1.5):.1f}" height="{lane_h - 2:.1f}" '
                         f'fill="{lane_color[logical]}" opacity="0.75"/>')
                i = j + 1
            else:
                i += 1
    # 采用边沿标记
    for e in r.get("adopted_edges", []):
        if e.get("position_pct") is None:
            continue
        x, y = x_of(e["t_s"]), y_of(e["position_pct"])
        color = "#2563eb" if e["contact"] == "open" else "#d97706"
        if e["kind"] == "actuate":
            p.append(f'<polygon points="{x:.1f},{y - 7:.1f} {x - 5:.1f},{y + 3:.1f} '
                     f'{x + 5:.1f},{y + 3:.1f}" fill="{color}">'
                     f'<title>{CONTACT_NAMES[e["contact"]]}动作 {e["t_s"]}s '
                     f'@ {e["position_pct"]}%</title></polygon>')
        else:
            p.append(f'<polygon points="{x:.1f},{y + 7:.1f} {x - 5:.1f},{y - 3:.1f} '
                     f'{x + 5:.1f},{y - 3:.1f}" fill="none" stroke="{color}" '
                     f'stroke-width="1.6"><title>{CONTACT_NAMES[e["contact"]]}释放 '
                     f'{e["t_s"]}s @ {e["position_pct"]}%</title></polygon>')
    ly = pad_t + 10
    p.append(f'<line x1="{pad_l + 8}" y1="{ly}" x2="{pad_l + 30}" y2="{ly}" '
             f'stroke="#059669" stroke-width="2"/>'
             f'<text x="{pad_l + 34}" y="{ly + 4}" fill="#374151">校正后阀位</text>')
    p.append(f'<polygon points="{pad_l + 118},{ly - 5} {pad_l + 113},{ly + 4} '
             f'{pad_l + 123},{ly + 4}" fill="#2563eb"/>'
             f'<text x="{pad_l + 127}" y="{ly + 4}" fill="#374151">动作边沿（实心）</text>')
    p.append(f'<polygon points="{pad_l + 238},{ly + 5} {pad_l + 233},{ly - 4} '
             f'{pad_l + 243},{ly - 4}" fill="none" stroke="#d97706"/>'
             f'<text x="{pad_l + 247}" y="{ly + 4}" fill="#374151">释放边沿（空心）</text>')
    p.append("</svg>")
    return "".join(p)


def _edges_table(r):
    out = ["<table><thead><tr><th>接点</th><th>边沿</th><th>时刻 (s)</th>"
           "<th>校正阀位 (%)</th><th>运动方向</th><th>位置窗 (%)</th><th>窗内</th>"
           "<th>边沿延迟 (s)</th><th>原始采样引用</th></tr></thead><tbody>"]
    for e in r.get("adopted_edges", []):
        pos = "—" if e.get("position_pct") is None else e["position_pct"]
        win = "–".join(f"{v:g}" for v in e.get("window_pct", []))
        in_win = e.get("in_window")
        in_txt = "—" if in_win is None else ("是" if in_win else "<b class='bad'>否</b>")
        delay = e.get("delay_s")
        delay_txt = "—" if delay is None else (
            f"{delay}" if delay >= 0 else f"<b class='warn'>{delay}（提前）</b>")
        refs = "，".join(f"{x['channel']}#{x['index']}@{x['t']}s={x['value']}"
                         for x in e.get("raw_refs", []))
        out.append(
            f"<tr><td>{CONTACT_NAMES[e['contact']]}</td>"
            f"<td>{KIND_NAMES[e['kind']]}</td><td>{e['t_s']}</td><td>{pos}</td>"
            f"<td>{DIRECTION_NAMES[e.get('direction')]}</td><td>[{win}]</td>"
            f"<td>{in_txt}</td><td>{delay_txt}</td>"
            f"<td class='reason'>{html.escape(refs)}</td></tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _metrics_table(r):
    rows = []
    for logical in ("open", "closed"):
        m = ((r.get("contacts") or {}).get(logical) or {}).get("metrics") or {}
        if not m:
            continue
        rows.append(
            f"<tr><td>{CONTACT_NAMES[logical]}</td>"
            f"<td>{m.get('n_actuate_edges')} / {m.get('n_release_edges')}</td>"
            f"<td>{m.get('mean_actuation_pct')}</td>"
            f"<td>{m.get('mean_release_pct')}</td>"
            f"<td>{m.get('mean_differential_pct')}</td>"
            f"<td>{m.get('max_edge_delay_s')}</td>"
            f"<td>{m.get('actuation_dispersion_pct')}</td>"
            f"<td>{m.get('release_dispersion_pct')}</td></tr>")
    return ("<table><thead><tr><th>接点</th><th>动作/释放边沿数</th>"
            "<th>平均动作点 (%)</th><th>平均释放点 (%)</th><th>平均开关回差 (%)</th>"
            "<th>最大边沿延迟 (s)</th><th>动作点离散度 (%)</th><th>释放点离散度 (%)</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def _checks_table(r):
    out = ["<table><thead><tr><th>接点</th><th>检查项</th><th>实测</th><th>阈值</th>"
           "<th>依据</th><th>判定</th></tr></thead><tbody>"]
    for c in r.get("checks", []):
        ok = c.get("pass")
        out.append(
            f"<tr><td>{CONTACT_NAMES[c['contact']]}</td>"
            f"<td>{html.escape(c['metric'])}</td><td>{c.get('value')}</td>"
            f"<td>≤ {c.get('threshold_max')}</td>"
            f"<td class='reason'>{html.escape(c.get('basis', ''))}</td>"
            f"<td class={'ok' if ok else 'bad'}>{'通过' if ok else '不通过'}</td></tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _events_html(r):
    items = []
    for ev in r.get("events", []):
        a, b = ev.get("interval_s") or [None, None]
        iv = f"{a}–{b}s" if a is not None and a != b else f"{a}s"
        raws = []
        for ri in ev.get("raw_intervals") or []:
            if ri:
                raws.append(f"{ri['channel']}#{ri['start_index']}–{ri['end_index']}"
                            f"（{ri['t_start']}–{ri['t_end']}s）")
        items.append(
            f"<li><b>[{html.escape(EVENT_NAMES.get(ev['kind'], ev['kind']))}]</b> "
            f"{html.escape(ev.get('detail', ''))}"
            f"<br/><span class='reason'>时段 {iv}；原始样本区间："
            f"{html.escape('；'.join(raws) or '—')}</span></li>")
    return "".join(items) or "<li>无</li>"


def render_ls_report(analysis, test, valve):
    r = analysis["result"]
    esc = html.escape
    verdict = r["verdict"]
    wiring = r.get("wiring") or {}
    sw = r.get("switch") or {}

    gaps = "".join(
        f"<li>[{esc(g['code'])}] {esc(g['detail'])}</li>"
        for g in r.get("evidence_gaps", [])) or "<li>无</li>"

    def _adj_line(a):
        who = f"v{analysis['version']} / {esc(a.get('author', ''))}"
        if a["type"] == "channel_rebind":
            return (f"<li>{who}：改绑通道 {esc(json.dumps(a.get('channel_map', {}), ensure_ascii=False))}"
                    f"（理由：{esc(a.get('reason', ''))}）</li>")
        if a["type"] == "polarity_override":
            return (f"<li>{who}：纠正极性 {esc(json.dumps(a.get('polarities', {}), ensure_ascii=False))}"
                    f"（理由：{esc(a.get('reason', ''))}）</li>")
        if a["type"] == "ignore_glitch":
            return (f"<li>{who}：忽略毛刺 {esc(a.get('contact', ''))} "
                    f"{a.get('t_start')}–{a.get('t_end')}s"
                    f"（理由：{esc(a.get('reason', ''))}）</li>")
        if a["type"] == "calibration_rebind":
            return (f"<li>{who}：改绑逐通道校准版本 "
                    f"{esc(json.dumps(a.get('bindings', {}), ensure_ascii=False))}"
                    "（派生新版本，旧分析冻结版本不变）</li>")
        return f"<li>{who}：{esc(a.get('type', ''))}</li>"

    adjustments = "".join(_adj_line(a) for a in analysis["adjustments"]) \
        or "<li>无（自动分析）</li>"
    basis = "".join(f"<li>{esc(b)}</li>" for b in r.get("decision_basis", [])) \
        or "<li>—</li>"
    mutex = sw.get("mutual_exclusion") or {}
    wiring_txt = (f"开到位←原始通道 {wiring.get('channel_map', {}).get('open')} "
                  f"（{wiring.get('polarities', {}).get('open')}），"
                  f"关到位←原始通道 {wiring.get('channel_map', {}).get('closed')} "
                  f"（{wiring.get('polarities', {}).get('closed')}）")
    switch_txt = []
    for logical in ("open", "closed"):
        spec = sw.get(logical) or {}
        switch_txt.append(
            f"{CONTACT_NAMES[logical]}：动作窗 {spec.get('actuate_window_pct')}%，"
            f"释放窗 {spec.get('release_window_pct')}%，极性 {spec.get('polarity')}")
    mutex_txt = ("启用，允许交叠 " + str(mutex.get("max_overlap_s", 0)) + "s") \
        if mutex.get("enabled", True) else "停用"
    switch_txt.append(f"去抖时长 {sw.get('debounce_s')}s；互斥规则：{mutex_txt}")

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"/>
<title>限位开关诊断报告 {esc(valve['tag'])} LS测试#{test['id']} v{analysis['version']}</title>
<style>
 body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; margin: 24px; color: #111827; }}
 h1 {{ font-size: 20px; }} h2 {{ font-size: 15px; margin-top: 20px; border-bottom: 1px solid #d1d5db; padding-bottom: 4px; }}
 table {{ border-collapse: collapse; font-size: 12px; }}
 th, td {{ border: 1px solid #d1d5db; padding: 4px 8px; text-align: left; }}
 th {{ background: #f3f4f6; }}
 .ok {{ color: #059669; font-weight: 600; }} .bad {{ color: #dc2626; font-weight: 600; }}
 .warn {{ color: #d97706; }} .reason {{ font-size: 11px; color: #4b5563; }}
 .verdict {{ display: inline-block; padding: 2px 10px; border-radius: 4px; font-weight: 700;
   background: {'#d1fae5' if verdict == 'ok' else ('#fee2e2' if verdict == 'exceedances' else '#fef3c7')}; }}
 .meta td {{ border: none; padding: 2px 12px 2px 0; }}
 ul {{ font-size: 12px; margin: 6px 0; }}
 @media print {{ body {{ margin: 8mm; }} h2 {{ page-break-after: avoid; }} svg {{ max-width: 100%; }} }}
</style></head><body>
<h1>调节阀限位开关（开/关到位接点）全行程诊断报告</h1>
<table class="meta"><tr>
<td><b>阀门位号</b>：{esc(valve['tag'])}</td>
<td><b>LS 测试编号</b>：{test['id']}（{esc(test['phase'])}）</td>
<td><b>分析版本</b>：v{analysis['version']}（{esc(analysis['author'])}，{esc(analysis['created_at'])}）</td>
<td><b>结论</b>：<span class="verdict">{VERDICT_NAMES[verdict]}</span></td></tr></table>
<p class="reason">{esc(r.get('conclusion_scope', ''))}。接线：{esc(wiring_txt)}。</p>
<p class="reason">{'；'.join(esc(x) for x in switch_txt)}</p>

<h2>阀位曲线与接点逻辑态（位置窗 / 采用边沿 / 异常时段）</h2>
{_ls_chart(r) if r.get("curves") else "<p class='bad'>无对齐曲线（见证据缺口）。</p>"}

<h2>实际采用的边沿及判定</h2>
{_edges_table(r)}

<h2>接点指标（动作点 / 释放点 / 开关回差 / 边沿延迟 / 多循环离散度）</h2>
{_metrics_table(r)}

<h2>阈值检查</h2>
{_checks_table(r)}

<h2>异常事件（均标注原始样本区间，不构成维修结论）</h2>
<ul>{_events_html(r)}</ul>

<h2>判定依据明细</h2>
<ul>{basis}</ul>

{render_calibration_section(r)}

<h2>证据缺口（存在时不得给出结论）</h2>
<ul>{gaps}</ul>

<h2>版本与人工修订记录（改绑通道 / 纠正极性 / 忽略毛刺，均须理由）</h2>
<ul>{adjustments}</ul>

<p class="reason">单位处理：{esc('；'.join(r.get('unit_notes', [])) or '均为原生单位')}。
对齐网格：{r['alignment']['grid_step_s'] if r.get('alignment') else '—'}s ×
{r['alignment']['n_points'] if r.get('alignment') else '—'} 点。
报告由分析版本 v{analysis['version']} 生成，数据可追溯至 LS 测试 #{test['id']} 原始采样；
检修前后比较仅接纳接线与阈值版本完全一致的分析版本。</p>
</body></html>"""
