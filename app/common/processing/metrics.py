"""指标计算：行程时间、死区、回差、过冲、稳态偏差。"""


def _moving(segments):
    return [s for s in segments if s["type"] in ("opening", "closing")]


def travel_time(grid, cmd, pos, segments, settle_band_pct):
    """行程时间：指令到达终值（下一段起点）后，阀位进入目标 ±settle_band 的附加时间。

    同时给出从段起点算起的总响应时间 total_response_s 供参考。
    搜索窗口覆盖本段及后续停留段（阶跃响应在停留段内完成稳定）。
    """
    per_segment = []
    for k, seg in enumerate(segments):
        if seg["type"] not in ("opening", "closing"):
            continue
        target = seg.get("target_pct", seg["cmd_end"])
        # 指令到位时刻：下一段起点（通常为停留段），末段则为自身终点
        if k + 1 < len(segments):
            t_cmd_final = segments[k + 1]["t_start"]
            i_search_end = segments[k + 1]["i_end"]
        else:
            t_cmd_final = seg["t_end"]
            i_search_end = seg["i_end"]
        t_settle = None
        for i in range(seg["i_start"], i_search_end + 1):
            if abs(pos[i] - target) <= settle_band_pct:
                t_settle = grid[i]
                break
        per_segment.append({
            "segment_index": seg["index"],
            "type": seg["type"],
            "target_pct": round(target, 3),
            "travel_time_s": round(max(0.0, t_settle - t_cmd_final), 3) if t_settle is not None else None,
            "total_response_s": round(t_settle - seg["t_start"], 3) if t_settle is not None else None,
            "reached": t_settle is not None,
        })
    vals = [e["travel_time_s"] for e in per_segment if e["travel_time_s"] is not None]
    return {"per_segment": per_segment,
            "max_s": round(max(vals), 3) if vals else None}


def deadband(grid, cmd, pos, segments, move_thresh_pct=0.5):
    """方向反转点处，阀位尚未响应（位移 < move_thresh）期间指令的最大变化量。

    从反向运动段的起点开始扫描，跳过中间停留段内阀位的缓慢爬行。
    """
    reversals = []
    mov = _moving(segments)
    for a, b in zip(mov, mov[1:]):
        if a["type"] == b["type"]:
            continue
        i0 = b["i_start"]
        pos0, cmd0 = pos[i0], cmd[i0]
        band = 0.0
        t_end = grid[i0]
        for i in range(i0, b["i_end"] + 1):
            if abs(pos[i] - pos0) >= move_thresh_pct:
                break
            band = max(band, abs(cmd[i] - cmd0))
            t_end = grid[i]
        reversals.append({
            "at_time": round(grid[i0], 3),
            "direction": f"{a['type']}->{b['type']}",
            "deadband_pct": round(band, 3),
            "unresponsive_until": round(t_end, 3),
        })
    vals = [r["deadband_pct"] for r in reversals]
    return {"reversals": reversals,
            "max_pct": round(max(vals), 3) if vals else None}


def _interp_by_cmd(points, c):
    """points: 按 cmd 排序的 (cmd, pos)。线性插值。"""
    if not points:
        return None
    if c <= points[0][0]:
        return points[0][1]
    if c >= points[-1][0]:
        return points[-1][1]
    for k in range(len(points) - 1):
        c0, p0 = points[k]
        c1, p1 = points[k + 1]
        if c0 <= c <= c1:
            if c1 - c0 < 1e-9:
                return p0
            return p0 + (p1 - p0) * (c - c0) / (c1 - c0)
    return None


def _sorted_unique(points):
    points = sorted(points)
    out = []
    for c, p in points:
        if out and abs(c - out[-1][0]) < 1e-6:
            out[-1] = (c, (out[-1][1] + p) / 2.0)
        else:
            out.append((c, p))
    return out


def hysteresis(grid, cmd, pos, segments, settle_band_pct, step_pct=1.0):
    """同一指令下开/关行程阀位差的最大值。

    仅取准静态点（|阀位-指令| 在带宽内），避免把阶跃响应的动态滞后
    误计为回差；阶跃型测试通常无准静态运动数据，返回 None 并说明。
    """
    qs_band = max(5.0, 2.0 * settle_band_pct)
    opening, closing = [], []
    for seg in segments:
        if seg["type"] not in ("opening", "closing"):
            continue
        for i in range(seg["i_start"], seg["i_end"] + 1):
            if abs(pos[i] - cmd[i]) > qs_band:
                continue  # 动态滞后点，不计入回差
            if seg["type"] == "opening":
                opening.append((cmd[i], pos[i]))
            else:
                closing.append((cmd[i], pos[i]))
    opening = _sorted_unique(opening)
    closing = _sorted_unique(closing)
    if not opening or not closing:
        return {"max_pct": None, "at_command_pct": None,
                "note": "无足够准静态数据（阶跃型指令或跟踪偏差过大），回差不适用"}
    lo = max(opening[0][0], closing[0][0])
    hi = min(opening[-1][0], closing[-1][0])
    if hi <= lo:
        return {"max_pct": None, "at_command_pct": None, "note": "开/关行程指令范围无重叠"}
    best, best_c = 0.0, None
    n = max(2, int((hi - lo) / step_pct) + 1)
    for k in range(n):
        c = lo + (hi - lo) * k / (n - 1)
        po = _interp_by_cmd(opening, c)
        pc = _interp_by_cmd(closing, c)
        if po is None or pc is None:
            continue
        d = abs(po - pc)
        if d > best:
            best, best_c = d, c
    return {"max_pct": round(best, 3),
            "at_command_pct": round(best_c, 2) if best_c is not None else None}


def overshoot(grid, cmd, pos, segments, settle_s=2.0):
    """越过目标指令（下一段起点指令值）的最大超出量（段内及其后 settle_s 内）。"""
    per_segment = []
    for seg in _moving(segments):
        target = seg.get("target_pct", seg["cmd_end"])
        i_end = seg["i_end"]
        while i_end + 1 < len(grid) and grid[i_end + 1] - seg["t_end"] <= settle_s:
            i_end += 1
        window = pos[seg["i_start"]:i_end + 1]
        if seg["type"] == "opening":
            os = max(0.0, max(window) - target)
        else:
            os = max(0.0, target - min(window))
        per_segment.append({
            "segment_index": seg["index"],
            "type": seg["type"],
            "target_pct": round(target, 3),
            "overshoot_pct": round(os, 3),
        })
    vals = [e["overshoot_pct"] for e in per_segment]
    return {"per_segment": per_segment,
            "max_pct": round(max(vals), 3) if vals else None}


def steady_state_deviation(grid, cmd, pos, segments, tail_s_min=1.0, tail_frac=0.25):
    """稳态偏差：停留段尾部（最后 25% 或至少 tail_s_min 秒）的 |阀位-指令|。"""
    per_segment = []
    for seg in segments:
        if seg["type"] != "dwell":
            continue
        dur = seg["t_end"] - seg["t_start"]
        tail = max(tail_s_min, tail_frac * dur)
        i0 = seg["i_start"]
        while i0 < seg["i_end"] and grid[i0] < seg["t_end"] - tail:
            i0 += 1
        devs = [abs(pos[i] - cmd[i]) for i in range(i0, seg["i_end"] + 1)]
        if not devs:
            continue
        per_segment.append({
            "segment_index": seg["index"],
            "t_start": round(grid[i0], 3),
            "t_end": seg["t_end"],
            "mean_pct": round(sum(devs) / len(devs), 3),
            "max_pct": round(max(devs), 3),
        })
    vals = [e["max_pct"] for e in per_segment]
    return {"per_segment": per_segment,
            "max_pct": round(max(vals), 3) if vals else None}
