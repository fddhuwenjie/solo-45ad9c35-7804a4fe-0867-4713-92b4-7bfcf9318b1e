"""自动分段：按指令信号把全行程测试切分为开阀 / 停留 / 关阀区段。

每个边界都返回依据（观测到的斜率、稳定带、方向反转或人工调整理由），
技术人员可据此审核并移动边界。
"""

SEG_OPENING = "opening"
SEG_CLOSING = "closing"
SEG_DWELL = "dwell"

_DIR_NAME = {1: SEG_OPENING, -1: SEG_CLOSING, 0: SEG_DWELL}


def _classify(grid, cmd, slope_thr, win_s):
    """按窗口斜率把每个采样点分类为 升(1)/降(-1)/稳(0)。"""
    n = len(grid)
    dirs = [0] * n
    slopes = [0.0] * n
    for i in range(n):
        j = i
        while j + 1 < n and grid[j + 1] - grid[i] < win_s:
            j += 1
        dt = grid[j] - grid[i]
        s = (cmd[j] - cmd[i]) / dt if dt > 0 else 0.0
        slopes[i] = s
        dirs[i] = 1 if s > slope_thr else (-1 if s < -slope_thr else 0)
    return dirs, slopes


def _runs(dirs):
    """把方向序列切成连续段 [(dir, start, end)]（end 含）。"""
    out = []
    start = 0
    for i in range(1, len(dirs)):
        if dirs[i] != dirs[start]:
            out.append((dirs[start], start, i - 1))
            start = i
    out.append((dirs[start], start, len(dirs) - 1))
    return out


def _merge_short_dwells(runs, grid, min_dwell_s):
    """短于最小时长的稳定段并入前一段（运动延续）；开头短稳定段并入后一段。"""
    merged = list(runs)
    changed = True
    while changed:
        changed = False
        for k, (d, s, e) in enumerate(merged):
            if d == 0 and grid[e] - grid[s] < min_dwell_s and len(merged) > 1:
                if k == 0:
                    nd, ns, ne = merged[1]
                    merged[1] = (nd, s, ne)
                    del merged[0]
                else:
                    pd, ps, pe = merged[k - 1]
                    merged[k - 1] = (pd, ps, e)
                    del merged[k]
                changed = True
                break
    # 合并相邻同向段
    out = []
    for d, s, e in merged:
        if out and out[-1][0] == d:
            out[-1] = (d, out[-1][1], e)
        else:
            out.append((d, s, e))
    return out


def _boundary_reason(kind, grid, cmd, slopes, idx, slope_thr, stable_band):
    c = cmd[idx]
    s = slopes[idx]
    if kind == "dwell->opening":
        return f"指令由稳定（{c:.1f}%）转为上升，窗口斜率 {s:+.2f}%/s 超过阈值 {slope_thr}%/s"
    if kind == "dwell->closing":
        return f"指令由稳定（{c:.1f}%）转为下降，窗口斜率 {s:+.2f}%/s 低于阈值 -{slope_thr}%/s"
    if kind == "opening->dwell":
        return f"指令停止上升并稳定在 {c:.1f}%（波动带 ±{stable_band}%）"
    if kind == "closing->dwell":
        return f"指令停止下降并稳定在 {c:.1f}%（波动带 ±{stable_band}%）"
    if kind == "opening->closing":
        return f"指令方向反转（升→降），反转点指令 {c:.1f}%"
    if kind == "closing->opening":
        return f"指令方向反转（降→升），反转点指令 {c:.1f}%"
    return f"区段边界（指令 {c:.1f}%）"


def segment(grid, cmd, slope_thr=0.5, win_s=0.4, min_dwell_s=1.0, stable_band=0.3,
            manual_boundaries=None):
    """切分区段。manual_boundaries: [{segment_index, boundary, new_time, reason}]。

    返回 (segments, boundary_log)。segments 元素:
    {index, type, i_start, i_end, t_start, t_end, cmd_start, cmd_end,
     start_reason, end_reason}
    """
    dirs, slopes = _classify(grid, cmd, slope_thr, win_s)
    runs = _merge_short_dwells(_runs(dirs), grid, min_dwell_s)

    segments = []
    boundary_log = []
    for k, (d, s, e) in enumerate(runs):
        seg_type = _DIR_NAME[d]
        if k == 0:
            start_reason = "测试记录起点"
        else:
            kind = f"{_DIR_NAME[runs[k - 1][0]]}->{seg_type}"
            start_reason = _boundary_reason(kind, grid, cmd, slopes, s, slope_thr, stable_band)
        if k == len(runs) - 1:
            end_reason = "测试记录终点"
        else:
            kind = f"{seg_type}->{_DIR_NAME[runs[k + 1][0]]}"
            end_reason = _boundary_reason(kind, grid, cmd, slopes, e, slope_thr, stable_band)
        segments.append({
            "index": len(segments),
            "type": seg_type,
            "i_start": s,
            "i_end": e,
            "t_start": round(grid[s], 6),
            "t_end": round(grid[e], 6),
            "cmd_start": round(cmd[s], 4),
            "cmd_end": round(cmd[e], 4),
            "start_reason": start_reason,
            "end_reason": end_reason,
        })

    # 人工边界调整：移动指定区段的起点/终点，保留原自动依据
    for mv in manual_boundaries or []:
        si = mv["segment_index"]
        if si < 0 or si >= len(segments):
            boundary_log.append({"move": mv, "applied": False,
                                 "note": f"区段序号 {si} 超出范围"})
            continue
        seg = segments[si]
        t_new = float(mv["new_time"])
        i_new = min(range(len(grid)), key=lambda i: abs(grid[i] - t_new))
        reason = mv.get("reason", "")
        if mv["boundary"] == "start" and si > 0:
            old = seg["t_start"]
            prev = segments[si - 1]
            i_new = max(prev["i_start"] + 1, min(i_new, seg["i_end"] - 1))
            note = f"人工调整：{reason}（原自动边界 {old:.2f}s，依据：{seg['start_reason']}）"
            seg["i_start"] = i_new
            prev["i_end"] = i_new - 1
            prev["end_reason"] = note
            seg["start_reason"] = note
        elif mv["boundary"] == "end" and si < len(segments) - 1:
            old = seg["t_end"]
            nxt = segments[si + 1]
            i_new = max(seg["i_start"] + 1, min(i_new, nxt["i_end"] - 1))
            note = f"人工调整：{reason}（原自动边界 {old:.2f}s，依据：{seg['end_reason']}）"
            seg["i_end"] = i_new
            nxt["i_start"] = i_new + 1
            nxt["start_reason"] = note
            seg["end_reason"] = note
        else:
            boundary_log.append({"move": mv, "applied": False,
                                 "note": "首段起点/末段终点为记录边界，不可移动"})
            continue
        boundary_log.append({"move": mv, "applied": True})

    # 调整后刷新时间/指令缓存
    for seg in segments:
        seg["t_start"] = round(grid[seg["i_start"]], 6)
        seg["t_end"] = round(grid[seg["i_end"]], 6)
        seg["cmd_start"] = round(cmd[seg["i_start"]], 4)
        seg["cmd_end"] = round(cmd[seg["i_end"]], 4)
    # 运动段目标指令：取下一段起点的指令值（前向窗口会使本段终点指令滞后）
    for k, seg in enumerate(segments):
        if seg["type"] in (SEG_OPENING, SEG_CLOSING):
            nxt = segments[k + 1] if k + 1 < len(segments) else seg
            seg["target_pct"] = round(cmd[nxt["i_start"]], 4)
    return segments, boundary_log
