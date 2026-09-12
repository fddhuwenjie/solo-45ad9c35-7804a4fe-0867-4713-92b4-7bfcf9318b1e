"""推力签名区段划分：按阀位运动识别 开/关行程，再细分相位。

与通用指令斜率分段不同，推力签名按**阀位速度**切分运动行程（启程是压力
建压而阀位未动的阶段，指令斜率不能代表运动）：窗口速度持续超过阈值为运动，
开/关之间的静止段为换向停留。每个开/关行程再细分：

- breakaway（启程）：行程起点 → 阀位首次持续移动（开/关均有）；
- unseat（离座，仅开阀）：开始移动 → 阀位离开关位带；
- running（匀速）：行程中部速度持续段；
- seating（落座，仅关阀）：阀位进入关位带 → 行程终点。

边界均返回自动依据；复核人可移动相位边界（不得越出所属行程），移动留痕
到新版本。相位无法圈定（行程过短、全程爬行）记为证据缺口。
"""

PHASE_BREAKAWAY = "breakaway"
PHASE_UNSEAT = "unseat"
PHASE_RUNNING = "running"
PHASE_SEATING = "seating"
PHASE_REVERSAL = "reversal"


def _velocity(grid, pos, win_s=0.3):
    """阀位窗口速度 %/s（居中窗口）。"""
    n = len(grid)
    vel = [0.0] * n
    half = 0
    for i in range(n):
        j = i
        while j + 1 < n and grid[j + 1] - grid[i] < win_s:
            j += 1
        k = i
        while k - 1 >= 0 and grid[i] - grid[k - 1] < win_s:
            k -= 1
        dt = grid[j] - grid[k]
        vel[i] = (pos[j] - pos[k]) / dt if dt > 0 else 0.0
    return vel


def _detect_runs(grid, pos, vel, velocity_min):
    """按阀位速度切分开/关行程与换向停留。

    返回 [(kind, i_start, i_end)]，kind ∈ opening/closing/reversal。
    运动判定：连续窗口速度同向且超过 velocity_min；短于 0.5s 的运动段并入
    相邻停留，短停留并入相邻运动。行程随后向前吸收紧邻停留段，以包含
    建压/排气但阀位未动的启程阶段。
    """
    n = len(grid)
    state = []
    for i in range(n):
        if vel[i] > velocity_min:
            state.append(1)
        elif vel[i] < -velocity_min:
            state.append(-1)
        else:
            state.append(0)

    def runs_of(seq):
        out = []
        s = 0
        for i in range(1, len(seq)):
            if seq[i] != seq[s]:
                out.append((seq[s], s, i - 1))
                s = i
        out.append((seq[s], s, len(seq) - 1))
        return out

    runs = runs_of(state)
    name = {1: "opening", -1: "closing", 0: "reversal"}

    # 合并：短运动段（<0.5s）视为停留；短停留（<0.8s）并入相邻运动
    changed = True
    while changed and len(runs) > 1:
        changed = False
        for k, (d, s, e) in enumerate(runs):
            dur = grid[e] - grid[s]
            if d != 0 and dur < 0.5:
                runs[k] = (0, s, e)
                changed = True
                break
            if d == 0 and dur < 0.8:
                if k == 0 and len(runs) > 1:
                    nd = runs[1][0]
                    runs[1] = (nd, s, runs[1][2])
                    del runs[k]
                else:
                    pd = runs[k - 1][0]
                    runs[k - 1] = (pd, runs[k - 1][1], e)
                    del runs[k]
                changed = True
                break
        if changed:
            merged = []
            for d, s, e in runs:
                if merged and merged[-1][0] == d:
                    merged[-1] = (d, merged[-1][1], e)
                else:
                    merged.append((d, s, e))
            runs = merged

    # 首/尾停留若覆盖整个记录则保留；合并相邻同向
    out = [(name[d], s, e) for d, s, e in runs]

    # 启程相位需要包含「建压但阀位未动」阶段，落座力需要运动后的保持段：
    # 运动行程向前吸收紧邻停留段的尾部（开程建压窗口 ≤2.5s，避免吞掉记录
    # 开头的长静止段），向后吸收紧邻停留段（端点保持）。
    PRE_MOTION_MAX_S = 2.5
    extended = list(out)
    k = 0
    while k < len(extended):
        kind, s, e = extended[k]
        if kind in ("opening", "closing") and k > 0 \
                and extended[k - 1][0] == "reversal":
            rs, re_ = extended[k - 1][1], extended[k - 1][2]
            new_s = s
            for j in range(s - 1, rs - 1, -1):
                if grid[s] - grid[j] <= PRE_MOTION_MAX_S:
                    new_s = j
                else:
                    break
            extended[k] = (kind, new_s, e)
            if new_s > rs:
                extended[k - 1] = ("reversal", rs, new_s - 1)
            else:
                del extended[k - 1]
                k -= 1
        k += 1
    k = 0
    while k < len(extended):
        kind, s, e = extended[k]
        if kind in ("opening", "closing") and k + 1 < len(extended) \
                and extended[k + 1][0] == "reversal":
            ns, ne = extended[k + 1][1], extended[k + 1][2]
            if kind == "closing":
                # 落座后只吸收前 1.2s 保持段用于落座力稳定，其余仍是停留
                cut = min(ne, next((j for j in range(ns, ne + 1)
                                   if grid[j] - grid[ns] >= 1.2), ne))
                extended[k] = (kind, s, cut)
                if cut < ne:
                    extended[k + 1] = ("reversal", cut + 1, ne)
                    k += 1
                else:
                    del extended[k + 1]
            else:
                extended[k] = (kind, s, ne)
                del extended[k + 1]
        k += 1
    return extended


def _sustained_move_index(grid, pos, i0, i1, direction, move_pct, sustained_s,
                          fnet=None, build_fraction=0.6):
    """启程终点：压力建满后阀位开始持续移动的首点。

    判据：从行程起点阀位最低/最高点（即运动前静止基准）起，累计偏离
    ≥ move_pct，且随后 sustained_s 内不再回落到该基准 ±0.5·move_pct 内。
    在座内缓慢移动的情形，配合净推力建压完成点放宽到位移 0.5·move_pct。
    """
    seg = range(i0, min(i1 + 1, i0 + max(2, int(4.0 / max(grid[1] - grid[0], 1e-9)))))
    i_base = min(seg, key=lambda i: direction * pos[i])
    x0 = pos[i_base]

    def holds(i, need, dur=None):
        k = i
        dur = sustained_s if dur is None else dur
        while k + 1 <= i1 and grid[k + 1] - grid[i] < dur:
            k += 1
        return (grid[k] - grid[i] >= dur
                and all(direction * (pos[j] - x0) >= need for j in range(i, k + 1)))

    # 第一判据：持续位移 ≥ move_pct
    for i in range(i_base, i1 + 1):
        if direction * (pos[i] - x0) >= move_pct and holds(i, 0.5 * move_pct):
            return i

    # 第二判据（建压完成后在座内缓慢移动）：建压完成点之后最早满足
    # 持续位移 ≥0.5·move_pct 的点
    if fnet is not None:
        peak = (max(fnet[i_base:i1 + 1]) if direction > 0
                else min(fnet[i_base:i1 + 1]))
        threshold = peak * build_fraction
        built = next((j for j in range(i_base, i1 + 1)
                      if (fnet[j] >= threshold if direction > 0
                          else fnet[j] <= threshold)), i_base)
        for i in range(built, i1 + 1):
            if direction * (pos[i] - x0) >= 0.5 * move_pct and \
                    holds(i, 0.25 * move_pct):
                return i
    return None


def _running_bounds(grid, pos, i0, i1, direction, thr):
    """匀速相位边界：行程中部（距两端 run_margin_pct）的最长连续区。"""
    p_lo = min(pos[i0], pos[i1])
    p_hi = max(pos[i0], pos[i1])
    lo = p_lo + thr["run_margin_pct"]
    hi = p_hi - thr["run_margin_pct"]
    good = [i for i in range(i0, i1 + 1) if lo <= pos[i] <= hi]
    if not good:
        return None, None
    best, cur = (good[0], good[0]), (good[0], good[0])
    for a, b in zip(good, good[1:]):
        if b == a + 1:
            cur = (cur[0], b)
        else:
            if cur[1] - cur[0] > best[1] - best[0]:
                best = cur
            cur = (b, b)
    if cur[1] - cur[0] > best[1] - best[0]:
        best = cur
    return best


def delineate(grid, cmd, pos, thr, manual_moves=None, fnet=None):
    """切分行程与相位。

    fnet：阀杆净推力序列（可选），用于在阀位缓慢在座内移动时依据建压完成
    点确定启程终点。

    返回 (runs, phases, boundary_log, gaps, vel)。
    runs: [{index,type,i_start,i_end,t_start,t_end,start_reason,end_reason}]；
    phases: [{run,phase,i_start,i_end,start_reason,end_reason}]。
    """
    vel = _velocity(grid, pos)
    raw_runs = _detect_runs(grid, pos, vel, thr["velocity_min_pct_s"])
    gaps, boundary_log, phases = [], [], []

    runs = []
    for k, (kind, s, e) in enumerate(raw_runs):
        r = {"index": k, "type": kind, "i_start": s, "i_end": e,
             "t_start": round(grid[s], 6), "t_end": round(grid[e], 6),
             "position_start_pct": round(pos[s], 3),
             "position_end_pct": round(pos[e], 3)}
        if kind == "reversal":
            r["start_reason"] = "阀位停止运动（换向停留/保持）"
            r["end_reason"] = "阀位恢复运动"
        else:
            label = "开阀" if kind == "opening" else "关阀"
            r["start_reason"] = (
                f"阀位窗口速度持续{('上升' if kind == 'opening' else '下降')}"
                f"超过 {thr['velocity_min_pct_s']}%/s，{label}行程开始")
            r["end_reason"] = "阀位速度回落，行程结束"
        runs.append(r)

    motion_runs = [r for r in runs if r["type"] in ("opening", "closing")]
    if not any(r["type"] == "opening" for r in motion_runs):
        gaps.append({"code": "stroke_incomplete",
                     "detail": "未识别到开阀运动行程，开阀方向推力签名缺失"})
    if not any(r["type"] == "closing" for r in motion_runs):
        gaps.append({"code": "stroke_incomplete",
                     "detail": "未识别到关阀运动行程，关阀方向推力签名缺失"})

    for r in motion_runs:
        kind = r["type"]
        i0, i1 = r["i_start"], r["i_end"]
        direction = 1 if kind == "opening" else -1

        # ---- 启程 ----
        i_brk = _sustained_move_index(
            grid, pos, i0, i1, direction,
            thr["move_detect_pct"], thr["move_sustained_s"],
            fnet=fnet)
        brk_reason_start = f"行程起点 t={grid[i0]:.2f}s（{r['start_reason']}）"
        if i_brk is not None:
            brk_reason_end = (
                f"阀位持续偏离初始位置 ≥{thr['move_detect_pct']}% 达 "
                f"{thr['move_sustained_s']}s，粘滞已克服")
        else:
            i_brk = i0 + max(1, (i1 - i0) // 3)
            brk_reason_end = "阀位未出现满足阈值的持续移动，启程相位按行程前 1/3 估计"
            gaps.append({"code": "phase_not_determinable",
                         "detail": f"{kind} 行程内未检测到持续移动（位移 ≥"
                                   f"{thr['move_detect_pct']}% 且持续 "
                                   f"{thr['move_sustained_s']}s），启动力判读不充分"})
        phases.append({"run": kind, "phase": PHASE_BREAKAWAY,
                       "i_start": i0, "i_end": min(i_brk, i1),
                       "start_reason": brk_reason_start,
                       "end_reason": brk_reason_end})

        if kind == "opening":
            i_unseat_end = next((i for i in range(i_brk, i1 + 1)
                                 if pos[i] > thr["closed_band_pct"]), None)
            if i_unseat_end is None:
                gaps.append({"code": "phase_not_determinable",
                             "detail": "开阀行程阀位始终未离开关位带 "
                                       f"≤{thr['closed_band_pct']}%，离座力无法判读"})
            else:
                phases.append({
                    "run": kind, "phase": PHASE_UNSEAT,
                    "i_start": i_brk, "i_end": i_unseat_end,
                    "start_reason": brk_reason_end,
                    "end_reason": f"阀位 {pos[i_unseat_end]:.1f}% 首次离开关位带 "
                                  f"≤{thr['closed_band_pct']}%（阀座负载释放）"})

        i_rs, i_re = _running_bounds(grid, pos, i0, i1, direction, thr)
        if i_rs is None:
            gaps.append({"code": "phase_not_determinable",
                         "detail": f"{kind} 行程中部（距两端 "
                                   f"{thr['run_margin_pct']}%）无有效连续段，"
                                   "运行摩擦与摩擦带无法判读"})
        else:
            phases.append({
                "run": kind, "phase": PHASE_RUNNING,
                "i_start": i_rs, "i_end": i_re,
                "start_reason": f"进入行程中部（距两端 {thr['run_margin_pct']}%）"
                                "的持续运动段",
                "end_reason": "离开匀速带（接近行程端点）"})

        if kind == "closing":
            i_seat = next((i for i in range(i0, i1 + 1)
                           if pos[i] <= thr["closed_band_pct"]), None)
            if i_seat is None:
                gaps.append({"code": "phase_not_determinable",
                             "detail": "关阀行程阀位未进入关位带 "
                                       f"≤{thr['closed_band_pct']}%，落座裕量无法判读"})
            else:
                phases.append({
                    "run": kind, "phase": PHASE_SEATING,
                    "i_start": i_seat, "i_end": i1,
                    "start_reason": f"阀位 {pos[i_seat]:.1f}% 进入关位带 "
                                    f"≤{thr['closed_band_pct']}%（开始压紧阀座）",
                    "end_reason": f"行程终点 t={grid[i1]:.2f}s（{r['end_reason']}）"})

    for r in runs:
        if r["type"] == "reversal":
            phases.append({"run": "reversal", "phase": PHASE_REVERSAL,
                           "i_start": r["i_start"], "i_end": r["i_end"],
                           "start_reason": r["start_reason"],
                           "end_reason": r["end_reason"]})

    _apply_manual_moves(grid, runs, phases, manual_moves or [], boundary_log)

    for ph in phases:
        ph["t_start"] = round(grid[ph["i_start"]], 6)
        ph["t_end"] = round(grid[ph["i_end"]], 6)
        ph["position_start_pct"] = round(pos[ph["i_start"]], 3)
        ph["position_end_pct"] = round(pos[ph["i_end"]], 3)
    return runs, phases, boundary_log, gaps, vel


def _apply_manual_moves(grid, runs, phases, manual_moves, boundary_log):
    """复核人移动相位边界；只允许在所属开/关行程内移动，且须留下理由。"""
    bounds = {r["type"]: (r["i_start"], r["i_end"])
              for r in runs if r["type"] in ("opening", "closing")}
    for mv in manual_moves:
        run, phase, boundary = mv["run"], mv["phase"], mv["boundary"]
        target = next((p for p in phases if p["run"] == run and p["phase"] == phase), None)
        t_new = float(mv["new_time"])
        reason = (mv.get("reason") or "").strip()
        if target is None:
            boundary_log.append({"move": mv, "applied": False,
                                 "note": f"{run} 行程不存在 {phase} 相位（自动分析未圈定）"})
            continue
        if not reason:
            boundary_log.append({"move": mv, "applied": False,
                                 "note": "人工移动必须填写理由"})
            continue
        lo, hi = bounds[run]
        i_new = min(range(len(grid)), key=lambda i: abs(grid[i] - t_new))
        if not (lo <= i_new <= hi):
            boundary_log.append({"move": mv, "applied": False,
                                 "note": f"新边界 {t_new}s 超出 {run} 行程范围 "
                                         f"{grid[lo]:.2f}–{grid[hi]:.2f}s"})
            continue
        old_t = grid[target["i_end"] if boundary == "end" else target["i_start"]]
        if boundary == "start":
            target["i_start"] = i_new
            original = target["start_reason"]
            target["start_reason"] = (
                f"人工调整：{reason}（原自动边界 {old_t:.2f}s，依据：{original}）")
        else:
            target["i_end"] = i_new
            original = target["end_reason"]
            target["end_reason"] = (
                f"人工调整：{reason}（原自动边界 {old_t:.2f}s，依据：{original}）")
        boundary_log.append({"move": mv, "applied": True})
