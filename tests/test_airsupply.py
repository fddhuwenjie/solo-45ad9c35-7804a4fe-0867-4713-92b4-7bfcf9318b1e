"""气动执行机构供气瞬态核算 API 的端到端回归。

覆盖：
- 供气链充裕（pass）、供气不足（fail：供压低于要求/行程超时，指出首个设备）、
  储气罐不足（fail：安全位未到达）；
- 证据缺口：单位冲突、事件倒序、流量曲线覆盖不足、积分不收敛（no_conclusion）；
- 两个已复现缺陷的回归：
  1) 收敛判定必须遵守 convergence_tol_pct（0.000001 时不得 converged=True/pass）；
  2) spring_return + fail_close + 仅 A 腔供气的方案必须可提交并计算失气关闭
     安全行程（不得再因“驱动腔 B 不是供气驱动腔”拒绝整份方案）；
- 修订（并发关系改动 / 实测边界，须写理由）、版本比较与 JSON 导出。
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "as.db"))
    with TestClient(app) as c:
        yield c


def load_sample(name, **patch):
    payload = json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    for k, v in patch.items():
        payload[k] = v
    return payload


def create_and_solve(client, payload, author="test"):
    r = client.post("/airsupply/schemes", json=payload)
    assert r.status_code == 201, r.text
    sid = r.json()["scheme_id"]
    r = client.post(f"/airsupply/schemes/{sid}/solve", json={"author": author})
    assert r.status_code == 201, r.text
    return sid, r.json()


def spring_return_scheme(**overrides):
    """spring_return + fail_close + 仅 A 腔供气（反例 2 的最小方案）。"""
    scheme = {
        "valve_tag": "XV-7101", "valve_description": "弹簧复位执行器",
        "name": "sr_fail_close",
        "actuator": {
            "actuator_type": "spring_return", "fail_mode": "fail_close",
            "air_chambers": ["a"],
            "chamber_a": {"min_volume": {"value": 0.5, "unit": "l"},
                          "max_volume": {"value": 3.5, "unit": "l"}},
            "chamber_b": {"min_volume": {"value": 0.5, "unit": "l"},
                          "max_volume": {"value": 3.5, "unit": "l"}},
            "area_a": {"value": 100.0, "unit": "cm2"},
            "area_b": {"value": 100.0, "unit": "cm2"},
            "spring": {"force_unit": "n",
                       "points": [[0.0, 800.0], [50.0, 1900.0], [100.0, 3000.0]]},
            "load": {"value": 200.0, "unit": "n"},
            "friction": {"value": 100.0, "unit": "n"},
            "port_conductance": {"value": 5.0, "unit": "nl/min/kpa"},
            "initial_position_pct": 0.0,
            "initial_chamber_pressure": {"value": 0.0, "unit": "kPa"},
        },
        "supply": {"header_pressure": {"value": 600.0, "unit": "kPa"}},
        "pipe_segments": [{"name": "branch", "length_m": 20.0,
                           "inner_diameter_mm": 10.0, "friction_factor": 0.03,
                           "minor_loss_k": 0.0}],
        "regulator": {"set_pressure": {"value": 500.0, "unit": "kPa"},
                      "dp_unit": "kPa", "flow_unit": "Nl/min",
                      "flow_curve": [[25.0, 150.0], [50.0, 300.0],
                                     [100.0, 500.0], [200.0, 700.0]]},
        "tank": {"volume": {"value": 40.0, "unit": "l"},
                 "initial_pressure": {"value": 500.0, "unit": "kPa"}},
        "events": [],
        "actions": [
            {"name": "open1", "kind": "powered_stroke", "direction": "open",
             "t_start_s": 2.0, "travel_time_s_max": 15.0,
             "required_supply_pressure_min": {"value": 400.0, "unit": "kPa"}},
            {"name": "fail_close", "kind": "fail_safe_stroke", "direction": "close",
             "t_start_s": 30.0, "travel_time_s_max": 20.0,
             "required_supply_pressure_min": {"value": 150.0, "unit": "kPa"}},
        ],
        "solver": {"dt_s": 0.005, "t_max_s": 60.0, "record_dt_s": 0.05,
                   "convergence_tol_pct": 2.0, "max_stroke_rate_pct_s": 200.0,
                   "ambient_temp_c": 20.0},
    }
    for k, v in overrides.items():
        scheme[k] = v
    return scheme


# ---- 基本判定 ----

def test_good_sample_pass(client):
    _, d = create_and_solve(client, load_sample("sample_as_good"))
    r = d["result"]
    assert r["verdict"] == "pass"
    assert r["solver"]["converged"] is True
    assert len(r["actions"]) == 4
    for a in r["actions"]:
        assert a["reached"] is True
        assert a["time_margin_s"] > 0
        assert a["pressure_margin_kpa"] > 0
    assert r["series"]["t"], "应返回逐时结果"


def test_undersized_findings_first_device(client):
    _, d = create_and_solve(client, load_sample("sample_as_undersized"))
    r = d["result"]
    assert r["verdict"] == "fail"
    open1 = next(a for a in r["actions"] if a["name"] == "open1")
    kinds = {f["kind"] for f in open1["findings"]}
    assert "pressure_below_requirement" in kinds
    assert "stroke_timeout" in kinds
    # 首个相关用气设备与区间
    f = next(f for f in open1["findings"]
             if f["kind"] == "pressure_below_requirement")
    assert f["first_device"]["device"] == "D1"
    assert f["interval_s"][0] <= f["interval_s"][1]


def test_tank_shortfall_safe_position(client):
    _, d = create_and_solve(client, load_sample("sample_as_tank_shortfall"))
    r = d["result"]
    assert r["verdict"] == "fail"
    fail = next(a for a in r["actions"] if a["kind"] == "fail_safe_stroke")
    kinds = {f["kind"] for f in fail["findings"]}
    assert "safe_position_not_reached" in kinds
    assert fail["reached"] is False
    # 罐压仍高于要求，但存量不足
    assert fail["min_supply_pressure_kpa"] >= \
        fail["required_supply_pressure_min_kpa"]


# ---- 证据缺口 ----

def test_unit_conflict_no_conclusion(client):
    _, d = create_and_solve(client, load_sample("sample_as_unit_conflict"))
    r = d["result"]
    assert r["verdict"] == "no_conclusion"
    assert any(g["code"] == "unit_conflict" for g in r["evidence_gaps"])
    assert r["series"] is None


def test_event_order_no_conclusion(client):
    _, d = create_and_solve(client, load_sample("sample_as_event_order"))
    r = d["result"]
    assert r["verdict"] == "no_conclusion"
    assert any(g["code"] == "event_order" for g in r["evidence_gaps"])


def test_flow_curve_coverage_no_conclusion(client):
    _, d = create_and_solve(client, load_sample("sample_as_curve_gap"))
    r = d["result"]
    assert r["verdict"] == "no_conclusion"
    assert any(g["code"] == "flow_curve_coverage" for g in r["evidence_gaps"])


# ---- 反例 1：收敛判定必须遵守 convergence_tol_pct ----

def test_convergence_tolerance_is_binding(client):
    """tol=0.000001 且存在相对偏差时：不得 converged=True 或 pass，
    只生成 integration_not_converged 证据缺口。"""
    payload = load_sample("sample_as_good")
    payload["solver"]["convergence_tol_pct"] = 0.000001
    _, d = create_and_solve(client, payload)
    r = d["result"]
    assert r["solver"]["converged"] is False
    assert r["solver"]["max_rel_diff_pct"] > 0.000001
    assert r["verdict"] == "no_conclusion"
    assert r["verdict"] != "pass"
    gaps = [g for g in r["evidence_gaps"]
            if g["code"] == "integration_not_converged"]
    assert gaps, "应只生成 integration_not_converged 证据缺口"


def test_convergence_default_tolerance_passes(client):
    """对照：默认容差下同一方案应收敛并通过。"""
    _, d = create_and_solve(client, load_sample("sample_as_good"))
    r = d["result"]
    assert r["solver"]["converged"] is True
    assert r["verdict"] == "pass"
    assert not any(g["code"] == "integration_not_converged"
                   for g in r["evidence_gaps"])


# ---- 反例 2：spring_return + fail_close + 仅 A 腔供气 ----

def test_spring_return_fail_close_accepted_and_computed(client):
    """不得再因“驱动腔 B 不是供气驱动腔”拒绝整份方案；失气关闭安全行程可计算。"""
    r = client.post("/airsupply/schemes", json=spring_return_scheme())
    assert r.status_code == 201, r.text
    sid = r.json()["scheme_id"]
    r = client.post(f"/airsupply/schemes/{sid}/solve", json={"author": "test"})
    assert r.status_code == 201, r.text
    res = r.json()["result"]
    assert res["verdict"] == "pass"
    fail = next(a for a in res["actions"] if a["kind"] == "fail_safe_stroke")
    assert fail["spring_driven"] is True
    assert fail["reached"] is True
    assert fail["stroke_time_s"] is not None
    assert fail["final_position_pct"] <= fail["safe_band_pct"]


def test_spring_return_fail_close_weak_spring_not_reached(client):
    """弹簧过弱（最大力 < 负载+摩擦）时失气关闭停滞，安全位未到达。"""
    scheme = spring_return_scheme()
    scheme["actuator"]["spring"]["points"] = \
        [[0.0, 100.0], [50.0, 175.0], [100.0, 250.0]]
    _, d = create_and_solve(client, scheme)
    res = d["result"]
    assert res["verdict"] == "fail"
    fail = next(a for a in res["actions"] if a["kind"] == "fail_safe_stroke")
    assert fail["spring_driven"] is True
    assert fail["reached"] is False
    kinds = {f["kind"] for f in fail["findings"]}
    assert "safe_position_not_reached" in kinds


def test_spring_return_without_spring_rejected(client):
    """spring_return 未提供弹簧曲线仍为 422（结构性校验）。"""
    scheme = spring_return_scheme()
    del scheme["actuator"]["spring"]
    r = client.post("/airsupply/schemes", json=scheme)
    assert r.status_code == 422


def test_powered_stroke_on_spring_side_still_rejected(client):
    """供气驱动行程仍不得作用于弹簧侧腔（非失气安全行程）。"""
    scheme = spring_return_scheme()
    scheme["actions"] = [
        {"name": "close1", "kind": "powered_stroke", "direction": "close",
         "t_start_s": 2.0, "travel_time_s_max": 15.0,
         "required_supply_pressure_min": {"value": 400.0, "unit": "kPa"}},
    ]
    r = client.post("/airsupply/schemes", json=scheme)
    assert r.status_code == 422


# ---- 修订 / 比较 / 导出 ----

def test_revise_concurrency_and_measured_boundary(client):
    sid, d1 = create_and_solve(client, load_sample("sample_as_undersized"))
    assert d1["result"]["verdict"] == "fail"
    # 人工改动并发关系（错开 D1/D2）+ 采用实测边界（实测罐压），均须写理由
    r = client.post(f"/airsupply/schemes/{sid}/revise", json={
        "author": "tech",
        "concurrency_overrides": [
            {"device": "D1", "operation": "update",
             "t_start_s": 40.0, "t_end_s": 50.0,
             "reason": "与工艺确认 D1 用气窗口错后"},
            {"device": "D2", "operation": "remove",
             "reason": "D2 已改接独立气源"},
        ],
        "measured_boundaries": [
            {"field": "tank_initial_pressure", "value": 520.0, "unit": "kPa",
             "reason": "按现场 9:00 实测罐压"},
        ],
    })
    assert r.status_code == 201, r.text
    d2 = r.json()
    assert d2["revision"] == 2
    assert len(d2["adjustments"]) == 3
    assert all(a["reason"].strip() for a in d2["adjustments"])
    # 版本比较：读取同一冻结方案与逐时结果
    r = client.post("/airsupply/comparisons", json={
        "revision_id_a": d1["id"], "revision_id_b": d2["id"]})
    assert r.status_code == 201, r.text
    cmp = r.json()
    assert cmp["action_deltas"]
    assert len(cmp["adjustments_between"]) == 3


def test_revise_requires_reason(client):
    sid, _ = create_and_solve(client, load_sample("sample_as_good"))
    r = client.post(f"/airsupply/schemes/{sid}/revise", json={
        "author": "tech",
        "concurrency_overrides": [
            {"device": "D1", "operation": "remove", "reason": "  "},
        ],
    })
    assert r.status_code == 422


def test_export_matches_frozen_revision(client):
    sid, d = create_and_solve(client, load_sample("sample_as_good"))
    rid = d["id"]
    r_get = client.get(f"/airsupply/revisions/{rid}")
    r_exp = client.get(f"/airsupply/revisions/{rid}/export")
    assert r_get.status_code == 200 and r_exp.status_code == 200
    assert "attachment" in r_exp.headers.get("content-disposition", "")
    # 导出与读取为同一冻结方案与逐时结果
    assert r_exp.json()["scheme"] == r_get.json()["scheme"]
    assert r_exp.json()["result"]["series"] == r_get.json()["result"]["series"]


def test_compare_different_schemes_rejected(client):
    sid1, d1 = create_and_solve(client, load_sample("sample_as_good"))
    payload2 = load_sample("sample_as_good")
    payload2["name"] = "other"
    payload2["valve_tag"] = "XV-9999"
    sid2, d2 = create_and_solve(client, payload2)
    assert sid1 != sid2
    r = client.post("/airsupply/comparisons", json={
        "revision_id_a": d1["id"], "revision_id_b": d2["id"]})
    assert r.status_code == 422
