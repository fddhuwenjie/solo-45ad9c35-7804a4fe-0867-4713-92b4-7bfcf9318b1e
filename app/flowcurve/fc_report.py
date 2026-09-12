"""单相液体流量曲线校核打印报告：HTML + 内联 SVG。

包含：
- 时序图（阀位/流量、阀前后压差/温度），稳态平台底色，平台测点编号；
- Cv-阀位曲线：铭牌基准、实测采用点与拟合曲线、被排除点（展示用 Cv）、残差带；
- 测点明细表：采用点、逐点换算参数（Q、ΔP、绝压、Ff、ΔPchoked）与排除缘由；
- 平台划分依据（含原始点引用）、检查项、嫌疑诊断、证据缺口、版本修订留痕。
"""

import html
import json

from ..calibration_report import render_calibration_section
from .liquid import FLOW_TO_M3H, characteristic_curve

VERDICT_NAMES = {
    "matches_nameplate": "与铭牌特性一致",
    "suspect": "存在偏差嫌疑",
    "no_conclusion": "证据不足，不得判定",
}
CHAR_NAMES = {"linear": "线性", "equal_percentage": "等百分比", "quick_open": "快开"}
DIRECTION_NAMES = {"upstream_to_downstream": "上游→下游",
                   "downstream_to_upstream": "下游→上游"}
EXCL_SHORT = {
    "property_missing": "物性缺项",
    "property_out_of_range": "物性温度不适用",
    "insufficient_differential_pressure": "压差不足",
    "plateau_drift": "平台漂移",
    "flow_overrange": "流量计超量程",
    "cavitation": "气蚀（阻塞流）",
    "flashing": "闪蒸（阻塞流）",
    "nonpositive_flow": "流量≤0",
    "pressure_physical": "压力物理不合理",
    "plateau_too_short": "平台过短",
    "manual_disabled": "人工停用",
}


def _frame(width, height, pad_l, pad_r, pad_t, pad_b):
    return (f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'xmlns="http://www.w3.org/2000/svg" font-family="sans-serif" font-size="11">')


def _polyline(xs, ys, x_of, y_of):
    return " ".join(f"{x_of(x):.1f},{y_of(y):.1f}" for x, y in zip(xs, ys)
                    if y is not None)


def _plateau_shading(r, x_of, pad_t, height, pad_b):
    out = []
    for p in r.get("plateaus", []):
        x0, x1 = x_of(p["t_start"]), x_of(p["t_end"])
        fill = "#f3f4f6" if p.get("disabled") else "#fef9c3"
        out.append(f'<rect x="{x0:.1f}" y="{pad_t}" width="{max(x1 - x0, 1):.1f}" '
                   f'height="{height - pad_t - pad_b}" fill="{fill}" opacity="0.6">'
                   f'<title>平台 #{p["index"]} {p["t_start"]}–{p["t_end"]}s '
                   f'中位 {p["position_median_pct"]}%</title></rect>')
    return "".join(out)


def _time_axes(t0, t1, width, height, pad_l, pad_r, pad_t, pad_b):
    def x_of(tv):
        return pad_l + (tv - t0) / max(t1 - t0, 1e-9) * (width - pad_l - pad_r)
    ticks = []
    for k in range(11):
        tv = t0 + (t1 - t0) * k / 10
        ticks.append(f'<text x="{x_of(tv):.1f}" y="{height - pad_b + 16}" '
                     f'text-anchor="middle" fill="#6b7280">{tv:.0f}s</text>')
    return x_of, "".join(ticks)


def _time_charts(r, width=920):
    """时序双图：阀位/流量（双纵轴）、压差/温度。"""
    c = r.get("curves")
    if not c:
        return "<p class='bad'>无对齐曲线（见证据缺口）。</p>"
    t = c["t"]
    t0, t1 = t[0], t[-1]
    pad_l, pad_r, pad_t, pad_b = 52, 58, 26, 32

    # ---- 图 1：阀位 + 流量 ----
    h1 = 230
    x_of, xticks = _time_axes(t0, t1, width, h1, pad_l, pad_r, pad_t, pad_b)

    def y_pos(v):
        return pad_t + (105.0 - v) / 110.0 * (h1 - pad_t - pad_b)

    fmax = max(c["flow_m3h"]) if c["flow_m3h"] else 1.0
    fs = r["meter"]["full_scale"]
    fs_m3h = float(FLOW_TO_M3H.get((r["meter"].get("unit") or "m3/h").strip().lower(),
                                   1.0)) * fs
    fscale = max(fmax, fs_m3h) * 1.12

    def y_flow(v):
        return pad_t + (fscale - v) / fscale * (h1 - pad_t - pad_b)

    p = [_frame(width, h1, pad_l, pad_r, pad_t, pad_b),
         _plateau_shading(r, x_of, pad_t, h1, pad_b), xticks]
    for v in range(0, 101, 25):
        y = y_pos(v)
        p.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
                 f'stroke="#e5e7eb"/>')
        p.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" '
                 f'fill="#059669">{v}%</text>')
    y = y_flow(fs)
    p.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
             f'stroke="#dc2626" stroke-dasharray="5,3">'
             f'<title>流量计满量程 {fs:g} {r["meter"]["unit"]}'
             f'（{fs_m3h:g} m³/h）</title></line>')
    p.append(f'<text x="{width - pad_r - 4}" y="{y - 4:.1f}" text-anchor="end" '
             f'fill="#dc2626">流量计满量程 {fs:g} {r["meter"]["unit"]}</text>')
    p.append(f'<polyline points="{_polyline(t, c["position_pct"], x_of, y_pos)}" '
             f'fill="none" stroke="#059669" stroke-width="1.8"/>')
    p.append(f'<polyline points="{_polyline(t, c["flow_m3h"], x_of, y_flow)}" '
             f'fill="none" stroke="#2563eb" stroke-width="1.6"/>')
    # 平台编号
    for q in r.get("plateaus", []):
        pm = q["position_median_pct"]
        p.append(f'<text x="{x_of((q["t_start"] + q["t_end"]) / 2):.1f}" '
                 f'y="{y_pos(pm) - 6:.1f}" text-anchor="middle" fill="#374151">'
                 f'#{q["index"]}</text>')
    p.append(f'<text x="{pad_l + 4}" y="{pad_t - 10}" fill="#059669">阀位 (%)</text>')
    p.append(f'<text x="{pad_l + 90}" y="{pad_t - 10}" fill="#2563eb">'
             f'流量 (m³/h)</text>')
    p.append("</svg>")

    # ---- 图 2：压差 + 温度 ----
    h2 = 190
    x_of2, xticks2 = _time_axes(t0, t1, width, h2, pad_l, pad_r, pad_t, pad_b)
    dp = c["differential_pressure_kpa"]
    dpmax = max(max(dp), r["thresholds"]["min_differential_kpa"]) * 1.15

    def y_dp(v):
        return pad_t + (dpmax - v) / dpmax * (h2 - pad_t - pad_b)

    temps = c["temperature_c"]
    tlo, thi = min(temps), max(temps)
    tspan = max(thi - tlo, 1.0)
    tlo, thi = tlo - 0.15 * tspan, thi + 0.15 * tspan

    def y_t(v):
        return pad_t + (thi - v) / (thi - tlo) * (h2 - pad_t - pad_b)

    p2 = [_frame(width, h2, pad_l, pad_r, pad_t, pad_b),
          _plateau_shading(r, x_of2, pad_t, h2, pad_b), xticks2]
    y = y_dp(r["thresholds"]["min_differential_kpa"])
    p2.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
              f'stroke="#dc2626" stroke-dasharray="5,3"/>')
    p2.append(f'<text x="{width - pad_r - 4}" y="{y - 4:.1f}" text-anchor="end" '
              f'fill="#dc2626">最小有效压差 '
              f'{r["thresholds"]["min_differential_kpa"]:g} kPa</text>')
    p2.append(f'<polyline points="{_polyline(t, dp, x_of2, y_dp)}" fill="none" '
              f'stroke="#d97706" stroke-width="1.7"/>')
    p2.append(f'<polyline points="{_polyline(t, temps, x_of2, y_t)}" fill="none" '
              f'stroke="#7c3aed" stroke-width="1.4" stroke-dasharray="3,2"/>')
    p2.append(f'<text x="{pad_l + 4}" y="{pad_t - 10}" fill="#92400e">'
              f'阀前后压差 (kPa)</text>')
    p2.append(f'<text x="{pad_l + 150}" y="{pad_t - 10}" fill="#6d28d9">'
              f'温度 (°C，虚线)</text>')
    p2.append("</svg>")
    return "".join(p) + "".join(p2)


def _cv_chart(r, width=920, height=420, pad_l=60, pad_r=20, pad_t=24, pad_b=44):
    """Cv-阀位：铭牌基准、拟合曲线、采用点、排除点（展示 Cv）。"""
    curve = r.get("curve")
    pts = r.get("points", [])
    rated = r["valve"]["rated_cv"]
    char = r["characteristic"]
    rr = r["valve"].get("equal_percentage_r", 50.0)
    xs = [p / 100.0 * 100 for p in range(0, 101)]
    nameplate = characteristic_curve(char, xs, rated, rr)
    ymax = max([rated] + nameplate
               + [p["cv"] for p in pts if p.get("cv")]
               + [p["cv_display"] for p in pts if p.get("cv_display")]
               + [1.0]) * 1.12

    def x_of(v):
        return pad_l + v / 100.0 * (width - pad_l - pad_r)

    def y_of(v):
        return pad_t + (ymax - v) / ymax * (height - pad_t - pad_b)

    s = [_frame(width, height, pad_l, pad_r, pad_t, pad_b)]
    for k in range(11):
        xv = k * 10
        s.append(f'<line x1="{x_of(xv):.1f}" y1="{pad_t}" x2="{x_of(xv):.1f}" '
                 f'y2="{height - pad_b}" stroke="#f3f4f6"/>')
        s.append(f'<text x="{x_of(xv):.1f}" y="{height - pad_b + 18}" '
                 f'text-anchor="middle" fill="#6b7280">{xv}</text>')
    for k in range(6):
        yv = ymax * k / 5
        y = y_of(yv)
        s.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
                 f'stroke="#e5e7eb"/>')
        s.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" '
                 f'fill="#6b7280">{yv:.0f}</text>')
    s.append(f'<text x="{(width) // 2}" y="{height - 6}" text-anchor="middle" '
             f'fill="#374151">阀位 (%)</text>')
    s.append(f'<text x="14" y="{height // 2}" text-anchor="middle" fill="#374151" '
             f'transform="rotate(-90 14 {height // 2})">Cv</text>')

    # 铭牌基准
    s.append(f'<polyline points="{_polyline(xs, nameplate, x_of, y_of)}" fill="none" '
             f'stroke="#9ca3af" stroke-width="2" stroke-dasharray="7,4"/>')
    # 拟合曲线（采用点区间）
    adopted = sorted((p for p in pts if p.get("adopted")),
                     key=lambda p: p["position_pct"])
    if curve and curve.get("capacity_factor") is not None and adopted:
        k = curve["capacity_factor"]
        x0 = max(0.0, adopted[0]["position_pct"] - 2)
        x1 = min(100.0, adopted[-1]["position_pct"] + 2)
        fx = [x0 + (x1 - x0) * i / 80 for i in range(81)]
        fy = [k * rated * f_ for f_ in
              characteristic_curve(char, fx, 1.0, rr)]
        s.append(f'<polyline points="{_polyline(fx, fy, x_of, y_of)}" fill="none" '
                 f'stroke="#2563eb" stroke-width="2"/>')
    # 采用点
    for p in adopted:
        s.append(f'<circle cx="{x_of(p["position_pct"]):.1f}" '
                 f'cy="{y_of(p["cv"]):.1f}" r="4.5" fill="#059669" stroke="white">'
                 f'<title>平台 #{p["plateau_index"]}：阀位 {p["position_pct"]}%，'
                 f'Cv={p["cv"]}</title></circle>')
    # 排除点（展示 Cv）
    for p in pts:
        if p.get("adopted") or not p.get("cv_display"):
            continue
        x, y = x_of(p["position_pct"]), y_of(p["cv_display"])
        s.append(f'<path d="M{x - 4:.1f},{y - 4:.1f} l8,8 m-8,0 l8,-8" '
                 f'stroke="#dc2626" stroke-width="1.6">'
                 f'<title>平台 #{p["plateau_index"]}（已排除：'
                 f'{"，".join(EXCL_SHORT.get(c, c) for c in p["exclusion_codes"])}）'
                 f' 展示 Cv={p["cv_display"]}</title></path>')
    ly = pad_t - 8
    s.append(f'<line x1="{pad_l + 8}" y1="{ly}" x2="{pad_l + 30}" y2="{ly}" '
             f'stroke="#9ca3af" stroke-width="2" stroke-dasharray="7,4"/>'
             f'<text x="{pad_l + 34}" y="{ly + 4}" fill="#374151">'
             f'铭牌 {CHAR_NAMES.get(char, char)}（额定 Cv={rated:g}）</text>')
    s.append(f'<line x1="{pad_l + 250}" y1="{ly}" x2="{pad_l + 272}" y2="{ly}" '
             f'stroke="#2563eb" stroke-width="2"/>'
             f'<text x="{pad_l + 276}" y="{ly + 4}" fill="#374151">实测拟合</text>')
    s.append(f'<circle cx="{pad_l + 360}" cy="{ly}" r="4.5" fill="#059669"/>'
             f'<text x="{pad_l + 370}" y="{ly + 4}" fill="#374151">采用点</text>')
    s.append(f'<path d="M{pad_l + 430},{ly - 4} l8,8 m-8,0 l8,-8" '
             f'stroke="#dc2626" stroke-width="1.6"/>'
             f'<text x="{pad_l + 444}" y="{ly + 4}" fill="#374151">'
             f'排除点（展示 Cv，不参与拟合）</text>')
    s.append("</svg>")
    return "".join(s)


def _points_table(r):
    rows = []
    for p in r.get("points", []):
        conv = p.get("conversion") or {}
        refs_start = "，".join(
            f"{x['channel']}#{x['index']}@{x['t']}s" for x in p["raw_refs"]["start"])
        refs_end = "，".join(
            f"{x['channel']}#{x['index']}@{x['t']}s" for x in p["raw_refs"]["end"])
        if p.get("adopted"):
            status = '<span class="ok">采用</span>'
            cv_cell = p["cv"]
            reasons = "—"
        else:
            status = '<span class="bad">排除</span>'
            cv_cell = f"{p['cv'] if p['cv'] is not None else '—'}"
            if p.get("cv_display") is not None:
                cv_cell += f' <span class="reason">（展示 {p["cv_display"]}）</span>'
            reasons = "<br/>".join(
                f'<span class="bad">{html.escape(EXCL_SHORT.get(c, c))}</span>：'
                f'{html.escape(txt)}'
                for c, txt in zip(p["exclusion_codes"], p["exclusion_reasons"]))
        choked = {True: "是", False: "否", None: "无法判别"}.get(p.get("choked"))
        rows.append(
            f"<tr><td>#{p['plateau_index']}</td>"
            f"<td>{p['t_start_s']}–{p['t_end_s']}</td>"
            f"<td>{p['position_pct']}</td>"
            f"<td>{p['flow_m3h']:g}</td>"
            f"<td>{conv.get('p1_abs_kpa')}</td><td>{conv.get('p2_abs_kpa')}</td>"
            f"<td>{conv.get('dp_kpa')}</td><td>{p['temp_c']}</td>"
            f"<td>{conv.get('ff') if conv.get('ff') is not None else '—'}</td>"
            f"<td>{conv.get('dp_choked_kpa') if conv.get('dp_choked_kpa') is not None else '—'}</td>"
            f"<td>{choked}</td><td>{cv_cell}</td><td>{status}</td>"
            f'<td class="reason">{reasons}</td>'
            f'<td class="reason">{html.escape(refs_start)}<br/>{html.escape(refs_end)}</td>'
            "</tr>")
    return (
        '<table><thead><tr><th>平台</th><th>时间 (s)</th><th>中位阀位 %</th>'
        '<th>Q (m³/h)</th><th>P1绝 (kPa)</th><th>P2绝 (kPa)</th><th>ΔP (kPa)</th>'
        '<th>T (°C)</th><th>Ff</th><th>ΔPchoked (kPa)</th><th>阻塞</th>'
        '<th>Cv</th><th>状态</th><th>排除缘由</th><th>原始点引用</th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def _plateaus_table(r):
    rows = []
    refs_by_index = {p["plateau_index"]: p["raw_refs"] for p in r.get("points", [])}
    for q in r.get("plateaus", []):
        refs = refs_by_index.get(q["index"], {"start": [], "end": []})
        ref_txt = "起点：" + "，".join(
            f"{x['channel']}#{x['index']}@{x['t']}s" for x in refs["start"]) \
            + "<br/>终点：" + "，".join(
            f"{x['channel']}#{x['index']}@{x['t']}s" for x in refs["end"])
        state = '<span class="bad">已停用</span>' if q.get("disabled") else "有效"
        rows.append(
            f"<tr><td>#{q['index']}</td><td>{q['t_start']}–{q['t_end']}</td>"
            f"<td>{q['position_median_pct']}</td><td>{q['order']}</td><td>{state}</td>"
            f'<td class="reason">起点：{html.escape(q["start_reason"])}<br/>'
            f'终点：{html.escape(q["end_reason"])}</td>'
            f'<td class="reason">{ref_txt}</td></tr>')
    return ("<table><thead><tr><th>平台</th><th>时间 (s)</th><th>中位阀位 %</th>"
            "<th>行程方向</th><th>状态</th><th>边界依据</th><th>原始点引用</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def _checks_table(r):
    out = ["<table><thead><tr><th>检查项</th><th>实测</th><th>允许带</th>"
           "<th>依据</th><th>判定</th></tr></thead><tbody>"]
    for c in r.get("checks", []):
        band = c.get("threshold_band")
        limit = "—" if band is None else str(band)
        ok = c.get("pass")
        val = c.get("value")
        out.append(f"<tr><td>{html.escape(c['metric'])}</td><td>{val}</td>"
                   f"<td>{limit}</td><td class='reason'>{html.escape(c.get('basis', ''))}</td>"
                   f"<td class='{'ok' if ok else 'bad'}'>{'通过' if ok else '不通过'}</td></tr>")
    out.append("</tbody></table>")
    return "".join(out)


def render_fc_report(analysis, test, valve):
    r = analysis["result"]
    esc = html.escape
    verdict = r["verdict"]
    curve = r.get("curve") or {}
    v = r["valve"]
    f = r["fluid"]

    gaps = "".join(f"<li>[{esc(g['code'])}] {esc(g['detail'])}</li>"
                   for g in r.get("evidence_gaps", [])) or "<li>无</li>"
    suspects = "".join(
        f"<li>[{esc(s['kind'])}] {esc(s['detail'])}</li>"
        for s in r.get("suspects", [])) or "<li>无</li>"
    basis = "".join(f"<li>{esc(b)}</li>" for b in r.get("decision_basis", [])) \
        or "<li>—</li>"

    def adj_text(a):
        if a["type"] == "plateau_move":
            return (f"移动平台 #{a['plateau_index']} {a['boundary']} 边界 → "
                    f"{a['new_time']}s（{esc(a.get('reason', ''))}）")
        if a["type"] == "calibration_rebind":
            return ("改绑逐通道校准版本 "
                    + json.dumps(a.get("bindings", {}), ensure_ascii=False)
                    + "（派生新版本，旧分析冻结版本不变）")
        return f"停用平台测点 #{a['plateau_index']}（{esc(a.get('reason', ''))}）"

    adjustments = "".join(
        f"<li>v{analysis['version']} / {esc(a.get('author', ''))}：{adj_text(a)}</li>"
        for a in analysis["adjustments"]) or "<li>无（自动分析）</li>"
    exclusions = "".join(
        f"<li>{esc(e['channel'])} 原始点 #{e['start_index']}–#{e['end_index']}，"
        f"共 {len(e['original_points'])} 点，理由：{esc(e['reason'])}</li>"
        for e in r.get("exclusions", [])) or "<li>无</li>"

    density = f.get("density_kg_m3")
    pv = f.get("vapor_pressure_kpa")
    metric_rows = "".join(f"<tr><td>{n}</td><td>{val}</td></tr>" for n, val in [
        ("设计特性", f"{CHAR_NAMES.get(r['characteristic'], r['characteristic'])}"
                    f"（额定 Cv={v['rated_cv']:g}，口径 DN{v['size_dn_mm']:g}，"
                    f"阀内件 {esc(str(v.get('trim')))}，FL={v.get('liquid_recovery_factor_fl')}）"),
        ("介质", f"{esc(str(f.get('name')))}，密度 "
                 f"{density if density is not None else '缺项'} kg/m³，"
                 f"饱和蒸气压 {pv if pv is not None else '缺项'} kPa"),
        ("流量计量程", f"{r['meter']['full_scale']:g} {r['meter']['unit']}"),
        ("拟合容量系数 k", curve.get("capacity_factor")),
        ("拟合实测额定 Cv", curve.get("fitted_rated_cv")),
        ("拟合 RMSE (Cv / %)", f"{curve.get('rmse_cv')} / {curve.get('rmse_pct')}%"),
        ("残差带 (%)", curve.get("residual_band_pct")),
        ("Spearman 秩相关 / 逆序数",
         f"{curve.get('spearman_rho')} / {curve.get('monotonic_inversions')}"),
        ("有效调节比", (f"{curve.get('effective_turndown')}:1"
                      f"（阀位 {curve.get('turndown_position_range_pct')}%）"
                      if curve.get("effective_turndown") is not None else "—")),
        ("镜像行程容量系数 / RMSE",
         f"{curve.get('capacity_factor_mirrored')} / {curve.get('rmse_cv_mirrored')}"),
    ])

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"/>
<title>单相液体流量曲线校核 {esc(valve['tag'])} 测次#{test['id']} v{analysis['version']}</title>
<style>
 body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; margin: 24px; color: #111827; }}
 h1 {{ font-size: 20px; }} h2 {{ font-size: 15px; margin-top: 20px; border-bottom: 1px solid #d1d5db; padding-bottom: 4px; }}
 table {{ border-collapse: collapse; font-size: 11px; }}
 th, td {{ border: 1px solid #d1d5db; padding: 4px 6px; text-align: left; }}
 th {{ background: #f3f4f6; }}
 .ok {{ color: #059669; font-weight: 600; }} .bad {{ color: #dc2626; font-weight: 600; }}
 .warn {{ color: #d97706; }} .reason {{ font-size: 10px; color: #4b5563; }}
 .verdict {{ display: inline-block; padding: 2px 10px; border-radius: 4px; font-weight: 700;
   background: {'#d1fae5' if verdict == 'matches_nameplate' else ('#fee2e2' if verdict == 'suspect' else '#fef3c7')}; }}
 .meta td {{ border: none; padding: 2px 12px 2px 0; }}
 ul {{ font-size: 12px; margin: 6px 0; }}
 @media print {{ body {{ margin: 8mm; }} h2 {{ page-break-after: avoid; }} svg {{ max-width: 100%; }} table {{ font-size: 9px; }} }}
</style></head><body>
<h1>单相液体流量曲线校核报告（IEC 60534-2-1）</h1>
<table class="meta"><tr>
<td><b>阀门位号</b>：{esc(valve['tag'])}</td>
<td><b>设计特性</b>：{CHAR_NAMES.get(r['characteristic'], r['characteristic'])}</td>
<td><b>流向</b>：{DIRECTION_NAMES.get(r['flow_direction'], r['flow_direction'])}</td>
<td><b>测次编号</b>：{test['id']}（{esc(test['phase'])}）</td>
<td><b>分析版本</b>：v{analysis['version']}（{esc(analysis['author'])}，{esc(analysis['created_at'])}）</td>
<td><b>结论</b>：<span class="verdict">{VERDICT_NAMES[verdict]}</span></td></tr></table>

<h2>阀位/流量时序与稳态平台（黄色=平台，灰色=人工停用，#为测点编号）</h2>
{_time_charts(r)}

<h2>Cv-阀位实测曲线对铭牌基准（绿点=采用点，红叉=排除点仅展示）</h2>
{_cv_chart(r) if r.get("points") else "<p class='bad'>无测点（见证据缺口）。</p>"}

<h2>平台划分与边界依据（含原始点引用）</h2>
{_plateaus_table(r) if r.get("plateaus") else "<p class='bad'>未圈定稳态平台。</p>"}

<h2>测点明细：采用状态、逐点换算参数与排除缘由</h2>
{_points_table(r) if r.get("points") else "<p>—</p>"}

<h2>校核指标</h2>
<table><tbody>{metric_rows}</tbody></table>
<p class="reason">换算：Cv = Q /（N1·√(ΔP/(ρ/ρ0))），N1=0.865（Q:m³/h，ΔP:bar，
ρ0=999 kg/m³）；阻塞流判据 ΔPchoked = FL²·(P1 − Ff·Pv)，
Ff = 0.96 − 0.28·√(Pv/Pc)，压力均按表压加大气压 {r.get('atmospheric_pressure_kpa')} kPa
换算绝压。排除点不进入容量系数拟合。</p>

<h2>检查项</h2>
{_checks_table(r)}

<h2>判定依据明细</h2>
<ul>{basis}</ul>

<h2>嫌疑诊断（堵塞 / 冲蚀 / 反装行程）</h2>
<ul>{suspects}</ul>

{render_calibration_section(r)}

<h2>证据缺口（存在时不得给出结论）</h2>
<ul>{gaps}</ul>

<h2>版本与人工修订记录</h2>
<ul>{adjustments}</ul>

<h2>通道坏点屏蔽明细（保留原始引用）</h2>
<ul>{exclusions}</ul>

<p class="reason">单位处理：{esc('；'.join(r.get('unit_notes', [])) or '均为原生单位')}。
对齐网格：{r['alignment']['grid_step_s'] if r.get('alignment') else '—'}s ×
{r['alignment']['n_points'] if r.get('alignment') else '—'} 点。
报告由分析版本 v{analysis['version']} 生成，数据可追溯至测次 #{test['id']} 原始采样。</p>
</body></html>"""
