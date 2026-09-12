"""测量不确定度评估（蒙特卡洛）。

对单位统一后的原始采样点按各通道声明的输入分量做误差扰动，并通过时间抖动
重新对齐/重采样，再沿与中心值分析完全相同的分段与指标管线复算行程时间、
死区、回差、过冲、稳态偏差（附运动段最低供压），输出经验区间。

设计约定：
- 分辨率/准确度/零点漂移为**每重采样一次**的系统性抽样（均匀分布），
  分辨率声明值为量化步进，扰动半宽取步进的一半（resolution=0.2% → ±0.1%，
  点间独立）；准确度、零点漂移按次整体偏置，叠加与声明半宽一致的
  ±半宽 抽样；校准标准不确定度按次正态抽样。
- 采样时间抖动为**通道时钟偏差**：每通道每重采样一次一个时钟偏置（正态），
  叠加各点独立抖动，重新对齐即完成重采样。
- 固定随机种子；同输入、同复算参数必须可复现。
- 区间（默认 95%，分位数法）跨越判定阈值时符合性为 indeterminate，
  不得只按中心值通过。
"""

import bisect
import math
import random

from .processing import alignment, metrics, segmentation
from .processing.units import PRESSURE_TO_KPA, TRAVEL_UNITS

CHANNELS = ("command", "position", "pressure")
SIGNAL_CHANNELS = ("command", "position")

# 指标键 → (中文名, 结果取值函数, 阈值键, 单位)，与 pairing 保持一致
METRIC_SPECS = [
    ("travel_time", "行程时间", "travel_time_s_max", "s"),
    ("deadband", "死区", "deadband_pct_max", "%"),
    ("hysteresis", "回差", "hysteresis_pct_max", "%"),
    ("overshoot", "过冲", "overshoot_pct_max", "%"),
    ("steady_state", "稳态偏差", "steady_state_pct_max", "%"),
]

RECT_KINDS = ("resolution", "accuracy", "zero_drift")

DEFAULT_SEED = 20260912
DEFAULT_N_SAMPLES = 120
DEFAULT_INTERVAL_PROB = 0.95
MIN_VALID_RATIO = 0.8
MIN_VALID_COUNT = 30


# ===================== 分量标准化 =====================

def _signal_factor(unit):
    """信号分量单位换算为量程百分比的乘数。"""
    u = (unit or "").strip().lower()
    if u == "%":
        return 1.0
    if u == "ma":
        return 100.0 / 16.0
    if u in TRAVEL_UNITS:
        return None  # 需要量程跨度
    return False  # 不支持的单位


def _component_normalized(comp, channel, span):
    """把一个分量换算到通道归一化量纲（信号=%，压力=kPa，时间=s）。

    返回 (sigma_or_halfwidth, is_halfwidth)；单位冲突抛 ValueError。
    """
    u = (comp["unit"] or "").strip().lower()
    v = float(comp["value"])
    distribution = comp.get("distribution") or (
        "normal" if comp["kind"] == "calibration" else "rectangular")
    if channel == "pressure":
        if u == "s":
            raise ValueError(f"pressure: 分量 {comp['kind']} 单位 {comp['unit']} 与压力量纲冲突")
        factor = PRESSURE_TO_KPA.get(u)
        if factor is None:
            raise ValueError(f"pressure: 分量 {comp['kind']} 单位 {comp['unit']} 与压力量纲冲突")
        return v * factor, distribution
    # 指令/阀位：% / mA / 物理行程
    if u == "s":
        raise ValueError(f"{channel}: 分量 {comp['kind']} 单位 s 与阀位信号量纲冲突，"
                         "时钟抖动应使用 time_jitter_s")
    factor = _signal_factor(u)
    if factor is False:
        raise ValueError(f"{channel}: 分量 {comp['kind']} 单位 {comp['unit']} "
                         "与信号量纲冲突（允许 %、mA、mm/cm/in）")
    if factor is None:
        if not span or span <= 0:
            raise ValueError(f"{channel}: 量程跨度为 0，无法把 {comp['unit']} 分量换算为 %")
        factor = 100.0 / span
    return v * factor, distribution


def _calibration_range_to_pct(cal, channel, series_units, span, range_min):
    """校准覆盖范围换算为归一化量纲，返回 (lo, hi)；无法换算抛 ValueError。"""
    ru = (cal.get("range_unit") or cal["unit"] or "").strip().lower()
    lo, hi = cal.get("range_min"), cal.get("range_max")
    if lo is None or hi is None:
        raise ValueError("校准不确定度未提供覆盖范围（range_min/range_max），无法确认覆盖")
    if channel == "pressure":
        factor = PRESSURE_TO_KPA.get(ru)
        if factor is None:
            raise ValueError(f"校准范围单位 {cal.get('range_unit')} 与压力量纲冲突")
        return lo * factor, hi * factor
    factor = _signal_factor(ru)
    if factor is False:
        raise ValueError(f"校准范围单位 {cal.get('range_unit')} 与信号量纲冲突")
    if ru == "ma":
        # 4-20mA → 0-100%
        return (lo - 4.0) / 16.0 * 100.0, (hi - 4.0) / 16.0 * 100.0
    if factor is None:
        # 物理行程：按量程起点归一
        if not span or span <= 0:
            raise ValueError("量程跨度为 0，无法把校准范围换算为 %")
        return (lo - range_min) / span * 100.0, (hi - range_min) / span * 100.0
    return lo * factor, hi * factor


# ===================== 输入装配 =====================

def _build_channel_inputs(raw_norm, source_units, rng_spec, cal,
                          calibration_components=None):
    """把声明分量装配为每通道扰动参数。

    calibration_components: 逐通道校准链冻结版本的标准不确定度分量
    [{channel, value, unit, distribution="normal", instrument_serial,
      calibration_version_id}, ...]。通道一旦绑定冻结证书版本，其校准
    standard_uncertainty 只取该证书值：旧通用 calibration 块与逐通道
    calibration 分量都不得覆盖（跳过并在记录中标注 suppressed_by_chain）；
    其余未绑定通道继续兼容旧字段。
    返回 (channel_inputs, component_records, reasons)。
    任一分量单位冲突 → 整体无效（reasons 非空）。
    """
    span = rng_spec["max"] - rng_spec["min"]
    inputs = {c: {"components": [], "time_jitter_s": 0.0} for c in CHANNELS}
    records = []
    reasons = []

    # 绑定冻结证书版本的通道：校准标准不确定度以证书为准，旧配置被抑制
    chain_bound = {}
    for cc in (calibration_components or []):
        if cc.get("channel") in CHANNELS:
            chain_bound[cc["channel"]] = cc

    def add_component(channel, comp, source):
        # 绑定冻结证书版本的通道：旧 calibration 分量不得覆盖证书标准不确定度
        if comp["kind"] == "calibration" and channel in chain_bound:
            cert = chain_bound[channel]
            records.append({
                "channel": channel, "kind": "calibration",
                "source": source, "status": "suppressed_by_chain",
                "declared": {"value": comp["value"], "unit": comp["unit"]},
                "note": ("该通道已冻结校准版本 "
                         f"#{cert.get('calibration_version_id')}"
                         f"（{cert.get('instrument_serial')}），"
                         "旧校准分量以证书 standard_uncertainty 为准，不参与扰动"),
            })
            return
        try:
            scale, distribution = _component_normalized(comp, channel, span)
        except ValueError as e:
            reasons.append(f"分量单位冲突：{e}")
            return
        if scale <= 0:
            return
        declared_scale = scale
        # resolution 的声明值为量化步进（如 0.2%），均匀量化的扰动半宽为步进/2
        # （±0.1%）；accuracy/zero_drift 的声明值本身即允许误差半宽，不折半。
        if comp["kind"] == "resolution":
            scale = scale / 2.0
        inputs[channel]["components"].append({
            "kind": comp["kind"], "scale": scale, "distribution": distribution,
            "per_point": comp["kind"] == "resolution",
        })
        records.append({
            "channel": channel, "kind": comp["kind"], "source": source,
            "declared": {"value": comp["value"], "unit": comp["unit"],
                         "distribution": distribution},
            "normalized_scale": round(declared_scale, 6),
            "perturbation_halfwidth": round(scale, 6),
            "normalized_unit": "kPa" if channel == "pressure" else (
                "s" if comp["unit"].strip().lower() == "s" else "%_of_span"),
            "distribution": distribution,
        })

    channels = (rng_spec.get("channels") or {})
    for ch in CHANNELS:
        cu = channels.get(ch)
        if not cu:
            continue
        for key in ("resolution", "accuracy", "zero_drift", "calibration"):
            comp = cu.get(key)
            if comp:
                add_component(ch, comp, "channel")
        jitter = float(cu.get("time_jitter_s") or 0.0)
        if jitter > 0:
            inputs[ch]["time_jitter_s"] = jitter
            records.append({
                "channel": ch, "kind": "time_jitter", "source": "channel",
                "declared": {"value": jitter, "unit": "s", "distribution": "normal"},
                "normalized_scale": round(jitter, 6), "normalized_unit": "s",
                "distribution": "normal",
            })

    if cal:
        comp = {"kind": "calibration", "value": cal["value"], "unit": cal["unit"],
                "distribution": cal.get("distribution", "normal")}
        ru = (cal.get("range_unit") or cal["unit"] or "").strip().lower()
        for ch in cal.get("applies_to") or CHANNELS:
            # 已绑定冻结证书的通道：旧通用 calibration 块不覆盖该通道
            # （含覆盖范围检查），其校准标准不确定度只取证书值
            if ch in chain_bound:
                records.append({
                    "channel": ch, "kind": "calibration",
                    "source": "calibration", "status": "suppressed_by_chain",
                    "declared": {"value": cal["value"], "unit": cal["unit"]},
                    "note": ("该通道已冻结校准版本 "
                             f"#{chain_bound[ch].get('calibration_version_id')}"
                             f"（{chain_bound[ch].get('instrument_serial')}），"
                             "旧通用 calibration 块不覆盖该通道"),
                })
                continue
            # 校准分量单位须与应用通道量纲一致
            try:
                _component_normalized(comp, ch, span)
            except ValueError as e:
                reasons.append(f"校准范围/分量与通道量纲冲突（应用于 {ch}）：{e}")
                continue
            # 覆盖范围检查
            try:
                lo, hi = _calibration_range_to_pct(cal, ch, source_units, span, rng_spec["min"])
            except ValueError as e:
                reasons.append(f"校准范围不覆盖：{ch}：{e}")
                continue
            ts, vs = raw_norm[ch]
            obs_lo, obs_hi = min(vs), max(vs)
            if obs_lo < lo or obs_hi > hi:
                reasons.append(
                    f"校准范围不覆盖：{ch} 通道观测区间 "
                    f"[{obs_lo:.3f}, {obs_hi:.3f}] 超出校准覆盖 "
                    f"[{lo:.3f}, {hi:.3f}]（归一化单位）")
                continue
            add_component(ch, comp, "calibration")
            for r in records:
                if r["channel"] == ch and r["kind"] == "calibration" and r["source"] == "calibration":
                    r["coverage"] = {"min": round(lo, 4), "max": round(hi, 4),
                                     "observed_min": round(obs_lo, 4),
                                     "observed_max": round(obs_hi, 4)}

    # 逐通道校准链冻结版本的标准不确定度（链模式）。绑定通道一律注入证书值；
    # 其旧 calibration 分量已在上面被抑制，不会重复计列。
    for ch, cc in chain_bound.items():
        comp = {"kind": "calibration", "value": cc["value"], "unit": cc.get("unit"),
                "distribution": cc.get("distribution", "normal")}
        try:
            scale, distribution = _component_normalized(comp, ch, span)
        except ValueError as e:
            reasons.append(f"校准链标准不确定度冲突：{e}")
            continue
        if scale <= 0:
            continue
        inputs[ch]["components"].append({
            "kind": "calibration", "scale": scale, "distribution": distribution,
            "per_point": False})
        records.append({
            "channel": ch, "kind": "calibration", "source": "calibration_chain",
            "declared": {"value": cc["value"], "unit": cc.get("unit"),
                         "distribution": distribution},
            "normalized_scale": round(scale, 6),
            "perturbation_halfwidth": round(scale, 6),
            "normalized_unit": "kPa" if ch == "pressure" else "%_of_span",
            "distribution": distribution,
            "instrument_serial": cc.get("instrument_serial"),
            "calibration_version_id": cc.get("calibration_version_id"),
        })
    return inputs, records, reasons


def has_any_component(channel_inputs):
    return any(cin["components"] or cin["time_jitter_s"] > 0
               for cin in channel_inputs.values())


# ===================== 单次重采样 =====================

def _perturb_channel(ts, vs, cin, rng):
    """对一路信号做值与时标扰动，返回扰动后的 (ts, vs)。"""
    comps = cin["components"]
    # 系统性（每次抽样）偏置
    bias = 0.0
    per_point_scales = []
    for c in comps:
        if c["per_point"]:
            per_point_scales.append(c["scale"])
            continue
        if c["distribution"] == "normal":
            bias += rng.gauss(0.0, c["scale"])
        else:
            bias += rng.uniform(-c["scale"], c["scale"])
    if per_point_scales or bias != 0.0:
        if per_point_scales:
            # 多个量化/点间分量叠加：按独立均匀变量合成标准差后仍按均匀抽样近似
            half = math.sqrt(sum(s * s for s in per_point_scales))
            new_vs = [v + bias + rng.uniform(-half, half) for v in vs]
        else:
            new_vs = [v + bias for v in vs]
    else:
        new_vs = list(vs)
    jitter = cin["time_jitter_s"]
    if jitter > 0:
        clock_bias = rng.gauss(0.0, jitter)  # 通道时钟偏差（系统性）
        new_ts = [t + clock_bias + rng.gauss(0.0, jitter) for t in ts]
        # 时标可能因抖动乱序：按时间排序后再对齐
        if any(new_ts[i] < new_ts[i - 1] for i in range(1, len(new_ts))):
            order = sorted(range(len(new_ts)), key=lambda i: new_ts[i])
            new_ts = [new_ts[i] for i in order]
            new_vs = [new_vs[i] for i in order]
    else:
        new_ts = list(ts)
    return new_ts, new_vs


def _median(vals):
    s = sorted(vals)
    return s[len(s) // 2]


def _fast_interp(ts, vs, t):
    """线性插值（bisect），t 超界取端点。仅用于蒙特卡洛热路径。"""
    if t <= ts[0]:
        return vs[0]
    if t >= ts[-1]:
        return vs[-1]
    i = bisect.bisect_right(ts, t) - 1
    if i >= len(ts) - 1:
        return vs[-1]
    span = ts[i + 1] - ts[i]
    if span <= 0:
        return vs[i]
    return vs[i] + (vs[i + 1] - vs[i]) * (t - ts[i]) / span


def _fast_align(series):
    """alignment.align 的蒙特卡洛快速版：同一网格规则，插值用 bisect。"""
    t0 = max(series[c][0][0] for c in CHANNELS)
    t1 = min(series[c][0][-1] for c in CHANNELS)
    if t1 <= t0:
        raise ValueError("各通道时间轴无重叠区间，无法对齐")
    step = min(_median([b - a for a, b in zip(series[c][0], series[c][0][1:])])
               for c in CHANNELS)
    step = max(0.02, min(1.0, step))
    n = int((t1 - t0) / step) + 1
    grid = [t0 + i * step for i in range(n)]
    aligned = {}
    for c in CHANNELS:
        ts, vs = series[c]
        aligned[c] = [_fast_interp(ts, vs, t) for t in grid]
    return grid, aligned


def _interp_by_cmd(points, c):
    """metrics._interp_by_cmd 的 bisect 快速版。"""
    if c <= points[0][0]:
        return points[0][1]
    if c >= points[-1][0]:
        return points[-1][1]
    xs = [p[0] for p in points]
    i = bisect.bisect_right(xs, c) - 1
    c0, p0 = points[i]
    c1, p1 = points[i + 1]
    if c1 - c0 < 1e-9:
        return p0
    return p0 + (p1 - p0) * (c - c0) / (c1 - c0)


def _fast_hysteresis(cmd, pos, segments, settle_band_pct, step_pct=1.0):
    """metrics.hysteresis 的快速版（排序插值用 bisect），返回 max_pct 或 None。"""
    qs_band = max(5.0, 2.0 * settle_band_pct)
    opening, closing = [], []
    for seg in segments:
        if seg["type"] not in ("opening", "closing"):
            continue
        for i in range(seg["i_start"], seg["i_end"] + 1):
            if abs(pos[i] - cmd[i]) > qs_band:
                continue
            (opening if seg["type"] == "opening" else closing).append((cmd[i], pos[i]))
    opening = metrics._sorted_unique(opening)
    closing = metrics._sorted_unique(closing)
    if not opening or not closing:
        return None
    lo = max(opening[0][0], closing[0][0])
    hi = min(opening[-1][0], closing[-1][0])
    if hi <= lo:
        return None
    best = 0.0
    n = max(2, int((hi - lo) / step_pct) + 1)
    for k in range(n):
        c = lo + (hi - lo) * k / (n - 1)
        po = _interp_by_cmd(opening, c)
        pc = _interp_by_cmd(closing, c)
        d = abs(po - pc)
        if d > best:
            best = d
    return best


def _trial_metrics(raw_norm, channel_inputs, thresholds, manual_boundaries, rng):
    """一次蒙特卡洛重采样：扰动 → 对齐（重采样）→ 分段 → 指标。失败抛 ValueError。"""
    perturbed = {}
    for ch in CHANNELS:
        ts, vs = raw_norm[ch]
        perturbed[ch] = _perturb_channel(ts, vs, channel_inputs[ch], rng)
    grid, aligned = _fast_align(perturbed)
    cmd, pos, prs = aligned["command"], aligned["position"], aligned["pressure"]
    segments, _ = segmentation.segment(
        grid, cmd, manual_boundaries=manual_boundaries)
    settle = thresholds["settle_band_pct"]
    mt = metrics.travel_time(grid, cmd, pos, segments, settle)
    md = metrics.deadband(grid, cmd, pos, segments)
    hyst_max = _fast_hysteresis(cmd, pos, segments, settle)
    mo = metrics.overshoot(grid, cmd, pos, segments)
    ms = metrics.steady_state_deviation(grid, cmd, pos, segments)
    # 运动段最低供压（供压精度的传播）
    pmin = None
    for seg in segments:
        if seg["type"] not in ("opening", "closing"):
            continue
        seg_min = min(prs[seg["i_start"]:seg["i_end"] + 1])
        pmin = seg_min if pmin is None else min(pmin, seg_min)
    return {
        "travel_time": mt["max_s"],
        "deadband": md["max_pct"],
        "hysteresis": round(hyst_max, 3) if hyst_max is not None else None,
        "overshoot": mo["max_pct"],
        "steady_state": ms["max_pct"],
        "supply_pressure_min": pmin,
    }


# ===================== 区间与判定 =====================

def _quantile(sorted_vals, q):
    """线性插值分位数（与 numpy 'linear' 一致）。"""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _interval(values, prob):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    alpha = (1.0 - prob) / 2.0
    return [round(_quantile(vals, alpha), 4), round(_quantile(vals, 1.0 - alpha), 4)]


def _conformance(interval, limit, lower_is_better=True):
    """区间相对判定阈值的符合性。

    pass：整个区间在限内；fail：整个区间超限；indeterminate：区间跨越限值
    （含端点恰好等于限值——贴限值不允许只按中心值通过）。
    """
    if interval is None or limit is None:
        return "indeterminate"
    lo, hi = interval
    if lower_is_better:
        if hi <= limit:
            return "pass" if hi < limit else "indeterminate"
        if lo > limit:
            return "fail"
        return "indeterminate"
    else:
        if lo >= limit:
            return "pass" if lo > limit else "indeterminate"
        if hi < limit:
            return "fail"
        return "indeterminate"


# ===================== 主入口 =====================

def not_evaluated(reason="未提供测量不确定度输入分量（分辨率/准确度/零点漂移/时钟抖动/校准均缺省），"
                         "指标仅给出中心值，测量不确定度未评估"):
    return {"status": "not_evaluated", "reason": reason, "metrics": {},
            "components": [], "recompute_params": None}


def evaluate(raw_norm, source_units, range_spec, thresholds, manual_boundaries,
             input_spec, central_metrics=None, calibration_components=None):
    """执行蒙特卡洛不确定度评估。

    raw_norm: normalize_series 的输出（含 command/position/pressure 的 (ts, vs)）
    input_spec: UncertaintyInput.model_dump() 或 None
    calibration_components: 逐通道校准链冻结版本的标准不确定度分量（链模式），
    无显式输入时据此自动评估；central_metrics: 中心值分析的 metrics dict。
    返回不确定度结果块（见 README/导出 JSON）。
    """
    if not input_spec and not calibration_components:
        return not_evaluated()

    n_samples = int((input_spec or {}).get("n_samples") or DEFAULT_N_SAMPLES)
    seed = int((input_spec or {}).get("seed", DEFAULT_SEED))
    prob = float((input_spec or {}).get("interval_prob") or DEFAULT_INTERVAL_PROB)
    rng_spec = {"min": range_spec["min"], "max": range_spec["max"],
                "channels": (input_spec or {}).get("channels") or {}}

    channel_inputs, components, reasons = _build_channel_inputs(
        raw_norm, source_units, rng_spec,
        (input_spec or {}).get("calibration"),
        calibration_components=calibration_components)

    if reasons:
        return {
            "status": "invalid",
            "reason": "存在输入冲突，未执行蒙特卡洛评估",
            "reasons": reasons,
            "components": components,
            "input_spec": _resolved_input(input_spec),
            "metrics": {},
            "n_samples_requested": n_samples,
            "n_samples_valid": 0,
            "seed": seed,
            "interval_prob": prob,
            "recompute_params": _recompute_params(
                n_samples, seed, prob, manual_boundaries),
        }

    if not has_any_component(channel_inputs):
        return not_evaluated()

    # 固定种子的独立随机流：同输入可复现
    rng = random.Random(seed)
    samples = []
    n_failed = 0
    fail_detail = {}
    for _ in range(n_samples):
        try:
            samples.append(_trial_metrics(
                raw_norm, channel_inputs, thresholds, manual_boundaries, rng))
        except ValueError as e:
            n_failed += 1
            fail_detail[str(e)] = fail_detail.get(str(e), 0) + 1

    n_valid = len(samples)
    min_required = max(MIN_VALID_COUNT, int(math.ceil(MIN_VALID_RATIO * n_samples)))
    insufficient = []
    if n_valid < min_required:
        insufficient.append(
            f"有效重采样不足：{n_valid}/{n_samples}，要求至少 {min_required}"
            f"（≥{MIN_VALID_COUNT} 且 ≥{int(MIN_VALID_RATIO * 100)}%）")

    metric_results = {}
    for key, name, thr_key, unit in METRIC_SPECS:
        limit = thresholds.get(thr_key)
        vals = [s[key] for s in samples if s[key] is not None]
        central = None
        if central_metrics and key in central_metrics:
            central = central_metrics[key].get("max_pct") if key != "travel_time" \
                else central_metrics[key].get("max_s")
        if central is None:
            metric_results[key] = {
                "name": name, "unit": unit, "central": None,
                "interval": None, "status": "not_applicable",
                "threshold": limit, "n_valid": len(vals),
                "note": "中心值分析无该指标（数据不适用），不参与区间判定"}
            continue
        if len(vals) < MIN_VALID_COUNT:
            metric_results[key] = {
                "name": name, "unit": unit, "central": round(central, 4),
                "interval": None, "status": "indeterminate",
                "threshold": limit, "n_valid": len(vals),
                "note": f"该指标有效重采样 {len(vals)} < {MIN_VALID_COUNT}，无法给出区间"}
            continue
        interval = _interval(vals, prob)
        status = _conformance(interval, limit)
        entry = {"name": name, "unit": unit, "central": round(central, 4),
                 "interval": interval, "status": status,
                 "threshold": limit, "n_valid": len(vals)}
        if status == "indeterminate":
            entry["note"] = f"{prob * 100:.0f}% 区间跨越判定限值 {limit}（含贴限值情形），符合性不确定"
        metric_results[key] = entry

    # 运动段最低供压（下侧阈值：越大越安全）
    pvals = [s["supply_pressure_min"] for s in samples
             if s["supply_pressure_min"] is not None]
    pressure_entry = None
    if pvals and len(pvals) >= MIN_VALID_COUNT:
        p_interval = _interval(pvals, prob)
        p_status = _conformance(p_interval, thresholds["supply_pressure_min_kpa"],
                                lower_is_better=False)
        pressure_entry = {
            "name": "运动段最低供压", "unit": "kPa",
            "interval": p_interval, "status": p_status,
            "threshold": thresholds["supply_pressure_min_kpa"],
            "n_valid": len(pvals),
        }

    statuses = [m["status"] for m in metric_results.values()]
    if insufficient:
        overall = "indeterminate"
    elif any(s == "fail" for s in statuses):
        overall = "fail"
    elif any(s == "indeterminate" for s in statuses):
        overall = "indeterminate"
    else:
        overall = "pass"

    return {
        "status": "evaluated",
        "overall_status": overall,
        "reason": "" if not insufficient else "; ".join(insufficient),
        "reasons": insufficient,
        "input_spec": _resolved_input(input_spec),
        "components": components,
        "metrics": metric_results,
        "supply_pressure": pressure_entry,
        "n_samples_requested": n_samples,
        "n_samples_valid": n_valid,
        "n_samples_failed": n_failed,
        "resample_failures": fail_detail,
        "seed": seed,
        "interval_prob": prob,
        "note": (input_spec or {}).get("note", ""),
        "recompute_params": _recompute_params(n_samples, seed, prob, manual_boundaries),
    }


def _resolved_input(input_spec):
    """解析后的输入回显（复现用；人工调整后另存版本时沿用同一组分量）。"""
    if not input_spec:
        return None
    return {
        "channels": input_spec.get("channels") or {},
        "calibration": input_spec.get("calibration"),
        "n_samples": int(input_spec.get("n_samples") or DEFAULT_N_SAMPLES),
        "seed": int(input_spec.get("seed", DEFAULT_SEED)),
        "interval_prob": float(input_spec.get("interval_prob") or DEFAULT_INTERVAL_PROB),
        "note": (input_spec or {}).get("note", ""),
    }


def _recompute_params(n_samples, seed, prob, manual_boundaries):
    """JSON 导出与打印页共用的同一组复算参数。"""
    return {
        "method": "monte_carlo",
        "pipeline": "perturb_raw_points -> realign_resample -> resegment -> metrics",
        "n_samples": n_samples,
        "seed": seed,
        "interval_prob": prob,
        "quantiles": [round((1 - prob) / 2, 5), round(1 - (1 - prob) / 2, 5)],
        "manual_boundaries": [dict(m) for m in (manual_boundaries or [])],
    }


# ===================== 检修前后差值区间 =====================

def compare_intervals(pre_block, post_block, prob=DEFAULT_INTERVAL_PROB):
    """根据两侧不确定度区间构造指标差值区间并判定改善/退化。

    delta = 检修前 − 检修后（对供压为 后−前，方向另列）。只有差值区间
    整体越过零点才标记明确改善/退化；跨零为 indeterminate；
    任一侧未评估则 not_evaluated。
    """
    out = []
    pre_ok = pre_block and pre_block.get("status") == "evaluated"
    post_ok = post_block and post_block.get("status") == "evaluated"
    for key, name, thr_key, unit in METRIC_SPECS:
        entry = {"metric": key, "name": name, "unit": unit}
        if not (pre_ok and post_ok):
            entry.update({
                "delta_interval": None,
                "change": "not_evaluated",
                "note": "一侧或两侧测量不确定度未评估，差值区间不适用",
            })
            out.append(entry)
            continue
        pm = pre_block["metrics"].get(key) or {}
        qm = post_block["metrics"].get(key) or {}
        pi, qi = pm.get("interval"), qm.get("interval")
        if not pi or not qi:
            entry.update({
                "delta_interval": None,
                "change": "indeterminate",
                "note": "一侧指标无有效区间（数据不适用或有效重采样不足）",
            })
            out.append(entry)
            continue
        # 独立重采样差值的极值区间（保守包络）
        d_lo = round(pi[0] - qi[1], 4)
        d_hi = round(pi[1] - qi[0], 4)
        if d_lo > 0:
            change = "improved"      # 检修后指标整体变小
        elif d_hi < 0:
            change = "degraded"
        else:
            change = "indeterminate"
        entry.update({
            "pre_interval": pi, "post_interval": qi,
            "delta_interval": [d_lo, d_hi],
            "change": change,
            "post_conformance": qm.get("status"),
        })
        out.append(entry)
    return out
