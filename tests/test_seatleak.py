"""气体阀座密封保持试验（/seatleak 路由组）端到端回归。

每次测试使用独立临时数据库，覆盖整组路由：提交 → 分析 → 人工调整（版本
留痕）→ 多次试验比较 → JSON 导出 → 打印报告。

场景矩阵：现有样例覆盖密封正常、内漏、温压回升补偿、流量计交叉核对与五类
证据缺口；新增 downstream_to_upstream 反向流动样例（下游高压封闭容积降压），
逐项断言泄漏率、累计漏量、首次超限与判定状态。
"""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "seatleak.db"))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def client_and_db(tmp_path):
    db_path = str(tmp_path / "seatleak.db")
    app = create_app(db_path)
    with TestClient(app) as c:
        yield c, db_path


def load_sample(name, **patch):
    payload = json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    for k, v in patch.items():
        payload[k] = v
    return payload


def submit_and_analyze(client, payload, author="test"):
    r = client.post("/seatleak/tests", json=payload)
    assert r.status_code == 201, r.text
    test_id = r.json()["seatleak_test_id"]
    r = client.post(f"/seatleak/tests/{test_id}/analyze", json={"author": author})
    assert r.status_code == 201, r.text
    return test_id, r.json()


def hold_segment(result):
    return next(s for s in result["segments"] if s["type"] == "hold")


# ---- 场景矩阵：判定 / 泄漏率 / 累计漏量 / 首次超限 ----

# 样例 → (判定, 泄漏率均值 Nl/min, 累计漏量 Nl, 首次超限类型或 None)
CONCLUSIVE = {
    "sample_seatleak_tight": ("pass", 0.0205, 0.0443, None),
    "sample_seatleak_tight2": ("pass", 0.0253, 0.0598, None),
    "sample_seatleak_leaking": ("fail", 0.3003, 0.7066, "leak_rate"),
    "sample_seatleak_thermal_recovery": ("pass", 0.0097, 0.0294, None),
    "sample_seatleak_flow_checked": ("pass", 0.0298, 0.0665, None),
    "sample_seatleak_reverse_leaking": ("fail", 0.3009, 0.7129, "leak_rate"),
}


@pytest.mark.parametrize("name", sorted(CONCLUSIVE))
def test_conclusive_scenarios(client, name):
    verdict, leak, cum, first_kind = CONCLUSIVE[name]
    _, a = submit_and_analyze(client, load_sample(name))
    r = a["result"]
    m = r["metrics"]
    thr = r["thresholds"]
    assert r["verdict"] == verdict
    assert r["evidence_gaps"] == []
    # 泄漏率与累计漏量
    assert m["leak_rate_mean_nl_min"] == pytest.approx(leak, abs=2e-3)
    assert m["cumulative_leak_nl"] == pytest.approx(cum, abs=5e-3)
    chk = {c["metric"]: c for c in r["checks"]}
    if verdict == "pass":
        assert m["leak_rate_mean_nl_min"] <= thr["leak_rate_max_nl_min"]
        assert m["cumulative_leak_nl"] <= thr["cumulative_leak_max_nl"]
        assert m["first_exceedance"] is None
        assert all(c["pass"] for c in r["checks"])
        assert r["issues"] == []
        assert any("未出现" in b for b in r["decision_basis"])
    else:
        assert m["leak_rate_mean_nl_min"] > thr["leak_rate_max_nl_min"]
        assert m["cumulative_leak_nl"] > thr["cumulative_leak_max_nl"]
        assert chk["leak_rate_mean_nl_min"]["pass"] is False
        assert chk["cumulative_leak_nl"]["pass"] is False
        # 首次超限：保持段起点附近泄漏率持续超限
        fe = m["first_exceedance"]
        assert fe["kind"] == first_kind
        assert fe["t_s"] == pytest.approx(43.7, abs=0.3)
        assert fe["value"] > fe["limit"]
        assert {i["kind"] for i in r["issues"]} == {
            "leak_rate_exceeded", "cumulative_leak_exceeded"}
        assert any("首次超限" in b for b in r["decision_basis"])


# 五类证据缺口样例 → (缺口代码, 详情关键词)
EVIDENCE_GAPS = {
    "sample_seatleak_temp_dropout": ("temperature_dropout", "温度"),
    "sample_seatleak_bad_sequence": ("isolation_sequence_conflict", "次序"),
    "sample_seatleak_low_dp": ("insufficient_differential_pressure", "压差"),
    "sample_seatleak_unstable_position": ("closed_position_unstable", "关位"),
    "sample_seatleak_blank_uncovered": ("blank_baseline_uncovered", "空白"),
}


@pytest.mark.parametrize("name", sorted(EVIDENCE_GAPS))
def test_evidence_gap_scenarios(client, name):
    code, needle = EVIDENCE_GAPS[name]
    _, a = submit_and_analyze(client, load_sample(name))
    r = a["result"]
    assert r["verdict"] == "no_conclusion"
    gaps = {g["code"]: g["detail"] for g in r["evidence_gaps"]}
    assert code in gaps
    assert needle in gaps[code]
    # 指标照常计算并留痕，但不得给出合格结论
    assert r["metrics"]["leak_rate_mean_nl_min"] is not None
    assert r["metrics"]["cumulative_leak_nl"] is not None
    assert r["metrics"]["first_exceedance"] is None


# ---- 反向流动（downstream_to_upstream） ----

def test_reverse_direction_flow(client):
    """下游高压封闭容积向上游内漏：压力/等效标准体积下降，按流向符号换算后判超限。"""
    _, a = submit_and_analyze(client, load_sample("sample_seatleak_reverse_leaking"))
    r = a["result"]
    m = r["metrics"]
    assert r["flow_direction"] == "downstream_to_upstream"
    assert r["verdict"] == "fail"
    # 反向：封闭容积（下游侧）降压，等效标准体积下降
    assert m["p_down_end_kpa"] < m["p_down_start_kpa"]
    assert m["veq_end_nl"] < m["veq_start_nl"]
    # 有效压差按流向符号换算后仍为正且满足下限
    assert m["differential_median_kpa"] == pytest.approx(346.3, abs=1.0)
    assert m["differential_median_kpa"] >= r["thresholds"]["min_differential_kpa"]
    # 补偿参数记录流向符号
    assert r["compensation"]["parameters"]["direction_sign"] == -1.0
    # 泄漏率 / 累计漏量 / 首次超限
    assert m["leak_rate_mean_nl_min"] == pytest.approx(0.3009, abs=2e-3)
    assert m["cumulative_leak_nl"] == pytest.approx(0.7129, abs=5e-3)
    fe = m["first_exceedance"]
    assert fe["kind"] == "leak_rate"
    assert fe["t_s"] == pytest.approx(43.7, abs=0.3)
    # 报告呈现反向流向与补偿依据
    body = client.get(f"/seatleak/analyses/{a['id']}/report").text
    assert "下游→上游（下游降压）" in body
    assert "方向符号 -1" in body


def test_flow_direction_schema_validation(client):
    """流向为枚举值，非法声明在提交时即被拒绝。"""
    p = load_sample("sample_seatleak_tight", flow_direction="sideways")
    r = client.post("/seatleak/tests", json=p)
    assert r.status_code == 422


# ---- 温压回升补偿与流量计交叉核对 ----

def test_thermal_recovery_compensated(client):
    """保持段温度回升使表观压力上升，温压补偿后不得误判为内漏。"""
    _, a = submit_and_analyze(client, load_sample("sample_seatleak_thermal_recovery"))
    r = a["result"]
    m = r["metrics"]
    # 表观：保持段下游压力随温度回升而上升
    assert m["p_down_end_kpa"] > m["p_down_start_kpa"]
    assert m["temp_mean_hold_c"] > 24.5
    assert r["curves"]["downstream_temp"][-1] > r["curves"]["downstream_temp"][0] + 1.0
    # 补偿后真实泄漏远低于限值 → 合格
    assert r["verdict"] == "pass"
    assert m["leak_rate_mean_nl_min"] == pytest.approx(0.0097, abs=2e-3)
    # 补偿与空白扣除过程留痕
    adopted = {ai["name"]: ai for ai in r["adopted_intervals"]}
    assert "blank_correction" in adopted
    assert "已从净漏量扣除" in adopted["blank_correction"]["method"]
    assert "leak_rate_evaluation" in adopted


def test_flow_meter_crosscheck(client):
    """带流量计的试验：流量计与质量平衡互相核对，偏差来源留痕。"""
    _, a = submit_and_analyze(client, load_sample("sample_seatleak_flow_checked"))
    r = a["result"]
    fc = r["flow_crosscheck"]
    assert fc["flow_mean_nl_min"] == pytest.approx(0.0305, abs=2e-3)
    assert fc["mass_balance_nl_min"] == pytest.approx(0.0298, abs=2e-3)
    assert fc["deviation_pct"] == pytest.approx(0.99, abs=0.3)
    assert fc["deviation_pct"] <= fc["deviation_limit_pct"]
    assert fc["deviation_sources"]            # 偏差来源说明非空
    chk = next(c for c in r["checks"] if c["metric"] == "flow_deviation_pct")
    assert chk["pass"] is True
    assert any(ai["name"] == "flow_crosscheck" for ai in r["adopted_intervals"])
    assert "flow_nl_min" in r["curves"]
    body = client.get(f"/seatleak/analyses/{a['id']}/report").text
    assert "流量计均值" in body
    assert "偏差来源" in body


# ---- 人工调整与版本留痕 ----

def test_adjust_creates_version_and_keeps_raw_refs(client):
    test_id, a1 = submit_and_analyze(client, load_sample("sample_seatleak_tight"))
    r1 = a1["result"]
    hold = hold_segment(r1)
    r = client.post(f"/seatleak/analyses/{a1['id']}/adjust", json={
        "author": "tech-01",
        "segment_moves": [{"segment": "hold", "boundary": "start",
                           "new_time": hold["t_start"] + 10.0,
                           "reason": "前段温度仍有漂移，推迟保持段起点"}],
        "exclusions": [{"channel": "downstream_temp", "start_index": 5, "end_index": 7,
                        "reason": "温度探头瞬时干扰"}],
    })
    assert r.status_code == 201, r.text
    a2 = r.json()
    assert a2["version"] == a1["version"] + 1
    assert a2["id"] != a1["id"]
    # 版本留痕：调整含作者与理由
    adj = a2["adjustments"]
    assert len(adj) == 2
    assert all(x["author"] == "tech-01" for x in adj)
    move = next(x for x in adj if x["type"] == "segment_move")
    assert move["reason"] == "前段温度仍有漂移，推迟保持段起点"
    # 边界移动生效并保留人工依据与原始点引用
    hold2 = hold_segment(a2["result"])
    assert "人工调整" in hold2["start_reason"]
    assert "前段温度仍有漂移" in hold2["start_reason"]
    assert a2["result"]["boundary_log"][0]["applied"] is True
    assert hold2["references"]["start"]
    assert all({"channel", "index", "t"} <= set(x) for x in hold2["references"]["start"])
    # 保持段确实缩短
    assert a2["result"]["metrics"]["hold_duration_s"] < r1["metrics"]["hold_duration_s"]
    # 屏蔽点保留原始引用（通道、序号、时标、数值）与理由
    ex = a2["result"]["exclusions"][0]
    assert ex["channel"] == "downstream_temp"
    assert ex["reason"] == "温度探头瞬时干扰"
    assert [p["index"] for p in ex["original_points"]] == [5, 6, 7]
    orig = load_sample("sample_seatleak_tight")["series"]["downstream_temp"]["points"]
    for got, raw in zip(ex["original_points"], orig[5:8]):
        assert got["t"] == pytest.approx(raw[0])
        assert got["value"] == pytest.approx(raw[1])
    # 旧版只读：内容不被新版本修改
    v1 = client.get(f"/seatleak/analyses/{a1['id']}").json()
    assert v1["version"] == 1
    assert v1["adjustments"] == []
    assert v1["result"]["metrics"] == r1["metrics"]
    assert v1["result"]["exclusions"] == []
    # 版本列表按序留痕
    r = client.get(f"/seatleak/tests/{test_id}/analyses")
    assert [x["version"] for x in r.json()] == [1, 2]


def test_adjust_is_cumulative_and_old_versions_readonly(client):
    _, a1 = submit_and_analyze(client, load_sample("sample_seatleak_tight"))
    hold = hold_segment(a1["result"])
    r = client.post(f"/seatleak/analyses/{a1['id']}/adjust", json={
        "author": "tech-01",
        "segment_moves": [{"segment": "hold", "boundary": "start",
                           "new_time": hold["t_start"] + 10.0,
                           "reason": "推迟保持段起点"}],
        "exclusions": [{"channel": "downstream_temp", "start_index": 5, "end_index": 7,
                        "reason": "温度探头瞬时干扰"}],
    })
    a2 = r.json()
    hold2 = hold_segment(a2["result"])
    r = client.post(f"/seatleak/analyses/{a2['id']}/adjust", json={
        "author": "tech-02",
        "segment_moves": [{"segment": "hold", "boundary": "end",
                           "new_time": hold2["t_end"] - 20.0,
                           "reason": "末端放空扰动，提前保持段终点"}],
    })
    assert r.status_code == 201, r.text
    a3 = r.json()
    assert a3["version"] == 3
    # 调整累计：v3 继承 v2 的全部调整（移动 + 屏蔽）并追加本次移动
    assert [x["author"] for x in a3["adjustments"]] == ["tech-01", "tech-01", "tech-02"]
    assert [x["type"] for x in a3["adjustments"]] == [
        "segment_move", "exclusion", "segment_move"]
    # v3 的屏蔽记录仍然保留原始点引用与理由
    ex = a3["result"]["exclusions"][0]
    assert ex["reason"] == "温度探头瞬时干扰"
    assert [p["index"] for p in ex["original_points"]] == [5, 6, 7]
    assert a3["result"]["metrics"]["hold_duration_s"] == pytest.approx(
        a2["result"]["metrics"]["hold_duration_s"] - 20.0, abs=0.5)
    # v1 / v2 均保持只读
    v1 = client.get(f"/seatleak/analyses/{a1['id']}").json()
    v2 = client.get(f"/seatleak/analyses/{a2['id']}").json()
    assert v1["adjustments"] == []
    assert len(v2["adjustments"]) == 2
    assert v2["result"]["metrics"] == a2["result"]["metrics"]


def test_adjust_validation_errors(client):
    _, a = submit_and_analyze(client, load_sample("sample_seatleak_tight"))
    # 空调整请求
    r = client.post(f"/seatleak/analyses/{a['id']}/adjust", json={"author": "x"})
    assert r.status_code == 422
    # 分析不存在
    r = client.post("/seatleak/analyses/999/adjust", json={
        "author": "x",
        "segment_moves": [{"segment": "hold", "boundary": "start",
                           "new_time": 50.0, "reason": "测试"}],
    })
    assert r.status_code == 404


# ---- 多次试验比较（趋势） ----

def test_comparison_degrading_trend(client):
    """同阀三次试验（基线 → 复测 → 内漏）：趋势判退化，比较结果持久化。"""
    analyses = []
    for name in ("sample_seatleak_tight", "sample_seatleak_tight2",
                 "sample_seatleak_leaking"):
        _, a = submit_and_analyze(client, load_sample(name))
        analyses.append(a)
    r = client.post("/seatleak/comparisons", json={"valve_tag": "XV-501"})
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["compatible"] is True
    assert c["overall"] == "degrading"
    # 冻结条件取自基准试验
    fr = c["frozen_conditions"]
    assert fr["gas"] == "air"
    assert fr["flow_direction"] == "upstream_to_downstream"
    assert fr["seat_config"] == "soft_seat"
    assert fr["reference_test_id"] == c["series"][0]["test_id"]
    # 序列按试验时间排序，全部可比
    assert [e["verdict"] for e in c["series"]] == ["pass", "pass", "fail"]
    assert all(e["comparable"] for e in c["series"])
    assert c["incomparable"] == []
    # 趋势：三项指标均退化
    tr = {t["metric"]: t for t in c["trends"]}
    assert tr["leak_rate_mean_nl_min"]["status"] == "degrading"
    assert tr["leak_rate_mean_nl_min"]["delta"] == pytest.approx(0.2797, abs=1e-3)
    assert tr["leak_rate_max_nl_min"]["status"] == "degrading"
    assert tr["cumulative_leak_nl"]["status"] == "degrading"
    assert len(tr["leak_rate_mean_nl_min"]["points"]) == 3
    # 指定 analysis_ids 得到相同结论
    r = client.post("/seatleak/comparisons",
                    json={"analysis_ids": [a["id"] for a in analyses]})
    assert r.status_code == 201
    assert r.json()["overall"] == "degrading"
    # 比较记录持久化，可复查
    r = client.get(f"/seatleak/comparisons/{c['comparison_id']}")
    assert r.status_code == 200
    assert r.json()["result"]["overall"] == "degrading"


def test_comparison_uses_latest_version(client):
    """调整后比较应取每个试验的最新版本。"""
    _, a1 = submit_and_analyze(client, load_sample("sample_seatleak_tight"))
    hold = hold_segment(a1["result"])
    r = client.post(f"/seatleak/analyses/{a1['id']}/adjust", json={
        "author": "tech-01",
        "segment_moves": [{"segment": "hold", "boundary": "start",
                           "new_time": hold["t_start"] + 10.0,
                           "reason": "推迟保持段起点"}],
    })
    a2 = r.json()
    r = client.post("/seatleak/comparisons", json={"valve_tag": "XV-501"})
    c = r.json()
    assert c["series"][0]["version"] == 2
    assert c["series"][0]["analysis_id"] == a2["id"]


def _variant_payload(case):
    if case == "gas_name":
        p = load_sample("sample_seatleak_tight")
        p["gas"]["name"] = "nitrogen"           # 摩尔质量相同，仅气体名称不同
        return p
    if case == "gas_molar_mass":
        p = load_sample("sample_seatleak_tight")
        p["gas"]["molar_mass_g_mol"] = 31.5     # 与 28.97 相差超过 0.5 g/mol
        return p
    if case == "flow_direction":
        # 反向流动样例改挂同一阀门：压差/气体/阀座均兼容，仅流向不同
        return load_sample("sample_seatleak_reverse_leaking", valve_tag="XV-501")
    if case == "seat_config":
        p = load_sample("sample_seatleak_tight")
        p["conditions"]["seat_config"] = "metal_seat"
        return p
    if case == "differential":
        # 上游 500→300 kPa：压差中位约 149 kPa（仍 ≥100，试验本身有效），
        # 但与基准 349 kPa 偏差超过 ±10%
        p = load_sample("sample_seatleak_tight")
        p["series"]["upstream_pressure"]["points"] = [
            [t, 300.0] for t, _ in p["series"]["upstream_pressure"]["points"]]
        return p
    if case == "evidence_gap":
        # 温度断档版本存在证据缺口，数值不参与定量比较
        return load_sample("sample_seatleak_temp_dropout", valve_tag="XV-501")
    raise AssertionError(case)


# 不兼容情形 → 期望的具体原因
INCOMPATIBLE = {
    "gas_name": "气体不一致",
    "gas_molar_mass": "摩尔质量不一致",
    "flow_direction": "流向不一致",
    "seat_config": "阀座配置不一致",
    "differential": "压差范围不兼容",
    "evidence_gap": "证据缺口",
}


@pytest.mark.parametrize("case", sorted(INCOMPATIBLE))
def test_incompatible_tests_excluded_from_trend(client, case):
    """气体/压差/流向/阀座配置不兼容或存在证据缺口的试验不得进入趋势。"""
    _, base = submit_and_analyze(client, load_sample("sample_seatleak_tight"))
    _, var = submit_and_analyze(client, _variant_payload(case))
    r = client.post("/seatleak/comparisons",
                    json={"analysis_ids": [base["id"], var["id"]]})
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["compatible"] is False
    # 基准试验可比，变体不可比且给出具体原因
    ref = next(e for e in c["series"] if e["analysis_id"] == base["id"])
    assert ref["comparable"] is True
    entry = next(e for e in c["series"] if e["analysis_id"] == var["id"])
    assert entry["comparable"] is False
    assert any(INCOMPATIBLE[case] in reason for reason in entry["reasons"])
    # 不可比试验不得进入趋势，且在 incomparable 清单中留痕
    for tr in c["trends"]:
        assert var["id"] not in [p["analysis_id"] for p in tr["points"]]
    inc = next(i for i in c["incomparable"] if i["analysis_id"] == var["id"])
    assert any(INCOMPATIBLE[case] in reason for reason in inc["reasons"])
    assert c["overall"] == "inconclusive"


def test_comparison_request_errors(client):
    r = client.post("/seatleak/comparisons", json={})
    assert r.status_code == 422                     # 既无 analysis_ids 也无 valve_tag
    r = client.post("/seatleak/comparisons", json={"valve_tag": "NO-SUCH-VALVE"})
    assert r.status_code == 404
    r = client.post("/seatleak/comparisons", json={"analysis_ids": [999]})
    assert r.status_code == 404
    r = client.get("/seatleak/comparisons/999")
    assert r.status_code == 404


# ---- 导出与打印报告 ----

def test_export_json_contains_compensation_and_raw_refs(client):
    _, a = submit_and_analyze(client, load_sample("sample_seatleak_leaking"))
    r = client.get(f"/seatleak/analyses/{a['id']}/export")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert f"seatleak_{a['id']}_v{a['version']}.json" in r.headers["content-disposition"]
    data = r.json()
    assert data["result"]["version"] == a["version"]
    # 补偿依据：方法、参数与空白插值说明
    comp = data["result"]["compensation"]
    assert "温压补偿质量平衡" in comp["method"]
    params = comp["parameters"]
    assert params["direction_sign"] == 1.0
    assert params["volume_m3"] == 0.01
    assert params["blank_rate_kpa_min"] > 0
    assert comp["blank_interp_note"]
    # 评估点带原始点引用；首次超限时刻纳入评估点
    eps = comp["evaluation_points"]
    assert len(eps) >= 3
    for ep in eps:
        channels = {x["channel"] for x in ep["raw_refs"]}
        assert {"downstream_pressure", "downstream_temp"} <= channels
        for x in ep["raw_refs"]:
            assert {"channel", "index", "t", "value"} <= set(x)
    fe = data["result"]["metrics"]["first_exceedance"]
    assert any(ep["t_s"] == pytest.approx(fe["t_s"]) for ep in eps)


def test_report_contains_compensation_basis(client):
    _, a = submit_and_analyze(client, load_sample("sample_seatleak_leaking"))
    r = client.get(f"/seatleak/analyses/{a['id']}/report")
    assert r.status_code == 200
    body = r.text
    assert "<svg" in body                            # 曲线
    assert "温压补偿质量平衡" in body                 # 补偿方法
    assert "按温度线性插值" in body                   # 空白回升插值依据
    assert "空白回升" in body
    assert "方向符号 +1" in body
    assert "首次超限 43.7s" in body                   # 首次超限标注
    assert "泄漏率限值 0.05 Nl/min" in body           # 阈值标注
    assert "累计限值 0.2 Nl" in body
    assert "泄漏超限" in body                         # 判定
    assert f"v{a['version']}" in body                 # 分析版本
    assert "downstream_pressure#" in body             # 原始点引用


def test_report_for_no_conclusion_shows_gaps(client):
    _, a = submit_and_analyze(client, load_sample("sample_seatleak_low_dp"))
    body = client.get(f"/seatleak/analyses/{a['id']}/report").text
    assert "证据不足，不得判定" in body
    assert "insufficient_differential_pressure" in body
    assert "有效压差" in body


# ---- 整组路由走查与数据库副作用 ----

def test_full_route_walkthrough_and_db_side_effects(client_and_db):
    client, db_path = client_and_db
    # 提交：阀门档案自动建立
    r = client.post("/seatleak/tests", json=load_sample("sample_seatleak_tight"))
    assert r.status_code == 201, r.text
    test_id = r.json()["seatleak_test_id"]
    valve_id = r.json()["valve_id"]
    assert any(v["tag"] == "XV-501" for v in client.get("/valves").json())
    # 试验详情：原始载荷与流向留存
    t = client.get(f"/seatleak/tests/{test_id}").json()
    assert t["flow_direction"] == "upstream_to_downstream"
    assert t["analyses"] == []
    assert t["payload"]["series"]["downstream_pressure"]["unit"] == "kPa"
    # 分析 → v1
    r = client.post(f"/seatleak/tests/{test_id}/analyze", json={"author": "tech-01"})
    assert r.status_code == 201, r.text
    a1 = r.json()
    assert a1["version"] == 1
    assert client.get(f"/seatleak/analyses/{a1['id']}").json()["result"]["verdict"] == "pass"
    # 调整 → v2
    hold = hold_segment(a1["result"])
    r = client.post(f"/seatleak/analyses/{a1['id']}/adjust", json={
        "author": "tech-02",
        "segment_moves": [{"segment": "hold", "boundary": "start",
                           "new_time": hold["t_start"] + 10.0,
                           "reason": "前段温度仍有漂移"}],
    })
    assert r.status_code == 201, r.text
    a2 = r.json()
    versions = client.get(f"/seatleak/tests/{test_id}/analyses").json()
    assert [x["version"] for x in versions] == [1, 2]
    # 导出与打印报告
    r = client.get(f"/seatleak/analyses/{a2['id']}/export")
    assert r.status_code == 200
    assert r.json()["result"]["version"] == 2
    r = client.get(f"/seatleak/analyses/{a2['id']}/report")
    assert r.status_code == 200 and "<svg" in r.text
    # 比较（单试验 → 数据不足，但记录持久化）
    r = client.post("/seatleak/comparisons", json={"valve_tag": "XV-501"})
    assert r.status_code == 201, r.text
    comp = r.json()
    assert comp["overall"] == "inconclusive"
    assert client.get(f"/seatleak/comparisons/{comp['comparison_id']}").status_code == 200
    # 数据库副作用
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM valves").fetchone()[0] == 1
        row = conn.execute(
            "SELECT flow_direction, payload_json FROM seatleak_tests").fetchone()
        assert row[0] == "upstream_to_downstream"
        assert json.loads(row[1])["gas"]["name"] == "air"
        rows = conn.execute(
            "SELECT version, author, adjustments_json FROM seatleak_analyses "
            "ORDER BY version").fetchall()
        assert [r[0] for r in rows] == [1, 2]
        assert rows[0][1] == "tech-01" and rows[1][1] == "tech-02"
        assert rows[0][2] == "[]"                       # v1 无调整且保持只读
        adj2 = json.loads(rows[1][2])
        assert adj2[0]["type"] == "segment_move"
        assert adj2[0]["reason"] == "前段温度仍有漂移"
        row = conn.execute(
            "SELECT valve_id FROM seatleak_comparisons").fetchone()
        assert row[0] == valve_id
    finally:
        conn.close()


def test_seatleak_not_found_routes(client):
    assert client.get("/seatleak/tests/999").status_code == 404
    assert client.post("/seatleak/tests/999/analyze", json={}).status_code == 404
    assert client.get("/seatleak/analyses/999").status_code == 404
    assert client.get("/seatleak/analyses/999/export").status_code == 404
    assert client.get("/seatleak/analyses/999/report").status_code == 404
