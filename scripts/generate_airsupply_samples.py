#!/usr/bin/env python3
"""生成气动执行机构供气瞬态核算方案样例。

样例清单：
- as_good            供气链充裕：四个动作（开/关/开/失气关）全部通过
- as_undersized      细长支管 + 小调压阀 + 多设备并发耗气：开阀行程供压
                     低于要求且超时（首个设备 D1），失气关行程勉强到位
- as_tank_shortfall  储气罐容积不足：失气后罐压仍高于要求，但存量不够，
                     阀位停在行程中途，安全位未到达
- as_unit_conflict   容积单位不支持（gallon），判 no_conclusion
- as_event_order     并发用气事件终点早于起点（事件倒序），判 no_conclusion
- as_curve_gap       调压阀流量曲线覆盖范围不足（运行压差超出曲线），
                     判 no_conclusion
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "samples"


def base_scheme(name, valve_tag="XV-6101"):
    """双作用执行器（fail_close），A/B 腔均供气驱动，负载 2000 N + 摩擦 500 N。"""
    return {
        "valve_tag": valve_tag,
        "valve_description": "气动执行机构供气瞬态核算样例",
        "name": name,
        "actuator": {
            "actuator_type": "double_acting",
            "fail_mode": "fail_close",
            "air_chambers": ["a", "b"],
            "chamber_a": {"min_volume": {"value": 0.5, "unit": "l"},
                          "max_volume": {"value": 3.5, "unit": "l"}},
            "chamber_b": {"min_volume": {"value": 0.5, "unit": "l"},
                          "max_volume": {"value": 3.5, "unit": "l"}},
            "area_a": {"value": 100.0, "unit": "cm2"},
            "area_b": {"value": 100.0, "unit": "cm2"},
            "load": {"value": 2000.0, "unit": "n"},
            "friction": {"value": 500.0, "unit": "n"},
            "port_conductance": {"value": 5.0, "unit": "nl/min/kpa"},
            "initial_position_pct": 0.0,
            "initial_chamber_pressure": {"value": 0.0, "unit": "kPa"},
        },
        "supply": {"header_pressure": {"value": 600.0, "unit": "kPa"}},
        "pipe_segments": [
            {"name": "branch", "length_m": 20.0, "inner_diameter_mm": 10.0,
             "friction_factor": 0.03, "minor_loss_k": 0.0},
        ],
        "regulator": {
            "set_pressure": {"value": 500.0, "unit": "kPa"},
            "dp_unit": "kPa", "flow_unit": "Nl/min",
            "flow_curve": [[25.0, 150.0], [50.0, 300.0],
                           [100.0, 500.0], [200.0, 700.0]],
        },
        "tank": {"volume": {"value": 40.0, "unit": "l"},
                 "initial_pressure": {"value": 500.0, "unit": "kPa"}},
        "events": [
            {"device": "D1", "t_start_s": 3.0, "t_end_s": 13.0,
             "flow": {"value": 60.0, "unit": "Nl/min"}},
            {"device": "D2", "t_start_s": 26.0, "t_end_s": 36.0,
             "flow": {"value": 40.0, "unit": "Nl/min"}},
        ],
        "actions": [
            {"name": "open1", "kind": "powered_stroke", "direction": "open",
             "t_start_s": 2.0, "travel_time_s_max": 15.0,
             "required_supply_pressure_min": {"value": 300.0, "unit": "kPa"}},
            {"name": "close1", "kind": "powered_stroke", "direction": "close",
             "t_start_s": 25.0, "travel_time_s_max": 15.0,
             "required_supply_pressure_min": {"value": 300.0, "unit": "kPa"}},
            {"name": "open2", "kind": "powered_stroke", "direction": "open",
             "t_start_s": 50.0, "travel_time_s_max": 15.0,
             "required_supply_pressure_min": {"value": 300.0, "unit": "kPa"}},
            {"name": "fail_close", "kind": "fail_safe_stroke", "direction": "close",
             "t_start_s": 75.0, "travel_time_s_max": 20.0,
             "required_supply_pressure_min": {"value": 150.0, "unit": "kPa"}},
        ],
        "solver": {"dt_s": 0.005, "t_max_s": 100.0, "record_dt_s": 0.05,
                   "convergence_tol_pct": 2.0, "max_stroke_rate_pct_s": 200.0,
                   "ambient_temp_c": 20.0},
    }


def as_good():
    s = base_scheme("as_good")
    s["actuator"]["load"] = {"value": 300.0, "unit": "n"}
    s["actuator"]["friction"] = {"value": 150.0, "unit": "n"}
    return s


def as_undersized():
    """细长支管 + 小调压阀 + 两台设备并发耗气；开阀行程供压不足且超时。"""
    s = base_scheme("as_undersized", valve_tag="XV-6201")
    s["pipe_segments"] = [
        {"name": "branch", "length_m": 80.0, "inner_diameter_mm": 6.0,
         "friction_factor": 0.03, "minor_loss_k": 0.0},
    ]
    s["regulator"]["flow_curve"] = [[10.0, 40.0], [25.0, 80.0], [50.0, 120.0],
                                    [100.0, 150.0], [200.0, 170.0]]
    s["tank"]["volume"] = {"value": 5.0, "unit": "l"}
    s["actuator"]["port_conductance"] = {"value": 0.5, "unit": "nl/min/kpa"}
    s["events"] = [
        {"device": "D1", "t_start_s": 2.5, "t_end_s": 20.0,
         "flow": {"value": 150.0, "unit": "Nl/min"}},
        {"device": "D2", "t_start_s": 5.0, "t_end_s": 15.0,
         "flow": {"value": 100.0, "unit": "Nl/min"}},
    ]
    s["actions"] = [
        {"name": "open1", "kind": "powered_stroke", "direction": "open",
         "t_start_s": 2.0, "travel_time_s_max": 10.0,
         "required_supply_pressure_min": {"value": 300.0, "unit": "kPa"}},
        {"name": "fail_close", "kind": "fail_safe_stroke", "direction": "close",
         "t_start_s": 30.0, "travel_time_s_max": 20.0,
         "required_supply_pressure_min": {"value": 150.0, "unit": "kPa"}},
    ]
    s["solver"]["t_max_s"] = 60.0
    return s


def as_tank_shortfall():
    """储气罐 1.5 L：失气后罐压仍高于要求，但存量不足以走完全行程。"""
    s = base_scheme("as_tank_shortfall", valve_tag="XV-6301")
    s["tank"]["volume"] = {"value": 1.5, "unit": "l"}
    s["events"] = []
    s["actions"] = [
        {"name": "open1", "kind": "powered_stroke", "direction": "open",
         "t_start_s": 2.0, "travel_time_s_max": 15.0,
         "required_supply_pressure_min": {"value": 300.0, "unit": "kPa"}},
        {"name": "fail_close", "kind": "fail_safe_stroke", "direction": "close",
         "t_start_s": 30.0, "travel_time_s_max": 20.0,
         "required_supply_pressure_min": {"value": 150.0, "unit": "kPa"}},
    ]
    s["solver"]["t_max_s"] = 60.0
    return s


def as_unit_conflict():
    s = base_scheme("as_unit_conflict", valve_tag="XV-6401")
    s["tank"]["volume"] = {"value": 10.0, "unit": "gallon"}
    return s


def as_event_order():
    s = base_scheme("as_event_order", valve_tag="XV-6501")
    s["events"] = [
        {"device": "D1", "t_start_s": 10.0, "t_end_s": 5.0,
         "flow": {"value": 60.0, "unit": "Nl/min"}},
    ]
    return s


def as_curve_gap():
    s = base_scheme("as_curve_gap", valve_tag="XV-6601")
    s["regulator"]["flow_curve"] = [[5.0, 50.0], [10.0, 80.0]]
    s["events"] = [
        {"device": "D1", "t_start_s": 2.5, "t_end_s": 20.0,
         "flow": {"value": 200.0, "unit": "Nl/min"}},
    ]
    return s


def main():
    samples = {
        "sample_as_good": as_good(),
        "sample_as_undersized": as_undersized(),
        "sample_as_tank_shortfall": as_tank_shortfall(),
        "sample_as_unit_conflict": as_unit_conflict(),
        "sample_as_event_order": as_event_order(),
        "sample_as_curve_gap": as_curve_gap(),
    }
    for name, payload in samples.items():
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
