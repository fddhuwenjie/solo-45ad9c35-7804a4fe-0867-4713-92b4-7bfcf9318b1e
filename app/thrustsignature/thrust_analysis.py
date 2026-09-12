"""阀杆推力签名分析编排。

流程：执行机构配置校验（类型/面积/弹簧版本）→ 必需压力通道核对 →
单位统一（面积 m²、压力 kPa、弹簧力 N）→ 校准有效期 → 异频对齐 →
弹簧曲线校验（覆盖试验行程）→ 行程/相位划分（启程、匀速、换向、离座、落座）→
净推力曲线（压差×面积±弹簧力）→ 启动力 / 运行摩擦 / 摩擦带 / 离座力 /
落座裕量 / 异常区间 → 判定。

判定：
- 证据缺口（必需压力通道缺失、时间无重叠、参数量纲不符、校准失效、
  弹簧曲线不覆盖行程、相位无法圈定等）→ no_conclusion，只列证据缺口；
- 无缺口但推力/摩擦/落座指标超限或供压不足 → fail；
- 全部通过 → pass。

人工调整（移动相位边界 / 屏蔽异常点）累计重算并派生不可覆盖的新版本。
"""

import json
import statistics
from datetime import datetime, timezone

from ..processing import alignment, detection, units
from .. import calibration as calchain
from . import mechanics, phases as phmod

PHASE_NAMES = {"breakaway": "启程", "unseat": "离座", "running": "匀速",
               "seating": "落座", "reversal": "换向停留"}

# 检查项键 → (中文名, 单位)
METRIC_LABELS = {
    "breakaway_open_peak_n": ("开阀启动力", "N"),
    "breakaway_close_peak_n": ("关阀启动力", "N"),
    "running_friction_open_n": ("开阀运行摩擦", "N"),
    "running_friction_close_n": ("关阀运行摩擦", "N"),
    "friction_band_open_n": ("开阀摩擦带", "N"),
    "friction_band_close_n": ("关阀摩擦带", "N"),
    "friction_band_total_n": ("总摩擦带（开/关中位差）", "N"),
    "unseat_open_peak_n": ("开阀离座力", "N"),
    "seating_close_n": ("关阀落座力", "N"),
    "seating_margin_n": ("落座裕量（相对最小密封力）", "N"),
}


def _parse_time(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _raw_ref(channel, ts, t_target, vals=None):
    if not ts:
        return None
    k = min(range(len(ts)), key=lambda i: abs(ts[i] - t_target))
    ref = {"channel": channel, "index": k, "t": round(ts[k], 6)}
    if vals is not None:
        ref["value"] = round(vals[k], 4)
    return ref


def _apply_exclusions(series, channels, exclusions):
    records, filtered = [], {}
    for ch in channels:
        data = series.get(ch)
        filtered[ch] = (None if data is None
                        else {"unit": data["unit"],
                              "points": [list(p) for p in data["points"]]})
    for ex in exclusions:
        ch = ex["channel"]
        if filtered.get(ch) is None:
            continue
        pts = filtered[ch]["points"]
        lo, hi = ex["start_index"], ex["end_index"]
        removed = [[i, pts[i][0], pts[i][1]] for i in range(lo, hi + 1) if 0 <= i < len(pts)]
        records.append({
            "type": "exclusion", "channel": ch,
            "start_index": lo, "end_index": hi,
            "reason": ex.get("reason", ""),
            "original_points": [{"index": i, "t": t, "value": v} for i, t, v in removed],
        })
        filtered[ch]["points"] = [p for i, p in enumerate(pts) if not (lo <= i <= hi)]
    return filtered, records


def _periods_over(grid, vals, i0, i1, predicate):
    """predicate(v) 为真的连续网格区间 → [[t_start, t_end], ...]。"""
    periods, start = [], None
    for i in range(i0, i1 + 1):
        if predicate(vals[i]):
            if start is None:
                start = i
        elif start is not None:
            periods.append([round(grid[start], 3), round(grid[i - 1], 3)])
            start = None
    if start is not None:
        periods.append([round(grid[start], 3), round(grid[i1], 3)])
    return periods


def _phase_fnet(thrust, ph):
    return [thrust[i]["f_net_n"] for i in range(ph["i_start"], ph["i_end"] + 1)]


def _check(checks, metric, value, thr_max=None, thr_min=None, basis=""):
    chk = {"metric": metric, "value": round(value, 2) if value is not None else None,
           "pass": value is not None, "basis": basis}
    if thr_max is not None:
        chk["threshold_max"] = thr_max
        chk["pass"] = value is not None and value <= thr_max
    if thr_min is not None:
        chk["threshold_min"] = thr_min
        chk["pass"] = value is not None and value >= thr_min
    checks.append(chk)


def run_thrust_analysis(db, ts_test_id, author="auto", new_adjustments=None,
                        bindings_override=None):
    """执行阀杆推力签名分析并保存新版本。"""
    test = db.get_thrust_test(ts_test_id)
    if test is None:
        raise KeyError(f"阀杆推力测试 {ts_test_id} 不存在")
    payload = test["payload"]
    thresholds = dict(payload.get("thresholds") or test["thresholds"])
    actuator_in = payload["actuator"]
    new_adjustments = new_adjustments or {}

    # ---- 累计调整 ----
    existing = db.list_thrust_analyses(ts_test_id)
    cumulative = []
    prev_result = None
    if existing:
        prev = db.get_thrust_analysis(existing[-1]["id"])
        cumulative = list(prev["adjustments"])
        prev_result = prev["result"]
    for mv in new_adjustments.get("segment_moves", []):
        cumulative.append({"type": "segment_move", "author": author, **mv})
    for ex in new_adjustments.get("exclusions", []):
        cumulative.append({"type": "exclusion", "author": author, **ex})
    if new_adjustments.get("calibration_bindings") is not None:
        cumulative.append(calchain.rebind_adjustment(
            author, new_adjustments["calibration_bindings"]))
    exclusions = [a for a in cumulative if a["type"] == "exclusion"]
    manual_moves = [a for a in cumulative if a["type"] == "segment_move"]

    evidence_gaps, notes = [], []
    series_in = payload["series"]
    all_channels = ("command", "position", "supply_pressure",
                    "chamber_a_pressure", "chamber_b_pressure")
    filtered, exclusion_records = _apply_exclusions(series_in, all_channels, exclusions)

    # ---- 逐通道仪器校准链（先修正，再单位统一） ----
    bindings = calchain.resolve_bindings(payload, prev_result, bindings_override)
    corrected, chain_block = calchain.apply_chain(
        db, test_kind="thrust", series=filtered, bindings=bindings,
        test_started_at=payload.get("test_started_at"),
        submitted_at=test["submitted_at"],
        range_min=payload["range"]["min"], range_max=payload["range"]["max"])
    series_for_units = corrected if corrected is not None else filtered
    if chain_block["mode"] == "channel_chain":
        for r in chain_block["rejections"]:
            evidence_gaps.append({"code": r["code"], "detail": r["detail"],
                                  "channel": r["channel"],
                                  "raw_reading_interval": r["raw_reading_interval"]})

    # ---- 执行机构配置（面积量纲） ----
    cfg, unit_block = None, False
    try:
        cfg = mechanics.actuator_config(actuator_in)
    except ValueError as e:
        evidence_gaps.append({"code": "unit_conflict",
                              "detail": f"参数量纲不符：{e}"})
        unit_block = True

    # ---- 必需压力通道核对（按执行器类型） ----
    act_type = actuator_in["actuator_type"]
    present = {ch: bool(filtered.get(ch) and filtered[ch]["points"])
               for ch in all_channels}
    if present["supply_pressure"] is False:
        evidence_gaps.append({"code": "pressure_channel_missing",
                              "detail": "缺少供气压力通道，无法核对供压是否充足"})
    if present["chamber_a_pressure"] is False:
        evidence_gaps.append({"code": "pressure_channel_missing",
                              "detail": "缺少 A 腔（工作腔）压力通道，无法计算阀杆净推力"})
    if act_type == "double_acting" and present["chamber_b_pressure"] is False:
        evidence_gaps.append({"code": "pressure_channel_missing",
                              "detail": "双作用执行器缺少 B 腔压力通道，"
                                        "压差无法建立，净推力不可计算"})
    if act_type == "single_acting" and actuator_in.get("spring") is None:
        evidence_gaps.append({"code": "spring_curve_missing",
                              "detail": "单作用执行器必须提供弹簧曲线"})
    if present["chamber_b_pressure"] and act_type == "single_acting":
        notes.append("单作用执行器提供了 B 腔压力，净推力计算不采用该通道")

    # ---- 单位统一 ----
    rng = payload["range"]
    raw = {}
    for ch in ("command", "position"):
        if not present[ch]:
            continue
        vals, n, c = units.normalize_signal(
            series_for_units[ch]["points"], series_for_units[ch]["unit"],
            rng["min"], rng["max"], rng["unit"], ch)
        notes += n
        evidence_gaps += [{"code": "unit_conflict", "detail": f"单位冲突：{x}"} for x in c]
        raw[ch] = ([float(p[0]) for p in series_for_units[ch]["points"]], vals)
    for ch in ("supply_pressure", "chamber_a_pressure", "chamber_b_pressure"):
        if not present[ch]:
            continue
        vals, n, c = units.normalize_pressure(series_for_units[ch]["points"],
                                              series_for_units[ch]["unit"], ch)
        notes += n
        evidence_gaps += [{"code": "unit_conflict", "detail": f"单位冲突：{x}"} for x in c]
        raw[ch] = ([float(p[0]) for p in series_for_units[ch]["points"]], vals)
    if any(g["code"] == "unit_conflict" for g in evidence_gaps):
        unit_block = True

    # ---- 校准有效期（legacy 模式沿用单一字段；链模式已逐通道核验） ----
    if chain_block["mode"] == "legacy":
        cal_until = _parse_time(payload.get("calibration_valid_until"))
        test_time = _parse_time(payload.get("test_started_at")) or _parse_time(test["submitted_at"])
        if cal_until is None:
            evidence_gaps.append({"code": "calibration_missing",
                                  "detail": "未提供校准有效期，压力/阀位仪表校准状态未知"})
        elif test_time and cal_until < test_time:
            evidence_gaps.append({
                "code": "calibration_expired",
                "detail": f"校准已于 {payload['calibration_valid_until']} 失效，测试时间 "
                          f"{payload.get('test_started_at') or test['submitted_at']} 晚于有效期"})
    chain_block["legacy_calibration_valid_until"] = payload.get("calibration_valid_until")

    metrics, checks, issues, adopted = {}, [], [], []
    runs_out, phases_out, boundary_log = [], [], []
    align_meta, curves = None, None
    dropouts_out = []
    grid = thrust = vel = None

    need_align = [c for c in ("command", "position", "supply_pressure",
                              "chamber_a_pressure") if present[c]]
    if not unit_block and len(need_align) == 4:
        align_channels = tuple(need_align +
                               (["chamber_b_pressure"] if present["chamber_b_pressure"] else []))
        try:
            grid, aligned, align_meta = alignment.align(raw, channels=align_channels)
        except ValueError as e:
            evidence_gaps.append({"code": "channel_no_overlap",
                                  "detail": f"各通道时间轴不重叠：{e}"})
            grid = None

    if grid is not None and cfg is not None:
        pos = aligned["position"]
        travel_lo, travel_hi = min(pos), max(pos)

        # ---- 弹簧曲线校验与插值函数 ----
        spring_fn = None
        if act_type == "single_acting":
            spring_fn, sprobs = mechanics.validate_spring(
                actuator_in.get("spring"), travel_lo, travel_hi,
                thresholds["spring_coverage_margin_pct"])
            evidence_gaps += sprobs
        else:
            if actuator_in.get("spring"):
                notes.append("双作用执行器附带的弹簧曲线不参与净推力计算")

        # ---- 净推力曲线（相位划分需借助建压完成点） ----
        p_b = aligned.get("chamber_b_pressure")
        thrust = mechanics.thrust_curve(
            pos, aligned["chamber_a_pressure"], p_b,
            aligned["supply_pressure"], cfg, spring_fn)
        fnet = [q["f_net_n"] for q in thrust]

        # ---- 行程/相位划分 ----
        runs_out, phases_out, boundary_log, seg_gaps, vel = phmod.delineate(
            grid, aligned["command"], pos, thresholds,
            manual_moves=manual_moves, fnet=fnet)
        evidence_gaps += seg_gaps

        dropouts_out = detection.detect_dropouts(
            {c: raw[c] for c in raw})

        def phase(run, name):
            return next((p for p in phases_out
                         if p["run"] == run and p["phase"] == name), None)

        def run_span(run):
            r = next(r for r in runs_out if r["type"] == run)
            return r["i_start"], r["i_end"]

        # ---- 启动力（启程相位内沿运动方向的净推力极值） ----
        # 开阀取最大正向推力，关阀取最大负向推力（绝对值），避开反向弹簧预紧
        for run, label, key in (
                ("opening", "开阀", "breakaway_open_peak_n"),
                ("closing", "关阀", "breakaway_close_peak_n")):
            ph = phase(run, "breakaway")
            if ph:
                vals = _phase_fnet(thrust, ph)
                peak = max(vals) if run == "opening" else min(vals)
                metrics[key] = round(abs(peak), 2)
                metrics[key.replace("_peak_n", "_signed_peak_n")] = round(peak, 2)

        # ---- 离座力（离座相位内最大正向净推力） ----
        ph = phase("opening", "unseat")
        if ph:
            vals = _phase_fnet(thrust, ph)
            peak = max(vals)
            metrics["unseat_open_peak_n"] = round(abs(peak), 2)
            metrics["unseat_open_signed_peak_n"] = round(peak, 2)

        # ---- 运行摩擦（匀速段中位力的绝对值）与摩擦带 ----
        med_open = med_close = None
        for run, pref, bkey, mkey in (
                ("opening", "开阀", "friction_band_open_n", "running_friction_open_n"),
                ("closing", "关阀", "friction_band_close_n", "running_friction_close_n")):
            ph = phase(run, "running")
            if ph:
                vals = _phase_fnet(thrust, ph)
                med = statistics.median(vals)
                if run == "opening":
                    med_open = med
                else:
                    med_close = med
                metrics[mkey] = round(abs(med), 2)
                metrics[mkey.replace("friction", "friction_signed")] = round(med, 2)
                band = max(vals) - min(vals)
                metrics[bkey] = round(band, 2)
                adopted.append({
                    "name": f"{pref}_running_friction",
                    "interval_s": [round(grid[ph["i_start"]], 3),
                                   round(grid[ph["i_end"]], 3)],
                    "method": f"{pref}匀速段净推力中位（{len(vals)} 点）取绝对值为运行摩擦；"
                              "段内最大-最小为摩擦带",
                    "value": round(abs(med), 2)})
        if med_open is not None and med_close is not None:
            metrics["friction_band_total_n"] = round(abs(med_open - med_close), 2)

        # ---- 落座力与落座裕量 ----
        ph = phase("closing", "seating")
        if ph:
            # 落座力：行程终点附近（末 20% 落座相位）净推力中位的绝对值（压紧方向）
            k0 = ph["i_start"] + int((ph["i_end"] - ph["i_start"]) * 0.8)
            tail = _phase_fnet(thrust, {**ph, "i_start": k0})
            seat_force = statistics.median(tail)
            metrics["seating_close_n"] = round(abs(seat_force), 2)
            metrics["seating_close_signed_n"] = round(seat_force, 2)
            metrics["seating_margin_n"] = round(
                abs(seat_force) - thresholds["seating_min_n"], 2)

        # ---- 检查项 ----
        if metrics.get("breakaway_open_peak_n") is not None:
            _check(checks, "breakaway_open_peak_n", metrics["breakaway_open_peak_n"],
                   thr_max=thresholds["breakaway_open_max_n"],
                   basis="开阀启程相位净推力峰值（克服初始静摩擦/卡涩所需推力）")
        if metrics.get("breakaway_close_peak_n") is not None:
            _check(checks, "breakaway_close_peak_n", metrics["breakaway_close_peak_n"],
                   thr_max=thresholds["breakaway_close_max_n"],
                   basis="关阀启程相位净推力峰值")
        for key, thrkey, basis in (
                ("running_friction_open_n", "running_friction_max_n",
                 "开阀匀速段净推力中位绝对值"),
                ("running_friction_close_n", "running_friction_max_n",
                 "关阀匀速段净推力中位绝对值"),
                ("friction_band_open_n", "friction_band_max_n",
                 "开阀匀速段净推力波动带"),
                ("friction_band_close_n", "friction_band_max_n",
                 "关阀匀速段净推力波动带"),
                ("friction_band_total_n", "friction_band_total_max_n",
                 "开/关匀速段中位力之差（总摩擦带，双向不对称）"),
                ("unseat_open_peak_n", "unseat_open_max_n",
                 "开阀离座相位净推力峰值（克服阀座负载）"),
                ("seating_close_n", "seating_max_n",
                 "关阀落座末段净推力中位（上限，过大可能压伤阀座）")):
            if metrics.get(key) is not None:
                _check(checks, key, metrics[key], thr_max=thresholds[thrkey], basis=basis)
        if metrics.get("seating_close_n") is not None:
            _check(checks, "seating_close_n", metrics["seating_close_n"],
                   thr_min=thresholds["seating_min_n"],
                   basis="关阀落座末段净推力中位（下限，保证密封比压）")

        # ---- 异常区间 ----
        # 1) 供压不足（运动行程内）
        motion_idx = [i for r in runs_out if r["type"] in ("opening", "closing")
                      for i in range(r["i_start"], r["i_end"] + 1)]
        if motion_idx:
            periods = _periods_over(
                grid, aligned["supply_pressure"], min(motion_idx), max(motion_idx),
                lambda v: v < thresholds["supply_pressure_min_kpa"])
            if periods:
                minsup = min(aligned["supply_pressure"][i] for i in motion_idx)
                issues.append({
                    "kind": "supply_pressure_low",
                    "detail": f"运动行程供气压力最低 {minsup:.0f} kPa，低于下限 "
                              f"{thresholds['supply_pressure_min_kpa']} kPa，"
                              "启动力/离座力异常可能源于供气而非阀体",
                    "periods": periods,
                    "suspected_source": "actuator"})

        # 2) 启动力超限
        for run, key, thrkey in (
                ("opening", "breakaway_open_peak_n", "breakaway_open_max_n"),
                ("closing", "breakaway_close_peak_n", "breakaway_close_max_n")):
            ph = phase(run, "breakaway")
            if ph and metrics.get(key) is not None:
                directed = (fnet if run == "opening" else [-v for v in fnet])
                periods = _periods_over(
                    grid, directed, ph["i_start"], ph["i_end"],
                    lambda v: v > thresholds[thrkey])
                if periods:
                    issues.append({
                        "kind": f"{run}_breakaway_force_exceeded",
                        "detail": f"{'开阀' if run == 'opening' else '关阀'}"
                                  f"启动力 {metrics[key]:.0f} N 超过限值 "
                                  f"{thresholds[thrkey]:.0f} N",
                        "value": metrics[key], "threshold": thresholds[thrkey],
                        "periods": periods, "suspected_source": "unknown"})

        # 3) 摩擦带 / 运行摩擦超限（运行段宽频高摩擦，指向填料/阀体卡涩）
        for run, fkey, bkey, label in (
                ("opening", "running_friction_open_n", "friction_band_open_n", "开阀"),
                ("closing", "running_friction_close_n", "friction_band_close_n", "关阀")):
            ph = phase(run, "running")
            if not ph:
                continue
            if (metrics.get(fkey) is not None
                    and metrics[fkey] > thresholds["running_friction_max_n"]):
                issues.append({
                    "kind": f"{run}_running_friction_exceeded",
                    "detail": f"{label}匀速段运行摩擦 {metrics[fkey]:.0f} N 超过限值 "
                              f"{thresholds['running_friction_max_n']:.0f} N，"
                              "填料摩擦或阀体卡涩嫌疑（执行机构匀速推力不应含此量级）",
                    "value": metrics[fkey],
                    "threshold": thresholds["running_friction_max_n"],
                    "periods": [[round(grid[ph["i_start"]], 3),
                                 round(grid[ph["i_end"]], 3)]],
                    "suspected_source": "valve_body"})
            if metrics.get(bkey) is not None and metrics[bkey] > thresholds["friction_band_max_n"]:
                issues.append({
                    "kind": f"{run}_friction_band_exceeded",
                    "detail": f"{label}匀速段摩擦带 {metrics[bkey]:.0f} N 超过限值 "
                              f"{thresholds['friction_band_max_n']:.0f} N，"
                              "粘滑/导向卡涩嫌疑",
                    "value": metrics[bkey],
                    "threshold": thresholds["friction_band_max_n"],
                    "periods": [[round(grid[ph["i_start"]], 3),
                                 round(grid[ph["i_end"]], 3)]],
                    "suspected_source": "valve_body"})

        # 4) 离座力超限（阀座负载异常）
        ph = phase("opening", "unseat")
        if ph and metrics.get("unseat_open_peak_n") is not None:
            periods = _periods_over(
                grid, fnet, ph["i_start"], ph["i_end"],
                lambda v: v > thresholds["unseat_open_max_n"])
            if periods:
                issues.append({
                    "kind": "unseat_force_exceeded",
                    "detail": f"开阀离座力 {metrics['unseat_open_peak_n']:.0f} N 超过限值 "
                              f"{thresholds['unseat_open_max_n']:.0f} N，"
                              "阀座负载异常（咬住/热膨胀/密封过压）嫌疑，区别于执行机构卡涩",
                    "value": metrics["unseat_open_peak_n"],
                    "threshold": thresholds["unseat_open_max_n"],
                    "periods": periods, "suspected_source": "valve_seat"})

        # 5) 落座超限（不足或过压）
        if metrics.get("seating_close_n") is not None:
            sf = metrics["seating_close_n"]
            ph = phase("closing", "seating")
            if sf < thresholds["seating_min_n"]:
                issues.append({
                    "kind": "seating_force_insufficient",
                    "detail": f"落座力 {sf:.0f} N 低于最小密封力 "
                              f"{thresholds['seating_min_n']:.0f} N，密封比压不足",
                    "value": sf, "threshold": thresholds["seating_min_n"],
                    "periods": [[round(grid[ph["i_start"]], 3),
                                 round(grid[ph["i_end"]], 3)]],
                    "suspected_source": "actuator"})
            elif sf > thresholds["seating_max_n"]:
                issues.append({
                    "kind": "seating_force_overload",
                    "detail": f"落座力 {sf:.0f} N 超过最大允许压力 "
                              f"{thresholds['seating_max_n']:.0f} N，阀座/阀杆可能压伤",
                    "value": sf, "threshold": thresholds["seating_max_n"],
                    "periods": [[round(grid[ph["i_start"]], 3),
                                 round(grid[ph["i_end"]], 3)]],
                    "suspected_source": "actuator"})

        # 采用区间（相位）汇总
        for ph in phases_out:
            if ph["phase"] == phmod.PHASE_REVERSAL:
                continue
            adopted.append({
                "name": f"{ph['run']}_{ph['phase']}",
                "interval_s": [ph["t_start"], ph["t_end"]],
                "method": f"区间：{PHASE_NAMES[ph['phase']]}（{ph['run']}）；"
                          f"起：{ph['start_reason']}；止：{ph['end_reason']}"})

        curves = {
            "t": [round(t, 3) for t in grid],
            "command": [round(v, 4) for v in aligned["command"]],
            "position": [round(v, 4) for v in pos],
            "supply_pressure": [round(v, 2) for v in aligned["supply_pressure"]],
            "chamber_a_pressure": [round(v, 2) for v in aligned["chamber_a_pressure"]],
            "f_net_n": [round(q["f_net_n"], 2) for q in thrust],
            "f_spring_n": [round(q["f_spring_n"], 2) for q in thrust],
            "velocity_pct_s": [round(v, 3) for v in vel],
        }
        if p_b is not None:
            curves["chamber_b_pressure"] = [round(v, 2) for v in p_b]

    # ---- 判定 ----
    if evidence_gaps:
        verdict = "no_conclusion"
    elif any(not c["pass"] for c in checks) or issues:
        verdict = "fail"
    else:
        verdict = "pass"

    # ---- 判定依据 ----
    basis = []
    for c in checks:
        line = f"{METRIC_LABELS.get(c['metric'], (c['metric'], ''))[0]}："
        if c.get("value") is not None:
            line += f"实测 {c['value']} N"
        if "threshold_max" in c:
            line += f"，限值 ≤ {c['threshold_max']} N"
        if "threshold_min" in c:
            line += f"，限值 ≥ {c['threshold_min']} N"
        line += f"，判定 {'通过' if c['pass'] else '不通过'}"
        if c.get("basis"):
            line += f"（依据：{c['basis']}）"
        basis.append(line)

    actuator_summary = None
    if cfg is not None:
        spring = actuator_in.get("spring")
        actuator_summary = {
            "actuator_type": act_type,
            "area_a_m2": round(cfg["area_a_m2"], 8),
            "area_b_m2": round(cfg["area_b_m2"], 8) if cfg["area_b_m2"] else None,
            "area_b_assumed_equal": cfg["area_b_assumed_equal"],
            "stem_direction": cfg["stem_direction"],
            "spring_version": spring.get("version") if spring else None,
            "spring_action": spring.get("action") if spring else None,
        }

    result = {
        "test_id": ts_test_id,
        "valve_id": test["valve_id"],
        "actuator": actuator_summary,
        "load_condition": test["conditions"].get("load"),
        "medium": test["conditions"].get("medium"),
        "thresholds": thresholds,
        "conditions": test["conditions"],
        "unit_notes": notes,
        "verdict": verdict,
        "evidence_gaps": evidence_gaps,
        "calibration_chain": chain_block,
        "runs": runs_out,
        "phases": [
            {k: v for k, v in ph.items() if not k.startswith("i_")}
            for ph in phases_out],
        "boundary_log": boundary_log,
        "metrics": metrics,
        "checks": checks,
        "issues": issues,
        "adopted_intervals": adopted,
        "dropouts": dropouts_out,
        "alignment": align_meta,
        "curves": curves,
        "exclusions": exclusion_records,
        "decision_basis": basis,
    }

    analysis_id, version = db.create_thrust_analysis(ts_test_id, author, cumulative, result)
    result["analysis_id"] = analysis_id
    result["version"] = version
    db._conn.execute("UPDATE thrust_analyses SET result_json=? WHERE id=?",
                     (json.dumps(result), analysis_id))
    db._conn.commit()
    return db.get_thrust_analysis(analysis_id)
