#!/usr/bin/env python3
"""生成三组请求样例（卡涩 / 信号断档 / 正常行程）及一组检修后对照。

各通道采样频率故意不同（指令/阀位/压力分别为 20/10/2 Hz 等），
且三组样例之间也不一致，用于验证时间轴对齐。
"""

import json
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "samples"


def ramp_profile(t):
    """斜坡型全行程：10% →(3%/s)→ 90% → 停留 → 3%/s → 10%。"""
    if t < 5:
        return 10.0
    if t < 31.67:
        return 10.0 + (t - 5) * 3.0
    if t < 40.67:
        return 90.0
    if t < 67.33:
        return 90.0 - (t - 40.67) * 3.0
    return 10.0


def step_profile(t):
    """阶跃型全行程：10% →阶跃→ 90% → 停留 → 阶跃→ 10%。"""
    if t < 5:
        return 10.0
    if t < 5.3:
        return 10.0 + (t - 5) / 0.3 * 80.0
    if t < 30:
        return 90.0
    if t < 30.3:
        return 90.0 - (t - 30) / 0.3 * 80.0
    return 10.0


def stiction_position(cmd_seq, ts, stick=3.5, seed=7):
    """卡涩阀：|指令-阀位| 超过粘滞带才动作，动作时跳动；指令稳定后缓慢到位。"""
    rng = random.Random(seed)
    pos = cmd_seq[0]
    out = []
    last_cmd_change_t = ts[0]
    prev_cmd = cmd_seq[0]
    for t, c in zip(ts, cmd_seq):
        if abs(c - prev_cmd) > 1e-9:
            last_cmd_change_t = t
        prev_cmd = c
        if t - last_cmd_change_t > 1.5:
            # 指令已稳定：定位器缓慢消除残余偏差
            pos += (c - pos) * 0.25
        elif abs(c - pos) > stick:
            # 挣脱粘滞：跳到指令附近并带少量过冲
            pos = c + (0.6 if c > pos else -0.6) + rng.uniform(-0.15, 0.15)
        out.append(pos + rng.uniform(-0.05, 0.05))
    return out


def first_order_position(cmd_seq, ts, tau=1.2, seed=3, noise=0.05):
    rng = random.Random(seed)
    pos = cmd_seq[0]
    out = []
    prev_t = ts[0]
    for t, c in zip(ts, cmd_seq):
        dt = max(t - prev_t, 1e-6)
        prev_t = t
        pos += (c - pos) * min(dt / tau, 1.0)
        out.append(pos + rng.uniform(-noise, noise))
    return out


def pressure_series(ts, moving_fn, base=400.0, dip=55.0, seed=11, noise=1.5):
    rng = random.Random(seed)
    return [base - (dip if moving_fn(t) else 0.0) + rng.uniform(-noise, noise) for t in ts]


def make_times(dt, t_end, offset=0.0):
    n = int(t_end / dt) + 1
    return [round(offset + i * dt, 6) for i in range(n)]


def build(name, phase, profile, pos_fn, dt_cmd, dt_pos, dt_prs, t_end,
          pos_offset=0.03, prs_offset=0.11, mutate=None):
    ts_cmd = make_times(dt_cmd, t_end)
    ts_pos = make_times(dt_pos, t_end, pos_offset)
    ts_prs = make_times(dt_prs, t_end, prs_offset)
    cmd = [profile(t) for t in ts_cmd]
    cmd_at_pos = [profile(t) for t in ts_pos]
    pos = pos_fn(cmd_at_pos, ts_pos)
    moving = lambda t: abs(profile(t + 0.25) - profile(t)) > 1e-6
    prs = pressure_series(ts_prs, moving)

    pos_points = [[t, round(v, 4)] for t, v in zip(ts_pos, pos)]
    if mutate:
        pos_points = mutate(pos_points)

    return {
        "valve_tag": "FV-101",
        "valve_description": " boiler feedwater control valve (sample)",
        "phase": phase,
        "test_started_at": "2026-09-10T08:00:00+00:00",
        "range": {"min": 0.0, "max": 100.0, "unit": "%"},
        "series": {
            "command": {"unit": "%", "points": [[t, round(v, 4)] for t, v in zip(ts_cmd, cmd)]},
            "position": {"unit": "%", "points": pos_points},
            "pressure": {"unit": "kPa", "points": [[t, round(v, 2)] for t, v in zip(ts_prs, prs)]},
        },
        "conditions": {"load": "offline", "medium": "air", "supply_pressure_kpa": 400.0,
                       "ambient_temp_c": 25.0, "note": name},
        "thresholds": {"travel_time_s_max": 10.0, "deadband_pct_max": 2.0,
                       "hysteresis_pct_max": 3.0, "overshoot_pct_max": 5.0,
                       "steady_state_pct_max": 2.0, "supply_pressure_min_kpa": 300.0,
                       "settle_band_pct": 2.0},
        "calibration_valid_until": "2027-06-30T00:00:00+00:00",
        "uncertainty": uncertainty_input(),
    }


def uncertainty_input():
    """典型仪表不确定度声明（固定种子，蒙特卡洛可复现）。"""
    return {
        "channels": {
            "command": {
                "resolution": {"kind": "resolution", "value": 0.1, "unit": "%"},
                "accuracy": {"kind": "accuracy", "value": 0.2, "unit": "%"},
                "time_jitter_s": 0.01,
            },
            "position": {
                "resolution": {"kind": "resolution", "value": 0.15, "unit": "%"},
                "accuracy": {"kind": "accuracy", "value": 0.25, "unit": "%"},
                "zero_drift": {"kind": "zero_drift", "value": 0.1, "unit": "%"},
                "time_jitter_s": 0.02,
            },
            "pressure": {
                "resolution": {"kind": "resolution", "value": 1.0, "unit": "kPa"},
                "accuracy": {"kind": "accuracy", "value": 2.0, "unit": "kPa"},
                "time_jitter_s": 0.05,
            },
        },
        "calibration": {
            "value": 0.2, "unit": "%", "applies_to": ["command", "position"],
            "range_min": 0, "range_max": 100, "range_unit": "%",
        },
        "n_samples": 120, "seed": 20260912, "interval_prob": 0.95,
        "note": "定位器/压力变送器检定证书分量",
    }


def dropout_mutate(points):
    """制造断档：删除 20–24s 的阀位点（缺口），40–42s 强制值不变（冻结）。"""
    kept = [p for p in points if not (20.0 <= p[0] <= 24.0)]
    out = []
    for t, v in kept:
        if 40.0 <= t <= 42.0:
            out.append([t, 10.02])  # 冻结为恒定值
        else:
            out.append([t, v])
    return out


def main():
    OUT.mkdir(exist_ok=True)
    samples = {
        # 卡涩：斜坡指令，阀位粘滑，死区/回差大（检修前）
        "sample_stiction": build(
            "sample_stiction", "pre", ramp_profile,
            lambda c, ts: stiction_position(c, ts, stick=3.5),
            dt_cmd=0.05, dt_pos=0.1, dt_prs=0.5, t_end=75),
        # 检修后对照：同一阀门、同一工况，粘滞消除
        "sample_stiction_post": build(
            "sample_stiction_post", "post", ramp_profile,
            lambda c, ts: first_order_position(c, ts, tau=0.3, seed=5),
            dt_cmd=0.05, dt_pos=0.1, dt_prs=0.5, t_end=75),
        # 信号断档：响应正常，但阀位反馈有缺口与冻结
        "sample_dropout": build(
            "sample_dropout", "standalone", step_profile,
            lambda c, ts: first_order_position(c, ts, tau=1.2),
            dt_cmd=0.1, dt_pos=0.2, dt_prs=1.0, t_end=55,
            mutate=dropout_mutate),
        # 正常行程：阶跃响应干净
        "sample_normal": build(
            "sample_normal", "standalone", step_profile,
            lambda c, ts: first_order_position(c, ts, tau=1.2),
            dt_cmd=0.1, dt_pos=0.2, dt_prs=1.0, t_end=55),
    }
    for name, payload in samples.items():
        p = OUT / f"{name}.json"
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"wrote {p} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
