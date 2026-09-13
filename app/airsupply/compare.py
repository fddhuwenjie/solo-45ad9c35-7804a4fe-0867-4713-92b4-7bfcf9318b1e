"""供气瞬态核算修订比较。

只读取两个修订各自冻结的方案与逐时结果（不重算）：逐动作对比行程时间、
最低供压、时间/压力余量与到达标志，汇总判定变化与两修订之间的修订记录。
不同方案的修订不得比较；含 no_conclusion 版本时差值仅供参考。
"""

# (结果取值键, 名称, 单位, 改善方向: +1 越大越好, -1 越小越好)
METRICS = [
    ("stroke_time_s", "行程时间", "s", -1),
    ("min_supply_pressure_kpa", "最低供压", "kPa", 1),
    ("time_margin_s", "时间余量", "s", 1),
    ("pressure_margin_kpa", "压力余量", "kPa", 1),
]


def _metric_delta(a, b, key, better_dir):
    va = a.get(key) if a else None
    vb = b.get(key) if b else None
    row = {"a": va, "b": vb, "delta": None, "improved": None}
    if va is not None and vb is not None:
        delta = round(vb - va, 4)
        row["delta"] = delta
        if delta == 0:
            row["improved"] = None
        else:
            row["improved"] = (delta * better_dir) > 0
    return row


def compare_revisions(db, revision_id_a, revision_id_b):
    """比较同一方案的两个修订。返回比较结果（不写库，由调用方落库）。"""
    ra = db.get_airsupply_revision(revision_id_a)
    if ra is None:
        raise KeyError(f"供气瞬态核算修订 {revision_id_a} 不存在")
    rb = db.get_airsupply_revision(revision_id_b)
    if rb is None:
        raise KeyError(f"供气瞬态核算修订 {revision_id_b} 不存在")
    if ra["scheme_id"] != rb["scheme_id"]:
        raise ValueError("两个修订不属于同一方案，不得比较")

    res_a, res_b = ra["result"], rb["result"]
    actions_a = {a["name"]: a for a in res_a.get("actions", [])}
    rows = []
    for b in res_b.get("actions", []):
        a = actions_a.get(b["name"])
        row = {"name": b["name"], "kind": b.get("kind"),
               "in_both": a is not None,
               "reached": {"a": a.get("reached") if a else None,
                           "b": b.get("reached")}}
        for key, _label, _unit, better_dir in METRICS:
            row[key] = _metric_delta(a, b, key, better_dir)
        rows.append(row)

    adj_a, adj_b = ra["adjustments"], rb["adjustments"]
    if len(adj_b) >= len(adj_a):
        between = adj_b[len(adj_a):]
    else:
        between = adj_a[len(adj_b):]

    totals_a = res_a.get("totals") or {}
    totals_b = res_b.get("totals") or {}
    totals_cmp = {}
    for key in ("min_tank_pressure_kpa", "air_to_actuator_nl",
                    "events_consumed_nl", "max_reg_dp_kpa"):
        va, vb = totals_a.get(key), totals_b.get(key)
        totals_cmp[key] = {"a": va, "b": vb,
                           "delta": round(vb - va, 4)
                           if va is not None and vb is not None else None}

    verdicts = (res_a.get("verdict"), res_b.get("verdict"))
    return {
        "scheme_id": ra["scheme_id"],
        "revision_a": {"id": ra["id"], "revision": ra["revision"],
                       "verdict": res_a.get("verdict"),
                       "created_at": ra["created_at"]},
        "revision_b": {"id": rb["id"], "revision": rb["revision"],
                       "verdict": res_b.get("verdict"),
                       "created_at": rb["created_at"]},
        "verdict_change": f"{verdicts[0]}→{verdicts[1]}",
        "note": "含无结论（no_conclusion）版本，差值仅供参考"
                if "no_conclusion" in verdicts else "",
        "adjustments_between": between,
        "action_deltas": rows,
        "totals": totals_cmp,
    }
