"""开/关到位离散接点：状态规整、极性归一、去抖与有效边沿提取。

接点原始值约定 0/1（单位 bool/di/空串）；中间电平与越界值属于**非法状态**，
逐段标出原始样本区间并不参与边沿提取。逻辑态统一定义为“到位有效”：
常开（NO）raw=1 → 有效；常闭（NC）raw=0 → 有效。

去抖按脉冲簇处理：相邻有效段间隔 ≤ 去抖时长时合并为一个有效区间，
簇内含多段记为抖动（chatter）；持续不足去抖时长的孤立脉冲为毛刺
（glitch），不采用为边沿但留痕，供技术人员复核后按理由忽略。
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
        if v <= FRACTION_LOW:
            bits.append(0)
        else:
            bits.append(1)
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


def apply_polarity(bits, polarity):
    """原始 0/1/None → 逻辑“到位有效” 1/0/None。NO：1 有效；NC：0 有效。"""
    if polarity == "NC":
        return [None if b is None else 1 - b for b in bits]
    return list(bits)


def state_at(ts, bits, t):
    """时刻 t 的逻辑状态（取最近一个 ≤t 的样本；无则取首个样本）。"""
    if not ts:
        return None
    lo, hi = 0, len(ts) - 1
    if t <= ts[0]:
        return bits[0]
    if t >= ts[-1]:
        return bits[-1]
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ts[mid] <= t:
            lo = mid
        else:
            hi = mid
    return bits[lo]


def extract_intervals(ts, bits, debounce_s):
    """去抖并提取有效动作区间。

    返回 (adopted, glitches, chatters)：
    - adopted：有效区间 [{t_start, t_end, i_start, i_end, n_runs,
      release_t, release_i, release_observed, active_until}]。
      release_t 为区间后首个有效 0 样本时刻（释放沿）；区间延伸到记录
      末尾、或其后紧邻样本为非法状态（None）时为 None——非法样本不得
      结束有效边沿，该区间不产生释放沿。active_until 为有效状态保守
      延伸的终点（其后首个有效样本时刻，无则 None 表示至记录末），
      仅供互斥/次序判定的状态查询，不生成边沿；
    - glitches：孤立短脉冲（持续 < 去抖时长），不采用为边沿；
    - chatters：簇内含多段的抖动（含毛刺簇），只留痕不影响采用。
    """
    runs = []
    i, n = 0, len(bits)
    while i < n:
        if bits[i] == 1:
            j = i
            while j + 1 < n and bits[j + 1] == 1:
                j += 1
            runs.append((i, j))
            i = j + 1
        else:
            i += 1
    if not runs:
        return [], [], []

    clusters = [[runs[0]]]
    for run in runs[1:]:
        gap = ts[run[0]] - ts[clusters[-1][-1][1]]
        if gap <= debounce_s + 1e-9:
            clusters[-1].append(run)
        else:
            clusters.append([run])

    adopted, glitches, chatters = [], [], []
    for cl in clusters:
        i0 = cl[0][0]
        i1 = cl[-1][1]
        t0, t1 = ts[i0], ts[i1]
        n_runs = len(cl)
        if n_runs > 1:
            # 抖动区间止于首个长稳态段（持续 ≥ 去抖时长）的起点：
            # 抖动是过渡期的反复弹跳，其后稳定有效的长段不属于抖动
            bounce_i1 = i1
            for a, b in cl:
                if ts[b] - ts[a] >= debounce_s - 1e-9:
                    bounce_i1 = a
                    break
            chatters.append({
                "t_start": round(t0, 6), "t_end": round(ts[bounce_i1], 6),
                "i_start": i0, "i_end": bounce_i1, "n_pulses": n_runs,
                "pulse_times": [round(ts[a], 6) for a, _ in cl],
            })
        duration = t1 - t0
        if n_runs == 1 and duration < debounce_s - 1e-9:
            glitches.append({
                "t_start": round(t0, 6), "t_end": round(t1, 6),
                "i_start": i0, "i_end": i1, "duration_s": round(duration, 6),
            })
            continue
        # 释放沿仅当区间后紧邻样本为有效 0 时才可定位；非法（None）样本
        # 不得生成或结束有效边沿——此时释放时刻不可定位（release_t=None，
        # 不进入指标计算），有效状态保守延伸到其后首个有效样本（active_until）。
        j = i1 + 1
        release_t = release_i = None
        release_observed = False
        active_until = float("inf")
        if j < n:
            if bits[j] == 0:
                release_t, release_i = round(ts[j], 6), j
                release_observed = True
                active_until = ts[j]
            elif bits[j] is None:
                k = j
                while k < n and bits[k] is None:
                    k += 1
                if k < n:
                    active_until = ts[k]
            else:
                active_until = ts[j]
        rec = {
            "t_start": round(t0, 6), "t_end": round(t1, 6),
            "i_start": i0, "i_end": i1, "n_runs": n_runs,
            "release_t": release_t,
            "release_i": release_i,
            "release_observed": release_observed,
            "active_until": (round(active_until, 6)
                             if active_until != float("inf") else None),
        }
        adopted.append(rec)
    return adopted, glitches, chatters


def interp_at(ts, vs, t):
    """线性插值；t 超出范围返回 None（不外推）。"""
    if not ts or t < ts[0] or t > ts[-1]:
        return None
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


def slope_at(ts, vs, t, half_win_s=0.6):
    """时刻 t 附近窗口内的最小二乘斜率（%/s）；数据不足返回 None。"""
    pts = [(x, y) for x, y in zip(ts, vs) if t - half_win_s <= x <= t + half_win_s]
    if len(pts) < 2:
        # 放宽：取 t 前后各最近一点
        before = [(x, y) for x, y in zip(ts, vs) if x <= t]
        after = [(x, y) for x, y in zip(ts, vs) if x >= t]
        pts = (before[-1:] + after[:1]) or pts
    if len(pts) < 2:
        return None
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    if sxx <= 0:
        return 0.0
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx


def direction_at(ts, vs, t, slope_thresh_pct_s=0.5):
    """运动方向：+1 开向（上行），-1 关向（下行），0 停留，None 数据不足。"""
    s = slope_at(ts, vs, t)
    if s is None:
        return None
    if s > slope_thresh_pct_s:
        return 1
    if s < -slope_thresh_pct_s:
        return -1
    return 0


def window_entries(ts, pos, lo, hi, end):
    """位置进入 [lo,hi] 窗的时刻序列。

    end='high' 期望自下而上进入（开位动作窗），end='low' 期望自上而下
    （关位动作窗）。记录起点已在窗内时以首个样本为进入点（方向未知）。
    返回 [{t, index, direction_ok}]，direction_ok 为 None 表示无法判定。
    """
    entries = []
    prev_inside = False
    for i, p in enumerate(pos):
        inside = (lo - 1e-9) <= p <= (hi + 1e-9)
        if inside and not prev_inside:
            if i == 0:
                direction_ok = None
            elif end == "high":
                direction_ok = pos[i] >= pos[i - 1] - 1e-9
            else:
                direction_ok = pos[i] <= pos[i - 1] + 1e-9
            entries.append({"t": ts[i], "index": i, "direction_ok": direction_ok})
        prev_inside = inside
    return entries


def window_exits(ts, pos, lo, hi, end):
    """位置离开 [lo,hi] 窗并向远离端点方向而去的时刻序列。

    end='high'：向下穿出窗下沿（lo）；end='low'：向上穿出窗上沿（hi）。
    返回 [{t, index}]。
    """
    exits = []
    for i in range(1, len(pos)):
        if end == "high":
            if pos[i - 1] >= lo - 1e-9 and pos[i] < lo - 1e-9:
                exits.append({"t": ts[i], "index": i})
        else:
            if pos[i - 1] <= hi + 1e-9 and pos[i] > hi + 1e-9:
                exits.append({"t": ts[i], "index": i})
    return exits


def dwell_from(ts, pos, i_start, lo, hi):
    """从 i_start 起位置连续停留在 [lo,hi] 内的时长与结束序号（出窗或记录末）。"""
    j = i_start
    while j + 1 < len(pos) and (lo - 1e-9) <= pos[j + 1] <= (hi + 1e-9):
        j += 1
    return ts[j] - ts[i_start], j
