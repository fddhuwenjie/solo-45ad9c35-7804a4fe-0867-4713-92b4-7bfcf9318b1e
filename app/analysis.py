"""分析编排：单位统一 → 时间对齐 → 分段 → 指标 → 异常定位 → 判定。

每次人工调整（移动边界 / 剔除无效点）都以累计调整列表重新计算，
生成新的分析版本；被剔除点保留原始引用（通道、序号、时标、数值）与理由。
"""

import json
from datetime import datetime, timezone

from .processing import alignment, detection, metrics, segmentation, units


def _parse_time(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _apply_exclusions(series, exclusions):
    """从原始序列中剔除无效点，返回 (filtered_series, exclusion_records)。"""
    records = []
    filtered = {}
    for ch in ("command", "position", "pressure"):
        filtered[ch] = {
            "unit": series[ch]["unit"],
            "points": [list(p) for p in series[ch]["points"]],
        }
    for ex in exclusions:
        ch = ex["channel"]
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


def _steady_state_periods(grid, cmd, pos, segments, threshold):
    periods = []
    for seg in segments:
        if seg["type"] != "dwell":
            continue
        start = None
        for i in range(seg["i_start"], seg["i_end"] + 1):
            if abs(pos[i] - cmd[i]) > threshold:
                if start is None:
                    start = i
            else:
                if start is not None:
                    periods.append([round(grid[start], 3), round(grid[i - 1], 3)])
                    start = None
        if start is not None:
            periods.append([round(grid[start], 3), round(grid[seg["i_end"]], 3)])
    return periods


def run_analysis(db, test_id, author="auto", new_adjustments=None):
    """执行分析并保存新版本。new_adjustments: 本次新增的 boundary_moves/exclusions。"""
    test = db.get_test(test_id)
    if test is None:
        raise KeyError(f"测试 {test_id} 不存在")
    payload = test["payload"]
    thresholds = payload.get("thresholds") or test["thresholds"]
    new_adjustments = new_adjustments or {}

    # 累计历史调整（上一版本）+ 本次新增
    existing = db.list_analyses(test_id)
    cumulative = []
    if existing:
        prev = db.get_analysis(existing[-1]["id"])
        cumulative = list(prev["adjustments"])
    for mv in new_adjustments.get("boundary_moves", []):
        cumulative.append({"type": "boundary_move", "author": author, **mv})
    for ex in new_adjustments.get("exclusions", []):
        cumulative.append({"type": "exclusion", "author": author, **ex})

    exclusions = [a for a in cumulative if a["type"] == "exclusion"]
    boundary_moves = [a for a in cumulative if a["type"] == "boundary_move"]

    # 1. 剔除无效点（保留原始点引用）
    filtered, exclusion_records = _apply_exclusions(payload["series"], exclusions)

    # 2. 单位统一
    rng = payload["range"]
    norm = units.normalize_series(filtered, rng["min"], rng["max"], rng["unit"])
    raw_norm = {c: norm[c] for c in ("command", "position", "pressure")}

    blocking = [f"单位冲突：{c}" for c in norm["conflicts"]]

    # 3. 校准有效期
    cal_until = _parse_time(payload.get("calibration_valid_until"))
    test_time = _parse_time(payload.get("test_started_at")) or _parse_time(test["submitted_at"])
    if cal_until is None:
        blocking.append("未提供校准有效期，无法确认仪表校准状态")
    elif test_time and cal_until < test_time:
        blocking.append(
            f"校准已于 {payload['calibration_valid_until']} 失效，测试时间 "
            f"{payload.get('test_started_at') or test['submitted_at']} 晚于有效期")

    # 4. 时间对齐
    grid, aligned, align_meta = alignment.align(raw_norm)
    cmd, pos, prs = aligned["command"], aligned["position"], aligned["pressure"]

    # 5. 断档检测（基于剔除后的原始采样）
    dropouts = detection.detect_dropouts(raw_norm)

    # 6. 分段（自动 + 人工边界）
    segments, boundary_log = segmentation.segment(
        grid, cmd, manual_boundaries=boundary_moves)

    # 7. 指标
    settle = thresholds["settle_band_pct"]
    m_travel = metrics.travel_time(grid, cmd, pos, segments, settle)
    m_dead = metrics.deadband(grid, cmd, pos, segments)
    m_hyst = metrics.hysteresis(grid, cmd, pos, segments, settle)
    m_over = metrics.overshoot(grid, cmd, pos, segments)
    m_ss = metrics.steady_state_deviation(grid, cmd, pos, segments)

    # 8. 异常定位
    d_stick = detection.detect_stick_slip(grid, cmd, pos, segments)
    d_rev = detection.detect_reverse_hysteresis(m_dead, thresholds["deadband_pct_max"])
    d_low = detection.detect_low_supply(grid, prs, segments, thresholds["supply_pressure_min_kpa"])
    d_incomplete = detection.detect_incomplete_travel(cmd, pos, settle)

    # 9. 关键区段完整性
    types = {s["type"] for s in segments}
    for need, name in (("opening", "开阀"), ("closing", "关阀"), ("dwell", "停留")):
        if need not in types:
            blocking.append(f"关键区段不完整：缺少{name}区段")
    # 断档覆盖运动区段 > 20% 视为关键区段数据不完整
    for seg in segments:
        if seg["type"] not in ("opening", "closing"):
            continue
        dur = seg["t_end"] - seg["t_start"]
        if dur <= 0:
            continue
        lost = 0.0
        for ev in dropouts:
            lo = max(ev["t_start"], seg["t_start"])
            hi = min(ev["t_end"], seg["t_end"])
            if hi > lo:
                lost += hi - lo
        if lost / dur > 0.2:
            blocking.append(
                f"关键区段不完整：{seg['type']} 段 [{seg['t_start']:.1f}, {seg['t_end']:.1f}]s "
                f"数据断档占 {lost / dur * 100:.0f}%")

    # 10. 超限清单（含时段）
    issues = []

    def add(kind, detail, value, limit, periods):
        issues.append({"kind": kind, "detail": detail, "value": value,
                       "threshold": limit, "periods": periods})

    if m_travel["max_s"] is not None and m_travel["max_s"] > thresholds["travel_time_s_max"]:
        segs = [e for e in m_travel["per_segment"]
                if e["travel_time_s"] and e["travel_time_s"] > thresholds["travel_time_s_max"]]
        add("travel_time", "行程时间超限", m_travel["max_s"], thresholds["travel_time_s_max"],
            [[segments[e["segment_index"]]["t_start"], segments[e["segment_index"]]["t_end"]]
             for e in segs])
    if m_dead["max_pct"] is not None and m_dead["max_pct"] > thresholds["deadband_pct_max"]:
        add("deadband", "死区超限", m_dead["max_pct"], thresholds["deadband_pct_max"],
            [[r["at_time"], r["unresponsive_until"]] for r in m_dead["reversals"]
             if r["deadband_pct"] > thresholds["deadband_pct_max"]])
    if m_hyst["max_pct"] is not None and m_hyst["max_pct"] > thresholds["hysteresis_pct_max"]:
        add("hysteresis", "回差超限", m_hyst["max_pct"], thresholds["hysteresis_pct_max"], [])
    if m_over["max_pct"] is not None and m_over["max_pct"] > thresholds["overshoot_pct_max"]:
        segs = [e for e in m_over["per_segment"] if e["overshoot_pct"] > thresholds["overshoot_pct_max"]]
        add("overshoot", "过冲超限", m_over["max_pct"], thresholds["overshoot_pct_max"],
            [[segments[e["segment_index"]]["t_start"], segments[e["segment_index"]]["t_end"]]
             for e in segs])
    if m_ss["max_pct"] is not None and m_ss["max_pct"] > thresholds["steady_state_pct_max"]:
        add("steady_state", "稳态偏差超限", m_ss["max_pct"], thresholds["steady_state_pct_max"],
            _steady_state_periods(grid, cmd, pos, segments, thresholds["steady_state_pct_max"]))
    for ep in d_stick:
        add("stick_slip",
            f"卡跳：停滞 {ep['stuck_duration_s']}s 后跳动 {ep['jump_pct']}%",
            ep["jump_pct"], None, [[ep["t_start"], ep["t_end"]]])
    for r in d_rev:
        add("reverse_hysteresis",
            f"反向迟滞：{r['direction']} 死区 {r['deadband_pct']}%",
            r["deadband_pct"], r["threshold_pct"], [[r["at_time"], r["at_time"]]])
    for p in d_low:
        add("low_supply",
            f"供压不足：最低 {p['min_pressure_kpa']} kPa",
            p["min_pressure_kpa"], p["supply_min_kpa"], [[p["t_start"], p["t_end"]]])
    for it in d_incomplete:
        add("incomplete_travel", f"未完成行程：{it['detail']}", None, None, [])
    for ev in dropouts:
        add("dropout", f"信号断档（{ev['channel']}/{ev['kind']}）：{ev['detail']}",
            None, None, [[ev["t_start"], ev["t_end"]]])

    if blocking:
        verdict = "no_conclusion"
    elif issues:
        verdict = "exceedances"
    else:
        verdict = "ok"

    result = {
        "test_id": test_id,
        "verdict": verdict,
        "blocking_issues": blocking,
        "unit_notes": norm["notes"],
        "alignment": align_meta,
        "segments": segments,
        "boundary_log": boundary_log,
        "metrics": {
            "travel_time": m_travel,
            "deadband": m_dead,
            "hysteresis": m_hyst,
            "overshoot": m_over,
            "steady_state": m_ss,
        },
        "detections": {
            "stick_slip": d_stick,
            "reverse_hysteresis": d_rev,
            "low_supply": d_low,
            "incomplete_travel": d_incomplete,
            "dropouts": dropouts,
        },
        "thresholds": thresholds,
        "issues": issues,
        "exclusions": exclusion_records,
        "curves": {
            "t": [round(t, 3) for t in grid],
            "command": [round(v, 4) for v in cmd],
            "position": [round(v, 4) for v in pos],
            "pressure": [round(v, 2) for v in prs],
        },
    }
    analysis_id, version = db.create_analysis(test_id, author, cumulative, result)
    result["analysis_id"] = analysis_id
    result["version"] = version
    # 把 id/version 回写库存储
    db._conn.execute("UPDATE analyses SET result_json=? WHERE id=?",
                     (json.dumps(result), analysis_id))
    db._conn.commit()
    return db.get_analysis(analysis_id)
