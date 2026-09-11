"""从隔离动作圈定稳压段与保持段，并校验隔离次序。

期望次序（上游→下游座漏试验）：
    close_command → valve_closed → downstream_isolated → hold_start → hold_end

稳压段 = [downstream_isolated, hold_start)：隔离后热瞬态（温度平衡、绝热压缩）
衰减期；保持段 = [hold_start, hold_end]：泄漏率计量期。hold_start 缺省时按
温度窗口斜率自动圈定，hold_end 缺省取记录终点。技术人员可移动段边界，
每次移动记录原自动依据与人工理由。
"""

# 动作在期望次序中的位次（越小应越早）
ACTION_ORDER = {
    "close_command": 0,
    "valve_closed": 1,
    "downstream_isolated": 2,
    "hold_start": 3,
    "hold_end": 4,
}

ACTION_NAMES = {
    "close_command": "关阀指令",
    "valve_closed": "阀门关到位",
    "upstream_isolated": "上游隔离",
    "downstream_isolated": "下游隔离",
    "vent_opened": "放空打开",
    "vent_closed": "放空关闭",
    "hold_start": "保持段开始",
    "hold_end": "保持段结束",
}


def validate_isolation(actions):
    """校验隔离动作序列。返回 (events, contradictions, derived)。

    events: 按时间排序的动作时间线（含原始序号引用）；
    contradictions: [{code, detail}] 次序矛盾（证据缺口）；
    derived: {t_close_command, t_valve_closed, t_isolated, t_hold_start, t_hold_end}。
    """
    events = sorted(
        ({"t": float(a["t"]), "action": a["action"], "index": k,
          "note": a.get("note", "")} for k, a in enumerate(actions)),
        key=lambda e: (e["t"], e["index"]))
    contradictions = []

    # 同一动作重复记录：以首次为准并记矛盾
    seen = {}
    deduped = []
    for e in events:
        if e["action"] in ACTION_ORDER:
            if e["action"] in seen:
                contradictions.append({
                    "code": "isolation_sequence_conflict",
                    "detail": f"动作「{ACTION_NAMES[e['action']]}」重复记录："
                              f"t={seen[e['action']]['t']}s（#{seen[e['action']]['index']}）与 "
                              f"t={e['t']}s（#{e['index']}），以首次为准",
                })
                continue
            seen[e["action"]] = e
        deduped.append(e)
    events = deduped

    # 成对次序校验
    keys = [k for k in ("close_command", "valve_closed", "downstream_isolated",
                        "hold_start", "hold_end") if k in seen]
    for a, b in zip(keys, keys[1:]):
        if seen[a]["t"] >= seen[b]["t"]:
            contradictions.append({
                "code": "isolation_sequence_conflict",
                "detail": f"隔离次序矛盾：「{ACTION_NAMES[a]}」t={seen[a]['t']}s（#{seen[a]['index']}）"
                          f"不早于「{ACTION_NAMES[b]}」t={seen[b]['t']}s（#{seen[b]['index']}），"
                          "封闭容积形成过程不可信",
            })

    t_iso = seen.get("downstream_isolated", {}).get("t")
    t_hold_end = seen.get("hold_end", {}).get("t")

    # 保持段内放空/上游隔离：封闭容积不成立
    for e in events:
        if e["action"] == "vent_opened" and t_iso is not None and e["t"] >= t_iso and (
                t_hold_end is None or e["t"] <= t_hold_end):
            contradictions.append({
                "code": "isolation_sequence_conflict",
                "detail": f"放空阀在下游隔离后打开（t={e['t']}s，#{e['index']}），"
                          "保持段封闭容积不成立",
            })
        if e["action"] == "upstream_isolated" and t_iso is not None and e["t"] >= t_iso and (
                t_hold_end is None or e["t"] <= t_hold_end):
            contradictions.append({
                "code": "isolation_sequence_conflict",
                "detail": f"上游在保持段内被隔离（t={e['t']}s，#{e['index']}），"
                          "试验压差无法维持",
            })

    derived = {
        "t_close_command": seen.get("close_command", {}).get("t"),
        "t_valve_closed": seen.get("valve_closed", {}).get("t"),
        "t_isolated": t_iso,
        "t_hold_start": seen.get("hold_start", {}).get("t"),
        "t_hold_end": t_hold_end,
    }
    return events, contradictions, derived


def auto_hold_start(grid, temp_c, i_iso, slope_window_s, temp_slope_max_c_min):
    """自动保持段起点：隔离后温度窗口斜率首次降到阈值内且不再超出 2 倍阈值。

    返回网格下标；温度始终未稳定返回 None。
    """
    from .gas import trailing_slopes

    slopes = trailing_slopes(grid, temp_c, slope_window_s)
    n = len(grid)
    for i in range(max(i_iso, 1), n):
        s = slopes[i]
        if s is None or abs(s) > temp_slope_max_c_min:
            continue
        rest = [abs(x) for x in slopes[i:] if x is not None]
        if rest and max(rest) <= 2.0 * temp_slope_max_c_min:
            return i
    return None


def delineate_segments(grid, temp_c, derived, thr, manual_moves=None):
    """圈定稳压段与保持段。

    返回 (segments, boundary_log, gaps)。segments 为两段式结构：
    [ {index:0, type:"stabilization", ...}, {index:1, type:"hold", ...} ]，
    每段含 t_start/t_end/i_start/i_end 与 start_reason/end_reason。
    人工移动通过 manual_moves=[{segment, boundary, new_time, reason}] 施加，
    共享边界（稳压段终点=保持段起点）联动更新。
    """
    gaps, boundary_log = [], []
    t_iso = derived["t_isolated"]
    i_iso = min(range(len(grid)), key=lambda i: abs(grid[i] - t_iso))
    if grid[i_iso] < t_iso and i_iso + 1 < len(grid):
        i_iso += 1

    # ---- 保持段起点：显式动作 > 自动温度斜率 ----
    hold_start_reason = None
    if derived.get("t_hold_start") is not None:
        i_hs = min(range(len(grid)), key=lambda i: abs(grid[i] - derived["t_hold_start"]))
        hold_start_reason = (f"保持段开始动作 t={derived['t_hold_start']}s（隔离记录），"
                             "采用显式声明")
    else:
        i_hs = auto_hold_start(grid, temp_c, i_iso, thr["slope_window_s"],
                               thr["temp_slope_max_c_min"])
        if i_hs is not None:
            hold_start_reason = (
                f"温度窗口斜率降至 ≤{thr['temp_slope_max_c_min']}°C/min "
                f"（{thr['slope_window_s']}s 滑窗），判定热瞬态已衰减")
    if i_hs is None:
        gaps.append({"code": "stabilization_not_reached",
                     "detail": f"隔离后温度在记录内未稳定（窗口斜率未降至 "
                               f"{thr['temp_slope_max_c_min']}°C/min 以下），"
                               "无法圈定保持段"})
        i_hs = None

    # ---- 保持段终点：显式动作 > 记录终点 ----
    if derived.get("t_hold_end") is not None:
        i_he = min(range(len(grid)), key=lambda i: abs(grid[i] - derived["t_hold_end"]))
        hold_end_reason = f"保持段结束动作 t={derived['t_hold_end']}s（隔离记录）"
    else:
        i_he = len(grid) - 1
        hold_end_reason = "测试记录终点（未提供保持段结束动作）"

    segments = [
        {"index": 0, "type": "stabilization", "i_start": i_iso,
         "i_end": (i_hs - 1) if i_hs is not None else None,
         "start_reason": f"下游隔离动作 t={t_iso}s（隔离记录），封闭容积形成",
         "end_reason": hold_start_reason or "（保持段起点未圈定）"},
        {"index": 1, "type": "hold", "i_start": i_hs, "i_end": i_he,
         "start_reason": hold_start_reason or "（未圈定）",
         "end_reason": hold_end_reason},
    ]

    # ---- 人工边界移动（共享边界联动） ----
    for mv in manual_moves or []:
        seg_name, boundary = mv["segment"], mv["boundary"]
        t_new = float(mv["new_time"])
        reason = mv.get("reason", "")
        i_new = min(range(len(grid)), key=lambda i: abs(grid[i] - t_new))
        applied = False
        note = ""
        if seg_name == "stabilization" and boundary == "start":
            if i_new < segments[0]["i_end"]:
                old = grid[segments[0]["i_start"]]
                segments[0]["i_start"] = i_new
                segments[0]["start_reason"] = (
                    f"人工调整：{reason}（原自动边界 {old:.2f}s，依据："
                    f"{segments[0]['start_reason']}）")
                applied = True
            else:
                note = "稳压段起点不能越过稳压段终点"
        elif seg_name in ("stabilization", "hold") and (
                (seg_name == "stabilization" and boundary == "end")
                or (seg_name == "hold" and boundary == "start")):
            # 共享边界：稳压段终点 == 保持段起点
            if segments[0]["i_start"] < i_new < segments[1]["i_end"]:
                old = grid[segments[1]["i_start"]] if segments[1]["i_start"] is not None else None
                segments[0]["i_end"] = i_new - 1
                segments[1]["i_start"] = i_new
                txt = (f"人工调整：{reason}（原边界 {old if old is None else round(old, 2)}s，"
                       f"依据：{segments[1]['start_reason']}）")
                segments[0]["end_reason"] = txt
                segments[1]["start_reason"] = txt
                applied = True
            else:
                note = "共享边界须位于稳压段起点与保持段终点之间"
        elif seg_name == "hold" and boundary == "end":
            if segments[1]["i_start"] is not None and i_new > segments[1]["i_start"]:
                old = grid[segments[1]["i_end"]]
                segments[1]["i_end"] = i_new
                segments[1]["end_reason"] = (
                    f"人工调整：{reason}（原自动边界 {old:.2f}s，依据："
                    f"{segments[1]['end_reason']}）")
                applied = True
            else:
                note = "保持段终点必须晚于保持段起点"
        else:
            note = f"不支持移动 {seg_name}.{boundary}"
        boundary_log.append({"move": mv, "applied": applied, **({"note": note} if note else {})})

    # ---- 刷新时间缓存与时长检查 ----
    for seg in segments:
        seg["t_start"] = (round(grid[seg["i_start"]], 6)
                          if seg["i_start"] is not None else None)
        seg["t_end"] = (round(grid[seg["i_end"]], 6)
                        if seg["i_end"] is not None else None)

    stab, hold = segments
    if stab["i_end"] is not None:
        stab_len = grid[stab["i_end"]] - grid[stab["i_start"]]
        if stab_len < thr["stabilization_min_s"]:
            gaps.append({"code": "stabilization_too_short",
                         "detail": f"稳压段仅 {stab_len:.1f}s（要求 ≥"
                                   f"{thr['stabilization_min_s']}s），热瞬态可能未充分衰减，"
                                   "泄漏率计量不可靠"})
    if hold["i_start"] is not None:
        hold_len = grid[hold["i_end"]] - grid[hold["i_start"]]
        if hold_len < thr["hold_min_s"]:
            gaps.append({"code": "hold_too_short",
                         "detail": f"保持段仅 {hold_len:.1f}s（要求 ≥{thr['hold_min_s']}s），"
                                   "泄漏率与累计漏量统计意义不足"})
    return segments, boundary_log, gaps
