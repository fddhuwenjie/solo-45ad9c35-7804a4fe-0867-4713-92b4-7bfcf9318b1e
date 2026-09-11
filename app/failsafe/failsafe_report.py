"""故障安全动作测试打印报告：HTML + 内联 SVG。

记录阈值、采用区间、判定依据、跳闸事件（抖动/重复触发/指令先行）、
证据缺口与版本调整；另提供退化趋势报告。
"""

import html

VERDICT_NAMES = {"pass": "通过", "fail": "未达安全要求", "no_conclusion": "证据不足，不得判定"}
MODE_NAMES = {"fail_open": "失气打开 (fail-open)", "fail_close": "失气关闭 (fail-close)",
              "fail_in_place": "原位保持 (fail-in-place)"}
TREND_NAMES = {"degrading": "退化", "improving": "改善", "stable": "稳定",
               "inconclusive": "数据不足", "mixed": "有升有降"}


def _polyline(xs, ys, x_of, y_of):
    return " ".join(f"{x_of(x):.1f},{y_of(y):.1f}" for x, y in zip(xs, ys))


def _fs_chart(r, width=920, height=320, pad_l=50, pad_r=16, pad_t=16, pad_b=34):
    c = r["curves"]
    t, cmd, pos, trip = c["t"], c["command"], c["position"], c["trip"]
    t0, t1 = t[0], t[-1]
    v0, v1 = -5.0, 105.0

    def x_of(tv):
        return pad_l + (tv - t0) / (t1 - t0) * (width - pad_l - pad_r)

    def y_of(v):
        return pad_t + (v1 - v) / (v1 - v0) * (height - pad_t - pad_b)

    p = [f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
         f'xmlns="http://www.w3.org/2000/svg" font-family="sans-serif" font-size="11">']
    # 跳闸高电平底色
    in_trip, seg_start = False, None
    for i in range(len(t) + 1):
        active = i < len(t) and (trip[i] or 0) >= 1
        if active and not in_trip:
            seg_start, in_trip = i, True
        elif not active and in_trip:
            x0, x1 = x_of(t[seg_start]), x_of(t[min(i, len(t) - 1)])
            p.append(f'<rect x="{x0:.1f}" y="{pad_t}" width="{max(x1 - x0, 1):.1f}" '
                     f'height="{height - pad_t - pad_b}" fill="#fee2e2" opacity="0.55"/>')
            in_trip = False
    # 跳闸沿竖线
    tt = (r.get("trip") or {}).get("trip_time_s")
    if tt is not None:
        x = x_of(tt)
        p.append(f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{height - pad_b}" '
                 f'stroke="#dc2626" stroke-width="1.4" stroke-dasharray="6,3"/>')
        p.append(f'<text x="{x + 4:.1f}" y="{pad_t + 12}" fill="#dc2626">有效跳闸 {tt}s</text>')
    # 观察窗终点
    aw = r.get("adopted_window")
    if aw and aw.get("end_s") is not None:
        x = x_of(aw["end_s"])
        p.append(f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{height - pad_b}" '
                 f'stroke="#7c3aed" stroke-width="1.2" stroke-dasharray="3,3"/>')
        p.append(f'<text x="{x - 4:.1f}" y="{pad_t + 24}" text-anchor="end" '
                 f'fill="#7c3aed">观察窗末 {aw["end_s"]}s</text>')
    # 安全位带
    if r["fail_mode"] == "fail_close":
        band_lo, band_hi = 0.0, r["thresholds"]["settle_band_pct"]
    elif r["fail_mode"] == "fail_open":
        band_lo, band_hi = 100.0 - r["thresholds"]["settle_band_pct"], 100.0
    else:
        band_lo = band_hi = None
    if band_lo is not None:
        y0, y1v = y_of(band_hi), y_of(band_lo)
        p.append(f'<rect x="{pad_l}" y="{y0:.1f}" width="{width - pad_l - pad_r}" '
                 f'height="{y1v - y0:.1f}" fill="#d1fae5" opacity="0.7"/>')
    for v in range(0, 101, 25):
        y = y_of(v)
        p.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
                 f'stroke="#e5e7eb"/>')
        p.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" fill="#6b7280">{v}%</text>')
    n_ticks = 10
    for k in range(n_ticks + 1):
        tv = t0 + (t1 - t0) * k / n_ticks
        p.append(f'<text x="{x_of(tv):.1f}" y="{height - pad_b + 16}" text-anchor="middle" '
                 f'fill="#6b7280">{tv:.1f}s</text>')
    p.append(f'<polyline points="{_polyline(t, cmd, x_of, y_of)}" fill="none" '
             f'stroke="#2563eb" stroke-width="1.7"/>')
    p.append(f'<polyline points="{_polyline(t, pos, x_of, y_of)}" fill="none" '
             f'stroke="#059669" stroke-width="1.9"/>')
    # 停滞/反弹标注
    for st in r["metrics"].get("mid_stalls", []):
        x0, x1 = x_of(st["t_start"]), x_of(st["t_end"])
        p.append(f'<rect x="{x0:.1f}" y="{pad_t}" width="{max(x1 - x0, 2):.1f}" height="8" '
                 f'fill="#d97706"><title>中途停滞 {st["duration_s"]}s</title></rect>')
    ly = height - 10
    p.append(f'<line x1="{pad_l + 8}" y1="{ly}" x2="{pad_l + 30}" y2="{ly}" stroke="#2563eb" '
             f'stroke-width="2"/><text x="{pad_l + 34}" y="{ly + 4}" fill="#374151">指令</text>')
    p.append(f'<line x1="{pad_l + 76}" y1="{ly}" x2="{pad_l + 98}" y2="{ly}" stroke="#059669" '
             f'stroke-width="2"/><text x="{pad_l + 102}" y="{ly + 4}" fill="#374151">阀位</text>')
    p.append(f'<line x1="{pad_l + 146}" y1="{ly}" x2="{pad_l + 168}" y2="{ly}" stroke="#dc2626" '
             f'stroke-dasharray="6,3"/><text x="{pad_l + 172}" y="{ly + 4}" fill="#374151">跳闸</text>')
    p.append("</svg>")
    return "".join(p)


def _pressure_chart(r, width=920, height=130, pad_l=50, pad_r=16, pad_t=10, pad_b=28):
    c = r["curves"]
    t, prs = c["t"], c["pressure"]
    t0, t1 = t[0], t[-1]
    p_max = max(max(prs) * 1.1, 1.0)
    rlim = r["thresholds"]["pressure_residual_kpa_max"]

    def x_of(tv):
        return pad_l + (tv - t0) / (t1 - t0) * (width - pad_l - pad_r)

    def y_of(v):
        return pad_t + (p_max - v) / p_max * (height - pad_t - pad_b)

    p = [f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
         f'xmlns="http://www.w3.org/2000/svg" font-family="sans-serif" font-size="11">',
         f'<polyline points="{_polyline(t, prs, x_of, y_of)}" fill="none" stroke="#d97706" '
         f'stroke-width="1.6"/>',
         f'<line x1="{pad_l}" y1="{y_of(rlim):.1f}" x2="{width - pad_r}" y2="{y_of(rlim):.1f}" '
         f'stroke="#dc2626" stroke-dasharray="5,3"/>',
         f'<text x="{width - pad_r - 4}" y="{y_of(rlim) - 4:.1f}" text-anchor="end" '
         f'fill="#dc2626">残压上限 {rlim:.0f} kPa</text>',
         f'<text x="{pad_l + 4}" y="{pad_t + 12}" fill="#92400e">执行器压力 (kPa)</text>',
         "</svg>"]
    return "".join(p)


def _checks_table(r):
    out = ["<table><thead><tr><th>检查项</th><th>实测</th><th>阈值</th><th>采用区间/依据</th>"
           "<th>判定</th></tr></thead><tbody>"]
    intervals = {a["name"]: a for a in r.get("adopted_intervals", [])}
    interval_for = {"response_delay_s": "response_delay", "t90_s": "t90",
                    "final_position_pct": "final_position_settle",
                    "fip_drift_pct": "fip_drift"}
    for c in r.get("checks", []):
        val = "—" if c.get("value") is None else c["value"]
        if "threshold_max" in c:
            limit = f"≤ {c['threshold_max']}"
        elif "threshold_min" in c:
            limit = f"≥ {c['threshold_min']}"
        else:
            limit = "—"
        iv = intervals.get(interval_for.get(c["metric"], ""))
        if iv and iv.get("interval_s"):
            basis = f"{iv['interval_s'][0]}–{iv['interval_s'][1]}s；{html.escape(iv.get('method', ''))}"
        else:
            basis = html.escape(c.get("basis", ""))
        ok = c.get("pass")
        cls = "ok" if ok else "bad"
        out.append(f'<tr><td>{html.escape(c["metric"])}</td><td>{val}</td><td>{limit}</td>'
                   f'<td class="reason">{basis}</td><td class="{cls}">{"通过" if ok else "不通过"}</td></tr>')
    out.append("</tbody></table>")
    return "".join(out)


def _metrics_rows(r):
    m = r["metrics"]
    rows = [
        ("跳闸前位置 (%)", m.get("pre_trip_position_pct")),
        ("响应延迟 (s)", m.get("response_delay_s")),
        ("90% 行程时间 (s)", m.get("t90_s")),
        ("最终位置 (%)", m.get("final_position_pct")),
        ("稳定时间（自跳闸，s）", m.get("settle_time_s")),
        ("窗末连续在带 (s)", m.get("stable_tail_s")),
        ("最大反弹 (%)", m.get("max_rebound_pct")),
        ("中途停滞次数", m.get("mid_stall_count")),
        ("跳闸前压力 (kPa)", m.get("pressure_pre_kpa")),
        ("窗末残压 (kPa)", m.get("pressure_end_kpa")),
        ("压力衰减比例 (%)", m.get("pressure_decay_pct")),
        ("低于残压阈值用时 (s)", m.get("pressure_below_residual_s")),
    ]
    if r["fail_mode"] == "fail_in_place":
        rows.insert(3, ("最大漂移 (%)", m.get("max_drift_pct")))
    return "".join(f"<tr><td>{n}</td><td>{v if v is not None else '—'}</td></tr>" for n, v in rows)


def render_fs_report(analysis, test, valve):
    r = analysis["result"]
    esc = html.escape
    trip = r.get("trip") or {}

    def trip_events():
        lines = []
        if trip.get("trip_time_s") is not None:
            src = "人工指定" if trip.get("source") == "manual" else "自动定位"
            verified = "已由接点沿佐证" if trip.get("verified") else "<b class='bad'>无接点沿佐证</b>"
            lines.append(f"<li>有效跳闸时刻：<b>{trip['trip_time_s']}s</b>（{src}，{verified}）</li>")
        for ch in trip.get("chatter", []):
            lines.append(f"<li class='warn'>接点抖动：{ch['t_start']}–{ch['t_end']}s，"
                         f"{ch['pulse_count']} 个脉冲；{esc(ch['detail'])}</li>")
        for rt in trip.get("retriggers", []):
            lines.append(f"<li class='warn'>重复触发：{rt['t_start']}–{rt['t_end']}s；"
                         f"{esc(rt['detail'])}</li>")
        if trip.get("command_loss_time_s") is not None:
            lines.append(f"<li>控制指令先行：{trip['command_loss_time_s']}s 偏离基线 "
                         f"{trip.get('command_baseline_pct')}%，早于跳闸 "
                         f"{trip.get('command_loss_lead_s')}s</li>")
        return "".join(lines) or "<li>未观察到跳闸动作</li>"

    gaps = "".join(
        f"<li>[{esc(g['code'])}] {esc(g['detail'])}</li>" for g in r.get("evidence_gaps", [])
    ) or "<li>无</li>"
    issues = "".join(
        f"<li>[{esc(i['kind'])}] {esc(i['detail'])}"
        + (f"（时段：{', '.join(f'{a}–{b}s' for a, b in i.get('periods', []))}）"
           if i.get("periods") else "") + "</li>"
        for i in r.get("issues", [])) or "<li>无</li>"
    adjustments = "".join(
        f"<li>v{analysis['version']} / {esc(a.get('author', ''))}："
        + (f"人工跳闸点 → {a['new_trip_time']}s" if a["type"] == "trip_move"
           else f"观察窗缩短/延长至跳闸后 {a['window_end_s']}s")
        + f"（{esc(a.get('reason', ''))}）</li>"
        for a in analysis["adjustments"]) or "<li>无（自动分析）</li>"
    adopted = "".join(
        f"<li><b>{esc(a['name'])}</b>：{a['interval_s'][0]}–{a['interval_s'][1]}s"
        f"（{esc(a.get('method', ''))}）</li>"
        for a in r.get("adopted_intervals", []) if a.get("interval_s")) or "<li>—</li>"
    basis = "".join(f"<li>{esc(b)}</li>" for b in r.get("decision_basis", [])) or "<li>—</li>"
    refs = "".join(
        f"<li>跳闸沿原始采样："
        + "，".join(f"#{x['index']}@{x['t']}s={x['value']}" for x in trip.get("edge_references", []))
        + "</li>")
    aw = r.get("adopted_window") or {}
    verdict = r["verdict"]

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"/>
<title>故障安全动作报告 {esc(valve['tag'])} FS测试#{test['id']} v{analysis['version']}</title>
<style>
 body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; margin: 24px; color: #111827; }}
 h1 {{ font-size: 20px; }} h2 {{ font-size: 15px; margin-top: 20px; border-bottom: 1px solid #d1d5db; padding-bottom: 4px; }}
 table {{ border-collapse: collapse; font-size: 12px; }}
 th, td {{ border: 1px solid #d1d5db; padding: 4px 8px; text-align: left; }}
 th {{ background: #f3f4f6; }}
 .ok {{ color: #059669; font-weight: 600; }} .bad {{ color: #dc2626; font-weight: 600; }}
 .warn {{ color: #d97706; }} .reason {{ font-size: 11px; color: #4b5563; }}
 .verdict {{ display: inline-block; padding: 2px 10px; border-radius: 4px; font-weight: 700;
   background: {'#d1fae5' if verdict == 'pass' else ('#fee2e2' if verdict != 'pass' else '#fef3c7')}; }}
 .meta td {{ border: none; padding: 2px 12px 2px 0; }}
 ul {{ font-size: 12px; margin: 6px 0; }}
 @media print {{ body {{ margin: 8mm; }} h2 {{ page-break-after: avoid; }} svg {{ max-width: 100%; }} }}
</style></head><body>
<h1>调节阀故障安全动作（SIS 跳闸）测试报告</h1>
<table class="meta"><tr>
<td><b>阀门位号</b>：{esc(valve['tag'])}</td>
<td><b>故障安全配置</b>：{MODE_NAMES.get(r['fail_mode'], r['fail_mode'])}</td>
<td><b>执行器</b>：{esc(r.get('actuator_type', ''))}</td>
<td><b>FS 测试编号</b>：{test['id']}（{esc(test['phase'])}）</td>
<td><b>分析版本</b>：v{analysis['version']}（{esc(analysis['author'])}，{esc(analysis['created_at'])}）</td>
<td><b>结论</b>：<span class="verdict">{VERDICT_NAMES[verdict]}</span></td></tr></table>

<h2>动作曲线（跳闸接点 / 控制指令 / 阀位，含采用区间与安全位带）</h2>
{_fs_chart(r) if r.get("curves") else "<p class='bad'>无对齐曲线（见证据缺口）。</p>"}
<h2>执行器压力与残压上限</h2>
{_pressure_chart(r) if r.get("curves") else "<p>—</p>"}

<h2>跳闸定位：有效沿 / 接点抖动 / 重复触发 / 指令先行</h2>
<ul>{trip_events()}</ul>

<h2>阈值检查与判定依据</h2>
{_checks_table(r)}

<h2>动作指标与采用区间</h2>
<table><thead><tr><th>指标</th><th>数值</th></tr></thead><tbody>{_metrics_rows(r)}</tbody></table>
<p class="reason">采用观察窗：{aw.get('start_s')}–{aw.get('end_s')}s（长度 {aw.get('length_s')}s，
{'人工调整：' + esc(aw.get('reason', '')) if aw.get('manual') else '默认/提交值'}）。</p>
<ul>{adopted}</ul>

<h2>判定依据明细</h2>
<ul>{basis}</ul>

<h2>超限与动作异常</h2>
<ul>{issues}</ul>

<h2>证据缺口（存在时不得给出通过结论）</h2>
<ul>{gaps}</ul>

<h2>版本与人工调整记录（派生版本保留理由与原始引用）</h2>
<ul>{adjustments}</ul>
{refs}

<p class="reason">单位处理：{esc('；'.join(r.get('unit_notes', [])) or '均为原生单位')}。
对齐网格：{r['alignment']['grid_step_s'] if r.get('alignment') else '—'}s ×
{r['alignment']['n_points'] if r.get('alignment') else '—'} 点，
区间 [{r['alignment']['t0'] if r.get('alignment') else '—'},
{r['alignment']['t1'] if r.get('alignment') else '—'}]s。
报告由分析版本 v{analysis['version']} 生成，数据可追溯至 FS 测试 #{test['id']} 原始采样。</p>
</body></html>"""


def render_fs_trend_report(trend_id, data, valve):
    esc = html.escape
    overall = data.get("overall", "inconclusive")
    rows = []
    for e in data.get("series", []):
        cls = "ok" if e["verdict"] == "pass" else ("bad" if e["verdict"] == "fail" else "warn")
        rows.append(
            f"<tr><td>{e['test_id']}</td><td>{esc(e.get('test_started_at') or '')}</td>"
            f"<td>{esc(e['phase'])}</td><td class='{cls}'>{VERDICT_NAMES[e['verdict']]}</td>"
            f"<td>{'参与趋势' if e['comparable'] else '不参与'}</td>"
            f"<td class='reason'>{esc('；'.join(e.get('reasons', [])))}</td></tr>")
    trend_rows = []
    for t in data.get("trends", []):
        pts = " → ".join(f"{p['value']}" for p in t["points"]) or "—"
        cls = {"degrading": "bad", "improving": "ok"}.get(t["status"], "")
        within = ""
        if "latest_within_threshold" in t:
            within = ("末点在阈值内" if t["latest_within_threshold"] else "末点超出阈值")
        trend_rows.append(
            f"<tr><td>{esc(t['name'])} ({esc(t['unit'])})</td><td>{pts}</td>"
            f"<td class='{cls}'>{TREND_NAMES[t['status']]}</td>"
            f"<td>{t.get('delta', '—')}</td><td>{t.get('threshold', '—')}</td><td>{within}</td></tr>")
    incomparable = "".join(
        f"<li>FS 测试 #{x['test_id']}：{esc('；'.join(x['reasons']))}</li>"
        for x in data.get("incomparable", [])) or "<li>无</li>"
    fc = data.get("frozen_conditions") or {}

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"/>
<title>故障安全退化趋势 {esc(valve['tag'] if valve else '')} {data.get('fail_mode', '')}</title>
<style>
 body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; margin: 24px; }}
 h1 {{ font-size: 20px; }} h2 {{ font-size: 15px; margin-top: 20px; border-bottom: 1px solid #d1d5db; }}
 table {{ border-collapse: collapse; font-size: 12px; }}
 th, td {{ border: 1px solid #d1d5db; padding: 4px 8px; text-align: left; }}
 th {{ background: #f3f4f6; }} .ok {{ color: #059669; font-weight: 600; }}
 .bad {{ color: #dc2626; font-weight: 600; }} .warn {{ color: #d97706; }}
 .reason {{ font-size: 11px; color: #4b5563; }} ul {{ font-size: 12px; }}
 .verdict {{ display: inline-block; padding: 2px 10px; border-radius: 4px; font-weight: 700;
   background: {'#fee2e2' if overall == 'degrading' else ('#d1fae5' if overall == 'stable' else '#fef3c7')}; }}
 @media print {{ body {{ margin: 8mm; }} }}
</style></head><body>
<h1>故障安全动作退化趋势报告</h1>
<table><tr><td><b>阀门位号</b>：{esc(valve['tag'] if valve else str(data.get('valve_id')))}</td>
<td><b>故障模式</b>：{MODE_NAMES.get(data.get('fail_mode'), data.get('fail_mode', ''))}</td>
<td><b>总体趋势</b>：<span class="verdict">{TREND_NAMES.get(overall, overall)}</span></td></tr></table>

<h2>冻结工况（对齐基准）</h2>
<ul>
<li>负载：{esc(str(fc.get('load')))}；介质：{esc(str(fc.get('medium')))}；
执行器：{esc(str(fc.get('actuator_type')))}</li>
<li>供压：{fc.get('supply_pressure_kpa')} kPa；环境温度：{fc.get('ambient_temp_c')} °C；
观察窗：{fc.get('window_length_s')}s</li>
<li>冻结阈值：{esc(', '.join(f'{k}={v}' for k, v in (fc.get('thresholds') or {}).items()))}</li>
</ul>

<h2>历次测试与可比性</h2>
<table><thead><tr><th>FS测试</th><th>测试时间</th><th>阶段</th><th>结论</th><th>参与趋势</th><th>不可比原因</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>

<h2>逐指标退化趋势</h2>
<table><thead><tr><th>指标</th><th>历次实测（按时间）</th><th>趋势</th><th>首末差</th><th>阈值</th><th>末点判定</th>
</tr></thead><tbody>{''.join(trend_rows)}</tbody></table>

<h2>不可比版本及原因</h2>
<ul>{incomparable}</ul>
</body></html>"""
