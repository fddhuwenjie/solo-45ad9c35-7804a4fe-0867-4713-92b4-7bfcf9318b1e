"""单位统一：指令/阀位归一化为量程百分比 %，压力统一为 kPa。

单位冲突（无法换算、量纲与量程不符、数值越界）作为阻断问题返回，
携带冲突的测试不得用于形成维修结论。
"""

PRESSURE_TO_KPA = {
    "kpa": 1.0,
    "mpa": 1000.0,
    "bar": 100.0,
    "psi": 6.894757,
    "kgf/cm2": 98.0665,
}

TRAVEL_UNITS = {"mm", "cm", "in", "inch"}


def _norm_signal(points, unit, range_min, range_max, range_unit, channel):
    """把指令/阀位信号换算为量程百分比。返回 (values_pct, notes, conflicts)。"""
    u = (unit or "").strip().lower()
    ru = (range_unit or "").strip().lower()
    notes, conflicts = [], []
    vals = [float(p[1]) for p in points]
    if not vals:
        conflicts.append(f"{channel}: 采样点为空")
        return [], notes, conflicts

    if u == "%":
        if ru != "%":
            conflicts.append(
                f"{channel}: 信号单位为 %，但量程单位为 {range_unit}，百分比无法对应物理行程"
            )
        out = vals
        if min(vals) < -10.0 or max(vals) > 110.0:
            conflicts.append(
                f"{channel}: 百分比信号越界 [{min(vals):.1f}, {max(vals):.1f}]，疑似单位声明错误"
            )
    elif u == "ma":
        if ru != "%":
            conflicts.append(
                f"{channel}: mA 信号要求量程以 % 声明，实际为 {range_unit}"
            )
        if min(vals) < 0.0 or max(vals) > 25.0:
            conflicts.append(
                f"{channel}: mA 信号越界 [{min(vals):.2f}, {max(vals):.2f}]，与 4-20mA 不符"
            )
        out = [(v - 4.0) / 16.0 * 100.0 for v in vals]
        notes.append(f"{channel}: 4-20mA 已换算为 0-100%")
    elif u in TRAVEL_UNITS:
        if ru != u:
            conflicts.append(
                f"{channel}: 行程信号单位 {unit} 与量程单位 {range_unit} 不一致"
            )
        span = range_max - range_min
        if abs(span) < 1e-9:
            conflicts.append(f"{channel}: 量程跨度为 0，无法归一化")
            out = vals
        else:
            out = [(v - range_min) / span * 100.0 for v in vals]
            notes.append(f"{channel}: {unit} 行程已按量程 [{range_min}, {range_max}] 换算为 %")
    else:
        conflicts.append(f"{channel}: 不支持的信号单位 {unit!r}")
        out = vals
    return out, notes, conflicts


def _norm_pressure(points, unit):
    """压力统一为 kPa。返回 (values_kpa, notes, conflicts)。"""
    u = (unit or "").strip().lower()
    notes, conflicts = [], []
    vals = [float(p[1]) for p in points]
    if not vals:
        conflicts.append("pressure: 采样点为空")
        return [], notes, conflicts
    factor = PRESSURE_TO_KPA.get(u)
    if factor is None:
        conflicts.append(f"pressure: 不支持的压力单位 {unit!r}")
        return vals, notes, conflicts
    if factor != 1.0:
        notes.append(f"pressure: {unit} 已换算为 kPa")
    if min(vals) < 0.0:
        conflicts.append(f"pressure: 出现负压力 {min(vals):.2f} {unit}")
    return [v * factor for v in vals], notes, conflicts


def normalize_series(series, range_min, range_max, range_unit):
    """统一三路信号单位。

    返回 dict: {
        "command": (ts, values_pct), "position": (ts, values_pct),
        "pressure": (ts, values_kpa),
        "notes": [...], "conflicts": [...]
    }
    """
    notes, conflicts = [], []
    out = {}
    for channel in ("command", "position"):
        ch = series[channel]
        ts = [float(p[0]) for p in ch["points"]]
        vals, n, c = _norm_signal(ch["points"], ch["unit"], range_min, range_max, range_unit, channel)
        notes += n
        conflicts += c
        out[channel] = (ts, vals)
    ch = series["pressure"]
    ts = [float(p[0]) for p in ch["points"]]
    vals, n, c = _norm_pressure(ch["points"], ch["unit"])
    notes += n
    conflicts += c
    out["pressure"] = (ts, vals)
    out["notes"] = notes
    out["conflicts"] = conflicts
    return out
