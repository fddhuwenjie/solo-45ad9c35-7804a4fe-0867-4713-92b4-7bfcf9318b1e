"""跳闸接点与控制指令时序：首个有效跳闸定位、接点抖动聚类、重复触发识别、
指令先行（失气/失电时常早于接点动作）量化。

接点信号约定：0=正常（未跳闸），1=跳闸。输入允许工程单位 bool/di/空串，
数值按下列规则规整，规整不了（中间电平、越界）记为单位冲突类证据缺口。
"""

# 原始点引用统一结构：{channel, index, t, value}

FRACTION_LOW = 0.3    # <=0.3 视为 0
FRACTION_HIGH = 0.7   # >=0.7 视为 1


def low_median(vals):
    """偶数样本取两个中值的较小者（避免斜坡过渡点抬高/压低基线）。"""
    s = sorted(vals)
    return s[(len(s) - 1) // 2] if s else None


def normalize_trip(points, unit):
    """把跳闸接点点序列规整为 0/1。

    返回 (ts, bits, notes, conflicts)；conflicts 为阻断性证据缺口。
    """
    u = (unit or "").strip().lower()
    if u not in ("", "bool", "di", "digital", "0/1"):
        return [p[0] for p in points], [None] * len(points), [], \
               [f"trip: 不支持的接点单位 {unit!r}（允许 bool/di）"]
    ts, bits, notes = [], [], []
    snapped = 0
    for p in points:
        t, v = float(p[0]), float(p[1])
        if v < -0.1 or v > 1.1:
            return [p[0] for p in points], [None] * len(points), [], \
                   [f"trip: 接点值越界 {v:g}（允许 0/1），疑似单位声明错误"]
        if v <= FRACTION_LOW:
            b = 0
            if v not in (0.0, 1.0):
                snapped += 1
        elif v >= FRACTION_HIGH:
            b = 1
            if v not in (0.0, 1.0):
                snapped += 1
        else:
            return [p[0] for p in points], [None] * len(points), [], \
                   [f"trip: t={t:g}s 出现中间电平 {v:g}（介于 {FRACTION_LOW}/{FRACTION_HIGH}），"
                    "接点信号无法解释"]
        ts.append(t)
        bits.append(b)
    if snapped:
        notes.append(f"trip: {snapped} 个采样为非严格 0/1 值，已按电平阈值规整")
    return ts, bits, notes, []


def _edge_refs(points, i_lo, i_hi):
    """原始采样引用：取沿两侧各一个原始点。"""
    refs = []
    for i in sorted({max(0, i_lo - 1), i_lo, min(len(points) - 1, i_hi),
                     min(len(points) - 1, i_hi + 1)}):
        refs.append({"channel": "trip", "index": i,
                     "t": round(float(points[i][0]), 6),
                     "value": points[i][1]})
    return refs


def locate_trip(ts, bits, raw_points, chatter_merge_s, pre_trip_margin_s):
    """定位首个有效跳闸。

    步骤：
    1. 找出全部上升/下降沿与高电平脉冲；
    2. 相邻脉冲间隔（下降沿到下一上升沿）短于 chatter_merge_s 的聚为一个抖动簇；
    3. 首个包含至少 2 个脉冲的簇判定为接点抖动（chatter）；
       有效跳闸沿取该簇第一个上升沿；
    4. 有效跳闸之后再次出现独立高电平脉冲（间隔超过抖动合并窗）为重复触发；
    5. 跳闸沿之前若没有至少 pre_trip_margin_s 的稳态低电平余量，
       可能丢掉了首次动作，记证据缺口。

    返回 dict（trip_time 为 None 表示未找到有效跳闸）。
    """
    # 找高电平运行段 [(i_start, i_end)]
    runs = []
    i = 0
    n = len(bits)
    while i < n:
        if bits[i] == 1:
            j = i
            while j + 1 < n and bits[j + 1] == 1:
                j += 1
            runs.append((i, j))
            i = j + 1
        else:
            i += 1

    info = {
        "trip_time": None,
        "trip_index": None,
        "initial_state": bits[0] if bits else None,
        "chatter": [],
        "retriggers": [],
        "edge_references": [],
        "evidence_gaps": [],
        "notes": [],
    }
    if not runs:
        if bits and bits[0] == 1:
            pass  # 已被下面初始状态检查覆盖
        info["notes"].append("整个记录内接点从未动作，未观察到跳闸")
        return info

    # 初始即高电平：跳闸发生在记录起点之前，无法定位首个有效沿
    if runs[0][0] == 0:
        info["evidence_gaps"].append({
            "code": "trip_inside_record_left",
            "detail": "记录起点接点已处于跳闸状态，首个有效跳闸沿落在记录之外，"
                      "无法测量响应延迟",
        })
        info["trip_time"] = ts[0]
        info["trip_index"] = 0
        info["edge_references"] = _edge_refs(raw_points, 0, 0)
        return info

    # 把连续脉冲按间距聚类为簇
    clusters = [[runs[0]]]
    for run in runs[1:]:
        gap = ts[run[0]] - ts[clusters[-1][-1][1]]
        if gap <= chatter_merge_s:
            clusters[-1].append(run)
        else:
            clusters.append([run])

    first = clusters[0]
    i_lo, i_hi = first[0]
    info["trip_time"] = round(ts[i_lo], 6)
    info["trip_index"] = i_lo
    info["edge_references"] = _edge_refs(raw_points, i_lo, i_hi)

    if len(first) > 1:
        pulse_times = [round(ts[a], 6) for a, _ in first]
        info["chatter"].append({
            "t_start": round(ts[first[0][0]], 6),
            "t_end": round(ts[first[-1][1]], 6),
            "pulse_count": len(first),
            "pulse_times": pulse_times,
            "detail": f"首个跳闸动作含 {len(first)} 个脉冲（脉冲间隔 ≤ {chatter_merge_s}s），"
                      "判定为接点抖动；采用首个上升沿作为有效跳闸时刻",
        })

    # 跳闸前稳态低电平余量
    margin = ts[i_lo] - ts[0]
    if margin < pre_trip_margin_s:
        info["evidence_gaps"].append({
            "code": "insufficient_pre_trip_data",
            "detail": f"跳闸前有效接点余量仅 {margin:.3f}s，少于要求的 "
                      f"{pre_trip_margin_s}s，可能遗漏更早的跳闸动作",
        })

    # 抖动（有效跳闸之后再次出现多脉冲簇）与重复触发（独立单脉冲簇）
    for k, cl in enumerate(clusters[1:], start=1):
        a, b = cl[0], cl[-1]
        base = {"t_start": round(ts[a[0]], 6), "t_end": round(ts[b[1]], 6),
                "edge_references": _edge_refs(raw_points, a[0], b[1])}
        if len(cl) > 1:
            base.update({"pulse_count": len(cl),
                         "pulse_times": [round(ts[x[0]], 6) for x in cl],
                         "detail": f"有效跳闸之后 {base['t_start']}s 处再次出现 "
                                   f"{len(cl)} 脉冲抖动簇，记为重复触发（含接点抖动）"})
            info["chatter"].append(dict(base))
        else:
            base.update({"detail": f"有效跳闸之后 {base['t_start']}s 处接点再次动作，"
                                   "判定为重复触发"})
        info["retriggers"].append(base)
    return info


def command_loss_time(ts, cmd, trip_time, loss_pct, pre_end_s=0.6):
    """控制指令相对跳闸前基线首次明显变化（失气/失电时指令常先消失）。

    基线取 [trip-1.0, trip-pre_end_s)，避开指令消失斜坡的过渡点。
    返回 (t_loss, lead_s, baseline)；未检测到返回 (None, None, baseline)。
    """
    pre = [v for t, v in zip(ts, cmd)
           if trip_time - 1.0 <= t < trip_time - pre_end_s]
    if len(pre) < 2:
        pre = [v for t, v in zip(ts, cmd) if t < trip_time]
    if not pre:
        return None, None, None
    baseline = low_median(pre)
    t_loss = None
    for t, v in zip(ts, cmd):
        if t >= trip_time:
            break
        if abs(v - baseline) >= loss_pct:
            t_loss = t
    lead = round(trip_time - t_loss, 3) if t_loss is not None else None
    return (round(t_loss, 3) if t_loss is not None else None), lead, round(baseline, 3)
