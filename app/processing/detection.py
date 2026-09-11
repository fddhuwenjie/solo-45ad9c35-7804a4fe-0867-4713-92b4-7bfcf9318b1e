"""异常定位：卡跳、反向迟滞、供压不足、信号断档、未完成行程。"""

from .alignment import median_dt


def detect_dropouts(raw_series, gap_factor=3.0, frozen_min=5):
    """在原始（未对齐）采样上检测断档：时间缺口与冻结（值不变）。

    冻结检测只用于反馈类信号（阀位/压力）——指令在停留段本来就该恒定。
    """
    events = []
    for channel, (ts, vs) in raw_series.items():
        if len(ts) < 2:
            continue
        dt_med = median_dt(ts)
        for i in range(1, len(ts)):
            if ts[i] - ts[i - 1] > gap_factor * dt_med:
                events.append({
                    "channel": channel,
                    "kind": "gap",
                    "t_start": round(ts[i - 1], 3),
                    "t_end": round(ts[i], 3),
                    "detail": f"采样间隔 {ts[i] - ts[i - 1]:.3f}s 超过中位间隔 {dt_med:.3f}s 的 {gap_factor} 倍",
                })
        if channel == "command":
            continue
        run = 1
        for i in range(1, len(vs)):
            if vs[i] == vs[i - 1]:
                run += 1
            else:
                if run >= frozen_min:
                    events.append({
                        "channel": channel,
                        "kind": "frozen",
                        "t_start": round(ts[i - run], 3),
                        "t_end": round(ts[i - 1], 3),
                        "detail": f"连续 {run} 个采样值完全相同（{vs[i - 1]:.3f}），信号疑似冻结",
                    })
                run = 1
        if run >= frozen_min:
            events.append({
                "channel": channel,
                "kind": "frozen",
                "t_start": round(ts[len(vs) - run], 3),
                "t_end": round(ts[-1], 3),
                "detail": f"连续 {run} 个采样值完全相同（{vs[-1]:.3f}），信号疑似冻结",
            })
    return events


def detect_stick_slip(grid, cmd, pos, segments, stuck_tol_pct=0.3, cmd_excursion_pct=1.0,
                      jump_pct=1.0, min_stuck_s=0.5, jump_win_s=0.3):
    """卡跳：指令持续变化而阀位停滞，随后突然跳动。"""
    episodes = []
    for seg in segments:
        if seg["type"] not in ("opening", "closing"):
            continue
        i = seg["i_start"]
        while i < seg["i_end"]:
            j = i
            while j + 1 <= seg["i_end"] and abs(pos[j + 1] - pos[i]) < stuck_tol_pct:
                j += 1
            stuck_dur = grid[j] - grid[i]
            cmd_chg = abs(cmd[j] - cmd[i])
            if stuck_dur >= min_stuck_s and cmd_chg >= cmd_excursion_pct:
                jump = 0.0
                k_end = j
                k = j + 1
                while k <= seg["i_end"] and grid[k] - grid[j] <= jump_win_s:
                    jump = max(jump, abs(pos[k] - pos[j]))
                    k_end = k
                    k += 1
                if jump >= jump_pct:
                    episodes.append({
                        "segment_index": seg["index"],
                        "t_start": round(grid[i], 3),
                        "t_end": round(grid[k_end], 3),
                        "stuck_duration_s": round(stuck_dur, 3),
                        "cmd_change_while_stuck_pct": round(cmd_chg, 3),
                        "jump_pct": round(jump, 3),
                    })
                    i = k_end
                    continue
            i = max(j, i + 1)
    return episodes


def detect_reverse_hysteresis(deadband_result, threshold_pct):
    """反向迟滞：反转点死区超过阈值。"""
    out = []
    for r in deadband_result.get("reversals", []):
        if r["deadband_pct"] > threshold_pct:
            out.append({
                "at_time": r["at_time"],
                "direction": r["direction"],
                "deadband_pct": r["deadband_pct"],
                "threshold_pct": threshold_pct,
            })
    return out


def detect_low_supply(grid, pressure, segments, supply_min_kpa):
    """供压不足：运动区段内执行器压力低于下限。返回合并后的时段。"""
    periods = []
    for seg in segments:
        if seg["type"] not in ("opening", "closing"):
            continue
        start = None
        min_p = None
        for i in range(seg["i_start"], seg["i_end"] + 1):
            if pressure[i] < supply_min_kpa:
                if start is None:
                    start = i
                    min_p = pressure[i]
                else:
                    min_p = min(min_p, pressure[i])
            else:
                if start is not None:
                    periods.append({
                        "segment_index": seg["index"],
                        "t_start": round(grid[start], 3),
                        "t_end": round(grid[i - 1], 3),
                        "min_pressure_kpa": round(min_p, 1),
                        "supply_min_kpa": supply_min_kpa,
                    })
                    start = None
        if start is not None:
            periods.append({
                "segment_index": seg["index"],
                "t_start": round(grid[start], 3),
                "t_end": round(grid[seg["i_end"]], 3),
                "min_pressure_kpa": round(min_p, 1),
                "supply_min_kpa": supply_min_kpa,
            })
    return periods


def detect_incomplete_travel(cmd, pos, settle_band_pct):
    """未完成行程：阀位未到达指令端点（±settle_band 内）。"""
    cmd_hi, cmd_lo = max(cmd), min(cmd)
    pos_hi, pos_lo = max(pos), min(pos)
    issues = []
    if pos_hi < cmd_hi - settle_band_pct:
        issues.append({
            "end": "high",
            "commanded_pct": round(cmd_hi, 2),
            "reached_pct": round(pos_hi, 2),
            "detail": f"阀位最高 {pos_hi:.1f}%，未达到指令上限 {cmd_hi:.1f}%（允差 {settle_band_pct}%）",
        })
    if pos_lo > cmd_lo + settle_band_pct:
        issues.append({
            "end": "low",
            "commanded_pct": round(cmd_lo, 2),
            "reached_pct": round(pos_lo, 2),
            "detail": f"阀位最低 {pos_lo:.1f}%，未达到指令下限 {cmd_lo:.1f}%（允差 {settle_band_pct}%）",
        })
    return issues
