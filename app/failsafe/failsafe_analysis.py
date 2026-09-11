"""故障安全动作测试分析编排。

流程：接点规整 → 跳闸定位（抖动/重复触发）→ 跳闸前基线覆盖检查 →
四路异频对齐 → 指令先行量化 → 动作指标（延迟/T90/最终位置/反弹/停滞/压力衰减）
→ 证据缺口与判定。人工调整跳闸点或观察窗派生新版本，累计保留调整理由
与原始采样引用。

判定：
- 存在证据缺口（跳闸前数据不足、通道不重叠、方向错误、窗末未稳定、
  校准失效、单位冲突、动作窗断档等）→ no_conclusion，不得给出通过结论；
- 无缺口但任一阈值检查不通过 → fail；
- 全部检查通过 → pass。
"""

import json
from datetime import datetime, timezone

from ..processing import alignment, detection, units
from . import events, motion


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


def _rising_edges(ts, bits):
    return [k for k in range(1, len(bits)) if bits[k - 1] == 0 and bits[k] == 1]


def _coverage(ts, lo, hi):
    """区间 [lo,hi) 内原始采样相对中位间隔期望点数的覆盖率。

    期望点数直接按区间长度/中位间隔估计（区间不与整周期对齐时不+1，
    避免短窗被边界点抬高期望）。
    """
    if hi <= lo or not ts:
        return 0.0
    dts = sorted(b - a for a, b in zip(ts, ts[1:]) if b - a > 0)
    dt_med = dts[(len(dts) - 1) // 2] if dts else (hi - lo)
    expected = max(1.0, (hi - lo) / dt_med)
    n = sum(1 for t in ts if lo <= t < hi)
    return min(1.0, n / expected)


def _dropouts_in_window(raw_series, trip_time, window_end, coverage_min=0.8):
    """动作窗内的采样断档；阀位缺口/冻结覆盖动作窗比例过大时构成证据缺口。"""
    events_out = detection.detect_dropouts(raw_series)
    win_len = max(window_end - trip_time, 1e-9)
    pos_lost = 0.0
    for ev in events_out:
        lo = max(ev["t_start"], trip_time)
        hi = min(ev["t_end"], window_end)
        if hi > lo and ev["channel"] == "position":
            pos_lost += hi - lo
    gap = None
    if pos_lost / win_len > 1 - coverage_min:
        gap = {"code": "dropout_in_action_window",
               "detail": f"动作窗内阀位断档/冻结覆盖 {pos_lost / win_len * 100:.0f}%"
                         f"（上限 {(1 - coverage_min) * 100:.0f}%），动作曲线不可信"}
    return events_out, gap


def run_failsafe_analysis(db, fs_test_id, author="auto", new_adjustments=None):
    test = db.get_failsafe_test(fs_test_id)
    if test is None:
        raise KeyError(f"故障安全测试 {fs_test_id} 不存在")
    payload = test["payload"]
    thresholds = dict(payload.get("thresholds") or test["thresholds"])
    new_adjustments = new_adjustments or {}

    # ---- 累计调整（上一版本 + 本次新增） ----
    existing = db.list_failsafe_analyses(fs_test_id)
    cumulative = []
    if existing:
        prev = db.get_failsafe_analysis(existing[-1]["id"])
        cumulative = list(prev["adjustments"])
    if new_adjustments.get("trip_move"):
        cumulative.append({"type": "trip_move", "author": author,
                           **new_adjustments["trip_move"]})
    if new_adjustments.get("window_move"):
        cumulative.append({"type": "window_move", "author": author,
                           **new_adjustments["window_move"]})
    manual_trip = next((a for a in reversed(cumulative) if a["type"] == "trip_move"), None)
    manual_window = next((a for a in reversed(cumulative) if a["type"] == "window_move"), None)

    evidence_gaps = []
    notes = []
    rng = payload["range"]

    # ---- 单位统一：指令/阀位/压力 ----
    norm = units.normalize_series(payload["series"], rng["min"], rng["max"], rng["unit"])
    raw = {c: norm[c] for c in ("command", "position", "pressure")}
    notes += norm["notes"]
    for c in norm["conflicts"]:
        evidence_gaps.append({"code": "unit_conflict", "detail": f"单位冲突：{c}"})

    # ---- 跳闸接点规整 ----
    trip_pts = payload["series"]["trip"]["points"]
    tts, bits, tnotes, tconf = events.normalize_trip(
        trip_pts, payload["series"]["trip"]["unit"])
    notes += tnotes
    for c in tconf:
        evidence_gaps.append({"code": "unit_conflict", "detail": f"单位冲突：{c}"})

    # ---- 校准有效期 ----
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

    # ---- 跳闸定位 ----
    trip_info = events.locate_trip(tts, bits, trip_pts,
                                   thresholds["chatter_merge_s"],
                                   thresholds["pre_trip_margin_s"])
    trip_time = trip_info["trip_time"]
    trip_source = "auto"
    trip_verified = True

    result_base = {
        "test_id": fs_test_id,
        "valve_id": test["valve_id"],
        "fail_mode": payload["fail_mode"],
        "actuator_type": payload.get("conditions", {}).get("actuator_type", "spring_return"),
        "thresholds": thresholds,
        "conditions": test["conditions"],
        "unit_notes": notes,
    }

    if manual_trip and trip_time is not None:
        t_man = float(manual_trip["new_trip_time"])
        edges = _rising_edges(tts, bits)
        dt_med = alignment.median_dt(tts) if len(tts) > 1 else 0.0
        bracketed = any(tts[k - 1] - dt_med <= t_man <= tts[k] + dt_med for k in edges)
        trip_time = round(t_man, 6)
        trip_source = "manual"
        trip_verified = bracketed
        if not bracketed:
            evidence_gaps.append({
                "code": "manual_trip_unverified",
                "detail": f"人工跳闸点 {t_man:.3f}s 附近无接点上升沿可佐证（搜索容差 "
                          f"{dt_med:.3f}s），该跳闸时刻无法由原始采样证实",
            })

    window_len_s = float(manual_window["window_end_s"]) if manual_window \
        else float(payload["observation_window_s"])
    if window_len_s <= 0:
        raise ValueError("观察窗长度必须为正")
    window_manual = manual_window is not None

    metrics, checks, issues, adopted, dropouts_out = {}, [], [], [], []
    align_meta = None
    curves = None
    t_loss = None
    lead = None
    cmd_base = None

    if trip_time is None:
        evidence_gaps.append({"code": "no_trip_observed",
                              "detail": "记录中未定位到任何有效跳闸沿，无法评估安全动作"})

    if trip_time is not None and not any(g["code"] == "unit_conflict" for g in evidence_gaps):
        raw["trip"] = (tts, [float(b) if b is not None else None for b in bits])

        # ---- 跳闸前基线覆盖（阀位/压力；指令仅用于先行量，不阻断） ----
        pre_lo = trip_time - thresholds["baseline_min_s"]
        for ch, name in (("position", "阀位"), ("pressure", "执行器压力")):
            cov = _coverage(raw[ch][0], pre_lo, trip_time)
            if cov < thresholds["baseline_coverage_min"]:
                evidence_gaps.append({
                    "code": "insufficient_pre_trip_data",
                    "detail": f"跳闸前 {thresholds['baseline_min_s']}s 内{name}采样覆盖率 "
                              f"{cov * 100:.0f}%，低于要求的 "
                              f"{thresholds['baseline_coverage_min'] * 100:.0f}%，基线不可靠",
                })

        # ---- 四路异频对齐 ----
        try:
            grid, aligned, align_meta = alignment.align(
                raw, channels=("trip", "command", "position", "pressure"))
        except ValueError as e:
            evidence_gaps.append({"code": "channel_no_overlap",
                                  "detail": f"各通道时间轴不重叠：{e}"})
            grid = None

        if grid is not None:
            window_end_req = trip_time + window_len_s
            if grid[-1] < trip_time:
                evidence_gaps.append({"code": "channel_no_overlap",
                                      "detail": "公共时间轴在跳闸沿之前结束，跳闸后无重叠数据"})
            else:
                # ---- 指令先行 ----
                cmd_ts, cmd_vals = raw["command"]
                t_loss, lead, cmd_base = events.command_loss_time(
                    cmd_ts, cmd_vals, trip_time, thresholds["command_loss_pct"])

                # ---- 动作窗断档 ----
                dropouts_out, dgap = _dropouts_in_window(
                    {c: raw[c] for c in ("command", "position", "pressure")},
                    trip_time, window_end_req, thresholds["baseline_coverage_min"])
                if dgap:
                    evidence_gaps.append(dgap)

                # ---- 动作指标 ----
                mout = motion.analyze_motion(
                    grid, aligned["position"], aligned["pressure"],
                    payload["fail_mode"],
                    payload.get("conditions", {}).get("actuator_type", "spring_return"),
                    trip_time, window_len_s, thresholds)
                metrics = mout["metrics"]
                issues = mout["issues"]
                checks = mout["checks"]
                adopted = mout["adopted_intervals"]
                evidence_gaps += mout["evidence_gaps"]
                if "pressure_note" in mout:
                    notes.append(mout["pressure_note"])

                # 采用区间：指令先行
                if t_loss is not None:
                    adopted.append({
                        "name": "command_loss_lead",
                        "interval_s": [t_loss, round(trip_time, 3)],
                        "method": f"指令相对跳闸前基线 {cmd_base}% 偏离 ≥"
                                  f"{thresholds['command_loss_pct']}% 的首点到有效跳闸沿",
                        "value": lead,
                    })

                curves = {
                    "t": [round(t, 3) for t in grid],
                    "trip": [int(round(v)) if v is not None else None for v in aligned["trip"]],
                    "command": [round(v, 4) for v in aligned["command"]],
                    "position": [round(v, 4) for v in aligned["position"]],
                    "pressure": [round(v, 2) for v in aligned["pressure"]],
                }
    elif trip_time is not None:
        evidence_gaps.append({"code": "channel_no_overlap",
                              "detail": "单位冲突未解算前不进行时间对齐与指标计算"})

    # ---- 判定 ----
    if evidence_gaps:
        verdict = "no_conclusion"
    elif any(not c.get("pass", False) for c in checks) or issues:
        verdict = "fail"
    else:
        verdict = "pass"

    # ---- 判定依据（逐项：数值 / 阈值 / 采用区间） ----
    adopted_by_name = {}
    for a in adopted:
        adopted_by_name.setdefault(a["name"], a)
    basis = []
    for c in checks:
        line = f"{c['metric']}："
        if c.get("value") is not None:
            line += f"实测 {c['value']}"
        if "threshold_max" in c:
            line += f"，阈值 ≤ {c['threshold_max']}"
        if "threshold_min" in c:
            line += f"，阈值 ≥ {c['threshold_min']}"
        ref = adopted_by_name.get(c["metric"])
        if ref and ref.get("interval_s"):
            line += f"，采用区间 {ref['interval_s'][0]}–{ref['interval_s'][1]}s"
        line += f"，判定 {'通过' if c.get('pass') else '不通过'}"
        if c.get("basis"):
            line += f"（依据：{c['basis']}）"
        basis.append(line)

    result = {
        **result_base,
        "verdict": verdict,
        "evidence_gaps": evidence_gaps,
        "trip": {
            "trip_time_s": round(trip_time, 3) if trip_time is not None else None,
            "source": trip_source,
            "verified": trip_verified,
            "initial_state": trip_info.get("initial_state"),
            "chatter": trip_info.get("chatter", []),
            "retriggers": trip_info.get("retriggers", []),
            "edge_references": trip_info.get("edge_references", []),
            "command_loss_time_s": t_loss,
            "command_loss_lead_s": lead,
            "command_baseline_pct": cmd_base,
        },
        "adopted_window": ({
            "start_s": round(trip_time, 3),
            "end_s": metrics.get("adopted_window_end_s"),
            "length_s": window_len_s,
            "manual": window_manual,
            "reason": manual_window.get("reason", "") if manual_window else "",
        } if trip_time is not None else None),
        "metrics": metrics,
        "checks": checks,
        "issues": issues,
        "adopted_intervals": adopted,
        "dropouts": dropouts_out,
        "alignment": align_meta,
        "curves": curves,
        "decision_basis": basis,
    }

    analysis_id, version = db.create_failsafe_analysis(fs_test_id, author, cumulative, result)
    result["analysis_id"] = analysis_id
    result["version"] = version
    db._conn.execute("UPDATE failsafe_analyses SET result_json=? WHERE id=?",
                     (json.dumps(result), analysis_id))
    db._conn.commit()
    return db.get_failsafe_analysis(analysis_id)
