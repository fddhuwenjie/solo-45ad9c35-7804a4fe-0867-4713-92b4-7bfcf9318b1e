"""在线部分行程测次的检修前后/多测次比较。

只有**阀内件（trim）、冻结起点、动作方向、目标行程完全一致**的测次才允许
定量比较；不一致逐条给出不可比原因。存在阻断性证据缺口（no_conclusion）
的版本不参与数值比较。指标按测次时间排序，给出起步延迟、有效行程、平均
速度、超调、保持漂移、返回时间与残余偏差的趋势判定。比较结果同时汇总
逐通道校准链差异。
"""

from .. import calibration as calchain

# (指标键, 名称, 单位, 劣化方向, 相对容差, 绝对容差)
TREND_METRICS = [
    ("breakaway_delay_s", "起步延迟", "s", 1, 0.20, 0.2),
    ("effective_travel_pct", "有效行程", "%", -1, 0.10, 0.5),
    ("avg_speed_pct_s", "动作平均速度", "%/s", -1, 0.20, 0.2),
    ("overshoot_pct", "超调", "%", 1, 0.20, 0.3),
    ("hold_drift_pct", "保持漂移", "%", 1, 0.20, 0.3),
    ("return_time_s", "返回时间", "s", 1, 0.20, 0.5),
    ("residual_pct", "残余偏差", "%", 1, 0.20, 0.3),
]

# 与分析编排一致的阻断性证据缺口（防御性复核；主判定依据 verdict）
BLOCKING_GAP_CODES = {"unit_conflict", "calibration_missing",
                      "calibration_expired", "channel_no_overlap",
                      "series_empty", "permit_missing",
                      "abort_contradiction", "test_aborted",
                      "pv_out_of_bounds", "travel_over_limit",
                      "no_command_step", "insufficient_baseline",
                      "start_position_mismatch"}


def _metric(result, key):
    return (result.get("metrics") or {}).get(key)


def _diff_detail(name, a, b):
    if a == b:
        return None
    return f"{name} 不一致：{a!r} vs {b!r}"


def _compat_reasons(ref_t, test):
    """相对基准测次的不可比原因（阀内件/起点/方向/目标行程须一致）。"""
    reasons = []
    spec_a, spec_b = ref_t.get("spec") or {}, test.get("spec") or {}
    d = _diff_detail("阀内件", ref_t.get("trim"), test.get("trim"))
    if d:
        reasons.append(d + "，阀内件不同的测次不得对比")
    d = _diff_detail("冻结起点", spec_a.get("start_pct"), spec_b.get("start_pct"))
    if d:
        reasons.append(d + "，起点不同的测次不得对比")
    d = _diff_detail("动作方向", spec_a.get("direction"), spec_b.get("direction"))
    if d:
        reasons.append(d + "，方向不同的测次不得对比")
    d = _diff_detail("目标行程", spec_a.get("target_travel_pct"),
                     spec_b.get("target_travel_pct"))
    if d:
        reasons.append(d + "，目标行程不同的测次不得对比")
    return reasons


def _trend_entry(key, name, unit, pts, worse_dir, rel_tol, abs_tol):
    entry = {"metric": key, "name": name, "unit": unit, "points": pts,
             "status": "inconclusive"}
    vals = [p["value"] for p in pts if p["value"] is not None]
    if len(vals) < 2:
        entry["note"] = "可比数据点不足 2 个"
        return entry
    first, last = vals[0], vals[-1]
    scale = max(abs(first), 1e-9)
    delta = last - first
    worse = delta * worse_dir
    if worse > abs_tol and worse > rel_tol * scale:
        entry["status"] = "degrading"
    elif worse < -abs_tol and worse < -rel_tol * scale:
        entry["status"] = "improving"
    else:
        entry["status"] = "stable"
    entry.update({"delta": round(delta, 6), "first": first, "last": last})
    return entry


def compare_pst_tests(db, analysis_ids=None, valve_tag=None):
    """比较多次部分行程测次。指定 analysis_ids 时以其为准，否则取每测次最新版本。"""
    items = []
    if analysis_ids:
        for aid in analysis_ids:
            a = db.get_pst_analysis(aid)
            if a is None:
                raise KeyError(f"部分行程分析 {aid} 不存在")
            t = db.get_pst_test(a["pst_test_id"])
            items.append((t, a, a["result"]))
    else:
        valve = db.get_valve_by_tag(valve_tag)
        if valve is None:
            raise KeyError(f"阀门 {valve_tag} 不存在")
        for tid, aid, _tt in db.latest_pst_analysis_ids(valve["id"]):
            a = db.get_pst_analysis(aid)
            t = db.get_pst_test(tid)
            items.append((t, a, a["result"]))

    if not items:
        return {"compatible": False, "series": [], "trends": [],
                "incomparable": [{"reason": "没有可用于比较的分析版本"}],
                "overall": "inconclusive"}

    items.sort(key=lambda it: (it[0].get("test_started_at") or it[0]["submitted_at"],
                               it[0]["id"]))
    ref_t, ref_a, ref_r = items[0]
    valve = db.get_valve(ref_t["valve_id"])

    series, incomparable = [], []
    for t, a, r in items:
        entry = {
            "test_id": t["id"], "analysis_id": a["id"], "version": a["version"],
            "test_started_at": t.get("test_started_at"),
            "phase": t["phase"], "trim": t.get("trim"),
            "verdict": r.get("verdict"),
            "breakaway_delay_s": _metric(r, "breakaway_delay_s"),
            "effective_travel_pct": _metric(r, "effective_travel_pct"),
            "return_time_s": _metric(r, "return_time_s"),
            "residual_pct": _metric(r, "residual_pct"),
            "comparable": True, "reasons": [],
        }
        if t["valve_id"] != ref_t["valve_id"]:
            entry["reasons"].append("不属于同一台阀门")
        if (t["id"], a["id"]) != (ref_t["id"], ref_a["id"]):
            entry["reasons"] += _compat_reasons(ref_t, t)
        blocking = [g for g in r.get("evidence_gaps", [])
                    if g.get("code") in BLOCKING_GAP_CODES]
        if r.get("verdict") == "no_conclusion" or blocking:
            gaps = "；".join(sorted({g.get("code") for g in r.get("evidence_gaps", [])}))
            entry["reasons"].append(f"该版本存在证据缺口（{gaps}），数值不参与比较")
        chain = r.get("calibration_chain") or {}
        if chain.get("mode") == "channel_chain":
            entry["calibration"] = {
                "accepted": chain.get("accepted"),
                "certificates": [
                    {"channel": c["channel"],
                     "instrument_serial": c.get("instrument_serial"),
                     "calibration_version_id": c.get("calibration_version_id"),
                     "certificate_digest": c.get("certificate_digest"),
                     "mean_correction": c.get("mean_correction"),
                     "status": c.get("status"),
                     "rejection_code": c.get("rejection_code")}
                    for c in chain.get("channels", [])],
            }
            if not chain.get("accepted"):
                entry["reasons"].append(
                    "该版本存在被拒绝的校准通道（"
                    + "、".join(f"{x['channel']}:{x['code']}"
                               for x in chain.get("rejections", []))
                    + "），数值不参与比较")
        entry["comparable"] = not entry["reasons"]
        if not entry["comparable"]:
            incomparable.append({"test_id": t["id"], "analysis_id": a["id"],
                                 "reasons": entry["reasons"]})
        series.append(entry)

    comparable_ids = {e["analysis_id"] for e in series if e["comparable"]}
    trends = []
    for key, name, unit, worse_dir, rel_tol, abs_tol in TREND_METRICS:
        pts = [{"test_id": t["id"], "analysis_id": a["id"],
                "test_started_at": t.get("test_started_at"),
                "value": _metric(r, key)}
               for t, a, r in items if a["id"] in comparable_ids]
        trends.append(_trend_entry(key, name, unit, pts, worse_dir,
                                   rel_tol, abs_tol))

    statuses = [t["status"] for t in trends]
    if len(comparable_ids) < 2:
        overall = "inconclusive"
    elif any(s == "degrading" for s in statuses):
        overall = "degrading"
    elif all(s in ("stable", "inconclusive") for s in statuses):
        overall = "stable"
    else:
        overall = "improving" if any(s == "improving" for s in statuses) else "mixed"

    return {
        "valve_tag": valve["tag"] if valve else None,
        "valve_id": ref_t["valve_id"],
        "compatible": len(incomparable) == 0 and len(comparable_ids) >= 2,
        "calibration_comparison": calchain.compare_chains(ref_r, items[-1][2]),
        "frozen_conditions": {
            "reference_test_id": ref_t["id"],
            "trim": ref_t.get("trim"),
            "spec": ref_t.get("spec"),
        },
        "series": series,
        "trends": trends,
        "incomparable": incomparable,
        "overall": overall,
    }
