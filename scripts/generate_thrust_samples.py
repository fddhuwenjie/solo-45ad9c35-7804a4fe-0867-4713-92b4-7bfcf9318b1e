#!/usr/bin/env python3
"""生成阀杆推力签名测试请求样例。

物理模型（单作用气开弹簧关，A 腔有效面积 200 cm²）：
    开阀 F_net = pA·A − Fs(x) − f_run（匀速段）
    关阀 F_net = Fs(x) − pA·A − f_run
弹簧 220 N（0%）→ 2200 N（100%）；正常填料运行摩擦约 300 N，
供气压 500 kPa 时最大推力 pA·A = 10000 N，裕量充足。

通道异频：指令 20Hz、阀位 10Hz、供气 5Hz、A 腔 2Hz（双作用样例 B 腔 1Hz）。

样例清单（同阀 TV-701 的 good/good2/pre/post 可用于检修前后比较）：
- sample_thrust_good            正常：启动力/摩擦/离座/落座全部在限内
- sample_thrust_good2           正常复测（双作用执行器，结构不兼容 → 比较时列不可比）
- sample_thrust_packing_high    检修前：填料摩擦大（运行摩擦与摩擦带超限，阀体嫌疑）
- sample_thrust_post_overhaul   检修后：填料更换，可与前者配对量化改善
- sample_thrust_sticky_breakaway 启程卡涩：启动力超限但匀速摩擦正常（执行机构/初始卡滞）
- sample_thrust_seat_overload   落座过压：落座力超上限
- sample_thrust_missing_chamber 缺 A 腔压力通道 → 证据缺口
- sample_thrust_no_overlap      A 腔压力时间轴与其他通道无重叠 → 证据缺口
- sample_thrust_bad_area_unit   面积单位声明为 mm2（量纲不符）→ 证据缺口
- sample_thrust_cal_expired     校准过期 → 证据缺口
- sample_thrust_spring_short    弹簧曲线只覆盖 20–80%，不覆盖全行程 → 证据缺口
"""

import json
import math
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "samples"
random.seed(701)

# ---- 时序 ----
T0_OPEN_CMD = 4.0
T_O_MOVE = 4.6          # A 腔建压/启程结束，阀杆开始移动
T_O_SEAT = 5.3          # 离开关位带（离座结束）
T_OPEN_END = 29.2       # 到开位
T_CLOSE_CMD = 32.0
T_C_MOVE = 32.6         # 排气/启程结束，阀杆开始关
T_C_SEAT = 55.0         # 进入关位带（落座开始）
T_CLOSE_END = 56.0
T_END = 60.0
SEAT_BAND = 2.0

# ---- 执行机构参数 ----
AREA_CM2 = 200.0
A_M2 = AREA_CM2 * 1e-4          # 0.02 m²
FS_0, FS_100 = 1300.0, 3000.0   # 弹簧预紧 / 满行程弹簧力 N
P_SUPPLY = 500.0                # 供气 kPa
T_MOVE = T0_OPEN_CMD + 0.5      # 开阀开始移动（启程结束）
BREAKAWAY_TAU = 0.06            # 启程附加压力峰值衰减时间常数


T_MOVE = T_O_MOVE             # 开阀开始移动（启程结束）
BREAKAWAY_TAU = 0.3           # 启程附加压力峰值衰减时间常数


def spring_force(x_pct):
    return FS_0 + (FS_100 - FS_0) * x_pct / 100.0


def position_at(t):
    if t < T_O_MOVE:
        return 0.0
    if t < T_O_SEAT:
        # 离座相位（在座内缓慢移动）
        return SEAT_BAND * (t - T_O_MOVE) / (T_O_SEAT - T_O_MOVE)
    if t < T_OPEN_END:
        # 匀速开
        v = (100.0 - SEAT_BAND) / (T_OPEN_END - T_O_SEAT)
        return SEAT_BAND + v * (t - T_O_SEAT)
    if t < T_C_MOVE:
        return 100.0
    if t < T_C_SEAT:
        # 匀速关
        v = (100.0 - SEAT_BAND) / (T_C_SEAT - T_C_MOVE)
        return 100.0 - v * (t - T_C_MOVE)
    if t < T_CLOSE_END:
        # 落座相位（座内压紧）
        return SEAT_BAND * max(0.0, (T_CLOSE_END - t) / (T_CLOSE_END - T_C_SEAT))
    return 0.0


def command_at(t):
    if t < T0_OPEN_CMD:
        return 0.0
    if t < T_OPEN_END:
        return min(100.0, 100.0 * (t - T0_OPEN_CMD) / (T_OPEN_END - T0_OPEN_CMD))
    if t < T_CLOSE_CMD:
        return 100.0
    if t < T_CLOSE_END:
        return max(0.0, 100.0 * (T_CLOSE_END - t) / (T_CLOSE_END - T_CLOSE_CMD))
    return 0.0


def chamber_a_pressure(t, x, f_run, breakaway_extra=0.0, unseat_extra=0.0,
                       seat_extra=0.0):
    """按目标净推力反推单作用 A 腔压力（气开弹簧关）。"""
    fs = spring_force(x)
    noise = random.uniform(-2.0, 2.0)
    if t < T0_OPEN_CMD:
        return 0.0
    if t < T_O_MOVE:
        # 启程：建压到克服弹簧预紧+静摩擦+启程附加
        frac = (t - T0_OPEN_CMD) / (T_O_MOVE - T0_OPEN_CMD)
        pa = frac * (FS_0 + f_run * 1.4 + breakaway_extra) / (A_M2 * 1000.0)
        return pa + noise * 0.3
    if t < T_O_SEAT:
        # 离座：克服弹簧+摩擦+阀座附加；启程附加快速衰减
        decay = breakaway_extra * math.exp(-(t - T_O_MOVE) / BREAKAWAY_TAU)
        return (fs + f_run + unseat_extra + decay) / (A_M2 * 1000.0) + noise
    if t < T_OPEN_END:
        return (fs + f_run) / (A_M2 * 1000.0) + noise   # 匀速开
    if t < T_C_MOVE:
        return P_SUPPLY                                     # 开位保持
    if t < T_C_SEAT:
        return max(0.0, (fs - f_run) / (A_M2 * 1000.0)) + noise  # 匀速关，排气
    if t < T_CLOSE_END:
        # 落座：排气到残余背压；seat_extra>0 时背压顶住 → 落座过压
        frac = (t - T_C_SEAT) / (T_CLOSE_END - T_C_SEAT)
        base = (FS_0 - f_run) / (A_M2 * 1000.0)
        target = base if seat_extra > 0 else base * (1.0 - 0.9 * frac)
        return max(0.0, target) + noise * 0.3
    return max(0.0, noise * 0.2)


def supply_at(t, low_dip=False):
    if t < T0_OPEN_CMD:
        return P_SUPPLY
    dip = 0.0
    if low_dip and T0_OPEN_CMD <= t <= T_O_MOVE + 1.5:
        dip = 120.0
    return P_SUPPLY - dip + random.uniform(-2.0, 2.0)


def times(dt, t_start=0.0, t_end=T_END):
    n = int((t_end - t_start) / dt) + 1e-9
    return [round(t_start + i * dt, 6) for i in range(int(n) + 1)]


def chamber_a_double(t, x, f_run, breakaway_extra=0.0, unseat_extra=0.0,
                     seat_extra=0.0):
    """双作用 A 腔（开阀侧）压力：开程供气，关程提前排净。"""
    noise = random.uniform(-2.0, 2.0)
    if t < T0_OPEN_CMD - 0.8:
        return 0.0
    if t < T0_OPEN_CMD:
        return 0.0
    if t < T_O_MOVE:
        frac = (t - T0_OPEN_CMD) / (T_O_MOVE - T0_OPEN_CMD)
        return frac * (f_run * 1.4 + breakaway_extra) / (A_M2 * 1000.0) + noise * 0.3
    if t < T_O_SEAT:
        decay = breakaway_extra * math.exp(-(t - T_O_MOVE) / BREAKAWAY_TAU)
        return (f_run + unseat_extra + decay) / (A_M2 * 1000.0) + noise
    if t < T_OPEN_END:
        return f_run / (A_M2 * 1000.0) + noise
    # 换向：A 腔斜坡排净
    if t < T_C_MOVE:
        return max(0.0, f_run / (A_M2 * 1000.0)
                   * max(0.0, (T_C_MOVE - t) / (T_C_MOVE - T_OPEN_END)))
    return 0.0


def chamber_b_double(t, x, f_run, seat_extra=0.0):
    """双作用 B 腔（关阀侧）压力：开程提前排净，关程供气，落座建立密封力。"""
    noise = random.uniform(-2.0, 2.0)
    if t < T0_OPEN_CMD - 0.8:
        return P_SUPPLY
    if t < T0_OPEN_CMD:
        return P_SUPPLY * (T0_OPEN_CMD - t) / 0.8
    if t < T_CLOSE_CMD - 0.8:
        return 0.0
    if t < T_C_MOVE:
        # 启程：B 腔斜坡建压到克服静摩擦
        return min(P_SUPPLY,
                   (f_run * 1.4) / (A_M2 * 1000.0)
                   * (t - (T_CLOSE_CMD - 0.8)) / (T_C_MOVE - (T_CLOSE_CMD - 0.8)))
    if t < T_C_SEAT:
        return f_run / (A_M2 * 1000.0) + noise
    if t < T_CLOSE_END:
        frac = (t - T_C_SEAT) / (T_CLOSE_END - T_C_SEAT)
        return (900.0 + seat_extra) / (A_M2 * 1000.0) * frac + noise * 0.3
    return (900.0 + seat_extra) / (A_M2 * 1000.0) + noise


def series(values_by_t, unit, clamp_nonneg=False):
    pts = [[round(t, 6), round(max(0.0, v) if clamp_nonneg else v, 4)]
           for t, v in values_by_t]
    return {"unit": unit, "points": pts}


def spring_curve(short=False):
    if short:
        return {"version": "SPG-701-A", "action": "fail_close", "force_unit": "n",
                "points": [[20.0, spring_force(20.0)], [50.0, spring_force(50.0)],
                           [80.0, spring_force(80.0)]]}
    return {"version": "SPG-701-A", "action": "fail_close", "force_unit": "n",
            "points": [[0.0, FS_0], [25.0, spring_force(25.0)],
                       [50.0, spring_force(50.0)], [75.0, spring_force(75.0)],
                       [100.0, FS_100]]}


def make_payload(*, name, tag="TV-701", phase="standalone", actuator_type="single_acting",
                 f_run=250.0, breakaway_extra=0.0, unseat_extra=0.0, seat_extra=0.0,
                 area_value=AREA_CM2, area_unit="cm2", area_b=None, spring=None,
                 include_a=True, include_b=False, a_time_shift=None,
                 pos_unit="%",
                 calibration="2027-06-30T00:00:00+00:00", started="2026-08-20T09:00:00+00:00",
                 load="offline", thresholds=None):
    t_cmd = times(0.05)
    t_pos = times(0.1)
    t_sup = times(0.2)
    if a_time_shift is not None:
        # A 腔时间轴整体平移到记录之后 → 与其他通道无重叠
        t_a = times(0.1, t_start=T_END + a_time_shift, t_end=T_END + a_time_shift + 30.0)
    else:
        t_a = times(0.1)
    t_b = times(0.5)

    cmd = series(((t, command_at(t)) for t in t_cmd), "%")
    if pos_unit == "mm":
        # 量纲冲突：阀位以 mm 给出但量程声明为 %
        pos = series(((t, position_at(t) * 0.15) for t in t_pos), "mm")
    else:
        pos = series(((t, position_at(t) + random.uniform(-0.05, 0.05))
                      for t in t_pos), "%")
    sup = series(((t, supply_at(t)) for t in t_sup), "kpa", clamp_nonneg=True)
    s = {"command": cmd, "position": pos, "supply_pressure": sup}
    double = actuator_type == "double_acting"
    if include_a:
        if double:
            s["chamber_a_pressure"] = series(
                ((t, chamber_a_double(t, position_at(t), f_run,
                                      breakaway_extra, unseat_extra, seat_extra))
                 for t in t_a), "kpa", clamp_nonneg=True)
        else:
            s["chamber_a_pressure"] = series(
                ((t, chamber_a_pressure(t, position_at(t), f_run, breakaway_extra,
                                        unseat_extra, seat_extra)) for t in t_a),
                "kpa", clamp_nonneg=True)
    if include_b:
        s["chamber_b_pressure"] = series(
            ((t, chamber_b_double(t, position_at(t), f_run, seat_extra))
             for t in t_b), "kpa", clamp_nonneg=True)

    actuator = {
        "actuator_type": actuator_type,
        "area_a": {"value": area_value, "unit": area_unit},
        "stem_direction": "down_to_close",
    }
    if area_b is not None:
        actuator["area_b"] = area_b
    if spring is not None:
        actuator["spring"] = spring
    elif not double:
        actuator["spring"] = spring_curve()

    payload = {
        "valve_tag": tag,
        "valve_description": "汽轮机调节阀" if tag == "TV-701" else "",
        "phase": phase,
        "test_started_at": started,
        "range": {"min": 0.0, "max": 100.0, "unit": "%"},
        "actuator": actuator,
        "series": s,
        "conditions": {"load": load, "medium": "steam", "ambient_temp_c": 28.0,
                       "note": name},
        "thresholds": thresholds or {},
        "calibration_valid_until": calibration,
    }
    return payload


SAMPLES = {
    "sample_thrust_good": dict(name="good", phase="baseline", f_run=220.0),
    "sample_thrust_good2": dict(name="good2", phase="periodic",
                                actuator_type="double_acting", spring=None,
                                include_b=True, f_run=200.0),
    "sample_thrust_packing_high": dict(name="packing-high", phase="pre",
                                       f_run=900.0),
    "sample_thrust_post_overhaul": dict(name="post-overhaul", phase="post",
                                        f_run=230.0),
    "sample_thrust_sticky_breakaway": dict(name="sticky-breakaway",
                                           breakaway_extra=3000.0),
    "sample_thrust_seat_overload": dict(name="seat-overload",
                                        actuator_type="double_acting", spring=None,
                                        include_b=True, seat_extra=3600.0,
                                        f_run=200.0),
    "sample_thrust_missing_chamber": dict(name="missing-chamber", include_a=False),
    "sample_thrust_no_overlap": dict(name="no-overlap", a_time_shift=30.0),
    "sample_thrust_unit_conflict": dict(name="unit-conflict", pos_unit="mm"),
    "sample_thrust_cal_expired": dict(name="cal-expired",
                                      calibration="2025-01-01T00:00:00+00:00"),
    "sample_thrust_spring_short": dict(name="spring-short",
                                       spring=spring_curve(short=True)),
}


def main():
    OUT.mkdir(exist_ok=True)
    for fname, kw in SAMPLES.items():
        payload = make_payload(**kw)
        (OUT / f"{fname}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        n = sum(len(v["points"]) for v in payload["series"].values())
        print(f"wrote {fname}.json ({n} points)")


if __name__ == "__main__":
    main()
