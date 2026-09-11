"""故障安全动作指标：响应延迟、90% 行程时间、最终位置、最大反弹、中途停滞、
压力衰减。所有指标基于对齐后的公共网格计算，采用区间与判定依据随结果返回。

安全方向：
- fail_close：安全位 0%，动作方向向下；
- fail_open：安全位 100%，动作方向向上；
- fail_in_place：不要求行程，只允许小幅漂移。
"""

# 开始移动判定：相对跳闸前基线的位移阈值（满量程 %）


def low_median(vals):
    """偶数样本取两个中值的较小者（避免斜坡过渡点污染跳闸前基线）。"""
    s = sorted(vals)
    if not s:
        return None
    return s[(len(s) - 1) // 2]

# 开始移动判定：相对跳闸前基线的位移阈值（满量程 %）
ONSET_MOVE_PCT = 0.5
# 错误方向累计位移达到该值才判错向（避开噪声）
WRONG_MOVE_PCT = 2.0
# 方向错误判定：错误方向位移超限且正确方向进度不足该比例
WRONG_PROGRESS_LIMIT = 0.5
# 窗末残压时段的回看长度
RESIDUAL_TAIL_S = 2.0


def baseline_value(ts, vals, trip_time, pre_s):
    """跳闸前 [trip-pre_s, trip) 区间的中位基线及采样覆盖率。

    返回 (baseline, coverage)；无采样返回 (None, 0.0)。
    """
    pre_pts = [(t, v) for t, v in zip(ts, vals)
               if trip_time - pre_s <= t < trip_time and v is not None]
    if not pre_pts:
        return None, 0.0
    dts = sorted(b - a for a, b in zip(ts, ts[1:]) if b - a > 0)
    dt_med = dts[(len(dts) - 1) // 2] if dts else pre_s
    expected = max(1, int(pre_s / dt_med) + 1)
    coverage = min(1.0, len(pre_pts) / expected)
    return low_median(v for _, v in pre_pts), coverage


def _sgn_and_target(fail_mode):
    if fail_mode == "fail_close":
        return -1, 0.0
    if fail_mode == "fail_open":
        return 1, 100.0
    return 0, None


def analyze_motion(grid, pos, prs, fail_mode, actuator_type, trip_time,
                   window_len_s, thr):
    """计算全部动作指标（观察窗为 [trip_time, trip_time+window_len_s]）。

    返回 dict：metrics、issues、evidence_gaps、adopted_intervals、checks。
    """
    sgn, target = _sgn_and_target(fail_mode)
    settle_band = thr["settle_band_pct"]
    metrics, issues, gaps, adopted, checks = [], [], [], [], []
    metrics = {}
    i_trip = min(range(len(grid)), key=lambda i: abs(grid[i] - trip_time))
    window_end = trip_time + window_len_s
    i_end = i_trip
    while i_end + 1 < len(grid) and grid[i_end + 1] <= window_end + 1e-9:
        i_end += 1
    step = grid[1] - grid[0] if len(grid) > 1 else 1.0

    # ---- 跳闸前位置基线 ----
    pre = [(t, v) for t, v in zip(grid[:i_trip], pos[:i_trip])
           if trip_time - thr["baseline_min_s"] <= t < trip_time]
    if not pre:
        gaps.append({"code": "insufficient_pre_trip_data",
                     "detail": f"跳闸前 {thr['baseline_min_s']}s 内无阀位采样，"
                               "无法建立跳闸前位置基线"})
        pos0 = pos[i_trip]
    else:
        pos0 = low_median(v for _, v in pre)
    metrics["pre_trip_position_pct"] = round(pos0, 3)
    adopted.append({
        "name": "pre_trip_baseline",
        "interval_s": [round(trip_time - thr["baseline_min_s"], 3), round(trip_time, 3)],
        "n_grid_samples": len(pre),
        "method": "区间内阀位中位数",
        "value": round(pos0, 3),
    })
    metrics["adopted_window_start_s"] = round(trip_time, 3)
    metrics["adopted_window_end_s"] = round(grid[i_end], 3)

    # 观察窗在数据结束前被截断（短于一个网格步长视为对齐取整）
    if grid[i_end] < window_end - step - 1e-6:
        gaps.append({
            "code": "window_truncated",
            "detail": f"请求观察窗至跳闸后 {window_len_s:.2f}s，"
                      f"但通道重叠数据在 {grid[i_end] - trip_time:.2f}s 结束，采用区间被截断",
        })

    result_extra = {}
    if fail_mode == "fail_in_place":
        motion_out = _analyze_fip(grid, pos, i_trip, i_end, pos0, thr,
                                  metrics, issues, gaps, adopted, checks)
    else:
        motion_out = _analyze_travel(
            grid, pos, i_trip, i_end, pos0, sgn, target, settle_band,
            thr, trip_time, metrics, issues, gaps, adopted, checks, step)

    # ---- 压力衰减（双作用执行器/FIP 只记录不判定） ----
    pressure_judged = actuator_type != "double_acting" and fail_mode != "fail_in_place"
    p = _pressure_decay(grid, prs, trip_time, i_trip, i_end, step, thr, judged=pressure_judged)
    metrics.update(p["metrics"])
    issues += p["issues"]
    gaps += p["evidence_gaps"]
    adopted += p["adopted_intervals"]
    checks += p["checks"]
    if not pressure_judged:
        result_extra["pressure_note"] = (
            "双作用/保位执行器不要求失气泄压，压力衰减仅记录不参与判定"
            if actuator_type == "double_acting"
            else "fail-in-place 配置要求保压保位，压力衰减仅记录不参与判定")

    out = {"metrics": metrics, "issues": issues, "evidence_gaps": gaps,
           "adopted_intervals": adopted, "checks": checks}
    out.update(result_extra)
    return out


def _analyze_travel(grid, pos, i_trip, i_end, pos0, sgn, target, settle_band,
                    thr, trip_time, metrics, issues, gaps, adopted, checks, step=0.1):
    window_vals = pos[i_trip:i_end + 1]
    wrong_excursion = max(sgn * (pos0 - v) for v in window_vals)

    def progress(v):
        return sgn * (v - pos0)

    max_progress = max(progress(v) for v in window_vals)

    # 开始移动：正确方向位移首次超阈
    i_move = next((i for i in range(i_trip, i_end + 1)
                   if progress(pos[i]) >= ONSET_MOVE_PCT), None)

    if wrong_excursion >= WRONG_MOVE_PCT and \
            max_progress < WRONG_PROGRESS_LIMIT * abs(target - pos0):
        want = "关闭（向 0%）" if target == 0.0 else "打开（向 100%）"
        gaps.append({
            "code": "wrong_direction",
            "detail": f"实际动作方向错误：要求{want}，跳闸后阀位反向移动 "
                      f"{wrong_excursion:.2f}%，正确方向最大进度仅 {max_progress:.2f}%，"
                      "安全动作未按配置方向发生",
        })
        checks.append({"metric": "direction",
                       "expected": "fail_close" if target == 0.0 else "fail_open",
                       "observed": "opposite", "pass": False,
                       "basis": "跳闸后阀位位移方向与 fail-open/fail-close 配置相反"})
        # 方向已错：行程/到位/反弹/停滞类指标无意义，不做阈值检查
        metrics.update({
            "response_delay_s": None,
            "t90_s": None,
            "final_position_pct": round(pos[i_end], 3),
            "settled": False,
            "settle_time_s": None,
            "stable_tail_s": 0.0,
            "max_rebound_pct": None,
            "mid_stall_count": 0,
            "mid_stalls": [],
        })
        adopted.append({"name": "wrong_direction",
                        "interval_s": [round(trip_time, 3), round(grid[i_end], 3)],
                        "method": "观察窗内阀位位移方向检查"})
        return

    metrics["response_delay_s"] = (round(grid[i_move] - trip_time, 3)
                                   if i_move is not None else None)
    adopted.append({
        "name": "response_delay",
        "interval_s": [round(trip_time, 3),
                       round(grid[i_move], 3) if i_move is not None else None],
        "method": f"有效跳闸沿到阀位向安全方向移动 ≥{ONSET_MOVE_PCT}% 的首点",
    })
    checks.append({
        "metric": "response_delay_s",
        "value": metrics["response_delay_s"],
        "threshold_max": thr["response_delay_s_max"],
        "pass": (metrics["response_delay_s"] is not None
                 and metrics["response_delay_s"] <= thr["response_delay_s_max"]),
        "basis": adopted[-1]["method"],
    })
    if i_move is None:
        issues.append({"kind": "no_movement",
                       "detail": "观察窗内阀位未向安全方向移动，阀门未执行安全动作",
                       "periods": []})

    # 行程进度（相对跳闸前位置到安全位的剩余行程）
    span = abs(target - pos0)

    def frac(v):
        return sgn * (v - pos0) / span if span > 1e-9 else 1.0

    # ---- 90% 行程时间（要求越过 90% 后保持，避免单点噪声提前触发） ----
    t90, i_90 = None, None
    if i_move is not None:
        hold_need = max(1, int(round(0.2 / step)))
        run = 0
        for i in range(i_move, i_end + 1):
            if frac(pos[i]) >= 0.9:
                run += 1
                if run >= hold_need:
                    i_90 = i - hold_need + 1
                    t90 = round(grid[i_90] - grid[i_move], 3)
                    break
            else:
                run = 0
    metrics["t90_s"] = t90
    adopted.append({
        "name": "t90",
        "interval_s": ([round(grid[i_move], 3), round(grid[i_90], 3)]
                       if t90 is not None else None),
        "method": "开始移动到行程进度 ≥90% 的首点（进度=相对跳闸前剩余行程）",
        "pre_trip_position_pct": round(pos0, 3),
        "target_pct": target,
    })
    checks.append({
        "metric": "t90_s", "value": t90, "threshold_max": thr["t90_s_max"],
        "pass": t90 is not None and t90 <= thr["t90_s_max"],
        "basis": adopted[-1]["method"],
    })

    # ---- 首次进入安全位带 ----
    i_band = next((i for i in range(i_trip, i_end + 1)
                   if abs(pos[i] - target) <= settle_band), None)

    # ---- 稳定性：以观察窗末端连续在带时长判定（中间允许出现反弹） ----
    settled, stable_tail_s, t_settle = False, 0.0, None
    k = i_end
    while k >= i_trip and abs(pos[k] - target) <= settle_band:
        k -= 1
    i_tail_band = k + 1                      # 窗末连续在带区间起点
    if i_tail_band <= i_end:
        stable_tail_s = grid[i_end] - grid[i_tail_band]
        if stable_tail_s >= thr["settle_dwell_s"]:
            settled = True
            # 进入稳定平台后保持满 dwell 的时刻
            t_settle = grid[i_tail_band] + thr["settle_dwell_s"]

    metrics["final_position_pct"] = round(pos[i_end], 3)
    metrics["settled"] = settled
    metrics["settle_time_s"] = round(t_settle - trip_time, 3) if settled else None
    metrics["stable_tail_s"] = round(stable_tail_s, 3)
    i_tail = min(i for i in range(i_trip, i_end + 1)
                 if grid[i] >= grid[i_end] - RESIDUAL_TAIL_S)
    adopted.append({
        "name": "final_position_settle",
        "interval_s": [round(grid[i_tail], 3), round(grid[i_end], 3)],
        "method": f"观察窗末端阀位；进入目标 ±{settle_band}% 后连续保持 "
                  f"≥{thr['settle_dwell_s']}s 且窗末仍在带内判为稳定",
    })
    checks.append({"metric": "final_position_pct", "value": metrics["final_position_pct"],
                   "target_pct": target, "settle_band_pct": settle_band,
                   "pass": abs(metrics["final_position_pct"] - target) <= settle_band})
    checks.append({"metric": "settled_at_window_end", "value": settled,
                   "stable_tail_s": metrics["stable_tail_s"],
                   "required_dwell_s": thr["settle_dwell_s"], "pass": settled,
                   "basis": "观察窗结束时阀位须仍在安全位带内且保持足够时长"})
    if not settled:
        gaps.append({
            "code": "not_settled_at_window_end",
            "detail": f"观察窗结束（跳闸后 {grid[i_end] - trip_time:.2f}s）阀位未稳定在"
                      f"安全位 {target:.0f}%±{settle_band}%：窗末位置 "
                      f"{pos[i_end]:.2f}%，末端连续在带 {stable_tail_s:.2f}s "
                      f"（要求 ≥{thr['settle_dwell_s']}s）",
        })

    # ---- 最大反弹 ----
    rebounds = _rebound_episodes(
        grid, pos, i_band if i_band is not None else i_end + 1,
        i_end, target, sgn, settle_band)
    metrics["max_rebound_pct"] = round(
        max((r["magnitude_pct"] for r in rebounds), default=0.0), 3)
    checks.append({"metric": "max_rebound_pct", "value": metrics["max_rebound_pct"],
                   "threshold_max": thr["rebound_pct_max"],
                   "pass": metrics["max_rebound_pct"] <= thr["rebound_pct_max"],
                   "basis": "首次进入安全位带后反向离开再返回的最大幅度"})
    for r in rebounds:
        issues.append({"kind": "rebound",
                       "detail": f"安全位反弹 {r['magnitude_pct']:.2f}%（"
                                 f"{r['t_start']}–{r['t_end']}s）",
                       "value": r["magnitude_pct"],
                       "threshold": thr["rebound_pct_max"],
                       "periods": [[r["t_start"], r["t_end"]]]})

    # ---- 中途停滞 ----
    stalls = _mid_stalls(grid, pos, i_move, i_band, thr)
    metrics["mid_stall_count"] = len(stalls)
    metrics["mid_stalls"] = stalls
    for st in stalls:
        issues.append({"kind": "mid_travel_stall",
                       "detail": f"中途停滞 {st['duration_s']}s（{st['t_start']}–"
                                 f"{st['t_end']}s，期间位移 {st['move_pct']:.2f}%）",
                       "value": st["duration_s"], "threshold": thr["stall_min_s"],
                       "periods": [[st["t_start"], st["t_end"]]]})


def _rebound_episodes(grid, pos, i_band, i_end, target, sgn, settle_band):
    """进入安全位带后离开位带再返回的反弹事件。"""
    out = []
    i = i_band
    while i <= i_end:
        if abs(pos[i] - target) > settle_band:
            j, worst_i = i, i
            while j + 1 <= i_end:
                j += 1
                if sgn * (target - pos[j]) > sgn * (target - pos[worst_i]):
                    worst_i = j
                if abs(pos[j] - target) <= settle_band:
                    break
            out.append({"t_start": round(grid[i], 3), "t_end": round(grid[j], 3),
                        "t_peak": round(grid[worst_i], 3),
                        "peak_position_pct": round(pos[worst_i], 3),
                        "magnitude_pct": round(abs(pos[worst_i] - target), 3)})
            i = j + 1
        else:
            i += 1
    return out


def _mid_stalls(grid, pos, i_move, i_band, thr):
    """移动开始后到进入安全位带前：位移 < stall_move_pct 持续 ≥ stall_min_s。"""
    if i_move is None:
        return []
    stop = i_band if i_band is not None else len(grid) - 1
    out = []
    i = i_move
    while i < stop:
        j, anchor = i, pos[i]
        while j + 1 <= stop and abs(pos[j + 1] - anchor) < thr["stall_move_pct"]:
            j += 1
        dur = grid[j] - grid[i]
        if dur >= thr["stall_min_s"] and j - i >= 2:
            out.append({"t_start": round(grid[i], 3), "t_end": round(grid[j], 3),
                        "duration_s": round(dur, 3),
                        "move_pct": round(abs(pos[j] - pos[i]), 3),
                        "progress_span_pct": round(max(pos[i:j + 1]) - min(pos[i:j + 1]), 3)})
            i = j + 1
        else:
            i = max(i + 1, j)
    return out


def _pressure_decay(grid, prs, trip_time, i_trip, i_end, step, thr, judged):
    """执行器压力衰减：跳闸前基线 → 窗末残压、衰减比例、低于残压阈值时间。"""
    issues, gaps, adopted, checks = [], [], [], []
    pre = [v for i, v in enumerate(prs[:i_trip])
           if trip_time - thr["baseline_min_s"] <= grid[i] < trip_time]
    p0 = low_median(pre) if pre else None
    p_end = prs[i_end]
    win = prs[i_trip:i_end + 1]
    metrics = {
        "pressure_pre_kpa": round(p0, 2) if p0 is not None else None,
        "pressure_end_kpa": round(p_end, 2),
        "pressure_min_kpa": round(min(win), 2),
    }
    adopted.append({"name": "pressure_decay",
                    "interval_s": [round(trip_time, 3), round(grid[i_end], 3)],
                    "method": "跳闸前压力中位数与观察窗末端/最低执行器压力比较"})
    if p0 is None or p0 <= 0:
        gaps.append({"code": "insufficient_pre_trip_data",
                     "detail": "跳闸前无有效执行器压力采样，无法计算压力衰减"})
        metrics.update({"pressure_decay_pct": None, "pressure_below_residual_s": None})
        return {"metrics": metrics, "issues": issues, "evidence_gaps": gaps,
                "adopted_intervals": adopted, "checks": checks}
    decay = (p0 - p_end) / p0 * 100.0
    metrics["pressure_decay_pct"] = round(decay, 2)
    t_below = next((grid[i] - trip_time for i in range(i_trip, i_end + 1)
                    if prs[i] <= thr["pressure_residual_kpa_max"]), None)
    metrics["pressure_below_residual_s"] = round(t_below, 3) if t_below is not None else None
    i_tail = min(i for i in range(i_trip, i_end + 1)
                 if grid[i] >= grid[i_end] - RESIDUAL_TAIL_S)
    if judged:
        checks.append({"metric": "pressure_residual_kpa", "value": metrics["pressure_end_kpa"],
                       "threshold_max": thr["pressure_residual_kpa_max"],
                       "pass": p_end <= thr["pressure_residual_kpa_max"],
                       "basis": "观察窗末端执行器残压"})
        checks.append({"metric": "pressure_decay_pct", "value": metrics["pressure_decay_pct"],
                       "threshold_min": thr["pressure_decay_pct_min"],
                       "pass": decay >= thr["pressure_decay_pct_min"],
                       "basis": "窗末残压相对跳闸前压力的衰减比例"})
        if p_end > thr["pressure_residual_kpa_max"]:
            issues.append({"kind": "pressure_residual_high",
                           "detail": f"观察窗末端执行器残压 {p_end:.1f} kPa 高于 "
                                     f"{thr['pressure_residual_kpa_max']:.0f} kPa",
                           "value": round(p_end, 2),
                           "threshold": thr["pressure_residual_kpa_max"],
                           "periods": [[round(grid[i_tail], 3), round(grid[i_end], 3)]]})
        if decay < thr["pressure_decay_pct_min"]:
            issues.append({"kind": "pressure_decay_slow",
                           "detail": f"压力仅衰减 {decay:.1f}%，低于要求的 "
                                     f"{thr['pressure_decay_pct_min']:.0f}%",
                           "value": round(decay, 2),
                           "threshold": thr["pressure_decay_pct_min"], "periods": []})
    return {"metrics": metrics, "issues": issues, "evidence_gaps": gaps,
            "adopted_intervals": adopted, "checks": checks}


def _analyze_fip(grid, pos, i_trip, i_end, pos0, thr, metrics, issues, gaps,
                 adopted, checks):
    """fail-in-place：不允许动作，窗末漂移须在 fip_drift 带内。"""
    window = pos[i_trip:i_end + 1]
    drift_hi = max(abs(v - pos0) for v in window)
    end_drift = abs(pos[i_end] - pos0)
    metrics.update({
        "max_drift_pct": round(drift_hi, 3),
        "final_position_pct": round(pos[i_end], 3),
        "response_delay_s": None,
        "t90_s": None,
        "max_rebound_pct": None,
        "mid_stall_count": 0,
        "mid_stalls": [],
        "settled": end_drift <= thr["fip_drift_pct_max"],
        "stable_tail_s": round(grid[i_end] - grid[i_trip], 3),
    })
    adopted.append({"name": "fip_drift",
                    "interval_s": [round(grid[i_trip], 3), round(grid[i_end], 3)],
                    "method": "跳闸后阀位相对跳闸前位置的最大偏移"})
    checks.append({"metric": "fip_drift_pct", "value": metrics["max_drift_pct"],
                   "threshold_max": thr["fip_drift_pct_max"],
                   "pass": drift_hi <= thr["fip_drift_pct_max"],
                   "basis": "fail-in-place 阀门跳闸后须保持原位"})
    checks.append({"metric": "settled_at_window_end", "value": metrics["settled"],
                   "pass": metrics["settled"],
                   "basis": "观察窗末端阀位仍在保持带内"})
    if drift_hi > thr["fip_drift_pct_max"]:
        issues.append({"kind": "fip_drift",
                       "detail": f"fail-in-place 阀门跳闸后漂移 {drift_hi:.2f}%，"
                                 f"超过保持带 ±{thr['fip_drift_pct_max']}%",
                       "value": round(drift_hi, 3),
                       "threshold": thr["fip_drift_pct_max"], "periods": []})
