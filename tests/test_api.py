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


def load_sample(name, **patch):
    payload = json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    for k, v in patch.items():
        payload[k] = v
    return payload


def submit_and_analyze(client, payload, author="test"):
    r = client.post("/tests", json=payload)
    assert r.status_code == 201, r.text
    test_id = r.json()["test_id"]
    r = client.post(f"/tests/{test_id}/analyze", json={"author": author})
    assert r.status_code == 201, r.text
    return test_id, r.json()


# ---- 基础流程 ----

def test_normal_sample_ok(client):
    _, a = submit_and_analyze(client, load_sample("sample_normal"))
    r = a["result"]
    assert r["verdict"] == "ok"
    types = [s["type"] for s in r["segments"]]
    assert "opening" in types and "closing" in types and "dwell" in types
    # 自动分段必须给出边界依据
    for s in r["segments"]:
        assert s["start_reason"] and s["end_reason"]
    assert r["metrics"]["travel_time"]["max_s"] < r["thresholds"]["travel_time_s_max"]


def test_stiction_sample_detected(client):
    _, a = submit_and_analyze(client, load_sample("sample_stiction"))
    r = a["result"]
    assert r["verdict"] == "exceedances"
    assert len(r["detections"]["stick_slip"]) > 0
    assert r["metrics"]["deadband"]["max_pct"] > r["thresholds"]["deadband_pct_max"]
    assert r["metrics"]["hysteresis"]["max_pct"] > r["thresholds"]["hysteresis_pct_max"]
    assert len(r["detections"]["reverse_hysteresis"]) > 0


def test_dropout_sample_detected(client):
    _, a = submit_and_analyze(client, load_sample("sample_dropout"))
    kinds = {(d["channel"], d["kind"]) for d in a["result"]["detections"]["dropouts"]}
    assert ("position", "gap") in kinds
    assert ("position", "frozen") in kinds
    assert a["result"]["verdict"] == "exceedances"


# ---- 人工调整与版本 ----

def test_adjust_creates_new_version(client):
    test_id, a1 = submit_and_analyze(client, load_sample("sample_normal"))
    opening = next(s for s in a1["result"]["segments"] if s["type"] == "opening")
    r = client.post(f"/analyses/{a1['id']}/adjust", json={
        "author": "tech-01",
        "boundary_moves": [{"segment_index": opening["index"], "boundary": "start",
                            "new_time": opening["t_start"] + 0.2,
                            "reason": "起点前段为记录噪声"}],
        "exclusions": [{"channel": "position", "start_index": 10, "end_index": 12,
                        "reason": "手持终端干扰跳点"}],
    })
    assert r.status_code == 201, r.text
    a2 = r.json()
    assert a2["version"] == a1["version"] + 1
    # 剔除点保留原始引用与理由
    ex = a2["result"]["exclusions"][0]
    assert ex["reason"] == "手持终端干扰跳点"
    assert len(ex["original_points"]) == 3
    assert ex["original_points"][0]["index"] == 10
    # 边界调整留下人工依据
    seg2 = next(s for s in a2["result"]["segments"] if s["index"] == opening["index"])
    assert "人工调整" in seg2["start_reason"]
    # 旧版本仍可追溯
    r = client.get(f"/tests/{test_id}/analyses")
    assert [x["version"] for x in r.json()] == [1, 2]


# ---- 配对 ----

def test_pairing_compatible(client):
    _, pre = submit_and_analyze(client, load_sample("sample_stiction"))
    _, post = submit_and_analyze(client, load_sample("sample_stiction_post"))
    r = client.post("/pairings", json={"pre_analysis_id": pre["id"],
                                       "post_analysis_id": post["id"]})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["compatible"] is True
    imp = {i["metric"]: i for i in p["improvements"]}
    assert imp["deadband_pct"]["delta"] > 0          # 死区改善
    assert imp["deadband_pct"]["within_threshold"]   # 检修后回到阈值内
    assert imp["stick_slip_count"]["post"] == 0      # 卡跳消除
    assert p["still_exceeding"] == []


def test_pairing_incompatible_load(client):
    _, pre = submit_and_analyze(client, load_sample("sample_stiction"))
    post_payload = load_sample("sample_stiction_post")
    post_payload["conditions"]["load"] = "online"   # 负载条件不同
    _, post = submit_and_analyze(client, post_payload)
    r = client.post("/pairings", json={"pre_analysis_id": pre["id"],
                                       "post_analysis_id": post["id"]})
    p = r.json()
    assert p["compatible"] is False
    assert any("负载条件" in reason for reason in p["incompatible_reasons"])
    assert p["improvements"] == []


# ---- 阻断条件 ----

def test_expired_calibration_blocks_conclusion(client):
    payload = load_sample("sample_normal",
                          calibration_valid_until="2020-01-01T00:00:00+00:00")
    _, a = submit_and_analyze(client, payload)
    r = a["result"]
    assert r["verdict"] == "no_conclusion"
    assert any("校准" in b for b in r["blocking_issues"])


def test_unit_conflict_blocks_conclusion(client):
    payload = load_sample("sample_normal")
    payload["series"]["command"]["unit"] = "mA"     # 值仍是 0-100，与 mA 冲突
    _, a = submit_and_analyze(client, payload)
    r = a["result"]
    assert r["verdict"] == "no_conclusion"
    assert any("单位冲突" in b for b in r["blocking_issues"])


def test_incomplete_key_segment_blocks(client):
    payload = load_sample("sample_normal")
    for ch in payload["series"].values():           # 截断到 20s，只有开阀没有关阀
        ch["points"] = [p for p in ch["points"] if p[0] <= 20.0]
    _, a = submit_and_analyze(client, payload)
    r = a["result"]
    assert r["verdict"] == "no_conclusion"
    assert any("关键区段" in b for b in r["blocking_issues"])


def test_no_conclusion_analysis_cannot_pair(client):
    _, pre = submit_and_analyze(client, load_sample("sample_stiction"))
    bad = load_sample("sample_stiction_post",
                      calibration_valid_until="2020-01-01T00:00:00+00:00")
    _, post = submit_and_analyze(client, bad)
    assert post["result"]["verdict"] == "no_conclusion"
    r = client.post("/pairings", json={"pre_analysis_id": pre["id"],
                                       "post_analysis_id": post["id"]})
    p = r.json()
    assert p["compatible"] is False
    assert any("阻断" in reason for reason in p["incompatible_reasons"])


# ---- 导出与报告 ----

def test_export_and_report(client):
    _, a = submit_and_analyze(client, load_sample("sample_stiction"))
    r = client.get(f"/analyses/{a['id']}/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "content-disposition" in r.headers
    assert r.json()["result"]["version"] == a["version"]

    r = client.get(f"/analyses/{a['id']}/report")
    assert r.status_code == 200
    body = r.text
    assert "<svg" in body                       # 曲线
    assert "供压下限" in body                    # 阈值标注
    assert f"v{a['version']}" in body            # 分析版本
    assert "卡跳" in body                        # 检测事件


# ---- 样例端点 ----

def test_samples_endpoint_roundtrip(client):
    r = client.get("/samples")
    names = r.json()
    assert {"sample_stiction", "sample_dropout", "sample_normal"} <= set(names)
    for name in ("sample_stiction", "sample_dropout", "sample_normal"):
        r = client.get(f"/samples/{name}")
        assert r.status_code == 200
        submit = client.post("/tests", json=r.json())
        assert submit.status_code == 201
