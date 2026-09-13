"""供气瞬态核算编排：方案归一化 → 求解 → 动作评估与判定。

流程：单位统一（容积/压力/流量/力/面积，冲突即证据缺口）→ 并发用气事件
时序核查（倒序为证据缺口）→ 调压阀流量曲线覆盖核查（运行压差超出曲线
覆盖范围即证据缺口）→ 积分收敛核查（dt 与 dt/2 结果超差即证据缺口）→
逐动作评估（最低供压、预计行程时间、余量）→ 判定。

判定：
- 存在证据缺口（单位冲突、流量曲线覆盖不足、事件倒序、积分不收敛）
  → no_conclusion，只列证据缺口，不给通过/超限结论；
- 无缺口但任一动作核查不通过（供压低于要求、行程超时、安全位未到达）
  → fail，指出首个相关用气设备与时间区间；
- 全部通过 → pass。
"""

from ..common.processing.units import PRESSURE_TO_KPA, FLOW_TO_NL_MIN
from . import solver

VOLUME_TO_M3 = {"l": 1e-3, "m3": 1.0, "m³": 1.0}
FORCE_TO_N = {"n": 1.0, "kn": 1000.0}
AREA_TO_M2 = {"cm2": 1e-4, "m2": 1.0, "in2": 6.4516e-4}
PORT_COND_TO_NL_MIN_KPA = {"nl/min/kpa": 1.0, "nm3/h/kpa": 1000.0 / 60.0}

_GAP_UNIT = "unit_conflict"
_GAP_CURVE = "flow_curve_coverage"
_GAP_EVENT_ORDER = "event_order"
_GAP_CONVERGE = "integration_not_converged"


def _norm_quantity(q, table, field, gaps, notes, allow_zero=True):
    """带单位数值归一化。返回 (值, 单位原文)；单位不支持时记证据缺口并返回 None。"""
    value, unit = q.get("value"), (q.get("unit") or "").strip().lower()
    if value is None:
        gaps.append({"code": _GAP_UNIT, "detail": f"{field}: 缺少数值"})
        return None
    factor = table.get(unit)
    if factor is None:
        gaps.append({"code": _GAP_UNIT,
                     "detail": f"{field}: 不支持的单位 {q.get('unit')!r}，"
                               f"支持 {sorted(table)}"})
        return None
    if not allow_zero and value <= 0:
        gaps.append({"code": _GAP_UNIT, "detail": f"{field}: 数值必须为正（{value}）"})
        return None
    if factor != 1.0:
        notes.append(f"{field}: {q.get('unit')} 已换算为标准单位")
    return value * factor


def _norm_pressure(q, field, gaps, notes, allow_zero=True):
    return _norm_quantity(q, PRESSURE_TO_KPA, field, gaps, notes, allow_zero)


def _norm_volume(q, field, gaps, notes):
    return _norm_quantity(q, VOLUME_TO_M3, field, gaps, notes, allow_zero=False)


def _norm_flow(q, field, gaps, notes):
    return _norm_quantity(q, FLOW_TO_NL_MIN, field, gaps, notes)


def _norm_force(q, field, gaps, notes):
    return _norm_quantity(q, FORCE_TO_N, field, gaps, notes)


def _norm_area(q, field, gaps, notes):
    return _norm_quantity(q, AREA_TO_M2, field, gaps, notes, allow_zero=False)


def normalize_scheme(payload):
    """把冻结方案（dict）归一化为求解器模型。返回 (model, gaps, notes)。

    单位冲突时 model 为 None（无法求解）；事件倒序不阻断求解（结果仅供参考）。
    """
    gaps, notes = [], []
    model = {"t_max_s": float(payload["solver"]["t_max_s"]),
             "dt_s": float(payload["solver"]["dt_s"]),
             "record_dt_s": float(payload["solver"]["record_dt_s"]),
             "conv_tol_pct": float(payload["solver"]["convergence_tol_pct"]),
             "max_rate": float(payload["solver"]["max_stroke_rate_pct_s"]),
             "ambient_temp_k": float(payload["solver"]["ambient_temp_c"]) + 273.15}

    act = payload["actuator"]
    model["fail_mode"] = act["fail_mode"]
    model["x0"] = float(act["initial_position_pct"])
    model["p_ch0"] = _norm_pressure(act["initial_chamber_pressure"],
                                    "actuator.initial_chamber_pressure", gaps, notes)
    model["chambers"] = {}
    for c in ("a", "b"):
        spec = act[f"chamber_{c}"]
        vmin = _norm_volume(spec["min_volume"], f"chamber_{c}.min_volume", gaps, notes)
        vmax = _norm_volume(spec["max_volume"], f"chamber_{c}.max_volume", gaps, notes)
        if vmin is not None and vmax is not None and vmax <= vmin:
            gaps.append({"code": _GAP_UNIT,
                         "detail": f"chamber_{c}: 最大容积必须大于最小容积"})
            vmin = vmax = None
        model["chambers"][c] = (vmin, vmax)
    model["areas"] = {
        "a": _norm_area(act["area_a"], "actuator.area_a", gaps, notes),
        "b": _norm_area(act["area_b"], "actuator.area_b", gaps, notes),
    }
    if act.get("spring"):
        pts = sorted((float(p[0]), float(p[1])) for p in act["spring"]["points"])
        su = act["spring"].get("force_unit", "n")
        factor = FORCE_TO_N.get(su.strip().lower())
        if factor is None:
            gaps.append({"code": _GAP_UNIT,
                         "detail": f"actuator.spring: 不支持的力单位 {su!r}"})
            spring = None
        else:
            spring = [(x, f * factor) for x, f in pts]
            if factor != 1.0:
                notes.append(f"actuator.spring: {su} 已换算为 N")
        model["spring"] = spring
    else:
        model["spring"] = None
    model["load_n"] = _norm_force(act["load"], "actuator.load", gaps, notes)
    model["friction_n"] = _norm_force(act["friction"], "actuator.friction", gaps, notes)
    model["port_g"] = _norm_quantity(act["port_conductance"], PORT_COND_TO_NL_MIN_KPA,
                                     "actuator.port_conductance", gaps, notes,
                                     allow_zero=False)

    model["p_header"] = _norm_pressure(payload["supply"]["header_pressure"],
                                       "supply.header_pressure", gaps, notes,
                                       allow_zero=False)
    model["segments"] = []
    for i, seg in enumerate(payload["pipe_segments"]):
        model["segments"].append({
            "name": seg.get("name") or f"seg{i}",
            "length_m": float(seg["length_m"]),
            "d_m": float(seg["inner_diameter_mm"]) / 1000.0,
            "friction": float(seg["friction_factor"]),
            "minor_k": float(seg["minor_loss_k"]),
        })
    reg = payload["regulator"]
    model["p_set"] = _norm_pressure(reg["set_pressure"], "regulator.set_pressure",
                                    gaps, notes, allow_zero=False)
    dp_factor = PRESSURE_TO_KPA.get((reg.get("dp_unit") or "kPa").strip().lower())
    if dp_factor is None:
        gaps.append({"code": _GAP_UNIT,
                     "detail": f"regulator: 不支持的压差单位 {reg.get('dp_unit')!r}"})
        dp_factor = 1.0
    flow_factor = None
    if reg.get("flow_unit") is not None:
        flow_factor = FLOW_TO_NL_MIN.get(reg["flow_unit"].strip().lower())
    if flow_factor is None:
        gaps.append({"code": _GAP_UNIT,
                     "detail": f"regulator: 不支持的流量单位 {reg.get('flow_unit')!r}"})
        flow_factor = 1.0
    model["reg_curve"] = sorted(
        (float(p[0]) * dp_factor, float(p[1]) * flow_factor) for p in reg["flow_curve"])
    if model["reg_curve"] and model["reg_curve"][0][0] <= 0 and len(model["reg_curve"]) < 2:
        gaps.append({"code": _GAP_CURVE,
                     "detail": "调压阀流量曲线至少需要一个正压差点"})
    model["tank_v"] = _norm_volume(payload["tank"]["volume"], "tank.volume",
                                   gaps, notes)
    model["p_tank0"] = _norm_pressure(payload["tank"]["initial_pressure"],
                                      "tank.initial_pressure", gaps, notes)

    # ---- 并发用气事件（倒序为证据缺口） ----
    model["events"] = []
    events = []
    for i, ev in enumerate(payload.get("events", [])):
        q = _norm_flow(ev["flow"], f"events[{i}].flow", gaps, notes)
        e = {"device": ev["device"], "t0": float(ev["t_start_s"]),
             "t1": float(ev["t_end_s"]), "q": q if q is not None else 0.0}
        events.append(e)
        if e["t1"] <= e["t0"]:
            gaps.append({"code": _GAP_EVENT_ORDER,
                         "detail": f"并发用气事件 {ev['device']!r} 终点 "
                                   f"{e['t1']:g}s 不晚于起点 {e['t0']:g}s（事件倒序）"})
    if any(events[k]["t0"] < events[k - 1]["t0"] - 1e-9 for k in range(1, len(events))):
        gaps.append({"code": _GAP_EVENT_ORDER,
                     "detail": "并发用气事件未按开始时刻先后排列（事件倒序），"
                               "请按时间顺序提交"})
    model["events"] = events

    # ---- 动作 ----
    air_chambers = list(act.get("air_chambers") or [])
    model["actions"] = []
    for a in payload["actions"]:
        req = _norm_pressure(a["required_supply_pressure_min"],
                             f"actions[{a['name']}].required_supply_pressure_min",
                             gaps, notes)
        driven = "a" if a["direction"] == "open" else "b"
        # 失气安全行程的驱动腔不是供气腔时，由弹簧驱动（供气腔放空）；
        # 否则由储气罐/供气链驱动该腔。
        spring_driven = (a["kind"] == "fail_safe_stroke"
                         and driven not in air_chambers)
        chamber = air_chambers[0] if spring_driven else driven
        model["actions"].append({
            "name": a["name"], "kind": a["kind"], "direction": a["direction"],
            "t_start": float(a["t_start_s"]), "travel_max": float(a["travel_time_s_max"]),
            "p_req_min": req if req is not None else 0.0,
            "band": float(a["safe_band_pct"]),
            "spring_driven": spring_driven, "chamber": chamber,
        })

    if any(g["code"] == _GAP_UNIT for g in gaps):
        return None, gaps, notes
    if model["reg_curve"] and model["reg_curve"][-1][0] <= 0:
        gaps.append({"code": _GAP_CURVE,
                     "detail": "调压阀流量曲线压差范围必须覆盖正压差"})
    return model, gaps, notes


def _first_device(events, t0, t1):
    """区间内最早开始的并发用气设备；无则 (None, [])。"""
    active = [e for e in events if e["t0"] < t1 - 1e-9 and e["t1"] > t0 + 1e-9]
    active.sort(key=lambda e: (e["t0"], e["device"]))
    if not active:
        return None, []
    first = active[0]
    fmt = lambda e: {"device": e["device"], "t_start_s": round(e["t0"], 3),
                     "t_end_s": round(e["t1"], 3),
                     "flow_nl_min": round(e["q"], 3)}
    return fmt(first), [fmt(e) for e in active]


def _build_action_results(model, sim):
    """由逐动作跟踪量生成评估结果：最低供压、行程时间、余量、问题（首个设备与区间）。"""
    out = []
    for tr, a in zip(sim["actions"], model["actions"]):
        t0 = tr["t_start"]
        t_end = tr["t_end"] if tr["t_end"] is not None else sim["t_end_s"]
        stroke_s = tr["t_reached"] - t0 if tr["reached"] else None
        min_p = tr["min_p"] if tr["min_p"] is not None else None
        findings = []

        # 供压低于要求
        if tr["below_intervals"]:
            first_iv = tr["below_intervals"][0]
            dev, active_devs = _first_device(model["events"], first_iv[0], first_iv[1])
            findings.append({
                "kind": "pressure_below_requirement",
                "detail": f"供压（储气罐/母管节点）最低 {min_p:.1f} kPa，"
                          f"低于要求 {a['p_req_min']:g} kPa；首个区间 "
                          f"{first_iv[0]:.3f}–{first_iv[1]:.3f}s"
                          + (f"，首个相关用气设备 {dev['device']!r}" if dev else
                             "，区间内无并发用气设备，瓶颈在供气链（支管/调压阀/储气罐）"),
                "interval_s": [round(first_iv[0], 3), round(first_iv[1], 3)],
                "all_intervals_s": [[round(a0, 3), round(a1, 3)]
                                    for a0, a1 in tr["below_intervals"]],
                "first_device": dev, "active_devices": active_devs,
            })

        # 行程超时 / 未到达
        if not tr["reached"]:
            dev, active_devs = _first_device(model["events"], t0, t_end)
            findings.append({
                "kind": "stroke_timeout",
                "detail": f"评估窗 {t0:.3f}–{t_end:.3f}s 内未到达目标位置"
                          f"（终位 {tr['final_x'] if tr['final_x'] is not None else '—'}%），"
                          f"超过允许行程时间 {a['travel_max']:g}s"
                          + (f"；首个相关用气设备 {dev['device']!r}" if dev else
                             "；区间内无并发用气设备，瓶颈在供气链"),
                "interval_s": [round(t0, 3), round(t_end, 3)],
                "first_device": dev, "active_devices": active_devs,
            })
        elif stroke_s > a["travel_max"] + 1e-9:
            dev, active_devs = _first_device(model["events"], t0, tr["t_reached"])
            findings.append({
                "kind": "stroke_timeout",
                "detail": f"行程时间 {stroke_s:.3f}s 超过允许 {a['travel_max']:g}s"
                          + (f"；首个相关用气设备 {dev['device']!r}" if dev else
                             "；区间内无并发用气设备，瓶颈在供气链"),
                "interval_s": [round(t0, 3), round(tr["t_reached"], 3)],
                "first_device": dev, "active_devices": active_devs,
            })

        # 安全位未到达（仅失气安全行程）
        if a["kind"] == "fail_safe_stroke":
            cause = ("弹簧驱动力不足或排气不畅（弹簧驱动失气行程）"
                     if a.get("spring_driven") else
                     "储气罐存量不足以走完全行程")
            if not tr["reached"]:
                dev, active_devs = _first_device(model["events"], t0, t_end)
                findings.append({
                    "kind": "safe_position_not_reached",
                    "detail": f"失气后未到达安全位（终位 "
                              f"{tr['final_x'] if tr['final_x'] is not None else '—'}%，"
                              f"判定带 ±{a['band']:g}%），{cause}",
                    "interval_s": [round(t0, 3), round(t_end, 3)],
                    "first_device": dev, "active_devices": active_devs,
                })
            elif tr["exit_band_t"] is not None:
                dev, active_devs = _first_device(model["events"],
                                                 tr["exit_band_t"], t_end)
                findings.append({
                    "kind": "safe_position_not_reached",
                    "detail": f"{tr['t_reached']:.3f}s 到达安全位，"
                              f"{tr['exit_band_t']:.3f}s 跌出判定带（供气不足无法保持）",
                    "interval_s": [round(tr["exit_band_t"], 3), round(t_end, 3)],
                    "first_device": dev, "active_devices": active_devs,
                })

        checks = [
            {"metric": "min_supply_pressure_kpa", "value": round(min_p, 2)
             if min_p is not None else None,
             "threshold_min": a["p_req_min"],
             "pass": min_p is not None and min_p >= a["p_req_min"] - 1e-9,
             "basis": "动作区间内储气罐/母管节点最低压力（表压）"},
            {"metric": "stroke_time_s", "value": round(stroke_s, 3)
             if stroke_s is not None else None,
             "threshold_max": a["travel_max"],
             "pass": stroke_s is not None and stroke_s <= a["travel_max"] + 1e-9,
             "basis": "动作开始到阀位进入目标判定带的时间"},
        ]
        if a["kind"] == "fail_safe_stroke":
            checks.append({
                "metric": "safe_position_reached", "value": tr["reached"]
                and tr["exit_band_t"] is None,
                "threshold_min": True,
                "pass": tr["reached"] and tr["exit_band_t"] is None,
                "basis": "失气后到达安全位判定带并保持至动作结束"})
        out.append({
            "name": a["name"], "kind": a["kind"], "direction": a["direction"],
            "spring_driven": a.get("spring_driven", False),
            "t_start_s": round(t0, 3), "t_end_s": round(t_end, 3),
            "target_pct": 100.0 if a["direction"] == "open" else 0.0,
            "safe_band_pct": a["band"],
            "reached": tr["reached"],
            "t_reached_s": round(tr["t_reached"], 3) if tr["t_reached"] is not None else None,
            "stroke_time_s": round(stroke_s, 3) if stroke_s is not None else None,
            "travel_time_s_max": a["travel_max"],
            "time_margin_s": round(a["travel_max"] - stroke_s, 3)
            if stroke_s is not None else None,
            "min_supply_pressure_kpa": round(min_p, 2) if min_p is not None else None,
            "min_supply_pressure_t_s": round(tr["min_p_t"], 3)
            if tr["min_p_t"] is not None else None,
            "required_supply_pressure_min_kpa": a["p_req_min"],
            "pressure_margin_kpa": round(min_p - a["p_req_min"], 2)
            if min_p is not None else None,
            "final_position_pct": round(tr["final_x"], 3)
            if tr["final_x"] is not None else None,
            "air_consumed_nl": round(tr["air_nl"], 3),
            "events_consumed_nl": round(tr["events_nl"], 3),
            "findings": findings,
            "checks": checks,
        })
    return out


def _round_series(sim):
    s = sim["series"]
    return {
        "dt_s": round(s["t"][1] - s["t"][0], 6) if len(s["t"]) > 1 else None,
        "t": [round(v, 3) for v in s["t"]],
        "p_tank_kpa": [round(v, 2) for v in s["p_tank"]],
        "p_junction_kpa": [round(v, 2) for v in s["p_junction"]],
        "p_chamber_a_kpa": [round(v, 2) for v in s["p_ch_a"]],
        "p_chamber_b_kpa": [round(v, 2) for v in s["p_ch_b"]],
        "position_pct": [round(v, 3) for v in s["position"]],
        "q_regulator_nl_min": [round(v, 3) for v in s["q_regulator"]],
        "q_chamber_nl_min": [round(v, 3) for v in s["q_chamber"]],
        "q_events_nl_min": [round(v, 3) for v in s["q_events"]],
    }


def apply_adjustments(base_payload, adjustments):
    """在冻结方案上依次应用修订（并发关系改动 / 实测边界），返回有效方案。

    并发关系改动按设备标识匹配；实测边界覆盖初始压力/初始阀位。
    """
    eff = dict(base_payload)
    eff["actuator"] = dict(base_payload["actuator"])
    eff["supply"] = dict(base_payload["supply"])
    eff["tank"] = dict(base_payload["tank"])
    eff["events"] = [dict(e) for e in base_payload.get("events", [])]
    for adj in adjustments:
        if adj["type"] == "concurrency_override":
            od = adj["override"]
            device = od["device"]
            op = od["operation"]
            idx = next((i for i, e in enumerate(eff["events"])
                        if e["device"] == device), None)
            if op == "remove":
                if idx is None:
                    raise ValueError(f"并发设备 {device!r} 不存在，无法移除")
                eff["events"].pop(idx)
            elif op == "add":
                if idx is not None:
                    raise ValueError(f"并发设备 {device!r} 已存在，无法新增")
                for f in ("t_start_s", "t_end_s", "flow"):
                    if od.get(f) is None:
                        raise ValueError(f"新增并发设备 {device!r} 缺少字段 {f}")
                eff["events"].append({"device": device,
                                      "t_start_s": od["t_start_s"],
                                      "t_end_s": od["t_end_s"],
                                      "flow": od["flow"]})
            else:  # update
                if idx is None:
                    raise ValueError(f"并发设备 {device!r} 不存在，无法更新")
                ev = eff["events"][idx]
                for f in ("t_start_s", "t_end_s", "flow"):
                    if od.get(f) is not None:
                        ev[f] = od[f]
        elif adj["type"] == "measured_boundary":
            mb = adj["boundary"]
            field, value, unit = mb["field"], mb["value"], mb["unit"]
            if field == "header_pressure":
                eff["supply"]["header_pressure"] = {"value": value, "unit": unit}
            elif field == "tank_initial_pressure":
                eff["tank"]["initial_pressure"] = {"value": value, "unit": unit}
            elif field == "chamber_initial_pressure":
                eff["actuator"]["initial_chamber_pressure"] = {"value": value,
                                                               "unit": unit}
            elif field == "initial_position_pct":
                if (unit or "").strip() != "%":
                    raise ValueError("initial_position_pct 的单位必须为 %")
                if not (0.0 <= value <= 100.0):
                    raise ValueError("initial_position_pct 须在 0–100 之间")
                eff["actuator"]["initial_position_pct"] = value
            else:
                raise ValueError(f"不支持的实测边界字段 {field!r}")
        else:
            raise ValueError(f"不支持的修订类型 {adj['type']!r}")
    return eff


def run_solve(db, scheme_id, author="auto", new_adjustments=None):
    """求解当前有效方案并写入新修订。返回修订记录。"""
    scheme = db.get_airsupply_scheme(scheme_id)
    if scheme is None:
        raise KeyError(f"供气瞬态核算方案 {scheme_id} 不存在")
    new_adjustments = new_adjustments or []

    existing = db.list_airsupply_revisions(scheme_id)
    cumulative = []
    if existing:
        latest = db.get_airsupply_revision(existing[-1]["id"])
        cumulative = list(latest["adjustments"])
    for adj in new_adjustments:
        if adj["type"] == "concurrency_override":
            cumulative.append({"type": "concurrency_override", "author": author,
                               "override": adj["override"], "reason": adj["reason"]})
        else:
            cumulative.append({"type": "measured_boundary", "author": author,
                               "boundary": adj["boundary"], "reason": adj["reason"]})

    effective = apply_adjustments(scheme["payload"], cumulative)
    model, gaps, notes = normalize_scheme(effective)

    series = None
    actions = []
    totals = None
    solver_info = None
    if model is not None:
        fine, convergence = solver.solve(model)
        series = _round_series(fine)
        actions = _build_action_results(model, fine)
        totals = {k: (round(v, 3) if isinstance(v, float) else v)
                  for k, v in fine["totals"].items()}
        solver_info = {"dt_s": convergence["dt_s"], "steps": fine["steps"],
                       "converged": convergence["converged"],
                       "max_rel_diff_pct": convergence["max_rel_diff_pct"],
                       "tolerance_pct": convergence["tolerance_pct"],
                       "t_max_s": model["t_max_s"],
                       "series_dt_s": series["dt_s"]}
        if convergence["converged"] is False:
            gaps.append({
                "code": _GAP_CONVERGE,
                "detail": f"步长 {convergence['dt_s']}s 与 {convergence['dt_s'] / 2}s "
                          f"的结果相对偏差 {convergence['max_rel_diff_pct']}% "
                          f"超过允许 {convergence['tolerance_pct']}%，积分不收敛",
                "details": convergence["actions"],
            })
        cov = model["reg_curve"][-1][0] if model["reg_curve"] else 0.0
        if model["tank_v"] is not None and totals["max_reg_dp_kpa"] > cov + 1e-9:
            gaps.append({
                "code": _GAP_CURVE,
                "detail": f"调压阀运行压差最大 {totals['max_reg_dp_kpa']:.1f} kPa，"
                          f"超出流量曲线覆盖上限 {cov:g} kPa；覆盖不足段按曲线末端流量 "
                          f"{model['reg_curve'][-1][1]:g} Nl/min 截断，结果仅为指示性",
                "demanded_dp_kpa": round(totals["max_reg_dp_kpa"], 2),
                "curve_max_dp_kpa": cov,
            })

    issues = []
    for a in actions:
        for f in a["findings"]:
            issues.append({"action": a["name"], **f})

    if gaps:
        verdict = "no_conclusion"
    elif any(not c["pass"] for a in actions for c in a["checks"]):
        verdict = "fail"
    else:
        verdict = "pass"

    basis = []
    for a in actions:
        for c in a["checks"]:
            line = f"{a['name']}/{c['metric']}："
            if c["value"] is not None:
                line += f"实测 {c['value']}"
            else:
                line += "无有效值"
            if "threshold_min" in c:
                line += f"，阈值 ≥ {c['threshold_min']}"
            if "threshold_max" in c:
                line += f"，阈值 ≤ {c['threshold_max']}"
            line += f"，判定 {'通过' if c['pass'] else '不通过'}"
            if c.get("basis"):
                line += f"（依据：{c['basis']}）"
            basis.append(line)

    result = {
        "scheme_id": scheme_id,
        "valve_id": scheme["valve_id"],
        "scheme_name": scheme["name"],
        "verdict": verdict,
        "evidence_gaps": gaps,
        "unit_notes": notes,
        "solver": solver_info,
        "actions": actions,
        "issues": issues,
        "events": [{"device": e["device"], "t_start_s": e["t0"], "t_end_s": e["t1"],
                    "flow_nl_min": round(e["q"], 3)} for e in (model["events"] if model else [])],
        "totals": totals,
        "series": series,
        "decision_basis": basis,
    }
    revision_id, revision = db.create_airsupply_revision(
        scheme_id, author, cumulative, effective, result)
    result["revision_id"] = revision_id
    result["revision"] = revision
    db.update_airsupply_revision_result(revision_id, result)
    return db.get_airsupply_revision(revision_id)
