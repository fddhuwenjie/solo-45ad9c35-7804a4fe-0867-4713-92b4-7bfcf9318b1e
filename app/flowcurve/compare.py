"""多次单相液体流量曲线校核测次的叠加比较。

只有**阀内件（trim）、介质（名称/密度/饱和蒸气压）、流向、流量计量程**
一致的测次才允许叠加到同一 Cv-阀位图上；铭牌特性与额定 Cv 作为冻结基准
一并展示。存在证据缺口（no_conclusion）的版本不参与定量比较，只列原因。
输出叠加采用点、容量系数与残差带趋势（退化/改善/稳定）。
"""

# (结果取值键, 名称, 单位, 劣化方向, 相对容差, 绝对容差)
TREND_METRICS = [
    ("capacity_factor", "容量系数 k", "-", -1, 0.10, 0.03),
    ("rmse_pct", "拟合残差 RMSE", "%", 1, 0.15, 1.0),
    ("max_abs_residual_pct", "最大残差绝对值", "%", 1, 0.15, 2.0),
    ("effective_turndown", "有效调节比", ":1", -1, 0.10, 2.0),
]

METER_FS_TOL = 0.001     # 流量计量程须一致（数值相等，容差 0.1% 防浮点）
DENSITY_TOL = 0.5        # kg/m³
PV_TOL_KPA = 5.0


def _get_items(db, analysis_ids=None, valve_tag=None):
    items = []
    if analysis_ids:
        for aid in analysis_ids:
            a = db.get_flowcurve_analysis(aid)
            if a is None:
                raise KeyError(f"流量曲线分析 {aid} 不存在")
            t = db.get_flowcurve_test(a["fc_test_id"])
            items.append((t, a, a["result"]))
    else:
        valve = db.get_valve_by_tag(valve_tag)
        if valve is None:
            raise KeyError(f"阀门 {valve_tag} 不存在")
        for tid, aid, _tt in db.latest_flowcurve_analysis_ids(valve["id"]):
            a = db.get_flowcurve_analysis(aid)
            t = db.get_flowcurve_test(tid)
            items.append((t, a, a["result"]))
    return items


def _compat_reasons(ref_r, result):
    reasons = []
    rv, cv = ref_r["valve"], result["valve"]
    if (rv.get("trim") or "") != (cv.get("trim") or ""):
        reasons.append(f"阀内件不一致：{rv.get('trim')!r} vs {cv.get('trim')!r}")
    rf, cf = ref_r["fluid"], result["fluid"]
    if (rf.get("name") or "").lower() != (cf.get("name") or "").lower():
        reasons.append(f"介质不一致：{rf.get('name')!r} vs {cf.get('name')!r}")
    else:
        d0, d1 = rf.get("density_kg_m3"), cf.get("density_kg_m3")
        if d0 is None or d1 is None or abs(d0 - d1) > DENSITY_TOL:
            reasons.append(f"介质密度不一致：{d0} vs {d1} kg/m³")
        v0, v1 = rf.get("vapor_pressure_kpa"), cf.get("vapor_pressure_kpa")
        if v0 is None or v1 is None or abs(v0 - v1) > PV_TOL_KPA:
            reasons.append(f"饱和蒸气压不一致：{v0} vs {v1} kPa")
    if ref_r["flow_direction"] != result["flow_direction"]:
        reasons.append(f"流向不一致：{ref_r['flow_direction']} vs "
                       f"{result['flow_direction']}")
    m0, m1 = ref_r["meter"], result["meter"]
    if (m0.get("unit") or "").lower() != (m1.get("unit") or "").lower():
        reasons.append(f"流量计单位不一致：{m0.get('unit')} vs {m1.get('unit')}")
    else:
        fs0, fs1 = float(m0["full_scale"]), float(m1["full_scale"])
        if abs(fs1 - fs0) / max(fs0, 1e-9) > METER_FS_TOL:
            reasons.append(f"流量计量程不一致：{fs0:g} vs {fs1:g} {m0.get('unit')}")
    # 叠加基准展示项（不作为硬性门槛，但记录差异）
    if (rv.get("rated_cv") != cv.get("rated_cv")
            or rv.get("characteristic") != cv.get("characteristic")):
        reasons.append(
            "铭牌基准不同（额定 Cv "
            f"{rv.get('rated_cv')}/{cv.get('rated_cv')}，特性 "
            f"{rv.get('characteristic')}/{cv.get('characteristic')}），"
            "残差按各自铭牌归一后再叠加")
    return reasons


def _trend_entry(key, name, unit, pts, worse_dir, rel_tol, abs_tol):
    entry = {"metric": key, "name": name, "unit": unit, "points": pts,
             "status": "inconclusive"}
    vals = [p["value"] for p in pts if p["value"] is not None]
    if len(vals) < 2:
        entry["note"] = "可比数据点不足 2 个"
        return entry
    first, last = vals[0], vals[-1]
    delta = last - first
    scale = max(abs(first), abs_tol)
    worse = delta * worse_dir
    if worse > abs_tol and worse > rel_tol * scale:
        entry["status"] = "degrading"
    elif worse < -abs_tol and worse < -rel_tol * scale:
        entry["status"] = "improving"
    else:
        entry["status"] = "stable"
    entry.update({"delta": round(delta, 4), "first": first, "last": last})
    return entry


def compare_flowcurve_tests(db, analysis_ids=None, valve_tag=None):
    """叠加比较多次校核。指定 analysis_ids 时以其为准，否则取每测次最新版本。"""
    items = _get_items(db, analysis_ids, valve_tag)
    if not items:
        return {"compatible": False, "series": [], "overlay": [], "trends": [],
                "incomparable": [{"reason": "没有可用于比较的校核版本"}],
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
            "capacity_factor": (r.get("curve") or {}).get("capacity_factor"),
            "rmse_pct": (r.get("curve") or {}).get("rmse_pct"),
            "effective_turndown": (r.get("curve") or {}).get("effective_turndown"),
            "comparable": True, "reasons": [],
        }
        if (t["id"], a["id"]) != (ref_t["id"], ref_a["id"]):
            entry["reasons"] += _compat_reasons(ref_r, r)
        if r["verdict"] == "no_conclusion":
            gaps = "；".join(sorted({g["code"] for g in r.get("evidence_gaps", [])}))
            entry["reasons"].append(f"该版本存在证据缺口（{gaps}），数值不参与比较")
        entry["comparable"] = not entry["reasons"]
        if not entry["comparable"]:
            incomparable.append({"test_id": t["id"], "analysis_id": a["id"],
                                 "reasons": entry["reasons"]})
        series.append(entry)

    comparable_ids = {e["analysis_id"] for e in series if e["comparable"]}

    # ---- 叠加采用点（Cv-阀位；附设计份额与残差） ----
    overlay = []
    for t, a, r in items:
        if a["id"] not in comparable_ids:
            continue
        curve = r.get("curve") or {}
        adopted = sorted((p for p in r.get("points", []) if p.get("adopted")),
                         key=lambda p: p["position_pct"])
        fit_by_idx = {id(p): None for p in adopted}
        fit_list = curve.get("fit_cv_by_point") or []
        for p, fv in zip(adopted, fit_list):
            fit_by_idx[id(p)] = fv
        overlay.append({
            "test_id": t["id"], "analysis_id": a["id"], "version": a["version"],
            "test_started_at": t.get("test_started_at"), "phase": t["phase"],
            "points": [{
                "plateau_index": p["plateau_index"],
                "position_pct": p["position_pct"],
                "cv": p["cv"],
                "fit_cv": fit_by_idx.get(id(p)),
                "residual_pct": (round((p["cv"] - fit_by_idx[id(p)])
                                       / max(fit_by_idx[id(p)], 1e-9) * 100.0, 2)
                                 if fit_by_idx.get(id(p)) else None),
                "dp_kpa": p["conversion"]["dp_kpa"],
            } for p in adopted],
            "capacity_factor": curve.get("capacity_factor"),
        })

    trends = []
    for key, name, unit, worse_dir, rel_tol, abs_tol in TREND_METRICS:
        pts = [{"test_id": t["id"], "analysis_id": a["id"],
                "test_started_at": t.get("test_started_at"),
                "value": (r.get("curve") or {}).get(key)}
               for t, a, r in items if a["id"] in comparable_ids]
        trends.append(_trend_entry(key, name, unit, pts, worse_dir,
                                   rel_tol, abs_tol))

    if len(comparable_ids) < 2:
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
        "compatible": len(incomparable) == 0 and len(comparable_ids) >= 2,
        "frozen_conditions": {
            "reference_test_id": ref_t["id"],
            "trim": ref_r["valve"].get("trim"),
            "rated_cv": ref_r["valve"].get("rated_cv"),
            "characteristic": ref_r["characteristic"],
            "size_dn_mm": ref_r["valve"].get("size_dn_mm"),
            "fluid": ref_r["fluid"].get("name"),
            "density_kg_m3": ref_r["fluid"].get("density_kg_m3"),
            "vapor_pressure_kpa": ref_r["fluid"].get("vapor_pressure_kpa"),
            "flow_direction": ref_r["flow_direction"],
            "meter_full_scale": ref_r["meter"].get("full_scale"),
            "meter_unit": ref_r["meter"].get("unit"),
        },
        "series": series,
        "overlay": overlay,
        "trends": trends,
        "incomparable": incomparable,
        "overall": overall,
    }
