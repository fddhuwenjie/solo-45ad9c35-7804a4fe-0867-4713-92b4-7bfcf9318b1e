#!/usr/bin/env python3
"""生成气体阀座密封保持试验请求样例。

场景：阀杆压到关位后隔离下游形成封闭容积（10 L），上游保持 500 kPa，
下游背压 150 kPa。温度从 24.2°C 向 25.0°C 平衡（τ=8s 热瞬态），之后进入
保持段计量泄漏。泄漏换算：1 Nl/min ≈ 10.46 kPa/min（10 L、297 K、
参考状态 15°C/101.325 kPa）。空白回升基线 0.08–0.16 kPa/min（随温度）。

通道异频：指令 5Hz、阀位 2Hz、上游/下游压力 1Hz、下游温度 0.2Hz、
流量计 0.5Hz（可选）。

样例清单（同阀 XV-501 的前三个用于多次试验比较）：
- sample_seatleak_tight            密封合格（泄漏率 0.02 Nl/min，限值 0.05）
- sample_seatleak_tight2           合格复测（0.025，显式 hold_start/hold_end 动作）
- sample_seatleak_leaking          阀座内漏（0.30 Nl/min，保持段持续超限）
- sample_seatleak_thermal_recovery 保持段温度回升 1.6°C：表观压力回升超限，
                                   温压补偿后合格（不误判为内漏）
- sample_seatleak_flow_checked     带流量计交叉核对（两种估算一致）
- sample_seatleak_temp_dropout     保持段温度断档 → 证据缺口
- sample_seatleak_bad_sequence     下游隔离早于关阀到位（次序矛盾）→ 证据缺口
- sample_seatleak_low_dp           有效压差不足（50 kPa < 100）→ 证据缺口
- sample_seatleak_unstable_position 保持段关位未稳定（周期性抬起到 2.8%）→ 证据缺口
- sample_seatleak_blank_uncovered  试验温度 40°C 超出空白基线包络 → 证据缺口
"""

import json
import math
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "samples"

T_END = 190.0
T_CMD = 10.0          # 关阀指令
T_CLOSED = 12.5       # 阀位到关位
T_ISO = 15.0          # 下游隔离
P_UP = 500.0          # 上游压力 kPa
P_DOWN = 150.0        # 下游背压 kPa
V_M3 = 0.01           # 封闭容积
T_REF_K = 288.15      # 标准状态 15°C
P_REF = 101.325
T_ISO_C = 24.2        # 隔离时温度
BLANK_RATE = 0.12     # 空白回升 kPa/min（25°C 插值结果）


def kpa_per_nl_min(t_iso_c=T_ISO_C):
    """1 Nl/min 泄漏在封闭容积内产生的压力速率（kPa/min）。"""
    return P_REF * (t_iso_c + 273.15) / (V_M3 * 1000.0 * T_REF_K)


def make_times(dt, t_end=T_END, offset=0.0):
    n = int((t_end - offset) / dt) + 1
    return [round(offset + i * dt, 6) for i in range(n)]


def command_at(t):
    if t < T_CMD:
        return 60.0
    if t < T_CMD + 2.0:
        return 60.0 - 30.0 * (t - T_CMD)
    return 0.0


def position_at(t, unstable=False):
    if t < T_CMD + 0.5:
        v = 60.0
    elif t < T_CLOSED:
        v = 60.0 - 30.0 * (t - T_CMD - 0.5)
    else:
        v = 0.0
    if unstable and t >= 40.0 and (t % 40.0) < 20.0:
        v = 2.8                      # 保持段周期性抬起，关位未稳定
    return v


def make_temp_fn(t0_c=T_ISO_C, t1_c=25.0, tau=8.0, ramp=None):
    """ramp: (t_start, rate_c_per_s, cap_c) 保持段缓慢升温（环境传热）。"""
    def temp_at(t):
        if t < T_ISO:
            return t0_c
        v = t1_c - (t1_c - t0_c) * math.exp(-(t - T_ISO) / tau)
        if ramp:
            t_start, rate, cap = ramp
            v += min(max(0.0, (t - t_start) * rate), cap)
        return v
    return temp_at


def downstream_at(t, leak_nl_min, temp_fn, t_iso_c=T_ISO_C, blank_rate=BLANK_RATE):
    """封闭容积压力：泄漏与空白回升按隔离温度折算，热平衡按理想气体跟随温度。"""
    if t < T_ISO:
        return P_DOWN
    leak_kpa = leak_nl_min * kpa_per_nl_min(t_iso_c) * (t - T_ISO) / 60.0
    blank_kpa = blank_rate * (t - T_ISO) / 60.0
    t_k = temp_fn(t) + 273.15
    return (P_DOWN + leak_kpa) * t_k / (t_iso_c + 273.15) + blank_kpa


def series(ts, fn, digits, seed, noise):
    rng = random.Random(seed)
    return [[round(t, 4), round(fn(t) + rng.uniform(-noise, noise), digits)] for t in ts]


def base_payload(name, tag, started, phase="periodic", explicit_hold=False,
                 temp_slope_max=0.5):
    isolation = [
        {"t": T_CMD, "action": "close_command", "note": "DCS 关阀指令"},
        {"t": T_CLOSED, "action": "valve_closed", "note": "阀位进入关位带"},
        {"t": T_ISO, "action": "downstream_isolated", "note": "下游切断阀关闭，封闭容积形成"},
    ]
    if explicit_hold:
        isolation += [
            {"t": 45.0, "action": "hold_start", "note": "现场确认温度已稳定"},
            {"t": 185.0, "action": "hold_end", "note": "保持结束"},
        ]
    return {
        "valve_tag": tag,
        "valve_description": f"{name} (sample)",
        "phase": phase,
        "test_started_at": started,
        "range": {"min": 0.0, "max": 100.0, "unit": "%"},
        "flow_direction": "upstream_to_downstream",
        "gas": {"name": "air", "molar_mass_g_mol": 28.97, "compressibility_z": 1.0,
                "reference_temp_c": 15.0, "reference_pressure_kpa": 101.325},
        "closed_volume": {"downstream_volume_m3": V_M3, "uncertainty_pct": 8.0},
        "isolation": isolation,
        "blank_baseline": {
            "points": [
                {"temp_c": 15.0, "pressure_kpa": 120.0, "recovery_rate_kpa_min": 0.08},
                {"temp_c": 35.0, "pressure_kpa": 400.0, "recovery_rate_kpa_min": 0.16},
            ],
            "temp_margin_c": 2.0,
            "pressure_margin_kpa": 20.0,
        },
        "conditions": {"load": "offline", "seat_config": "soft_seat",
                       "ambient_temp_c": 25.0, "note": name},
        "thresholds": {
            "leak_rate_max_nl_min": 0.05, "cumulative_leak_max_nl": 0.2,
            "min_differential_kpa": 100.0, "closed_band_pct": 2.0,
            "position_drift_pct_max": 0.5, "stabilization_min_s": 15.0,
            "hold_min_s": 60.0, "temp_coverage_min": 0.9,
            "temp_slope_max_c_min": temp_slope_max, "slope_window_s": 15.0,
            "rate_exceed_dwell_s": 10.0, "flow_deviation_pct_max": 25.0,
        },
        "calibration_valid_until": "2027-06-30T00:00:00+00:00",
    }


def build_series(payload, *, leak_nl_min, temp_fn, unstable_pos=False,
                 p_up=P_UP, flow_nl_min=None, temp_gap=None, seed=3):
    ts_cmd = make_times(0.2, offset=0.0)
    ts_pos = make_times(0.5, offset=0.11)
    ts_up = make_times(1.0, offset=0.23)
    ts_dn = make_times(1.0, offset=0.31)
    ts_tp = make_times(5.0, offset=0.7)
    if temp_gap:
        ts_tp = [t for t in ts_tp if not (temp_gap[0] <= t <= temp_gap[1])]
    payload["series"] = {
        "command": {"unit": "%", "points": series(ts_cmd, command_at, 3, seed, 0.02)},
        "position": {"unit": "%", "points": series(
            ts_pos, lambda t: position_at(t, unstable_pos), 3, seed + 1, 0.05)},
        "upstream_pressure": {"unit": "kPa", "points": series(
            ts_up, lambda t: p_up, 2, seed + 2, 0.4)},
        "downstream_pressure": {"unit": "kPa", "points": series(
            ts_dn, lambda t: downstream_at(t, leak_nl_min, temp_fn), 3, seed + 3, 0.05)},
        "downstream_temp": {"unit": "c", "points": series(ts_tp, temp_fn, 3, seed + 4, 0.02)},
    }
    if flow_nl_min is not None:
        ts_fl = make_times(2.0, offset=1.3)
        payload["series"]["flow"] = {"unit": "nl/min", "points": series(
            ts_fl, lambda t: flow_nl_min, 4, seed + 5, 0.001)}
    return payload


def s_tight():
    p = base_payload("sample_seatleak_tight", "XV-501",
                     "2026-08-01T08:00:00+00:00", phase="baseline")
    return build_series(p, leak_nl_min=0.02, temp_fn=make_temp_fn(), seed=3)


def s_tight2():
    p = base_payload("sample_seatleak_tight2", "XV-501",
                     "2026-08-20T08:00:00+00:00", explicit_hold=True)
    return build_series(p, leak_nl_min=0.025, temp_fn=make_temp_fn(), seed=11)


def s_leaking():
    p = base_payload("sample_seatleak_leaking", "XV-501",
                     "2026-09-05T08:00:00+00:00")
    return build_series(p, leak_nl_min=0.30, temp_fn=make_temp_fn(), seed=21)


def s_thermal_recovery():
    # 保持段环境传热使封闭容积升温 1.6°C：表观压力回升折合 ~0.055 Nl/min（超限），
    # 温压补偿后真实泄漏仅 0.01 Nl/min
    p = base_payload("sample_seatleak_thermal_recovery", "XV-502",
                     "2026-08-03T08:00:00+00:00", temp_slope_max=0.8)
    ramp = (50.0, 1.6 / 130.0, 1.6)
    return build_series(p, leak_nl_min=0.01, temp_fn=make_temp_fn(ramp=ramp), seed=31)


def s_flow_checked():
    p = base_payload("sample_seatleak_flow_checked", "XV-503",
                     "2026-08-05T08:00:00+00:00")
    return build_series(p, leak_nl_min=0.03, temp_fn=make_temp_fn(),
                        flow_nl_min=0.0305, seed=41)


def s_temp_dropout():
    p = base_payload("sample_seatleak_temp_dropout", "XV-504",
                     "2026-08-06T08:00:00+00:00")
    return build_series(p, leak_nl_min=0.02, temp_fn=make_temp_fn(),
                        temp_gap=(90.0, 130.0), seed=51)


def s_bad_sequence():
    p = base_payload("sample_seatleak_bad_sequence", "XV-505",
                     "2026-08-07T08:00:00+00:00")
    # 下游隔离记录早于关阀指令与关到位：次序矛盾
    p["isolation"] = [
        {"t": 8.0, "action": "downstream_isolated", "note": "现场记录"},
        {"t": T_CMD, "action": "close_command"},
        {"t": T_CLOSED, "action": "valve_closed"},
    ]
    return build_series(p, leak_nl_min=0.02, temp_fn=make_temp_fn(), seed=61)


def s_low_dp():
    p = base_payload("sample_seatleak_low_dp", "XV-506",
                     "2026-08-08T08:00:00+00:00")
    return build_series(p, leak_nl_min=0.02, temp_fn=make_temp_fn(),
                        p_up=200.0, seed=71)   # ΔP ≈ 50 kPa < 100


def s_unstable_position():
    p = base_payload("sample_seatleak_unstable_position", "XV-507",
                     "2026-08-09T08:00:00+00:00")
    return build_series(p, leak_nl_min=0.02, temp_fn=make_temp_fn(),
                        unstable_pos=True, seed=81)


def s_blank_uncovered():
    # 环境 40°C，试验温度区间超出空白基线包络（≤37°C）
    p = base_payload("sample_seatleak_blank_uncovered", "XV-508",
                     "2026-08-10T08:00:00+00:00")
    p["conditions"]["ambient_temp_c"] = 40.0
    return build_series(p, leak_nl_min=0.02,
                        temp_fn=make_temp_fn(t0_c=39.2, t1_c=40.0), seed=91)


def main():
    OUT.mkdir(exist_ok=True)
    samples = {
        "sample_seatleak_tight": s_tight(),
        "sample_seatleak_tight2": s_tight2(),
        "sample_seatleak_leaking": s_leaking(),
        "sample_seatleak_thermal_recovery": s_thermal_recovery(),
        "sample_seatleak_flow_checked": s_flow_checked(),
        "sample_seatleak_temp_dropout": s_temp_dropout(),
        "sample_seatleak_bad_sequence": s_bad_sequence(),
        "sample_seatleak_low_dp": s_low_dp(),
        "sample_seatleak_unstable_position": s_unstable_position(),
        "sample_seatleak_blank_uncovered": s_blank_uncovered(),
    }
    for name, payload in samples.items():
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
