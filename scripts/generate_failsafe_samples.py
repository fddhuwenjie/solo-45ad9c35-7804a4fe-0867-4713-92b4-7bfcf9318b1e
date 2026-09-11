#!/usr/bin/env python3
"""生成故障安全动作（失气/失电跳闸）测试请求样例。

通道异频：跳闸接点 100Hz（DI）、控制指令 20Hz、阀位 10Hz、执行器压力 2Hz。
跳闸接点常带抖动/重复触发；控制指令的消失早于接点动作。

样例清单：
- fs_close_good / fs_close_good2   失气关闭，按时到位（同阀同工况，趋势基准与良好复测）
- fs_open_good                     失气打开，按时到位
- fs_fip_good                      原位保持
- fs_close_degraded                同阀退化：延迟、T90 变慢、反弹、停滞、残压偏高
- fs_close_unsettled               观察窗结束仍在慢爬，未稳定
- fs_close_wrong_direction         实际反向动作
- fs_close_short_pretrip           跳闸前数据不足
- fs_close_no_trip                 未观察到跳闸
"""

import json
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "samples"

T_TRIP = 5.0
CMD_LEAD = 0.5


def make_times(dt, t_end, offset=0.0):
    n = int((t_end - offset) / dt) + 1
    return [round(offset + i * dt, 6) for i in range(n)]


def trip_series(ts, chatter=False, retrigger=False, no_trip=False, t_trip=T_TRIP):
    """100Hz DI；可选跳闸沿抖动与 12s 处重复触发。

    抖动簇脉冲宽约 20ms、间隔 30-40ms（≤ chatter_merge_s=0.25）；
    重复触发在 12s 后（远超合并窗），为独立簇。
    """
    if no_trip:
        return [[round(t, 4), 0] for t in ts]
    pulses = [0.0]
    if chatter:
        pulses += [0.06, 0.13]
    if retrigger:
        pulses += [12.0, 12.07, 12.15]
    width = 0.025
    out = []
    for t in ts:
        dt = t - t_trip
        b = 1 if any(p <= dt < p + width for p in pulses) else 0
        out.append([round(t, 4), b])
    return out


def command_series(ts, hold, t_trip=T_TRIP, lead=CMD_LEAD):
    """跳闸前 hold%；失气时指令先行（lead 秒）掉到 0（fail-close 场景）。"""
    out = []
    for t in ts:
        if t < t_trip - lead:
            v = hold
        elif t < t_trip - lead + 0.1:
            v = hold * (1 - (t - (t_trip - lead)) / 0.1)
        else:
            v = 0.0
        out.append([round(t, 4), round(v, 3)])
    return out


def spring_position(ts, hold, direction, t_move, move_speed, end_crawl_speed=0.0,
                    rebound=None, stall=None, noise=0.06, seed=3, target_override=None):
    """弹簧复位阀位：t_move 前保持 hold；之后匀速移动，可选末端缓行、反弹、中途停滞。

    direction: -1 关向, +1 开向。rebound: {"at_progress":0.9, "mag":7, "dur":0.8}
    stall: {"at_t": 相对开始移动秒, "dur":1.2}
    """
    rng = random.Random(seed)
    target = target_override if target_override is not None else (0.0 if direction < 0 else 100.0)
    total = abs(target - hold)
    band = rebound["band"] if rebound else 2.0
    # 快速段走到距安全位 settle_band 处，之后末端缓行（在带边界处连续）
    fast_dist = total - band if end_crawl_speed else None
    t_fast = fast_dist / move_speed if (end_crawl_speed and fast_dist > 0) else None
    band_entry_t = None
    out = []
    for t in ts:
        tau = t - t_move
        if tau < 0:
            v = hold
        else:
            eff_tau = tau
            if stall and stall["at_t"] <= tau < stall["at_t"] + stall["dur"]:
                eff_tau = stall["at_t"]          # 停滞期：保持在停滞起点
            elif stall and tau >= stall["at_t"] + stall["dur"]:
                eff_tau = tau - stall["dur"]     # 冻结结束后从冻结点继续移动
            # 位置分段：快速到 target+dir·band，之后末端缓行至 target
            fast_pos = hold + direction * move_speed * eff_tau
            if end_crawl_speed:
                dist = direction * (fast_pos - target)
                if dist > band:
                    v = fast_pos
                else:
                    # 进入慢段：以进入时刻为零点连续积分
                    entry_tau = t_fast
                    v = target + direction * max(
                        0.0, band - end_crawl_speed * (eff_tau - entry_tau))
            else:
                v = fast_pos
            # 到达安全位后贴住，不反向越过
            if direction < 0:
                v = min(v, target)
            else:
                v = max(v, target)
            if rebound:
                # 首次进入安全位带后储气罐余压顶开一个三角波再回落
                if band_entry_t is None and abs(v - target) <= rebound["band"]:
                    band_entry_t = t
                if band_entry_t is not None:
                    dt_in = t - band_entry_t
                    if 0 <= dt_in < rebound["dur"]:
                        shape = 1 - abs(dt_in / rebound["dur"] - 0.5) * 2
                        v += direction * rebound["mag"] * shape
            v = max(0.0, min(100.0, v))
        out.append([round(t, 4), round(v + rng.uniform(-noise, noise), 3)])
    return out


def pressure_series(ts, hold_p, t_decay, tau, residual=8.0, seed=11, noise=1.2):
    rng = random.Random(seed)
    out = []
    for t in ts:
        if t < t_decay:
            v = hold_p
        else:
            v = residual + (hold_p - residual) * 2.718281828 ** (-(t - t_decay) / tau)
        out.append([round(t, 4), round(v + rng.uniform(-noise, noise), 2)])
    return out


def base_payload(name, tag, mode, t_end, hold=60.0, window=20.0, phase="periodic",
                 started="2026-08-01T08:00:00+00:00", actuator="spring_return",
                 supply=400.0, temp=25.0):
    return {
        "valve_tag": tag,
        "valve_description": f"{name} (sample)",
        "phase": phase,
        "test_started_at": started,
        "range": {"min": 0.0, "max": 100.0, "unit": "%"},
        "fail_mode": mode,
        "conditions": {"load": "offline", "medium": "air", "supply_pressure_kpa": supply,
                       "ambient_temp_c": temp, "actuator_type": actuator, "note": name},
        "thresholds": {"response_delay_s_max": 2.0, "t90_s_max": 8.0, "settle_band_pct": 2.0,
                       "settle_dwell_s": 1.5, "rebound_pct_max": 5.0, "stall_min_s": 0.8,
                       "stall_move_pct": 0.5, "pressure_residual_kpa_max": 50.0,
                       "pressure_decay_pct_min": 90.0, "command_loss_pct": 5.0,
                       "fip_drift_pct_max": 2.0, "baseline_min_s": 1.0,
                       "baseline_coverage_min": 0.8, "chatter_merge_s": 0.25,
                       "pre_trip_margin_s": 0.2},
        "observation_window_s": window,
        "calibration_valid_until": "2027-06-30T00:00:00+00:00",
        "_t_end": t_end, "_hold": hold,
    }


def build_series(payload, *, trip_pts, cmd_pts, pos_pts, prs_pts):
    payload.pop("_t_end", None)
    payload.pop("_hold", None)
    payload["series"] = {
        "trip": {"unit": "di", "points": trip_pts},
        "command": {"unit": "%", "points": cmd_pts},
        "position": {"unit": "%", "points": pos_pts},
        "pressure": {"unit": "kPa", "points": prs_pts},
    }
    return payload


def fs_close_good(name="sample_failsafe_close_good", tag="FV-202",
                  started="2026-08-01T08:00:00+00:00", phase="baseline",
                  chatter=False, retrigger=False, t_end=27.0):
    p = base_payload(name, tag, "fail_close", t_end, phase=phase, started=started)
    ts_t = make_times(0.01, t_end)
    ts_c = make_times(0.05, t_end, offset=0.004)
    ts_p = make_times(0.1, t_end, offset=0.03)
    ts_r = make_times(0.5, t_end, offset=0.11)
    return build_series(
        p,
        trip_pts=trip_series(ts_t, chatter=chatter, retrigger=retrigger),
        cmd_pts=command_series(ts_c, hold=60.0),
        pos_pts=spring_position(ts_p, 60.0, -1, t_move=T_TRIP + 0.6,
                                move_speed=14.0, end_crawl_speed=3.0, seed=3),
        prs_pts=pressure_series(ts_r, 400.0, T_TRIP + 0.15, tau=1.1),
    )


def fs_open_good(name="sample_failsafe_open_good", tag="FV-203", t_end=27.0):
    p = base_payload(name, tag, "fail_open", t_end, hold=40.0)
    ts_t = make_times(0.01, t_end)
    ts_c = make_times(0.05, t_end, offset=0.004)
    ts_p = make_times(0.1, t_end, offset=0.03)
    ts_r = make_times(0.5, t_end, offset=0.11)
    # fail-open：指令消失时阀位向 100% 打开；指令跳到 100 表示失气开
    cmd = [[round(t, 4), round(0.0 if t < T_TRIP - CMD_LEAD else 100.0, 3)] for t in ts_c]
    return build_series(
        p,
        trip_pts=trip_series(ts_t, chatter=True),
        cmd_pts=cmd,
        pos_pts=spring_position(ts_p, 40.0, +1, t_move=T_TRIP + 0.8,
                                move_speed=12.0, end_crawl_speed=3.5, seed=9),
        prs_pts=pressure_series(ts_r, 380.0, T_TRIP + 0.2, tau=1.3, seed=13),
    )


def fs_fip_good(name="sample_failsafe_fip_good", tag="FV-204", t_end=27.0):
    p = base_payload(name, tag, "fail_in_place", t_end, hold=55.0, actuator="double_acting")
    ts_t = make_times(0.01, t_end)
    ts_c = make_times(0.05, t_end, offset=0.004)
    ts_p = make_times(0.1, t_end, offset=0.03)
    ts_r = make_times(0.5, t_end, offset=0.11)
    rng = random.Random(5)
    pos = [[round(t, 4), round(55.0 + rng.uniform(-0.25, 0.25), 3)] for t in ts_p]
    prs = [[round(t, 4), round(400.0 + rng.uniform(-2, 2), 2)] for t in ts_r]
    cmd = command_series(ts_c, hold=55.0)
    return build_series(p, trip_pts=trip_series(ts_t), cmd_pts=cmd,
                        pos_pts=pos, prs_pts=prs)


def fs_close_degraded(name="sample_failsafe_close_degraded", tag="FV-202",
                      started="2026-09-05T08:00:00+00:00", t_end=27.0):
    p = base_payload(name, tag, "fail_close", t_end, phase="periodic", started=started)
    ts_t = make_times(0.01, t_end)
    ts_c = make_times(0.05, t_end, offset=0.004)
    ts_p = make_times(0.1, t_end, offset=0.03)
    ts_r = make_times(0.5, t_end, offset=0.11)
    return build_series(
        p,
        trip_pts=trip_series(ts_t),
        cmd_pts=command_series(ts_c, hold=60.0),
        pos_pts=spring_position(
            ts_p, 60.0, -1, t_move=T_TRIP + 1.4, move_speed=9.0,
            end_crawl_speed=1.6, seed=21,
            stall={"at_t": 1.6, "dur": 1.4},
            # 进入安全位带（与 settle_band_pct=2.0 一致）后余压顶开 ~8% 再回落
            rebound={"band": 2.0, "mag": 8.0, "dur": 1.4}),
        # 储气罐容量不足：泄压慢、残压高
        prs_pts=pressure_series(ts_r, 400.0, T_TRIP + 0.2, tau=3.2, residual=120.0, seed=19),
    )


def fs_close_unsettled(name="sample_failsafe_close_unsettled", tag="FV-205", t_end=27.0):
    p = base_payload(name, tag, "fail_close", t_end, window=10.0)
    ts_t = make_times(0.01, t_end)
    ts_c = make_times(0.05, t_end, offset=0.004)
    ts_p = make_times(0.1, t_end, offset=0.03)
    ts_r = make_times(0.5, t_end, offset=0.11)
    return build_series(
        p,
        trip_pts=trip_series(ts_t),
        cmd_pts=command_series(ts_c, hold=60.0),
        # 全程慢速，窗末仍在 10% 附近，未进入安全位带
        pos_pts=spring_position(ts_p, 60.0, -1, t_move=T_TRIP + 0.5,
                                move_speed=4.2, seed=31),
        prs_pts=pressure_series(ts_r, 400.0, T_TRIP + 0.1, tau=2.0, residual=30.0, seed=29),
    )


def fs_close_wrong_direction(name="sample_failsafe_close_wrong_direction",
                             tag="FV-206", t_end=27.0):
    p = base_payload(name, tag, "fail_close", t_end)
    ts_t = make_times(0.01, t_end)
    ts_c = make_times(0.05, t_end, offset=0.004)
    ts_p = make_times(0.1, t_end, offset=0.03)
    ts_r = make_times(0.5, t_end, offset=0.11)
    return build_series(
        p,
        trip_pts=trip_series(ts_t),
        cmd_pts=command_series(ts_c, hold=40.0),
        # 实际向上打开（错向），冲到 ~88%
        pos_pts=spring_position(ts_p, 40.0, +1, t_move=T_TRIP + 0.6,
                                move_speed=9.0, seed=41),
        prs_pts=pressure_series(ts_r, 400.0, T_TRIP + 0.1, tau=1.2, seed=23),
    )


def fs_close_short_pretrip(name="sample_failsafe_close_short_pretrip",
                           tag="FV-207", t_end=26.0):
    p = base_payload(name, tag, "fail_close", t_end)
    # 记录起点 4.9s，跳闸前仅 0.1s（少于 pre_trip_margin/baseline 要求）
    t0 = T_TRIP - 0.1
    ts_t = make_times(0.01, t_end, offset=t0)
    ts_c = make_times(0.05, t_end, offset=t0 + 0.004)
    ts_p = make_times(0.1, t_end, offset=t0 + 0.03)
    ts_r = make_times(0.5, t_end, offset=t0 + 0.11)
    # 时标保持绝对秒（5.0 跳闸），生成器基于绝对时间
    return build_series(
        p,
        trip_pts=trip_series(ts_t),
        cmd_pts=command_series(ts_c, hold=60.0),
        pos_pts=spring_position(ts_p, 60.0, -1, t_move=T_TRIP + 0.6,
                                move_speed=14.0, seed=3),
        prs_pts=pressure_series(ts_r, 400.0, T_TRIP + 0.15, tau=1.1),
    )


def fs_close_no_trip(name="sample_failsafe_close_no_trip", tag="FV-208", t_end=27.0):
    p = base_payload(name, tag, "fail_close", t_end)
    ts_t = make_times(0.01, t_end)
    ts_c = make_times(0.05, t_end, offset=0.004)
    ts_p = make_times(0.1, t_end, offset=0.03)
    ts_r = make_times(0.5, t_end, offset=0.11)
    rng = random.Random(7)
    pos = [[round(t, 4), round(60.0 + rng.uniform(-0.1, 0.1), 3)] for t in ts_p]
    cmd = [[round(t, 4), 60.0] for t in ts_c]
    prs = [[round(t, 4), round(400.0 + rng.uniform(-1.5, 1.5), 2)] for t in ts_r]
    return build_series(p, trip_pts=trip_series(ts_t, no_trip=True),
                        cmd_pts=cmd, pos_pts=pos, prs_pts=prs)


def main():
    OUT.mkdir(exist_ok=True)
    samples = {
        "sample_failsafe_close_good": fs_close_good(chatter=True, retrigger=True),
        "sample_failsafe_close_good2": fs_close_good(
            name="sample_failsafe_close_good2",
            started="2026-08-20T08:00:00+00:00", phase="periodic"),
        "sample_failsafe_open_good": fs_open_good(),
        "sample_failsafe_fip_good": fs_fip_good(),
        "sample_failsafe_close_degraded": fs_close_degraded(),
        "sample_failsafe_close_unsettled": fs_close_unsettled(),
        "sample_failsafe_close_wrong_direction": fs_close_wrong_direction(),
        "sample_failsafe_close_short_pretrip": fs_close_short_pretrip(),
        "sample_failsafe_close_no_trip": fs_close_no_trip(),
    }
    for name, payload in samples.items():
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
