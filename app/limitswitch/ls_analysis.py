"""限位开关（开/关到位接点）全行程诊断分析编排。

流程：接点状态规整（非法状态逐段留痕）→ 按提交/修订的接线与极性归一为
“到位有效”逻辑态 → 去抖提取有效动作区间与边沿 → 校正后连续阀位上定位
每次边沿的位置与运动方向 → 动作点/释放点/开关回差/边沿延迟/多循环离散度
→ 异常事件（非法状态、漏跳变、抖动、同时有效、次序反转、位置越窗、
到端未触发）逐条标注原始样本区间。

本模块只标识接点行为与阀位行程的一致性异常，**不形成维修结论**：
- 存在阻断性证据缺口（单位冲突、校准缺失/失效、通道不重叠、接点不可用）
  → no_conclusion；
- 存在异常事件或阈值检查不通过 → exceedances；
- 否则 → ok。

人工修订（改绑通道、纠正极性、忽略毛刺、改绑校准）必须填写理由，
每次修订另存为不可覆盖的新分析版本；检修前后比较仅接纳接线
（通道绑定+极性）与阈值版本完全一致的分析版本。
"""

import json
from datetime import datetime, timezone

from ..processing import alignment, units
from .. import calibration as calchain
from . import contacts as ct

EVENT_NAMES = {
    "illegal_state": "非法状态",
    "missed_transition": "漏跳变",
    "chatter": "抖动",
    "both_active": "开关触点同时有效",
    "order_reversal": "次序反转",
    "position_out_of_window": "位置越窗",
    "no_trigger_at_end": "到端未触发",
}

DIRECTION_NAMES = {1: "开向", -1: "关向", 0: "停留", None: "未知"}
END_OF = {"open": "high", "closed": "low"}
CONTACT_NAMES = {"open": "开到位", "closed": "关到位"}

BLOCKING_GAP_CODES = {"unit_conflict", "calibration_missing",
                      "calibration_expired", "channel_no_overlap",
                      "contact_unusable", "series_empty"}


def _parse_time(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _raw_ref(channel, points, index):
    if not points or index is None:
        return None
    k = min(max(0, index), len(points) - 1)
    return {"channel": channel, "index": k,
            "t": round(float(points[k][0]), 6), "value": points[k][1]}


def _edge_refs(channel, points, i_lo, i_hi):
    """边沿两侧各取一个原始点作为引用。"""
    n = len(points)
    refs = []
    for i in sorted({max(0, i_lo - 1), max(0, i_lo),
                     min(n - 1, i_hi), min(n - 1, i_hi + 1)}):
        refs.append(_raw_ref(channel, points, i))
    return [r for r in refs if r]


def _raw_interval(channel, points, i_lo, i_hi):
    if not points:
        return None
    lo = min(max(0, i_lo), len(points) - 1)
    hi = min(max(0, i_hi), len(points) - 1)
    return {"channel": channel, "start_index": lo, "end_index": hi,
            "t_start": round(float(points[lo][0]), 6),
            "t_end": round(float(points[hi][0]), 6)}


def _active_at(intervals, t):
    """t 是否落在某个有效区间内（含释放沿之前）。"""
    for iv in intervals:
        end = iv["release_t"] if iv["release_t"] is not None else float("inf")
        if iv["t_start"] - 1e-9 <= t < end:
            return True
    return False


def _pair_delay(t_edge, entries):
    """边沿相对最近一次进窗的延迟；边沿早于进窗（提前翻转）时为负值。"""
    before = [e for e in entries if e["t"] <= t_edge + 1e-9]
    if before:
        return round(t_edge - before[-1]["t"], 6)
    after = [e for e in entries if e["t"] > t_edge + 1e-9]
    if after:
        return round(t_edge - after[0]["t"], 6)
    return None


def run_limitswitch_analysis(db, ls_test_id, author="auto", new_adjustments=None,
                             bindings_override=None):
    """执行限位开关全行程诊断并保存新版本。"""
    test = db.get_limitswitch_test(ls_test_id)
    if test is None:
        raise KeyError(f"限位开关测试 {ls_test_id} 不存在")
    payload = test["payload"]
    thr = dict(payload.get("thresholds") or test["thresholds"])
    switch_spec = payload["switch"]
    new_adjustments = new_adjustments or {}

    # ---- 累计调整（上一版本 + 本次新增） ----
    existing = db.list_limitswitch_analyses(ls_test_id)
    cumulative = []
    prev_result = None
    if existing:
        prev = db.get_limitswitch_analysis(existing[-1]["id"])
        cumulative = list(prev["adjustments"])
        prev_result = prev["result"]
    if new_adjustments.get("channel_rebind"):
        cumulative.append({"type": "channel_rebind", "author": author,
                           **new_adjustments["channel_rebind"]})
    if new_adjustments.get("polarity_override"):
        cumulative.append({"type": "polarity_override", "author": author,
                           **new_adjustments["polarity_override"]})
    for ig in new_adjustments.get("ignore_glitches", []):
        cumulative.append({"type": "ignore_glitch", "author": author, **ig})
    if new_adjustments.get("calibration_bindings") is not None:
        cumulative.append(calchain.rebind_adjustment(
            author, new_adjustments["calibration_bindings"]))

    # ---- 有效接线（通道绑定 + 极性）与忽略毛刺清单 ----
    channel_map = {"open": "open", "closed": "closed"}
    polarity_over = {}
    ignore_list = []
    for a in cumulative:
        if a["type"] == "channel_rebind":
            channel_map = dict(a["channel_map"])
        elif a["type"] == "polarity_override":
            polarity_over.update(a["polarities"])
        elif a["type"] == "ignore_glitch":
            ignore_list.append(a)
    polarities = {"open": switch_spec["open"]["polarity"],
                  "closed": switch_spec["closed"]["polarity"]}
    polarities.update(polarity_over)
    debounce_s = float(switch_spec["debounce_s"])
    mutex = switch_spec.get("mutual_exclusion") or {}
    mutex_enabled = bool(mutex.get("enabled", True))
    max_overlap_s = float(mutex.get("max_overlap_s", 0.0))

    evidence_gaps, notes = [], []
    rng = payload["range"]

    # ---- 逐通道仪器校准链（先修正，再单位统一；接点不是测量通道） ----
    bindings = calchain.resolve_bindings(payload, prev_result, bindings_override)
    corrected, chain_block = calchain.apply_chain(
        db, test_kind="limitswitch", series=payload["series"], bindings=bindings,
        test_started_at=payload.get("test_started_at"),
        submitted_at=test["submitted_at"],
        range_min=rng["min"], range_max=rng["max"])
    series_for_units = payload["series"]
    if corrected is not None:
        series_for_units = {**payload["series"], **corrected}
    if chain_block["mode"] == "channel_chain":
        for r in chain_block["rejections"]:
            evidence_gaps.append({"code": r["code"], "detail": r["detail"],
                                  "channel": r["channel"],
                                  "raw_reading_interval": r["raw_reading_interval"]})

    # ---- 单位统一：连续阀位 → % ----
    pos_series = series_for_units["position"]
    pos_pct, n1, c1 = units.normalize_signal(
        pos_series["points"], pos_series["unit"],
        rng["min"], rng["max"], rng["unit"], "position")
    notes += n1
    for c in c1:
        evidence_gaps.append({"code": "unit_conflict", "detail": f"单位冲突：{c}"})
    pos_ts = [float(p[0]) for p in pos_series["points"]]
    pos_points = pos_series["points"]

    # ---- 校准有效期（legacy 模式） ----
    if chain_block["mode"] == "legacy":
        cal_until = _parse_time(payload.get("calibration_valid_until"))
        test_time = (_parse_time(payload.get("test_started_at"))
                     or _parse_time(test["submitted_at"]))
        if cal_until is None:
            evidence_gaps.append({"code": "calibration_missing",
                                  "detail": "未提供校准有效期，无法确认阀位仪表校准状态"})
        elif test_time and cal_until < test_time:
            evidence_gaps.append({
                "code": "calibration_expired",
                "detail": f"校准已于 {payload['calibration_valid_until']} 失效，"
                          "测试数据不得用于维修结论"})
    chain_block["legacy_calibration_valid_until"] = payload.get("calibration_valid_until")

    # ---- 两路接点：规整 → 极性 → 去抖 ----
    contacts_out, events = {}, []
    exclusions_out = []
    raw_bits = {}
    for logical in ("open", "closed"):
        raw_ch = channel_map[logical]
        raw_series = payload["series"].get(raw_ch)
        if raw_series is None or not raw_series.get("points"):
            evidence_gaps.append({
                "code": "contact_unusable",
                "detail": f"{CONTACT_NAMES[logical]}接点绑定的原始通道 "
                          f"{raw_ch!r} 无采样点，无法诊断"})
            raw_bits[logical] = (raw_ch, [], [], [], [])
            continue
        ts, bits, nts, cfs, illegal = ct.normalize_contact(
            raw_series["points"], raw_series.get("unit"), f"{logical}({raw_ch})")
        notes += nts
        for c in cfs:
            evidence_gaps.append({"code": "unit_conflict", "detail": f"单位冲突：{c}"})
        logical_bits = ct.apply_polarity(bits, polarities[logical])
        raw_points = raw_series["points"]
        for seg in illegal:
            events.append({
                "kind": "illegal_state", "contact": logical,
                "interval_s": [seg["t_start"], seg["t_end"]],
                "detail": f"{CONTACT_NAMES[logical]}接点原始通道 {raw_ch} 出现 "
                          f"{seg['n_samples']} 个非法电平采样（示值 "
                          f"{seg['sample_values']}），介于 0/1 阈值之外，"
                          "该段状态未知，不参与边沿提取",
                "raw_intervals": [{"channel": raw_ch,
                                   "start_index": seg["start_index"],
                                   "end_index": seg["end_index"],
                                   "t_start": seg["t_start"],
                                   "t_end": seg["t_end"]}],
                "raw_refs": [_raw_ref(raw_ch, raw_points, seg["start_index"]),
                             _raw_ref(raw_ch, raw_points, seg["end_index"])],
            })
        if not bits or all(b is None for b in bits):
            evidence_gaps.append({
                "code": "contact_unusable",
                "detail": f"{CONTACT_NAMES[logical]}接点通道 {raw_ch} 全部采样均为"
                          "非法状态，无有效逻辑电平"})
        adopted, glitches, chatters = ct.extract_intervals(ts, logical_bits, debounce_s)
        raw_bits[logical] = (raw_ch, ts, logical_bits, raw_points,
                             (adopted, glitches, chatters))
        contacts_out[logical] = {
            "raw_channel": raw_ch,
            "polarity": polarities[logical],
            "n_samples": len(ts),
            "n_illegal": len(illegal),
            "adopted_intervals": adopted,
            "glitches": glitches,
            "chatters": chatters,
        }

    # ---- 忽略毛刺（人工修订）：命中的有效区间/毛刺/抖动不再采用 ----
    for logical in ("open", "closed"):
        got = contacts_out.get(logical)
        if not got:
            continue
        igs = [a for a in ignore_list if a["contact"] == logical]
        if not igs:
            continue
        ignored = []

        def _hit(t0, t1):
            return any(t0 <= a["t_end"] + 1e-9 and a["t_start"] - 1e-9 <= t1
                       for a in igs)

        kept = []
        for iv in got["adopted_intervals"]:
            end = iv["release_t"] if iv["release_t"] is not None else iv["t_end"]
            if _hit(iv["t_start"], end):
                ignored.append({"kind": "interval", "t_start": iv["t_start"],
                                "t_end": end})
            else:
                kept.append(iv)
        got["adopted_intervals"] = kept
        kept_g = []
        for g in got["glitches"]:
            if _hit(g["t_start"], g["t_end"]):
                ignored.append({"kind": "glitch", "t_start": g["t_start"],
                                "t_end": g["t_end"]})
            else:
                kept_g.append(g)
        got["glitches"] = kept_g
        kept_c = []
        for ch in got["chatters"]:
            if _hit(ch["t_start"], ch["t_end"]):
                ignored.append({"kind": "chatter", "t_start": ch["t_start"],
                                "t_end": ch["t_end"]})
            else:
                kept_c.append(ch)
        got["chatters"] = kept_c
        for a in igs:
            exclusions_out.append({
                "type": "ignore_glitch", "contact": logical,
                "t_start": a["t_start"], "t_end": a["t_end"],
                "reason": a.get("reason", ""), "author": a.get("author", ""),
                "ignored": [dict(x) for x in ignored],
            })

    # ---- 抖动/毛刺事件（未忽略部分，留痕原始区间） ----
    for logical in ("open", "closed"):
        got = contacts_out.get(logical)
        if not got:
            continue
        raw_ch, _, _, raw_points, _ = raw_bits[logical]
        for ch in got["chatters"]:
            events.append({
                "kind": "chatter", "contact": logical,
                "interval_s": [ch["t_start"], ch["t_end"]],
                "detail": f"{CONTACT_NAMES[logical]}接点在 {ch['t_start']}–{ch['t_end']}s "
                          f"内出现 {ch['n_pulses']} 个脉冲（间隔 ≤ 去抖 "
                          f"{debounce_s}s），判定为触点抖动；采用簇首上升沿为有效动作",
                "raw_intervals": [_raw_interval(raw_ch, raw_points,
                                                ch["i_start"], ch["i_end"])],
                "raw_refs": _edge_refs(raw_ch, raw_points, ch["i_start"], ch["i_end"]),
            })
        for g in got["glitches"]:
            events.append({
                "kind": "chatter", "contact": logical,
                "interval_s": [g["t_start"], g["t_end"]],
                "detail": f"{CONTACT_NAMES[logical]}接点孤立毛刺脉冲，持续 "
                          f"{g['duration_s']}s 短于去抖 {debounce_s}s，"
                          "未采用为有效边沿",
                "raw_intervals": [_raw_interval(raw_ch, raw_points,
                                                g["i_start"], g["i_end"])],
                "raw_refs": _edge_refs(raw_ch, raw_points, g["i_start"], g["i_end"]),
            })

    # ---- 边沿提取：在校正后阀位上定位每次动作/释放 ----
    move_slope = float(thr.get("move_slope_pct_s", 0.5))
    edge_delay_max = float(thr.get("edge_delay_s_max", 1.0))
    end_dwell_s = float(thr.get("end_dwell_s", 1.0))
    dispersion_max = float(thr.get("dispersion_pct_max", 1.0))

    edges_out = {"open": [], "closed": []}
    for logical in ("open", "closed"):
        got = contacts_out.get(logical)
        if not got:
            continue
        raw_ch, ts_c, bits_c, raw_points, _ = raw_bits[logical]
        spec = switch_spec[logical]
        end = END_OF[logical]
        act_win = [float(spec["actuate_window_pct"][0]),
                   float(spec["actuate_window_pct"][1])]
        rel_win = [float(spec["release_window_pct"][0]),
                   float(spec["release_window_pct"][1])]
        act_entries = ct.window_entries(pos_ts, pos_pct, act_win[0], act_win[1], end) \
            if pos_pct else []
        # 释放窗：位置从端点方向进入（open 自上而下、closed 自下而上）
        rel_entries = ct.window_entries(
            pos_ts, pos_pct, rel_win[0], rel_win[1],
            "low" if end == "high" else "high") if pos_pct else []

        for iv in got["adopted_intervals"]:
            pair = [("actuate", iv["t_start"], iv["i_start"], act_win, act_entries)]
            if iv["release_t"] is not None:
                pair.append(("release", iv["release_t"], iv["release_i"],
                             rel_win, rel_entries))
            for kind, t_e, i_e, win, entries in pair:
                pos_e = ct.interp_at(pos_ts, pos_pct, t_e) if pos_pct else None
                direction = ct.direction_at(pos_ts, pos_pct, t_e, move_slope) \
                    if pos_pct else None
                in_window = None
                if pos_e is not None:
                    in_window = (win[0] - 1e-9) <= pos_e <= (win[1] + 1e-9)
                delay = _pair_delay(t_e, entries)
                edge = {
                    "contact": logical, "kind": kind,
                    "t_s": round(t_e, 6),
                    "position_pct": round(pos_e, 4) if pos_e is not None else None,
                    "direction": direction,
                    "direction_name": DIRECTION_NAMES[direction],
                    "window_pct": win,
                    "in_window": in_window,
                    "delay_s": delay,
                    "raw_refs": _edge_refs(raw_ch, raw_points, i_e, i_e),
                }
                edges_out[logical].append(edge)
                if pos_e is None:
                    evidence_gaps.append({
                        "code": "edge_outside_position_span",
                        "detail": f"{CONTACT_NAMES[logical]}接点 {kind} 边沿 "
                                  f"t={t_e:.3f}s 超出阀位记录区间 "
                                  f"[{pos_ts[0] if pos_ts else '—'}, "
                                  f"{pos_ts[-1] if pos_ts else '—'}]s，"
                                  "无法定位动作位置"})
                    continue
                # 位置越窗
                if in_window is False:
                    events.append({
                        "kind": "position_out_of_window", "contact": logical,
                        "interval_s": [round(t_e, 6), round(t_e, 6)],
                        "detail": f"{CONTACT_NAMES[logical]}接点{'动作' if kind == 'actuate' else '释放'}"
                                  f"边沿 t={t_e:.3f}s 时校正阀位 {pos_e:.2f}%，"
                                  f"超出声明{'动作' if kind == 'actuate' else '释放'}窗 "
                                  f"[{win[0]:g}, {win[1]:g}]%"
                                  + ("（触点提前翻转）" if delay is not None and delay < 0
                                     else ""),
                        "raw_intervals": [_raw_interval(raw_ch, raw_points, i_e, i_e)],
                        "raw_refs": edge["raw_refs"],
                    })
                # 次序反转：方向明显相反
                expected = 1 if (kind == "actuate") == (end == "high") else -1
                if direction is not None and direction == -expected:
                    events.append({
                        "kind": "order_reversal", "contact": logical,
                        "interval_s": [round(t_e, 6), round(t_e, 6)],
                        "detail": f"{CONTACT_NAMES[logical]}接点{'动作' if kind == 'actuate' else '释放'}"
                                  f"边沿 t={t_e:.3f}s 处阀位正向"
                                  f"{DIRECTION_NAMES[-expected]}运动"
                                  f"（{pos_e:.2f}%），与端点逻辑相反，"
                                  "疑似通道接反或极性声明错误",
                        "raw_intervals": [_raw_interval(raw_ch, raw_points, i_e, i_e)],
                        "raw_refs": edge["raw_refs"],
                    })
                # 次序反转：动作时另一接点仍有效
                if kind == "actuate":
                    other = "closed" if logical == "open" else "open"
                    other_got = contacts_out.get(other) or {}
                    if _active_at(other_got.get("adopted_intervals", []), t_e):
                        o_raw_ch, _, _, o_points, _ = raw_bits[other]
                        events.append({
                            "kind": "order_reversal", "contact": logical,
                            "interval_s": [round(t_e, 6), round(t_e, 6)],
                            "detail": f"{CONTACT_NAMES[logical]}接点动作时"
                                      f"{CONTACT_NAMES[other]}接点仍为有效状态，"
                                      "动作次序与行程逻辑相反，疑似通道接反",
                            "raw_intervals": [
                                _raw_interval(raw_ch, raw_points, i_e, i_e),
                                _raw_interval(o_raw_ch, o_points, 0,
                                              len(o_points) - 1)],
                            "raw_refs": edge["raw_refs"],
                        })

    # ---- 同时有效（互斥规则） ----
    if mutex_enabled:
        o_iv = (contacts_out.get("open") or {}).get("adopted_intervals", [])
        c_iv = (contacts_out.get("closed") or {}).get("adopted_intervals", [])
        o_ch, _, _, o_pts, _ = raw_bits.get("open", ("open", [], [], [], None))
        c_ch, _, _, c_pts, _ = raw_bits.get("closed", ("closed", [], [], [], None))
        for a in o_iv:
            a_end = a["release_t"] if a["release_t"] is not None else float("inf")
            for b in c_iv:
                b_end = b["release_t"] if b["release_t"] is not None else float("inf")
                lo = max(a["t_start"], b["t_start"])
                hi = min(a_end, b_end)
                if hi - lo > max_overlap_s + 1e-9:
                    hi_disp = hi if hi != float("inf") else max(
                        raw_bits["open"][1][-1] if raw_bits["open"][1] else 0.0,
                        raw_bits["closed"][1][-1] if raw_bits["closed"][1] else 0.0)
                    events.append({
                        "kind": "both_active", "contact": "both",
                        "interval_s": [round(lo, 6), round(hi_disp, 6)],
                        "detail": f"开到位与关到位接点在 {lo:.3f}–{hi_disp:.3f}s "
                                  f"同时有效（交叠 {hi - lo:.3f}s，允许 "
                                  f"{max_overlap_s}s），违反互斥规则，"
                                  "联锁输入不可信",
                        "raw_intervals": [
                            _raw_interval(o_ch, o_pts, a["i_start"], a["i_end"]),
                            _raw_interval(c_ch, c_pts, b["i_start"], b["i_end"])],
                        "raw_refs": _edge_refs(o_ch, o_pts, a["i_start"], a["i_end"])
                        + _edge_refs(c_ch, c_pts, b["i_start"], b["i_end"]),
                    })

    # ---- 到端未触发 / 漏跳变 ----
    if pos_pct:
        for logical in ("open", "closed"):
            got = contacts_out.get(logical)
            if not got:
                continue
            raw_ch, ts_c, bits_c, raw_points, _ = raw_bits[logical]
            if not ts_c:
                continue
            spec = switch_spec[logical]
            end = END_OF[logical]
            act_win = [float(spec["actuate_window_pct"][0]),
                       float(spec["actuate_window_pct"][1])]
            rel_win = [float(spec["release_window_pct"][0]),
                       float(spec["release_window_pct"][1])]
            adopted = got["adopted_intervals"]
            act_edges = [e for e in edges_out[logical] if e["kind"] == "actuate"]
            rel_edges = [e for e in edges_out[logical] if e["kind"] == "release"]

            # 到端未触发：位置进入动作窗并停留 ≥ end_dwell_s，无动作边沿
            for en in ct.window_entries(pos_ts, pos_pct, act_win[0], act_win[1], end):
                dwell, j_end = ct.dwell_from(pos_ts, pos_pct, en["index"],
                                             act_win[0], act_win[1])
                if dwell < end_dwell_s:
                    continue
                if ct.state_at(ts_c, bits_c, en["t"]) == 1:
                    continue  # 进窗时已有效（如记录起点即在端点）
                t0, t1 = en["t"] - debounce_s, en["t"] + edge_delay_max
                if any(t0 - 1e-9 <= e["t_s"] <= t1 + 1e-9 for e in act_edges):
                    continue
                events.append({
                    "kind": "no_trigger_at_end", "contact": logical,
                    "interval_s": [round(en["t"], 6), round(pos_ts[j_end], 6)],
                    "detail": f"校正阀位于 {en['t']:.3f}s 进入"
                              f"{CONTACT_NAMES[logical]}动作窗 "
                              f"[{act_win[0]:g}, {act_win[1]:g}]% 并停留 "
                              f"{dwell:.2f}s（≥{end_dwell_s}s），接点始终未动作",
                    "raw_intervals": [_raw_interval("position", pos_points,
                                                    en["index"], j_end)],
                    "raw_refs": [_raw_ref("position", pos_points, en["index"]),
                                 _raw_ref("position", pos_points, j_end)],
                })

            # 漏跳变：位置穿出释放窗远离端点，接点仍有效且无释放边沿
            for ex in ct.window_exits(pos_ts, pos_pct, rel_win[0], rel_win[1], end):
                if ct.state_at(ts_c, bits_c, ex["t"]) != 1:
                    continue
                t0, t1 = ex["t"] - debounce_s, ex["t"] + edge_delay_max
                if any(t0 - 1e-9 <= e["t_s"] <= t1 + 1e-9 for e in rel_edges):
                    continue
                events.append({
                    "kind": "missed_transition", "contact": logical,
                    "interval_s": [round(ex["t"], 6), round(ex["t"], 6)],
                    "detail": f"校正阀位于 {ex['t']:.3f}s 穿出"
                              f"{CONTACT_NAMES[logical]}释放窗 "
                              f"[{rel_win[0]:g}, {rel_win[1]:g}]% 远离端点，"
                              "接点仍保持有效且未观察到释放边沿（触点粘连嫌疑）",
                    "raw_intervals": [_raw_interval("position", pos_points,
                                                    max(0, ex["index"] - 1),
                                                    ex["index"])],
                    "raw_refs": [_raw_ref("position", pos_points, ex["index"])],
                })
            # 记录结束仍有效且阀位已在释放窗之外
            if bits_c and bits_c[-1] == 1 and pos_pct:
                p_end = pos_pct[-1]
                if not (rel_win[0] - 1e-9 <= p_end <= rel_win[1] + 1e-9) \
                        and not (act_win[0] - 1e-9 <= p_end <= act_win[1] + 1e-9):
                    events.append({
                        "kind": "missed_transition", "contact": logical,
                        "interval_s": [round(pos_ts[-1], 6), round(pos_ts[-1], 6)],
                        "detail": f"记录结束时{CONTACT_NAMES[logical]}接点仍有效，"
                                  f"但校正阀位 {p_end:.2f}% 已离开动作/释放窗，"
                                  "释放跳变缺失",
                        "raw_intervals": [
                            _raw_interval(raw_ch, raw_points,
                                          max(0, len(raw_points) - 2),
                                          len(raw_points) - 1),
                            _raw_interval("position", pos_points,
                                          max(0, len(pos_points) - 2),
                                          len(pos_points) - 1)],
                        "raw_refs": [_raw_ref(raw_ch, raw_points,
                                              len(raw_points) - 1),
                                     _raw_ref("position", pos_points,
                                              len(pos_points) - 1)],
                    })

    # ---- 指标：动作点/释放点/回差/边沿延迟/多循环离散度 ----
    checks = []
    for logical in ("open", "closed"):
        got = contacts_out.get(logical)
        if not got:
            continue
        edges = edges_out[logical]
        act = [e for e in edges if e["kind"] == "actuate" and e["position_pct"] is not None]
        rel = [e for e in edges if e["kind"] == "release" and e["position_pct"] is not None]
        act_pts = [e["position_pct"] for e in act]
        rel_pts = [e["position_pct"] for e in rel]
        # 开关回差：同一有效区间的动作边沿 → 其后的释放边沿
        diffs = []
        for i, a in enumerate(act):
            nxt = next((r for r in rel if r["t_s"] > a["t_s"]), None)
            if nxt is not None:
                diffs.append(round(abs(a["position_pct"] - nxt["position_pct"]), 4))
        act_delays = [e["delay_s"] for e in act if e["delay_s"] is not None]
        rel_delays = [e["delay_s"] for e in rel if e["delay_s"] is not None]
        disp_act = round(max(act_pts) - min(act_pts), 4) if len(act_pts) >= 2 else None
        disp_rel = round(max(rel_pts) - min(rel_pts), 4) if len(rel_pts) >= 2 else None
        got["edges"] = edges
        got["metrics"] = {
            "n_actuate_edges": len(act),
            "n_release_edges": len(rel),
            "actuation_points_pct": [round(v, 4) for v in act_pts],
            "release_points_pct": [round(v, 4) for v in rel_pts],
            "differentials_pct": diffs,
            "actuation_delays_s": [round(v, 4) for v in act_delays],
            "release_delays_s": [round(v, 4) for v in rel_delays],
            "mean_actuation_pct": round(sum(act_pts) / len(act_pts), 4) if act_pts else None,
            "mean_release_pct": round(sum(rel_pts) / len(rel_pts), 4) if rel_pts else None,
            "mean_differential_pct": round(sum(diffs) / len(diffs), 4) if diffs else None,
            "actuation_dispersion_pct": disp_act,
            "release_dispersion_pct": disp_rel,
            "max_edge_delay_s": round(max(act_delays + rel_delays), 4)
            if (act_delays or rel_delays) else None,
        }
        # 阈值检查
        delays = act_delays + rel_delays
        if delays:
            late = max(delays)
            checks.append({
                "contact": logical, "metric": "edge_delay_s",
                "value": round(late, 4), "threshold_max": edge_delay_max,
                "pass": late <= edge_delay_max + 1e-9,
                "basis": f"{CONTACT_NAMES[logical]}接点各边沿相对位置进窗时刻的"
                         "最大延迟（负值为提前翻转）",
            })
        if disp_act is not None:
            checks.append({
                "contact": logical, "metric": "actuation_dispersion_pct",
                "value": disp_act, "threshold_max": dispersion_max,
                "pass": disp_act <= dispersion_max + 1e-9,
                "basis": f"{CONTACT_NAMES[logical]}接点 {len(act_pts)} 次循环动作点"
                         "峰峰离散度",
            })
        if disp_rel is not None:
            checks.append({
                "contact": logical, "metric": "release_dispersion_pct",
                "value": disp_rel, "threshold_max": dispersion_max,
                "pass": disp_rel <= dispersion_max + 1e-9,
                "basis": f"{CONTACT_NAMES[logical]}接点 {len(rel_pts)} 次循环释放点"
                         "峰峰离散度",
            })

    # ---- 展示曲线（异频对齐到公共网格；接点逻辑态前向填充非法段） ----
    curves, align_meta = None, None
    unit_conflict = any(g["code"] == "unit_conflict" for g in evidence_gaps)
    if pos_pct and not unit_conflict:
        raw_align = {"position": (pos_ts, pos_pct)}
        for logical in ("open", "closed"):
            raw_ch, ts_c, bits_c, _, _ = raw_bits.get(logical, (logical, [], [], [], None))
            if not ts_c:
                continue
            filled, last = [], 0.0
            for b in bits_c:
                if b is None:
                    filled.append(last)
                else:
                    last = float(b)
                    filled.append(last)
            raw_align[logical] = (ts_c, filled)
        try:
            grid, aligned, align_meta = alignment.align(
                raw_align, channels=tuple(raw_align.keys()))
            curves = {
                "t": [round(t, 3) for t in grid],
                "position_pct": [round(v, 4) for v in aligned["position"]],
            }
            for logical in ("open", "closed"):
                if logical in aligned:
                    curves[logical] = [int(round(v)) for v in aligned[logical]]
        except ValueError as e:
            evidence_gaps.append({"code": "channel_no_overlap",
                                  "detail": f"阀位与接点时间轴不重叠：{e}"})

    # ---- 判定 ----
    blocking_codes = BLOCKING_GAP_CODES | calchain.BLOCKING_REJECT_CODES
    blocking = [g for g in evidence_gaps if g["code"] in blocking_codes]
    if blocking:
        verdict = "no_conclusion"
    elif events or any(not c.get("pass", False) for c in checks):
        verdict = "exceedances"
    else:
        verdict = "ok"

    # ---- 判定依据 ----
    basis = []
    for c in checks:
        line = (f"{CONTACT_NAMES[c['contact']]} {c['metric']}：实测 {c['value']}"
                f"，阈值 ≤ {c['threshold_max']}，判定 "
                f"{'通过' if c.get('pass') else '不通过'}（依据：{c['basis']}）")
        basis.append(line)
    n_edges = sum(len(edges_out[k]) for k in edges_out)
    basis.append(f"采用有效边沿 {n_edges} 个（去抖 {debounce_s}s，"
                 f"互斥规则{'启用' if mutex_enabled else '停用'}），"
                 f"异常事件 {len(events)} 条（均标注原始样本区间，不构成维修结论）")

    result = {
        "test_id": ls_test_id,
        "valve_id": test["valve_id"],
        "verdict": verdict,
        "conclusion_scope": "仅标识接点行为与阀位行程的一致性异常区间，不构成维修结论",
        "switch": switch_spec,
        "wiring": {"channel_map": channel_map, "polarities": polarities},
        "thresholds": thr,
        "conditions": test["conditions"],
        "unit_notes": notes,
        "evidence_gaps": evidence_gaps,
        "calibration_chain": chain_block,
        "contacts": contacts_out,
        "events": events,
        "checks": checks,
        "adopted_edges": [e for k in ("open", "closed") for e in edges_out[k]],
        "alignment": align_meta,
        "curves": curves,
        "exclusions": exclusions_out,
        "decision_basis": basis,
        "compatibility": {
            "wiring": {"channel_map": channel_map, "polarities": polarities},
            "switch": switch_spec,
            "thresholds": thr,
        },
    }

    analysis_id, version = db.create_limitswitch_analysis(
        ls_test_id, author, cumulative, result)
    result["analysis_id"] = analysis_id
    result["version"] = version
    db._conn.execute("UPDATE limitswitch_analyses SET result_json=? WHERE id=?",
                     (json.dumps(result), analysis_id))
    db._conn.commit()
    return db.get_limitswitch_analysis(analysis_id)
