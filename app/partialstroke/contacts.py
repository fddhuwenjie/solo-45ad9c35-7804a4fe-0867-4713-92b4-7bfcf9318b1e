"""许可/中止离散接点：状态规整与有效区间提取。

接点原始值约定 0/1（单位 bool/di/空串）；中间电平与越界值属于**非法状态**，
逐段标出原始样本区间，该段状态未知，不参与许可/中止判定。

许可接点逻辑态：1=许可有效；中止接点逻辑态：1=请求中止。
本模块不依赖其他诊断能力包（与限位开关的接点规整同构，各自实现，
避免能力包之间互相越界依赖）。
"""

FRACTION_LOW = 0.3    # <=0.3 视为 0
FRACTION_HIGH = 0.7   # >=0.7 视为 1

CONTACT_UNITS = ("", "bool", "di", "digital", "0/1")


def normalize_contact(points, unit, name):
    """把接点点序列规整为 0/1/None（None=非法状态）。

    返回 (ts, bits, notes, conflicts, illegal)：
    - conflicts：阻断性单位冲突（单位声明不属于接点族）；
    - illegal：非法状态区间 [{start_index, end_index, t_start, t_end,
      n_samples, sample_values}]，中间电平与越界值逐段合并。
    """
    u = (unit or "").strip().lower()
    ts = [float(p[0]) for p in points]
    if u not in CONTACT_UNITS:
        return ts, [None] * len(points), [], \
               [f"{name}: 不支持的接点单位 {unit!r}（允许 bool/di）"], []
    bits, notes, illegal = [], [], []
    snapped = 0
    run = None  # 当前非法段 [start_index, values]
    for i, p in enumerate(points):
        v = float(p[1])
        bad = None
        if v < -0.1 or v > 1.1:
            bad = "越界"
        elif FRACTION_LOW < v < FRACTION_HIGH:
            bad = "中间电平"
        if bad is not None:
            bits.append(None)
            if run is None:
                run = [i, []]
            run[1].append(round(v, 4))
            continue
        if run is not None:
            illegal.append(_illegal_record(ts, run, i - 1))
            run = None
        bits.append(0 if v <= FRACTION_LOW else 1)
        if v not in (0.0, 1.0):
            snapped += 1
    if run is not None:
        illegal.append(_illegal_record(ts, run, len(points) - 1))
    if snapped:
        notes.append(f"{name}: {snapped} 个采样为非严格 0/1 值，已按电平阈值规整")
    return ts, bits, notes, [], illegal


def _illegal_record(ts, run, i_end):
    i_start, values = run
    return {
        "start_index": i_start,
        "end_index": i_end,
        "t_start": round(ts[i_start], 6),
        "t_end": round(ts[i_end], 6),
        "n_samples": len(values),
        "sample_values": values[:8],
    }


def active_intervals(ts, bits):
    """提取逻辑态为 1 的连续区间（非法样本打断区间）。

    返回 [{t_start, t_end, i_start, i_end, open_end}]：
    区间延伸到记录末尾时 open_end=True（其后状态未知）。
    """
    out = []
    i, n = 0, len(bits)
    while i < n:
        if bits[i] == 1:
            j = i
            while j + 1 < n and bits[j + 1] == 1:
                j += 1
            out.append({"t_start": round(ts[i], 6), "t_end": round(ts[j], 6),
                        "i_start": i, "i_end": j, "open_end": j == n - 1})
            i = j + 1
        else:
            i += 1
    return out


def inactive_intervals(ts, bits, t_lo, t_hi):
    """[t_lo, t_hi] 内逻辑态不为 1 的区间（含非法段；非法段状态未知按无效处理）。

    返回 [{t_start, t_end, i_start, i_end}]，供“许可缺失”标注原始区间。
    """
    out = []
    run = None
    for i, (t, b) in enumerate(zip(ts, bits)):
        if t < t_lo - 1e-9 or t > t_hi + 1e-9:
            continue
        if b == 1:
            if run is not None:
                out.append(run)
                run = None
            continue
        if run is None:
            run = {"t_start": round(t, 6), "i_start": i}
        run["t_end"] = round(t, 6)
        run["i_end"] = i
    if run is not None:
        out.append(run)
    return out


def state_at(ts, bits, t):
    """时刻 t 的逻辑状态（取最近一个 ≤t 的样本；无则取首个样本）。"""
    if not ts:
        return None
    if t <= ts[0]:
        return bits[0]
    if t >= ts[-1]:
        return bits[-1]
    lo, hi = 0, len(ts) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ts[mid] <= t:
            lo = mid
        else:
            hi = mid
    return bits[lo]
