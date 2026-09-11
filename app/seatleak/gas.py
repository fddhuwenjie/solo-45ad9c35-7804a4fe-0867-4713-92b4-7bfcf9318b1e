"""气体阀座密封保持试验：温压补偿质量平衡与空白回升基线。

封闭下游容积内的等效标准体积（温度-压力补偿）：

    Veq(t) = P(t) · V · T_ref / (Z · (T(t) + 273.15) · P_ref)

泄漏率 = 等效标准体积变化率扣除空白回升速率后的净速率（标准状态，Nl/min）。
温度升高造成的压力回升在 Veq 中被自动补偿，不会误判为内漏；空白基线
（同温同压下无泄漏阀门的残余回升）进一步扣除仪表滞后/释气等非泄漏贡献。
"""

TEMP_OFFSET_K = 273.15

# 流量单位 → 标准升每分钟 (Nl/min)
FLOW_TO_NL_MIN = {
    "nl/min": 1.0,
    "nl/h": 1.0 / 60.0,
    "nml/min": 0.001,
    "sccm": 0.001,
    "slm": 1.0,
    "sl/min": 1.0,
    "nm3/h": 1000.0 / 60.0,
    "nm3/min": 1000.0,
}


def norm_temperature(points, unit, channel="downstream_temp"):
    """温度统一为 °C。返回 (values_c, notes, conflicts)。"""
    u = (unit or "").strip().lower()
    notes, conflicts = [], []
    vals = [float(p[1]) for p in points]
    if not vals:
        conflicts.append(f"{channel}: 采样点为空")
        return [], notes, conflicts
    if u in ("c", "°c", "celsius", "degc"):
        out = vals
    elif u in ("k", "kelvin"):
        out = [v - TEMP_OFFSET_K for v in vals]
        notes.append(f"{channel}: K 已换算为 °C")
    elif u in ("f", "°f", "fahrenheit"):
        out = [(v - 32.0) / 1.8 for v in vals]
        notes.append(f"{channel}: °F 已换算为 °C")
    else:
        conflicts.append(f"{channel}: 不支持的温度单位 {unit!r}")
        return vals, notes, conflicts
    if min(out) < -TEMP_OFFSET_K:
        conflicts.append(f"{channel}: 温度低于绝对零度（{min(out):.1f} °C），疑似单位声明错误")
    return out, notes, conflicts


def norm_flow(points, unit, channel="flow"):
    """流量统一为标准状态 Nl/min。返回 (values_nl_min, notes, conflicts)。"""
    u = (unit or "").strip().lower()
    notes, conflicts = [], []
    vals = [float(p[1]) for p in points]
    if not vals:
        conflicts.append(f"{channel}: 采样点为空")
        return [], notes, conflicts
    factor = FLOW_TO_NL_MIN.get(u)
    if factor is None:
        conflicts.append(
            f"{channel}: 不支持的流量单位 {unit!r}（允许 { '/'.join(sorted(FLOW_TO_NL_MIN)) }，"
            "均按标准状态体积流量计）")
        return vals, notes, conflicts
    if factor != 1.0:
        notes.append(f"{channel}: {unit} 已换算为 Nl/min（标准状态）")
    return [v * factor for v in vals], notes, conflicts


def eq_std_volume_nl(p_kpa, temp_c, volume_m3, z, t_ref_c, p_ref_kpa):
    """等效标准体积（Nl）：封闭容积内气体折算到参考状态的体积。"""
    t_k = temp_c + TEMP_OFFSET_K
    if t_k <= 0 or z <= 0:
        return None
    return p_kpa * volume_m3 * 1000.0 * (t_ref_c + TEMP_OFFSET_K) / (z * t_k * p_ref_kpa)


def blank_envelope(baseline):
    """空白基线覆盖包络：{temp_lo, temp_hi, pressure_lo, pressure_hi}（含裕度）。"""
    pts = baseline["points"]
    temps = [p["temp_c"] for p in pts]
    prs = [p["pressure_kpa"] for p in pts]
    mt = baseline.get("temp_margin_c", 2.0)
    mp = baseline.get("pressure_margin_kpa", 20.0)
    return {
        "temp_lo": min(temps) - mt, "temp_hi": max(temps) + mt,
        "pressure_lo": min(prs) - mp, "pressure_hi": max(prs) + mp,
    }


def blank_coverage(baseline, temp_lo, temp_hi, p_lo, p_hi):
    """试验温压区间是否被空白基线包络覆盖。返回 (covered, detail, envelope)。"""
    env = blank_envelope(baseline)
    misses = []
    if temp_lo < env["temp_lo"] or temp_hi > env["temp_hi"]:
        misses.append(f"试验温度区间 [{temp_lo:.1f}, {temp_hi:.1f}]°C 超出基线包络 "
                      f"[{env['temp_lo']:.1f}, {env['temp_hi']:.1f}]°C")
    if p_lo < env["pressure_lo"] or p_hi > env["pressure_hi"]:
        misses.append(f"试验压力区间 [{p_lo:.0f}, {p_hi:.0f}]kPa 超出基线包络 "
                      f"[{env['pressure_lo']:.0f}, {env['pressure_hi']:.0f}]kPa")
    detail = ("；".join(misses) if misses else
              f"试验温压区间在空白基线包络内（温度 [{env['temp_lo']:.1f}, {env['temp_hi']:.1f}]°C，"
              f"压力 [{env['pressure_lo']:.0f}, {env['pressure_hi']:.0f}]kPa）")
    return not misses, detail, env


def blank_rate_at(baseline, temp_c, pressure_kpa):
    """按温度线性插值空白回升速率（kPa/min）；单点基线取常数。

    返回 (rate_kpa_min, method_note)。调用前应先做 blank_coverage 检查。
    """
    pts = sorted(baseline["points"], key=lambda p: p["temp_c"])
    if len(pts) == 1 or pts[0]["temp_c"] == pts[-1]["temp_c"]:
        p = min(pts, key=lambda p: abs(p["pressure_kpa"] - pressure_kpa))
        return p["recovery_rate_kpa_min"], \
            f"单点/等温基线，取最近压力点（{p['temp_c']}°C, {p['pressure_kpa']}kPa）的回升速率"
    for k in range(len(pts) - 1):
        a, b = pts[k], pts[k + 1]
        if a["temp_c"] <= temp_c <= b["temp_c"]:
            f = (temp_c - a["temp_c"]) / (b["temp_c"] - a["temp_c"])
            rate = a["recovery_rate_kpa_min"] + f * (
                b["recovery_rate_kpa_min"] - a["recovery_rate_kpa_min"])
            return rate, (f"按温度线性插值：{a['temp_c']}°C→{a['recovery_rate_kpa_min']} 与 "
                          f"{b['temp_c']}°C→{b['recovery_rate_kpa_min']} kPa/min 之间，"
                          f"试验 {temp_c:.1f}°C")
    edge = pts[0] if temp_c < pts[0]["temp_c"] else pts[-1]
    return edge["recovery_rate_kpa_min"], \
        f"试验温度超出插值区间，取最近端点（{edge['temp_c']}°C）的回升速率"


def linreg_slope(xs, ys):
    """最小二乘斜率；点数不足返回 None。"""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx


def trailing_slopes(grid, y, window_s, min_points=3):
    """因果滑动窗回归斜率（每分钟）：rates[i] 用 [t_i - window_s, t_i] 的数据。

    窗口内点数不足或时间跨度不足 80% 窗长时返回 None（序列头部）。
    """
    n = len(grid)
    rates = [None] * n
    j0 = 0
    for i in range(n):
        while grid[j0] < grid[i] - window_s - 1e-9:
            j0 += 1
        if i - j0 + 1 < min_points:
            continue
        if grid[i] - grid[j0] < 0.8 * window_s:
            continue
        xs = [(grid[k] - grid[i]) / 60.0 for k in range(j0, i + 1)]
        rates[i] = linreg_slope(xs, y[j0:i + 1])
    return rates


def leak_from_mass_balance(grid, p_down, temp_c, i0, i1, volume_m3, gas,
                           blank_rate_kpa_min, direction_sign, slope_window_s):
    """保持段 [i0, i1] 的温压补偿质量平衡。

    direction_sign: +1 上游→下游（下游升压），-1 下游→上游（下游降压）。
    返回 dict：等效标准体积序列、空白修正、净累计漏量、窗口泄漏率、
    全段回归平均泄漏率及换算参数（供导出补偿过程）。
    """
    z = gas["compressibility_z"]
    t_ref_c = gas["reference_temp_c"]
    p_ref = gas["reference_pressure_kpa"]
    veq = [eq_std_volume_nl(p, tc, volume_m3, z, t_ref_c, p_ref)
           for p, tc in zip(p_down, temp_c)]
    if any(v is None for v in veq[i0:i1 + 1]):
        raise ValueError("保持段内温度/压缩因子非法，无法换算等效标准体积")
    t_hold = grid[i0]
    t_mean = sum(temp_c[i0:i1 + 1]) / (i1 - i0 + 1)
    # 空白回升速率（kPa/min）→ 等效标准体积速率（Nl/min），按保持段平均温度
    q_blank = blank_rate_kpa_min * volume_m3 * 1000.0 * (t_ref_c + TEMP_OFFSET_K) / (
        z * (t_mean + TEMP_OFFSET_K) * p_ref)
    # 扣除空白后的定向净漏量信号：y(t) = sign · (Veq(t) − q_blank·(t−t0))
    y = [direction_sign * (veq[i] - q_blank * (grid[i] - t_hold) / 60.0)
         for i in range(len(grid))]
    cumulative = [round(y[i] - y[i0], 6) for i in range(len(grid))]
    rates = trailing_slopes(grid, y, slope_window_s)
    xs_hold = [(grid[i] - t_hold) / 60.0 for i in range(i0, i1 + 1)]
    mean_rate = linreg_slope(xs_hold, y[i0:i1 + 1])
    hold_rates = [r for r in rates[i0:i1 + 1] if r is not None]
    return {
        "veq_nl": [round(v, 6) for v in veq],
        "y_net": y,
        "cumulative_nl": cumulative,
        "rate_nl_min": [round(r, 6) if r is not None else None for r in rates],
        "mean_rate_nl_min": mean_rate,
        "max_rate_nl_min": max(hold_rates) if hold_rates else None,
        "cumulative_end_nl": cumulative[i1],
        "blank_rate_nl_min": q_blank,
        "temp_mean_hold_c": t_mean,
        "params": {"volume_m3": volume_m3, "z": z, "t_ref_k": round(t_ref_c + TEMP_OFFSET_K, 2),
                   "p_ref_kpa": p_ref, "direction_sign": direction_sign,
                   "blank_rate_kpa_min": blank_rate_kpa_min},
    }


def first_exceedance(grid, rates, cumulative, i0, i1, rate_max, cum_max, dwell_s):
    """首次超限：窗口泄漏率持续超限（dwell）或累计漏量超限，取较早者。"""
    hits = []
    if rate_max is not None:
        k = i0
        while k <= i1:
            r = rates[k]
            if r is not None and r > rate_max:
                j = k
                while j + 1 <= i1 and grid[j + 1] - grid[k] <= dwell_s + 1e-9:
                    j += 1
                    if rates[j] is not None and rates[j] <= rate_max:
                        break
                else:
                    j = min(j, i1)
                seg_rates = [rates[m] for m in range(k, j + 1) if rates[m] is not None]
                sustained = (grid[j] - grid[k] >= dwell_s - 1e-9
                             and all(r > rate_max for r in seg_rates))
                if sustained:
                    hits.append({"t_s": round(grid[k], 3), "kind": "leak_rate",
                                 "value": round(rates[k], 6), "limit": rate_max})
                    break
                k = j + 1
            else:
                k += 1
    if cum_max is not None:
        for i in range(i0, i1 + 1):
            if cumulative[i] > cum_max:
                hits.append({"t_s": round(grid[i], 3), "kind": "cumulative_leak",
                             "value": round(cumulative[i], 6), "limit": cum_max})
                break
    if not hits:
        return None
    return min(hits, key=lambda h: h["t_s"])
