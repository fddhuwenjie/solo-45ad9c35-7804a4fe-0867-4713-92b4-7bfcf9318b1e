"""气体阀座密封保持试验分析编排。

流程：单位统一 → 校准有效期 → 隔离次序校验 → 异频对齐 → 圈定稳压段/保持段 →
关位稳定性 / 有效压差 / 温度覆盖 / 空白基线覆盖检查 → 温压补偿质量平衡
（标准状态泄漏率、累计漏量、首次超限）→ 流量计交叉核对 → 判定。

判定：
- 存在证据缺口（关位未稳定、有效压差不足、隔离次序矛盾、温度断档、
  空白基线无法覆盖试验温压、单位冲突、校准失效、段无法圈定等）
  → no_conclusion，只列证据缺口，不作合格结论；
- 无缺口但泄漏率/累计漏量超限或流量计核对偏差超限 → fail；
- 全部通过 → pass。

人工调整（移动段边界 / 屏蔽异常点）以累计调整列表重算并派生新版本，
被屏蔽点保留原始引用（通道、序号、时标、数值）与理由。
"""

import json
import statistics
from datetime import datetime, timezone

from ..processing import alignment, detection, units
from .. import calibration as calchain
from . import gas, segments as segmod


def _parse_time(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _raw_ref(channel, ts, t_target, vals=None):
    """距 t_target 最近的原始采样引用。"""
    if not ts:
        return None
    k = min(range(len(ts)), key=lambda i: abs(ts[i] - t_target))
    ref = {"channel": channel, "index": k, "t": round(ts[k], 6)}
    if vals is not None:
        ref["value"] = round(vals[k], 4)
    return ref


def _coverage(ts, lo, hi):
    """区间 [lo,hi) 内原始采样相对中位间隔期望点数的覆盖率。"""
    if hi <= lo or not ts:
        return 0.0
    dts = sorted(b - a for a, b in zip(ts, ts[1:]) if b - a > 0)
    dt_med = dts[(len(dts) - 1) // 2] if dts else (hi - lo)
    expected = max(1.0, (hi - lo) / dt_med)
    n = sum(1 for t in ts if lo <= t < hi)
    return min(1.0, n / expected)


def _apply_exclusions(series, exclusions):
    """从原始序列中剔除无效点，返回 (filtered_series, exclusion_records)。"""
    records = []
    filtered = {}
    for ch, data in series.items():
        if data is None:
            filtered[ch] = None
            continue
        filtered[ch] = {"unit": data["unit"],
                        "points": [list(p) for p in data["points"]]}
    for ex in exclusions:
        ch = ex["channel"]
        if filtered.get(ch) is None:
            continue
        pts = filtered[ch]["points"]
        lo, hi = ex["start_index"], ex["end_index"]
        removed = [[i, pts[i][0], pts[i][1]] for i in range(lo, hi + 1) if 0 <= i < len(pts)]
        records.append({
            "type": "exclusion",
            "channel": ch,
            "start_index": lo,
            "end_index": hi,
            "reason": ex.get("reason", ""),
            "original_points": [{"index": i, "t": t, "value": v} for i, t, v in removed],
        })
        filtered[ch]["points"] = [p for i, p in enumerate(pts) if not (lo <= i <= hi)]
    return filtered, records


def _interp_at(ts, vs, t):
    """单点线性插值（端点外取端点值）。"""
    if not ts:
        return None
    if t <= ts[0]:
        return vs[0]
    if t >= ts[-1]:
        return vs[-1]
    for k in range(1, len(ts)):
        if ts[k] >= t:
            span = ts[k] - ts[k - 1]
            f = (t - ts[k - 1]) / span if span > 0 else 0.0
            return vs[k - 1] + f * (vs[k] - vs[k - 1])
    return vs[-1]


def _exceed_periods(grid, rates, i0, i1, limit):
    """窗口泄漏率超过限值的连续时段。"""
    periods, start = [], None
    for i in range(i0, i1 + 1):
        r = rates[i]
        over = r is not None and r > limit
        if over and start is None:
            start = i
        elif not over and start is not None:
            periods.append([round(grid[start], 3), round(grid[i - 1], 3)])
            start = None
    if start is not None:
        periods.append([round(grid[start], 3), round(grid[i1], 3)])
    return periods


def run_seat_analysis(db, sl_test_id, author="auto", new_adjustments=None,
                      bindings_override=None):
    """执行阀座密封保持试验分析并保存新版本。"""
    test = db.get_seatleak_test(sl_test_id)
    if test is None:
        raise KeyError(f"阀座密封试验 {sl_test_id} 不存在")
    payload = test["payload"]
    thresholds = dict(payload.get("thresholds") or test["thresholds"])
    new_adjustments = new_adjustments or {}

    # ---- 累计调整（上一版本 + 本次新增） ----
    existing = db.list_seatleak_analyses(sl_test_id)
    cumulative = []
    prev_result = None
    if existing:
        prev = db.get_seatleak_analysis(existing[-1]["id"])
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
    direction = payload["flow_direction"]
    sign = 1.0 if direction == "upstream_to_downstream" else -1.0
    gas_spec = payload["gas"]
    volume_m3 = payload["closed_volume"]["downstream_volume_m3"]
    blank = payload["blank_baseline"]

    # ---- 屏蔽异常点（保留原始引用） ----
    filtered, exclusion_records = _apply_exclusions(payload["series"], exclusions)

    # ---- 逐通道仪器校准链（先修正，再单位统一；flow 按气体流量计处理） ----
    bindings = calchain.resolve_bindings(payload, prev_result, bindings_override)
    corrected, chain_block = calchain.apply_chain(
        db, test_kind="seatleak", series=filtered, bindings=bindings,
        test_started_at=payload.get("test_started_at"),
        submitted_at=test["submitted_at"],
        range_min=payload["range"]["min"], range_max=payload["range"]["max"],
        flow_measurement_type="flow_gas")
    series_for_units = corrected if corrected is not None else filtered
    if chain_block["mode"] == "channel_chain":
        for r in chain_block["rejections"]:
            evidence_gaps.append({"code": r["code"], "detail": r["detail"],
                                  "channel": r["channel"],
                                  "raw_reading_interval": r["raw_reading_interval"]})

    # ---- 单位统一 ----
    rng = payload["range"]
    raw = {}
    for ch in ("command", "position"):
        vals, n, c = units.normalize_signal(
            series_for_units[ch]["points"], series_for_units[ch]["unit"],
            rng["min"], rng["max"], rng["unit"], ch)
        notes += n
        evidence_gaps += [{"code": "unit_conflict", "detail": f"单位冲突：{x}"} for x in c]
        raw[ch] = ([float(p[0]) for p in series_for_units[ch]["points"]], vals)
    for ch in ("upstream_pressure", "downstream_pressure"):
        vals, n, c = units.normalize_pressure(series_for_units[ch]["points"],
                                              series_for_units[ch]["unit"], ch)
        notes += n
        evidence_gaps += [{"code": "unit_conflict", "detail": f"单位冲突：{x}"} for x in c]
        raw[ch] = ([float(p[0]) for p in series_for_units[ch]["points"]], vals)
    temp_vals, n, c = gas.norm_temperature(series_for_units["downstream_temp"]["points"],
                                           series_for_units["downstream_temp"]["unit"])
    notes += n
    evidence_gaps += [{"code": "unit_conflict", "detail": f"单位冲突：{x}"} for x in c]
    raw["downstream_temp"] = (
        [float(p[0]) for p in series_for_units["downstream_temp"]["points"]], temp_vals)
    flow_raw = None
    if series_for_units.get("flow"):
        fvals, n, c = gas.norm_flow(series_for_units["flow"]["points"],
                                    series_for_units["flow"]["unit"])
        notes += n
        evidence_gaps += [{"code": "unit_conflict", "detail": f"单位冲突：{x}"} for x in c]
        flow_raw = ([float(p[0]) for p in series_for_units["flow"]["points"]], fvals)

    # ---- 校准有效期（legacy 模式沿用单一字段；链模式已逐通道核验） ----
    if chain_block["mode"] == "legacy":
        cal_until = _parse_time(payload.get("calibration_valid_until"))
        test_time = _parse_time(payload.get("test_started_at")) or _parse_time(test["submitted_at"])
        if cal_until is None:
            evidence_gaps.append({"code": "calibration_missing",
                                  "detail": "未提供校准有效期，无法确认仪表校准状态"})
        elif test_time and cal_until < test_time:
            evidence_gaps.append({
                "code": "calibration_expired",
                "detail": f"校准已于 {payload['calibration_valid_until']} 失效，测试时间 "
                          f"{payload.get('test_started_at') or test['submitted_at']} 晚于有效期"})
    chain_block["legacy_calibration_valid_until"] = payload.get("calibration_valid_until")

    # ---- 隔离次序 ----
    events, contradictions, derived = segmod.validate_isolation(payload["isolation"])
    evidence_gaps += contradictions
    if derived["t_isolated"] is None:
        evidence_gaps.append({
            "code": "isolation_record_missing",
            "detail": "隔离记录中缺少「下游隔离」动作，无法圈定封闭容积形成时刻"})

    unit_conflict = any(g["code"] == "unit_conflict" for g in evidence_gaps)

    metrics, checks, issues, adopted = {}, [], [], []
    segments, boundary_log = [], []
    align_meta, curves = None, None
    compensation, flow_check = None, None
    dropouts_out = []
    grid = aligned = None

    if not unit_conflict:
        try:
            grid, aligned, align_meta = alignment.align(
                raw, channels=("command", "position", "upstream_pressure",
                               "downstream_pressure", "downstream_temp"))
        except ValueError as e:
            evidence_gaps.append({"code": "channel_no_overlap",
                                  "detail": f"各通道时间轴不重叠：{e}"})
            grid = None

    if grid is not None and derived["t_isolated"] is not None:
        if grid[-1] <= derived["t_isolated"]:
            evidence_gaps.append({
                "code": "channel_no_overlap",
                "detail": "公共时间轴在下游隔离动作之前结束，无保持段数据"})
        else:
            segments, boundary_log, seg_gaps = segmod.delineate_segments(
                grid, aligned["downstream_temp"], derived, thresholds, manual_moves)
            evidence_gaps += seg_gaps

            # 段边界原始点引用
            for seg in segments:
                if seg["t_start"] is None or seg["t_end"] is None:
                    continue
                seg["references"] = {
                    "start": [r for r in (
                        _raw_ref("downstream_pressure", raw["downstream_pressure"][0],
                                 seg["t_start"], raw["downstream_pressure"][1]),
                        _raw_ref("downstream_temp", raw["downstream_temp"][0],
                                 seg["t_start"], raw["downstream_temp"][1])) if r],
                    "end": [r for r in (
                        _raw_ref("downstream_pressure", raw["downstream_pressure"][0],
                                 seg["t_end"], raw["downstream_pressure"][1]),
                        _raw_ref("downstream_temp", raw["downstream_temp"][0],
                                 seg["t_end"], raw["downstream_temp"][1])) if r],
                }

            # 断档事件（全通道，供报告与温度覆盖判定）
            dropouts_out = detection.detect_dropouts(
                {c: raw[c] for c in ("position", "upstream_pressure",
                                     "downstream_pressure", "downstream_temp")})

            hold = segments[1] if len(segments) > 1 else None
            stab = segments[0] if segments else None
            if hold and hold["i_start"] is not None:
                i0, i1 = hold["i_start"], hold["i_end"]
                t0, t1 = grid[i0], grid[i1]
                pos = aligned["position"]
                p_up = aligned["upstream_pressure"]
                p_dn = aligned["downstream_pressure"]
                temp = aligned["downstream_temp"]

                adopted.append({
                    "name": "stabilization_segment",
                    "interval_s": [round(grid[stab["i_start"]], 3),
                                   round(grid[stab["i_end"]], 3)]
                    if stab["i_end"] is not None else None,
                    "method": "下游隔离动作到保持段起点（热瞬态衰减期，不计漏量）",
                })
                adopted.append({
                    "name": "hold_segment",
                    "interval_s": [round(t0, 3), round(t1, 3)],
                    "method": "保持段：温压补偿质量平衡计量泄漏率与累计漏量",
                })

                # ---- 关位稳定性 ----
                pos_hold = pos[i0:i1 + 1]
                pos_at_iso = _interp_at(grid, pos, derived["t_isolated"])
                closed_band = thresholds["closed_band_pct"]
                drift = max(pos_hold) - min(pos_hold)
                metrics["position_at_isolation_pct"] = round(pos_at_iso, 3)
                metrics["position_max_hold_pct"] = round(max(pos_hold), 3)
                metrics["position_drift_hold_pct"] = round(drift, 3)
                if pos_at_iso > closed_band:
                    evidence_gaps.append({
                        "code": "closed_position_unstable",
                        "detail": f"下游隔离时阀位 {pos_at_iso:.2f}%，未进入关位带 "
                                  f"≤{closed_band}%，阀门未关到位即隔离"})
                elif max(pos_hold) > closed_band or drift > thresholds["position_drift_pct_max"]:
                    evidence_gaps.append({
                        "code": "closed_position_unstable",
                        "detail": f"保持段关位未稳定：最高 {max(pos_hold):.2f}%"
                                  f"（关位带 ≤{closed_band}%），漂移 {drift:.2f}%"
                                  f"（允许 ≤{thresholds['position_drift_pct_max']}%），"
                                  "阀杆可能未持续压紧阀座"})

                # ---- 有效压差 ----
                dp = [sign * (u - d) for u, d in zip(p_up[i0:i1 + 1], p_dn[i0:i1 + 1])]
                dp_med = statistics.median(dp)
                metrics["differential_median_kpa"] = round(dp_med, 2)
                metrics["differential_min_kpa"] = round(min(dp), 2)
                if dp_med < thresholds["min_differential_kpa"]:
                    evidence_gaps.append({
                        "code": "insufficient_differential_pressure",
                        "detail": f"保持段有效压差中位 {dp_med:.1f} kPa 低于要求的 "
                                  f"{thresholds['min_differential_kpa']} kPa，"
                                  "泄漏驱动力不足，试验无效"})

                # ---- 温度覆盖（断档） ----
                cov = _coverage(raw["downstream_temp"][0], t0, t1)
                metrics["temp_coverage_hold"] = round(cov, 3)
                if cov < thresholds["temp_coverage_min"]:
                    evidence_gaps.append({
                        "code": "temperature_dropout",
                        "detail": f"保持段温度采样覆盖率 {cov * 100:.0f}% 低于要求的 "
                                  f"{thresholds['temp_coverage_min'] * 100:.0f}%，"
                                  "温压补偿不可靠"})

                # ---- 空白基线覆盖 ----
                t_lo, t_hi = min(temp[i0:i1 + 1]), max(temp[i0:i1 + 1])
                p_lo, p_hi = min(p_dn[i0:i1 + 1]), max(p_dn[i0:i1 + 1])
                covered, cov_detail, envelope = gas.blank_coverage(
                    blank, t_lo, t_hi, p_lo, p_hi)
                metrics["blank_envelope"] = envelope
                if not covered:
                    evidence_gaps.append({
                        "code": "blank_baseline_uncovered",
                        "detail": f"空白回升基线无法覆盖试验温压：{cov_detail}，"
                                  "温度引起的压力回升无法与内漏区分"})

                # ---- 温压补偿质量平衡 ----
                blank_rate, blank_note = gas.blank_rate_at(
                    blank, sum(temp[i0:i1 + 1]) / (i1 - i0 + 1),
                    statistics.median(p_dn[i0:i1 + 1]))
                mb = gas.leak_from_mass_balance(
                    grid, p_dn, temp, i0, i1, volume_m3, gas_spec,
                    blank_rate, sign, thresholds["slope_window_s"])
                metrics.update({
                    "hold_duration_s": round(t1 - t0, 3),
                    "stabilization_duration_s": (
                        round(grid[stab["i_end"]] - grid[stab["i_start"]], 3)
                        if stab["i_end"] is not None else None),
                    "temp_mean_hold_c": round(mb["temp_mean_hold_c"], 3),
                    "p_down_start_kpa": round(p_dn[i0], 2),
                    "p_down_end_kpa": round(p_dn[i1], 2),
                    "veq_start_nl": mb["veq_nl"][i0],
                    "veq_end_nl": mb["veq_nl"][i1],
                    "blank_rate_kpa_min": round(blank_rate, 6),
                    "blank_rate_nl_min": round(mb["blank_rate_nl_min"], 6),
                    "leak_rate_mean_nl_min": (round(mb["mean_rate_nl_min"], 6)
                                              if mb["mean_rate_nl_min"] is not None else None),
                    "leak_rate_max_nl_min": (round(mb["max_rate_nl_min"], 6)
                                             if mb["max_rate_nl_min"] is not None else None),
                    "cumulative_leak_nl": round(mb["cumulative_end_nl"], 6),
                })
                first_hit = gas.first_exceedance(
                    grid, mb["rate_nl_min"], mb["cumulative_nl"], i0, i1,
                    thresholds["leak_rate_max_nl_min"],
                    thresholds["cumulative_leak_max_nl"],
                    thresholds["rate_exceed_dwell_s"])
                metrics["first_exceedance"] = first_hit

                adopted.append({
                    "name": "blank_correction",
                    "interval_s": [round(t0, 3), round(t1, 3)],
                    "method": f"空白回升 {blank_rate:.4f} kPa/min（{blank_note}），"
                              f"折算 {mb['blank_rate_nl_min']:.5f} Nl/min 已从净漏量扣除",
                    "value": round(blank_rate, 6),
                })
                adopted.append({
                    "name": "leak_rate_evaluation",
                    "interval_s": [round(t0, 3), round(t1, 3)],
                    "method": f"等效标准体积 Veq=P·V·T_ref/(Z·T·P_ref) 扣除空白后，"
                              f"{thresholds['slope_window_s']}s 因果滑窗回归泄漏率；"
                              f"均值取全段最小二乘回归",
                })

                # ---- 检查项与超限 ----
                rate_mean = mb["mean_rate_nl_min"]
                checks.append({
                    "metric": "leak_rate_mean_nl_min",
                    "value": round(rate_mean, 6) if rate_mean is not None else None,
                    "threshold_max": thresholds["leak_rate_max_nl_min"],
                    "pass": (rate_mean is not None
                             and rate_mean <= thresholds["leak_rate_max_nl_min"]),
                    "basis": "保持段等效标准体积净变化率（温压补偿并扣除空白回升）",
                })
                cum_end = mb["cumulative_end_nl"]
                checks.append({
                    "metric": "cumulative_leak_nl",
                    "value": round(cum_end, 6),
                    "threshold_max": thresholds["cumulative_leak_max_nl"],
                    "pass": cum_end <= thresholds["cumulative_leak_max_nl"],
                    "basis": "保持段累计净漏量（标准状态）",
                })
                if rate_mean is not None and rate_mean > thresholds["leak_rate_max_nl_min"]:
                    issues.append({
                        "kind": "leak_rate_exceeded",
                        "detail": f"泄漏率均值 {rate_mean:.4f} Nl/min 超过限值 "
                                  f"{thresholds['leak_rate_max_nl_min']} Nl/min",
                        "value": round(rate_mean, 6),
                        "threshold": thresholds["leak_rate_max_nl_min"],
                        "periods": _exceed_periods(grid, mb["rate_nl_min"], i0, i1,
                                                   thresholds["leak_rate_max_nl_min"]),
                    })
                if cum_end > thresholds["cumulative_leak_max_nl"]:
                    t_hit = first_hit["t_s"] if first_hit else t0
                    issues.append({
                        "kind": "cumulative_leak_exceeded",
                        "detail": f"累计漏量 {cum_end:.4f} Nl 超过限值 "
                                  f"{thresholds['cumulative_leak_max_nl']} Nl",
                        "value": round(cum_end, 6),
                        "threshold": thresholds["cumulative_leak_max_nl"],
                        "periods": [[round(t_hit, 3), round(t1, 3)]],
                    })

                # ---- 补偿过程（导出/报告用，含原始点引用） ----
                eval_idx = sorted({i0, (i0 + i1) // 2, i1} | (
                    {min(range(len(grid)),
                         key=lambda i: abs(grid[i] - first_hit["t_s"]))}
                    if first_hit else set()))
                eval_points = []
                for i in eval_idx:
                    eval_points.append({
                        "t_s": round(grid[i], 3),
                        "p_down_kpa": round(p_dn[i], 3),
                        "temp_c": round(temp[i], 3),
                        "veq_nl": mb["veq_nl"][i],
                        "blank_correction_nl": round(
                            mb["blank_rate_nl_min"] * (grid[i] - t0) / 60.0, 6),
                        "net_cumulative_nl": mb["cumulative_nl"][i],
                        "leak_rate_nl_min": mb["rate_nl_min"][i],
                        "raw_refs": [r for r in (
                            _raw_ref("downstream_pressure", raw["downstream_pressure"][0],
                                     grid[i], raw["downstream_pressure"][1]),
                            _raw_ref("downstream_temp", raw["downstream_temp"][0],
                                     grid[i], raw["downstream_temp"][1])) if r],
                    })
                compensation = {
                    "method": "温压补偿质量平衡：Veq=P·V·T_ref/(Z·T·P_ref)，"
                              "扣除空白回升后按流向符号换算标准状态泄漏率",
                    "parameters": mb["params"],
                    "blank_interp_note": blank_note,
                    "evaluation_points": eval_points,
                }

                # ---- 流量计交叉核对 ----
                if flow_raw is not None:
                    fts, fvs = flow_raw
                    flow_grid = [(_interp_at(fts, fvs, t)
                                  if fts[0] <= t <= fts[-1] else None) for t in grid]
                    hold_flow = [v for v in flow_grid[i0:i1 + 1] if v is not None]
                    flow_cov = _coverage(fts, t0, t1)
                    fc = {"flow_coverage_hold": round(flow_cov, 3)}
                    if flow_cov >= 0.5 and hold_flow:
                        q_flow = sum(hold_flow) / len(hold_flow)
                        q_mb = rate_mean if rate_mean is not None else 0.0
                        scale = max(thresholds["leak_rate_max_nl_min"], abs(q_mb))
                        dev_pct = abs(q_flow - q_mb) / scale * 100.0
                        fc.update({
                            "flow_mean_nl_min": round(q_flow, 6),
                            "mass_balance_nl_min": (round(q_mb, 6)
                                                    if rate_mean is not None else None),
                            "deviation_pct": round(dev_pct, 2),
                            "deviation_limit_pct": thresholds["flow_deviation_pct_max"],
                            "deviation_sources": [
                                f"封闭容积标称不确定度 ±{payload['closed_volume'].get('uncertainty_pct', 10.0)}%"
                                "（直接影响质量平衡换算的比例）",
                                "流量计响应滞后与安装位置（阀前/阀后）造成的相位差",
                                "温度测点滞后使热瞬态期补偿偏差偏大",
                                "流量计若按不同参考状态标定，标准状态换算引入固定偏差",
                            ],
                        })
                        checks.append({
                            "metric": "flow_deviation_pct",
                            "value": round(dev_pct, 2),
                            "threshold_max": thresholds["flow_deviation_pct_max"],
                            "pass": dev_pct <= thresholds["flow_deviation_pct_max"],
                            "basis": "流量计均值与质量平衡泄漏率的相对偏差"
                                     "（分母取泄漏限值与质量平衡值的较大者）",
                        })
                        if dev_pct > thresholds["flow_deviation_pct_max"]:
                            issues.append({
                                "kind": "flow_massbalance_deviation",
                                "detail": f"流量计 {q_flow:.4f} Nl/min 与质量平衡 "
                                          f"{q_mb:.4f} Nl/min 偏差 {dev_pct:.1f}% 超过 "
                                          f"{thresholds['flow_deviation_pct_max']}%",
                                "value": round(dev_pct, 2),
                                "threshold": thresholds["flow_deviation_pct_max"],
                                "periods": [],
                            })
                    else:
                        fc["note"] = (f"保持段流量计覆盖率 {flow_cov * 100:.0f}% 不足 50%，"
                                      "仅记录读数，不参与交叉核对")
                    flow_check = fc
                    adopted.append({
                        "name": "flow_crosscheck",
                        "interval_s": [round(t0, 3), round(t1, 3)],
                        "method": "流量计均值与温压补偿质量平衡泄漏率比对，"
                                  "偏差来源见 deviation_sources",
                    })

                curves = {
                    "t": [round(t, 3) for t in grid],
                    "command": [round(v, 4) for v in aligned["command"]],
                    "position": [round(v, 4) for v in aligned["position"]],
                    "upstream_pressure": [round(v, 2) for v in p_up],
                    "downstream_pressure": [round(v, 2) for v in p_dn],
                    "downstream_temp": [round(v, 3) for v in temp],
                    "veq_nl": mb["veq_nl"],
                    "cumulative_leak_nl": mb["cumulative_nl"],
                    "leak_rate_nl_min": mb["rate_nl_min"],
                }
                if flow_raw is not None:
                    curves["flow_nl_min"] = [round(v, 4) if v is not None else None
                                             for v in flow_grid]
            elif segments:
                evidence_gaps.append({
                    "code": "hold_segment_missing",
                    "detail": "保持段未能圈定，无法计算泄漏率"})

    # ---- 判定 ----
    if evidence_gaps:
        verdict = "no_conclusion"
    elif any(not c.get("pass", False) for c in checks) or issues:
        verdict = "fail"
    else:
        verdict = "pass"

    # ---- 判定依据 ----
    basis = []
    for c in checks:
        line = f"{c['metric']}："
        if c.get("value") is not None:
            line += f"实测 {c['value']}"
        if "threshold_max" in c:
            line += f"，阈值 ≤ {c['threshold_max']}"
        if "threshold_min" in c:
            line += f"，阈值 ≥ {c['threshold_min']}"
        line += f"，判定 {'通过' if c.get('pass') else '不通过'}"
        if c.get("basis"):
            line += f"（依据：{c['basis']}）"
        basis.append(line)
    if metrics.get("first_exceedance"):
        fe = metrics["first_exceedance"]
        basis.append(f"首次超限：{fe['t_s']}s（{fe['kind']} 实测 {fe['value']}，"
                     f"限值 {fe['limit']}）")
    elif checks:
        basis.append("保持段内未出现泄漏率或累计漏量超限")

    result = {
        "test_id": sl_test_id,
        "valve_id": test["valve_id"],
        "flow_direction": direction,
        "gas": gas_spec,
        "closed_volume": payload["closed_volume"],
        "seat_config": payload.get("conditions", {}).get("seat_config"),
        "thresholds": thresholds,
        "conditions": test["conditions"],
        "unit_notes": notes,
        "verdict": verdict,
        "evidence_gaps": evidence_gaps,
        "calibration_chain": chain_block,
        "isolation": {"events": events, "derived": derived},
        "segments": segments,
        "boundary_log": boundary_log,
        "metrics": metrics,
        "checks": checks,
        "issues": issues,
        "adopted_intervals": adopted,
        "compensation": compensation,
        "flow_crosscheck": flow_check,
        "dropouts": dropouts_out,
        "alignment": align_meta,
        "curves": curves,
        "exclusions": exclusion_records,
        "decision_basis": basis,
    }

    analysis_id, version = db.create_seatleak_analysis(sl_test_id, author, cumulative, result)
    result["analysis_id"] = analysis_id
    result["version"] = version
    db._conn.execute("UPDATE seatleak_analyses SET result_json=? WHERE id=?",
                     (json.dumps(result), analysis_id))
    db._conn.commit()
    return db.get_seatleak_analysis(analysis_id)
