"""限位开关诊断的检修前后/多测次比较。

只有**接线（通道绑定 + 极性）与阈值版本（接点规格窗、去抖、互斥规则、
判定阈值）完全一致**的分析版本才允许定量比较；不一致逐条给出不可比原因。
存在阻断性证据缺口（no_conclusion）的版本不参与数值比较。
指标按测试时间排序，给出动作点/释放点漂移、开关回差、边沿延迟与
多循环离散度的趋势判定。比较结果同时汇总逐通道校准链差异。
"""

from .. import calibration as calchain

# (指标键, 名称, 单位, 劣化方向, 相对容差, 绝对容差)
TREND_METRICS = [
    ("open.max_edge_delay_s", "开位接点边沿延迟", "s", 1, 0.20, 0.05),
    ("closed.max_edge_delay_s", "关位接点边沿延迟", "s", 1, 0.20, 0.05),
    ("open.actuation_dispersion_pct", "开位动作点离散度", "%", 1, 0.20, 0.2),
    ("closed.actuation_dispersion_pct", "关位动作点离散度", "%", 1, 0.20, 0.2),
    ("open.release_dispersion_pct", "开位释放点离散度", "%", 1, 0.20, 0.2),
    ("closed.release_dispersion_pct", "关位释放点离散度", "%", 1, 0.20, 0.2),
    ("open.mean_differential_pct", "开位开关回差", "%", 1, 0.20, 0.3),
    ("closed.mean_differential_pct", "关位开关回差", "%", 1, 0.20, 0.3),
]

# 动作/释放点：任一方向漂移都视为变化（不区分优劣，只标漂移）
DRIFT_METRICS = [
    ("open.mean_actuation_pct", "开位动作点", "%", 0.5),
    ("closed.mean_actuation_pct", "关位动作点", "%", 0.5),
    ("open.mean_release_pct", "开位释放点", "%", 0.5),
    ("closed.mean_release_pct", "关位释放点", "%", 0.5),
]


def _metric(result, dotted):
    contact, key = dotted.split(".", 1)
    return ((result.get("contacts") or {}).get(contact) or {}).get(
        "metrics", {}).get(key)


def _diff_detail(name, a, b):
    if a == b:
        return None
    return f"{name} 不一致：{a!r} vs {b!r}"


def _compat_reasons(ref_r, result):
    """相对基准版本的不可比原因（接线与阈值版本须完全一致）。"""
    reasons = []
    ca, cb = ref_r.get("compatibility") or {}, result.get("compatibility") or {}
    wa, wb = ca.get("wiring") or {}, cb.get("wiring") or {}
    d = _diff_detail("通道绑定（接线）", wa.get("channel_map"), wb.get("channel_map"))
    if d:
        reasons.append(d + "，检修前后接线版本不同，不得对比")
    d = _diff_detail("接点极性", wa.get("polarities"), wb.get("polarities"))
    if d:
        reasons.append(d + "，检修前后接线版本不同，不得对比")
    sa, sb = ca.get("switch") or {}, cb.get("switch") or {}
    for key, name in (("debounce_s", "去抖时长"), ("mutual_exclusion", "互斥规则")):
        d = _diff_detail(name, sa.get(key), sb.get(key))
        if d:
            reasons.append(d)
    for contact in ("open", "closed"):
        for key, name in (("actuate_window_pct", "动作窗"),
                          ("release_window_pct", "释放窗"),
                          ("polarity", "声明极性")):
            d = _diff_detail(f"{contact} 接点{name}",
                             (sa.get(contact) or {}).get(key),
                             (sb.get(contact) or {}).get(key))
            if d:
                reasons.append(d)
    d = _diff_detail("判定阈值", ca.get("thresholds"), cb.get("thresholds"))
    if d:
        reasons.append("判定阈值版本不一致，检修前后不得对比")
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


def _drift_entry(key, name, unit, pts, abs_tol):
    entry = {"metric": key, "name": name, "unit": unit, "points": pts,
             "status": "inconclusive"}
    vals = [p["value"] for p in pts if p["value"] is not None]
    if len(vals) < 2:
        entry["note"] = "可比数据点不足 2 个"
        return entry
    first, last = vals[0], vals[-1]
    delta = last - first
    entry.update({"delta": round(delta, 6), "first": first, "last": last,
                  "status": "shifted" if abs(delta) > abs_tol else "stable",
                  "abs_tol": abs_tol})
    return entry


def compare_limitswitch_tests(db, analysis_ids=None, valve_tag=None):
    """比较多次限位开关诊断。指定 analysis_ids 时以其为准，否则取每测次最新版本。"""
    items = []
    if analysis_ids:
        for aid in analysis_ids:
            a = db.get_limitswitch_analysis(aid)
            if a is None:
                raise KeyError(f"限位开关分析 {aid} 不存在")
            t = db.get_limitswitch_test(a["ls_test_id"])
            items.append((t, a, a["result"]))
    else:
        valve = db.get_valve_by_tag(valve_tag)
        if valve is None:
            raise KeyError(f"阀门 {valve_tag} 不存在")
        for tid, aid, _tt in db.latest_limitswitch_analysis_ids(valve["id"]):
            a = db.get_limitswitch_analysis(aid)
            t = db.get_limitswitch_test(tid)
            items.append((t, a, a["result"]))

    if not items:
        return {"compatible": False, "series": [], "trends": [],
                "incomparable": [{"reason": "没有可用于比较的诊断版本"}],
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
            "phase": t["phase"], "verdict": r["verdict"],
            "n_events": len(r.get("events", [])),
            "open_mean_actuation_pct": _metric(r, "open.mean_actuation_pct"),
            "closed_mean_actuation_pct": _metric(r, "closed.mean_actuation_pct"),
            "comparable": True, "reasons": [],
        }
        if t["valve_id"] != ref_t["valve_id"]:
            entry["reasons"].append("不属于同一台阀门")
        if (t["id"], a["id"]) != (ref_t["id"], ref_a["id"]):
            entry["reasons"] += _compat_reasons(ref_r, r)
        blocking = [g for g in r.get("evidence_gaps", [])
                    if g["code"] in ("unit_conflict", "calibration_missing",
                                     "calibration_expired", "channel_no_overlap",
                                     "contact_unusable", "series_empty")]
        if r["verdict"] == "no_conclusion" or blocking:
            gaps = "；".join(sorted({g["code"] for g in r.get("evidence_gaps", [])}))
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
    for key, name, unit, abs_tol in DRIFT_METRICS:
        pts = [{"test_id": t["id"], "analysis_id": a["id"],
                "test_started_at": t.get("test_started_at"),
                "value": _metric(r, key)}
               for t, a, r in items if a["id"] in comparable_ids]
        trends.append(_drift_entry(key, name, unit, pts, abs_tol))

    statuses = [t["status"] for t in trends]
    if len(comparable_ids) < 2:
        overall = "inconclusive"
    elif any(s in ("degrading", "shifted") for s in statuses):
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
            "wiring": (ref_r.get("compatibility") or {}).get("wiring"),
            "switch": (ref_r.get("compatibility") or {}).get("switch"),
            "thresholds": (ref_r.get("compatibility") or {}).get("thresholds"),
        },
        "series": series,
        "trends": trends,
        "incomparable": incomparable,
        "overall": overall,
    }
