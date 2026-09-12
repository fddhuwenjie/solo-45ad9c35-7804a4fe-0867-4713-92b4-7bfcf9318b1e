"""全行程分析的测量不确定度评估测试。"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.uncertainty import compare_intervals, _conformance

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test_unc.db"))
    with TestClient(app) as c:
        yield c


def load_sample(name, **patch):
    payload = json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    for k, v in patch.items():
        payload[k] = v
    return payload


def submit(client, payload):
    r = client.post("/tests", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["test_id"]


def submit_bare(client, name, **patch):
    """提交不带不确定度声明的样例（用于未评估场景）。"""
    payload = load_sample(name, **patch)
    payload.pop("uncertainty", None)
    return submit(client, payload)


def analyze(client, test_id, unc=None, author="test"):
    body = {"author": author}
    if unc is not None:
        body["uncertainty"] = unc
    r = client.post(f"/tests/{test_id}/analyze", json=body)
    assert r.status_code == 201, r.text
    return r.json()


FULL_UNC = {
    "channels": {
        "command": {
            "resolution": {"kind": "resolution", "value": 0.1, "unit": "%"},
            "accuracy": {"kind": "accuracy", "value": 0.2, "unit": "%"},
            "time_jitter_s": 0.01,
        },
        "position": {
            "resolution": {"kind": "resolution", "value": 0.2, "unit": "%"},
            "accuracy": {"kind": "accuracy", "value": 0.3, "unit": "%"},
            "zero_drift": {"kind": "zero_drift", "value": 0.15, "unit": "%"},
            "time_jitter_s": 0.02,
        },
        "pressure": {
            "resolution": {"kind": "resolution", "value": 2.0, "unit": "kPa"},
            "time_jitter_s": 0.05,
        },
    },
    "calibration": {
        "value": 0.25, "unit": "%", "applies_to": ["command", "position"],
        "range_min": 0, "range_max": 100, "range_unit": "%",
    },
    "n_samples": 60,
    "seed": 42,
    "note": "实验室仪表声明",
}


# ---- 缺省：未评估但保留中心值 ----

def test_no_uncertainty_input_marks_not_evaluated(client):
    tid = submit_bare(client, "sample_normal")
    a = analyze(client, tid)
    u = a["result"]["uncertainty"]
    assert u["status"] == "not_evaluated"
    assert u["metrics"] == {}
    # 中心值结果仍在
    assert a["result"]["metrics"]["travel_time"]["max_s"] is not None


# ---- 评估主流程 ----

def test_evaluated_block_records_samples_seed_components(client):
    tid = submit(client, load_sample("sample_stiction"))
    a = analyze(client, tid, FULL_UNC)
    u = a["result"]["uncertainty"]
    assert u["status"] == "evaluated"
    assert u["n_samples_requested"] == 60
    assert u["n_samples_valid"] == 60
    assert u["n_samples_failed"] == 0
    assert u["seed"] == 42
    kinds = {(c["channel"], c["kind"]) for c in u["components"]}
    assert ("position", "resolution") in kinds
    assert ("position", "zero_drift") in kinds
    assert ("command", "time_jitter") in kinds
    assert ("pressure", "resolution") in kinds
    assert ("command", "calibration") in kinds
    rp = u["recompute_params"]
    assert rp["method"] == "monte_carlo"
    assert rp["n_samples"] == 60 and rp["seed"] == 42
    assert "perturb" in rp["pipeline"]


def test_intervals_have_ordered_bounds_and_central(client):
    tid = submit(client, load_sample("sample_stiction"))
    u = analyze(client, tid, FULL_UNC)["result"]["uncertainty"]
    for key, e in u["metrics"].items():
        if e["interval"] is not None:
            assert e["interval"][0] <= e["interval"][1]
            assert e["n_valid"] >= 30


def test_fixed_seed_reproducible(client):
    tid = submit(client, load_sample("sample_normal"))
    u1 = analyze(client, tid, FULL_UNC)["result"]["uncertainty"]
    u2 = analyze(client, tid, FULL_UNC)["result"]["uncertainty"]
    for key in u1["metrics"]:
        assert u1["metrics"][key]["interval"] == u2["metrics"][key]["interval"]


# ---- 区间跨阈值：indeterminate ----

# ---- 区间跨阈值：顶层 verdict / 打印页结论必须为 indeterminate ----

def test_straddling_interval_is_indeterminate_not_pass(client):
    """区间与阈值的相对位置与状态严格一致：跨阈值（含贴限）必须 indeterminate。"""
    tid = submit_bare(client, "sample_normal")
    unc = {
        "channels": {"position": {
            "resolution": {"kind": "resolution", "value": 2.0, "unit": "%"},
            "accuracy": {"kind": "accuracy", "value": 1.5, "unit": "%"}}},
        "n_samples": 60, "seed": 7,
    }
    u = analyze(client, tid, unc)["result"]["uncertainty"]
    thr_map = {"travel_time": "travel_time_s_max", "deadband": "deadband_pct_max",
               "hysteresis": "hysteresis_pct_max", "overshoot": "overshoot_pct_max",
               "steady_state": "steady_state_pct_max"}
    payload_thr = load_sample("sample_normal")["thresholds"]
    saw_straddle = False
    for key, e in u["metrics"].items():
        iv, limit = e["interval"], payload_thr[thr_map[key]]
        if iv is None:
            continue
        straddles = iv[0] <= limit <= iv[1]
        assert e["status"] == _conformance(iv, limit)
        if straddles:
            assert e["status"] == "indeterminate"
            saw_straddle = True
    assert saw_straddle, "大扰动下至少应有一个指标区间跨越限值"


def test_top_verdict_and_report_indeterminate_when_interval_straddles(client):
    """反例：中心值全部合格但死区区间跨越 2% 限值。

    verdict 必须从 ok 改为 indeterminate，打印页顶部结论同步显示“不确定”，
    不能继续显示 ok/“正常”。
    """
    tid = submit_bare(client, "sample_normal")
    unc = {
        "channels": {"position": {
            "resolution": {"kind": "resolution", "value": 1.6, "unit": "%"},
            "accuracy": {"kind": "accuracy", "value": 1.2, "unit": "%"}}},
        "n_samples": 80, "seed": 7,
    }
    a = analyze(client, tid, unc)
    res = a["result"]
    u = res["uncertainty"]
    # 中心值本身合格（否则不构成“只按中心值通过”的反例）
    assert res["metrics"]["deadband"]["max_pct"] < u["metrics"]["deadband"]["threshold"]
    # 死区区间跨越限值，总体符合性不确定，且没有任何指标整段 fail
    dead = u["metrics"]["deadband"]
    assert dead["interval"][0] <= dead["threshold"] <= dead["interval"][1]
    assert u["overall_status"] == "indeterminate"
    assert all(e["status"] != "fail" for e in u["metrics"].values() if e["interval"])
    # 顶层 verdict 必须与 overall_status 一致
    assert res["verdict"] == "indeterminate"
    assert any(i["kind"] == "uncertainty_indeterminate" for i in res["issues"])
    # 打印页顶部结论
    page = client.get(f"/analyses/{a['id']}/report").text
    assert "符合性不确定" in page
    assert '结论</b>：<span class="verdict">正常</span>' not in page
    # JSON 导出同样为 indeterminate
    exp = client.get(f"/analyses/{a['id']}/export").json()
    assert exp["result"]["verdict"] == "indeterminate"


def test_small_uncertainty_keeps_verdict_ok(client):
    """对照：不确定度足够小时区间整体在限内，verdict 仍为 ok。"""
    tid = submit_bare(client, "sample_normal")
    unc = {"channels": {"position": {
        "resolution": {"kind": "resolution", "value": 0.2, "unit": "%"},
        "accuracy": {"kind": "accuracy", "value": 0.1, "unit": "%"}}},
        "n_samples": 60, "seed": 7}
    res = analyze(client, tid, unc)["result"]
    assert res["uncertainty"]["overall_status"] == "pass"
    assert res["verdict"] == "ok"


def test_verdict_indeterminate_even_with_existing_exceedances(client):
    """组合反例：已有超限事件（sample_dropout 两条 dropout issue），
    同时死区区间 [0.0, 2.667] 跨越 2.0 阈值。

    取消“仅原 verdict 为 ok 才同步”的限制后：overall_status=indeterminate
    必须把顶层 verdict 从 exceedances 改为 indeterminate，打印页顶部结论
    显示“符合性不确定”，原有 dropout issue 仍保留在问题清单中。
    """
    tid = submit_bare(client, "sample_dropout")
    unc = {
        "channels": {"position": {
            "resolution": {"kind": "resolution", "value": 1.6, "unit": "%"},
            "accuracy": {"kind": "accuracy", "value": 1.2, "unit": "%"}}},
        "n_samples": 80, "seed": 7,
    }
    a = analyze(client, tid, unc)
    res = a["result"]
    u = res["uncertainty"]
    # 前置条件：两条信号断档 issue 与中心值 exceedances
    dropout_issues = [i for i in res["issues"] if i["kind"] == "dropout"]
    assert len(dropout_issues) == 2
    # 死区区间跨越阈值，总体不确定
    dead = u["metrics"]["deadband"]
    assert dead["interval"][0] <= dead["threshold"] <= dead["interval"][1]
    assert dead["status"] == "indeterminate"
    assert u["overall_status"] == "indeterminate"
    # 顶层 verdict 必须跟随 overall_status（不能停留在 exceedances/ok）
    assert res["verdict"] == "indeterminate"
    kinds = [i["kind"] for i in res["issues"]]
    assert "uncertainty_indeterminate" in kinds
    # 原有断档事件不得被吞掉
    assert kinds.count("dropout") == 2
    # 打印页顶部结论
    page = client.get(f"/analyses/{a['id']}/report").text
    assert "符合性不确定" in page
    assert '<span class="verdict">存在超限</span>' not in page
    assert '<span class="verdict">正常</span>' not in page
    # JSON 导出一致
    exp = client.get(f"/analyses/{a['id']}/export").json()
    assert exp["result"]["verdict"] == "indeterminate"
    assert sum(1 for i in exp["result"]["issues"] if i["kind"] == "dropout") == 2


# ---- resolution 均匀扰动半宽 = 声明量化步进 / 2 ----

def test_resolution_halfwidth_is_value_over_two(client):
    """resolution=0.2%（量化步进）必须产生 ±0.1%（value/2）扰动，而非 ±0.2%。"""
    tid = submit_bare(client, "sample_normal")
    unc = {"channels": {"position": {
        "resolution": {"kind": "resolution", "value": 0.2, "unit": "%"}}},
        "n_samples": 40, "seed": 1}
    u = analyze(client, tid, unc)["result"]["uncertainty"]
    comp = next(c for c in u["components"]
                if c["channel"] == "position" and c["kind"] == "resolution")
    assert comp["normalized_scale"] == 0.2       # 声明量化步进
    assert comp["perturbation_halfwidth"] == 0.1  # 实际均匀扰动半宽 = value/2
    assert comp["distribution"] == "rectangular"


def test_resolution_perturbation_bounded_by_half_step():
    """逐点扰动幅度不得超过量化步进的一半（±value/2）。"""
    import random
    from app.uncertainty import _perturb_channel
    cin = {"components": [{"kind": "resolution", "scale": 0.1,
                           "distribution": "rectangular", "per_point": True}],
           "time_jitter_s": 0.0}
    ts = [float(i) for i in range(200)]
    vs = [50.0] * 200
    rng = random.Random(123)
    for _ in range(50):
        _, perturbed = _perturb_channel(ts, vs, cin, rng)
        assert max(abs(v - 50.0) for v in perturbed) <= 0.1 + 1e-12


def test_resolution_halfwidth_avoids_inflated_intervals(client):
    """反例：同样 0.2% 分辨率声明，修正后的区间不得宽于旧实现（±0.2%）。

    用固定种子对比死区区间宽度上界：半宽折半后所有指标区间应收敛在更窄范围。
    """
    tid = submit_bare(client, "sample_normal")
    unc_half = {"channels": {"position": {
        "resolution": {"kind": "resolution", "value": 0.2, "unit": "%"}}},
        "n_samples": 80, "seed": 7}
    u = analyze(client, tid, unc_half)["result"]["uncertainty"]
    over = u["metrics"]["overshoot"]["interval"]
    # ±0.1% 点间扰动下，过冲区间宽度不应超过 0.3%（旧 ±0.2% 实现会更宽）
    assert over[1] - over[0] < 0.3


def test_conformance_boundary_touch_is_indeterminate():
    assert _conformance([1.0, 2.0], 2.0) == "indeterminate"   # 上端贴限
    assert _conformance([2.0, 3.0], 2.0) == "indeterminate"   # 下端贴限
    assert _conformance([0.5, 1.5], 2.0) == "pass"
    assert _conformance([2.5, 3.0], 2.0) == "fail"


# ---- 无效输入：校准范围 / 单位冲突 ----

def test_calibration_range_not_covering_lists_reason(client):
    tid = submit(client, load_sample("sample_normal"))
    unc = {
        "channels": {"position": {"resolution": {"kind": "resolution", "value": 0.2, "unit": "%"}}},
        "calibration": {"value": 0.1, "unit": "%", "applies_to": ["position"],
                        "range_min": 0, "range_max": 50, "range_unit": "%"},
        "n_samples": 40,
    }
    u = analyze(client, tid, unc)["result"]["uncertainty"]
    assert u["status"] == "invalid"
    assert any("校准范围不覆盖" in r for r in u["reasons"])


def test_calibration_range_physical_units(client):
    """mm 校准范围覆盖 [0,50]mm、观测 0-50mm → 有效。"""
    payload = load_sample("sample_normal")
    payload["range"] = {"min": 0.0, "max": 50.0, "unit": "mm"}
    payload["series"]["command"]["unit"] = "mm"
    payload["series"]["position"]["unit"] = "mm"
    for ch in ("command", "position"):
        payload["series"][ch]["points"] = [
            [t, v / 100.0 * 50.0] for t, v in payload["series"][ch]["points"]]
    tid = submit(client, payload)
    unc = {
        "channels": {"position": {"resolution": {"kind": "resolution", "value": 0.1, "unit": "mm"}}},
        "calibration": {"value": 0.12, "unit": "mm", "applies_to": ["position"],
                        "range_min": 0, "range_max": 50, "range_unit": "mm"},
        "n_samples": 40,
    }
    u = analyze(client, tid, unc)["result"]["uncertainty"]
    assert u["status"] == "evaluated", u.get("reasons")


def test_component_unit_conflict_lists_reason(client):
    tid = submit(client, load_sample("sample_normal"))
    # 阀位通道给压力单位 bar
    unc = {"channels": {"position": {"resolution": {"kind": "resolution", "value": 0.2, "unit": "bar"}}},
           "n_samples": 40}
    u = analyze(client, tid, unc)["result"]["uncertainty"]
    assert u["status"] == "invalid"
    assert any("单位冲突" in r for r in u["reasons"])
    # 时间单位不能给值分量
    unc2 = {"channels": {"position": {"accuracy": {"kind": "accuracy", "value": 0.2, "unit": "s"}}},
            "n_samples": 40}
    u2 = analyze(client, tid, unc2)["result"]["uncertainty"]
    assert u2["status"] == "invalid"


def test_pressure_channel_accepts_pressure_units_only(client):
    tid = submit(client, load_sample("sample_normal"))
    unc = {"channels": {"pressure": {"resolution": {"kind": "resolution", "value": 0.02, "unit": "bar"}}},
           "n_samples": 40}
    u = analyze(client, tid, unc)["result"]["uncertainty"]
    assert u["status"] == "evaluated"
    comp = next(c for c in u["components"] if c["channel"] == "pressure")
    assert comp["normalized_unit"] == "kPa"
    assert comp["normalized_scale"] == pytest.approx(2.0)


# ---- 提交级输入与 analyze 覆盖 ----

def test_submission_level_uncertainty_used(client):
    payload = load_sample("sample_normal")
    payload["uncertainty"] = {
        "channels": {"position": {"resolution": {"kind": "resolution", "value": 0.2, "unit": "%"}}},
        "n_samples": 40, "seed": 3}
    tid = submit(client, payload)
    u = analyze(client, tid)["result"]["uncertainty"]
    assert u["status"] == "evaluated"
    assert u["seed"] == 3


def test_analyze_uncertainty_overrides_submission(client):
    payload = load_sample("sample_normal")
    payload["uncertainty"] = {
        "channels": {"position": {"resolution": {"kind": "resolution", "value": 0.2, "unit": "%"}}},
        "n_samples": 40, "seed": 3}
    tid = submit(client, payload)
    u = analyze(client, tid, {"n_samples": 30, "seed": 9,
                              "channels": {"position": {"accuracy": {"kind": "accuracy", "value": 0.1, "unit": "%"}}}})
    block = u["result"]["uncertainty"]
    assert block["seed"] == 9 and block["n_samples_requested"] == 30


# ---- 人工调整后另建不确定度版本，区间随版本保存 ----

def test_adjust_creates_separate_uncertainty_version(client):
    tid = submit(client, load_sample("sample_normal"))
    a1 = analyze(client, tid, FULL_UNC)
    opening = next(s for s in a1["result"]["segments"] if s["type"] == "opening")
    r = client.post(f"/analyses/{a1['id']}/adjust", json={
        "author": "tech-01",
        "boundary_moves": [{"segment_index": opening["index"], "boundary": "start",
                            "new_time": opening["t_start"] + 0.2,
                            "reason": "起点前段为记录噪声"}],
    })
    assert r.status_code == 201, r.text
    a2 = r.json()
    assert a2["version"] == a1["version"] + 1
    u2 = a2["result"]["uncertainty"]
    assert u2["status"] == "evaluated"          # 沿用上一版本的不确定度输入
    assert u2["seed"] == FULL_UNC["seed"]
    # 复算参数记录了人工边界
    mb = u2["recompute_params"]["manual_boundaries"]
    assert len(mb) == 1 and mb[0]["reason"] == "起点前段为记录噪声"
    # 旧版本区间未被覆盖
    r = client.get(f"/analyses/{a1['id']}")
    u1 = r.json()["result"]["uncertainty"]
    assert u1["recompute_params"]["manual_boundaries"] == []


def test_adjust_can_supply_new_uncertainty(client):
    tid = submit(client, load_sample("sample_normal"))
    a1 = analyze(client, tid, FULL_UNC)
    opening = next(s for s in a1["result"]["segments"] if s["type"] == "opening")
    r = client.post(f"/analyses/{a1['id']}/adjust", json={
        "author": "tech-02",
        "boundary_moves": [{"segment_index": opening["index"], "boundary": "start",
                            "new_time": opening["t_start"] + 0.15,
                            "reason": "复核用新仪表重新评估"}],
        "uncertainty": {"n_samples": 25, "seed": 100,
                        "channels": {"position": {"resolution": {"kind": "resolution", "value": 0.5, "unit": "%"}}}},
    })
    assert r.status_code == 201, r.text
    u2 = r.json()["result"]["uncertainty"]
    assert u2["seed"] == 100 and u2["n_samples_requested"] == 25


# ---- 配对：差值区间越零才判定改善/退化 ----

def _pair(client, pre_payload, post_payload, unc=FULL_UNC):
    pre = analyze(client, submit(client, pre_payload), unc)
    post = analyze(client, submit(client, post_payload), unc)
    r = client.post("/pairings",
                    json={"pre_analysis_id": pre["id"], "post_analysis_id": post["id"]})
    assert r.status_code == 201, r.text
    return r.json()


def test_pairing_delta_intervals(client):
    p = _pair(client, load_sample("sample_stiction"), load_sample("sample_stiction_post"))
    by = {e["metric"]: e for e in p["uncertainty_comparison"]}
    # 死区检修后明显变小：差值区间整体大于零
    assert by["deadband"]["change"] == "improved"
    assert by["deadband"]["delta_interval"][0] > 0
    assert by["deadband"]["post_conformance"] == "pass"
    imp = {i["metric"]: i for i in p["improvements"]}
    assert imp["deadband_pct"]["uncertainty_change"] == "improved"
    assert imp["deadband_pct"]["within_threshold"] is True


def test_pairing_unevaluated_side_not_judged(client):
    pre = analyze(client, submit(client, load_sample("sample_stiction")), FULL_UNC)
    post = analyze(client, submit_bare(client, "sample_stiction_post"), None)
    r = client.post("/pairings",
                    json={"pre_analysis_id": pre["id"], "post_analysis_id": post["id"]})
    p = r.json()
    assert p["compatible"] is True
    for e in p["uncertainty_comparison"]:
        assert e["change"] == "not_evaluated"
        assert e["delta_interval"] is None


def test_compare_intervals_crossing_zero_indeterminate():
    pre = {"status": "evaluated", "metrics": {
        "deadband": {"interval": [1.0, 3.0]}}}
    post = {"status": "evaluated", "metrics": {
        "deadband": {"interval": [1.5, 2.5], "status": "indeterminate"}}}
    out = {e["metric"]: e for e in compare_intervals(pre, post)}
    d = out["deadband"]
    lo, hi = d["delta_interval"]
    assert lo < 0 < hi
    assert d["change"] == "indeterminate"


# ---- 阻断版本：区间仅供参考，不得用于维修结论 ----

def test_blocking_version_marks_uncertainty_indeterminate(client):
    payload = load_sample("sample_normal",
                          calibration_valid_until="2020-01-01T00:00:00+00:00")
    tid = submit(client, payload)
    unc = {"channels": {"position": {"resolution": {"kind": "resolution", "value": 0.2, "unit": "%"}}},
           "n_samples": 30}
    a = analyze(client, tid, unc)
    assert a["result"]["verdict"] == "no_conclusion"
    u = a["result"]["uncertainty"]
    assert u["status"] == "evaluated"
    assert u["overall_status"] == "indeterminate"
    assert "阻断" in u["blocked_note"]


# ---- JSON 导出与打印页共用同一组区间 ----
def test_export_and_report_share_intervals(client):
    tid = submit(client, load_sample("sample_stiction"))
    a = analyze(client, tid, FULL_UNC)
    exp = client.get(f"/analyses/{a['id']}/export").json()
    assert exp["result"]["uncertainty"]["seed"] == 42
    exp_iv = exp["result"]["uncertainty"]["metrics"]["deadband"]["interval"]
    page = client.get(f"/analyses/{a['id']}/report").text
    assert f"[{exp_iv[0]}, {exp_iv[1]}]" in page
    assert "测量不确定度" in page
    assert "固定种子 42" in page


def test_report_not_evaluated_notice(client):
    tid = submit_bare(client, "sample_normal")
    a = analyze(client, tid)
    page = client.get(f"/analyses/{a['id']}/report").text
    assert "未评估" in page


# ---- 有效重采样不足 ----

def test_insufficient_valid_resamples(monkeypatch):
    """模拟一半重采样对齐失败：状态 evaluated 但总体 indeterminate 并列出原因。"""
    from app import uncertainty as unc
    from app.processing import units

    orig_align = unc._fast_align
    calls = {"n": 0}

    def flaky(series):
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            raise ValueError("各通道时间轴无重叠区间，无法对齐")
        return orig_align(series)

    monkeypatch.setattr(unc, "_fast_align", flaky)
    series = {
        "command": {"unit": "%", "points": [[0, 10], [1, 10], [2, 90], [3, 90], [4, 10], [5, 10]]},
        "position": {"unit": "%", "points": [[0, 10], [1, 10.1], [2, 80], [3, 90], [4, 20], [5, 10]]},
        "pressure": {"unit": "kPa", "points": [[0, 400], [1, 400], [2, 350], [3, 400], [4, 350], [5, 400]]},
    }
    norm = units.normalize_series(series, 0, 100, "%")
    raw = {c: norm[c] for c in ("command", "position", "pressure")}
    spec = {"channels": {"position": {"resolution": {"kind": "resolution", "value": 0.2, "unit": "%"}}},
            "n_samples": 40, "seed": 1}
    res = unc.evaluate(raw, {c: "%" for c in raw}, {"min": 0, "max": 100},
                       {"settle_band_pct": 2.0, "supply_pressure_min_kpa": 300}, [], spec)
    assert res["n_samples_failed"] == 20
    assert res["n_samples_valid"] == 20
    assert res["overall_status"] == "indeterminate"
    assert any("有效重采样不足" in r for r in res["reasons"])
