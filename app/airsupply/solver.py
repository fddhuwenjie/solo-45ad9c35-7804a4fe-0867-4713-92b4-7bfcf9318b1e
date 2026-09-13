"""气动执行机构供气瞬态集总参数求解器。

拓扑（与方案记录一一对应）：

    气源总管（恒压） → 供气管段（串联，Darcy-Weisbach 压降） → 支管节点 J
    （并发用气事件在此耗气） → 调压阀（压差-流量曲线，出口受设定压力限制）
    → 供气母管节点 A（储气罐直接挂接，罐压即供压） → 腔口（线性导流）
    → 被驱动腔；对侧腔直通大气（放空瞬态不建模，视为即时）。

时间步推进量：罐压、支管节点压力、两腔气体存量（P_abs·V）、阀位、
瞬时耗气量（调压阀流量 / 腔口流量 / 并发事件流量）。

阀位按**拟静态安置**：被驱动腔当前存气量能覆盖某位置所需存量
（所需腔压 × 该位置腔容积）即可占据该位置，否则停待充压；
供气不足、腔存气量低于当前位置所需时被弹簧/负载推回。
失气安全行程：总管隔离（调压阀流量为零、并发事件停供），仅靠储气罐驱动。

积分收敛性：同一模型按 dt 与 dt/2 各算一遍，逐动作比较行程时间、
最低供压与终位，超差即判不收敛（证据缺口），结果取更细步长。
"""

import math

P_ATM_KPA = 101.325       # 大气压
P_REF_KPA = 101.325       # 标准状态压力
T_REF_K = 288.15          # 标准状态温度（15 °C）
R_AIR = 287.05            # 空气气体常数 J/(kg·K)


def _interp(points, x):
    """线性插值；points 为 [(x, y), ...] 升序，超出范围夹取端点。"""
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    lo, hi = 0, len(points) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if points[mid][0] <= x:
            lo = mid
        else:
            hi = mid
    x0, y0 = points[lo]
    x1, y1 = points[hi]
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def _seg_drop_kpa(q_nl_min, p_up_gauge, seg, t_kelvin):
    """管段压降 kPa（Darcy-Weisbach + 局部阻力；密度按段上游压力，显式）。"""
    if q_nl_min <= 0.0:
        return 0.0
    p_abs_pa = max(p_up_gauge + P_ATM_KPA, 1.0) * 1000.0
    q_actual = (q_nl_min * 1e-3 / 60.0) * (P_REF_KPA * 1000.0 / p_abs_pa) \
        * (t_kelvin / T_REF_K)
    area = math.pi / 4.0 * seg["d_m"] ** 2
    v = q_actual / area
    rho = p_abs_pa / (R_AIR * t_kelvin)
    dp_pa = (seg["friction"] * seg["length_m"] / seg["d_m"] + seg["minor_k"]) \
        * rho * v * v / 2.0
    return dp_pa / 1000.0


def _reg_flow(model, dp_kpa):
    """调压阀流量 Nl/min：压差-流量曲线（原点锚定，超出覆盖范围夹取端点）。"""
    if dp_kpa <= 0.0:
        return 0.0
    curve = model["reg_curve"]
    if dp_kpa < curve[0][0]:
        return curve[0][1] * dp_kpa / curve[0][0] if curve[0][0] > 0 else curve[0][1]
    return _interp(curve, dp_kpa)


def _junction(model, p_tank, q_events, supply_lost, t_kelvin):
    """不动点迭代支管节点压力与调压阀流量。

    返回 (p_j, q_reg, dp_demanded)。dp_demanded 仅统计调压阀实际通流
    （罐压低于设定值且未失气）时的运行压差，用于流量曲线覆盖核查。
    """
    if supply_lost:
        return model["p_header"], 0.0, 0.0
    p_j = model["p_header"]
    q_reg = 0.0
    dp_demanded = 0.0
    for _ in range(16):
        dp = max(p_j - p_tank, 0.0)
        if p_tank >= model["p_set"]:
            q_reg = 0.0
            dp_demanded = 0.0
        else:
            q_reg = _reg_flow(model, dp)
            dp_demanded = dp
        q_tot = q_reg + q_events
        p_up = model["p_header"]
        drop = 0.0
        for seg in model["segments"]:
            d = _seg_drop_kpa(q_tot, p_up, seg, t_kelvin)
            drop += d
            p_up = max(p_up - d, 0.0)
        p_j_new = max(model["p_header"] - drop, 0.0)
        if abs(p_j_new - p_j) < 1e-9:
            p_j = p_j_new
            break
        p_j = 0.5 * (p_j + p_j_new)
    return p_j, q_reg, dp_demanded


def _chamber_vol(model, chamber, x_pct):
    """腔容积 m³：A 腔随开度增大，B 腔随关度增大（线性于阀位）。"""
    vmin, vmax = model["chambers"][chamber]
    frac = x_pct / 100.0 if chamber == "a" else (100.0 - x_pct) / 100.0
    return vmin + (vmax - vmin) * frac


def _p_req_gauge(model, chamber, direction, x_pct):
    """该位置推动阀位所需被驱动腔表压 kPa（弹簧按故障模式取向后 ±，负载/摩擦恒阻碍运动）。"""
    area = model["areas"][chamber]
    f_spring = _interp(model["spring"], x_pct) if model["spring"] else 0.0
    if direction == "open":
        s = 1.0 if model["fail_mode"] == "fail_close" else -1.0
    else:
        s = 1.0 if model["fail_mode"] == "fail_open" else -1.0
    f_need = s * f_spring + model["load_n"] + model["friction_n"]
    return max(f_need / area / 1000.0, 0.0)


def _g_req(model, chamber, direction, x_pct):
    """占据 x_pct 位置所需被驱动腔气体存量（kPa·m³，等温）。"""
    return (_p_req_gauge(model, chamber, direction, x_pct) + P_ATM_KPA) \
        * _chamber_vol(model, chamber, x_pct)


def _walk(model, chamber, direction, inv_c, x0, x1, advance):
    """从 x0 向 x1 拟静态行走，返回能到达的最远位置。

    advance=True：条件 g(x) ≤ inv_c 成立才能前进，遇到 g > inv_c 停止；
    advance=False（回退）：条件 g(x) > inv_c 才继续回退，遇到 g ≤ inv_c 停止。
    8 等分粗扫定位穿越点后二分 24 次。
    """
    eps = 1e-12
    if x1 == x0:
        return x0

    def _ok(xx):
        g = _g_req(model, chamber, direction, xx)
        return (g <= inv_c + eps) if advance else (g > inv_c + eps)

    prev = x0
    for k in range(1, 9):
        xx = x0 + (x1 - x0) * k / 8.0
        if not _ok(xx):
            lo, hi = prev, xx
            for _ in range(24):
                mid = 0.5 * (lo + hi)
                if _ok(mid):
                    lo = mid
                else:
                    hi = mid
            return lo
        prev = xx
    return x1


def _place_valve(model, chamber, direction, inv_c, x_pct, dt):
    """拟静态安置阀位：前进受限于存气量与最大速率；存量不足时回退。"""
    s = 1.0 if direction == "open" else -1.0
    x_target = 100.0 if direction == "open" else 0.0
    max_dx = model["max_rate"] * dt
    if _g_req(model, chamber, direction, x_pct) <= inv_c + 1e-12:
        x_cand = x_pct + s * max_dx
        x_cand = min(max(x_cand, 0.0), 100.0)
        x_cand = min(x_cand, x_target) if s > 0 else max(x_cand, x_target)
        return _walk(model, chamber, direction, inv_c, x_pct, x_cand, True)
    x_cand = x_pct - s * max_dx
    x_cand = min(max(x_cand, 0.0), 100.0)
    return _walk(model, chamber, direction, inv_c, x_pct, x_cand, False)


def simulate(model, dt):
    """按定步长推进仿真。返回逐时序列、逐动作跟踪结果与总量。"""
    t_kelvin = model["ambient_temp_k"]
    k_nl = P_REF_KPA * 1e-3 * (t_kelvin / T_REF_K)  # 1 Nl 对应的 kPa·m³（等温）
    n_steps = max(1, int(math.ceil(model["t_max_s"] / dt)))
    record_every = max(1, int(round(model["record_dt_s"] / dt)))

    p_tank = model["p_tank0"]
    x = model["x0"]
    inv = {
        "a": (model["p_ch0"] + P_ATM_KPA) * _chamber_vol(model, "a", x),
        "b": (model["p_ch0"] + P_ATM_KPA) * _chamber_vol(model, "b", x),
    }
    vented = {"a": False, "b": False}
    p_j = model["p_header"]
    q_reg = q_port = q_events = 0.0
    supply_lost = False

    actions = sorted(model["actions"], key=lambda a: a["t_start"])
    trackers = []
    for a in actions:
        trackers.append({
            "name": a["name"], "kind": a["kind"], "direction": a["direction"],
            "t_start": a["t_start"], "t_end": None,
            "min_p": None, "min_p_t": None,
            "reached": False, "t_reached": None, "exit_band_t": None,
            "final_x": None, "air_nl": 0.0, "events_nl": 0.0,
            "below_intervals": [], "_below_start": None,
        })
    cur = -1  # 当前动作序号（-1 = 尚无动作）
    driven = None

    totals = {"air_to_actuator_nl": 0.0, "events_consumed_nl": 0.0,
              "min_tank_pressure_kpa": p_tank, "min_tank_pressure_t_s": 0.0,
              "max_reg_dp_kpa": 0.0}
    series = {"t": [], "p_tank": [], "p_junction": [], "p_ch_a": [], "p_ch_b": [],
              "position": [], "q_regulator": [], "q_chamber": [], "q_events": []}

    for step in range(n_steps + 1):
        t = step * dt

        # ---- 动作切换 ----
        nxt = cur + 1
        if nxt < len(actions) and actions[nxt]["t_start"] <= t + 1e-12:
            if cur >= 0:
                tr = trackers[cur]
                tr["t_end"] = t
                tr["final_x"] = x
                if tr["_below_start"] is not None:
                    tr["below_intervals"].append([tr["_below_start"], t])
                    tr["_below_start"] = None
            cur = nxt
            a = actions[cur]
            driven = "a" if a["direction"] == "open" else "b"
            other = "b" if driven == "a" else "a"
            inv[other] = P_ATM_KPA * _chamber_vol(model, other, x)
            vented[other] = True
            vented[driven] = False
            if a["kind"] == "fail_safe_stroke":
                supply_lost = True

        active = cur >= 0
        chamber = driven if active else None

        # ---- 瞬时耗气：并发事件（失气后停供） ----
        q_events = 0.0 if supply_lost else sum(
            e["q"] for e in model["events"] if e["t0"] <= t < e["t1"])

        # ---- 支管节点与调压阀 ----
        p_j, q_reg, dp_demanded = _junction(model, p_tank, q_events, supply_lost,
                                            t_kelvin)
        if not supply_lost and p_tank < model["p_set"]:
            totals["max_reg_dp_kpa"] = max(totals["max_reg_dp_kpa"], dp_demanded)

        # ---- 腔口流量（母管 ↔ 被驱动腔） ----
        if active:
            p_ch = inv[chamber] / _chamber_vol(model, chamber, x) - P_ATM_KPA
            q_port = model["port_g"] * (p_tank - p_ch)
        else:
            q_port = 0.0
        if q_port > 0.0:  # 罐存量约束
            avail = q_reg + p_tank * model["tank_v"] * 60.0 / (k_nl * dt)
            q_port = max(min(q_port, avail), 0.0)
        elif q_port < 0.0:  # 腔存量不得破真空
            min_inv = P_ATM_KPA * _chamber_vol(model, chamber, x)
            out_max = (inv[chamber] - min_inv) * 60.0 / (k_nl * dt)
            q_port = -max(min(-q_port, out_max), 0.0)

        # ---- 罐压与腔存量更新 ----
        p_tank = max(p_tank + (q_reg - q_port) / 60.0 * k_nl / model["tank_v"] * dt,
                     0.0)
        if active:
            inv[chamber] += q_port / 60.0 * k_nl * dt
            x = _place_valve(model, chamber, actions[cur]["direction"],
                             inv[chamber], x, dt)
            other = "b" if chamber == "a" else "a"
            if vented[other]:
                inv[other] = P_ATM_KPA * _chamber_vol(model, other, x)

        # ---- 跟踪量 ----
        if active:
            tr = trackers[cur]
            a = actions[cur]
            if tr["min_p"] is None or p_tank < tr["min_p"]:
                tr["min_p"], tr["min_p_t"] = p_tank, t
            tr["air_nl"] += max(q_port, 0.0) / 60.0 * dt
            tr["events_nl"] += q_events / 60.0 * dt
            totals["air_to_actuator_nl"] += max(q_port, 0.0) / 60.0 * dt
            totals["events_consumed_nl"] += q_events / 60.0 * dt
            band = a["band"]
            if a["direction"] == "open":
                in_band = x >= 100.0 - band
            else:
                in_band = x <= band
            if not tr["reached"] and in_band:
                tr["reached"] = True
                tr["t_reached"] = t
            elif tr["reached"] and not in_band and tr["exit_band_t"] is None:
                tr["exit_band_t"] = t
            if p_tank < a["p_req_min"] - 1e-9:
                if tr["_below_start"] is None:
                    tr["_below_start"] = t
            elif tr["_below_start"] is not None:
                tr["below_intervals"].append([tr["_below_start"], t])
                tr["_below_start"] = None
        if p_tank < totals["min_tank_pressure_kpa"]:
            totals["min_tank_pressure_kpa"] = p_tank
            totals["min_tank_pressure_t_s"] = t

        # ---- 逐时记录 ----
        if step % record_every == 0 or step == n_steps:
            series["t"].append(t)
            series["p_tank"].append(p_tank)
            series["p_junction"].append(p_j)
            series["p_ch_a"].append(inv["a"] / _chamber_vol(model, "a", x)
                                    - P_ATM_KPA)
            series["p_ch_b"].append(inv["b"] / _chamber_vol(model, "b", x)
                                    - P_ATM_KPA)
            series["position"].append(x)
            series["q_regulator"].append(q_reg)
            series["q_chamber"].append(q_port)
            series["q_events"].append(q_events)

    # ---- 收尾 ----
    if cur >= 0:
        tr = trackers[cur]
        tr["t_end"] = n_steps * dt
        tr["final_x"] = x
        if tr["_below_start"] is not None:
            tr["below_intervals"].append([tr["_below_start"], n_steps * dt])
            tr["_below_start"] = None
    for i, tr in enumerate(trackers):
        if tr["t_end"] is None:  # 未排到的动作（评估窗不足）
            tr["t_end"] = n_steps * dt
            tr["final_x"] = None
    return {"series": series, "actions": trackers, "totals": totals,
            "t_end_s": n_steps * dt, "steps": n_steps}


def solve(model):
    """收敛性核查：dt 与 dt/2 各算一遍，逐动作比较。返回 (细步结果, 收敛核查)。"""
    dt = model["dt_s"]
    coarse = simulate(model, dt)
    fine = simulate(model, dt / 2.0)
    tol = model["conv_tol_pct"] / 100.0
    converged = True
    max_rel = 0.0
    details = []
    for a, b in zip(coarse["actions"], fine["actions"]):
        entry = {"action": b["name"], "checks": []}
        if a["reached"] != b["reached"]:
            converged = False
            entry["checks"].append({"quantity": "reached", "ok": False,
                                    "coarse": a["reached"], "fine": b["reached"]})
        if a["reached"] and b["reached"]:
            d = abs(a["t_reached"] - b["t_reached"])
            lim = max(tol * b["t_reached"], 2.0 * dt)
            ok = d <= lim + 1e-12
            converged = converged and ok
            rel = d / max(b["t_reached"], 1e-9)
            max_rel = max(max_rel, rel)
            entry["checks"].append({"quantity": "stroke_time_s", "ok": ok,
                                    "coarse": a["t_reached"], "fine": b["t_reached"],
                                    "abs_diff": round(d, 6), "limit": round(lim, 6)})
        else:
            d = abs((a["final_x"] or 0.0) - (b["final_x"] or 0.0))
            lim = max(tol * 100.0, 1.0)
            ok = d <= lim + 1e-12
            converged = converged and ok
            max_rel = max(max_rel, d / 100.0)
            entry["checks"].append({"quantity": "final_position_pct", "ok": ok,
                                    "coarse": a["final_x"], "fine": b["final_x"],
                                    "abs_diff": round(d, 6), "limit": round(lim, 6)})
        d = abs((a["min_p"] or 0.0) - (b["min_p"] or 0.0))
        lim = max(tol * (b["min_p"] or 0.0), 1.0)
        ok = d <= lim + 1e-12
        converged = converged and ok
        max_rel = max(max_rel, d / max(b["min_p"] or 0.0, 1e-9))
        entry["checks"].append({"quantity": "min_supply_pressure_kpa",
                                "ok": ok, "coarse": a["min_p"], "fine": b["min_p"],
                                "abs_diff": round(d, 6), "limit": round(lim, 6)})
        details.append(entry)
    return fine, {"converged": converged, "max_rel_diff_pct": round(max_rel * 100.0, 4),
                  "tolerance_pct": round(tol * 100.0, 6), "dt_s": dt,
                  "actions": details}
