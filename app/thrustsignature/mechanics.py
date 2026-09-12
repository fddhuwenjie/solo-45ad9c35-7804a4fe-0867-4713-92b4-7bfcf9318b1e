"""执行机构力学模型：面积/弹簧力单位统一、弹簧曲线校验、阀杆净推力计算。

符号约定：阀杆推力沿**开阀方向为正**（与阀位 % 同向）。

- 双作用执行器：F_net = pA·A_a − pB·A_b（无弹簧）。
- 单作用气开弹簧关（fail_close）：工作腔为开阀侧 A 腔，
  F_net = pA·A_a − Fs(x)，弹簧力随阀位增大而增大。
- 单作用气关弹簧开（fail_open）：工作腔为关阀侧（A 腔），
  F_net = −pA·A_a + Fs(x)，弹簧力随阀位增大而减小。

弹簧曲线以「力的大小（正值）+ 作用方式」给出，方向由本模块统一换算，
避免提交方同时处理符号导致量纲/符号混淆。
"""

# 面积 → m²
AREA_TO_M2 = {
    "cm2": 1e-4,
    "m2": 1.0,
    "in2": 0.0254 ** 2,
}

# 力 → N
FORCE_TO_N = {
    "n": 1.0,
    "kn": 1000.0,
}


def normalize_area(spec):
    """面积规格换算为 m²。spec: {value, unit}。"""
    factor = AREA_TO_M2.get((spec.get("unit") or "cm2").lower())
    if factor is None:
        raise ValueError(f"不支持的面积单位 {spec.get('unit')!r}")
    return float(spec["value"]) * factor


def validate_spring(spring, travel_lo_pct, travel_hi_pct, coverage_margin_pct):
    """校验弹簧曲线：单位、单调性与试验行程覆盖。

    返回 (force_fn_or_None, problems)。force_fn(position_pct) -> 弹簧力大小 N；
    problems 为 [{code, detail}]。
    """
    problems = []
    if spring is None:
        problems.append({"code": "spring_curve_missing",
                         "detail": "单作用执行器必须提供弹簧曲线"})
        return None, problems

    pts = spring.get("points") or []
    if len(pts) < 2:
        problems.append({"code": "spring_curve_invalid",
                         "detail": "弹簧曲线至少需要 2 个点"})
        return None, problems

    xs, fs_raw = [], []
    force_unit = (spring.get("force_unit") or "n").lower()
    factor = FORCE_TO_N.get(force_unit)
    if factor is None:
        problems.append({"code": "spring_curve_invalid",
                         "detail": f"不支持的弹簧力单位 {force_unit!r}"})
        return None, problems

    prev_x, prev_f = None, None
    action = spring.get("action", "fail_close")
    for k, p in enumerate(pts):
        if len(p) < 2:
            problems.append({"code": "spring_curve_invalid",
                             "detail": f"弹簧曲线第 {k} 点格式错误"})
            return None, problems
        x, f = float(p[0]), float(p[1]) * factor
        if f < 0.0:
            problems.append({"code": "spring_curve_invalid",
                             "detail": f"弹簧力应为非负大小，第 {k} 点为 {p[1]} "
                                       f"{force_unit}"})
            return None, problems
        if prev_x is not None and x <= prev_x:
            problems.append({"code": "spring_curve_invalid",
                             "detail": f"弹簧曲线阀位必须严格升序，"
                                       f"第 {k - 1}、{k} 点阀位 {prev_x}→{x}"})
            return None, problems
        if prev_f is not None:
            # fail_close：阀位越大弹簧压缩越大（力升）；fail_open 反之
            rising = f >= prev_f
            expected = (action == "fail_close")
            if rising != expected:
                problems.append({"code": "spring_curve_invalid",
                                 "detail": f"弹簧单调性与作用方式 {action} 不符："
                                           f"阀位 {prev_x}%→{x}% 时力 "
                                           f"{prev_f / factor:g}→{f / factor:g} "
                                           f"{force_unit}"})
                return None, problems
        xs.append(x)
        fs_raw.append(f)
        prev_x, prev_f = x, f

    # 行程覆盖（带裕度）
    m = float(coverage_margin_pct)
    if xs[0] > travel_lo_pct + m or xs[-1] < travel_hi_pct - m:
        problems.append({
            "code": "spring_curve_coverage",
            "detail": f"弹簧曲线阀位覆盖 [{xs[0]}%, {xs[-1]}%]，"
                      f"不能覆盖试验行程 [{travel_lo_pct:.1f}%, {travel_hi_pct:.1f}%]"
                      f"（裕度 ±{m:g}%），弹簧力需外推，推力签名不可信"})

    def force_fn(pos_pct):
        if pos_pct <= xs[0]:
            return fs_raw[0]
        if pos_pct >= xs[-1]:
            return fs_raw[-1]
        for k in range(1, len(xs)):
            if xs[k] >= pos_pct:
                span = xs[k] - xs[k - 1]
                f = (pos_pct - xs[k - 1]) / span
                return fs_raw[k - 1] + f * (fs_raw[k] - fs_raw[k - 1])
        return fs_raw[-1]

    return force_fn, problems


def actuator_config(actuator):
    """整理执行机构配置。返回 dict（面积统一 m²）或抛 ValueError（量纲不符）。"""
    act_type = actuator["actuator_type"]
    a_a = normalize_area(actuator["area_a"])
    a_b = None
    if actuator.get("area_b"):
        a_b = normalize_area(actuator["area_b"])
    elif act_type == "double_acting":
        a_b = a_a   # 双作用等面积活塞（双气缸），缺省视为 A=B
    cfg = {
        "actuator_type": act_type,
        "area_a_m2": a_a,
        "area_b_m2": a_b,
        "area_b_assumed_equal": act_type == "double_acting" and a_b == a_a
        and not actuator.get("area_b"),
        "spring": actuator.get("spring"),
        "spring_action": (actuator.get("spring") or {}).get("action", "fail_close"),
        "stem_direction": actuator.get("stem_direction", "down_to_close"),
    }
    return cfg


def thrust_curve(grid_pos_pct, p_a_kpa, p_b_kpa, p_supply_kpa, cfg, spring_fn):
    """逐点计算阀杆净推力（开阀方向为正，N）。

    p_a_kpa/p_supply_kpa 必须提供（已对齐）；p_b_kpa 仅双作用使用。
    返回 list[dict]：每点净推力与分解项。
    """
    a_a, a_b = cfg["area_a_m2"], cfg["area_b_m2"]
    act_type = cfg["actuator_type"]
    out = []
    for x, pa, psup in zip(grid_pos_pct, p_a_kpa, p_supply_kpa):
        fa = pa * a_a  # kPa·m² = kN → ×1000 = N
        fa_n = fa * 1000.0
        fs_n = spring_fn(x) if spring_fn is not None else 0.0
        if act_type == "double_acting":
            fb_n = (p_b_kpa[len(out)] * a_b * 1000.0
                    if p_b_kpa is not None and a_b else 0.0)
            f_net = fa_n - fb_n
            out.append({"f_net_n": f_net, "f_chamber_a_n": fa_n,
                        "f_chamber_b_n": fb_n, "f_spring_n": 0.0})
        elif cfg["spring_action"] == "fail_close":
            # 气开弹簧关：A 腔推动开阀
            f_net = fa_n - fs_n
            out.append({"f_net_n": f_net, "f_chamber_a_n": fa_n,
                        "f_chamber_b_n": 0.0, "f_spring_n": fs_n})
        else:
            # 气关弹簧开：A 腔推动关阀（负方向），弹簧推开阀
            f_net = -fa_n + fs_n
            out.append({"f_net_n": f_net, "f_chamber_a_n": -fa_n,
                        "f_chamber_b_n": 0.0, "f_spring_n": fs_n})
    return out
