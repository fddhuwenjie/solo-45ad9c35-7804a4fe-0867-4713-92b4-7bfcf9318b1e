#!/usr/bin/env python3
"""生成限位开关（开/关到位接点）诊断请求样例。

通道异频：连续阀位 10Hz、开位接点 50Hz、关位接点 20Hz。
开位接点常开（NO，raw=1 到位有效），关位接点常闭（NC，raw=0 到位有效）。

样例清单：
- ls_good            两个完整开-关循环，接点行为符合声明（基线）
- ls_post_good       同阀检修后复测（动作点略移，规格/阈值一致，可比较）
- ls_chatter         开位接点动作沿抖动 + 行程中段孤立毛刺
- ls_both_active     关位接点粘连：开向行程未释放，两接点同时有效
- ls_no_trigger      阀位到开端停留，开位接点始终未动作
- ls_early_flip      开位接点提前翻转（90% 即动作，低于动作窗）
- ls_swapped         两路接点接线对调（次序反转；可改绑通道修订）
- ls_illegal         开位接点 t=7.0 出现 0.5 非法电平（不得生成释放沿）
- ls_reverse_entry   关位释放沿处于开向运动，仅有反向进窗记录（延迟不配对）
"""

import json
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "samples"

SWITCH = {
    "open": {"polarity": "NO", "actuate_window_pct": [96.0, 100.0],
             "release_window_pct": [85.0, 94.0]},
    "closed": {"polarity": "NC", "actuate_window_pct": [0.0, 4.0],
               "release_window_pct": [6.0, 15.0]},
    "debounce_s": 0.1,
    "mutual_exclusion": {"enabled": True, "max_overlap_s": 0.0},
}

THRESHOLDS = {"edge_delay_s_max": 1.0, "dispersion_pct_max": 1.0,
              "end_dwell_s": 1.0, "move_slope_pct_s": 0.5}

BASE_WAYPOINTS = [(0, 0), (2, 0), (12, 100), (17, 100), (27, 0), (32, 0),
                  (42, 100), (47, 100), (57, 0), (60, 0)]


def make_times(dt, t_end, offset=0.0):
    n = int((t_end - offset) / dt) + 1
    return [round(offset + i * dt, 6) for i in range(n)]


def pos_fn_from_waypoints(waypoints):
    def f(t):
        if t <= waypoints[0][0]:
            return float(waypoints[0][1])
        for k in range(len(waypoints) - 1):
            t0, p0 = waypoints[k]
            t1, p1 = waypoints[k + 1]
            if t0 <= t <= t1:
                if t1 == t0:
                    return float(p1)
                return p0 + (p1 - p0) * (t - t0) / (t1 - t0)
        return float(waypoints[-1][1])
    return f


def position_series(ts, pos_fn, seed=5, noise=0.02):
    rng = random.Random(seed)
    out = []
    for t in ts:
        v = pos_fn(t) + rng.uniform(-noise, noise)
        out.append([round(t, 4), round(min(100.0, max(0.0, v)), 3)])
    return out


def simulate_contact(ts, pos_fn, act, rel, end, polarity):
    """按位置仿真接点逻辑态（迟滞状态机），再按极性转原始电平 0/1。"""
    active = False
    out = []
    for t in ts:
        p = pos_fn(t)
        if end == "high":
            if p >= act:
                active = True
            elif p < rel:
                active = False
        else:
            if p <= act:
                active = True
            elif p > rel:
                active = False
        logical = 1 if active else 0
        raw = logical if polarity == "NO" else 1 - logical
        out.append([round(t, 4), raw])
    return out


def base_payload(name, tag, waypoints=None, phase="standalone",
                 started="2026-08-01T08:00:00+00:00"):
    wp = waypoints or BASE_WAYPOINTS
    return {
        "valve_tag": tag,
        "valve_description": f"{name} (sample)",
        "phase": phase,
        "test_started_at": started,
        "range": {"min": 0.0, "max": 100.0, "unit": "%"},
        "series": {},
        "switch": json.loads(json.dumps(SWITCH)),
        "conditions": {"load": "offline", "medium": "air", "note": name},
        "thresholds": dict(THRESHOLDS),
        "calibration_valid_until": "2027-06-30T00:00:00+00:00",
        "_waypoints": wp,
    }


def fill_series(payload, open_pts, closed_pts, seed=5):
    wp = payload.pop("_waypoints")
    pos_fn = pos_fn_from_waypoints(wp)
    t_end = wp[-1][0]
    ts_p = make_times(0.1, t_end, offset=0.0)
    payload["series"] = {
        "position": {"unit": "%", "points": position_series(ts_p, pos_fn, seed=seed)},
        "open": {"unit": "di", "points": open_pts},
        "closed": {"unit": "di", "points": closed_pts},
    }
    return payload


def good(name="sample_ls_good", tag="XV-LS101", open_act=97.0, open_rel=90.0,
         closed_act=3.0, closed_rel=8.0, phase="baseline",
         started="2026-08-01T08:00:00+00:00", seed=5):
    p = base_payload(name, tag, phase=phase, started=started)
    pos_fn = pos_fn_from_waypoints(p["_waypoints"])
    t_end = p["_waypoints"][-1][0]
    ts_o = make_times(0.02, t_end, offset=0.005)
    ts_c = make_times(0.05, t_end, offset=0.02)
    open_pts = simulate_contact(ts_o, pos_fn, open_act, open_rel, "high", "NO")
    closed_pts = simulate_contact(ts_c, pos_fn, closed_act, closed_rel, "low", "NC")
    return fill_series(p, open_pts, closed_pts, seed=seed)


def chatter(name="sample_ls_chatter", tag="XV-LS102"):
    """开位接点首次动作沿抖动（3 脉冲簇）+ 行程中段孤立毛刺。"""
    p = good(name=name, tag=tag, phase="standalone", seed=7)
    pos_fn = pos_fn_from_waypoints(BASE_WAYPOINTS)
    for pt in p["series"]["open"]["points"]:
        t = pt[0]
        # 首次动作沿（约 11.7s，位置 ≥97）前后抖动：1,0,1,0,1
        if 11.70 - 1e-6 <= t <= 11.80 + 1e-6:
            k = round((t - 11.70) / 0.02)
            pt[1] = 1 if k % 2 == 0 else 0
        # 行程中段孤立毛刺（t≈25.0，位置约 70%，持续 0.04s < 去抖）
        if 25.00 - 1e-6 <= t <= 25.04 + 1e-6:
            pt[1] = 1
    return p


def both_active(name="sample_ls_both_active", tag="XV-LS103"):
    """关位接点粘连：开向行程越过释放窗仍保持有效，直到 t=15 才释放。"""
    p = good(name=name, tag=tag, phase="standalone", seed=9)
    for pt in p["series"]["closed"]["points"]:
        if pt[0] < 15.0:
            pt[1] = 0          # NC 有效态被粘连保持
        elif pt[0] < 26.7:
            pt[1] = 1          # t=15 释放（此时阀位已在 100%）
    return p


def no_trigger(name="sample_ls_no_trigger", tag="XV-LS104"):
    """开位接点始终不动作：阀位两次到开端停留均未触发。"""
    p = good(name=name, tag=tag, phase="standalone", seed=11)
    for pt in p["series"]["open"]["points"]:
        pt[1] = 0
    return p


def early_flip(name="sample_ls_early_flip", tag="XV-LS105"):
    """开位接点提前翻转：90% 即动作（动作窗声明 [96,100]）。"""
    p = base_payload(name, tag)
    pos_fn = pos_fn_from_waypoints(p["_waypoints"])
    t_end = p["_waypoints"][-1][0]
    ts_o = make_times(0.02, t_end, offset=0.005)
    ts_c = make_times(0.05, t_end, offset=0.02)
    open_pts = simulate_contact(ts_o, pos_fn, 90.0, 88.0, "high", "NO")
    closed_pts = simulate_contact(ts_c, pos_fn, 3.0, 8.0, "low", "NC")
    return fill_series(p, open_pts, closed_pts, seed=13)


def swapped(name="sample_ls_swapped", tag="XV-LS106"):
    """两路接点物理接线对调：open 通道实际携带关位接点信号。"""
    p = good(name=name, tag=tag, phase="standalone", seed=15)
    s = p["series"]
    s["open"], s["closed"] = s["closed"], s["open"]
    return p


def illegal(name="sample_ls_illegal", tag="XV-LS107"):
    """开位接点 t=7.0 出现 0.5 非法电平：不得作为释放沿进入指标。"""
    wp = [(0, 100), (5.6, 100), (15.6, 0), (20.6, 0), (30.6, 100),
          (35.6, 100), (45.6, 0), (50, 0)]
    p = base_payload(name, tag, waypoints=wp)
    pos_fn = pos_fn_from_waypoints(wp)
    t_end = wp[-1][0]
    # 开位接点 50Hz、零偏移：t=7.0 恰为采样点
    ts_o = make_times(0.02, t_end, offset=0.0)
    ts_c = make_times(0.05, t_end, offset=0.02)
    open_pts = simulate_contact(ts_o, pos_fn, 97.0, 90.0, "high", "NO")
    for pt in open_pts:
        t = pt[0]
        if t < 7.0 - 1e-6:
            pt[1] = 1                      # 阀位 100% 出发，接点保持有效
        elif abs(t - 7.0) < 1e-6:
            pt[1] = 0.5                    # 非法中间电平
        elif t < 7.1:
            pt[1] = 0                      # 其后恢复有效 0
    closed_pts = simulate_contact(ts_c, pos_fn, 3.0, 8.0, "low", "NC")
    return fill_series(p, open_pts, closed_pts, seed=17)


def reverse_entry(name="sample_ls_reverse_entry", tag="XV-LS108"):
    """关位释放沿 t=15.35 处于开向小幅运动，仅有 t=12 反向进窗记录。"""
    wp = [(0, 100), (3.5, 100), (13.5, 0), (14.5, 0), (15.5, 5),
          (16.5, 5), (17.5, 0), (20, 0)]
    p = base_payload(name, tag, waypoints=wp)
    pos_fn = pos_fn_from_waypoints(wp)
    t_end = wp[-1][0]
    ts_o = make_times(0.02, t_end, offset=0.005)
    ts_c = make_times(0.05, t_end, offset=0.0)
    open_pts = simulate_contact(ts_o, pos_fn, 97.0, 90.0, "high", "NO")
    # 关位接点：t≈13.3 动作（p≤2），t=15.35 开向运动中提前释放，
    # t≈17.1 回落到端再次动作
    closed_pts = []
    for t in ts_c:
        if 13.3 - 1e-6 <= t < 15.35 - 1e-6:
            raw = 0
        elif t >= 17.1 - 1e-6:
            raw = 0
        else:
            raw = 1
        closed_pts.append([round(t, 4), raw])
    return fill_series(p, open_pts, closed_pts, seed=19)


def main():
    OUT.mkdir(exist_ok=True)
    samples = {
        "sample_ls_good": good(),
        "sample_ls_post_good": good(
            name="sample_ls_post_good", open_act=97.3, open_rel=89.8,
            closed_act=2.8, closed_rel=8.2, phase="post",
            started="2026-09-01T08:00:00+00:00", seed=6),
        "sample_ls_chatter": chatter(),
        "sample_ls_both_active": both_active(),
        "sample_ls_no_trigger": no_trigger(),
        "sample_ls_early_flip": early_flip(),
        "sample_ls_swapped": swapped(),
        "sample_ls_illegal": illegal(),
        "sample_ls_reverse_entry": reverse_entry(),
    }
    for name, payload in samples.items():
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
