"""把连续对齐时序归为有序稳态平台（每个平台 = 曲线上一个测点）。

判定：居中窗口内阀位速度持续低于阈值（plateau_slope_pct_s）为候选稳态；
短于最短持续时间的稳态抖动并入相邻爬升段；被短爬升（≤ plateau_merge_gap_s）
隔开且中位阀位接近（≤ plateau_merge_position_pct）的相邻平台合并为同一
平台。平台按时间排序（0 起序号），同时给出与时间同向的**阀位序**标签
（ascending/descending，供反装行程识别）。

技术人员可移动平台起止边界或停用整个平台测点，每次操作记录理由并派生
新版本；共享语义与阀座模块一致（旧版只读）。
"""


def _window_velocity(grid, pos, win_s=1.0):
    """居中窗口阀位速度 %/s。

    窗口至少向两侧各取一个采样间隔（网格步长），保证 1 Hz 等粗采样下
    窗口仍含真实位移信息，而不会退化为单点零速度（把斜坡误判为稳态）。
    """
    n = len(grid)
    if n < 2:
        return [0.0] * n
    dt_step = grid[1] - grid[0]
    half = max(win_s / 2.0, dt_step)
    vel = [0.0] * n
    for i in range(n):
        j, k = i, i
        while j + 1 < n and grid[j + 1] - grid[i] <= half:
            j += 1
        while k - 1 >= 0 and grid[i] - grid[k - 1] <= half:
            k -= 1
        dt = grid[j] - grid[k]
        vel[i] = (pos[j] - pos[k]) / dt if dt > 0 else 0.0
    return vel


def delineate_plateaus(grid, position, thresholds, manual_moves=None,
                       manual_disabled=None):
    """圈定稳态平台。

    返回 (plateaus, boundary_log)。plateaus 为按时间排序的平台列表：
        {index, i_start, i_end, t_start, t_end, n_points, position_median_pct,
         start_reason, end_reason, order (ascending/descending), disabled,
         disable_reason}
    manual_moves: [{plateau_index, boundary:start/end, new_time, reason}]
    manual_disabled: [{plateau_index, reason}]（累计；同序号多次停用取最后理由）
    """
    slope_lim = thresholds["plateau_slope_pct_s"]
    min_dur = thresholds["plateau_min_duration_s"]
    merge_gap = thresholds["plateau_merge_gap_s"]
    merge_pos = thresholds["plateau_merge_position_pct"]
    band = thresholds["plateau_band_pct"]
    n = len(grid)

    vel = _window_velocity(grid, position, win_s=1.0)
    steady = [abs(v) <= slope_lim for v in vel]

    # ---- 连续稳态 run ----
    runs = []
    s = 0
    for i in range(1, n):
        if steady[i] != steady[s]:
            runs.append((steady[s], s, i - 1))
            s = i
    runs.append((steady[s], s, n - 1))

    # 短稳态 run（< 最短持续时间）并入爬升
    runs = [(is_st, s0, e0) for (is_st, s0, e0) in runs
            if not is_st or grid[e0] - grid[s0] >= min_dur]

    # 合并被相邻平台吸收的短爬升（合并同高相邻平台）
    def med(s0, e0):
        vals = sorted(position[s0:e0 + 1])
        return vals[len(vals) // 2]

    changed = True
    while changed:
        changed = False
        plats = [(s0, e0) for (is_st, s0, e0) in runs if is_st]
        for k in range(len(plats) - 1):
            s1, e1 = plats[k]
            s2, e2 = plats[k + 1]
            gap_dur = grid[s2] - grid[e1]
            if gap_dur <= merge_gap and abs(med(s1, e1) - med(s2, e2)) <= merge_pos:
                # 合并为 [s1, e2]（中间爬升纳入平台）
                new_runs = []
                for is_st, s0, e0 in runs:
                    if s0 == s1:
                        new_runs.append((True, s1, e2))
                    elif s1 < s0 <= e2:
                        continue
                    else:
                        new_runs.append((is_st, s0, e0))
                runs = new_runs
                changed = True
                break

    plat_runs = [(s0, e0) for (is_st, s0, e0) in runs if is_st]

    plateaus = []
    for k, (s0, e0) in enumerate(plat_runs):
        pos_med = med(s0, e0)
        if k + 1 < len(plat_runs):
            order = ("ascending" if med(*plat_runs[k + 1]) >= pos_med
                     else "descending")
        elif k > 0:
            order = ("ascending" if pos_med >= med(*plat_runs[k - 1])
                     else "descending")
        else:
            order = "ascending"
        plateaus.append({
            "index": k,
            "i_start": s0,
            "i_end": e0,
            "t_start": round(grid[s0], 6),
            "t_end": round(grid[e0], 6),
            "n_points": e0 - s0 + 1,
            "position_median_pct": round(pos_med, 4),
            "order": order,
            "start_reason": (
                f"窗口阀位速度降至 ≤{slope_lim:g}%/s 且进入 ±{band:g}% 稳态带"),
            "end_reason": (
                f"窗口阀位速度超过 {slope_lim:g}%/s，离开稳态带（或记录结束）"),
            "disabled": False,
            "disable_reason": None,
        })

    # ---- 人工停用（按累计列表施加） ----
    boundary_log = []
    for d in manual_disabled or []:
        idx = d["plateau_index"]
        if 0 <= idx < len(plateaus):
            plateaus[idx]["disabled"] = True
            plateaus[idx]["disable_reason"] = d.get("reason", "")

    # ---- 人工边界移动 ----
    for mv in manual_moves or []:
        idx = mv["plateau_index"]
        boundary, t_new = mv["boundary"], float(mv["new_time"])
        reason = mv.get("reason", "")
        i_new = min(range(n), key=lambda i: abs(grid[i] - t_new))
        applied, note = False, ""
        if not (0 <= idx < len(plateaus)):
            note = f"平台序号 {idx} 不存在（共 {len(plateaus)} 个平台）"
        elif boundary == "start":
            if i_new < plateaus[idx]["i_end"] and (
                    idx == 0 or i_new > plateaus[idx - 1]["i_end"]):
                old = plateaus[idx]["t_start"]
                plateaus[idx]["i_start"] = i_new
                plateaus[idx]["start_reason"] = (
                    f"人工调整：{reason}（原自动边界 {old}s，依据："
                    f"{plateaus[idx]['start_reason']}）")
                applied = True
            else:
                note = "新起点须位于前一平台终点与本平台终点之间"
        else:
            if i_new > plateaus[idx]["i_start"] and (
                    idx == len(plateaus) - 1 or i_new < plateaus[idx + 1]["i_start"]):
                old = plateaus[idx]["t_end"]
                plateaus[idx]["i_end"] = i_new
                plateaus[idx]["end_reason"] = (
                    f"人工调整：{reason}（原自动边界 {old}s，依据："
                    f"{plateaus[idx]['end_reason']}）")
                applied = True
            else:
                note = "新终点须位于本平台起点与后一平台起点之间"
        boundary_log.append({"move": mv, "applied": applied,
                             **({"note": note} if note else {})})

    # ---- 刷新时间/点数/中位阀位（边界移动后） ----
    for p in plateaus:
        s0, e0 = p["i_start"], p["i_end"]
        p["t_start"], p["t_end"] = round(grid[s0], 6), round(grid[e0], 6)
        p["n_points"] = e0 - s0 + 1
        vals = sorted(position[s0:e0 + 1])
        p["position_median_pct"] = round(vals[len(vals) // 2], 4)

    return plateaus, boundary_log
