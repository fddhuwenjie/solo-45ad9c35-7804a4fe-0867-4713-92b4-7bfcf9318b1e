"""单相液体流量曲线校核分析编排。

流程：单位统一（阀位 %、压力 kPa、温度 °C、流量 m³/h）→ 校准有效期 →
异频对齐 → 圈定有序稳态平台 → 平台中位值逐点换算 Cv（IEC 60534-2-1）→
按设计特性（线性/等百分比/快开）最小二乘拟合容量系数 → 实测曲线、残差带、
单调性、有效调节比 → 疑似堵塞 / 冲蚀 / 反装行程诊断。

排除规则（逐点单独解释，不进入拟合）：物性缺项、物性温度不适用、压差不足、
平台漂移（阀位峰峰或流量变异超限）、流量计超量程（或读数 ≤0）、气蚀/闪蒸
（阻塞流）、平台过短/点数不足、人工停用。单位冲突、校准缺失/失效、通道无
重叠、平台无法圈定属于证据缺口，整份校核给 no_conclusion，但仍返回全部
可算指标与逐点排除缘由。
"""

import json
import statistics
from datetime import datetime, timezone

from ..processing import alignment, units
from ..seatleak.gas import norm_temperature
from . import liquid, plateaus as platmod

EXCLUSION_NAMES = {
    "property_missing": "物性缺项（密度或饱和蒸气压未提供），无法换算 Cv/判别气蚀",
    "property_out_of_range": "介质温度超出物性参考温度适用带宽，密度/蒸气压不适用",
    "insufficient_differential_pressure": "有效压差不足，Cv 对压差噪声过敏，不参与拟合",
    "plateau_drift": "平台漂移：阀位峰峰或流量变异超稳态判据，非稳态测点",
    "flow_overrange": "流量计读数超量程上限（或 ≤0），读数不可信",
    "cavitation": "气蚀：实测压差超过阻塞流临界压差且 P2>Pv，线性 Cv 公式不成立",
    "flashing": "闪蒸：P2 低于饱和蒸气压且压差超过阻塞临界值，线性 Cv 公式不成立",
    "nonpositive_flow": "流量读数 ≤ 0，非有效调节点",
    "pressure_physical": "阀后绝压 ≤ 0，压力物理不合理，疑似表压/单位错误",
    "plateau_too_short": "人工调整后平台点数不足，无法统计稳态中位值",
    "manual_disabled": "人工停用测点",
}

MIN_POINTS_TO_FIT = 2


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


def _apply_exclusions(series, exclusions):
    """从原始序列剔除坏点（通道级剔除，保留原始引用），返回 (filtered, records)。"""
    records = []
    filtered = {ch: {"unit": d["unit"], "points": [list(p) for p in d["points"]]}
                for ch, d in series.items()}
    for ex in exclusions:
        ch = ex["channel"]
        if ch not in filtered:
            continue
        pts = filtered[ch]["points"]
        lo, hi = ex["start_index"], ex["end_index"]
        removed = [[i, pts[i][0], pts[i][1]] for i in range(lo, hi + 1)
                   if 0 <= i < len(pts)]
        records.append({
            "type": "exclusion", "channel": ch,
            "start_index": lo, "end_index": hi,
            "reason": ex.get("reason", ""),
            "original_points": [{"index": i, "t": t, "value": v}
                                for i, t, v in removed],
        })
        filtered[ch]["points"] = [p for i, p in enumerate(pts)
                                  if not (lo <= i <= hi)]
    return filtered, records


def _interp_at(ts, vs, t):
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


def _median(xs):
    return statistics.median(xs) if xs else None


# ---------------- 拟合与曲线指标 ----------------

def _fit_scale(positions, cvs, characteristic, rated_cv, r):
    """过原点最小二乘容量系数 k：cv ≈ k·rated_cv·f(x)。返回 (k, fit_cv, residuals)。"""
    fs = [liquid.characteristic_fraction(p, characteristic, r) for p in positions]
    denom = sum(f * f for f in fs)
    if denom <= 0:
        return None, [None] * len(fs), [None] * len(fs)
    k = sum(cv * f for cv, f in zip(cvs, fs)) / (rated_cv * denom)
    fit = [k * rated_cv * f for f in fs]
    resid = [cv - fv for cv, fv in zip(cvs, fit)]
    return k, fit, resid


def _spearman(xs, ys):
    """Spearman 秩相关（无库实现；并列取平均秩）。"""
    n = len(xs)
    if n < 2:
        return None

    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        rk = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for m in range(i, j + 1):
                rk[order[m]] = avg
            i = j + 1
        return rk

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    sxx = sum((x - mx) ** 2 for x in rx)
    syy = sum((y - my) ** 2 for y in ry)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(rx, ry)) / (sxx * syy) ** 0.5


def _evaluate_curve(adopted, characteristic, rated_cv, r, thr):
    """对采用点做拟合并产出曲线指标。adopted: 按阀位升序的点 dict 列表。"""
    positions = [p["position_pct"] for p in adopted]
    cvs = [p["cv"] for p in adopted]
    k, fit, resid = _fit_scale(positions, cvs, characteristic, rated_cv, r)

    # 镜像（反装）行程：x' = 100 − x 后再拟合
    k_rev, fit_rev, resid_rev = _fit_scale(
        [100.0 - x for x in positions], cvs, characteristic, rated_cv, r)

    rmse = (sum(e * e for e in resid) / len(resid)) ** 0.5 if resid else None
    rmse_rev = (sum(e * e for e in resid_rev) / len(resid_rev)) ** 0.5 \
        if resid_rev and k_rev is not None else None
    scale = rated_cv * k if k is not None else rated_cv
    scale_rev = rated_cv * k_rev if k_rev is not None else None
    pct_resid = [e / max(fv, 1e-9) * 100.0 for e, fv in zip(resid, fit)]

    monotonic_inversions = sum(
        1 for a, b in zip(cvs, cvs[1:]) if b + 1e-9 < a)
    rho = _spearman(positions, cvs)
    monotonic = (rho is not None and rho >= thr["monotonic_rho_min"]
                 and monotonic_inversions == 0)

    return {
        "n_adopted": len(adopted),
        "capacity_factor": round(k, 4) if k is not None else None,
        "fitted_rated_cv": round(scale, 4) if k is not None else None,
        "capacity_factor_mirrored": round(k_rev, 4) if k_rev is not None else None,
        "rmse_cv": round(rmse, 4) if rmse is not None else None,
        "rmse_pct": round(rmse / max(scale, 1e-9) * 100.0, 2)
        if rmse is not None else None,
        "rmse_cv_mirrored": round(rmse_rev, 4) if rmse_rev is not None else None,
        "residual_band_pct": [round(min(pct_resid), 2), round(max(pct_resid), 2)]
        if pct_resid else None,
        "max_abs_residual_pct": round(max((abs(e) for e in pct_resid), default=0.0), 2),
        "monotonic": monotonic,
        "spearman_rho": round(rho, 3) if rho is not None else None,
        "monotonic_inversions": monotonic_inversions,
        "fit_cv_by_point": [round(v, 4) for v in fit],
        "fit_cv_mirrored_by_point": [round(v, 4) for v in fit_rev],
    }


def _diagnose_suspects(points, curve, characteristic, rated_cv, r, thr):
    """堵塞 / 冲蚀 / 反装行程嫌疑诊断。返回 suspects 列表。"""
    adopted = [p for p in points if p["adopted"]]
    suspects = []
    if not adopted or curve["capacity_factor"] is None:
        return suspects
    k = curve["capacity_factor"]
    # 容量类结论（堵塞/冲蚀）仅在正向特性确实近似匹配（残差小）时成立；
    # 正向残差大说明曲线形状不匹配，容量系数失去物理意义（如反装）。
    shape_matched = (curve["rmse_pct"] is not None
                     and curve["rmse_pct"] <= thr["residual_warn_pct"])
    if shape_matched and curve["n_adopted"] >= MIN_POINTS_TO_FIT \
            and k < thr["blockage_scale_max"]:
        suspects.append({
            "kind": "blockage",
            "detail": f"实测容量系数 k={k:.3f} 低于堵塞判据 "
                      f"{thr['blockage_scale_max']:g}：全行程过流能力整体缩小，"
                      "阀芯/阀座结垢或异物卡阻嫌疑",
            "capacity_factor": k,
            "threshold": thr["blockage_scale_max"],
        })
    if shape_matched and curve["n_adopted"] >= MIN_POINTS_TO_FIT \
            and k > thr["erosion_scale_min"]:
        suspects.append({
            "kind": "erosion",
            "detail": f"实测容量系数 k={k:.3f} 高于冲蚀判据 "
                      f"{thr['erosion_scale_min']:g}：同阀位过流偏大，"
                      "阀芯冲蚀/内件磨损嫌疑（冲蚀点常同时伴随残差带扩大）",
            "capacity_factor": k,
            "threshold": thr["erosion_scale_min"],
        })
    # 反装：正向不单调 + 镜像行程残差显著更小
    rev_match = False
    if (curve["rmse_cv_mirrored"] is not None and curve["rmse_cv"] is not None
            and curve["rmse_cv"] > 0):
        rev_match = (curve["rmse_cv_mirrored"]
                     < thr["reversed_match_ratio"] * curve["rmse_cv"])
    decreasing = (curve["spearman_rho"] is not None
                  and curve["spearman_rho"] <= -thr["monotonic_rho_min"])
    if rev_match or (decreasing and curve["n_adopted"] >= 3):
        suspects.append({
            "kind": "reversed_installation",
            "detail": "Cv 随阀位上升反而下降"
                      + (f"（Spearman ρ={curve['spearman_rho']}），镜像行程拟合残差 "
                         f"{curve['rmse_cv_mirrored']} 远小于正向 {curve['rmse_cv']}："
                         if rev_match else "：")
                      + "阀芯/阀座流向装反或行程反馈反向嫌疑",
            "rmse_cv": curve["rmse_cv"],
            "rmse_cv_mirrored": curve["rmse_cv_mirrored"],
        })
    return suspects


def run_flowcurve_analysis(db, fc_test_id, author="auto", new_adjustments=None):
    """执行单相液体流量曲线校核并保存新版本。"""
    test = db.get_flowcurve_test(fc_test_id)
    if test is None:
        raise KeyError(f"流量曲线校核测试 {fc_test_id} 不存在")
    payload = test["payload"]
    thr = dict(payload.get("thresholds") or test["thresholds"])
    new_adjustments = new_adjustments or {}

    # ---- 累计调整（上一版本 + 本次新增） ----
    existing = db.list_flowcurve_analyses(fc_test_id)
    cumulative = []
    if existing:
        prev = db.get_flowcurve_analysis(existing[-1]["id"])
        cumulative = list(prev["adjustments"])
    for mv in new_adjustments.get("plateau_moves", []):
        cumulative.append({"type": "plateau_move", "author": author, **mv})
    for d in new_adjustments.get("disabled_points", []):
        cumulative.append({"type": "disable_point", "author": author, **d})

    channel_exclusions = [a for a in cumulative if a["type"] == "exclusion"]
    manual_moves = [a for a in cumulative if a["type"] == "plateau_move"]
    manual_disabled = [a for a in cumulative if a["type"] == "disable_point"]

    evidence_gaps, notes = [], []
    fluid = payload["fluid"]
    valve_spec = payload["valve"]
    meter = payload["meter"]
    characteristic = valve_spec["characteristic"]
    rated_cv = float(valve_spec["rated_cv"])
    r_ratio = float(valve_spec.get("equal_percentage_r", 50.0))

    # ---- 物性缺项（全局证据缺口；逐点仍标 property_missing） ----
    if fluid.get("density_kg_m3") is None:
        evidence_gaps.append({"code": "property_missing",
                              "detail": "未提供工况密度（kg/m³），Cv 无法换算"})
    if fluid.get("vapor_pressure_kpa") is None:
        evidence_gaps.append({"code": "property_missing",
                              "detail": "未提供试验温度下饱和蒸气压，无法判别气蚀/闪蒸"})

    # ---- 屏蔽坏点（通道级，保留原始引用） ----
    filtered, exclusion_records = _apply_exclusions(
        payload["series"], channel_exclusions)

    # ---- 单位统一 ----
    rng = payload["range"]
    raw = {}
    vals, n, c = units.normalize_signal(
        filtered["position"]["points"], filtered["position"]["unit"],
        rng["min"], rng["max"], rng["unit"], "position")
    notes += n
    evidence_gaps += [{"code": "unit_conflict", "detail": f"单位冲突：{x}"} for x in c]
    raw["position"] = ([float(p[0]) for p in filtered["position"]["points"]], vals)
    for ch in ("upstream_pressure", "downstream_pressure"):
        vals, n, c = units.normalize_pressure(filtered[ch]["points"],
                                              filtered[ch]["unit"], ch)
        notes += n
        evidence_gaps += [{"code": "unit_conflict", "detail": f"单位冲突：{x}"} for x in c]
        raw[ch] = ([float(p[0]) for p in filtered[ch]["points"]], vals)
    tvals, n, c = norm_temperature(filtered["temperature"]["points"],
                                   filtered["temperature"]["unit"], "temperature")
    notes += n
    evidence_gaps += [{"code": "unit_conflict", "detail": f"单位冲突：{x}"} for x in c]
    raw["temperature"] = ([float(p[0]) for p in filtered["temperature"]["points"]], tvals)
    fvals, n, c = liquid.norm_flow_liquid(filtered["flow"]["points"],
                                          filtered["flow"]["unit"], "flow")
    notes += n
    evidence_gaps += [{"code": "unit_conflict", "detail": f"单位冲突：{x}"} for x in c]
    raw["flow"] = ([float(p[0]) for p in filtered["flow"]["points"]], fvals)

    # ---- 校准有效期 ----
    cal_until = _parse_time(payload.get("calibration_valid_until"))
    test_time = (_parse_time(payload.get("test_started_at"))
                 or _parse_time(test["submitted_at"]))
    if cal_until is None:
        evidence_gaps.append({"code": "calibration_missing",
                              "detail": "未提供校准有效期，无法确认流量计/变送器校准状态"})
    elif test_time and cal_until < test_time:
        evidence_gaps.append({
            "code": "calibration_expired",
            "detail": f"校准已于 {payload['calibration_valid_until']} 失效，"
                      "校核数据不得用于维修结论"})

    unit_conflict = any(g["code"] == "unit_conflict" for g in evidence_gaps)

    grid, aligned, align_meta = None, None, None
    if not unit_conflict:
        try:
            grid, aligned, align_meta = alignment.align(
                raw, channels=("position", "flow", "upstream_pressure",
                               "downstream_pressure", "temperature"))
        except ValueError as e:
            evidence_gaps.append({"code": "channel_no_overlap",
                                  "detail": f"各通道时间轴不重叠：{e}"})

    plateaus_out, boundary_log, points, curves = [], [], [], None
    curve_metrics, checks, suspects = None, [], []
    adopted_intervals = []

    if grid is not None:
        plateaus, boundary_log = platmod.delineate_plateaus(
            grid, aligned["position"], thr, manual_moves, manual_disabled)
        if not plateaus:
            evidence_gaps.append({"code": "no_plateau",
                                  "detail": "全程未识别到稳态平台（阀位持续变化），"
                                            "无法提取曲线测点"})

        pos_a = aligned["position"]
        flow_a = aligned["flow"]
        p1_a = aligned["upstream_pressure"]
        p2_a = aligned["downstream_pressure"]
        temp_a = aligned["temperature"]
        full_scale = float(liquid.FLOW_TO_M3H.get(
            (meter.get("unit") or "m3/h").strip().lower(), 1.0) * meter["full_scale"])
        cv_params = {
            "atmospheric_pressure_kpa": payload["atmospheric_pressure_kpa"],
            "density_kg_m3": fluid.get("density_kg_m3"),
            "vapor_pressure_kpa": fluid.get("vapor_pressure_kpa"),
            "critical_pressure_kpa": fluid.get("critical_pressure_kpa", 22120.0),
            "liquid_recovery_factor_fl": valve_spec.get(
                "liquid_recovery_factor_fl", 0.9),
            "density_ref_temp_c": fluid.get("density_ref_temp_c"),
            "temp_band_c": thr.get("temp_applicability_band_c"),
        }

        points = []
        for p in plateaus:
            s0, e0 = p["i_start"], p["i_end"]
            # 流量/压力/温度统计窗从平台起点后 0.5s 起，避开前一段爬升沿
            # 插值（位置已稳定但流量仍在爬升），位置统计仍取整段。
            sf = s0
            while sf < e0 and grid[sf] < grid[s0] + 0.5:
                sf += 1
            idx = list(range(s0, e0 + 1))
            fidx = list(range(sf, e0 + 1))
            rec = {
                "plateau_index": p["index"],
                "t_start_s": p["t_start"], "t_end_s": p["t_end"],
                "n_grid_points": len(idx),
                "position_pct": round(_median(pos_a[s0:e0 + 1]), 4),
                "position_peakpeak_pct": round(
                    max(pos_a[s0:e0 + 1]) - min(pos_a[s0:e0 + 1]), 4),
                "flow_m3h": round(_median(flow_a[sf:e0 + 1]), 6),
                "flow_cv_pct": None,
                "p1_gauge_kpa": round(_median(p1_a[sf:e0 + 1]), 3),
                "p2_gauge_kpa": round(_median(p2_a[sf:e0 + 1]), 3),
                "temp_c": round(_median(temp_a[sf:e0 + 1]), 3),
                "eval_window_start_s": round(grid[sf], 3),
                "order": p["order"],
                "disabled": p["disabled"],
                "disable_reason": p["disable_reason"],
                "exclusion_codes": [],
                "exclusion_reasons": [],
                "cv": None, "choked": None,
                "conversion": None,
                "raw_refs": {
                    "window_start": [r for r in (
                        _raw_ref("position", raw["position"][0], grid[sf],
                                 raw["position"][1]),
                        _raw_ref("flow", raw["flow"][0], grid[sf], raw["flow"][1]),
                        _raw_ref("upstream_pressure", raw["upstream_pressure"][0],
                                 grid[sf], raw["upstream_pressure"][1]),
                        _raw_ref("downstream_pressure", raw["downstream_pressure"][0],
                                 grid[sf], raw["downstream_pressure"][1])) if r],
                    "start": [r for r in (
                        _raw_ref("position", raw["position"][0], grid[s0],
                                 raw["position"][1]),
                        _raw_ref("flow", raw["flow"][0], grid[s0], raw["flow"][1])) if r],
                    "end": [r for r in (
                        _raw_ref("position", raw["position"][0], grid[e0],
                                 raw["position"][1]),
                        _raw_ref("flow", raw["flow"][0], grid[e0], raw["flow"][1])) if r],
                },
            }

            fseg = flow_a[sf:e0 + 1]
            fmean = sum(fseg) / len(fseg)
            if abs(fmean) > 1e-12:
                rec["flow_cv_pct"] = round(
                    statistics.pstdev(fseg) / abs(fmean) * 100.0, 3)

            # ---- 逐点排除判据（可多重，全部留痕） ----
            def add_reason(code):
                rec["exclusion_codes"].append(code)
                rec["exclusion_reasons"].append(EXCLUSION_NAMES[code])

            if rec["n_grid_points"] < 3 or grid[e0] - grid[s0] < thr["plateau_min_duration_s"]:
                add_reason("plateau_too_short")
            if p["disabled"]:
                add_reason("manual_disabled")
            if rec["position_peakpeak_pct"] > thr["plateau_drift_pct_max"] or (
                    rec["flow_cv_pct"] is not None
                    and rec["flow_cv_pct"] > thr["plateau_flow_cv_pct_max"]):
                add_reason("plateau_drift")
            fmed = rec["flow_m3h"]
            if fmed <= 0:
                add_reason("nonpositive_flow")
            elif fmed >= full_scale:
                # 读数持续顶到满量程轨：真流量达到或超过量程上限，读数不可信
                add_reason("flow_overrange")

            conv = liquid.cv_at(
                fmed, rec["p1_gauge_kpa"], rec["p2_gauge_kpa"], rec["temp_c"],
                cv_params)
            rec["conversion"] = {
                "formula": "Cv = Q/(N1·sqrt(ΔP/(ρ/ρ0)))，N1=0.865（Q:m³/h, ΔP:bar）",
                "q_m3h": conv["q_m3h"], "dp_kpa": conv["dp_kpa"],
                "p1_abs_kpa": conv["p1_abs_kpa"], "p2_abs_kpa": conv["p2_abs_kpa"],
                "ff": conv["ff"], "dp_choked_kpa": conv["dp_choked_kpa"],
                "choked": conv["choked"],
                "density_kg_m3": cv_params["density_kg_m3"],
                "vapor_pressure_kpa": cv_params["vapor_pressure_kpa"],
                "fl": cv_params["liquid_recovery_factor_fl"],
            }
            rec["choked"] = conv["choked"]
            if conv["exclusion"]:
                add_reason(conv["exclusion"])
            if conv["dp_kpa"] is not None and 0 < conv["dp_kpa"] < thr[
                    "min_differential_kpa"] and "cavitation" not in (
                    rec["exclusion_codes"]) and "flashing" not in (
                    rec["exclusion_codes"]):
                add_reason("insufficient_differential_pressure")

            rec["adopted"] = not rec["exclusion_codes"]
            if rec["adopted"]:
                rec["cv"] = conv["cv"]
            # 仅用于展示/导出的诊断 Cv：压差为正且有密度时总是计算，不参与拟合
            if (cv_params["density_kg_m3"] and conv["dp_kpa"]
                    and conv["dp_kpa"] > 0):
                rec["cv_display"] = liquid.liquid_cv(
                    fmed, conv["dp_kpa"], cv_params["density_kg_m3"])
            else:
                rec["cv_display"] = None
                adopted_intervals.append({
                    "name": f"plateau_{p['index']}",
                    "interval_s": [p["t_start"], p["t_end"]],
                    "method": "平台中位阀位/流量/压力逐点换算 Cv 并纳入特性拟合",
                    "position_pct": rec["position_pct"],
                })
            points.append(rec)

        plateaus_out = [
            {k: p[k] for k in ("index", "i_start", "i_end", "t_start", "t_end",
                               "n_points", "position_median_pct", "order",
                               "start_reason", "end_reason", "disabled",
                               "disable_reason")}
            for p in plateaus]

        # ---- 拟合与曲线指标（按阀位升序） ----
        adopted = sorted((p for p in points if p["adopted"]),
                         key=lambda p: p["position_pct"])
        if len(adopted) >= MIN_POINTS_TO_FIT:
            curve_metrics = _evaluate_curve(
                adopted, characteristic, rated_cv, r_ratio, thr)
            suspects = _diagnose_suspects(
                points, curve_metrics, characteristic, rated_cv, r_ratio, thr)

            # 有效调节比（实测 Cv 最大/最小）
            cv_vals = [p["cv"] for p in adopted if p["cv"]]
            pos_vals = [p["position_pct"] for p in adopted if p["cv"]]
            if cv_vals and min(cv_vals) > 0:
                curve_metrics["effective_turndown"] = round(
                    max(cv_vals) / min(cv_vals), 2)
                curve_metrics["turndown_position_range_pct"] = [
                    round(min(pos_vals), 2), round(max(pos_vals), 2)]
            if characteristic == "equal_percentage":
                curve_metrics["design_turndown_r"] = r_ratio

            # ---- 残差带检查 ----
            warn = thr["residual_warn_pct"]
            lo, hi = curve_metrics["residual_band_pct"]
            checks.append({
                "metric": "residual_band_pct",
                "value": curve_metrics["residual_band_pct"],
                "threshold_band": [-warn, warn],
                "pass": lo >= -warn and hi <= warn,
                "basis": f"各采用点实测 Cv 相对 {characteristic} 基准拟合曲线的"
                         "百分比残差带",
            })
            checks.append({
                "metric": "monotonic",
                "value": curve_metrics["monotonic"],
                "pass": curve_metrics["monotonic"],
                "basis": (f"Cv-阀位 Spearman 秩相关 ρ="
                          f"{curve_metrics['spearman_rho']}（≥"
                          f"{thr['monotonic_rho_min']:g} 且无逆序）"),
            })
            shape_ok = (curve_metrics["rmse_pct"] is not None
                        and curve_metrics["rmse_pct"] <= thr["residual_warn_pct"])
            checks.append({
                "metric": "capacity_factor",
                "value": curve_metrics["capacity_factor"],
                "pass": (not shape_ok) or (
                    thr["blockage_scale_max"] <= curve_metrics["capacity_factor"]
                    <= thr["erosion_scale_min"]),
                "threshold_band": [thr["blockage_scale_max"],
                                   thr["erosion_scale_min"]],
                "basis": "实测 Cv 相对铭牌特性曲线的过原点最小二乘容量系数 k"
                         "（堵塞下限/冲蚀上限）；正向形状不匹配（RMSE 超残差带）"
                         "时本项不适用，见嫌疑诊断",
            })
        else:
            evidence_gaps.append({
                "code": "insufficient_adopted_points",
                "detail": f"有效测点仅 {len(adopted)} 个（少于 "
                          f"{MIN_POINTS_TO_FIT} 个），无法拟合特性曲线，"
                          "见各测点排除缘由"})

        # ---- 时间序列曲线（供报告/导出） ----
        def plateau_id_at(i):
            for k, p in enumerate(plateaus):
                if p["i_start"] <= i <= p["i_end"]:
                    return k
            return None

        curves = {
            "t": [round(t, 3) for t in grid],
            "position_pct": [round(v, 4) for v in pos_a],
            "flow_m3h": [round(v, 4) for v in flow_a],
            "upstream_pressure_kpa": [round(v, 2) for v in p1_a],
            "downstream_pressure_kpa": [round(v, 2) for v in p2_a],
            "differential_pressure_kpa": [round(u - d, 2)
                                          for u, d in zip(p1_a, p2_a)],
            "temperature_c": [round(v, 3) for v in temp_a],
            "plateau_index": [plateau_id_at(i) for i in range(len(grid))],
        }

    # ---- 判定 ----
    blocking_gap_codes = {"unit_conflict", "calibration_missing",
                          "calibration_expired", "channel_no_overlap",
                          "no_plateau", "insufficient_adopted_points"}
    blocking = [g for g in evidence_gaps if g["code"] in blocking_gap_codes]
    if blocking or any(g["code"] == "property_missing" for g in evidence_gaps):
        verdict = "no_conclusion"
    elif suspects:
        verdict = "suspect"
    elif curve_metrics and all(c.get("pass") for c in checks):
        verdict = "matches_nameplate"
    else:
        verdict = "suspect"

    # ---- 判定依据 ----
    basis = []
    for c in checks:
        line = f"{c['metric']}："
        if c["metric"] == "monotonic":
            line += f"{'单调' if c['value'] else '非单调'}"
        else:
            line += f"实测 {c['value']}"
            if "threshold_band" in c:
                line += f"，允许带 {c['threshold_band']}"
        line += f"，判定 {'通过' if c.get('pass') else '不通过'}"
        if c.get("basis"):
            line += f"（依据：{c['basis']}）"
        basis.append(line)
    if curve_metrics and curve_metrics.get("effective_turndown") is not None:
        basis.append(
            f"有效调节比 {curve_metrics['effective_turndown']}:1"
            + (f"（铭牌等百分比设计 R={r_ratio:g}）"
               if characteristic == "equal_percentage" else "")
            + f"，覆盖阀位 {curve_metrics['turndown_position_range_pct']}%")
    excluded_n = sum(1 for p in points if not p["adopted"])
    basis.append(f"平台测点 {len(points)} 个，采用 {len(points) - excluded_n} 个，"
                 f"排除 {excluded_n} 个（缘由逐点列出）")

    result = {
        "test_id": fc_test_id,
        "valve_id": test["valve_id"],
        "flow_direction": payload["flow_direction"],
        "characteristic": characteristic,
        "valve": valve_spec,
        "fluid": fluid,
        "meter": meter,
        "atmospheric_pressure_kpa": payload["atmospheric_pressure_kpa"],
        "thresholds": thr,
        "conditions": test["conditions"],
        "unit_notes": notes,
        "verdict": verdict,
        "evidence_gaps": evidence_gaps,
        "plateaus": plateaus_out,
        "boundary_log": boundary_log,
        "points": points,
        "curve": curve_metrics,
        "checks": checks,
        "suspects": suspects,
        "adopted_intervals": adopted_intervals,
        "alignment": align_meta,
        "curves": curves,
        "exclusions": exclusion_records,
        "decision_basis": basis,
    }

    analysis_id, version = db.create_flowcurve_analysis(
        fc_test_id, author, cumulative, result)
    result["analysis_id"] = analysis_id
    result["version"] = version
    db._conn.execute("UPDATE flowcurve_analyses SET result_json=? WHERE id=?",
                     (json.dumps(result), analysis_id))
    db._conn.commit()
    return db.get_flowcurve_analysis(analysis_id)
