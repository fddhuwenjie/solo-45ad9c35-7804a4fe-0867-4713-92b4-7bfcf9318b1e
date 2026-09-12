"""检修前后阀杆推力签名比较。

仅在执行器结构（类型/阀杆方向）、有效面积（±2%）、弹簧版本和负载条件
（load/medium）兼容时才比较推力指标；存在证据缺口的版本不参与定量比较。
不可比原因逐条列出；可比时给出启动力、运行摩擦、摩擦带、离座力、落座力的
变化（delta 正值表示改善，即摩擦力/超限力减小或落座裕量增大）。
"""

# (结果取值键, 名称, 单位, 劣化方向 +1 越大越差 / -1 越小越差, 相对容差, 绝对容差)
TREND_METRICS = [
    ("breakaway_open_peak_n", "开阀启动力", "N", 1, 0.10, 20.0),
    ("breakaway_close_peak_n", "关阀启动力", "N", 1, 0.10, 20.0),
    ("running_friction_open_n", "开阀运行摩擦", "N", 1, 0.10, 15.0),
    ("running_friction_close_n", "关阀运行摩擦", "N", 1, 0.10, 15.0),
    ("friction_band_total_n", "总摩擦带", "N", 1, 0.10, 20.0),
    ("unseat_open_peak_n", "开阀离座力", "N", 1, 0.10, 20.0),
    ("seating_close_n", "关阀落座力", "N", 0, 0.10, 15.0),  # 双向限值，仅记录
    ("seating_margin_n", "落座裕量", "N", -1, 0.10, 15.0),
]

AREA_TOL = 0.02  # 有效面积兼容容差 ±2%


def _compatibility(reference, other):
    """返回相对基准结果的不可比原因。"""
    reasons = []
    a0, a1 = reference.get("actuator") or {}, other.get("actuator") or {}
    if not a0 or not a1:
        reasons.append("执行机构配置缺失")
        return reasons
    if a0.get("actuator_type") != a1.get("actuator_type"):
        reasons.append(f"执行器结构不一致：{a0.get('actuator_type')} vs "
                       f"{a1.get('actuator_type')}")
    if a0.get("stem_direction") != a1.get("stem_direction"):
        reasons.append(f"阀杆方向不一致：{a0.get('stem_direction')} vs "
                       f"{a1.get('stem_direction')}")
    for side, key in (("A 腔", "area_a_m2"), ("B 腔", "area_b_m2")):
        v0, v1 = a0.get(key), a1.get(key)
        if v0 and v1:
            if abs(v1 - v0) / max(v0, 1e-12) > AREA_TOL:
                reasons.append(f"{side}有效面积不兼容：{v0} vs {v1} m²，"
                               f"偏差超过 ±{AREA_TOL * 100:.0f}%")
        elif bool(v0) != bool(v1):
            reasons.append(f"{side}有效面积一侧缺失")
    sv0, sv1 = a0.get("spring_version"), a1.get("spring_version")
    if sv0 != sv1:
        reasons.append(f"弹簧版本不一致：{sv0!r} vs {sv1!r}")
    if reference.get("load_condition") != other.get("load_condition"):
        reasons.append(f"负载条件不一致：{reference.get('load_condition')!r} vs "
                       f"{other.get('load_condition')!r}")
    if reference.get("medium") != other.get("medium"):
        reasons.append(f"介质不一致：{reference.get('medium')!r} vs "
                       f"{other.get('medium')!r}")
    return reasons


def _trend_entry(key, name, unit, worse_dir, rel_tol, abs_tol, pts):
    entry = {"metric": key, "name": name, "unit": unit, "points": pts,
             "status": "inconclusive"}
    vals = [p["value"] for p in pts if p["value"] is not None]
    if len(vals) < 2:
        entry["note"] = "可比数据点不足 2 个"
        return entry
    first, last = vals[0], vals[-1]
    delta = last - first
    worse = delta * worse_dir
    scale = max(abs(first), 1e-9)
    if worse > abs_tol and worse > rel_tol * scale:
        entry["status"] = "degrading"
    elif worse < -abs_tol and worse < -rel_tol * scale:
        entry["status"] = "improving"
    else:
        entry["status"] = "stable"
    entry["delta"] = round(delta, 2)
    entry["improvement_n"] = round(-delta * worse_dir, 2)  # 正值=改善
    entry["first"] = first
    entry["last"] = last
    return entry


def compare_thrust_tests(db, analysis_ids=None, valve_tag=None):
    """比较多次推力签名测试。"""
    items = []
    if analysis_ids:
        for aid in analysis_ids:
            a = db.get_thrust_analysis(aid)
            if a is None:
                raise KeyError(f"阀杆推力分析 {aid} 不存在")
            t = db.get_thrust_test(a["thrust_test_id"])
            items.append((t, a, a["result"]))
    else:
        valve = db.get_valve_by_tag(valve_tag)
        if valve is None:
            raise KeyError(f"阀门 {valve_tag} 不存在")
        for tid, aid, _tt in db.latest_thrust_analysis_ids(valve["id"]):
            a = db.get_thrust_analysis(aid)
            t = db.get_thrust_test(tid)
            items.append((t, a, a["result"]))

    if not items:
        return {"compatible": False, "series": [], "trends": [],
                "incomparable": [{"reason": "没有可用于比较的试验版本"}],
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
            "comparable": True, "reasons": [],
        }
        if (t["id"], a["id"]) != (ref_t["id"], ref_a["id"]):
            entry["reasons"] += _compatibility(ref_r, r)
        if r["verdict"] == "no_conclusion":
            gaps = "；".join(sorted({g["code"] for g in r["evidence_gaps"]}))
            entry["reasons"].append(f"该版本存在证据缺口（{gaps}），数值不参与比较")
        entry["comparable"] = not entry["reasons"]
        if not entry["comparable"]:
            incomparable.append({"test_id": t["id"], "analysis_id": a["id"],
                                 "reasons": entry["reasons"]})
        series.append(entry)

    comparable = [e for e in series if e["comparable"]]
    results_by_aid = {a["id"]: r for _t, a, r in items}
    trends = []
    for key, name, unit, worse_dir, rel_tol, abs_tol in TREND_METRICS:
        pts = [{"test_id": e["test_id"], "analysis_id": e["analysis_id"],
                "test_started_at": e["test_started_at"],
                "value": results_by_aid[e["analysis_id"]]["metrics"].get(key)}
               for e in comparable]
        trends.append(_trend_entry(key, name, unit, worse_dir, rel_tol, abs_tol, pts))

    if len(comparable) < 2:
        overall = "inconclusive"
    else:
        statuses = [t["status"] for t in trends]
        if any(s == "degrading" for s in statuses):
            overall = "degrading"
        elif all(s in ("stable", "inconclusive") for s in statuses):
            overall = "stable"
        else:
            overall = "improving" if any(s == "improving" for s in statuses) else "mixed"

    return {
        "valve_tag": valve["tag"] if valve else None,
        "valve_id": ref_t["valve_id"],
        "compatible": not incomparable and len(comparable) >= 2,
        "frozen_conditions": {
            "reference_test_id": ref_t["id"],
            "actuator_type": (ref_r.get("actuator") or {}).get("actuator_type"),
            "area_a_m2": (ref_r.get("actuator") or {}).get("area_a_m2"),
            "area_b_m2": (ref_r.get("actuator") or {}).get("area_b_m2"),
            "spring_version": (ref_r.get("actuator") or {}).get("spring_version"),
            "stem_direction": (ref_r.get("actuator") or {}).get("stem_direction"),
            "load": ref_r.get("load_condition"),
            "medium": ref_r.get("medium"),
        },
        "series": series,
        "trends": trends,
        "incomparable": incomparable,
        "overall": overall,
    }
