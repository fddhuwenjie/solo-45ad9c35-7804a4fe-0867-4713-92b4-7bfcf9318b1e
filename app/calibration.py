"""逐通道仪器校准链。

校准版本（calibration version）是不可变的证书记录：仪器序列号、测量类型、
单位、有效时段（valid_from/valid_until）、声明量程（range_min/range_max）、
示值—参考值点列（indication/reference，均严格单调递增）、标准不确定度与
证书摘要。版本一经创建不得修改；续证或纠错只能派生新版本。

每路时序在分析时**冻结**一个校准版本：先按点列分段线性把原始示值修正为
参考值，再进入单位统一、时间对齐、不确定度与指标计算。证书未生效/已过期、
测试区间跨越有效期边界、量程不覆盖原始读数、点列不单调、单位不兼容时，
该通道被拒绝（rejected），整个分析不得形成诊断结论（no_conclusion），并逐条
列出通道、原始读数区间与证据缺口代码。

旧字段 calibration_valid_until 在提交未提供 calibration_bindings 时继续生效
（legacy 模式）；提供绑定则进入逐通道链模式，链模式下所有测量通道都必须
绑定有效版本。修正前后差值、采用证书与拒绝原因在结果块 calibration_chain 中
与 JSON 导出、打印报告、版本比较共用，报告不另行重算。
"""

import hashlib
import json

from .flowcurve import liquid as liquid_units
from .seatleak import gas as gas_units
from .processing.units import PRESSURE_TO_KPA, TRAVEL_UNITS

# ===================== 测量类型与单位族 =====================

# 指令信号（% 与 4-20mA 同族，可互转；物理行程不属于本族）
SIGNAL_UNITS = ("%", "ma")
# 温度
# 温度（既有分析 gas.norm_temperature 支持的全部等价写法）
TEMP_ALIASES = {
    "c": "c", "°c": "c", "degc": "c", "celsius": "c",
    "k": "k", "kelvin": "k",
    "f": "f", "°f": "f", "fahrenheit": "f",
}
TEMP_UNITS = tuple(TEMP_ALIASES.keys())
# 标准状态气体流量
FLOW_GAS_UNITS = tuple(gas_units.FLOW_TO_NL_MIN.keys())
# 液体工况体积流量（含 m³/h 等既有分析已支持的 Unicode 写法）
FLOW_LIQUID_UNITS = tuple(liquid_units.FLOW_TO_M3H.keys())

MEASUREMENT_TYPES = (
    "command", "position", "pressure", "temperature", "flow_gas", "flow_liquid",
)

# 各测量类型单位族内到基准单位的乘数（温度为仿射换算，乘数仅用于族归属判断）
_FACTORS = {
    "command": {"%": 1.0, "ma": 1.0},
    "position": {"%": 1.0, "ma": 1.0,
                 **{u: 1.0 for u in TRAVEL_UNITS}},
    "pressure": dict(PRESSURE_TO_KPA),
    "temperature": {u: 1.0 for u in TEMP_ALIASES},
    "flow_gas": {u: f for u, f in gas_units.FLOW_TO_NL_MIN.items()},
    # 保留 m³/h、m³/min、l/min、l/h 等 Unicode 等价写法（与流量曲线分析一致）
    "flow_liquid": {u: f for u, f in liquid_units.FLOW_TO_M3H.items()},
}

# 各测量类型基准单位（族内换算目标）
BASE_UNIT = {
    "command": "%", "position": "%", "pressure": "kPa",
    "temperature": "C", "flow_gas": "Nl/min", "flow_liquid": "m3/h",
}

# 通道 → 期望测量类型
CHANNEL_MEASUREMENT_TYPE = {
    # 全行程
    "command": "command", "position": "position", "pressure": "pressure",
    # 故障安全
    # 阀座密封
    "upstream_pressure": "pressure", "downstream_pressure": "pressure",
    "downstream_temp": "temperature", "flow": "flow_gas",
    # 推力签名
    "supply_pressure": "pressure", "chamber_a_pressure": "pressure",
    "chamber_b_pressure": "pressure",
    # 流量曲线
    "temperature": "temperature", "flow": "flow_liquid",
}
# "flow" 同时出现在阀座（气体）与流量曲线（液体）：由测试模块显式传入
# measurement_type 覆盖。

# 拒绝原因代码
REJECT_NOT_BOUND = "calibration_not_bound"
REJECT_VERSION_MISSING = "calibration_version_missing"
REJECT_TYPE_MISMATCH = "calibration_type_mismatch"
REJECT_UNIT_INCOMPATIBLE = "calibration_unit_incompatible"
REJECT_NOT_YET_VALID = "calibration_not_yet_valid"
REJECT_EXPIRED = "calibration_expired"
REJECT_SPANS_VALIDITY = "calibration_spans_validity"
REJECT_RANGE_UNCOVERED = "calibration_range_uncovered"
REJECT_POINTS_NONMONOTONIC = "calibration_points_nonmonotonic"
REJECT_POINTS_OUTSIDE = "calibration_points_outside_range"
REJECT_EMPTY_SERIES = "series_empty"

BLOCKING_REJECT_CODES = frozenset({
    REJECT_NOT_BOUND, REJECT_VERSION_MISSING, REJECT_TYPE_MISMATCH,
    REJECT_UNIT_INCOMPATIBLE, REJECT_NOT_YET_VALID, REJECT_EXPIRED,
    REJECT_SPANS_VALIDITY, REJECT_RANGE_UNCOVERED, REJECT_POINTS_NONMONOTONIC,
    REJECT_POINTS_OUTSIDE, REJECT_EMPTY_SERIES,
})

# 各类测试的测量通道（trip 接点不是测量通道）
TEST_CHANNELS = {
    "fullstroke": ("command", "position", "pressure"),
    "failsafe": ("command", "position", "pressure"),
    "seatleak": ("command", "position", "upstream_pressure",
                 "downstream_pressure", "downstream_temp", "flow"),
    "thrust": ("command", "position", "supply_pressure",
               "chamber_a_pressure", "chamber_b_pressure"),
    "flowcurve": ("position", "flow", "upstream_pressure",
                  "downstream_pressure", "temperature"),
    # 限位开关（open/closed 为离散接点，不是测量通道）
    "limitswitch": ("position",),
}


# ===================== 单位换算 =====================

def _u(unit):
    return (unit or "").strip().lower()


def unit_family(measurement_type, unit):
    """单位是否属于该测量类型的单位族。"""
    return _u(unit) in _FACTORS.get(measurement_type, {})


def convert_value(value, from_unit, to_unit, measurement_type,
                  range_min=None, range_max=None):
    """同族单位之间换算单点数值。无法换算抛 ValueError。

    % ↔ mA 按 4–20mA 标准信号换算；物理行程 ↔ % 需要量程端点
    （range_min/range_max，按该单位声明）；温度做仿射变换；其余族内
    单位按基准单位乘除换算。
    """
    fu, tu = _u(from_unit), _u(to_unit)
    factors = _FACTORS.get(measurement_type)
    if factors is None:
        raise ValueError(f"未知测量类型 {measurement_type!r}")
    if fu not in factors or tu not in factors:
        raise ValueError(
            f"单位不兼容：{from_unit!r} 与 {to_unit!r} 不属于测量类型 "
            f"{measurement_type} 的同一单位族")
    if fu == tu:
        return float(value)

    mt = measurement_type
    # % ↔ mA
    if {fu, tu} <= {"%", "ma"}:
        if fu == "ma":
            return (float(value) - 4.0) / 16.0 * 100.0
        return 4.0 + float(value) / 100.0 * 16.0
    # 物理行程 ↔ %
    if "%" in (fu, tu) or "ma" in (fu, tu):
        if range_min is None or range_max is None:
            raise ValueError(
                f"{measurement_type} 通道在 {from_unit} 与 {to_unit} 间换算需要量程端点")
        span = float(range_max) - float(range_min)
        if abs(span) < 1e-12:
            raise ValueError("量程跨度为 0，无法换算")
        if fu in TRAVEL_UNITS and tu in ("%", "ma"):
            pct = (float(value) - range_min) / span * 100.0
            return pct if tu == "%" else 4.0 + pct / 100.0 * 16.0
        if fu in ("%", "ma") and tu in TRAVEL_UNITS:
            pct = float(value) if fu == "%" else (float(value) - 4.0) / 16.0 * 100.0
            return range_min + pct / 100.0 * span
        raise ValueError(f"单位不兼容：{from_unit!r} 与 {to_unit!r}")
    # 温度仿射（先到 °C，再到目标单位）
    if mt == "temperature":
        c = _to_celsius(value, fu)
        return _from_celsius(c, tu)
    # 线性乘数单位（压力/流量）
    return float(value) * factors[fu] / factors[tu]


def _to_celsius(value, u):
    u = TEMP_ALIASES.get(u, u)
    if u == "c":
        return float(value)
    if u == "k":
        return float(value) - gas_units.TEMP_OFFSET_K
    if u == "f":
        return (float(value) - 32.0) / 1.8
    raise ValueError(f"不支持的温度单位 {u!r}")


def _from_celsius(c, u):
    u = TEMP_ALIASES.get(u, u)
    if u == "c":
        return float(c)
    if u == "k":
        return float(c) + gas_units.TEMP_OFFSET_K
    if u == "f":
        return float(c) * 1.8 + 32.0
    raise ValueError(f"不支持的温度单位 {u!r}")


def convert_uncertainty(value, cal_unit, channel_unit, measurement_type,
                        range_min=None, range_max=None):
    """标准不确定度换算（仿射单位只取斜率，4mA 零点偏移不计）。"""
    fu, tu = _u(cal_unit), _u(channel_unit)
    if fu == tu:
        return float(value)
    mt = measurement_type
    if {fu, tu} <= {"%", "ma"}:
        return float(value) / 16.0 * 100.0 if fu == "ma" \
            else float(value) * 16.0 / 100.0
    if ("%" in (fu, tu) or "ma" in (fu, tu)) and mt == "position":
        if range_min is None or range_max is None:
            raise ValueError("物理行程不确定度换算需要量程端点")
        span = float(range_max) - float(range_min)
        if abs(span) < 1e-12:
            raise ValueError("量程跨度为 0")
        if fu in TRAVEL_UNITS and tu in ("%", "ma"):
            s = 100.0 / span
        elif fu in ("%", "ma") and tu in TRAVEL_UNITS:
            s = span / 100.0
        else:
            raise ValueError(f"单位不兼容：{cal_unit!r} 与 {channel_unit!r}")
        if "ma" in (fu, tu):
            s *= 16.0 / 100.0
        return float(value) * s
    if mt == "temperature":
        # 不确定度：用 ±1 小扰动求斜率
        c0 = _to_celsius(0.0, fu)
        c1 = _to_celsius(1.0, fu)
        slope_c = c1 - c0
        t0 = _from_celsius(0.0, tu)
        t1 = _from_celsius(1.0, tu)
        return float(value) * slope_c / (t1 - t0)
    factors = _FACTORS[mt]
    if fu not in factors or tu not in factors:
        raise ValueError(f"单位不兼容：{cal_unit!r} 与 {channel_unit!r}")
    return float(value) * factors[fu] / factors[tu]


# ===================== 点列校验与分段线性修正 =====================

def check_points(points):
    """校验示值—参考值点列。返回问题代码列表（空=通过）。

    要求：≥2 点；示值与参考值均严格单调递增。
    """
    problems = []
    pts = points or []
    if len(pts) < 2:
        return [REJECT_POINTS_NONMONOTONIC]
    for k, p in enumerate(pts):
        if len(p) < 2:
            return [REJECT_POINTS_NONMONOTONIC]
    ind = [float(p[0]) for p in pts]
    ref = [float(p[1]) for p in pts]
    if any(ind[i] <= ind[i - 1] for i in range(1, len(ind))):
        problems.append(REJECT_POINTS_NONMONOTONIC)
    if any(ref[i] <= ref[i - 1] for i in range(1, len(ref))):
        problems.append(REJECT_POINTS_NONMONOTONIC)
    return problems


def check_range_covers(version, values_in_cal_unit):
    """声明量程是否覆盖原始读数区间（已换算到证书单位）。返回 bool。"""
    if not values_in_cal_unit:
        return False
    lo, hi = min(values_in_cal_unit), max(values_in_cal_unit)
    return lo >= float(version["range_min"]) - 1e-9 and \
        hi <= float(version["range_max"]) + 1e-9


def correct_values(values, points):
    """按示值—参考值点列分段线性修正（不外推）。

    返回修正值列表；落在点列覆盖域外（小于首个示值或大于末个示值）的点
    返回 None，由调用方记为 REJECT_POINTS_OUTSIDE。
    """
    ind = [float(p[0]) for p in points]
    ref = [float(p[1]) for p in points]
    n = len(ind)
    out = []
    for v in values:
        if v < ind[0] or v > ind[-1]:
            out.append(None)
            continue
        # 二分查找所在段
        lo, hi = 0, n - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if ind[mid] <= v:
                lo = mid
            else:
                hi = mid
        if abs(ind[hi] - ind[lo]) < 1e-15:
            out.append(ref[lo])
        else:
            f = (v - ind[lo]) / (ind[hi] - ind[lo])
            out.append(ref[lo] + f * (ref[hi] - ref[lo]))
    return out


# ===================== 有效期与测试区间 =====================

def parse_iso(s):
    """宽松解析 ISO 日期/时间；返回 aware datetime 或 None。"""
    if not s:
        return None
    from datetime import datetime, timezone
    txt = str(s).strip().replace("Z", "+00:00")
    # 纯日期按当日 00:00 UTC
    if len(txt) == 10 and txt[4] == "-" and txt[7] == "-":
        txt += "T00:00:00+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def test_interval(series_by_channel, test_started_at, submitted_at):
    """测试时间区间（绝对时间）。

    起点 = test_started_at（缺省退回提交时间）；
    终点 = 起点 + 所有通道**最大样本时标**（时标是相对测试起点的秒数，
    非零起始时标必须按最大时标计，不能用 max−min 跨度把起始偏移吞掉）。
    返回 (start_dt, end_dt, max_t)，无法确定起点时返回 None。
    """
    start = parse_iso(test_started_at) or parse_iso(submitted_at)
    if start is None:
        return None
    max_t = 0.0
    for ch, data in series_by_channel.items():
        if not data:
            continue
        pts = data.get("points") if isinstance(data, dict) else None
        if not pts:
            continue
        ts = [float(p[0]) for p in pts]
        if ts:
            max_t = max(max_t, max(ts))
    return start, start + _timedelta(max_t), max_t


def _timedelta(seconds):
    from datetime import timedelta
    return timedelta(seconds=float(seconds))


def validity_status(version, interval):
    """证书有效期相对测试区间的状态。

    返回 (status, detail_code)：
    - ok：区间完全落在 [valid_from, valid_until] 内（含端点）
    - not_yet_valid / expired / spans_validity
    """
    vf = parse_iso(version.get("valid_from"))
    vu = parse_iso(version.get("valid_until"))
    start, end = interval[0], interval[1]
    if vf is None or vu is None:
        return "invalid", REJECT_NOT_YET_VALID
    if start >= vf and end <= vu:
        return "ok", None
    if end <= vf:
        return "not_yet_valid", REJECT_NOT_YET_VALID
    if start >= vu:
        return "expired", REJECT_EXPIRED
    return "spans", REJECT_SPANS_VALIDITY


# ===================== 摘要与冻结快照 =====================

def validate_version_create(data):
    """建版服务层校验：时段、量程、点列域、单位族。返回问题字符串列表（空=通过）。"""
    problems = []
    vf, vu = parse_iso(data.get("valid_from")), parse_iso(data.get("valid_until"))
    if vf is None or vu is None:
        problems.append("valid_from/valid_until 须为可解析的 ISO 8601 日期或时间")
    elif not (vf < vu):
        problems.append(
            f"证书时段非法：生效 {data.get('valid_from')} 不早于失效 {data.get('valid_until')}")
    mt = data.get("measurement_type")
    unit = data.get("unit")
    if not unit_family(mt, unit):
        problems.append(
            f"单位 {unit!r} 与测量类型 {mt} 不兼容（须同族单位）")
    lo, hi = data.get("range_min"), data.get("range_max")
    if lo is not None and hi is not None and hi <= lo:
        problems.append(f"量程非法：上限 {hi} 不大于下限 {lo}")
    pts = data.get("points") or []
    if check_points(pts):
        problems.append("示值—参考值点列须各自严格单调递增且至少 2 点")
    else:
        ind = [float(p[0]) for p in pts]
        if lo is not None and hi is not None:
            if min(ind) < float(lo) - 1e-9 or max(ind) > float(hi) + 1e-9:
                problems.append(
                    f"点列示值域 [{min(ind):g}, {max(ind):g}] 超出声明量程 "
                    f"[{lo:g}, {hi:g}]")
    su = data.get("standard_uncertainty")
    if su is not None and su <= 0:
        problems.append("标准不确定度必须为正数")
    return problems


def certificate_digest(version_input):
    """对证书识别要素做 SHA-256 摘要（不含自增 id/创建时间）。"""
    payload = {
        "instrument_serial": version_input.get("instrument_serial"),
        "measurement_type": version_input.get("measurement_type"),
        "unit": version_input.get("unit"),
        "valid_from": version_input.get("valid_from"),
        "valid_until": version_input.get("valid_until"),
        "range_min": version_input.get("range_min"),
        "range_max": version_input.get("range_max"),
        "points": [[float(p[0]), float(p[1])]
                   for p in version_input.get("points", [])],
        "standard_uncertainty": version_input.get("standard_uncertainty"),
        "certificate_summary": version_input.get("certificate_summary"),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def freeze_version(row):
    """DB 行 → 冻结快照（随分析版本保存，续证/纠错不影响旧分析）。"""
    points = row.get("points")
    if points is None:
        points = json.loads(row.get("points_json") or "[]")
    return {
        "calibration_version_id": row["id"],
        "instrument_serial": row["instrument_serial"],
        "measurement_type": row["measurement_type"],
        "unit": row["unit"],
        "valid_from": row["valid_from"],
        "valid_until": row["valid_until"],
        "range_min": row["range_min"],
        "range_max": row["range_max"],
        "points": points,
        "standard_uncertainty": row["standard_uncertainty"],
        "certificate_summary": row.get("certificate_summary") or "",
        "certificate_digest": row.get("certificate_digest"),
        "supersedes_id": row.get("supersedes_id"),
        "note": row.get("note") or "",
    }


# ===================== 逐通道校准链 =====================

def _present_channels(series, test_kind, flow_measurement_type=None):
    """实际提供（且有点）的测量通道。"""
    out = []
    for ch in TEST_CHANNELS[test_kind]:
        data = series.get(ch)
        if data and data.get("points"):
            out.append(ch)
    return out


def apply_chain(db, *, test_kind, series, bindings, test_started_at,
                submitted_at, range_min=None, range_max=None,
                flow_measurement_type=None):
    """对每路时序执行校准链。

    bindings: {channel: calibration_version_id}（dict 或 None）。
    返回 (corrected_series, chain_block)：
    - corrected_series: {channel: {"unit": 通道原单位, "points": [[t, 修正值], ...]}}，
      被拒绝通道保留原始读数以便诊断展示，但下游单位统一应跳过；
    - chain_block: 结果块（mode、channels、rejections、test_interval、accepted）。
    """
    present = _present_channels(series, test_kind, flow_measurement_type)
    interval = test_interval(
        {ch: series[ch] for ch in present}, test_started_at, submitted_at)

    # legacy 模式：未提供任何绑定，交由旧 calibration_valid_until 逻辑处理
    if bindings is None:
        return None, _legacy_block(present, interval)

    channels_out, rejections, calibration_components = [], [], []
    corrected = {}
    for ch in present:
        mt = flow_measurement_type if ch == "flow" and flow_measurement_type \
            else CHANNEL_MEASUREMENT_TYPE.get(ch)
        data = series[ch]
        raw_points = [[float(t), float(v)] for t, v in
                      ((p[0], p[1]) for p in data["points"])]
        raw_unit = data.get("unit")
        raw_vals = [p[1] for p in raw_points]
        ts = [p[0] for p in raw_points]
        raw_interval = [round(min(raw_vals), 6), round(max(raw_vals), 6)] if raw_vals else None

        entry = {
            "channel": ch,
            "measurement_type": mt,
            "instrument_serial": None,
            "calibration_version_id": bindings.get(ch),
            "certificate_digest": None,
            "unit": raw_unit,
            "raw_reading_interval": raw_interval,
            "corrected_interval": None,
            "raw_min": raw_interval[0] if raw_interval else None,
            "raw_max": raw_interval[1] if raw_interval else None,
            "corrected_min": None, "corrected_max": None,
            "n_points": len(raw_vals),
            "mean_correction": None, "max_abs_correction": None,
            "shift_samples": [],
            "standard_uncertainty": None,
            "status": "rejected",
            "rejection_code": None,
            "rejection_reason": None,
        }

        def reject(code, detail, version=None):
            entry["rejection_code"] = code
            entry["rejection_reason"] = detail
            entry["status"] = "rejected"
            rejections.append({
                "channel": ch, "code": code, "detail": detail,
                "raw_reading_interval": raw_interval, "raw_unit": raw_unit,
                "calibration_version_id": entry["calibration_version_id"],
                "instrument_serial": (version or {}).get("instrument_serial"),
            })
            channels_out.append(entry)
            corrected[ch] = {"unit": raw_unit, "points": raw_points}

        vid = bindings.get(ch)
        if vid is None:
            reject(REJECT_NOT_BOUND,
                   f"{ch}：链模式下测量通道未绑定校准版本，无法确认仪表校准状态")
            continue
        row = db.get_calibration_version(vid) if hasattr(db, "get_calibration_version") else None
        if row is None:
            reject(REJECT_VERSION_MISSING,
                   f"{ch}：绑定的校准版本 #{vid} 不存在（可能已被误删或编号错误）")
            continue
        version = freeze_version(row)

        entry["instrument_serial"] = version["instrument_serial"]
        entry["certificate_digest"] = version["certificate_digest"]

        if version["measurement_type"] != mt:
            reject(REJECT_TYPE_MISMATCH,
                   f"{ch}：通道测量类型为 {mt}，但版本 #{vid} 证书类型为 "
                   f"{version['measurement_type']}（仪器 {version['instrument_serial']}），"
                   "测量类型不匹配", version)
            continue
        cal_unit = version["unit"]
        # 单位族兼容性
        try:
            vals_in_cal = [convert_value(v, raw_unit, cal_unit, mt,
                                         range_min, range_max) for v in raw_vals]
        except ValueError as e:
            reject(REJECT_UNIT_INCOMPATIBLE,
                   f"{ch}：通道单位 {raw_unit!r} 与证书单位 {cal_unit!r} 不兼容（{e}）",
                   version)
            continue

        # 证书点列单调性（创建时也校验，冻结后再防御性检查）
        point_problems = check_points(version["points"])
        if point_problems:
            reject(REJECT_POINTS_NONMONOTONIC,
                   f"{ch}：版本 #{vid}（{version['instrument_serial']}）示值—参考值点列"
                   "未严格单调递增，无法建立分段线性修正", version)
            continue

        # 有效期（测试区间绝对时间）
        if interval is None:
            reject(REJECT_NOT_YET_VALID,
                   f"{ch}：无法确定测试时间区间（test_started_at 缺失且无法解析），"
                   "不能确认证书有效期覆盖", version)
            continue
        vstatus, vcode = validity_status(version, interval)
        if vstatus != "ok":
            label = {REJECT_NOT_YET_VALID: "证书尚未生效",
                     REJECT_EXPIRED: "证书已过期",
                     REJECT_SPANS_VALIDITY: "测试区间跨越证书有效期边界"}[vcode]
            reject(vcode,
                   f"{ch}：{label}（{version['valid_from']} ~ {version['valid_until']}，"
                   f"仪器 {version['instrument_serial']}），测试区间 "
                   f"{interval[0].isoformat()} ~ {interval[1].isoformat()}", version)
            continue

        # 声明量程覆盖
        if not check_range_covers(version, vals_in_cal):
            reject(REJECT_RANGE_UNCOVERED,
                   f"{ch}：原始读数区间 [{min(vals_in_cal):.4g}, {max(vals_in_cal):.4g}] "
                   f"{cal_unit} 超出证书量程 [{version['range_min']}, "
                   f"{version['range_max']}] {cal_unit}（仪器 "
                   f"{version['instrument_serial']}），量程未覆盖", version)
            continue

        # 点列覆盖域 + 分段线性修正
        corrected_in_cal = correct_values(vals_in_cal, version["points"])
        if any(v is None for v in corrected_in_cal):
            bad = [vals_in_cal[i] for i, v in enumerate(corrected_in_cal) if v is None]
            ind = [float(p[0]) for p in version["points"]]
            reject(REJECT_POINTS_OUTSIDE,
                   f"{ch}：原始读数（最低 {min(bad):.4g} {cal_unit}）落在示值—参考值点列"
                   f"覆盖域 [{ind[0]:.4g}, {ind[-1]:.4g}] 之外，不允许外推修正",
                   version)
            continue

        # 修正值换回通道原单位
        corrected_vals = [convert_value(v, cal_unit, raw_unit, mt,
                                        range_min, range_max)
                          for v in corrected_in_cal]
        corrections = [c - r for c, r in zip(corrected_vals, raw_vals)]
        corrected[ch] = {"unit": raw_unit,
                         "points": [[t, v] for t, v in zip(ts, corrected_vals)]}

        # 修正前后差值统计（供导出/报告/比较共用）
        entry.update({
            "status": "accepted",
            "rejection_code": None, "rejection_reason": None,
            "corrected_interval": [round(min(corrected_vals), 6),
                                   round(max(corrected_vals), 6)],
            "corrected_min": round(min(corrected_vals), 6),
            "corrected_max": round(max(corrected_vals), 6),
            "mean_correction": round(sum(corrections) / len(corrections), 6),
            "max_abs_correction": round(max(abs(c) for c in corrections), 6),
            "certificate": {
                "version_id": version["calibration_version_id"],
                "instrument_serial": version["instrument_serial"],
                "valid_from": version["valid_from"],
                "valid_until": version["valid_until"],
                "certificate_summary": version["certificate_summary"],
                "certificate_digest": version["certificate_digest"],
                "range": [version["range_min"], version["range_max"]],
                "unit": cal_unit,
            },
        })
        # 抽样差值点（等距至多 8 个）
        k = len(raw_vals)
        idxs = sorted({min(k - 1, int(round(j * (k - 1) / 7.0)))
                       for j in range(min(8, k))})
        entry["shift_samples"] = [
            {"t": round(ts[i], 6), "raw": round(raw_vals[i], 6),
             "corrected": round(corrected_vals[i], 6),
             "shift": round(corrections[i], 6)} for i in idxs]
        # 标准不确定度（换算到通道原单位，供蒙特卡洛校准分量）
        try:
            u_channel = convert_uncertainty(
                version["standard_uncertainty"], cal_unit, raw_unit, mt,
                range_min, range_max)
            entry["standard_uncertainty"] = round(u_channel, 6)
            entry["standard_uncertainty_unit"] = raw_unit
            calibration_components.append({
                "channel": ch, "value": u_channel, "unit": raw_unit,
                "distribution": "normal",
                "instrument_serial": version["instrument_serial"],
                "calibration_version_id": version["calibration_version_id"],
            })
        except ValueError:
            entry["standard_uncertainty"] = None
        channels_out.append(entry)

    block = {
        "mode": "channel_chain",
        "test_kind": test_kind,
        "test_interval": ([interval[0].isoformat(), interval[1].isoformat()]
                          if interval else None),
        "test_max_t_s": round(interval[2], 6) if interval else None,
        "legacy_calibration_valid_until": None,
        "channels": channels_out,
        "rejections": rejections,
        "accepted": all(c["status"] == "accepted" for c in channels_out)
                     and len(channels_out) == len(present),
        "calibration_components": calibration_components,
        "blocking_issues": [f"[{r['code']}] {r['detail']}" for r in rejections],
    }
    return corrected, block


def _legacy_block(present, interval):
    return {
        "mode": "legacy",
        "test_interval": ([interval[0].isoformat(), interval[1].isoformat()]
                          if interval else None),
        "test_max_t_s": round(interval[2], 6) if interval else None,
        "channels": [{"channel": ch, "status": "legacy",
                      "rejection_code": None} for ch in present],
        "rejections": [], "accepted": True,
        "calibration_components": [], "blocking_issues": [],
    }


def normalize_bindings(raw):
    """提交载荷中的 calibration_bindings → {channel: version_id}。

    支持两种写法：{"position": 3} 或 [{"channel": "position",
    "calibration_version_id": 3}, ...]。None 透传（legacy）。
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return {str(k): int(v) for k, v in raw.items() if v is not None}
    if isinstance(raw, list):
        out = {}
        for item in raw:
            if isinstance(item, dict):
                out[str(item["channel"])] = int(item["calibration_version_id"])
            elif isinstance(item, (list, tuple)):
                out[str(item[0])] = int(item[1])
        return out
    raise ValueError("calibration_bindings 须为对象或数组")


def resolve_bindings(payload, prev_result, override):
    """解析本次分析使用的校准绑定。

    优先级：本次请求 override（{} 显式取消→legacy）> 沿用上一分析版本实际采用
    的绑定 > 测试提交中的声明 > None（legacy）。
    """
    if override is not None:
        return override or None
    if prev_result is not None:
        chain = prev_result.get("calibration_chain")
        if chain and chain.get("mode") == "channel_chain":
            return {c["channel"]: c["calibration_version_id"]
                    for c in chain.get("channels", [])
                    if c.get("calibration_version_id") is not None}
    return normalize_bindings(payload.get("calibration_bindings"))


def rebind_adjustment(author, bindings):
    return {"type": "calibration_rebind", "author": author,
            "bindings": dict(bindings)}


# ===================== 版本比较 =====================

def compare_chains(pre_result, post_result):
    """两个分析版本的逐通道校准比较（配对/叠加比较共用）。

    每通道给出修正量变化、采用证书（序列号/摘要/版本）是否一致；
    链模式缺失或一侧 legacy 时标记 not_evaluated。
    """
    def chain_of(r):
        return (r or {}).get("calibration_chain")

    pre, post = chain_of(pre_result), chain_of(post_result)
    entries = []
    channels = sorted({c["channel"] for ch in (pre, post) if ch
                       for c in ch.get("channels", [])})
    for ch in channels:
        pe = next((c for c in (pre or {}).get("channels", [])
                   if c["channel"] == ch), None)
        qe = next((c for c in (post or {}).get("channels", [])
                   if c["channel"] == ch), None)
        e = {"channel": ch,
             "pre_status": (pe or {}).get("status"),
             "post_status": (qe or {}).get("status"),
             "change": "not_evaluated"}
        if not pre or not post or pre.get("mode") != "channel_chain" \
                or post.get("mode") != "channel_chain":
            e["note"] = "一侧未采用逐通道校准链（legacy 模式），证书与修正量不比较"
            entries.append(e)
            continue
        pc = (pe or {}).get("certificate") or {}
        qc = (qe or {}).get("certificate") or {}
        e["pre_certificate"] = pc
        e["post_certificate"] = qc
        e["same_certificate"] = pc.get("certificate_digest") == qc.get("certificate_digest") \
            and pc.get("certificate_digest") is not None
        e["pre_mean_correction"] = (pe or {}).get("mean_correction")
        e["post_mean_correction"] = (qe or {}).get("mean_correction")
        if pe and qe and pe["status"] == "accepted" and qe["status"] == "accepted":
            e["mean_correction_delta"] = round(
                (qe.get("mean_correction") or 0) - (pe.get("mean_correction") or 0), 6)
            e["max_abs_correction_pre"] = pe.get("max_abs_correction")
            e["max_abs_correction_post"] = qe.get("max_abs_correction")
            e["change"] = "same" if e["same_certificate"] else "recalibrated"
            if not e["same_certificate"]:
                e["note"] = ("两侧采用不同证书版本"
                             + ("（续证）" if pc.get("instrument_serial") ==
                                qc.get("instrument_serial") else "（仪器/证书变更）"))
        else:
            e["note"] = "一侧通道校准被拒绝，证书与修正量不参与定量比较"
        entries.append(e)
    summary = {
        "pre_mode": (pre or {}).get("mode"),
        "post_mode": (post or {}).get("mode"),
        "n_channels": len(channels),
        "n_same_certificate": sum(1 for e in entries if e.get("same_certificate")),
        "n_recalibrated": sum(1 for e in entries if e.get("change") == "recalibrated"),
        "pre_accepted": bool(pre and pre.get("accepted")),
        "post_accepted": bool(post and post.get("accepted")),
    }
    return {"summary": summary, "channels": entries}
