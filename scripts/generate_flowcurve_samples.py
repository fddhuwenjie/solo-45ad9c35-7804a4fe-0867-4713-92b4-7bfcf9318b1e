#!/usr/bin/env python3
"""生成单相液体流量曲线校核（/flowcurve）请求样例。

介质为 20 °C 水（密度 998 kg/m³，饱和蒸气压 2.34 kPa），DN80 调节阀，
额定 Cv=100；主样例流量计量程 0–160 m³/h（覆盖 Cv=100、ΔP=300 kPa 满点
流量 ≈120 m³/h），阀前恒定 500 kPa（表压）。
稳态平台：10、20、30、40、50、60、70、80 % 八个停留点，每点 6 s；
通道异频：阀位 2 Hz、流量/压力/温度 1 Hz。

样例清单：
- sample_fc_linear_good      线性特性、铭牌一致（k≈1.0，无嫌疑）
- sample_fc_blocked          结垢堵塞（实测 Cv 整体缩到 ~0.72）
- sample_fc_eroded           阀芯冲蚀（实测 Cv 整体放到 ~1.22）
- sample_fc_reversed         阀芯装反（阀位上升时 Cv 反而下降）
- sample_fc_low_dp           低平台压差不足（<20 kPa），该点排除不拟合
- sample_fc_overrange        高平台流量计超量程，该点排除
- sample_fc_cavitation       高平台进入气蚀区（ΔP>ΔPchoked，P2>Pv）
- sample_fc_drift            一个平台阀位持续漂移，按平台漂移排除
- sample_fc_missing_property 未提供密度/蒸气压 → 证据不足
- sample_fc_ramp_1hz         1 Hz 全程 0→100% 斜坡无停留 → 无平台
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "samples"

# 工况常量
P1_GAUGE = 500.0          # 阀前表压 kPa
ATM = 101.325
DENSITY = 998.0
PV = 2.34
PC = 22120.0
FL = 0.9
RATED_CV = 100.0
FS_FLOW = 120.0
DP_NOM = 300.0            # 名义压差 kPa（阀后 200 kPa 表压）
N1 = 0.865
SG = DENSITY / 999.0
DP_BAR = DP_NOM / 100.0
# Cv=100 时满平台流量：Q = N1·Cv·sqrt(ΔP/SG)
Q_AT_CV100 = N1 * 100.0 * (DP_BAR / SG) ** 0.5

PLATEAUS = [10, 20, 30, 40, 50, 60, 70, 80]
DWELL = 6.0
RAMP_T = 6.0             # 平台间爬升时长


def cv_fraction(x, char, r=50.0):
    if char == "linear":
        return x / 100.0
    if char == "equal_percentage":
        return r ** (x / 100.0 - 1.0)
    return (x / 100.0) ** 0.5


def build_schedule():
    """返回 [(t0, t1, position), ...]：每个平台停留 DWELL，间夹 RAMP_T 爬升。"""
    sched = []
    t = 2.0
    for x in PLATEAUS:
        sched.append((t, t + DWELL, float(x)))
        t += DWELL + RAMP_T
    return sched


def position_at(t, sched, drift_index=None, drift_rate=0.4):
    for k, (t0, t1, x) in enumerate(sched):
        if t0 <= t <= t1:
            if drift_index is not None and k == drift_index:
                return x + (t - t0) * drift_rate   # 平台内慢速漂移（<稳态速度阈值）
            return x
        if k + 1 < len(sched):
            tn = sched[k + 1][0]
            if t1 < t < tn:
                xn = sched[k + 1][2]
                return x + (xn - x) * (t - t1) / (tn - t1)
    return sched[-1][2]


def make_series(sched, char="linear", scale=1.0, reversed_=False,
                dp_by_index=None, meter_full_scale=None, q_extra_by_index=None,
                drift_index=None, nominal_dp=DP_NOM, dt_pos=0.5, dt_other=1.0):
    """生成五路异频序列。

    scale: 实测 Cv 相对铭牌的容量系数；reversed_: 阀位与 Cv 反向；
    dp_by_index: {平台序号: 压差 kPa} 覆盖；meter_full_scale: 仅用于判定
    是否钳位（超量程仪表读数停在满量程）；q_extra_by_index: 附加流量。
    """
    t_end = sched[-1][1] + 2.0
    pos_ts = [round(i * dt_pos, 3) for i in range(int(t_end / dt_pos) + 1)]
    other_ts = [round(i * dt_other, 3) for i in range(int(t_end / dt_other) + 1)]

    def plateau_of(t):
        for k, (t0, t1, _x) in enumerate(sched):
            if t0 - 0.05 <= t <= t1 + 0.05:
                return k
        return None

    def dp_plateau_of(t):
        """严格平台归属：爬升段（含相邻半秒）返回 None，压差覆盖不泄漏到爬升段。"""
        for k, (t0, t1, _x) in enumerate(sched):
            if t0 + 0.5 <= t <= t1:
                return k
        return None

    def frac_of(x):
        xx = (100.0 - x) if reversed_ else x
        return cv_fraction(xx, char)

    def cv_at_plateau(k):
        return RATED_CV * scale * frac_of(PLATEAUS[k])

    def dp_of(t):
        k = dp_plateau_of(t)
        if k is not None and dp_by_index and k in dp_by_index:
            # 压差阶跃在进入平台 1s 后发生，避开过渡点流量未建立的采样
            if t >= sched[k][0] + 1.0:
                return dp_by_index[k]
        # 末平台压差覆盖保持到记录末端（给 2s 恢复段），避免平台统计窗
        # 延伸到压差/流量回落段
        last_k = len(sched) - 1
        if (dp_by_index and last_k in dp_by_index
                and sched[last_k][1] <= t <= sched[last_k][1] + 2.0):
            return dp_by_index[last_k]
        return nominal_dp

    pos_pts, flow_pts, up_pts, dn_pts, temp_pts = [], [], [], [], []
    for t in pos_ts:
        pos_pts.append([t, round(position_at(t, sched, drift_index), 3)])
    for t in other_ts:
        k = plateau_of(t)
        if k is not None:
            local_x = position_at(t, sched, drift_index)
            xx = (100.0 - local_x) if reversed_ else local_x
            cvv = RATED_CV * scale * cv_fraction(xx, char)
        else:
            # 爬升段：按时间在相邻平台容量系数间线性插值
            j = next((j for j in range(len(sched) - 1)
                      if sched[j][1] < t < sched[j + 1][0]), None)
            if j is None:
                cvv = cv_at_plateau(len(sched) - 1)   # 末平台后停留
            else:
                f0, f1 = frac_of(PLATEAUS[j]), frac_of(PLATEAUS[j + 1])
                frac = (t - sched[j][1]) / (sched[j + 1][0] - sched[j][1])
                cvv = RATED_CV * scale * (f0 + (f1 - f0) * frac)
        dpv = dp_of(t)
        q = N1 * cvv * (dpv / 100.0 / SG) ** 0.5
        if q_extra_by_index and k in q_extra_by_index:
            q += q_extra_by_index[k]
        if meter_full_scale is not None and q > meter_full_scale:
            q = meter_full_scale            # 仪表超量程钳位（读数停在满量程）
        p2 = P1_GAUGE - dpv
        flow_pts.append([t, round(q, 3)])
        up_pts.append([t, P1_GAUGE])
        dn_pts.append([t, round(p2, 2)])
        temp_pts.append([t, 20.0])
    return {
        "position": {"unit": "%", "points": pos_pts},
        "flow": {"unit": "m3/h", "points": flow_pts},
        "upstream_pressure": {"unit": "kPa", "points": up_pts},
        "downstream_pressure": {"unit": "kPa", "points": dn_pts},
        "temperature": {"unit": "C", "points": temp_pts},
    }


def base_payload(tag="FCV-701", char="linear", series=None,
                 density=DENSITY, pv=PV, meter_fs=FS_FLOW,
                 flow_direction="upstream_to_downstream", started="2026-08-20T09:00:00",
                 cal="2027-01-01"):
    return {
        "valve_tag": tag,
        "valve_description": "DN80 给水调节阀",
        "phase": "periodic",
        "test_started_at": started,
        "range": {"min": 0.0, "max": 100.0, "unit": "%"},
        "flow_direction": flow_direction,
        "atmospheric_pressure_kpa": ATM,
        "series": series,
        "fluid": {
            "name": "water",
            "density_kg_m3": density,
            "vapor_pressure_kpa": pv,
            "critical_pressure_kpa": PC,
        },
        "valve": {
            "rated_cv": RATED_CV,
            "size_dn_mm": 80.0,
            "trim": "TRIM-LIN-01",
            "characteristic": char,
            "liquid_recovery_factor_fl": FL,
        },
        "meter": {"full_scale": meter_fs, "unit": "m3/h"},
        "conditions": {"load": "online", "note": ""},
        "calibration_valid_until": cal,
    }


def write(name, payload):
    (OUT / f"{name}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", name)


def main():
    sched = build_schedule()
    fs_main = 160.0       # 主样例流量计量程，覆盖 Cv=100、ΔP=300 kPa 的满点流量

    write("sample_fc_linear_good",
          base_payload(series=make_series(sched, char="linear", scale=1.0),
                       meter_fs=fs_main))
    write("sample_fc_blocked",
          base_payload(tag="FCV-702", meter_fs=fs_main,
                       series=make_series(sched, char="linear", scale=0.72)))
    write("sample_fc_eroded",
          base_payload(tag="FCV-703", meter_fs=fs_main,
                       series=make_series(sched, char="linear", scale=1.22)))
    write("sample_fc_reversed",
          base_payload(tag="FCV-704", meter_fs=fs_main,
                       series=make_series(sched, char="linear", scale=1.0,
                                          reversed_=True)))

    # 低压差：10% 平台（序号 0）压差只有 10 kPa
    write("sample_fc_low_dp",
          base_payload(tag="FCV-705", meter_fs=fs_main,
                       series=make_series(sched, char="linear", scale=1.0,
                                          dp_by_index={0: 10.0})))

    # 流量计超量程：仪表满量程仅 95 m³/h，70/80% 平台真值（105/120）超量程，
    # 读数钳在满量程（含进入超量程平台的爬升末段）
    def clamped_series():
        data = make_series(sched, char="linear", scale=1.0)
        qpts = data["flow"]["points"]
        # 第一个超量程平台（序号 6）起点
        clamp_t = sched[6][0] - RAMP_T
        data["flow"]["points"] = [
            [t, round(min(q, 95.0), 3)] if t >= clamp_t else [t, q] for t, q in qpts]
        return data

    write("sample_fc_overrange",
          base_payload(tag="FCV-706", meter_fs=95.0, series=clamped_series()))

    # 气蚀：70/80% 平台把阀后压到 10 kPa（表压），ΔP=490 kPa > ΔPchoked（≈495）
    write("sample_fc_cavitation",
          base_payload(tag="FCV-707", meter_fs=fs_main,
                       series=make_series(sched, char="linear", scale=1.0,
                                          dp_by_index={6: 490.0, 7: 490.0})))

    # 平台漂移：40% 平台（序号 3）阀位以 2.5%/s 漂移
    write("sample_fc_drift",
          base_payload(tag="FCV-708", meter_fs=fs_main,
                       series=make_series(sched, char="linear", scale=1.0,
                                          drift_index=3)))

    # 物性缺项
    write("sample_fc_missing_property",
          base_payload(tag="FCV-709", density=None, pv=None, meter_fs=fs_main,
                       series=make_series(sched, char="linear", scale=1.0)))

    # 1 Hz 全程斜坡 0→100%（无停留）
    ramp_ts = [float(i) for i in range(101)]
    ramp_series = {
        "position": {"unit": "%", "points": [[t, t] for t in ramp_ts]},
        "flow": {"unit": "m3/h",
                 "points": [[t, round(N1 * RATED_CV * (t / 100.0)
                                      * (DP_BAR / SG) ** 0.5, 3)] for t in ramp_ts]},
        "upstream_pressure": {"unit": "kPa", "points": [[t, P1_GAUGE] for t in ramp_ts]},
        "downstream_pressure": {"unit": "kPa",
                                "points": [[t, P1_GAUGE - DP_NOM] for t in ramp_ts]},
        "temperature": {"unit": "C", "points": [[t, 20.0] for t in ramp_ts]},
    }
    write("sample_fc_ramp_1hz", base_payload(tag="FCV-710", series=ramp_series))


if __name__ == "__main__":
    main()
