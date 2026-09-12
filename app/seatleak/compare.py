"""多次阀座密封保持试验的比较。

仅当气体（名称与摩尔质量）、压差范围（保持段压差中位 ±10%）、流向与
阀座配置一致时才比较；存在证据缺口的版本不参与定量比较，只列不可比原因。
指标按测试时间排序，给出退化/改善/稳定判定。比较结果同时汇总逐通道校准链：
采用证书（序列号/摘要）是否一致、修正量变化与拒绝原因。
"""

from .. import calibration as calchain

# (结果取值键, 名称, 单位, 劣化方向: +1 越大越差, 相对容差, 绝对容差)
TREND_METRICS = [
    ("leak_rate_mean_nl_min", "平均泄漏率", "Nl/min", 1, 0.15, 0.01),
    ("leak_rate_max_nl_min", "窗口最大泄漏率", "Nl/min", 1, 0.15, 0.02),
    ("cumulative_leak_nl", "保持段累计漏量", "Nl", 1, 0.15, 0.05),
]

DP_TOL = 0.10           # 压差范围冻结容差 ±10%
MOLAR_MASS_TOL = 0.5    # 摩尔质量容差 g/mol


def _compat_reasons(ref_t, ref_r, test, result):
    """相对基准试验的不可比原因。"""
    reasons = []
    if ref_t["valve_id"] != test["valve_id"]:
        reasons.append("不属于同一台阀门")
    rg, cg = ref_r["gas"], result["gas"]
    if (rg.get("name") or "").lower() != (cg.get("name") or "").lower():
        reasons.append(f"气体不一致：{rg.get('name')!r} vs {cg.get('name')!r}")
    elif abs(rg.get("molar_mass_g_mol", 0) - cg.get("molar_mass_g_mol", 0)) > MOLAR_MASS_TOL:
        reasons.append(f"气体摩尔质量不一致：{rg.get('molar_mass_g_mol')} vs "
                       f"{cg.get('molar_mass_g_mol')} g/mol")
    if ref_r["flow_direction"] != result["flow_direction"]:
        reasons.append(f"流向不一致：{ref_r['flow_direction']} vs {result['flow_direction']}")
    if ref_r.get("seat_config") != result.get("seat_config"):
        reasons.append(f"阀座配置不一致：{ref_r.get('seat_config')!r} vs "
                       f"{result.get('seat_config')!r}")
    d0 = ref_r["metrics"].get("differential_median_kpa")
    d1 = result["metrics"].get("differential_median_kpa")
    if d0 and d1 and abs(d1 - d0) / max(d0, 1e-9) > DP_TOL:
        reasons.append(f"压差范围不兼容：保持段压差中位 {d0} vs {d1} kPa，"
                       f"偏差超过 ±{DP_TOL * 100:.0f}%")
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
    entry["delta"] = round(delta, 6)
    entry["first"] = first
    entry["last"] = last
    return entry


def compare_seat_tests(db, analysis_ids=None, valve_tag=None):
    """比较多次试验。指定 analysis_ids 时以其为准，否则取该阀门每个试验的最新版本。"""
    items = []  # (test, analysis_dict, result)
    if analysis_ids:
        for aid in analysis_ids:
            a = db.get_seatleak_analysis(aid)
            if a is None:
                raise KeyError(f"阀座密封分析 {aid} 不存在")
            t = db.get_seatleak_test(a["seatleak_test_id"])
            items.append((t, a, a["result"]))
    else:
        valve = db.get_valve_by_tag(valve_tag)
        if valve is None:
            raise KeyError(f"阀门 {valve_tag} 不存在")
        for tid, aid, _tt in db.latest_seatleak_analysis_ids(valve["id"]):
            a = db.get_seatleak_analysis(aid)
            t = db.get_seatleak_test(tid)
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
            "leak_rate_mean_nl_min": r["metrics"].get("leak_rate_mean_nl_min"),
            "cumulative_leak_nl": r["metrics"].get("cumulative_leak_nl"),
            "comparable": True, "reasons": [],
        }
        if (t["id"], a["id"]) != (ref_t["id"], ref_a["id"]):
            entry["reasons"] += _compat_reasons(ref_t, ref_r, t, r)
        if r["verdict"] == "no_conclusion":
            gaps = "；".join(sorted({g["code"] for g in r["evidence_gaps"]}))
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

    comparable = [e for e in series if e["comparable"]]
    trends = []
    for key, name, unit, worse_dir, rel_tol, abs_tol in TREND_METRICS:
        pts = [{"test_id": e["test_id"], "analysis_id": e["analysis_id"],
                "test_started_at": e["test_started_at"],
                "value": next(r["metrics"].get(key)
                              for t, a, r in items if a["id"] == e["analysis_id"])}
               for e in comparable]
        trends.append(_trend_entry(key, name, unit, pts, worse_dir, rel_tol, abs_tol))

    statuses = [t["status"] for t in trends]
    if len(comparable) < 2:
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
        "compatible": len(incomparable) == 0 and len(comparable) >= 2,
        "calibration_comparison": calchain.compare_chains(ref_r, items[-1][2]),
        "frozen_conditions": {
            "reference_test_id": ref_t["id"],
            "gas": ref_r["gas"].get("name"),
            "molar_mass_g_mol": ref_r["gas"].get("molar_mass_g_mol"),
            "flow_direction": ref_r["flow_direction"],
            "seat_config": ref_r.get("seat_config"),
            "differential_median_kpa": ref_r["metrics"].get("differential_median_kpa"),
        },
        "series": series,
        "trends": trends,
        "incomparable": incomparable,
        "overall": overall,
    }
