"""打印报告：HTML + 内联 SVG 曲线，标注阈值、分段、检测事件与分析版本。

指标区间与复算参数直接取自分析结果中的 uncertainty 块，与 JSON 导出
（/analyses/{id}/export）共用同一组数据，报告不另行重算。
"""

import html

SEG_COLORS = {"opening": "#dbeafe", "closing": "#ffedd5", "dwell": "#f3f4f6"}
SEG_NAMES = {"opening": "开阀", "closing": "关阀", "dwell": "停留"}
VERDICT_NAMES = {"ok": "正常", "exceedances": "存在超限", "no_conclusion": "不得用于结论",
                 "indeterminate": "符合性不确定（测量不确定度区间跨越限值）"}
UNC_STATUS_NAMES = {
    "pass": ("区间合格", "ok"),
    "fail": ("区间超限", "bad"),
    "indeterminate": ("符合性不确定", "warn"),
    "not_applicable": ("不适用", "warn"),
    "not_evaluated": ("未评估", "warn"),
    "invalid": ("评估无效", "bad"),
}
UNC_METRIC_ORDER = [
    ("travel_time", "行程时间 (s)"),
    ("deadband", "死区 (%)"),
    ("hysteresis", "回差 (%)"),
    ("overshoot", "过冲 (%)"),
    ("steady_state", "稳态偏差 (%)"),
]


def _polyline(xs, ys, x_of, y_of):
    pts = " ".join(f"{x_of(x):.1f},{y_of(y):.1f}" for x, y in zip(xs, ys))
    return pts


def _chart(result, width=920, height=300, pad_l=50, pad_r=16, pad_t=16, pad_b=34):
    t = result["curves"]["t"]
    cmd = result["curves"]["command"]
    pos = result["curves"]["position"]
    t0, t1 = t[0], t[-1]
    v0, v1 = -5.0, 105.0

    def x_of(tv):
        return pad_l + (tv - t0) / (t1 - t0) * (width - pad_l - pad_r)

    def y_of(v):
        return pad_t + (v1 - v) / (v1 - v0) * (height - pad_t - pad_b)

    parts = [f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
             f'xmlns="http://www.w3.org/2000/svg" font-family="sans-serif" font-size="11">']
    # 分段底色
    for seg in result["segments"]:
        x0, x1 = x_of(seg["t_start"]), x_of(seg["t_end"])
        parts.append(
            f'<rect x="{x0:.1f}" y="{pad_t}" width="{max(x1 - x0, 1):.1f}" '
            f'height="{height - pad_t - pad_b}" fill="{SEG_COLORS[seg["type"]]}"/>')
    # 网格与坐标
    for v in range(0, 101, 25):
        y = y_of(v)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
                     f'stroke="#e5e7eb" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" fill="#6b7280">{v}%</text>')
    n_ticks = 8
    for k in range(n_ticks + 1):
        tv = t0 + (t1 - t0) * k / n_ticks
        parts.append(f'<text x="{x_of(tv):.1f}" y="{height - pad_b + 16}" text-anchor="middle" '
                     f'fill="#6b7280">{tv:.1f}s</text>')
    # 曲线
    parts.append(f'<polyline points="{_polyline(t, cmd, x_of, y_of)}" fill="none" '
                 f'stroke="#2563eb" stroke-width="1.8"/>')
    parts.append(f'<polyline points="{_polyline(t, pos, x_of, y_of)}" fill="none" '
                 f'stroke="#059669" stroke-width="1.8"/>')
    # 检测事件标记
    for ep in result["detections"]["stick_slip"]:
        x = x_of(ep["t_start"])
        parts.append(f'<path d="M {x:.1f} {pad_t + 4} l 6 10 h -12 z" fill="#dc2626">'
                     f'<title>卡跳 {ep["jump_pct"]}% @ {ep["t_start"]}s</title></path>')
    for ev in result["detections"]["dropouts"]:
        x0, x1 = x_of(ev["t_start"]), x_of(ev["t_end"])
        parts.append(f'<rect x="{x0:.1f}" y="{pad_t}" width="{max(x1 - x0, 2):.1f}" height="8" '
                     f'fill="#7c3aed"><title>断档 {ev["channel"]}/{ev["kind"]}</title></rect>')
    # 图例
    parts.append(f'<line x1="{pad_l + 8}" y1="{height - 10}" x2="{pad_l + 30}" y2="{height - 10}" '
                 f'stroke="#2563eb" stroke-width="2"/><text x="{pad_l + 34}" y="{height - 6}" '
                 f'fill="#374151">指令</text>')
    parts.append(f'<line x1="{pad_l + 76}" y1="{height - 10}" x2="{pad_l + 98}" y2="{height - 10}" '
                 f'stroke="#059669" stroke-width="2"/><text x="{pad_l + 102}" y="{height - 6}" '
                 f'fill="#374151">阀位</text>')
    parts.append(f'<path d="M {pad_l + 150} {height - 15} l 6 10 h -12 z" fill="#dc2626"/>'
                 f'<text x="{pad_l + 162}" y="{height - 6}" fill="#374151">卡跳</text>')
    parts.append(f'<rect x="{pad_l + 200}" y="{height - 15}" width="14" height="8" fill="#7c3aed"/>'
                 f'<text x="{pad_l + 220}" y="{height - 6}" fill="#374151">断档</text>')
    parts.append("</svg>")
    return "".join(parts)


def _pressure_chart(result, width=920, height=130, pad_l=50, pad_r=16, pad_t=10, pad_b=30):
    t = result["curves"]["t"]
    prs = result["curves"]["pressure"]
    t0, t1 = t[0], t[-1]
    p_max = max(max(prs) * 1.1, 1.0)
    supply_min = result["thresholds"]["supply_pressure_min_kpa"]

    def x_of(tv):
        return pad_l + (tv - t0) / (t1 - t0) * (width - pad_l - pad_r)

    def y_of(v):
        return pad_t + (p_max - v) / p_max * (height - pad_t - pad_b)

    parts = [f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
             f'xmlns="http://www.w3.org/2000/svg" font-family="sans-serif" font-size="11">']
    parts.append(f'<polyline points="{_polyline(t, prs, x_of, y_of)}" fill="none" '
                 f'stroke="#d97706" stroke-width="1.6"/>')
    y = y_of(supply_min)
    parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
                 f'stroke="#dc2626" stroke-width="1.2" stroke-dasharray="5,3"/>')
    parts.append(f'<text x="{width - pad_r - 4}" y="{y - 4:.1f}" text-anchor="end" fill="#dc2626">'
                 f'供压下限 {supply_min:.0f} kPa</text>')
    parts.append(f'<text x="{pad_l - 6}" y="{y_of(p_max) + 10:.1f}" text-anchor="end" '
                 f'fill="#6b7280">{p_max:.0f}</text>')
    parts.append(f'<text x="{pad_l - 6}" y="{y_of(0):.1f}" text-anchor="end" fill="#6b7280">0</text>')
    parts.append(f'<text x="{pad_l + 4}" y="{pad_t + 12}" fill="#92400e">执行器压力 (kPa)</text>')
    parts.append("</svg>")
    return "".join(parts)


def _fmt_interval(iv):
    if iv is None:
        return "—"
    return f"[{iv[0]}, {iv[1]}]"


def _metrics_table(result):
    """指标表：中心值 + 不确定度区间（与 JSON 导出共用同一组数据）。"""
    thr = result["thresholds"]
    m = result["metrics"]
    unc = result.get("uncertainty") or {}
    unc_metrics = unc.get("metrics") or {}
    rows = [
        ("行程时间 (s)", m["travel_time"]["max_s"], thr["travel_time_s_max"], "travel_time"),
        ("死区 (%)", m["deadband"]["max_pct"], thr["deadband_pct_max"], "deadband"),
        ("回差 (%)", m["hysteresis"]["max_pct"], thr["hysteresis_pct_max"], "hysteresis"),
        ("过冲 (%)", m["overshoot"]["max_pct"], thr["overshoot_pct_max"], "overshoot"),
        ("稳态偏差 (%)", m["steady_state"]["max_pct"], thr["steady_state_pct_max"], "steady_state"),
    ]
    out = ["<table><thead><tr><th>指标</th><th>中心值</th><th>不确定度区间</th>"
           "<th>阈值</th><th>中心值判定</th><th>区间符合性</th></tr></thead><tbody>"]
    for name, val, limit, ukey in rows:
        um = unc_metrics.get(ukey) or {}
        interval = um.get("interval")
        u_status = um.get("status")
        if val is None:
            out.append(f'<tr><td>{name}</td><td>—</td><td>{_fmt_interval(interval)}</td>'
                       f'<td colspan="3" class="warn">无数据/不适用</td></tr>')
            continue
        ok = val <= limit
        cls = "ok" if ok else "bad"
        if unc.get("status") == "evaluated" and u_status:
            uname, ucls = UNC_STATUS_NAMES.get(u_status, (u_status, "warn"))
            cell = f'<td class="{ucls}">{uname}</td>'
        elif unc.get("status") == "invalid":
            cell = '<td class="bad">评估无效</td>'
        else:
            cell = '<td class="warn">未评估</td>'
        note = f'<br/><span class="reason">{html.escape(um["note"])}</span>' if um.get("note") else ""
        out.append(
            f'<tr><td>{name}</td><td>{val}</td>'
            f'<td>{_fmt_interval(interval)}{note}</td>'
            f'<td>≤ {limit}</td>'
            f'<td class="{cls}">{"合格" if ok else "超限"}</td>{cell}</tr>')
    out.append("</tbody></table>")
    return "".join(out)


def _uncertainty_section(result):
    """测量不确定度评估段落：状态、分量、样本/种子/复算参数与无效原因。"""
    unc = result.get("uncertainty") or {}
    status = unc.get("status", "not_evaluated")
    if status == "not_evaluated":
        return (f'<h2>测量不确定度</h2><p class="warn">未评估：{html.escape(unc.get("reason", ""))}'
                f'。表中中心值判定仅供参考，贴限值时不得据此通过。</p>')
    if status == "invalid":
        reasons = "".join(f"<li>{html.escape(r)}</li>" for r in unc.get("reasons", []))
        return ("<h2>测量不确定度</h2><p class='bad'>评估无效，已列出原因：</p>"
                f"<ul>{reasons}</ul>")

    prob = unc.get("interval_prob", 0.95) * 100
    overall = unc.get("overall_status", "indeterminate")
    oname, ocls = UNC_STATUS_NAMES.get(overall, (overall, "warn"))
    comps = "".join(
        f'<tr><td>{c["channel"]}</td><td>{c["kind"]}</td>'
        f'<td>{c["declared"]["value"]} {html.escape(c["declared"]["unit"])}</td>'
        f'<td>{c["normalized_scale"]} {html.escape(c["normalized_unit"])}</td>'
        f'<td>{c["distribution"]}</td><td>{c["source"]}</td></tr>'
        for c in unc.get("components", [])) or '<tr><td colspan="6" class="warn">无</td></tr>'
    reasons = "".join(f"<li>{html.escape(r)}</li>" for r in unc.get("reasons", []))
    blocked = (f'<p class="bad">{html.escape(unc["blocked_note"])}</p>'
               if unc.get("blocked_note") else "")
    rp = unc.get("recompute_params") or {}
    press = unc.get("supply_pressure")
    press_line = ""
    if press:
        pname, pcls = UNC_STATUS_NAMES.get(press["status"], (press["status"], "warn"))
        press_line = (
            f'<p>运动段最低供压 {prob:.0f}% 区间 {_fmt_interval(press["interval"])} kPa'
            f'（下限 {press["threshold"]} kPa）：<b class="{pcls}">{pname}</b></p>')
    return f"""<h2>测量不确定度评估（蒙特卡洛）</h2>
<p>总体区间符合性：<b class="{ocls}">{oname}</b>。
区间为 {prob:.0f}% 经验区间（分位数法）；区间跨越判定阈值（含端点贴限）时符合性判为
“{UNC_STATUS_NAMES['indeterminate'][0]}”，不得只按中心值通过。</p>
<table><thead><tr><th>通道</th><th>分量</th><th>声明值</th><th>归一化尺度</th>
<th>分布</th><th>来源</th></tr></thead><tbody>{comps}</tbody></table>
{press_line}
<p class="reason">样本：请求 {unc.get('n_samples_requested')} 次，
有效 {unc.get('n_samples_valid')} 次，失败 {unc.get('n_samples_failed', 0)} 次；
固定种子 {unc.get('seed')}；方法 {rp.get('method','monte_carlo')}，
管线 {' → '.join((rp.get('pipeline') or '').split(' -> '))}；
分位点 {rp.get('quantiles')}。{html.escape(unc.get('note', ''))}</p>
{blocked}
{('<ul>' + reasons + '</ul>') if reasons else ''}"""


def _adjustment_line(a, version):
    if a["type"] == "exclusion":
        what = f"剔除 {a['channel']} #{a['start_index']}–{a['end_index']}"
    else:
        what = f"移动边界 段{a['segment_index']} {a['boundary']} → {a['new_time']}s"
    return (f'<li>v{version} / {html.escape(a.get("author", ""))}：{what}'
            f'（{html.escape(a.get("reason", ""))}）</li>')


def render_report(analysis, test, valve):
    r = analysis["result"]
    esc = html.escape
    segs = "".join(
        f'<tr><td>{s["index"]}</td><td>{SEG_NAMES[s["type"]]}</td>'
        f'<td>{s["t_start"]} – {s["t_end"]} s</td>'
        f'<td>{s["cmd_start"]}% → {s["cmd_end"]}%</td>'
        f'<td class="reason">起点：{esc(s["start_reason"])}<br/>终点：{esc(s["end_reason"])}</td></tr>'
        for s in r["segments"])
    issues = "".join(
        f'<li>[{i["kind"]}] {esc(i["detail"])}'
        + (f'（时段：{", ".join(f"{p[0]}–{p[1]}s" for p in i["periods"])}）' if i["periods"] else "")
        + "</li>"
        for i in r["issues"]) or "<li>无</li>"
    blocking = "".join(f"<li>{esc(b)}</li>" for b in r["blocking_issues"]) or "<li>无</li>"
    adjustments = "".join(
        _adjustment_line(a, analysis["version"]) for a in analysis["adjustments"]
    ) or "<li>无（自动分析）</li>"
    exclusions_detail = "".join(
        f'<li>{esc(e["channel"])} 原始点 #{e["start_index"]}–#{e["end_index"]}，'
        f'共 {len(e["original_points"])} 点，理由：{esc(e["reason"])}</li>'
        for e in r["exclusions"]) or "<li>无</li>"

    verdict_bg = {"ok": "#d1fae5", "no_conclusion": "#fee2e2"}.get(
        r["verdict"], "#fef3c7")
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"/>
<title>阀门诊断报告 {esc(valve['tag'])} 测试#{test['id']} v{analysis['version']}</title>
<style>
 body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; margin: 24px; color: #111827; }}
 h1 {{ font-size: 20px; }} h2 {{ font-size: 15px; margin-top: 20px; border-bottom: 1px solid #d1d5db; padding-bottom: 4px; }}
 table {{ border-collapse: collapse; font-size: 12px; }}
 th, td {{ border: 1px solid #d1d5db; padding: 4px 8px; text-align: left; }}
 th {{ background: #f3f4f6; }}
 .ok {{ color: #059669; font-weight: 600; }} .bad {{ color: #dc2626; font-weight: 600; }}
 .warn {{ color: #d97706; }} .reason {{ font-size: 11px; color: #4b5563; }}
 .verdict {{ display: inline-block; padding: 2px 10px; border-radius: 4px; font-weight: 700;
   background: {verdict_bg}; }}
 .meta td {{ border: none; padding: 2px 12px 2px 0; }}
 ul {{ font-size: 12px; margin: 6px 0; }}
 @media print {{ body {{ margin: 8mm; }} h2 {{ page-break-after: avoid; }} svg {{ max-width: 100%; }} }}
</style></head><body>
<h1>调节阀全行程诊断报告</h1>
<table class="meta"><tr><td><b>阀门位号</b>：{esc(valve['tag'])}</td>
<td><b>测试编号</b>：{test['id']}（{esc(test['phase'])}）</td>
<td><b>分析版本</b>：v{analysis['version']}（{esc(analysis['author'])}，{esc(analysis['created_at'])}）</td>
<td><b>结论</b>：<span class="verdict">{VERDICT_NAMES[r['verdict']]}</span></td></tr></table>

<h2>行程曲线（指令 / 阀位，含分段与事件标注）</h2>
{_chart(r)}
<h2>执行器压力与供压下限</h2>
{_pressure_chart(r)}

<h2>指标与阈值</h2>
{_metrics_table(r)}

{_uncertainty_section(r)}

<h2>区段划分与边界依据</h2>
<table><thead><tr><th>#</th><th>类型</th><th>时间 (s)</th><th>指令变化</th><th>边界依据</th></tr></thead>
<tbody>{segs}</tbody></table>

<h2>超限与异常事件</h2>
<ul>{issues}</ul>

<h2>阻断问题（存在时不得用于维修结论）</h2>
<ul>{blocking}</ul>

<h2>人工调整记录</h2>
<ul>{adjustments}</ul>

<h2>剔除点明细（保留原始引用）</h2>
<ul>{exclusions_detail}</ul>

<p class="reason">单位处理：{esc('；'.join(r['unit_notes']) or '均为原生单位')}。
对齐网格：{r['alignment']['grid_step_s']}s × {r['alignment']['n_points']} 点，
区间 [{r['alignment']['t0']}, {r['alignment']['t1']}]s。
报告由分析版本 v{analysis['version']} 生成，数据可追溯至测试 #{test['id']} 原始采样。</p>
</body></html>"""
