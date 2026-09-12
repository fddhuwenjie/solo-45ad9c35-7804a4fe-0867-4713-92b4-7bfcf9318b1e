"""逐通道校准链的打印报告片段（五类测试报告共用）。

数据直接取自分析结果中的 calibration_chain 块，与 JSON 导出、版本比较
共用同一组修正前后差值、采用证书与拒绝原因，报告不另行重算。
"""

import html

REJECT_NAMES = {
    "calibration_not_bound": "通道未绑定校准版本",
    "calibration_version_missing": "绑定版本不存在",
    "calibration_type_mismatch": "测量类型不匹配",
    "calibration_unit_incompatible": "单位不兼容",
    "calibration_not_yet_valid": "证书尚未生效",
    "calibration_expired": "证书已过期",
    "calibration_spans_validity": "测试区间跨越有效期边界",
    "calibration_range_uncovered": "量程未覆盖原始读数",
    "calibration_points_nonmonotonic": "示值—参考值点列不单调",
    "calibration_points_outside_range": "读数超出点列覆盖域（禁止外推）",
    "series_empty": "通道无采样点",
}


def render_calibration_section(result):
    """校准链段落 HTML；legacy 模式给简短说明，链模式给逐通道表。"""
    chain = result.get("calibration_chain")
    if not chain:
        return ""
    esc = html.escape
    mode = chain.get("mode")
    if mode == "legacy":
        until = chain.get("legacy_calibration_valid_until")
        if until:
            return (
                '<h2>仪器校准（旧版单字段）</h2>'
                f'<p class="reason">本分析未采用逐通道校准链，沿用提交中的统一'
                f"校准有效期 <b>{esc(str(until))}</b>（legacy 兼容；建议改用 "
                "calibration_bindings 为每路时序冻结证书版本）。</p>")
        return ('<h2>仪器校准（旧版单字段）</h2>'
                '<p class="reason">本分析未采用逐通道校准链，且未提供统一校准有效期。</p>')

    test_iv = chain.get("test_interval")
    iv_txt = f"{test_iv[0]} ~ {test_iv[1]}" if test_iv else "时间区间未知"
    rows = []
    for c in chain.get("channels", []):
        ch = esc(c["channel"])
        if c.get("status") == "accepted":
            cert = c.get("certificate") or {}
            shifts = "；".join(
                f"{s['t']:g}s: {s['raw']:g}→{s['corrected']:g} (Δ{s['shift']:+g})"
                for s in (c.get("shift_samples") or [])[:4])
            rows.append(
                f'<tr><td>{ch}</td><td class="ok">已采用</td>'
                f'<td>{esc(str(c.get("instrument_serial")))}</td>'
                f'<td>#{c.get("calibration_version_id")}</td>'
                f'<td>{esc(str(c.get("unit")))}</td>'
                f'<td>{c.get("raw_min"):g} ~ {c.get("raw_max"):g}</td>'
                f'<td>{c.get("corrected_min"):g} ~ {c.get("corrected_max"):g}</td>'
                f'<td>均值 {c.get("mean_correction"):+g}，'
                f'最大 {c.get("max_abs_correction"):g}'
                f'<br/><span class="reason">{esc(shifts)}</span></td>'
                f'<td class="reason">{esc(cert.get("valid_from", ""))} ~ '
                f'{esc(cert.get("valid_until", ""))}<br/>'
                f'{esc(cert.get("certificate_summary", ""))}</td></tr>')
        else:
            code = c.get("rejection_code") or ""
            raw_iv = c.get("raw_reading_interval")
            raw_txt = f"{raw_iv[0]:g} ~ {raw_iv[1]:g} {esc(str(c.get('unit') or ''))}" \
                if raw_iv else "—"
            rows.append(
                f'<tr><td>{ch}</td><td class="bad">{esc(REJECT_NAMES.get(code, code))}</td>'
                f'<td>{esc(str(c.get("instrument_serial") or "—"))}</td>'
                f'<td>#{c.get("calibration_version_id") if c.get("calibration_version_id") else "—"}</td>'
                f'<td>{esc(str(c.get("unit") or "—"))}</td>'
                f'<td>{raw_txt}</td><td class="bad">不修正、不进入指标结论</td>'
                f'<td class="reason">{esc(c.get("rejection_reason", ""))}</td></tr>')
    reject_items = "".join(
        f'<li>[{esc(r["code"])}] {esc(r["channel"])}：{esc(r["detail"])}'
        + (f'（原始读数区间 {r["raw_reading_interval"][0]:g} ~ '
           f'{r["raw_reading_interval"][1]:g} {esc(str(r.get("raw_unit") or ""))}）'
           if r.get("raw_reading_interval") else "") + '</li>'
        for r in chain.get("rejections", [])) or "<li>无</li>"
    accepted_note = ('<p class="ok">全部测量通道证书有效、量程覆盖、点列单调、'
                     '单位兼容，修正后数据进入分析。</p>'
                     if chain.get("accepted") else
                     '<p class="bad">存在被拒绝通道，本版本不得形成诊断结论（no_conclusion）。</p>')
    return f"""<h2>逐通道仪器校准链</h2>
<p class="reason">测试时间区间 {esc(iv_txt)}（采样跨度 {chain.get('test_span_s')} s）。
每路时序在分析时冻结一个不可变证书版本，先按示值—参考值点列分段线性修正，
再进入时间对齐、不确定度与指标计算。</p>
<table><thead><tr><th>通道</th><th>状态</th><th>仪器序列号</th><th>证书版本</th>
<th>单位</th><th>原始读数区间</th><th>修正后区间 / 修正前后差值</th><th>有效期 / 证书摘要</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>
{accepted_note}
<p><b>拒绝原因与证据缺口</b>：</p><ul>{reject_items}</ul>"""
