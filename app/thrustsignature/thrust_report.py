"""阀杆推力签名打印报告：HTML + 内联 SVG。

包含指令/阀位与行程相位底色、供气与工作腔压力、阀杆净推力签名（标注启程/
离座/匀速/落座区间与限值线）、相位划分依据、推力指标与阈值检查、异常区间
（含卡涩来源嫌疑）、证据缺口、版本调整与剔除点引用。
"""

import html

VERDICT_NAMES = {"pass": "合格", "fail": "推力/摩擦超限", "no_conclusion": "证据不足，不得判定"}
PHASE_CN = {"breakaway": "启程", "unseat": "离座", "running": "匀速",
            "seating": "落座", "reversal": "换向停留"}
RUN_CN = {"opening": "开阀", "closing": "关阀"}
PHASE_COLORS = {"breakaway": "#fde68a", "unseat": "#fecaca",
                "running": "#bbf7d0", "seating": "#bfdbfe",
                "reversal": "#e5e7eb"}
SOURCE_CN = {"actuator": "执行机构嫌疑", "valve_body": "阀体/填料嫌疑",
             "valve_seat": "阀座负载嫌疑", "unknown": "来源待区分"}


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


def _phase_shading(r, x_of, pad_t, height, pad_b):
    out = []
    for ph in r.get("phases", []):
        if ph["phase"] == "reversal":
            continue
        x0, x1 = x_of(ph["t_start"]), x_of(ph["t_end"])
        out.append(
            f'<rect x="{x0:.1f}" y="{pad_t}" width="{max(x1 - x0, 1):.1f}" '
            f'height="{height - pad_t - pad_b}" fill="{PHASE_COLORS[ph["phase"]]}" '
            f'opacity="0.45"><title>{RUN_CN.get(ph["run"], "")}{PHASE_CN[ph["phase"]]} '
            f'{ph["t_start"]}–{ph["t_end"]}s</title></rect>')
    return "".join(out)


def _travel_chart(r, width=920, height=260, pad_l=50, pad_r=16, pad_t=16, pad_b=34):
    c = r["curves"]
    t, cmd, pos = c["t"], c["command"], c["position"]
    t0, t1, v0, v1 = t[0], t[-1], -5.0, 105.0
    x_of, xticks = _axes(t0, t1, width, height, pad_l, pad_r, pad_t, pad_b)

    def y_of(v):
        return pad_t + (v1 - v) / (v1 - v0) * (height - pad_t - pad_b)

    band = r["thresholds"]["closed_band_pct"]
    p = [_frame(width, height, pad_l, pad_r, pad_t, pad_b),
         _phase_shading(r, x_of, pad_t, height, pad_b)]
    y0, y1v = y_of(band), y_of(0.0)
    p.append(f'<rect x="{pad_l}" y="{y0:.1f}" width="{width - pad_l - pad_r}" '
             f'height="{y1v - y0:.1f}" fill="#e0e7ff" opacity="0.7"/>')
    for v in range(0, 101, 25):
        y = y_of(v)
        p.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
                 f'stroke="#e5e7eb"/>')
        p.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" '
                 f'fill="#6b7280">{v}%</text>')
    p.append(xticks)
    p.append(f'<polyline points="{_polyline(t, cmd, x_of, y_of)}" fill="none" '
             f'stroke="#2563eb" stroke-width="1.6"/>')
    p.append(f'<polyline points="{_polyline(t, pos, x_of, y_of)}" fill="none" '
             f'stroke="#059669" stroke-width="1.8"/>')
    ly = height - 8
    p.append(f'<line x1="{pad_l + 8}" y1="{ly}" x2="{pad_l + 30}" y2="{ly}" '
             f'stroke="#2563eb" stroke-width="2"/>'
             f'<text x="{pad_l + 34}" y="{ly + 4}" fill="#374151">指令</text>')
    p.append(f'<line x1="{pad_l + 76}" y1="{ly}" x2="{pad_l + 98}" y2="{ly}" '
             f'stroke="#059669" stroke-width="2"/>'
             f'<text x="{pad_l + 102}" y="{ly + 4}" fill="#374151">阀位</text>')
    p.append("</svg>")
    return "".join(p)


def _pressure_chart(r, width=920, height=170, pad_l=56, pad_r=16, pad_t=14, pad_b=30):
    c = r["curves"]
    t = c["t"]
    series = [("supply_pressure", "#d97706", "供气压力"),
              ("chamber_a_pressure", "#7c3aed", "A 腔压力")]
    if "chamber_b_pressure" in c:
        series.append(("chamber_b_pressure", "#0891b2", "B 腔压力"))
    pmax = max(max(c[k]) for k, _, _ in series) * 1.08
    t0, t1 = t[0], t[-1]
    x_of, xticks = _axes(t0, t1, width, height, pad_l, pad_r, pad_t, pad_b)

    def y_of(v):
        return pad_t + (pmax - v) / pmax * (height - pad_t - pad_b)

    p = [_frame(width, height, pad_l, pad_r, pad_t, pad_b),
         _phase_shading(r, x_of, pad_t, height, pad_b), xticks]
    y = y_of(r["thresholds"]["supply_pressure_min_kpa"])
    p.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
             f'stroke="#dc2626" stroke-dasharray="5,3"/>')
    p.append(f'<text x="{width - pad_r - 4}" y="{y - 4:.1f}" text-anchor="end" '
             f'fill="#dc2626">供气下限 '
             f'{r["thresholds"]["supply_pressure_min_kpa"]:.0f} kPa</text>')
    ox = pad_l + 4
    for key, color, label in series:
        p.append(f'<polyline points="{_polyline(t, c[key], x_of, y_of)}" fill="none" '
                 f'stroke="{color}" stroke-width="1.6"/>')
        p.append(f'<text x="{ox}" y="{pad_t + 12}" fill="{color}">{label} (kPa)</text>')
        ox += 120
    p.append("</svg>")
    return "".join(p)


def _thrust_chart(r, width=920, height=240, pad_l=60, pad_r=16, pad_t=14, pad_b=30):
    c = r["curves"]
    t, fnet = c["t"], c["f_net_n"]
    t0, t1 = t[0], t[-1]
    thr = r["thresholds"]
    vmax = max(max(abs(v) for v in fnet),
               thr["breakaway_open_max_n"], thr["seating_max_n"]) * 1.15
    x_of, xticks = _axes(t0, t1, width, height, pad_l, pad_r, pad_t, pad_b)

    def y_of(v):
        return pad_t + (vmax - v) / (2 * vmax) * (height - pad_t - pad_b)

    p = [_frame(width, height, pad_l, pad_r, pad_t, pad_b),
         _phase_shading(r, x_of, pad_t, height, pad_b)]
    p.append(f'<line x1="{pad_l}" y1="{y_of(0):.1f}" x2="{width - pad_r}" '
             f'y2="{y_of(0):.1f}" stroke="#9ca3af"/>')
    for key, val in (("启动力/离座力限值", thr["breakaway_open_max_n"]),
                     ("落座最大", thr["seating_max_n"]),
                     ("落座最小", thr["seating_min_n"])):
        for sgn in (1, -1):
            y = y_of(sgn * val)
            p.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" '
                     f'y2="{y:.1f}" stroke="#fca5a5" stroke-dasharray="4,3"/>')
        p.append(f'<text x="{width - pad_r - 4}" y="{y_of(val) - 3:.1f}" '
                 f'text-anchor="end" fill="#b91c1c" font-size="10">{key} '
                 f'{val:.0f} N</text>')
    p.append(xticks)
    p.append(f'<polyline points="{_polyline(t, fnet, x_of, y_of)}" fill="none" '
             f'stroke="#111827" stroke-width="1.8"/>')
    p.append(f'<text x="{pad_l + 4}" y="{pad_t + 12}" fill="#111827">'
             f'阀杆净推力 F_net（N，开阀方向为正）</text>')
    p.append("</svg>")
    return "".join(p)


def _checks_table(r):
    names = {
        "breakaway_open_peak_n": "开阀启动力", "breakaway_close_peak_n": "关阀启动力",
        "running_friction_open_n": "开阀运行摩擦", "running_friction_close_n": "关阀运行摩擦",
        "friction_band_open_n": "开阀摩擦带", "friction_band_close_n": "关阀摩擦带",
        "friction_band_total_n": "总摩擦带", "unseat_open_peak_n": "开阀离座力",
        "seating_close_n": "关阀落座力", "seating_margin_n": "落座裕量",
    }
    out = ["<table><thead><tr><th>检查项</th><th>实测 (N)</th><th>阈值 (N)</th>"
           "<th>依据</th><th>判定</th></tr></thead><tbody>"]
    for c in r.get("checks", []):
        limit = (f"≤ {c['threshold_max']}" if "threshold_max" in c
                 else f"≥ {c['threshold_min']}" if "threshold_min" in c else "—")
        ok = c["pass"]
        out.append(
            f'<tr><td>{html.escape(names.get(c["metric"], c["metric"]))}</td>'
            f'<td>{"—" if c["value"] is None else c["value"]}</td><td>{limit}</td>'
            f'<td class="reason">{html.escape(c.get("basis", ""))}</td>'
            f'<td class="{"ok" if ok else "bad"}">{"通过" if ok else "不通过"}</td></tr>')
    out.append("</tbody></table>")
    return "".join(out)


def _phases_table(r):
    rows = []
    for ph in r.get("phases", []):
        rows.append(
            f'<tr><td>{RUN_CN.get(ph["run"], "")}</td><td>{PHASE_CN[ph["phase"]]}</td>'
            f'<td>{ph["t_start"]} – {ph["t_end"]} s</td>'
            f'<td>{ph.get("position_start_pct", "—")}% → '
            f'{ph.get("position_end_pct", "—")}%</td>'
            f'<td class="reason">起：{html.escape(ph["start_reason"])}<br/>'
            f'止：{html.escape(ph["end_reason"])}</td></tr>')
    return ("<table><thead><tr><th>方向</th><th>相位</th><th>时间</th><th>阀位</th>"
            "<th>边界依据</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def render_thrust_report(analysis, test, valve):
    r = analysis["result"]
    esc = html.escape
    m = r.get("metrics", {})
    verdict = r["verdict"]
    act = r.get("actuator") or {}

    gaps = "".join(f"<li>[{esc(g['code'])}] {esc(g['detail'])}</li>"
                   for g in r.get("evidence_gaps", [])) or "<li>无</li>"
    issues = "".join(
        f"<li>[{esc(i['kind'])}] {esc(i['detail'])}"
        f"（{esc(SOURCE_CN.get(i.get('suspected_source'), '来源待区分'))}）"
        + (f"（时段：{', '.join(f'{a}–{b}s' for a, b in i.get('periods', []))}）"
           if i.get("periods") else "") + "</li>"
        for i in r.get("issues", [])) or "<li>无</li>"
    adjustments = "".join(
        f"<li>v{analysis['version']} / {esc(a.get('author', ''))}："
        + (f"移动 {a['run']}.{PHASE_CN.get(a['phase'], a['phase'])}.{a['boundary']} "
           f"→ {a['new_time']}s"
           if a["type"] == "segment_move"
           else f"屏蔽 {a['channel']} #{a['start_index']}–#{a['end_index']}")
        + f"（{esc(a.get('reason', ''))}）</li>"
        for a in analysis["adjustments"]) or "<li>无（自动分析）</li>"
    basis = "".join(f"<li>{esc(b)}</li>" for b in r.get("decision_basis", [])) or "<li>—</li>"
    exclusions_detail = "".join(
        f'<li>{esc(e["channel"])} 原始点 #{e["start_index"]}–#{e["end_index"]}，'
        f'共 {len(e["original_points"])} 点，理由：{esc(e["reason"])}</li>'
        for e in r.get("exclusions", [])) or "<li>无</li>"

    metric_rows = "".join(f"<tr><td>{n}</td><td>{m.get(k) if m.get(k) is not None else '—'}</td></tr>"
                          for k, (n, _u) in [
        ("breakaway_open_peak_n", ("开阀启动力峰值", "N")),
        ("breakaway_close_peak_n", ("关阀启动力峰值", "N")),
        ("unseat_open_peak_n", ("开阀离座力峰值", "N")),
        ("running_friction_open_n", ("开阀运行摩擦（匀速中位绝对值）", "N")),
        ("running_friction_close_n", ("关阀运行摩擦（匀速中位绝对值）", "N")),
        ("friction_band_open_n", ("开阀摩擦带（匀速段峰峰）", "N")),
        ("friction_band_close_n", ("关阀摩擦带（匀速段峰峰）", "N")),
        ("friction_band_total_n", ("总摩擦带（开/关中位力之差）", "N")),
        ("seating_close_n", ("关阀落座力", "N")),
        ("seating_margin_n", ("落座裕量", "N")),
    ])

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"/>
<title>阀杆推力签名报告 {esc(valve['tag'])} 试验#{test['id']} v{analysis['version']}</title>
<style>
 body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; margin: 24px; color: #111827; }}
 h1 {{ font-size: 20px; }} h2 {{ font-size: 15px; margin-top: 20px; border-bottom: 1px solid #d1d5db; padding-bottom: 4px; }}
 table {{ border-collapse: collapse; font-size: 12px; }}
 th, td {{ border: 1px solid #d1d5db; padding: 4px 8px; text-align: left; }}
 th {{ background: #f3f4f6; }}
 .ok {{ color: #059669; font-weight: 600; }} .bad {{ color: #dc2626; font-weight: 600; }}
 .reason {{ font-size: 11px; color: #4b5563; }}
 .verdict {{ display: inline-block; padding: 2px 10px; border-radius: 4px; font-weight: 700;
   background: {'#d1fae5' if verdict == 'pass' else ('#fee2e2' if verdict == 'fail' else '#fef3c7')}; }}
 .meta td {{ border: none; padding: 2px 12px 2px 0; }}
 ul {{ font-size: 12px; margin: 6px 0; }}
 @media print {{ body {{ margin: 8mm; }} h2 {{ page-break-after: avoid; }} svg {{ max-width: 100%; }} }}
</style></head><body>
<h1>阀杆推力签名诊断报告</h1>
<table class="meta"><tr>
<td><b>阀门位号</b>：{esc(valve['tag'])}</td>
<td><b>执行器</b>：{esc(str(act.get('actuator_type')))}</td>
<td><b>阀杆方向</b>：{esc(str(act.get('stem_direction')))}</td>
<td><b>面积 A/B</b>：{act.get('area_a_m2')} / {act.get('area_b_m2') or '—'} m²</td>
<td><b>弹簧版本</b>：{esc(str(act.get('spring_version') or '—'))}</td>
<td><b>负载</b>：{esc(str(r.get('load_condition')))}</td>
<td><b>试验编号</b>：{test['id']}（{esc(test['phase'])}）</td>
<td><b>分析版本</b>：v{analysis['version']}（{esc(analysis['author'])}，{esc(analysis['created_at'])}）</td>
<td><b>结论</b>：<span class="verdict">{VERDICT_NAMES[verdict]}</span></td></tr></table>

<h2>指令 / 阀位与行程相位（黄=启程，红=离座，绿=匀速，蓝=落座）</h2>
{_travel_chart(r) if r.get("curves") else "<p class='bad'>无对齐曲线（见证据缺口）。</p>"}
<h2>供气与工作腔压力</h2>
{_pressure_chart(r) if r.get("curves") else "<p>—</p>"}
<h2>阀杆净推力签名（净推力 = 压差×有效面积 ± 弹簧力）</h2>
{_thrust_chart(r) if r.get("curves") else "<p>—</p>"}

<h2>相位划分与边界依据（启程 / 匀速 / 换向 / 离座 / 落座）</h2>
{_phases_table(r) if r.get("phases") else "<p>—（见证据缺口）</p>"}

<h2>推力指标</h2>
<table><tbody>{metric_rows}</tbody></table>

<h2>阈值检查</h2>
{_checks_table(r)}

<h2>判定依据明细</h2>
<ul>{basis}</ul>

<h2>异常区间（含卡涩来源嫌疑）</h2>
<ul>{issues}</ul>

<h2>证据缺口（存在时不得给出维修结论）</h2>
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
