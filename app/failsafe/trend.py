"""同一阀门、同种故障模式多次故障安全测试的退化趋势。

按冻结工况（负载、介质、执行器类型、供压/温度容差、观察窗长度、同一套阈值）
对齐：工况不冻结的测试不参与趋势，只列不可比原因；存在证据缺口的版本
同样不参与定量趋势。指标按测试时间排序，逐指标给出退化/改善/稳定判定。
"""

# (结果取值键, 名称, 单位, 劣化方向: +1 表示越大越差, -1 表示越小越差)
TREND_METRICS = [
    ("response_delay_s", "响应延迟", "s", 1, 0.15, 0.10),
    ("t90_s", "90% 行程时间", "s", 1, 0.15, 0.20),
    ("settle_time_s", "稳定时间（自跳闸）", "s", 1, 0.15, 0.30),
    ("pressure_end_kpa", "窗末残压", "kPa", 1, 0.15, 5.0),
    ("pressure_decay_pct", "压力衰减比例", "%", -1, 0.10, 3.0),
    ("max_rebound_pct", "最大反弹", "%", 1, 0.20, 0.5),
    ("mid_stall_count", "中途停滞次数", "次", 1, 0.0, 0.0),
]

SUPPLY_TOL = 0.10       # 供压冻结容差 ±10%
TEMP_TOL_C = 5.0        # 环境温度冻结容差 ±5°C
WINDOW_TOL = 0.10       # 观察窗长度冻结容差 ±10%

FROZEN_THRESHOLD_KEYS = [
    "response_delay_s_max", "t90_s_max", "settle_band_pct", "settle_dwell_s",
    "rebound_pct_max", "pressure_residual_kpa_max", "fip_drift_pct_max",
]


def _metric_value(result, key):
    if key == "settle_time_s":
        return result["metrics"].get("settle_time_s")
    return result["metrics"].get(key)


def _final_position_error(result):
    m = result["metrics"]
    if result["fail_mode"] == "fail_in_place":
        return m.get("max_drift_pct")
    fp = m.get("final_position_pct")
    if fp is None:
        return None
    target = 0.0 if result["fail_mode"] == "fail_close" else 100.0
    return round(abs(fp - target), 3)


def _frozen_condition_issues(ref_test, ref_result, test, result):
    """相对基准测试的工况/配置不可比原因。"""
    reasons = []
    rc, cc = ref_test["conditions"], test["conditions"]
    if rc.get("load") != cc.get("load"):
        reasons.append(f"负载条件不一致：{rc.get('load')!r} vs {cc.get('load')!r}")
    if rc.get("medium") != cc.get("medium"):
        reasons.append(f"介质不一致：{rc.get('medium')!r} vs {cc.get('medium')!r}")
    if rc.get("actuator_type") != cc.get("actuator_type"):
        reasons.append(
            f"执行器类型不一致：{rc.get('actuator_type')!r} vs {cc.get('actuator_type')!r}")
    ps0, ps1 = rc.get("supply_pressure_kpa"), cc.get("supply_pressure_kpa")
    if ps0 and ps1 and abs(ps1 - ps0) / max(ps0, 1e-9) > SUPPLY_TOL:
        reasons.append(f"供压偏差超过冻结容差 ±{SUPPLY_TOL * 100:.0f}%：{ps0} vs {ps1} kPa")
    t0, t1 = rc.get("ambient_temp_c"), cc.get("ambient_temp_c")
    if t0 is not None and t1 is not None and abs(t1 - t0) > TEMP_TOL_C:
        reasons.append(f"环境温度偏差超过冻结容差 ±{TEMP_TOL_C:.0f}°C：{t0} vs {t1}")
    w0 = ref_result.get("adopted_window") or {}
    w1 = result.get("adopted_window") or {}
    l0, l1 = w0.get("length_s"), w1.get("length_s")
    if l0 and l1 and abs(l1 - l0) / max(l0, 1e-9) > WINDOW_TOL:
        reasons.append(f"观察窗长度不一致超出 ±{WINDOW_TOL * 100:.0f}%：{l0}s vs {l1}s")
    th0, th1 = ref_result["thresholds"], result["thresholds"]
    for k in FROZEN_THRESHOLD_KEYS:
        if th0.get(k) != th1.get(k):
            reasons.append(f"判定阈值 {k} 不一致：{th0.get(k)} vs {th1.get(k)}，非冻结判据")
            break
    return reasons


def build_trend(db, analysis_ids=None, valve_id=None, fail_mode=None):
    """聚合趋势。指定 analysis_ids 时以其为准，否则按阀门+故障模式取每个测试最新版。"""
    items = []  # (test, analysis_dict, result)
    if analysis_ids:
        for aid in analysis_ids:
            a = db.get_failsafe_analysis(aid)
            if a is None:
                raise KeyError(f"故障安全分析 {aid} 不存在")
            t = db.get_failsafe_test(a["failsafe_test_id"])
            items.append((t, a, a["result"]))
    else:
        for tid, aid, _ttime in db.latest_failsafe_analysis_ids(valve_id, fail_mode):
            a = db.get_failsafe_analysis(aid)
            t = db.get_failsafe_test(tid)
            items.append((t, a, a["result"]))

    if not items:
        return {"compatible": False, "series": [], "trends": [],
                "incomparable": [{"reason": "没有可用于趋势分析的测试版本"}],
                "overall": "inconclusive"}

    # 按测试时间排序
    def sort_key(it):
        t = it[0]
        return t.get("test_started_at") or t["submitted_at"], t["id"]
    items.sort(key=sort_key)

    ref_test, ref_a, ref_result = items[0]
    valve = db.get_valve(ref_test["valve_id"])
    mode = ref_result["fail_mode"]

    series, incomparable = [], []
    for t, a, r in items:
        entry = {
            "test_id": t["id"], "analysis_id": a["id"], "version": a["version"],
            "test_started_at": t.get("test_started_at"),
            "phase": t["phase"], "verdict": r["verdict"],
            "comparable": True, "reasons": [],
        }
        if t["valve_id"] != ref_test["valve_id"]:
            entry["comparable"] = False
            entry["reasons"].append("不属于同一台阀门")
        if r["fail_mode"] != mode:
            entry["comparable"] = False
            entry["reasons"].append(f"故障模式不同：{r['fail_mode']} vs {mode}")
        if (t["id"], a["id"]) != (ref_test["id"], ref_a["id"]):
            for why in _frozen_condition_issues(ref_test, ref_result, t, r):
                entry["comparable"] = False
                entry["reasons"].append(why)
        if r["verdict"] == "no_conclusion":
            entry["comparable"] = False
            gaps = "；".join(g["code"] for g in r["evidence_gaps"])
            entry["reasons"].append(f"该版本存在证据缺口（{gaps}），数值不参与趋势")
        if not entry["comparable"]:
            incomparable.append({"test_id": t["id"], "analysis_id": a["id"],
                                 "reasons": entry["reasons"]})
        series.append(entry)

    comparable_idx = [k for k, e in enumerate(series) if e["comparable"]]
    trends = []
    for key, name, unit, worse_dir, rel_tol, abs_tol in TREND_METRICS:
        vals = [(k, _metric_value(items[k][2], key)) for k in comparable_idx]
        trends.append(_trend_entry(key, name, unit, vals, series,
                                   {items[k][1]["id"]: items[k][2] for k in comparable_idx},
                                   worse_dir, rel_tol, abs_tol))
    # 最终位置偏差（派生指标）
    vals = [(k, _final_position_error(items[k][2])) for k in comparable_idx]
    trends.append(_trend_entry("final_position_error_pct", "最终位置与安全位偏差",
                               "%", vals, series,
                               {items[k][1]["id"]: items[k][2] for k in comparable_idx},
                               1, 0.15, 0.5))

    pass_history = [{"test_id": series[k]["test_id"],
                     "analysis_id": series[k]["analysis_id"],
                     "verdict": items[k][2]["verdict"]} for k in comparable_idx]

    metric_statuses = [t["status"] for t in trends]
    if len(comparable_idx) < 2:
        overall = "inconclusive"
    elif any(s == "degrading" for s in metric_statuses):
        overall = "degrading"
    elif all(s in ("stable", "inconclusive") for s in metric_statuses):
        overall = "stable"
    else:
        overall = "improving" if any(s == "improving" for s in metric_statuses) else "mixed"

    latest_pass = pass_history[-1]["verdict"] == "pass" if pass_history else False
    return {
        "valve_tag": valve["tag"] if valve else None,
        "valve_id": ref_test["valve_id"],
        "fail_mode": mode,
        "compatible": len(incomparable) == 0 and len(comparable_idx) >= 2,
        "frozen_conditions": {
            "reference_test_id": ref_test["id"],
            "load": ref_test["conditions"].get("load"),
            "medium": ref_test["conditions"].get("medium"),
            "actuator_type": ref_test["conditions"].get("actuator_type"),
            "supply_pressure_kpa": ref_test["conditions"].get("supply_pressure_kpa"),
            "ambient_temp_c": ref_test["conditions"].get("ambient_temp_c"),
            "window_length_s": (ref_result.get("adopted_window") or {}).get("length_s"),
            "thresholds": {k: ref_result["thresholds"].get(k) for k in FROZEN_THRESHOLD_KEYS},
        },
        "series": series,
        "trends": trends,
        "pass_history": pass_history,
        "latest_pass": latest_pass,
        "incomparable": incomparable,
        "overall": overall,
    }


def _trend_entry(key, name, unit, vals, series, results_by_aid,
                 worse_dir, rel_tol, abs_tol):
    """vals: [(series_index, value)]，worse_dir=1 越大越差，-1 越小越差。"""
    pts = [{"test_id": series[k]["test_id"], "analysis_id": series[k]["analysis_id"],
            "test_started_at": series[k]["test_started_at"], "value": v}
           for k, v in vals if v is not None]
    entry = {"metric": key, "name": name, "unit": unit, "points": pts,
             "status": "inconclusive"}
    if len(pts) < 2:
        entry["note"] = "可比数据点不足 2 个"
        return entry
    first, last = pts[0]["value"], pts[-1]["value"]
    scale = max(abs(first), 1e-9)
    delta = last - first
    worse = delta * worse_dir
    if worse > abs_tol and worse > rel_tol * scale:
        entry["status"] = "degrading"
    elif worse < -abs_tol and worse < -rel_tol * scale:
        entry["status"] = "improving"
    else:
        entry["status"] = "stable"
    entry["delta"] = round(delta, 3)
    entry["first"] = first
    entry["last"] = last
    # 是否超出当前阈值（末点）
    last_result = results_by_aid[pts[-1]["analysis_id"]]
    limit = _threshold_for(key, last_result["thresholds"])
    if limit is not None:
        entry["threshold"] = limit
        entry["latest_within_threshold"] = (last <= limit) if worse_dir == 1 else (last >= limit)
    return entry


def _threshold_for(key, thr):
    return {
        "response_delay_s": thr.get("response_delay_s_max"),
        "t90_s": thr.get("t90_s_max"),
        "max_rebound_pct": thr.get("rebound_pct_max"),
        "pressure_end_kpa": thr.get("pressure_residual_kpa_max"),
        "pressure_decay_pct": thr.get("pressure_decay_pct_min"),
        "final_position_error_pct": thr.get("settle_band_pct"),
    }.get(key)
