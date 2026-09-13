"""单相液体（IEC 60534-2-1 / ISA-75.01）流量系数 Cv 换算与气蚀/闪蒸判别。

非阻塞液体、流量单位 m³/h、压力单位 bar、密度 kg/m³ 时：

    Cv = Q / (N1 · sqrt( ΔP / (ρ/ρ0) ))，ρ0 = 999.0 kg/m³（15 °C 水），N1 = 0.865

校验：1 US gpm（0.227125 m³/h）水、ΔP = 1 psi（0.06895 bar）→ Cv ≈ 1.0。

阻塞（气蚀/闪蒸）发生在 ΔP > ΔPchoked = FL² · (P1 − Ff·Pv) 时：

    Ff = 0.96 − 0.28·sqrt(Pv/Pc)

其中 P1、P2、Pv、Pc 均取绝压（现场给表压，按大气压换算）。阻塞流工况下
线性 Cv 公式不成立，测点必须排除并单独解释，不得进入曲线拟合。

流量单位（液用工况体积流量）：
m³/h、m³/min、L/min、L/h、US gpm、US gph。

流量单位换算表集中在公共时序处理层（app.common.processing.units），
本模块保留同名引用以兼容既有调用。
"""

from ..common.processing.units import (  # noqa: F401
    FLOW_TO_M3H, TEMP_OFFSET_K)

N1_M3H_BAR = 0.865
RHO_REF_KG_M3 = 999.0      # 15 °C 参考水密度


def norm_flow_liquid(points, unit, channel="flow"):
    """液体体积流量统一为 m³/h。返回 (values_m3h, notes, conflicts)。"""
    u = (unit or "").strip().lower()
    notes, conflicts = [], []
    vals = [float(p[1]) for p in points]
    if not vals:
        conflicts.append(f"{channel}: 采样点为空")
        return [], notes, conflicts
    factor = FLOW_TO_M3H.get(u)
    if factor is None:
        conflicts.append(
            f"{channel}: 不支持的液体流量单位 {unit!r}（允许 "
            f"{'/'.join(sorted({k for k in FLOW_TO_M3H if k.isascii()}))}）")
        return vals, notes, conflicts
    if factor != 1.0:
        notes.append(f"{channel}: {unit} 已换算为 m³/h（工况体积流量）")
    return [v * factor for v in vals], notes, conflicts


def ff_factor(pv_abs_kpa, pc_abs_kpa):
    """临界压力比系数 Ff。"""
    if pc_abs_kpa <= 0 or pv_abs_kpa < 0:
        return None
    return 0.96 - 0.28 * (pv_abs_kpa / pc_abs_kpa) ** 0.5


def choked_differential_kpa(p1_abs_kpa, pv_abs_kpa, pc_abs_kpa, fl):
    """阻塞流临界压差 ΔPchoked = FL²·(P1 − Ff·Pv)，单位 kPa；参数非法返回 None。"""
    ff = ff_factor(pv_abs_kpa, pc_abs_kpa)
    if ff is None or fl <= 0:
        return None
    return fl * fl * (p1_abs_kpa - ff * pv_abs_kpa)


def liquid_cv(q_m3h, dp_kpa, density_kg_m3):
    """非阻塞液体 Cv（m³/h, bar, kg/m³）。参数非法返回 None。"""
    if density_kg_m3 is None or density_kg_m3 <= 0:
        return None
    if dp_kpa is None or dp_kpa <= 0:
        return None
    sg = density_kg_m3 / RHO_REF_KG_M3
    dp_bar = dp_kpa / 100.0
    return q_m3h / (N1_M3H_BAR * (dp_bar / sg) ** 0.5)


def cv_at(q_m3h, p1_gauge_kpa, p2_gauge_kpa, temp_c, params):
    """单点 Cv 换算并判别阻塞。

    params: dict(atmospheric_pressure_kpa, density_kg_m3, vapor_pressure_kpa,
                 critical_pressure_kpa, liquid_recovery_factor_fl,
                 density_ref_temp_c, temp_band_c)
    返回 dict：q_m3h、dp_kpa、p1_abs_kpa、cv、choked（bool|None）、
    ff、dp_choked_kpa、exclusion（code 或 None）、conversion 参数回显。
    """
    atm = params["atmospheric_pressure_kpa"]
    density = params.get("density_kg_m3")
    pv = params.get("vapor_pressure_kpa")
    pc = params.get("critical_pressure_kpa")
    fl = params.get("liquid_recovery_factor_fl", 0.9)

    p1_abs = p1_gauge_kpa + atm
    p2_abs = p2_gauge_kpa + atm
    dp = p1_gauge_kpa - p2_gauge_kpa

    out = {
        "q_m3h": round(q_m3h, 6),
        "dp_kpa": round(dp, 3),
        "p1_abs_kpa": round(p1_abs, 3),
        "p2_abs_kpa": round(p2_abs, 3),
        "temp_c": round(temp_c, 3) if temp_c is not None else None,
        "cv": None, "choked": None, "ff": None, "dp_choked_kpa": None,
        "exclusion": None,
    }

    if q_m3h <= 0:
        out["exclusion"] = "nonpositive_flow"
    if dp is not None and dp <= 0:
        out["exclusion"] = out["exclusion"] or "insufficient_differential_pressure"
    if p2_abs <= 0 or p1_abs <= 0:
        out["exclusion"] = out["exclusion"] or "pressure_physical"

    # 物性适用温度窗（密度参考温度 ± 带宽）
    ref_t = params.get("density_ref_temp_c")
    band = params.get("temp_band_c")
    if (ref_t is not None and band is not None and temp_c is not None
            and abs(temp_c - ref_t) > band):
        out["exclusion"] = out["exclusion"] or "property_out_of_range"

    if density is None:
        out["exclusion"] = out["exclusion"] or "property_missing"
    if pv is None or pc is None:
        out["choked"] = None
        if out["exclusion"] is None:
            out["exclusion"] = "property_missing"
    else:
        ff = ff_factor(pv, pc)
        dp_choked = choked_differential_kpa(p1_abs, pv, pc, fl)
        out["ff"] = round(ff, 4)
        out["dp_choked_kpa"] = round(dp_choked, 2)
        # 仅在压差、压力物理有效时判别阻塞
        if p1_abs > 0 and p2_abs > 0 and dp > 0:
            out["choked"] = dp > dp_choked
            if out["choked"] and out["exclusion"] is None:
                out["exclusion"] = ("flashing" if p2_abs < pv else "cavitation")

    if out["exclusion"] is None:
        out["cv"] = round(liquid_cv(q_m3h, dp, density), 4)
    return out


# ---- 设计特性基准（固有流量特性） ----

def characteristic_fraction(position_pct, characteristic, r=50.0):
    """给定阀位（0–100%）返回设计固有 Cv 占额定 Cv 的份额 f(x)。"""
    x = min(max(position_pct, 0.0), 100.0) / 100.0
    if characteristic == "linear":
        return x
    if characteristic == "equal_percentage":
        return r ** (x - 1.0)
    if characteristic == "quick_open":
        # 快开：与等百分比互补的上凸平方根型，f(0)=0、f(1)=1
        return x ** 0.5
    raise ValueError(f"未知设计特性 {characteristic!r}")


def characteristic_curve(characteristic, positions, rated_cv, r=50.0):
    """给定阀位序列返回铭牌基准 Cv 序列。"""
    return [rated_cv * characteristic_fraction(p, characteristic, r)
            for p in positions]
