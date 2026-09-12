"""气体阀座密封保持试验打印报告：HTML + 内联 SVG。

包含指令/阀位与关位带、上下游压力与最小压差、下游温度与空白基线包络、
累计漏量与泄漏率曲线；记录隔离事件、段划分依据、温压补偿过程（含原始点
引用）、阈值检查与判定依据、证据缺口与版本调整。
"""

import html
import json

from ..calibration_report import render_calibration_section

VERDICT_NAMES = {"pass": "合格", "fail": "泄漏超限", "no_conclusion": "证据不足，不得判定"}
DIRECTION_NAMES = {"upstream_to_downstream": "上游→下游（下游升压）",
                   "downstream_to_upstream": "下游→上游（下游降压）"}
SEG_NAMES = {"stabilization": "稳压段", "hold": "保持段"}
SEG_COLORS = {"stabilization": "#fef3c7", "hold": "#d1fae5"}
ACTION_NAMES = {
    "close_command": "关阀指令", "valve_closed": "阀门关到位",
    "upstream_isolated": "上游隔离", "downstream_isolated": "下游隔离",
    "vent_opened": "放空打开", "vent_closed": "放空关闭",
    "hold_start": "保持段开始", "hold_end": "保持段结束",
}
TREND_NAMES = {"degrading": "退化", "improving": "改善", "stable": "稳定",
               "inconclusive": "数据不足", "mixed": "有升有降"}


def _polyline(xs, ys, x_of, y_of):
    return " ".join(f"{x_of(x):.1f},{y_of(y):.1f}" for x, y in zip(xs, ys)
                    if y is not None)


def _frame(width, height, pad_l, pad_r, pad_t, pad_b):
    return (f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'xmlns="http://www.w3.org/2000/svg" font-family="sans-serif" font-size="11">')


def _axes(t0, t1, width, height, pad_l, pad_r, pad_t, pad_b):
    def x_of(tv):
        return pad_l + (tv - t0) / max(t1 - t0, 1e-9) * (width - pad_l - pad_r)
    ticks = []
    for k in range(11):
        tv = t0 + (t1 - t0) * k / 10
        ticks.append(f'<text x="{x_of(tv):.1f}" y="{height - pad_b + 16}" '
                     f'text-anchor="middle" fill="#6b7280">{tv:.0f}s</text>')
    return x_of, "".join(ticks)


def _event_lines(r, x_of, pad_t, height, pad_b):
    out = []
    for e in (r.get("isolation") or {}).get("events", []):
        x = x_of(e["t"])
        color = "#7c3aed" if e["action"] in ("downstream_isolated", "hold_start",
                                             "hold_end") else "#9ca3af"
        out.append(f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{height - pad_b}" '
                   f'stroke="{color}" stroke-width="1" stroke-dasharray="4,3">'
                   f'<title>{ACTION_NAMES.get(e["action"], e["action"])} #{e["index"]} '
                   f'@ {e["t"]}s</title></line>')
    return "".join(out)


def _seg_shading(r, x_of, pad_t, height, pad_b, width, pad_l, pad_r):
    out = []
    for seg in r.get("segments", []):
        if seg.get("t_start") is None or seg.get("t_end") is None:
            continue
        x0, x1 = x_of(seg["t_start"]), x_of(seg["t_end"])
        out.append(f'<rect x="{x0:.1f}" y="{pad_t}" width="{max(x1 - x0, 1):.1f}" '
                   f'height="{height - pad_t - pad_b}" fill="{SEG_COLORS[seg["type"]]}" '
                   f'opacity="0.5"><title>{SEG_NAMES[seg["type"]]} '
                   f'{seg["t_start"]}–{seg["t_end"]}s</title></rect>')
    return "".join(out)


def _travel_chart(r, width=920, height=280, pad_l=50, pad_r=16, pad_t=16, pad_b=34):
    c = r["curves"]
    t, cmd, pos = c["t"], c["command"], c["position"]
    t0, t1 = t[0], t[-1]
    v0, v1 = -5.0, 105.0
    x_of, xticks = _axes(t0, t1, width, height, pad_l, pad_r, pad_t, pad_b)

    def y_of(v):
        return pad_t + (v1 - v) / (v1 - v0) * (height - pad_t - pad_b)

    band = r["thresholds"]["closed_band_pct"]
    p = [_frame(width, height, pad_l, pad_r, pad_t, pad_b),
         _seg_shading(r, x_of, pad_t, height, pad_b, width, pad_l, pad_r)]
    y0, y1v = y_of(band), y_of(0.0)
    p.append(f'<rect x="{pad_l}" y="{y0:.1f}" width="{width - pad_l - pad_r}" '
             f'height="{y1v - y0:.1f}" fill="#dcfce7" opacity="0.9"/>')
    for v in range(0, 101, 25):
        y = y_of(v)
        p.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
                 f'stroke="#e5e7eb"/>')
        p.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" fill="#6b7280">{v}%</text>')
    p.append(xticks)
    p.append(_event_lines(r, x_of, pad_t, height, pad_b))
    p.append(f'<polyline points="{_polyline(t, cmd, x_of, y_of)}" fill="none" '
             f'stroke="#2563eb" stroke-width="1.6"/>')
    p.append(f'<polyline points="{_polyline(t, pos, x_of, y_of)}" fill="none" '
             f'stroke="#059669" stroke-width="1.8"/>')
    ly = height - 8
    p.append(f'<line x1="{pad_l + 8}" y1="{ly}" x2="{pad_l + 30}" y2="{ly}" stroke="#2563eb" '
             f'stroke-width="2"/><text x="{pad_l + 34}" y="{ly + 4}" fill="#374151">指令</text>')
    p.append(f'<line x1="{pad_l + 76}" y1="{ly}" x2="{pad_l + 98}" y2="{ly}" stroke="#059669" '
             f'stroke-width="2"/><text x="{pad_l + 102}" y="{ly + 4}" fill="#374151">阀位</text>')
    p.append(f'<rect x="{pad_l + 146}" y="{ly - 7}" width="14" height="8" fill="#dcfce7"/>'
             f'<text x="{pad_l + 166}" y="{ly + 4}" fill="#374151">关位带 ≤{band}%</text>')
    p.append("</svg>")
    return "".join(p)


def _pressure_chart(r, width=920, height=180, pad_l=56, pad_r=16, pad_t=14, pad_b=30):
    c = r["curves"]
    t, up, dn = c["t"], c["upstream_pressure"], c["downstream_pressure"]
    t0, t1 = t[0], t[-1]
    p_max = max(max(up), max(dn)) * 1.08
    x_of, xticks = _axes(t0, t1, width, height, pad_l, pad_r, pad_t, pad_b)

    def y_of(v):
        return pad_t + (p_max - v) / p_max * (height - pad_t - pad_b)

    p = [_frame(width, height, pad_l, pad_r, pad_t, pad_b),
         _seg_shading(r, x_of, pad_t, height, pad_b, width, pad_l, pad_r),
         xticks, _event_lines(r, x_of, pad_t, height, pad_b),
         f'<polyline points="{_polyline(t, up, x_of, y_of)}" fill="none" stroke="#d97706" '
         f'stroke-width="1.6"/>',
         f'<polyline points="{_polyline(t, dn, x_of, y_of)}" fill="none" stroke="#7c3aed" '
         f'stroke-width="1.6"/>',
         f'<text x="{pad_l + 4}" y="{pad_t + 12}" fill="#92400e">上游压力 (kPa)</text>',
         f'<text x="{pad_l + 110}" y="{pad_t + 12}" fill="#6d28d9">下游压力 (kPa)</text>',
         "</svg>"]
    return "".join(p)


def _temp_chart(r, width=920, height=150, pad_l=56, pad_r=16, pad_t=14, pad_b=30):
    c = r["curves"]
    t, temp = c["t"], c["downstream_temp"]
    t0, t1 = t[0], t[-1]
    lo, hi = min(temp), max(temp)
    span = max(hi - lo, 0.5)
    lo, hi = lo - 0.15 * span, hi + 0.15 * span
    env = (r["metrics"].get("blank_envelope") or {})
    x_of, xticks = _axes(t0, t1, width, height, pad_l, pad_r, pad_t, pad_b)

    def y_of(v):
        return pad_t + (hi - v) / (hi - lo) * (height - pad_t - pad_b)

    p = [_frame(width, height, pad_l, pad_r, pad_t, pad_b),
         _seg_shading(r, x_of, pad_t, height, pad_b, width, pad_l, pad_r)]
    if env:
        ey0, ey1 = y_of(min(env["temp_hi"], hi)), y_of(max(env["temp_lo"], lo))
        p.append(f'<rect x="{pad_l}" y="{ey0:.1f}" width="{width - pad_l - pad_r}" '
                 f'height="{max(ey1 - ey0, 1):.1f}" fill="#e0e7ff" opacity="0.6">'
                 f'<title>空白基线温度包络 {env["temp_lo"]}–{env["temp_hi"]}°C</title></rect>')
    p.append(xticks)
    p.append(f'<polyline points="{_polyline(t, temp, x_of, y_of)}" fill="none" '
             f'stroke="#dc2626" stroke-width="1.6"/>')
    p.append(f'<text x="{pad_l + 4}" y="{pad_t + 12}" fill="#991b1b">下游温度 (°C)'
             f'　<span fill="#4338ca">蓝带=空白基线温度包络</span></text>')
    p.append("</svg>")
    return "".join(p)


def _leak_chart(r, width=920, height=200, pad_l=64, pad_r=16, pad_t=14, pad_b=30):
    c = r["curves"]
    t, cum, rate = c["t"], c["cumulative_leak_nl"], c["leak_rate_nl_min"]
    t0, t1 = t[0], t[-1]
    thr = r["thresholds"]
    cum_max = max(max(cum), thr["cumulative_leak_max_nl"]) * 1.15
    rate_vals = [v for v in rate if v is not None]
    rate_max = max(max(rate_vals) if rate_vals else 0.0,
                   thr["leak_rate_max_nl_min"]) * 1.15
    x_of, xticks = _axes(t0, t1, width, height, pad_l, pad_r, pad_t, pad_b)

    def y_cum(v):
        return pad_t + (cum_max - v) / cum_max * (height - pad_t - pad_b)

    def y_rate(v):
        return pad_t + (rate_max - v) / rate_max * (height - pad_t - pad_b)

    p = [_frame(width, height, pad_l, pad_r, pad_t, pad_b),
         _seg_shading(r, x_of, pad_t, height, pad_b, width, pad_l, pad_r), xticks]
    y = y_cum(thr["cumulative_leak_max_nl"])
    p.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
             f'stroke="#dc2626" stroke-dasharray="5,3"/>')
    p.append(f'<text x="{width - pad_r - 4}" y="{y - 4:.1f}" text-anchor="end" '
             f'fill="#dc2626">累计限值 {thr["cumulative_leak_max_nl"]} Nl</text>')
    y = y_rate(thr["leak_rate_max_nl_min"])
    p.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
             f'stroke="#b45309" stroke-dasharray="5,3"/>')
    p.append(f'<text x="{width - pad_r - 4}" y="{y + 12:.1f}" text-anchor="end" '
             f'fill="#b45309">泄漏率限值 {thr["leak_rate_max_nl_min"]} Nl/min</text>')
    p.append(f'<polyline points="{_polyline(t, cum, x_of, y_cum)}" fill="none" '
             f'stroke="#dc2626" stroke-width="1.8"/>')
    p.append(f'<polyline points="{_polyline(t, rate, x_of, y_rate)}" fill="none" '
             f'stroke="#b45309" stroke-width="1.4" stroke-dasharray="2,1"/>')
    fe = r["metrics"].get("first_exceedance")
    if fe:
        x = x_of(fe["t_s"])
        p.append(f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{height - pad_b}" '
                 f'stroke="#dc2626" stroke-width="1.4"/>')
        p.append(f'<text x="{x + 4:.1f}" y="{pad_t + 12}" fill="#dc2626">'
                 f'首次超限 {fe["t_s"]}s</text>')
    p.append(f'<text x="{pad_l + 4}" y="{pad_t + 12}" fill="#991b1b">累计漏量 (Nl)</text>')
    p.append(f'<text x="{pad_l + 110}" y="{pad_t + 12}" fill="#92400e">泄漏率 (Nl/min，虚线)</text>')
    p.append("</svg>")
    return "".join(p)


def _checks_table(r):
    out = ["<table><thead><tr><th>检查项</th><th>实测</th><th>阈值</th><th>依据</th>"
           "<th>判定</th></tr></thead><tbody>"]
    for c in r.get("checks", []):
        val = "—" if c.get("value") is None else c["value"]
        limit = (f"≤ {c['threshold_max']}" if "threshold_max" in c
                 else (f"≥ {c['threshold_min']}" if "threshold_min" in c else "—"))
        ok = c.get("pass")
        out.append(f'<tr><td>{html.escape(c["metric"])}</td><td>{val}</td><td>{limit}</td>'
                   f'<td class="reason">{html.escape(c.get("basis", ""))}</td>'
                   f'<td class="{"ok" if ok else "bad"}">{"通过" if ok else "不通过"}</td></tr>')
    out.append("</tbody></table>")
    return "".join(out)


def _segments_table(r):
    rows = []
    for seg in r.get("segments", []):
        if seg.get("t_start") is None or seg.get("t_end") is None:
            rows.append(f'<tr><td>{SEG_NAMES[seg["type"]]}</td><td colspan="3">未圈定</td></tr>')
            continue
        refs = seg.get("references") or {}
        ref_txt = ""
        for key, name in (("start", "起点"), ("end", "终点")):
            pts = "，".join(f"{x['channel']}#{x['index']}@{x['t']}s"
                            for x in refs.get(key, []))
            ref_txt += f"{name}原始点：{pts}<br/>"
        rows.append(
            f'<tr><td>{SEG_NAMES[seg["type"]]}</td>'
            f'<td>{seg["t_start"]} – {seg["t_end"]} s</td>'
            f'<td class="reason">起点：{html.escape(seg["start_reason"])}<br/>'
            f'终点：{html.escape(seg["end_reason"])}</td>'
            f'<td class="reason">{ref_txt}</td></tr>')
    return ("<table><thead><tr><th>区段</th><th>时间 (s)</th><th>边界依据</th>"
            "<th>原始点引用</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def _compensation_table(r):
    comp = r.get("compensation")
    if not comp:
        return "<p>—（未执行补偿计算，见证据缺口）</p>"
    params = comp["parameters"]
    rows = []
    for ep in comp["evaluation_points"]:
        refs = "，".join(f"{x['channel']}#{x['index']}@{x['t']}s={x.get('value')}"
                         for x in ep["raw_refs"])
        rows.append(
            f"<tr><td>{ep['t_s']}</td><td>{ep['p_down_kpa']}</td><td>{ep['temp_c']}</td>"
            f"<td>{ep['veq_nl']}</td><td>{ep['blank_correction_nl']}</td>"
            f"<td>{ep['net_cumulative_nl']}</td>"
            f"<td>{ep['leak_rate_nl_min'] if ep['leak_rate_nl_min'] is not None else '—'}</td>"
            f'<td class="reason">{html.escape(refs)}</td></tr>')
    return (f'<p class="reason">方法：{html.escape(comp["method"])}；参数：'
            f'V={params["volume_m3"]} m³，Z={params["z"]}，'
            f'T_ref={params["t_ref_k"]} K，P_ref={params["p_ref_kpa"]} kPa，'
            f'方向符号 {params["direction_sign"]:+g}；'
            f'空白回升 {params["blank_rate_kpa_min"]} kPa/min'
            f'（{html.escape(comp["blank_interp_note"])}）。</p>'
            '<table><thead><tr><th>t (s)</th><th>P下游 (kPa)</th><th>T (°C)</th>'
            '<th>Veq (Nl)</th><th>空白修正 (Nl)</th><th>净累计 (Nl)</th>'
            '<th>泄漏率 (Nl/min)</th><th>原始点引用</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def render_seat_report(analysis, test, valve):
    r = analysis["result"]
    esc = html.escape
    m = r.get("metrics", {})
    verdict = r["verdict"]

    events = "".join(
        f"<li>t={e['t']}s　{ACTION_NAMES.get(e['action'], e['action'])}"
        f"（隔离记录 #{e['index']}{'，' + esc(e['note']) if e.get('note') else ''}）</li>"
        for e in (r.get("isolation") or {}).get("events", [])) or "<li>无隔离记录</li>"
    gaps = "".join(f"<li>[{esc(g['code'])}] {esc(g['detail'])}</li>"
                   for g in r.get("evidence_gaps", [])) or "<li>无</li>"
    issues = "".join(
        f"<li>[{esc(i['kind'])}] {esc(i['detail'])}"
        + (f"（时段：{', '.join(f'{a}–{b}s' for a, b in i.get('periods', []))}）"
           if i.get("periods") else "") + "</li>"
        for i in r.get("issues", [])) or "<li>无</li>"
    def _adj_line(a):
        if a["type"] == "segment_move":
            body = f"移动区间 {a['segment']}.{a['boundary']} → {a['new_time']}s"
        elif a["type"] == "calibration_rebind":
            body = ("改绑逐通道校准版本 "
                    + json.dumps(a.get("bindings", {}), ensure_ascii=False)
                    + "（派生新版本，旧分析冻结版本不变）")
        else:
            body = f"屏蔽 {a['channel']} #{a['start_index']}–#{a['end_index']}"
        return (f"<li>v{analysis['version']} / {esc(a.get('author', ''))}：{body}"
                f"（{esc(a.get('reason', ''))}）</li>")

    adjustments = "".join(_adj_line(a) for a in analysis["adjustments"]) \
        or "<li>无（自动分析）</li>"
    basis = "".join(f"<li>{esc(b)}</li>" for b in r.get("decision_basis", [])) or "<li>—</li>"
    exclusions_detail = "".join(
        f'<li>{esc(e["channel"])} 原始点 #{e["start_index"]}–#{e["end_index"]}，'
        f'共 {len(e["original_points"])} 点，理由：{esc(e["reason"])}</li>'
        for e in r.get("exclusions", [])) or "<li>无</li>"

    fc = r.get("flow_crosscheck")
    if fc and fc.get("deviation_pct") is not None:
        flow_html = (
            f"<ul><li>流量计均值 {fc['flow_mean_nl_min']} Nl/min，质量平衡 "
            f"{fc['mass_balance_nl_min']} Nl/min，偏差 {fc['deviation_pct']}%"
            f"（限值 {fc['deviation_limit_pct']}%）</li>"
            + "".join(f"<li class='reason'>偏差来源：{esc(s)}</li>"
                      for s in fc["deviation_sources"]) + "</ul>")
    elif fc:
        flow_html = f"<p class='reason'>{esc(fc.get('note', ''))}</p>"
    else:
        flow_html = "<p>未提供流量计序列。</p>"

    metric_rows = "".join(f"<tr><td>{n}</td><td>{v if v is not None else '—'}</td></tr>"
                          for n, v in [
        ("稳压段时长 (s)", m.get("stabilization_duration_s")),
        ("保持段时长 (s)", m.get("hold_duration_s")),
        ("隔离时阀位 (%)", m.get("position_at_isolation_pct")),
        ("保持段最高阀位 (%)", m.get("position_max_hold_pct")),
        ("保持段阀位漂移 (%)", m.get("position_drift_hold_pct")),
        ("有效压差中位 (kPa)", m.get("differential_median_kpa")),
        ("有效压差最小 (kPa)", m.get("differential_min_kpa")),
        ("保持段平均温度 (°C)", m.get("temp_mean_hold_c")),
        ("温度覆盖率", m.get("temp_coverage_hold")),
        ("下游压力 起→末 (kPa)",
         f"{m.get('p_down_start_kpa')} → {m.get('p_down_end_kpa')}"),
        ("等效标准体积 起→末 (Nl)", f"{m.get('veq_start_nl')} → {m.get('veq_end_nl')}"),
        ("空白回升 (kPa/min → Nl/min)",
         f"{m.get('blank_rate_kpa_min')} → {m.get('blank_rate_nl_min')}"),
        ("泄漏率均值 (Nl/min)", m.get("leak_rate_mean_nl_min")),
        ("泄漏率窗口最大 (Nl/min)", m.get("leak_rate_max_nl_min")),
        ("累计漏量 (Nl)", m.get("cumulative_leak_nl")),
    ])

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"/>
<title>阀座密封保持试验报告 {esc(valve['tag'])} 试验#{test['id']} v{analysis['version']}</title>
<style>
 body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; margin: 24px; color: #111827; }}
 h1 {{ font-size: 20px; }} h2 {{ font-size: 15px; margin-top: 20px; border-bottom: 1px solid #d1d5db; padding-bottom: 4px; }}
 table {{ border-collapse: collapse; font-size: 12px; }}
 th, td {{ border: 1px solid #d1d5db; padding: 4px 8px; text-align: left; }}
 th {{ background: #f3f4f6; }}
 .ok {{ color: #059669; font-weight: 600; }} .bad {{ color: #dc2626; font-weight: 600; }}
 .warn {{ color: #d97706; }} .reason {{ font-size: 11px; color: #4b5563; }}
 .verdict {{ display: inline-block; padding: 2px 10px; border-radius: 4px; font-weight: 700;
   background: {'#d1fae5' if verdict == 'pass' else ('#fee2e2' if verdict == 'fail' else '#fef3c7')}; }}
 .meta td {{ border: none; padding: 2px 12px 2px 0; }}
 ul {{ font-size: 12px; margin: 6px 0; }}
 @media print {{ body {{ margin: 8mm; }} h2 {{ page-break-after: avoid; }} svg {{ max-width: 100%; }} }}
</style></head><body>
<h1>气体阀座密封保持试验报告</h1>
<table class="meta"><tr>
<td><b>阀门位号</b>：{esc(valve['tag'])}</td>
<td><b>阀座配置</b>：{esc(str(r.get('seat_config')))}</td>
<td><b>流向</b>：{DIRECTION_NAMES.get(r['flow_direction'], r['flow_direction'])}</td>
<td><b>试验编号</b>：{test['id']}（{esc(test['phase'])}）</td>
<td><b>分析版本</b>：v{analysis['version']}（{esc(analysis['author'])}，{esc(analysis['created_at'])}）</td>
<td><b>结论</b>：<span class="verdict">{VERDICT_NAMES[verdict]}</span></td></tr></table>

<h2>关阀过程与关位带（含稳压段/保持段底色与隔离事件）</h2>
{_travel_chart(r) if r.get("curves") else "<p class='bad'>无对齐曲线（见证据缺口）。</p>"}
<h2>上下游压力</h2>
{_pressure_chart(r) if r.get("curves") else "<p>—</p>"}
<h2>下游温度与空白基线包络</h2>
{_temp_chart(r) if r.get("curves") else "<p>—</p>"}
<h2>累计漏量与泄漏率（温压补偿并扣除空白回升后）</h2>
{_leak_chart(r) if r.get("curves") else "<p>—</p>"}

<h2>隔离动作时间线</h2>
<ul>{events}</ul>

<h2>段划分与边界依据（含原始点引用）</h2>
{_segments_table(r)}

<h2>试验指标</h2>
<table><tbody>{metric_rows}</tbody></table>

<h2>温压补偿过程（等效标准体积换算与空白扣除，含原始点引用）</h2>
{_compensation_table(r)}

<h2>阈值检查</h2>
{_checks_table(r)}

<h2>流量计交叉核对</h2>
{flow_html}

<h2>判定依据明细</h2>
<ul>{basis}</ul>

<h2>超限与异常</h2>
<ul>{issues}</ul>

{render_calibration_section(r)}

<h2>证据缺口（存在时不得给出合格结论）</h2>
<ul>{gaps}</ul>

<h2>版本与人工调整记录</h2>
<ul>{adjustments}</ul>

<h2>屏蔽点明细（保留原始引用）</h2>
<ul>{exclusions_detail}</ul>

<p class="reason">单位处理：{esc('；'.join(r.get('unit_notes', [])) or '均为原生单位')}。
对齐网格：{r['alignment']['grid_step_s'] if r.get('alignment') else '—'}s ×
{r['alignment']['n_points'] if r.get('alignment') else '—'} 点。
报告由分析版本 v{analysis['version']} 生成，数据可追溯至试验 #{test['id']} 原始采样。</p>
</body></html>"""
