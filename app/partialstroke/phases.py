"""部分行程测试的阶段对齐与指标计算。

在公共网格上按**许可、动作、保持、返回**四个阶段对齐六路异频信号：

- 许可（permit）：许可有效 → 指令阶跃；
- 动作（action）：指令阶跃 → 阀位到达目标行程；
- 保持（hold）：到达目标 → 指令复位；
- 返回（return）：指令复位 → 阀位回到起点判定带并稳定。

指标：起步延迟、有效行程、平均速度、超调、保持漂移、返回时间、残余偏差。
人工移动阶段边界（overrides）后按同一套公式重算，边界来源逐条标注
（auto/manual），采用区间随结果返回，与 JSON 导出、打印报告共用。
"""

# 阶段边界键：permit_start/cmd/target/return_cmd/settled
# （move 为数据驱动的起步点，不是可人工移动的阶段边界）
BOUNDARY_NAMES = {
    "permit_start": "许可生效",
    "cmd": "指令阶跃",
    "move": "阀位起步",
    "target": "到达目标",
    "return_cmd": "指令复位",
    "settled": "回位稳定",
}

PHASE_NAMES = {"permit": "许可", "action": "动作", "hold": "保持", "return": "返回"}

# 人工移动 (phase, boundary) → 边界键
MOVE_KEY = {
    ("permit", "start"): "permit_start",
    ("permit", "end"): "cmd",
    ("action", "start"): "cmd",
    ("action", "end"): "target",
    ("hold", "start"): "target",
    ("hold", "end"): "return_cmd",
    ("return", "start"): "return_cmd",
    ("return", "end"): "settled",
}


def low_median(vals):
    """偶数样本取两个中值的较小者（避免过渡点污染基线）。"""
    s = sorted(vals)
    return s[(len(s) - 1) // 2] if s else None


def _idx_at(grid, t):
    """grid 上距 t 最近的序号。"""
    return min(range(len(grid)), key=lambda i: abs(grid[i] - t))


def _first_idx(grid, pred, i0=0):
    for i in range(max(0, i0), len(grid)):
        if pred(i):
            return i
    return None


def _sustained(grid, vals, i, thresh, sustain_s, sign=1):
    """从 i 起 vals 在 sustain_s 内持续满足 sign*vals >= thresh。"""
    if i is None:
        return False
    t_end = grid[i] + sustain_s
    j = i
    while j < len(grid) and grid[j] <= t_end + 1e-9:
        if sign * vals[j] < thresh:
            return False
        j += 1
    return True


def compute_phases(grid, al, spec, thr, permit_start_auto, overrides=None):
    """对齐四阶段并计算全部指标。

    grid: 公共时间轴；al: {channel: 对齐后的值列}（command/position 已为 %）。
    spec: 冻结规格 dict（start_pct/direction/target_travel_pct/
    max_disturbance_pct/time_limit_s）；thr: 阈值 dict。
    permit_start_auto: 许可生效时刻（接点序列定位，可能为 None）。
    overrides: {边界键: {"t_s": float, ...}} 人工边界（可只覆盖部分）。

    返回 dict：boundaries/phases/metrics/checks/issues/evidence_gaps/
    adopted_intervals/baselines。人工边界破坏时序因果（许可→指令→目标→
    复位→回位）时抛 ValueError（由路由转 422）。
    """
    overrides = overrides or {}
    sign = 1.0 if spec["direction"] == "open" else -1.0
    n = len(grid)
    gaps, issues, checks, adopted = [], [], [], []

    # ---- 基线（指令阶跃前） ----
    base_hi = grid[0] + thr["baseline_min_s"]
    cmd_base = low_median([v for t, v in zip(grid, al["command"]) if t <= base_hi + 1e-9])
    pos0 = low_median([v for t, v in zip(grid, al["position"]) if t <= base_hi + 1e-9])
    baselines = {"command_pct": round(cmd_base, 4) if cmd_base is not None else None,
                 "position_pct": round(pos0, 4) if pos0 is not None else None,
                 "interval_s": [round(grid[0], 3), round(min(base_hi, grid[-1]), 3)]}

    boundaries = {}

    def _set(key, auto_t):
        if key in overrides:
            boundaries[key] = {"t_s": round(float(overrides[key]["t_s"]), 3),
                               "source": "manual"}
        else:
            boundaries[key] = ({"t_s": round(auto_t, 3), "source": "auto"}
                               if auto_t is not None else
                               {"t_s": None, "source": "auto"})

    _set("permit_start", permit_start_auto)

    if pos0 is None or cmd_base is None:
        gaps.append({"code": "insufficient_baseline",
                     "detail": "基线区间内无指令/阀位采样，无法建立测试前基线"})
        return _result(boundaries, gaps, issues, checks, adopted, baselines)

    # ---- 起点核对：实际起点须与冻结起点一致 ----
    if abs(pos0 - spec["start_pct"]) > thr["settle_band_pct"] + 1e-9:
        gaps.append({
            "code": "start_position_mismatch",
            "detail": f"实际起点 {pos0:.2f}% 与冻结起点 {spec['start_pct']:g}% 偏差超过 "
                      f"±{thr['settle_band_pct']:g}%，测试未按冻结规格执行",
        })

    # ---- 指令阶跃（动作阶段起点） ----
    i_cmd = _first_idx(grid, lambda i: sign * (al["command"][i] - cmd_base)
                       >= thr["cmd_step_detect_pct"])
    _set("cmd", grid[i_cmd] if i_cmd is not None else None)
    t_cmd = boundaries["cmd"]["t_s"]
    if t_cmd is None:
        gaps.append({"code": "no_command_step",
                     "detail": f"记录内指令相对基线 {cmd_base:.2f}% 未出现 "
                               f"≥{thr['cmd_step_detect_pct']:g}% 的阶跃，"
                               "测试指令未发出或未被记录"})
        return _result(boundaries, gaps, issues, checks, adopted, baselines)
    i_cmd = _idx_at(grid, t_cmd)

    # ---- 阀位起步（数据驱动，非阶段边界） ----
    exc = [sign * (v - pos0) for v in al["position"]]
    i_move = _first_idx(
        grid, lambda i: i >= i_cmd and exc[i] >= thr["move_detect_pct"]
        and _sustained(grid, exc, i, thr["move_detect_pct"],
                       thr["move_sustained_s"]), i_cmd)
    _set("move", grid[i_move] if i_move is not None else None)
    t_move = boundaries["move"]["t_s"]

    # ---- 行程极值与行程越限 ----
    exc_after = exc[i_cmd:]
    max_exc = max(exc_after)
    i_maxexc = i_cmd + exc_after.index(max_exc)
    if max_exc > spec["max_disturbance_pct"] + 1e-9:
        i_cross = _first_idx(grid, lambda i: i >= i_cmd
                             and exc[i] > spec["max_disturbance_pct"], i_cmd)
        gaps.append({
            "code": "travel_over_limit",
            "detail": f"阀位相对起点最大偏移 {max_exc:.2f}% 越过最大扰动限值 "
                      f"{spec['max_disturbance_pct']:g}%（{grid[i_cross]:.3f}s 越限，"
                      f"{grid[i_maxexc]:.3f}s 达极值），工艺扰动超出允许范围",
            "raw_intervals": [{"channel": "position",
                               "t_start": round(grid[i_cross], 3),
                               "t_end": round(grid[i_maxexc], 3)}],
        })

    # ---- 到达目标（动作阶段终点/保持阶段起点） ----
    i_target = _first_idx(grid, lambda i: i >= i_cmd
                          and exc[i] >= spec["target_travel_pct"], i_cmd)
    _set("target", grid[i_target] if i_target is not None else None)
    t_target = boundaries["target"]["t_s"]
    if t_target is not None:
        i_target = _idx_at(grid, t_target)

    # ---- 指令复位（保持阶段终点/返回阶段起点） ----
    i_search = (i_target if t_target is not None else i_maxexc) + 1
    i_ret = _first_idx(grid, lambda i: i >= i_search
                       and sign * (al["command"][i] - cmd_base)
                       < thr["cmd_step_detect_pct"], i_search)
    _set("return_cmd", grid[i_ret] if i_ret is not None else None)
    t_ret = boundaries["return_cmd"]["t_s"]
    if t_ret is not None:
        i_ret = _idx_at(grid, t_ret)

    # ---- 回位稳定（返回阶段终点） ----
    i_settled = None
    if t_ret is not None:
        band = thr["settle_band_pct"]
        dwell = thr["settle_dwell_s"]
        i_cand = _first_idx(grid, lambda i: i >= i_ret
                            and abs(al["position"][i] - pos0) <= band, i_ret)
        while i_cand is not None:
            t_end = grid[i_cand] + dwell
            j = i_cand
            ok = True
            while j < n and grid[j] <= t_end + 1e-9:
                if abs(al["position"][j] - pos0) > band:
                    ok = False
                    break
                j += 1
            if ok:
                i_settled = i_cand
                break
            i_cand = _first_idx(grid, lambda i: i > i_cand
                                and abs(al["position"][i] - pos0) <= band,
                                i_cand + 1)
    _set("settled", grid[i_settled] if i_settled is not None else None)
    t_settled = boundaries["settled"]["t_s"]
    if t_settled is not None:
        i_settled = _idx_at(grid, t_settled)

    # ---- 人工边界时序因果校验 ----
    order = ["permit_start", "cmd", "target", "return_cmd", "settled"]
    seq = [(k, boundaries[k]["t_s"]) for k in order]
    seq = [(k, t) for k, t in seq if t is not None]
    for (k1, t1), (k2, t2) in zip(seq, seq[1:]):
        if t2 < t1 - 1e-9:
            raise ValueError(
                f"阶段边界时序矛盾：{BOUNDARY_NAMES[k1]} {t1}s 晚于 "
                f"{BOUNDARY_NAMES[k2]} {t2}s，请检查人工移动的边界")

    # ---- 指标 ----
    metrics = {}
    i_end_eval = i_settled if t_settled is not None else n - 1
    eff_travel = max(exc[i_cmd:i_end_eval + 1]) if i_end_eval >= i_cmd else 0.0
    metrics["start_position_pct"] = round(pos0, 3)
    metrics["command_step_pct"] = round(
        max(sign * (v - cmd_base) for v in al["command"][i_cmd:]), 3)
    metrics["breakaway_delay_s"] = (round(t_move - t_cmd, 3)
                                    if t_move is not None else None)
    metrics["effective_travel_pct"] = round(eff_travel, 3)
    metrics["max_excursion_pct"] = round(max_exc, 3)
    metrics["target_reached"] = t_target is not None
    if t_move is not None and t_target is not None and t_target > t_move:
        metrics["avg_speed_pct_s"] = round(
            spec["target_travel_pct"] / (t_target - t_move), 3)
    else:
        metrics["avg_speed_pct_s"] = None
    i_os_end = i_ret if t_ret is not None else i_end_eval
    overshoot = max(0.0, max(exc[i_cmd:i_os_end + 1]) - spec["target_travel_pct"]) \
        if i_os_end >= i_cmd else 0.0
    metrics["overshoot_pct"] = round(overshoot, 3)
    if t_target is not None and t_ret is not None and i_ret > i_target:
        hold_vals = al["position"][i_target:i_ret + 1]
        metrics["hold_drift_pct"] = round(max(hold_vals) - min(hold_vals), 3)
        metrics["hold_duration_s"] = round(t_ret - t_target, 3)
    else:
        metrics["hold_drift_pct"] = None
        metrics["hold_duration_s"] = None
    metrics["return_time_s"] = (round(t_settled - t_ret, 3)
                                if t_settled is not None and t_ret is not None else None)
    tail_lo_t = (t_settled if t_settled is not None
                 else max(grid[0], grid[-1] - thr["settle_dwell_s"]))
    tail = [v for t, v in zip(grid, al["position"]) if t >= tail_lo_t - 1e-9]
    pos_tail = low_median(tail) if tail else al["position"][-1]
    metrics["final_position_pct"] = round(pos_tail, 3)
    metrics["residual_pct"] = round(abs(pos_tail - pos0), 3)
    metrics["returned"] = t_settled is not None
    metrics["total_time_s"] = round((t_settled if t_settled is not None
                                     else grid[-1]) - t_cmd, 3)

    # ---- 异常发现（非阻断；阻断类在 evidence_gaps） ----
    if t_move is None:
        issues.append({"kind": "no_movement",
                       "detail": f"指令阶跃后阀位未起步（持续偏离基线不足 "
                                 f"{thr['move_detect_pct']:g}%），阀杆疑似卡死",
                       "periods": [[round(t_cmd, 3), round(grid[-1], 3)]]})
    if t_target is None and t_move is not None:
        issues.append({"kind": "target_not_reached",
                       "detail": f"有效行程 {eff_travel:.2f}% 未到达目标行程 "
                                 f"{spec['target_travel_pct']:g}%",
                       "periods": [[round(t_cmd, 3), round(grid[i_maxexc], 3)]]})
    if t_ret is None:
        issues.append({"kind": "return_not_observed",
                       "detail": "记录内指令未复位到基线附近，返回阶段未开始",
                       "periods": []})
    elif t_settled is None:
        issues.append({"kind": "not_settled",
                       "detail": f"指令复位后阀位未回到起点 ±{thr['settle_band_pct']:g}% "
                                 f"判定带并稳定 {thr['settle_dwell_s']:g}s，回不到原位",
                       "periods": [[round(t_ret, 3), round(grid[-1], 3)]]})

    # ---- 阈值检查 ----
    def _check(metric, value, op, limit, basis):
        if value is None:
            ok = False
        elif op == "max":
            ok = value <= limit + 1e-9
        else:
            ok = value >= limit - 1e-9
        entry = {"metric": metric, "value": value, "pass": ok, "basis": basis}
        entry["threshold_max" if op == "max" else "threshold_min"] = limit
        checks.append(entry)
        return entry

    _check("breakaway_delay_s", metrics["breakaway_delay_s"], "max",
           thr["breakaway_delay_s_max"],
           f"指令阶跃到阀位持续偏离基线 ≥{thr['move_detect_pct']:g}% 的延迟")
    _check("effective_travel_pct", metrics["effective_travel_pct"], "min",
           round(spec["target_travel_pct"] - thr["travel_reach_tol_pct"], 6),
           "动作/保持阶段阀位相对起点基线的最大位移（目标行程−容差）")
    _check("overshoot_pct", metrics["overshoot_pct"], "max",
           thr["overshoot_pct_max"], "指令复位前越过目标行程的最大位移")
    if metrics["avg_speed_pct_s"] is not None:
        _check("avg_speed_pct_s", metrics["avg_speed_pct_s"], "min",
               thr["speed_min_pct_s"], "目标行程 /（起步到到达目标的时间）")
    if metrics["hold_drift_pct"] is not None:
        _check("hold_drift_pct", metrics["hold_drift_pct"], "max",
               thr["hold_drift_pct_max"], "保持段阀位峰峰漂移")
    if t_ret is not None:
        _check("return_time_s", metrics["return_time_s"], "max",
               thr["return_time_s_max"],
               f"指令复位到阀位回到起点 ±{thr['settle_band_pct']:g}% 并稳定 "
               f"{thr['settle_dwell_s']:g}s 的时间")
    _check("residual_pct", metrics["residual_pct"], "max",
           thr["residual_pct_max"], "回位后阀位与起点基线之差（回不到原位即超限）")
    _check("total_time_s", metrics["total_time_s"], "max",
           spec["time_limit_s"], "指令阶跃到回位稳定的总时长（冻结时限）")

    # ---- 采用区间（与 JSON 导出、打印报告共用） ----
    adopted.append({"name": "baseline", "interval_s": baselines["interval_s"],
                    "method": "指令阶跃前基线区间低中位数",
                    "value": baselines["position_pct"]})
    t_permit = boundaries["permit_start"]["t_s"]
    if t_permit is not None:
        adopted.append({"name": "permit_phase",
                        "interval_s": [round(t_permit, 3), round(t_cmd, 3)],
                        "method": "许可有效到指令阶跃"})
    adopted.append({"name": "action_phase",
                    "interval_s": [round(t_cmd, 3),
                                   round(t_target, 3) if t_target is not None else None],
                    "method": "指令阶跃到阀位到达目标行程"})
    if t_target is not None:
        adopted.append({"name": "hold_phase",
                        "interval_s": [round(t_target, 3),
                                       round(t_ret, 3) if t_ret is not None else None],
                        "method": "到达目标到指令复位"})
    if t_ret is not None:
        adopted.append({"name": "return_phase",
                        "interval_s": [round(t_ret, 3),
                                       round(t_settled, 3) if t_settled is not None else None],
                        "method": "指令复位到回位稳定"})
    adopted.append({"name": "residual_tail",
                    "interval_s": [round(tail_lo_t, 3), round(grid[-1], 3)],
                    "method": "回位稳定后尾部低中位数（残余偏差）",
                    "value": metrics["final_position_pct"]})

    return {"boundaries": boundaries, "metrics": metrics, "checks": checks,
            "issues": issues, "evidence_gaps": gaps, "adopted_intervals": adopted,
            "baselines": baselines}


def _result(boundaries, gaps, issues, checks, adopted, baselines):
    return {"boundaries": boundaries, "metrics": {}, "checks": checks,
            "issues": issues, "evidence_gaps": gaps, "adopted_intervals": adopted,
            "baselines": baselines}


def phases_from_boundaries(boundaries):
    """由边界时刻拼装四阶段区间（报告/导出共用）。"""
    b = {k: v["t_s"] for k, v in boundaries.items()}
    src = {k: v["source"] for k, v in boundaries.items()}

    def _phase(name, t0, t1):
        return {"name": name, "label": PHASE_NAMES[name],
                "start_s": t0, "end_s": t1,
                "start_source": src.get(_START_KEY[name]),
                "end_source": src.get(_END_KEY[name])}

    return [
        _phase("permit", b.get("permit_start"), b.get("cmd")),
        _phase("action", b.get("cmd"), b.get("target")),
        _phase("hold", b.get("target"), b.get("return_cmd")),
        _phase("return", b.get("return_cmd"), b.get("settled")),
    ]


_START_KEY = {"permit": "permit_start", "action": "cmd",
              "hold": "target", "return": "return_cmd"}
_END_KEY = {"permit": "cmd", "action": "target",
            "hold": "return_cmd", "return": "settled"}
