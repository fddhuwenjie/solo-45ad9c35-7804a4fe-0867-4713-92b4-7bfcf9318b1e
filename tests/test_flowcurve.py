"""单相液体流量曲线校核（/flowcurve 路由组）回归。

覆盖：
- 入口导入：app.main 可导入、create_app 注册全部 /flowcurve 路由，TestClient 可起；
- Cv 标准算例：1 US gpm 水、1 psi 压差 → Cv ≈ 1.0（单位换算系数 N1=0.865）；
- 稳态窗口采样频率：1 Hz 下 0→100% 连续斜坡不得识别出任何稳态平台；
- 端到端：铭牌一致 / 堵塞 / 冲蚀 / 反装判定，逐点排除（低压差、超量程、
  气蚀、平台漂移、物性缺项），人工修订版本留痕，叠加比较兼容性与打印页。
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.flowcurve import liquid
from app.flowcurve.plateaus import delineate_plateaus
from app.main import create_app

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "flowcurve.db"))
    with TestClient(app) as c:
        yield c


def load_sample(name, **patch):
    payload = json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    for k, v in patch.items():
        payload[k] = v
    return payload


def submit_and_analyze(client, payload, author="test"):
    r = client.post("/flowcurve/tests", json=payload)
    assert r.status_code == 201, r.text
    test_id = r.json()["flowcurve_test_id"]
    r = client.post(f"/flowcurve/tests/{test_id}/analyze", json={"author": author})
    assert r.status_code == 201, r.text
    return test_id, r.json()


# ---- 入口导入 ----

def test_app_imports_and_flowcurve_routes_registered():
    """create_app 成功（fc_report 等入口导入齐备），10 条 /flowcurve 路由就位。"""
    app = create_app(":memory:")
    paths = {r.path for r in app.routes}
    expected = {
        "/flowcurve/tests",
        "/flowcurve/tests/{fc_test_id}",
        "/flowcurve/tests/{fc_test_id}/analyze",
        "/flowcurve/tests/{fc_test_id}/analyses",
        "/flowcurve/analyses/{analysis_id}",
        "/flowcurve/analyses/{analysis_id}/adjust",
        "/flowcurve/comparisons",
        "/flowcurve/comparisons/{comparison_id}",
        "/flowcurve/analyses/{analysis_id}/export",
        "/flowcurve/analyses/{analysis_id}/report",
    }
    assert expected <= paths


def test_testclient_starts_and_openapi_serves(tmp_path):
    """应用可被 TestClient 启动（等同 Uvicorn 导入），OpenAPI 可访问。"""
    with TestClient(create_app(str(tmp_path / "x.db"))) as c:
        r = c.get("/openapi.json")
        assert r.status_code == 200
        assert "/flowcurve/tests" in r.json()["paths"]


# ---- Cv 标准算例 ----

def test_cv_one_gpm_one_psi_water_is_one():
    """1 US gpm 水、1 psi 压差 → Cv ≈ 1.0（N1=0.865, m³/h/bar 单位制）。"""
    q, notes, conflicts = liquid.norm_flow_liquid([[0.0, 1.0]], "gpm")
    assert conflicts == []
    assert q[0] == pytest.approx(0.227125, rel=1e-6)
    cv = liquid.liquid_cv(q[0], 6.894757, 999.0)
    assert cv == pytest.approx(1.0, abs=2e-3)


def test_cv_at_pipeline_gauge_pressures_standard_case():
    """cv_at 走表压→绝压换算：P1=200 kPa 表压、P2 低 1 psi、20 °C 水 → Cv≈1。"""
    out = liquid.cv_at(
        0.227125, 200.0, 200.0 - 6.894757, 20.0, {
            "atmospheric_pressure_kpa": 101.325,
            "density_kg_m3": 999.0,
            "vapor_pressure_kpa": 2.34,
            "critical_pressure_kpa": 22120.0,
            "liquid_recovery_factor_fl": 0.9,
            "density_ref_temp_c": None,
            "temp_band_c": None,
        })
    assert out["cv"] == pytest.approx(1.0, abs=2e-3)
    assert out["choked"] is False
    assert out["exclusion"] is None


def test_cv_non_si_units_convert_consistently():
    """gpm 与 psi 组合在不同流量水平保持 Cv 自洽（Q∝√ΔP）。"""
    base = liquid.liquid_cv(0.227125, 6.894757, 999.0)
    q4, _, _ = liquid.norm_flow_liquid([[0, 2.0]], "gpm")
    doubled = liquid.liquid_cv(q4[0], 4 * 6.894757, 999.0)
    assert doubled == pytest.approx(base, rel=1e-6)


def test_choked_flow_excluded_from_cv():
    """ΔP 超过阻塞临界压差（气蚀）时 cv_at 标记 cavitation 且不给拟合 Cv。"""
    # P1=600 kPa 绝压，FL=0.9，水：ΔPchoked≈0.81·(600−0.96·2.34)≈484 kPa
    out = liquid.cv_at(
        50.0, 498.675, 10.0, 20.0, {          # 表压：P1 绝 600，P2 绝 ≈111
            "atmospheric_pressure_kpa": 101.325,
            "density_kg_m3": 998.0,
            "vapor_pressure_kpa": 2.34,
            "critical_pressure_kpa": 22120.0,
            "liquid_recovery_factor_fl": 0.9,
            "density_ref_temp_c": None,
            "temp_band_c": None,
        })
    assert out["choked"] is True
    assert out["exclusion"] == "cavitation"
    assert out["cv"] is None


# ---- 稳态窗口采样频率处理 ----

PLATEAU_THR = {
    "plateau_band_pct": 1.0,
    "plateau_slope_pct_s": 0.5,
    "plateau_min_duration_s": 3.0,
    "plateau_merge_gap_s": 1.5,
    "plateau_merge_position_pct": 2.0,
    "plateau_drift_pct_max": 1.5,
    "plateau_flow_cv_pct_max": 5.0,
}


def test_one_hertz_ramp_has_no_plateau():
    """1 Hz 下 0→100% 全程斜坡（1%/s）不得识别为稳态平台。"""
    grid = [float(i) for i in range(101)]
    position = [float(i) for i in range(101)]
    plateaus, _ = delineate_plateaus(grid, position, PLATEAU_THR)
    assert plateaus == []


def test_one_hertz_slow_ramp_has_no_plateau():
    """0.6%/s 的 1 Hz 慢斜坡（仍高于 0.5 阈值）不得整段误判为稳态。"""
    grid = [float(i) for i in range(200)]
    position = [0.6 * i for i in range(200)]
    plateaus, _ = delineate_plateaus(grid, position, PLATEAU_THR)
    assert plateaus == []


def test_one_hertz_flat_with_step_yields_one_plateau():
    """1 Hz：长时间停在 40% 再爬升，停留段必须被识别为平台（修复不过度）。"""
    grid = [float(i) for i in range(40)]
    position = [40.0] * 20 + [40.0 + (i + 1) for i in range(20)]
    plateaus, _ = delineate_plateaus(grid, position, PLATEAU_THR)
    assert len(plateaus) == 1
    assert plateaus[0]["position_median_pct"] == pytest.approx(40.0)
    assert plateaus[0]["n_points"] >= 15


def test_coarse_two_second_step_grid_detects_dwell():
    """0.5 Hz（2s 步长）停留仍可识别：窗口按采样步长扩展而非固定 1s。"""
    grid = [2.0 * i for i in range(20)]
    position = [30.0] * 10 + [30.0 + 4.0 * (i + 1) for i in range(10)]
    plateaus, _ = delineate_plateaus(grid, position, PLATEAU_THR)
    assert len(plateaus) == 1
    assert plateaus[0]["position_median_pct"] == pytest.approx(30.0)


# ---- 端到端样例矩阵 ----

CONCLUSIVE = {
    "sample_fc_linear_good": ("matches_nameplate", 1.0, []),
    "sample_fc_blocked": ("suspect", 0.72, ["blockage"]),
    "sample_fc_eroded": ("suspect", 1.22, ["erosion"]),
    "sample_fc_reversed": ("suspect", None, ["reversed_installation"]),
}


@pytest.mark.parametrize("name", sorted(CONCLUSIVE))
def test_characteristic_scenarios(client, name):
    verdict, k_exp, suspects = CONCLUSIVE[name]
    _, a = submit_and_analyze(client, load_sample(name))
    r, curve = a["result"], a["result"]["curve"]
    assert r["verdict"] == verdict
    assert r["evidence_gaps"] == []
    assert all(p["adopted"] for p in r["points"])
    if k_exp is not None:
        assert curve["capacity_factor"] == pytest.approx(k_exp, abs=0.01)
    assert [s["kind"] for s in r["suspects"]] == suspects
    if name == "sample_fc_linear_good":
        assert curve["monotonic"] is True
        assert curve["spearman_rho"] == pytest.approx(1.0)
        assert curve["effective_turndown"] == pytest.approx(8.0, rel=0.05)
        assert all(c["pass"] for c in r["checks"])


EXCLUDED = {
    "sample_fc_low_dp": ({0}, "insufficient_differential_pressure"),
    "sample_fc_overrange": ({6, 7}, "flow_overrange"),
    "sample_fc_cavitation": ({6, 7}, "cavitation"),
    "sample_fc_drift": ({3}, "plateau_drift"),
}


@pytest.mark.parametrize("name", sorted(EXCLUDED))
def test_pointwise_exclusions(client, name):
    excluded_idx, code = EXCLUDED[name]
    _, a = submit_and_analyze(client, load_sample(name))
    r = a["result"]
    got = {p["plateau_index"]: p["exclusion_codes"] for p in r["points"]
           if not p["adopted"]}
    assert set(got) == excluded_idx
    for codes in got.values():
        assert code in codes
    # 被排除点不参与拟合：采用点拟合容量系数仍≈1（铭牌一致）
    assert r["curve"]["capacity_factor"] == pytest.approx(1.0, abs=0.02)
    # 排除缘由逐点给出（中文名与标准算例码）
    for p in r["points"]:
        if not p["adopted"]:
            assert p["exclusion_reasons"]
            assert p["cv"] is None
        else:
            assert p["cv"] is not None and p["cv"] > 0


def test_cavitation_point_has_choked_conversion_params(client):
    _, a = submit_and_analyze(client, load_sample("sample_fc_cavitation"))
    p7 = next(p for p in a["result"]["points"] if p["plateau_index"] == 7)
    conv = p7["conversion"]
    assert p7["choked"] is True
    assert conv["dp_choked_kpa"] is not None and conv["ff"] is not None
    assert conv["dp_kpa"] > conv["dp_choked_kpa"]
    assert "N1=0.865" in conv["formula"]


def test_missing_property_is_no_conclusion(client):
    _, a = submit_and_analyze(client, load_sample("sample_fc_missing_property"))
    r = a["result"]
    assert r["verdict"] == "no_conclusion"
    codes = [g["code"] for g in r["evidence_gaps"]]
    assert "property_missing" in codes
    assert all(not p["adopted"] for p in r["points"])
    assert r["curve"] is None


def test_ramp_sample_no_plateau_no_conclusion(client):
    _, a = submit_and_analyze(client, load_sample("sample_fc_ramp_1hz"))
    r = a["result"]
    assert r["verdict"] == "no_conclusion"
    assert r["plateaus"] == []
    assert any(g["code"] == "no_plateau" for g in r["evidence_gaps"])


# ---- 人工修订与版本留痕 ----

def test_adjust_disable_point_and_move_boundary(client):
    _, a1 = submit_and_analyze(client, load_sample("sample_fc_linear_good"))
    r = client.post(f"/flowcurve/analyses/{a1['id']}/adjust", json={
        "author": "tech-01",
        "disabled_points": [{"plateau_index": 0, "reason": "该点压差刚建立，弃用"}],
    })
    assert r.status_code == 201, r.text
    a2 = r.json()
    assert a2["version"] == a1["version"] + 1
    assert a2["adjustments"][0]["reason"] == "该点压差刚建立，弃用"
    p0 = next(p for p in a2["result"]["points"] if p["plateau_index"] == 0)
    assert p0["adopted"] is False
    assert "manual_disabled" in p0["exclusion_codes"]
    # 停用最低点后有效调节比下降
    assert a2["result"]["curve"]["effective_turndown"] < \
        a1["result"]["curve"]["effective_turndown"]

    # 再移动平台 7 结束边界（提前 2s）
    p7 = next(p for p in a2["result"]["points"] if p["plateau_index"] == 7)
    r = client.post(f"/flowcurve/analyses/{a2['id']}/adjust", json={
        "author": "tech-02",
        "plateau_moves": [{"plateau_index": 7, "boundary": "end",
                           "new_time": p7["t_end_s"] - 2.0, "reason": "末端扰动"}],
    })
    a3 = r.json()
    assert a3["version"] == 3
    assert [x["type"] for x in a3["adjustments"]] == ["disable_point", "plateau_move"]
    assert a3["result"]["boundary_log"][-1]["applied"] is True
    p7b = next(p for p in a3["result"]["points"] if p["plateau_index"] == 7)
    assert "人工调整" in next(
        q for q in a3["result"]["plateaus"] if q["index"] == 7)["end_reason"]
    assert p7b["t_end_s"] <= p7["t_end_s"]
    # 旧版只读
    v1 = client.get(f"/flowcurve/analyses/{a1['id']}").json()
    assert v1["adjustments"] == []
    assert all(p["adopted"] for p in v1["result"]["points"])


def test_adjust_validation(client):
    _, a = submit_and_analyze(client, load_sample("sample_fc_linear_good"))
    assert client.post(f"/flowcurve/analyses/{a['id']}/adjust",
                       json={"author": "x"}).status_code == 422
    assert client.post(f"/flowcurve/analyses/{a['id']}/adjust", json={
        "author": "x",
        "disabled_points": [{"plateau_index": 1, "reason": ""}],
    }).status_code == 422
    assert client.post("/flowcurve/analyses/999/adjust", json={
        "author": "x", "disabled_points": [{"plateau_index": 1, "reason": "r"}],
    }).status_code == 404


# ---- 叠加比较 ----

def _retag(payload, tag="FCV-701"):
    payload["valve_tag"] = tag
    return payload


def test_comparison_compatible_and_degrading(client):
    _, good = submit_and_analyze(client, load_sample("sample_fc_linear_good"))
    _, blocked = submit_and_analyze(
        client, _retag(load_sample("sample_fc_blocked")))
    r = client.post("/flowcurve/comparisons",
                    json={"analysis_ids": [good["id"], blocked["id"]]})
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["compatible"] is True
    assert c["overall"] == "degrading"
    assert [o["capacity_factor"] for o in c["overlay"]] == [1.0, pytest.approx(0.72, abs=0.01)]
    assert {p["position_pct"] for o in c["overlay"] for p in o["points"]}
    kf = next(t for t in c["trends"] if t["metric"] == "capacity_factor")
    assert kf["status"] == "degrading"
    # 比较记录持久化
    cid = c["comparison_id"]
    assert client.get(f"/flowcurve/comparisons/{cid}").status_code == 200


def test_comparison_incompatible_trim(client):
    _, good = submit_and_analyze(client, load_sample("sample_fc_linear_good"))
    p = _retag(load_sample("sample_fc_eroded"))
    p["valve"]["trim"] = "TRIM-LIN-99"
    _, other = submit_and_analyze(client, p)
    c = client.post("/flowcurve/comparisons",
                    json={"analysis_ids": [good["id"], other["id"]]}).json()
    assert c["compatible"] is False
    entry = next(e for e in c["series"] if e["analysis_id"] == other["id"])
    assert any("阀内件不一致" in x for x in entry["reasons"])


def test_comparison_incompatible_meter_range(client):
    _, good = submit_and_analyze(client, load_sample("sample_fc_linear_good"))
    p = _retag(load_sample("sample_fc_linear_good"))
    p["meter"]["full_scale"] = 95.0          # 量程不一致
    _, other = submit_and_analyze(client, p)
    c = client.post("/flowcurve/comparisons",
                    json={"analysis_ids": [good["id"], other["id"]]}).json()
    entry = next(e for e in c["series"] if e["analysis_id"] == other["id"])
    assert any("流量计量程不一致" in x for x in entry["reasons"])


def test_comparison_excludes_no_conclusion(client):
    _, good = submit_and_analyze(client, load_sample("sample_fc_linear_good"))
    _, noprop = submit_and_analyze(
        client, _retag(load_sample("sample_fc_missing_property")))
    c = client.post("/flowcurve/comparisons",
                    json={"analysis_ids": [good["id"], noprop["id"]]}).json()
    entry = next(e for e in c["series"] if e["analysis_id"] == noprop["id"])
    assert entry["comparable"] is False
    assert any("证据缺口" in x for x in entry["reasons"])


# ---- 导出与打印页 ----

def test_report_shows_adopted_points_conversion_and_exclusions(client):
    _, a = submit_and_analyze(client, load_sample("sample_fc_low_dp"))
    r = client.get(f"/flowcurve/analyses/{a['id']}/report")
    assert r.status_code == 200
    body = r.text
    assert "<svg" in body
    assert "采用" in body and "排除缘由" in body
    assert "N1=0.865" in body                    # 换算参数
    assert "ΔPchoked" in body
    assert "insufficient_differential_pressure" in body or "压差不足" in body
    assert "position#" in body and "flow#" in body   # 原始点引用
    assert "有效调节比" in body


def test_export_filename_and_content(client):
    _, a = submit_and_analyze(client, load_sample("sample_fc_blocked"))
    r = client.get(f"/flowcurve/analyses/{a['id']}/export")
    assert r.status_code == 200
    assert f"flowcurve_{a['id']}_v{a['version']}.json" in \
        r.headers["content-disposition"]
    data = r.json()
    assert data["result"]["suspects"][0]["kind"] == "blockage"


def test_not_found_routes(client):
    assert client.get("/flowcurve/tests/999").status_code == 404
    assert client.post("/flowcurve/tests/999/analyze", json={}).status_code == 404
    assert client.get("/flowcurve/analyses/999").status_code == 404
    assert client.get("/flowcurve/analyses/999/report").status_code == 404
