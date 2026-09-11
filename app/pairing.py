"""检修前后配对：仅在方向、行程范围与负载条件兼容时比较指标改善。"""

# 参与比较的标量指标：(名称, 取值函数, 阈值键, 单位)
METRIC_KEYS = [
    ("travel_time_s", "行程时间", lambda m: m["travel_time"]["max_s"], "travel_time_s_max", "s"),
    ("deadband_pct", "死区", lambda m: m["deadband"]["max_pct"], "deadband_pct_max", "%"),
    ("hysteresis_pct", "回差", lambda m: m["hysteresis"]["max_pct"], "hysteresis_pct_max", "%"),
    ("overshoot_pct", "过冲", lambda m: m["overshoot"]["max_pct"], "overshoot_pct_max", "%"),
    ("steady_state_pct", "稳态偏差", lambda m: m["steady_state"]["max_pct"], "steady_state_pct_max", "%"),
]

TRAVEL_RANGE_TOL_PCT = 5.0  # 两侧指令行程跨度允许偏差


def _span(result):
    cmds = [s["cmd_start"] for s in result["segments"]] + \
           [s["cmd_end"] for s in result["segments"]]
    return max(cmds) - min(cmds)


def _directions(result):
    return {s["type"] for s in result["segments"]}


def check_compatibility(pre, post):
    """pre/post: 分析 result。返回 (compatible, reasons)。"""
    reasons = []
    for label, res in (("检修前", pre), ("检修后", post)):
        if res["verdict"] == "no_conclusion":
            reasons.append(f"{label}分析存在阻断问题（{'; '.join(res['blocking_issues'])}），"
                           "不得用于维修结论")
    pre_dir, post_dir = _directions(pre), _directions(post)
    for d, name in (("opening", "开阀"), ("closing", "关阀")):
        if d not in pre_dir or d not in post_dir:
            reasons.append(f"方向不兼容：一侧缺少{name}行程，无法同向比较")
    span_pre, span_post = _span(pre), _span(post)
    if span_pre <= 0 or span_post <= 0:
        reasons.append("行程范围无效：指令跨度为 0")
    elif abs(span_pre - span_post) / max(span_pre, span_post) * 100 > TRAVEL_RANGE_TOL_PCT:
        reasons.append(
            f"行程范围不兼容：检修前 {span_pre:.1f}% 与检修后 {span_post:.1f}% "
            f"相差超过 {TRAVEL_RANGE_TOL_PCT}%")
    return not reasons, reasons


def check_conditions(pre_conditions, post_conditions):
    """负载条件兼容性。返回不兼容原因列表。"""
    reasons = []
    for key, name in (("load", "负载条件"), ("medium", "介质")):
        a, b = pre_conditions.get(key), post_conditions.get(key)
        if a != b:
            reasons.append(f"{name}不一致：检修前 {a!r} vs 检修后 {b!r}")
    return reasons


def compare(db, pre_analysis_id, post_analysis_id):
    pre_a = db.get_analysis(pre_analysis_id)
    post_a = db.get_analysis(post_analysis_id)
    if pre_a is None or post_a is None:
        raise KeyError("分析记录不存在")
    pre, post = pre_a["result"], post_a["result"]
    pre_test = db.get_test(pre_a["test_id"])
    post_test = db.get_test(post_a["test_id"])

    compatible, reasons = check_compatibility(pre, post)
    reasons += check_conditions(pre_test["conditions"], post_test["conditions"])
    if pre_test["valve_id"] != post_test["valve_id"]:
        reasons.append("两次测试不属于同一台阀门")
    compatible = compatible and not reasons

    result = {
        "pre_analysis_id": pre_analysis_id,
        "post_analysis_id": post_analysis_id,
        "pre_version": pre_a["version"],
        "post_version": post_a["version"],
        "valve_tag": db.get_valve(pre_test["valve_id"])["tag"],
        "compatible": compatible,
        "incompatible_reasons": reasons,
        "improvements": [],
        "still_exceeding": [],
    }
    if not compatible:
        return result

    thr = post["thresholds"]
    for key, name, getter, thr_key, unit in METRIC_KEYS:
        v_pre = getter(pre["metrics"])
        v_post = getter(post["metrics"])
        entry = {"metric": key, "name": name, "unit": unit,
                 "pre": v_pre, "post": v_post, "threshold": thr[thr_key]}
        if v_pre is not None and v_post is not None:
            entry["delta"] = round(v_pre - v_post, 3)  # 正值 = 改善
            entry["improved"] = v_post < v_pre
            entry["within_threshold"] = v_post <= thr[thr_key]
        else:
            entry["note"] = "一侧指标缺失，不可比"
        result["improvements"].append(entry)

    # 卡跳次数对比
    result["improvements"].append({
        "metric": "stick_slip_count", "name": "卡跳次数", "unit": "次",
        "pre": len(pre["detections"]["stick_slip"]),
        "post": len(post["detections"]["stick_slip"]),
        "delta": len(pre["detections"]["stick_slip"]) - len(post["detections"]["stick_slip"]),
        "improved": len(post["detections"]["stick_slip"]) < len(pre["detections"]["stick_slip"]),
    })

    # 检修后仍超限的时段
    for issue in post["issues"]:
        result["still_exceeding"].append({
            "kind": issue["kind"],
            "detail": issue["detail"],
            "value": issue["value"],
            "threshold": issue["threshold"],
            "periods": issue["periods"],
        })
    return result
