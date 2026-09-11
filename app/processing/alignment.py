"""时间轴对齐：不同采样频率的通道插值到公共等距网格。"""


def median_dt(ts):
    dts = sorted(b - a for a, b in zip(ts, ts[1:]))
    if not dts:
        return 1.0
    return dts[len(dts) // 2]


def _interp(ts, vs, t):
    """线性插值；t 超出范围时取端点值。"""
    if t <= ts[0]:
        return vs[0]
    if t >= ts[-1]:
        return vs[-1]
    lo, hi = 0, len(ts) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ts[mid] <= t:
            lo = mid
        else:
            hi = mid
    span = ts[hi] - ts[lo]
    if span <= 0:
        return vs[lo]
    f = (t - ts[lo]) / span
    return vs[lo] + f * (vs[hi] - vs[lo])


def align(series, channels=("command", "position", "pressure")):
    """series: {channel: (ts, values)}。对齐到公共网格。

    网格步长取各通道采样间隔中位数的最小值，限制在 [0.02, 1.0] s；
    时间窗取各通道的重叠区间。channels 指定参与对齐的通道（故障安全测试
    额外纳入跳闸接点 trip）。返回 (grid, aligned, meta)。
    """
    channels = tuple(c for c in channels if c in series)
    t0 = max(min(series[c][0]) for c in channels)
    t1 = min(max(series[c][0]) for c in channels)
    if t1 <= t0:
        raise ValueError("各通道时间轴无重叠区间，无法对齐")
    step = min(median_dt(series[c][0]) for c in channels)
    step = max(0.02, min(1.0, step))
    n = int((t1 - t0) / step) + 1
    grid = [t0 + i * step for i in range(n)]
    aligned = {}
    for c in channels:
        ts, vs = series[c]
        aligned[c] = [_interp(ts, vs, t) for t in grid]
    meta = {
        "t0": round(t0, 6),
        "t1": round(t1, 6),
        "grid_step_s": round(step, 6),
        "n_points": n,
        "source_dt_s": {c: round(median_dt(series[c][0]), 6) for c in channels},
    }
    return grid, aligned, meta
