"""故障安全动作测试 API 的端到端回归。

重点覆盖两个曾误判的边界反例：
1. 阀位到位后数值完全恒定（合法机械平台），采样完整时不得误报
   dropout_in_action_window / 信号冻结，应判 pass；
2. 阀位进安全带后发生约 7.52% 反弹、随后窗末连续稳定 ≥12.1s 时，
   稳定性应按窗末连续平台判 settled=True，因反弹超阈判 fail，
   而不是以 not_settled_at_window_end 给 no_conclusion。
"""

import math

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

T_TRIP = 5.0


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "fs.db"))
    with TestClient(app) as c:
        yield c


def _trip_pts(ts):
    return [[round(t, 4), 0 if t < T_TRIP else 1] for t in ts]


def _cmd_pts(ts, hold):
    # 失气时指令先于接点 0.5s 消失
    return [[round(t, 4), round(hold if t < T_TRIP - 0.5 else 0.0, 3)] for t in ts]


def _prs_pts(ts, hold=400.0, tau=1.0, resid=8.0):
    def p(t):
        if t < T_TRIP + 0.15:
            return hold
        return resid + (hold - resid) * math.exp(-(t - T_TRIP - 0.15) / tau)
    return [[round(t, 4), round(p(t), 2)] for t in ts]


def _build(tag, pos_fn, hold=60.0, window=20.0):
    t_end = 27.0
    ts_t = [round(i * 0.01, 3) for i in range(int(t_end / 0.01) + 1)]
    ts_c = [round(0.004 + i * 0.05, 3) for i in range(int(t_end / 0.05) + 1)]
    ts_p = [round(0.03 + i * 0.1, 3) for i in range(int(t_end / 0.1) + 1)]
    ts_r = [round(0.11 + i * 0.5, 3) for i in range(int(t_end / 0.5) + 1)]
    return {
        "valve_tag": tag, "valve_description": "counterexample", "phase": "standalone",
        "test_started_at": "2026-09-02T08:00:00+00:00",
        "range": {"min": 0.0, "max": 100.0, "unit": "%"},
        "fail_mode": "fail_close",
        "series": {
            "trip": {"unit": "di", "points": _trip_pts(ts_t)},
            "command": {"unit": "%", "points": _cmd_pts(ts_c, hold)},
            "position": {"unit": "%", "points": [[round(t, 4), round(pos_fn(t), 3)] for t in ts_p]},
            "pressure": {"unit": "kPa", "points": _prs_pts(ts_r)},
        },
        "conditions": {"load": "offline", "medium": "air", "supply_pressure_kpa": 400.0,
                       "ambient_temp_c": 25.0, "actuator_type": "spring_return"},
        "thresholds": {"response_delay_s_max": 2.0, "t90_s_max": 8.0, "settle_band_pct": 2.0,
                       "settle_dwell_s": 1.5, "rebound_pct_max": 5.0, "stall_min_s": 0.8,
                       "stall_move_pct": 0.5, "pressure_residual_kpa_max": 50.0,
                       "pressure_decay_pct_min": 90.0, "command_loss_pct": 5.0,
                       "fip_drift_pct_max": 2.0, "baseline_min_s": 1.0,
                       "baseline_coverage_min": 0.8, "chatter_merge_s": 0.25,
                       "pre_trip_margin_s": 0.2},
        "observation_window_s": window,
        "calibration_valid_until": "2027-06-30T00:00:00+00:00",
    }


def _analyze(client, body):
    r = client.post("/failsafe/tests", json=body)
    assert r.status_code == 201, r.text
    tid = r.json()["failsafe_test_id"]
    r = client.post(f"/failsafe/tests/{tid}/analyze", json={"author": "ce"})
    assert r.status_code == 201, r.text
    return r.json()["result"]


def test_settled_constant_platform_not_dropout(client):
    """到位后阀位严格恒定（机械平台）：不得误判信号冻结/未稳定。"""
    hold = 60.0

    def pos(t):
        if t < 5.6:
            return hold
        return max(hold - 14.0 * (t - 5.6), 0.0)   # 到位后恒定 0.0

    res = _analyze(client, _build("FV-CE1", pos))
    assert res["verdict"] == "pass"
    m = res["metrics"]
    assert m["settled"] is True
    assert m["final_position_pct"] == 0.0
    assert m["max_rebound_pct"] == 0.0
    # 恒定平台被重分类，而不是作为冻结/断档证据缺口
    gap_codes = [g["code"] for g in res["evidence_gaps"]]
    assert "dropout_in_action_window" not in gap_codes
    assert "not_settled_at_window_end" not in gap_codes
    pos_dropouts = [(e["channel"], e["kind"]) for e in res["dropouts"]]
    assert ("position", "frozen") not in pos_dropouts
    assert any(p["kind"] == "in_band_platform" for p in res["in_band_platforms"])


def test_rebound_after_settle_is_fail_not_no_conclusion(client):
    """进安全带后反弹约 7.52%、随后窗末连续稳定 ≥12.1s：判 fail 且 settled=True。"""
    hold, t_move, speed, band = 60.0, 5.6, 14.0, 2.0
    t_band = t_move + (hold - band) / speed     # 9.7429 首次到 2%

    def pos(t):
        if t < t_move:
            return hold
        base = hold - speed * (t - t_move)
        if base > band:
            return base
        if t < t_band + 0.56:
            return 0.5                            # 进带先稳定
        dt = t - (t_band + 0.56)
        if 0 <= dt < 1.6:
            shape = 1 - abs(dt / 1.6 - 0.5) * 2
            return 7.52 * shape                   # 峰 7.52%（采样网格上约 7.3%）
        return 0.2                                # 回落后恒定到窗末

    res = _analyze(client, _build("FV-CE2", pos))
    m = res["metrics"]
    # 稳定性按窗末连续平台判定，不再因反弹离带而判未稳定
    assert m["settled"] is True
    assert m["stable_tail_s"] >= 12.1
    # 反弹超过 5% 阈值 → fail（既不是 no_conclusion 也不是 pass）
    assert 7.0 < m["max_rebound_pct"] <= 8.0
    assert res["verdict"] == "fail"
    gap_codes = [g["code"] for g in res["evidence_gaps"]]
    assert "not_settled_at_window_end" not in gap_codes
    assert any(i["kind"] == "rebound" for i in res["issues"])
    rebound_check = next(c for c in res["checks"] if c["metric"] == "max_rebound_pct")
    assert rebound_check["pass"] is False
