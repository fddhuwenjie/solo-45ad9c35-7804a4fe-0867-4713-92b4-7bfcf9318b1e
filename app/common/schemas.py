"""跨诊断能力共享的请求模型。

各诊断能力特有的提交/调整模型见对应包的 schemas 模块
（``app.fullstroke.schemas``、``app.failsafe.schemas`` 等）。
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ===================== 逐通道仪器校准链 =====================

MeasurementType = Literal[
    "command", "position", "pressure", "temperature",
    "flow_gas", "flow_liquid"]


class CalibrationVersionCreate(BaseModel):
    """校准（证书）版本：一经创建不可变；续证/纠错只能派生新版本。"""
    instrument_serial: str = Field(..., min_length=1, description="仪器序列号")
    measurement_type: MeasurementType = Field(
        ..., description="测量类型：command/position/pressure/temperature/flow_gas/flow_liquid")
    unit: str = Field(..., description="证书单位，须与测量类型单位族兼容（同族可不同单位）")
    valid_from: str = Field(..., description="证书生效时间 ISO 8601（纯日期按当日 UTC 00:00）")
    valid_until: str = Field(..., description="证书失效时间 ISO 8601")
    range_min: float = Field(..., description="证书量程下限（证书单位）")
    range_max: float = Field(..., description="证书量程上限（证书单位）")
    points: list[list[float]] = Field(
        ..., min_length=2,
        description="示值—参考值点列 [[indication, reference], ...]，"
                    "示值与参考值均须严格单调递增")
    standard_uncertainty: float = Field(
        ..., gt=0, description="校准标准不确定度（1σ，证书单位）")
    certificate_summary: str = Field("", description="证书摘要/编号/签发机构等")
    supersedes_id: Optional[int] = Field(
        None, description="续证所替代的旧版本 id（旧版本不可改写，仅记录派生关系）")
    note: str = ""

    @field_validator("instrument_serial")
    @classmethod
    def _strip_serial(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("instrument_serial 不能为空")
        return v

    @field_validator("points")
    @classmethod
    def _check_points(cls, pts):
        for p in pts:
            if len(p) != 2:
                raise ValueError("每个校准点须为 [示值, 参考值] 二元组")
        ind = [float(p[0]) for p in pts]
        ref = [float(p[1]) for p in pts]
        if any(ind[i] <= ind[i - 1] for i in range(1, len(ind))):
            raise ValueError("示值列必须严格单调递增（点列不单调不允许建版）")
        if any(ref[i] <= ref[i - 1] for i in range(1, len(ref))):
            raise ValueError("参考值列必须严格单调递增（点列不单调不允许建版）")
        return pts

    @field_validator("range_max")
    @classmethod
    def _check_range(cls, v, info):
        lo = info.data.get("range_min")
        if lo is not None and v <= lo:
            raise ValueError("量程上限必须大于下限")
        return v


# ===================== 共享序列/量程 =====================

class SeriesData(BaseModel):
    unit: str
    points: list[list[float]] = Field(..., description="[[t_seconds, value], ...]")


class RangeSpec(BaseModel):
    min: float = 0.0
    max: float = 100.0
    unit: str = "%"


FlowDirection = Literal["upstream_to_downstream", "downstream_to_upstream"]


# ===================== 测量不确定度评估输入 =====================

UncertaintyDistribution = Literal["rectangular", "normal"]

# 允许声明的分量单位（按通道归一化后的量纲校验）：
# 指令/阀位：%、mA、mm/cm/in/inch；压力：kPa/MPa/bar/psi/kgf/cm2；时间：s
UNCERTAINTY_COMPONENT_UNITS = (
    "%", "mA", "mm", "cm", "in", "inch",
    "kPa", "MPa", "bar", "psi", "kgf/cm2", "s",
)


class UncertaintyComponent(BaseModel):
    """一个不确定度输入分量。

    - resolution 分辨率：声明值为量化步进（量化宽度），按均匀分布，
      扰动半宽 = 声明值 / 2（如 resolution=0.2% → ±0.1%）；
    - accuracy 准确度：默认均匀分布（声明值为允许误差限/半宽）；
    - zero_drift 零点漂移：默认均匀分布（声明值为漂移限/半宽）；
    - calibration 校准标准不确定度：默认正态分布（声明值即 1σ 标准不确定度）。
    value 的单位由 unit 声明，必须与通道量纲兼容（时间抖动为 s）。
    """
    kind: Literal["resolution", "accuracy", "zero_drift", "calibration"]
    value: float = Field(..., ge=0)
    unit: str = Field(..., description="分量单位，须与通道量纲一致；时间抖动为 s")
    distribution: Optional[UncertaintyDistribution] = Field(
        None, description="缺省：resolution/accuracy/zero_drift=rectangular，calibration=normal")


class ChannelUncertainty(BaseModel):
    """单通道测量不确定度分量（均可选，未提供的分量不计入评估）。"""
    resolution: Optional[UncertaintyComponent] = None
    accuracy: Optional[UncertaintyComponent] = None
    zero_drift: Optional[UncertaintyComponent] = None
    time_jitter_s: float = Field(
        0.0, ge=0, description="采样时刻抖动（标准差，秒）：通道时钟偏差，按正态分布扰动时标")
    calibration: Optional[UncertaintyComponent] = Field(
        None, description="本通道校准标准不确定度；也可在 calibration 块统一声明")


class CalibrationUncertainty(BaseModel):
    """校准标准不确定度及其覆盖范围；范围不覆盖观测值时评估无效。"""
    value: float = Field(..., gt=0, description="校准标准不确定度（1σ），单位由 unit 声明")
    unit: str = Field(..., description="与应用通道量纲一致（%/mA/行程 或压力单位）")
    distribution: UncertaintyDistribution = "normal"
    applies_to: list[Literal["command", "position", "pressure"]] = Field(
        ["command", "position", "pressure"], description="该校准分量应用的通道")
    range_min: Optional[float] = Field(None, description="校准覆盖范围下限（unit/range_unit 单位）")
    range_max: Optional[float] = Field(None, description="覆盖范围上限")
    range_unit: Optional[str] = Field(None, description="覆盖范围单位，默认与 unit 相同")


class UncertaintyInput(BaseModel):
    """测量不确定度评估输入（蒙特卡洛）。"""
    channels: dict[Literal["command", "position", "pressure"], ChannelUncertainty] = Field(
        default_factory=dict)
    calibration: Optional[CalibrationUncertainty] = None
    n_samples: int = Field(120, ge=20, le=2000, description="蒙特卡洛重采样次数")
    seed: int = Field(20260912, description="固定随机种子（同一输入须可复现）")
    interval_prob: float = Field(
        0.95, gt=0, lt=1, description="输出区间的经验概率（分位数法）")
    note: str = ""


class AnalyzeRequest(BaseModel):
    author: str = "auto"
    uncertainty: Optional[UncertaintyInput] = Field(
        None, description="本次分析使用的测量不确定度评估输入；缺省时沿用测试提交中的声明，"
                          "均未提供则指标只给中心值并标为未评估")
    calibration_bindings: Optional[dict[str, int]] = Field(
        None, description="本次分析冻结的逐通道校准绑定 {通道: 校准版本id}；"
                          "缺省沿用上一版本/测试提交；提供空对象 {} 可显式回到 legacy 模式。"
                          "改绑不改变测试数据，但派生新分析版本")
