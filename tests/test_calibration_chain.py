"""逐通道仪器校准链回归测试。

覆盖三处已确认行为：
1. 测试终点 = test_started_at + 所有通道的**最大样本时标**；非零起始时标使
   测试跨过证书 valid_until（或尚未生效）时 no_conclusion，并记录阻断原因；
2. 通道绑定冻结校准版本后，校准标准不确定度只取证书 standard_uncertainty，
   旧通用 calibration 块 / 通道 calibration 分量不得覆盖；未绑定通道仍兼容旧字段；
3. 建版接受既有分析已支持的 m³/h、°C 及等价写法（m3/h、K、degC…）。
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def load_sample(name="sample_normal"):
    return json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def create_version(client, *, serial, mtype, unit, points, su=0.2,
                   vf="2020-01-01", vu="2030-12-31",
                   rmin=None, rmax=None):
    body = {
        "instrument_serial": serial,
        "measurement_type": mtype,
        "unit": unit,
        "valid_from": vf,
        "valid_until": vu,
        "range_min": rmin if rmin is not None else min(p[0] for p in points),
        "range_max": rmax if rmax is not None else max(p[0] for p in points),
        "points": points,
        "standard_uncertainty": su,
        "certificate_summary": f"cert-{serial}",
    }
    r = client.post("/calibration-versions", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def fullstroke_versions(client, *, cmd_vu="2030-12-31",
                        cmd_vf="2020-01-01", cmd_points=None):
    """三路全行程通道的合法校准版本；command 有效期/点列可覆盖。"""
    v_cmd = create_version(
        client, serial="CMD-1", mtype="command", unit="%",
        vf=cmd_vf, vu=cmd_vu, rmin=-10, rmax=110,
        points=cmd_points or [[-10, -10], [0, 0.5], [50, 50.5],
                              [100, 100.5], [110, 110]], su=0.15)
    v_pos = create_version(
        client, serial="POS-1", mtype="position", unit="%",
        rmin=-10, rmax=110,
        points=[[-10, -10], [0, 0.2], [100, 100.2], [110, 110]], su=0.2)
    v_prs = create_version(
        client, serial="PRS-1", mtype="pressure", unit="kPa",
        rmin=0, rmax=1000,
        points=[[0, 0], [600, 605], [1000, 1005]], su=2.0)
    return v_cmd, v_pos, v_prs


def analyze(client, payload):
    r = client.post("/tests", json=payload)
    assert r.status_code == 201, r.text
    tid = r.json()["test_id"]
    r = client.post(f"/tests/{tid}/analyze", json={"author": "test"})
    assert r.status_code == 201, r.text
    return tid, r.json()["result"]


# ===================== 1) 测试时间区间与有效期边界 =====================

def test_test_interval_uses_max_absolute_timestamp(client):
    """终点 = started_at + 最大样本时标（不是 max−min 跨度）。"""
    sample = load_sample()
    v_cmd, v_pos, v_prs = fullstroke_versions(client)
    sample["calibration_bindings"] = {
        "command": v_cmd, "position": v_pos, "pressure": v_prs}
    _, result = analyze(client, sample)
    chain = result["calibration_chain"]
    # 样本压力通道最大时标 55.11s（起点 0.11），终点必须到 55.11s 而不是跨度
    assert chain["test_max_t_s"] == pytest.approx(55.11)
    assert chain["test_interval"] == [
        "2026-09-10T08:00:00+00:00", "2026-09-10T08:00:55.110000+00:00"]
    assert chain["accepted"] is True
    assert result["verdict"] == "ok"


def test_nonzero_start_timestamp_crossing_valid_until_blocks(client):
    """非零起始时标（+1000s 偏移）使测试跨越证书 valid_until → no_conclusion。"""
    sample = load_sample()
    # command 证书在 started_at 后约 8m20s 失效；样本时标平移到 [1000, 1055.11]s，
    # 区间 [start, start+1055.11] 跨越失效边界
    v_cmd = create_version(
        client, serial="CMD-SPAN", mtype="command", unit="%",
        vf="2020-01-01", vu="2026-09-10T08:08:20+00:00", rmin=-10, rmax=110,
        points=[[-10, -10], [0, 0], [100, 100], [110, 110]], su=0.1)
    v_pos = create_version(
        client, serial="POS-1", mtype="position", unit="%",
        rmin=-10, rmax=110,
        points=[[-10, -10], [0, 0.2], [100, 100.2], [110, 110]], su=0.2)
    v_prs = create_version(
        client, serial="PRS-1", mtype="pressure", unit="kPa",
        rmin=0, rmax=1000,
        points=[[0, 0], [600, 605], [1000, 1005]], su=2.0)
    for d in sample["series"].values():
        d["points"] = [[t + 1000.0, v] for t, v in d["points"]]
    sample["calibration_bindings"] = {
        "command": v_cmd, "position": v_pos, "pressure": v_prs}
    _, result = analyze(client, sample)

    assert result["verdict"] == "no_conclusion"
    chain = result["calibration_chain"]
    # 终点必须按最大绝对时标 1055.11s 计算（旧跨度逻辑只会得到 55.11s 而漏判）
    assert chain["test_max_t_s"] == pytest.approx(1055.11)
    assert chain["test_interval"][1] == "2026-09-10T08:17:35.110000+00:00"
    rej = {(r["channel"], r["code"]) for r in chain["rejections"]}
    assert ("command", "calibration_spans_validity") in rej
    # 阻断原因同时出现在顶层 blocking_issues，且带通道与读数区间证据
    cmd_rej = next(r for r in chain["rejections"] if r["channel"] == "command")
    assert cmd_rej["raw_reading_interval"]
    assert any("calibration_spans_validity" in b and "command" in b
               for b in result["blocking_issues"])


def test_certificate_expired_and_not_yet_valid_blocks(client):
    sample = load_sample()
    _, v_pos, v_prs = fullstroke_versions(client)
    v_expired = create_version(
        client, serial="CMD-OLD", mtype="command", unit="%",
        vf="2010-01-01", vu="2019-12-31", rmin=-10, rmax=110,
        points=[[-10, -10], [0, 0], [100, 100], [110, 110]], su=0.1)
    sample["calibration_bindings"] = {
        "command": v_expired, "position": v_pos, "pressure": v_prs}
    _, result = analyze(client, sample)
    assert result["verdict"] == "no_conclusion"
    assert ("command", "calibration_expired") in {
        (r["channel"], r["code"]) for r in result["calibration_chain"]["rejections"]}

    v_future = create_version(
        client, serial="CMD-NEW", mtype="command", unit="%",
        vf="2030-01-01", vu="2035-01-01", rmin=-10, rmax=110,
        points=[[-10, -10], [0, 0], [100, 100], [110, 110]], su=0.1)
    sample2 = load_sample()
    sample2["calibration_bindings"] = {
        "command": v_future, "position": v_pos, "pressure": v_prs}
    _, result2 = analyze(client, sample2)
    assert result2["verdict"] == "no_conclusion"
    assert ("command", "calibration_not_yet_valid") in {
        (r["channel"], r["code"]) for r in result2["calibration_chain"]["rejections"]}


def test_range_uncovered_and_unbound_block(client):
    sample = load_sample()
    v_cmd, v_pos, v_prs = fullstroke_versions(client)
    # 量程只到 50%，但阀位读数到 90%
    v_short = create_version(
        client, serial="POS-SHORT", mtype="position", unit="%",
        rmin=0, rmax=50, points=[[0, 0], [50, 50]], su=0.2)
    sample["calibration_bindings"] = {
        "command": v_cmd, "position": v_short, "pressure": v_prs}
    _, result = analyze(client, sample)
    assert result["verdict"] == "no_conclusion"
    rej = next(r for r in result["calibration_chain"]["rejections"]
               if r["channel"] == "position")
    assert rej["code"] == "calibration_range_uncovered"
    assert rej["raw_reading_interval"][1] > 50

    # pressure 未绑定
    sample2 = load_sample()
    sample2["calibration_bindings"] = {"command": v_cmd, "position": v_pos}
    _, result2 = analyze(client, sample2)
    assert result2["verdict"] == "no_conclusion"
    assert ("pressure", "calibration_not_bound") in {
        (r["channel"], r["code"]) for r in result2["calibration_chain"]["rejections"]}


# ===================== 2) 证书 standard_uncertainty 优先级 =====================

def test_chain_certificate_uncertainty_overrides_legacy_config(client):
    """绑定通道的校准标准不确定度只取证书值，旧通用/通道 calibration 被抑制。"""
    sample = load_sample()  # 自带 uncertainty.calibration 块（0.2%，command/position）
    v_cmd, v_pos, v_prs = fullstroke_versions(client)
    sample["calibration_bindings"] = {
        "command": v_cmd, "position": v_pos, "pressure": v_prs}
    _, result = analyze(client, sample)
    assert result["verdict"] == "ok"
    unc = result["uncertainty"]
    assert unc["status"] == "evaluated"
    comps = unc["components"]

    # 每路恰好一个生效的校准分量，来源 calibration_chain，值=证书 standard_uncertainty
    active = {(x["channel"], x["source"]): x for x in comps
              if x["source"] == "calibration_chain"}
    assert active[("command", "calibration_chain")]["declared"]["value"] == 0.15
    assert active[("position", "calibration_chain")]["declared"]["value"] == 0.2
    assert active[("pressure", "calibration_chain")]["declared"]["value"] == 2.0
    # 归一化尺度（command/position 为 %，pressure 为 kPa）
    assert active[("command", "calibration_chain")]["normalized_scale"] == 0.15
    assert active[("pressure", "calibration_chain")]["normalized_scale"] == 2.0
    assert active[("pressure", "calibration_chain")]["normalized_unit"] == "kPa"

    # 旧通用 calibration 块（command/position, 0.2%）必须被标记抑制，不参与扰动
    suppressed = {(x["channel"], x["source"]) for x in comps
                  if x.get("status") == "suppressed_by_chain"}
    assert ("command", "calibration") in suppressed
    assert ("position", "calibration") in suppressed
    # 非校准分量（resolution/accuracy/jitter）不受影响，仍按旧字段参与
    assert any(x["channel"] == "command" and x["kind"] == "accuracy"
               and "status" not in x for x in comps)
    assert any(x["channel"] == "pressure" and x["kind"] == "resolution"
               for x in comps)


def test_legacy_uncertainty_config_still_works_without_bindings(client):
    """未绑定（legacy 模式）：旧 calibration_valid_until + uncertainty 配置照旧。"""
    sample = load_sample()
    assert "calibration_bindings" not in sample
    _, result = analyze(client, sample)
    assert result["calibration_chain"]["mode"] == "legacy"
    unc = result["uncertainty"]
    assert unc["status"] == "evaluated"
    # 旧通用 calibration 块在 legacy 模式下仍然生效（没有抑制记录）
    cal_comps = [x for x in unc["components"] if x["kind"] == "calibration"]
    assert cal_comps and all(x.get("status") != "suppressed_by_chain"
                             for x in cal_comps)
    srcs = {x["source"] for x in cal_comps}
    assert "calibration" in srcs


def test_rebind_derives_new_version_and_freezes_old(client):
    """改绑派生新版本；旧分析冻结的证书与修正量不变。"""
    sample = load_sample()
    v_cmd, v_pos, v_prs = fullstroke_versions(client)
    sample["calibration_bindings"] = {
        "command": v_cmd, "position": v_pos, "pressure": v_prs}
    tid, a1 = analyze(client, sample)
    a1id = a1["analysis_id"]
    ch1 = next(c for c in a1["calibration_chain"]["channels"]
               if c["channel"] == "command")
    assert ch1["calibration_version_id"] == v_cmd
    assert ch1["mean_correction"] == pytest.approx(0.5)

    v_cmd2 = create_version(
        client, serial="CMD-2", mtype="command", unit="%",
        rmin=-10, rmax=110,
        points=[[-10, -10], [0, 0.7], [50, 50.7], [100, 100.7], [110, 110]],
        su=0.1)
    r = client.post(f"/analyses/{a1id}/adjust", json={
        "author": "tech-02",
        "calibration_bindings": {"command": v_cmd2, "position": v_pos,
                                 "pressure": v_prs}})
    assert r.status_code == 201, r.text
    a2 = r.json()
    assert a2["version"] == a1["version"] + 1
    ch2 = next(c for c in a2["result"]["calibration_chain"]["channels"]
               if c["channel"] == "command")
    assert ch2["calibration_version_id"] == v_cmd2
    assert ch2["mean_correction"] == pytest.approx(0.7)
    assert any(a["type"] == "calibration_rebind" for a in a2["adjustments"])

    # 旧版本仍冻结在原证书
    a1_refetch = client.get(f"/analyses/{a1id}").json()["result"]
    ch1_old = next(c for c in a1_refetch["calibration_chain"]["channels"]
                   if c["channel"] == "command")
    assert ch1_old["calibration_version_id"] == v_cmd
    assert ch1_old["mean_correction"] == pytest.approx(0.5)


# ===================== 3) 建版单位接受 m³/h、°C 及等价写法 =====================

@pytest.mark.parametrize("mtype,unit,points,rmin,rmax", [
    ("flow_liquid", "m³/h", [[0, 0], [100, 100]], 0, 100),
    ("flow_liquid", "m3/h", [[0, 0], [100, 100]], 0, 100),
    ("flow_liquid", "m³/min", [[0, 0], [60, 60]], 0, 60),
    ("flow_liquid", "l/min", [[0, 0], [1000, 1000]], 0, 1000),
    ("flow_liquid", "gpm", [[0, 0], [100, 100]], 0, 100),
    ("temperature", "°C", [[-20, -20], [0, 0], [80, 80]], -20, 80),
    ("temperature", "C", [[-20, -20], [80, 80]], -20, 80),
    ("temperature", "degC", [[-20, -20], [80, 80]], -20, 80),
    ("temperature", "celsius", [[-20, -20], [80, 80]], -20, 80),
    ("temperature", "K", [[253, 253], [353, 353]], 253, 353),
    ("temperature", "°F", [[0, 0], [176, 176]], 0, 176),
])
def test_create_version_accepts_legacy_units(client, mtype, unit, points,
                                             rmin, rmax):
    r = client.post("/calibration-versions", json={
        "instrument_serial": f"S-{unit}", "measurement_type": mtype,
        "unit": unit, "valid_from": "2020-01-01", "valid_until": "2030-01-01",
        "range_min": rmin, "range_max": rmax, "points": points,
        "standard_uncertainty": 0.3})
    assert r.status_code == 201, r.text
    assert r.json()["unit"] == unit


def test_create_version_rejects_unit_outside_family(client):
    r = client.post("/calibration-versions", json={
        "instrument_serial": "BAD", "measurement_type": "command",
        "unit": "kPa", "valid_from": "2020-01-01", "valid_until": "2030-01-01",
        "range_min": 0, "range_max": 100,
        "points": [[0, 0], [100, 100]], "standard_uncertainty": 0.2})
    assert r.status_code == 422
    assert "单位" in r.json()["detail"]


def test_create_version_rejects_nonmonotonic_points(client):
    r = client.post("/calibration-versions", json={
        "instrument_serial": "MONO", "measurement_type": "pressure",
        "unit": "kPa", "valid_from": "2020-01-01", "valid_until": "2030-01-01",
        "range_min": 0, "range_max": 100,
        "points": [[0, 0], [60, 62], [50, 50], [100, 100]],
        "standard_uncertainty": 0.5})
    assert r.status_code == 422


def test_temperature_and_flow_correction_roundtrip(client):
    """°C↔K 与 m³/h↔l/min 同族单位在修正链路上可换算。"""
    from app import calibration as cc

    # K 证书修正 °C 读数（同族仿射换算）
    out = cc.convert_value(25.0, "°C", "K", "temperature")
    assert out == pytest.approx(298.15)
    # m³/h 与 l/min 线性换算
    assert cc.convert_value(1.0, "m³/h", "l/min", "flow_liquid") == \
        pytest.approx(1000.0 / 60.0)
    # 不确定度换算：温度 °F→°C 斜率 5/9
    assert cc.convert_uncertainty(1.8, "°F", "°C", "temperature") == \
        pytest.approx(1.0)


# ===================== 跨模块（五类测试共用校准链） =====================

def _post_and_analyze(client, submit_path, analyze_path, sample_name, bindings):
    payload = load_sample(sample_name) if sample_name == "sample_normal" else \
        json.loads((SAMPLES / f"{sample_name}.json").read_text(encoding="utf-8"))
    payload["calibration_bindings"] = bindings
    r = client.post(submit_path, json=payload)
    assert r.status_code == 201, r.text
    tid = next(iter(r.json().values()))
    r = client.post(f"{analyze_path}/{tid}/analyze", json={"author": "t"})
    assert r.status_code == 201, r.text
    return r.json()["result"]


def test_seatleak_chain_with_degc_certificate(client):
    """阀座试验：温度证书用 °C，五路绑定后 pass。"""
    b = {
        "command": create_version(client, serial="S-CMD", mtype="command", unit="%",
                                  rmin=-5, rmax=65,
                                  points=[[-5, -5], [0, 0], [60, 60], [65, 65]]),
        "position": create_version(client, serial="S-POS", mtype="position", unit="%",
                                   rmin=-5, rmax=65,
                                   points=[[-5, -5], [0, 0], [60, 60], [65, 65]]),
        "upstream_pressure": create_version(client, serial="S-UP", mtype="pressure",
                                            unit="kPa", rmin=0, rmax=700,
                                            points=[[0, 0], [500, 500], [700, 700]]),
        "downstream_pressure": create_version(client, serial="S-DN", mtype="pressure",
                                              unit="kPa", rmin=0, rmax=400,
                                              points=[[0, 0], [200, 200], [400, 400]]),
        "downstream_temp": create_version(client, serial="S-T", mtype="temperature",
                                          unit="°C", rmin=0, rmax=60, su=0.3,
                                          points=[[0, 0], [25, 25], [60, 60]]),
    }
    r = _post_and_analyze(client, "/seatleak/tests", "/seatleak/tests",
                          "sample_seatleak_tight", b)
    assert r["calibration_chain"]["accepted"] is True
    assert r["verdict"] == "pass"
    html = client.get(f"/seatleak/analyses/{r['analysis_id']}/report").text
    assert "逐通道仪器校准链" in html


def test_flowcurve_chain_with_m3h_certificate(client):
    """流量曲线：流量计证书用 m³/h（通道单位 m3/h，同族），绑定后正常校核。"""
    b = {
        "position": create_version(client, serial="FC-POS", mtype="position", unit="%",
                                   rmin=0, rmax=100,
                                   points=[[0, 0], [10, 10], [80, 80], [100, 100]]),
        "flow": create_version(client, serial="FC-FLOW", mtype="flow_liquid",
                               unit="m³/h", rmin=0, rmax=200, su=0.5,
                               points=[[0, 0], [120, 120], [200, 200]]),
        "upstream_pressure": create_version(client, serial="FC-UP", mtype="pressure",
                                            unit="kPa", rmin=0, rmax=1200,
                                            points=[[0, 0], [500, 500], [1200, 1200]]),
        "downstream_pressure": create_version(client, serial="FC-DN", mtype="pressure",
                                              unit="kPa", rmin=0, rmax=1200,
                                              points=[[0, 0], [200, 200], [1200, 1200]]),
        "temperature": create_version(client, serial="FC-T", mtype="temperature",
                                      unit="degC", rmin=0, rmax=120, su=0.3,
                                      points=[[0, 0], [20, 20], [120, 120]]),
    }
    r = _post_and_analyze(client, "/flowcurve/tests", "/flowcurve/tests",
                          "sample_fc_linear_good", b)
    assert r["calibration_chain"]["accepted"] is True
    assert r["verdict"] == "matches_nameplate"
    fl = next(x for x in r["calibration_chain"]["channels"] if x["channel"] == "flow")
    # 通道单位 m3/h 与证书 m³/h 同族，修正前后区间一致（恒等点列）
    assert fl["unit"] == "m3/h"
    assert fl["certificate"]["unit"] == "m³/h"


def test_failsafe_and_thrust_chain(client):
    """故障安全（trip 不参与）与推力签名（单作用仅 A 腔）链模式通过。"""
    b_fs = {
        "command": create_version(client, serial="FS-CMD", mtype="command", unit="%",
                                  rmin=-5, rmax=105,
                                  points=[[-5, -5], [0, 0], [100, 100], [105, 105]]),
        "position": create_version(client, serial="FS-POS", mtype="position", unit="%",
                                   rmin=-5, rmax=105,
                                   points=[[-5, -5], [0, 0], [100, 100], [105, 105]]),
        "pressure": create_version(client, serial="FS-PRS", mtype="pressure",
                                   unit="kPa", rmin=0, rmax=1200,
                                   points=[[0, 0], [1000, 1000], [1200, 1200]]),
    }
    rfs = _post_and_analyze(client, "/failsafe/tests", "/failsafe/tests",
                            "sample_failsafe_close_good", b_fs)
    assert rfs["calibration_chain"]["accepted"] is True
    assert "trip" not in {x["channel"] for x in rfs["calibration_chain"]["channels"]}
    assert rfs["verdict"] == "pass"

    b_ts = {
        "command": create_version(client, serial="TS-CMD", mtype="command", unit="%",
                                  rmin=-5, rmax=105,
                                  points=[[-5, -5], [0, 0], [100, 100], [105, 105]]),
        "position": create_version(client, serial="TS-POS", mtype="position", unit="%",
                                   rmin=-5, rmax=105,
                                   points=[[-5, -5], [0, 0], [100, 100], [105, 105]]),
        "supply_pressure": create_version(client, serial="TS-SUP", mtype="pressure",
                                          unit="kPa", rmin=0, rmax=1200,
                                          points=[[0, 0], [500, 500], [1200, 1200]]),
        "chamber_a_pressure": create_version(client, serial="TS-A", mtype="pressure",
                                             unit="kPa", rmin=0, rmax=1200,
                                             points=[[0, 0], [500, 500], [1200, 1200]]),
    }
    rts = _post_and_analyze(client, "/stemthrust/tests", "/stemthrust/tests",
                            "sample_thrust_good", b_ts)
    assert rts["calibration_chain"]["accepted"] is True
    assert rts["verdict"] in ("pass", "fail")  # 链通过即达成目的（机械结论依样本）


def test_pairing_includes_calibration_comparison(client):
    """检修前后配对结果含逐通道证书比较（采用证书与修正量）。"""
    sample = load_sample("sample_normal")
    v_cmd, v_pos, v_prs = fullstroke_versions(client)
    pre = dict(sample)
    pre["calibration_bindings"] = {"command": v_cmd, "position": v_pos,
                                   "pressure": v_prs}
    _, pre_r = analyze(client, pre)

    post_sample = load_sample("sample_stiction_post")
    post = dict(post_sample)
    post["calibration_bindings"] = {"command": v_cmd, "position": v_pos,
                                    "pressure": v_prs}
    _, post_r = analyze(client, post)

    r = client.post("/pairings", json={"pre_analysis_id": pre_r["analysis_id"],
                                       "post_analysis_id": post_r["analysis_id"]})
    assert r.status_code == 201, r.text
    cc = r.json()["calibration_comparison"]
    assert cc["summary"]["n_channels"] == 3
    assert cc["summary"]["n_same_certificate"] == 3
    assert all(e["change"] in ("same", "recalibrated") for e in cc["channels"])
