"""阀杆推力签名测试（/stemthrust 路由组）端到端回归。

覆盖：正常单/双作用全行程的五类区段（启程、匀速、换向、离座、落座）与
正式诊断；填料高摩擦、启程卡涩、落座过压的超限判定；压力通道缺失、时间
无重叠、量纲冲突、校准失效、弹簧曲线不覆盖行程的证据缺口；人工调整
版本留痕；以及检修前后比较（兼容配对量化改善、结构不兼容列不可比原因、
含证据缺口版本不得抛错）。
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "thrust.db"))
    with TestClient(app) as c:
        yield c


def load_sample(name, **patch):
    payload = json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    for k, v in patch.items():
        payload[k] = v
    return payload


def submit_and_analyze(client, payload, author="test"):
    r = client.post("/stemthrust/tests", json=payload)
    assert r.status_code == 201, r.text
    test_id = r.json()["stemthrust_test_id"]
    r = client.post(f"/stemthrust/tests/{test_id}/analyze", json={"author": author})
    assert r.status_code == 201, r.text
    return test_id, r.json()


def phase(result, run, phase_name):
    return next(p for p in result["phases"]
                if p["run"] == run and p["phase"] == phase_name)


PASS_SAMPLES = ["sample_thrust_good", "sample_thrust_good2",
                "sample_thrust_post_overhaul"]


@pytest.mark.parametrize("name", PASS_SAMPLES)
def test_normal_full_stroke_five_phases_and_verdict(client, name):
    """正常单、双作用样例：五类区段齐全，无证据缺口，结论 pass。"""
    _, a = submit_and_analyze(client, load_sample(name))
    r = a["result"]
    assert r["verdict"] == "pass"
    assert r["evidence_gaps"] == []

    kinds = {(p["run"], p["phase"]) for p in r["phases"]}
    # 开方向：启程/离座/匀速；关方向：启程/匀速/落座；换向停留
    for required in [("opening", "breakaway"), ("opening", "unseat"),
                     ("opening", "running"), ("closing", "breakaway"),
                     ("closing", "running"), ("closing", "seating"),
                     ("reversal", "reversal")]:
        assert required in kinds, f"{name} 缺少区段 {required}"

    # 相位时序合理：启程结束（开始移动）→ 离座结束（离开关位带）→ 匀速
    brk = phase(r, "opening", "breakaway")
    uns = phase(r, "opening", "unseat")
    runn = phase(r, "opening", "running")
    assert brk["t_end"] <= uns["t_end"] <= runn["t_start"] + 1.0
    # 离座区间不得退化为零长度，启程不得错误延伸到匀速段
    assert uns["t_end"] - uns["t_start"] >= 0.15
    assert brk["t_end"] - brk["t_start"] <= 3.0
    # 启程终点阀位确实刚开始移动（未深入行程）
    assert brk["position_end_pct"] < 3.0
    # 落座发生在关位带内，落座力满足密封下限
    seat = phase(r, "closing", "seating")
    assert seat["position_start_pct"] <= r["thresholds"]["closed_band_pct"] + 0.5
    assert r["metrics"]["seating_close_n"] >= r["thresholds"]["seating_min_n"]
    assert all(c["pass"] for c in r["checks"])


def test_packing_high_friction_fail_body_suspect(client):
    _, a = submit_and_analyze(client, load_sample("sample_thrust_packing_high"))
    r = a["result"]
    assert r["verdict"] == "fail"
    assert r["evidence_gaps"] == []
    kinds = {i["kind"] for i in r["issues"]}
    assert "opening_running_friction_exceeded" in kinds
    assert "closing_running_friction_exceeded" in kinds
    assert all(i["suspected_source"] == "valve_body"
               for i in r["issues"] if "friction" in i["kind"])
    assert r["metrics"]["running_friction_open_n"] > \
        r["thresholds"]["running_friction_max_n"]


def test_sticky_breakaway_only_start_force_exceeded(client):
    """启程卡涩：启动力超限但匀速摩擦正常（区别于阀体填料卡涩）。"""
    _, a = submit_and_analyze(client, load_sample("sample_thrust_sticky_breakaway"))
    r = a["result"]
    assert r["verdict"] == "fail"
    assert [i["kind"] for i in r["issues"]] == ["opening_breakaway_force_exceeded"]
    assert r["metrics"]["breakaway_open_peak_n"] > \
        r["thresholds"]["breakaway_open_max_n"]
    assert r["metrics"]["running_friction_open_n"] <= \
        r["thresholds"]["running_friction_max_n"]
    # 离座力不应被启程附加力污染
    assert r["metrics"]["unseat_open_peak_n"] < \
        r["thresholds"]["unseat_open_max_n"]


def test_seating_overload(client):
    _, a = submit_and_analyze(client, load_sample("sample_thrust_seat_overload"))
    r = a["result"]
    assert r["verdict"] == "fail"
    assert [i["kind"] for i in r["issues"]] == ["seating_force_overload"]
    assert r["metrics"]["seating_close_n"] > r["thresholds"]["seating_max_n"]


GAP_SAMPLES = {
    "sample_thrust_missing_chamber": "pressure_channel_missing",
    "sample_thrust_no_overlap": "channel_no_overlap",
    "sample_thrust_unit_conflict": "unit_conflict",
    "sample_thrust_cal_expired": "calibration_expired",
    "sample_thrust_spring_short": "spring_curve_coverage",
}


@pytest.mark.parametrize("name", sorted(GAP_SAMPLES))
def test_evidence_gap_scenarios(client, name):
    _, a = submit_and_analyze(client, load_sample(name))
    r = a["result"]
    assert r["verdict"] == "no_conclusion"
    codes = {g["code"] for g in r["evidence_gaps"]}
    assert GAP_SAMPLES[name] in codes


def test_double_acting_requires_chamber_b(client):
    """双作用执行器缺 B 腔压力：证据缺口而非计算结果。"""
    payload = load_sample("sample_thrust_good2")
    payload["series"].pop("chamber_b_pressure")
    _, a = submit_and_analyze(client, payload)
    r = a["result"]
    assert r["verdict"] == "no_conclusion"
    assert any(g["code"] == "pressure_channel_missing" for g in r["evidence_gaps"])


def test_single_acting_requires_spring(client):
    payload = load_sample("sample_thrust_good")
    payload["actuator"].pop("spring")
    r = client.post("/stemthrust/tests", json=payload)
    assert r.status_code == 201, r.text
    tid = r.json()["stemthrust_test_id"]
    r = client.post(f"/stemthrust/tests/{tid}/analyze", json={"author": "t"})
    result = r.json()["result"]
    assert result["verdict"] == "no_conclusion"
    assert any(g["code"] == "spring_curve_missing"
               for g in result["evidence_gaps"])


def test_manual_adjust_creates_immutable_version(client):
    """移动相位边界/剔除坏点必须带理由，生成不可覆盖的新版本。"""
    tid, v1 = submit_and_analyze(client, load_sample("sample_thrust_good"))
    uns = phase(v1["result"], "opening", "unseat")
    new_t = round(uns["t_end"] + 0.2, 3)
    r = client.post(f"/stemthrust/analyses/{v1['id']}/adjust", json={
        "author": "reviewer-7",
        "segment_moves": [{"run": "opening", "phase": "unseat",
                           "boundary": "end", "new_time": new_t,
                           "reason": "慢放曲线复核，阀座实际脱离时刻偏晚"}],
    })
    assert r.status_code == 201, r.text
    v2 = r.json()
    assert v2["version"] == v1["version"] + 1
    assert v2["id"] != v1["id"]
    assert v2["adjustments"][-1]["reason"].startswith("慢放曲线")
    moved = phase(v2["result"], "opening", "unseat")
    assert abs(moved["t_end"] - new_t) < 0.1
    assert "人工调整" in moved["end_reason"]
    # 原版本不可覆盖
    got = client.get(f"/stemthrust/analyses/{v1['id']}").json()
    assert got["version"] == 1

    # 无理由的调整被拒绝
    r = client.post(f"/stemthrust/analyses/{v2['id']}/adjust", json={
        "author": "x",
        "segment_moves": [{"run": "opening", "phase": "unseat",
                           "boundary": "end", "new_time": new_t, "reason": "   "}],
    })
    assert r.status_code == 422


def test_pre_post_comparison_compatible_quantifies_improvement(client):
    _, pre = submit_and_analyze(
        client, load_sample("sample_thrust_packing_high"), author="pre")
    _, post = submit_and_analyze(
        client, load_sample("sample_thrust_post_overhaul"), author="post")
    r = client.post("/stemthrust/comparisons",
                    json={"analysis_ids": [pre["id"], post["id"]]})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["compatible"] is True
    assert all(e["comparable"] for e in d["series"])
    trends = {t["metric"]: t for t in d["trends"]}
    fr = trends["running_friction_open_n"]
    assert fr["status"] == "improving"
    assert fr["improvement_n"] > 100.0   # 约 900 → 230 N
    assert d["overall"] in ("improving", "mixed", "stable")


def test_comparison_incompatible_actuator_structure(client):
    """单作用 vs 双作用：结构不兼容，列出不可比原因，比较不中断。"""
    _, single = submit_and_analyze(client, load_sample("sample_thrust_good"))
    _, double = submit_and_analyze(client, load_sample("sample_thrust_good2"))
    r = client.post("/stemthrust/comparisons",
                    json={"analysis_ids": [single["id"], double["id"]]})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["compatible"] is False
    assert d["incomparable"]
    reasons = d["incomparable"][0]["reasons"]
    assert any("执行器结构不一致" in x for x in reasons)


def test_comparison_with_evidence_gap_does_not_raise(client):
    """回归：含证据缺口版本参与比较时不得抛 TypeError（set 交给 join）。"""
    _, good = submit_and_analyze(client, load_sample("sample_thrust_good"))
    gap_ids = {}
    for name in ("sample_thrust_missing_chamber", "sample_thrust_no_overlap",
                 "sample_thrust_cal_expired", "sample_thrust_spring_short"):
        _, a = submit_and_analyze(client, load_sample(name))
        gap_ids[name] = a["id"]
    for name, aid in gap_ids.items():
        r = client.post("/stemthrust/comparisons",
                        json={"analysis_ids": [good["id"], aid]})
        assert r.status_code == 201, f"{name}: {r.text}"
        d = r.json()
        entry = next(e for e in d["series"] if e["analysis_id"] == aid)
        assert entry["comparable"] is False
        assert any("证据缺口" in reason for reason in entry["reasons"])
        assert isinstance(entry["reasons"][0], str)


def test_export_and_report(client):
    _, a = submit_and_analyze(client, load_sample("sample_thrust_good"))
    r = client.get(f"/stemthrust/analyses/{a['id']}/export")
    assert r.status_code == 200
    assert r.json()["result"]["verdict"] == "pass"
    r = client.get(f"/stemthrust/analyses/{a['id']}/report")
    assert r.status_code == 200
    assert "阀杆推力签名" in r.text
