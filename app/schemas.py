"""请求/响应模型。"""

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


class SeriesData(BaseModel):
    unit: str
    points: list[list[float]] = Field(..., description="[[t_seconds, value], ...]")


class SeriesSet(BaseModel):
    command: SeriesData
    position: SeriesData
    pressure: SeriesData


class RangeSpec(BaseModel):
    min: float = 0.0
    max: float = 100.0
    unit: str = "%"


class Thresholds(BaseModel):
    travel_time_s_max: float = 10.0
    deadband_pct_max: float = 2.0
    hysteresis_pct_max: float = 3.0
    overshoot_pct_max: float = 5.0
    steady_state_pct_max: float = 2.0
    supply_pressure_min_kpa: float = 300.0
    settle_band_pct: float = 2.0


class Conditions(BaseModel):
    load: str = Field(..., description="负载条件，如 online / offline / bench")
    medium: str = "air"
    supply_pressure_kpa: Optional[float] = None
    ambient_temp_c: Optional[float] = None
    note: str = ""


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
    range_max: Optional[float] = Field(None, description="校准覆盖范围上限")
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


class TestSubmission(BaseModel):
    valve_tag: str
    valve_description: str = ""
    phase: Literal["pre", "post", "standalone"] = "standalone"
    test_started_at: Optional[str] = None
    range: RangeSpec = RangeSpec()
    series: SeriesSet
    conditions: Conditions
    thresholds: Thresholds = Thresholds()
    calibration_valid_until: Optional[str] = Field(
        None, description="校准有效期 ISO 日期（legacy 模式），过期则测试不得用于维修结论；"
                          "提供 calibration_bindings 后以逐通道链为准")
    calibration_bindings: Optional[dict[str, int]] = Field(
        None, description="逐通道校准版本绑定 {通道: 校准版本id}；提供后进入逐通道链模式，"
                          "全部测量通道都必须绑定有效版本。旧字段 calibration_valid_until 仅在"
                          "未提供绑定时生效")
    uncertainty: Optional[UncertaintyInput] = Field(
        None, description="测量不确定度评估输入（各通道分辨率/准确度/零点漂移/时钟抖动/校准）；"
                          "未提供时分析沿用中心值结果，指标明确标为未评估")


class AnalyzeRequest(BaseModel):
    author: str = "auto"
    uncertainty: Optional[UncertaintyInput] = Field(
        None, description="本次分析使用的测量不确定度评估输入；缺省时沿用测试提交中的声明，"
                          "均未提供则指标只给中心值并标为未评估")
    calibration_bindings: Optional[dict[str, int]] = Field(
        None, description="本次分析冻结的逐通道校准绑定 {通道: 校准版本id}；"
                          "缺省沿用上一版本/测试提交；提供空对象 {} 可显式回到 legacy 模式。"
                          "改绑不改变测试数据，但派生新分析版本")


class BoundaryMove(BaseModel):
    segment_index: int
    boundary: Literal["start", "end"]
    new_time: float
    reason: str


class Exclusion(BaseModel):
    channel: Literal["command", "position", "pressure"]
    start_index: int = Field(..., description="原始提交序列中的起始点序号（含）")
    end_index: int = Field(..., description="原始提交序列中的结束点序号（含）")
    reason: str


class AdjustRequest(BaseModel):
    author: str
    boundary_moves: list[BoundaryMove] = []
    exclusions: list[Exclusion] = []
    uncertainty: Optional[UncertaintyInput] = Field(
        None, description="可选：覆盖不确定度评估输入；缺省沿用上一版本的输入")
    calibration_bindings: Optional[dict[str, int]] = Field(
        None, description="可选：改绑逐通道校准版本（派生新版本；旧分析不变）；空对象 {} 回到 legacy")
    note: str = ""


class PairRequest(BaseModel):
    pre_analysis_id: int
    post_analysis_id: int


# ===================== 故障安全动作测试 =====================

FailMode = Literal["fail_open", "fail_close", "fail_in_place"]


class FSSeriesSet(BaseModel):
    trip: SeriesData = Field(..., description="跳闸接点（0/1，1=已跳闸；单位用 bool/di）")
    command: SeriesData
    position: SeriesData
    pressure: SeriesData


class FSThresholds(BaseModel):
    response_delay_s_max: float = Field(2.0, description="首次有效跳闸到阀位开始移动的允许延迟")
    t90_s_max: float = Field(8.0, description="开始移动到 90% 行程的允许时间")
    settle_band_pct: float = Field(2.0, description="到达安全位的判定带（相对满量程）")
    settle_dwell_s: float = Field(1.5, description="进入判定带后须连续保持的最短时间")
    rebound_pct_max: float = Field(5.0, description="进入安全位带后允许的最大反弹（满量程 %）")
    stall_min_s: float = Field(0.8, description="中途停滞最短持续时间")
    stall_move_pct: float = Field(0.5, description="中途停滞期间位移小于该值视为停滞")
    pressure_residual_kpa_max: float = Field(50.0, description="气关/气开执行器观察窗末端允许残压")
    pressure_decay_pct_min: float = Field(90.0, description="相对跳闸前压力要求的衰减比例")
    command_loss_pct: float = Field(5.0, description="指令相对跳闸前基线变化超过该值判为失指令")
    fip_drift_pct_max: float = Field(2.0, description="fail-in-place 允许的阀位漂移")
    baseline_min_s: float = Field(1.0, description="跳闸前要求的最短有效基线时长")
    baseline_coverage_min: float = Field(0.8, description="基线区间要求的采样覆盖率")
    chatter_merge_s: float = Field(0.25, description="跳闸沿间隔短于该值合并为接点抖动")
    pre_trip_margin_s: float = Field(0.2, description="首个有效跳闸沿之前要求的有效接点余量")


class FSConditions(BaseModel):
    load: str = Field("offline", description="负载条件，如 online / offline / bench")
    medium: str = "air"
    supply_pressure_kpa: Optional[float] = None
    ambient_temp_c: Optional[float] = None
    actuator_type: Literal["spring_return", "air_to_open", "air_to_close", "double_acting"] = "spring_return"
    note: str = ""


class FSTestSubmission(BaseModel):
    valve_tag: str
    valve_description: str = ""
    phase: Literal["pre", "post", "standalone", "baseline", "periodic"] = "standalone"
    test_started_at: Optional[str] = None
    range: RangeSpec = RangeSpec()
    fail_mode: FailMode
    series: FSSeriesSet
    conditions: FSConditions = FSConditions()
    thresholds: FSThresholds = FSThresholds()
    observation_window_s: float = Field(20.0, gt=0, description="首次有效跳闸后默认观察窗长度")
    calibration_valid_until: Optional[str] = None
    calibration_bindings: Optional[dict[str, int]] = Field(
        None, description="逐通道校准版本绑定 {通道: 校准版本id}；提供后进入逐通道链模式，"
                          "本测试的全部测量通道都必须绑定有效版本。旧字段 calibration_valid_until "
                          "在未提供绑定时继续生效")


class FSTripMove(BaseModel):
    new_trip_time: float
    reason: str


class FSWindowMove(BaseModel):
    window_end_s: float = Field(..., description="相对首次有效跳闸沿的观察窗新终点（秒，>0）")
    reason: str


class FSAdjustRequest(BaseModel):
    author: str
    trip_move: Optional[FSTripMove] = None
    window_move: Optional[FSWindowMove] = None
    calibration_bindings: Optional[dict[str, int]] = Field(
        None, description="可选：改绑逐通道校准版本（派生新版本；旧分析不变）；空对象 {} 回到 legacy")
    note: str = ""


class FSTrendRequest(BaseModel):
    analysis_ids: list[int] = []
    valve_tag: Optional[str] = None
    fail_mode: Optional[FailMode] = None


# ===================== 气体阀座密封保持试验 =====================

FlowDirection = Literal["upstream_to_downstream", "downstream_to_upstream"]


class SLSeriesSet(BaseModel):
    command: SeriesData = Field(..., description="关阀指令")
    position: SeriesData = Field(..., description="阀位")
    upstream_pressure: SeriesData = Field(..., description="上游压力")
    downstream_pressure: SeriesData = Field(..., description="下游压力（封闭容积侧）")
    downstream_temp: SeriesData = Field(..., description="下游温度")
    flow: Optional[SeriesData] = Field(None, description="可选流量计（标准状态体积流量）")


class GasSpec(BaseModel):
    name: str = "air"
    molar_mass_g_mol: float = 28.97
    compressibility_z: float = Field(1.0, gt=0)
    reference_temp_c: float = Field(15.0, description="标准状态温度")
    reference_pressure_kpa: float = Field(101.325, gt=0, description="标准状态压力")


class ClosedVolumeSpec(BaseModel):
    downstream_volume_m3: float = Field(..., gt=0, description="下游封闭容积")
    uncertainty_pct: float = Field(10.0, ge=0, description="容积标称不确定度（用于偏差来源说明）")


IsolationActionType = Literal[
    "close_command", "valve_closed", "upstream_isolated", "downstream_isolated",
    "vent_opened", "vent_closed", "hold_start", "hold_end",
]


class IsolationAction(BaseModel):
    t: float = Field(..., description="动作时刻（与采样同一时间轴，秒）")
    action: IsolationActionType
    note: str = ""


class BlankBaselinePoint(BaseModel):
    temp_c: float
    pressure_kpa: float
    recovery_rate_kpa_min: float = Field(..., description="空白试验下游压力回升速率（非泄漏贡献）")


class BlankBaseline(BaseModel):
    points: list[BlankBaselinePoint] = Field(..., min_length=1)
    temp_margin_c: float = Field(2.0, ge=0, description="温度覆盖裕度")
    pressure_margin_kpa: float = Field(20.0, ge=0, description="压力覆盖裕度")


class SLThresholds(BaseModel):
    leak_rate_max_nl_min: float = Field(0.05, description="标准状态泄漏率限值 Nl/min")
    cumulative_leak_max_nl: float = Field(0.2, description="保持段累计漏量限值 Nl")
    min_differential_kpa: float = Field(100.0, description="保持段最小有效压差")
    closed_band_pct: float = Field(2.0, description="关位判定带（阀位 ≤ 该值视为关位）")
    position_drift_pct_max: float = Field(0.5, description="保持段允许的阀位漂移")
    stabilization_min_s: float = Field(15.0, description="稳压段最短时长")
    hold_min_s: float = Field(60.0, description="保持段最短时长")
    temp_coverage_min: float = Field(0.9, description="保持段温度采样覆盖率下限")
    temp_slope_max_c_min: float = Field(0.5, description="自动圈定保持段起点的温度窗口斜率阈值 °C/min")
    slope_window_s: float = Field(15.0, description="泄漏率/温度斜率回归窗长")
    rate_exceed_dwell_s: float = Field(10.0, description="泄漏率持续超限判定停留")
    flow_deviation_pct_max: float = Field(25.0, description="流量计与质量平衡估算偏差限值 %")


class SLConditions(BaseModel):
    load: str = Field("offline", description="负载条件，如 online / offline / bench")
    seat_config: str = Field("soft_seat", description="阀座配置，如 soft_seat / metal_seat")
    ambient_temp_c: Optional[float] = None
    note: str = ""


class SLTestSubmission(BaseModel):
    valve_tag: str
    valve_description: str = ""
    phase: Literal["pre", "post", "standalone", "baseline", "periodic"] = "standalone"
    test_started_at: Optional[str] = None
    range: RangeSpec = RangeSpec()
    flow_direction: FlowDirection = "upstream_to_downstream"
    series: SLSeriesSet
    gas: GasSpec = GasSpec()
    closed_volume: ClosedVolumeSpec
    isolation: list[IsolationAction] = []
    blank_baseline: BlankBaseline
    conditions: SLConditions = SLConditions()
    thresholds: SLThresholds = SLThresholds()
    calibration_valid_until: Optional[str] = None
    calibration_bindings: Optional[dict[str, int]] = Field(
        None, description="逐通道校准版本绑定 {通道: 校准版本id}；提供后进入逐通道链模式，"
                          "本测试的全部测量通道都必须绑定有效版本。旧字段 calibration_valid_until "
                          "在未提供绑定时继续生效")


class SLSegmentMove(BaseModel):
    segment: Literal["stabilization", "hold"]
    boundary: Literal["start", "end"]
    new_time: float
    reason: str


class SLExclusion(BaseModel):
    channel: Literal["command", "position", "upstream_pressure",
                     "downstream_pressure", "downstream_temp", "flow"]
    start_index: int = Field(..., description="原始提交序列中的起始点序号（含）")
    end_index: int = Field(..., description="原始提交序列中的结束点序号（含）")
    reason: str


class SLAdjustRequest(BaseModel):
    author: str
    segment_moves: list[SLSegmentMove] = []
    exclusions: list[SLExclusion] = []
    calibration_bindings: Optional[dict[str, int]] = Field(
        None, description="可选：改绑逐通道校准版本（派生新版本；旧分析不变）；空对象 {} 回到 legacy")
    note: str = ""


class SLCompareRequest(BaseModel):
    analysis_ids: list[int] = []
    valve_tag: Optional[str] = None


# ===================== 阀杆推力签名测试 =====================

ActuatorType = Literal["single_acting", "double_acting"]
StemDirection = Literal["down_to_close", "up_to_close"]
SpringAction = Literal["fail_close", "fail_open"]


class TSSeriesSet(BaseModel):
    command: SeriesData = Field(..., description="指令信号")
    position: SeriesData = Field(..., description="阀位反馈（0=关位，100=开位）")
    supply_pressure: SeriesData = Field(..., description="供气（气源）压力")
    chamber_a_pressure: Optional[SeriesData] = Field(
        None, description="A 腔压力（开阀侧；单作用执行器的工作腔，必需）")
    chamber_b_pressure: Optional[SeriesData] = Field(
        None, description="B 腔压力（关阀侧；仅双作用执行器必需）")


class AreaSpec(BaseModel):
    value: float = Field(..., gt=0, description="有效面积数值")
    unit: Literal["cm2", "m2", "in2"] = "cm2"


class SpringCurve(BaseModel):
    version: str = Field(..., description="弹簧版本/图号，检修前后比较须一致")
    action: SpringAction = Field("fail_close", description="弹簧作用方式")
    force_unit: Literal["n", "kn"] = "n"
    points: list[list[float]] = Field(
        ..., min_length=2,
        description="[[阀位 %, 弹簧力大小（正值）], ...]，按阀位升序，须覆盖试验行程")


class TSActuator(BaseModel):
    actuator_type: ActuatorType
    area_a: AreaSpec = Field(..., description="A 腔（开阀侧）有效面积")
    area_b: Optional[AreaSpec] = Field(
        None, description="B 腔（关阀侧）有效面积；缺省视为与 A 腔相等")
    spring: Optional[SpringCurve] = Field(
        None, description="弹簧曲线；单作用执行器必需")
    stem_direction: StemDirection = "down_to_close"


class TSThresholds(BaseModel):
    breakaway_open_max_n: float = Field(3000.0, description="开阀启动力（净推力峰值）限值 N")
    breakaway_close_max_n: float = Field(3000.0, description="关阀启动力（净推力峰值）限值 N")
    running_friction_max_n: float = Field(600.0, description="匀速段运行摩擦力限值 N")
    friction_band_max_n: float = Field(400.0, description="单方向匀速段摩擦带（推力波动）限值 N")
    friction_band_total_max_n: float = Field(
        600.0, description="开/关匀速段中位力之差（总摩擦带/迟滞）限值 N")
    unseat_open_max_n: float = Field(2500.0, description="开阀离座力（净推力峰值）限值 N")
    seating_min_n: float = Field(600.0, description="关阀落座最小密封力 N")
    seating_max_n: float = Field(4000.0, description="关阀落座最大允许压力 N")
    supply_pressure_min_kpa: float = Field(450.0, description="运动段供气压力下限 kPa")
    closed_band_pct: float = Field(2.0, description="关位判定带（阀位 ≤ 该值视为在座）")
    move_detect_pct: float = Field(1.0, description="起程判定：阀位持续偏离初始位置的位移 %")
    move_sustained_s: float = Field(0.3, description="起程判定持续时间 s")
    run_margin_pct: float = Field(15.0, description="匀速段距行程两端的位置裕度 %")
    velocity_min_pct_s: float = Field(2.0, description="匀速段最小阀位速度 %/s")
    spring_coverage_margin_pct: float = Field(0.5, description="弹簧曲线覆盖试验行程的裕度 %")


class TSConditions(BaseModel):
    load: str = Field("offline", description="负载条件，如 online / offline / bench")
    medium: str = "air"
    ambient_temp_c: Optional[float] = None
    note: str = ""


class TSTestSubmission(BaseModel):
    valve_tag: str
    valve_description: str = ""
    phase: Literal["pre", "post", "standalone", "baseline", "periodic"] = "standalone"
    test_started_at: Optional[str] = None
    range: RangeSpec = RangeSpec()
    actuator: TSActuator
    series: TSSeriesSet
    conditions: TSConditions = TSConditions()
    thresholds: TSThresholds = TSThresholds()
    calibration_valid_until: Optional[str] = None
    calibration_bindings: Optional[dict[str, int]] = Field(
        None, description="逐通道校准版本绑定 {通道: 校准版本id}；提供后进入逐通道链模式，"
                          "本测试的全部测量通道都必须绑定有效版本。旧字段 calibration_valid_until "
                          "在未提供绑定时继续生效")


class TSSegmentMove(BaseModel):
    run: Literal["opening", "closing"]
    phase: Literal["breakaway", "running", "unseat", "seating"]
    boundary: Literal["start", "end"]
    new_time: float
    reason: str


class TSExclusion(BaseModel):
    channel: Literal["command", "position", "supply_pressure",
                     "chamber_a_pressure", "chamber_b_pressure"]
    start_index: int = Field(..., description="原始提交序列中的起始点序号（含）")
    end_index: int = Field(..., description="原始提交序列中的结束点序号（含）")
    reason: str


class TSAdjustRequest(BaseModel):
    author: str
    segment_moves: list[TSSegmentMove] = []
    exclusions: list[TSExclusion] = []
    calibration_bindings: Optional[dict[str, int]] = Field(
        None, description="可选：改绑逐通道校准版本（派生新版本；旧分析不变）；空对象 {} 回到 legacy")
    note: str = ""


class TSCompareRequest(BaseModel):
    analysis_ids: list[int] = []
    valve_tag: Optional[str] = None


# ===================== 单相液体流量曲线校核 =====================

FlowCharacteristic = Literal["linear", "equal_percentage", "quick_open"]


class FCSeriesSet(BaseModel):
    position: SeriesData = Field(..., description="阀位反馈")
    flow: SeriesData = Field(..., description="流量计实测体积流量（液相工况）")
    upstream_pressure: SeriesData = Field(..., description="阀前压力 P1（表压）")
    downstream_pressure: SeriesData = Field(..., description="阀后压力 P2（表压）")
    temperature: SeriesData = Field(..., description="介质温度")


class FCFluidSpec(BaseModel):
    name: str = Field("water", description="介质名称（叠加比较时须一致）")
    density_kg_m3: Optional[float] = Field(
        None, gt=0, description="工况密度 kg/m³；缺项时全部测点不得进入拟合并判证据不足")
    vapor_pressure_kpa: Optional[float] = Field(
        None, ge=0, description="试验温度下饱和蒸气压（绝压）kPa；缺项时无法判别气蚀/闪蒸")
    critical_pressure_kpa: float = Field(
        22120.0, gt=0, description="介质临界压力（绝压）kPa，默认水")
    density_ref_temp_c: Optional[float] = Field(
        None, description="密度对应的参考温度 °C；提供后超温测点按物性不适用排除")


class FCValveSpec(BaseModel):
    rated_cv: float = Field(..., gt=0, description="额定 Cv（铭牌）")
    size_dn_mm: float = Field(..., gt=0, description="口径 DN mm")
    trim: str = Field(..., description="阀内件标识/图号（叠加比较时须一致）")
    characteristic: FlowCharacteristic
    liquid_recovery_factor_fl: float = Field(
        0.9, gt=0, le=1.0, description="液体压力恢复系数 FL（铭牌/选型书）")
    equal_percentage_r: float = Field(
        50.0, gt=1.0, description="等百分比/快开基准的可调比 R")


class FCMeterSpec(BaseModel):
    full_scale: float = Field(..., gt=0, description="流量计量程上限（满量程）")
    unit: str = Field("m3/h", description="流量单位：m3/h、m3/min、l/min、l/h、gpm、gph")


class FCThresholds(BaseModel):
    min_differential_kpa: float = Field(20.0, description="最小有效压差 kPa，低于该值测点不拟合")
    plateau_band_pct: float = Field(1.0, description="稳态平台阀位波动带 %")
    plateau_slope_pct_s: float = Field(0.5, description="平台判定：窗口阀位速度阈值 %/s")
    plateau_min_duration_s: float = Field(3.0, description="平台最短持续时间 s")
    plateau_merge_gap_s: float = Field(1.5, description="短于该值的平台间抖动间隔并入同一平台")
    plateau_merge_position_pct: float = Field(
        2.0, description="相邻平台中位阀位差不超过该值视为同一平台")
    plateau_drift_pct_max: float = Field(
        1.5, description="平台内阀位峰峰漂移上限 %，超限按平台漂移排除")
    plateau_flow_cv_pct_max: float = Field(
        5.0, description="平台内流量变异系数上限 %，超限按平台漂移排除")
    temp_applicability_band_c: Optional[float] = Field(
        None, description="密度参考温度适用带宽 °C，提供后超温测点按物性不适用排除")
    residual_warn_pct: float = Field(15.0, description="实测 Cv 相对拟合基准的残差告警带 ±%")
    blockage_scale_max: float = Field(0.85, description="拟合容量系数低于该值疑似堵塞")
    erosion_scale_min: float = Field(1.15, description="拟合容量系数高于该值疑似冲蚀")
    reversed_match_ratio: float = Field(
        0.6, description="镜像行程拟合残差小于正向该倍数且单调下降时疑似反装")
    monotonic_rho_min: float = Field(0.9, description="Cv-阀位 Spearman 秩相关达到该值判单调")


class FCConditions(BaseModel):
    load: str = Field("online", description="负载条件，如 online / offline / bench")
    note: str = ""


class FCTestSubmission(BaseModel):
    valve_tag: str
    valve_description: str = ""
    phase: Literal["pre", "post", "standalone", "baseline", "periodic"] = "standalone"
    test_started_at: Optional[str] = None
    range: RangeSpec = RangeSpec()
    flow_direction: FlowDirection = "upstream_to_downstream"
    atmospheric_pressure_kpa: float = Field(
        101.325, gt=0, description="现场大气压（表压转绝压用）kPa")
    series: FCSeriesSet
    fluid: FCFluidSpec = FCFluidSpec()
    valve: FCValveSpec
    meter: FCMeterSpec
    conditions: FCConditions = FCConditions()
    thresholds: FCThresholds = FCThresholds()
    calibration_valid_until: Optional[str] = None
    calibration_bindings: Optional[dict[str, int]] = Field(
        None, description="逐通道校准版本绑定 {通道: 校准版本id}；提供后进入逐通道链模式，"
                          "本测试的全部测量通道都必须绑定有效版本。旧字段 calibration_valid_until "
                          "在未提供绑定时继续生效")


class FCMovePlateau(BaseModel):
    plateau_index: int = Field(..., description="按时间排序的平台序号（0 起）")
    boundary: Literal["start", "end"]
    new_time: float = Field(..., description="新边界时刻（与采样同一时间轴，秒）")
    reason: str


class FCDisablePoint(BaseModel):
    plateau_index: int = Field(..., description="停用的稳态平台测点序号（0 起）")
    reason: str


class FCAdjustRequest(BaseModel):
    author: str
    plateau_moves: list[FCMovePlateau] = []
    disabled_points: list[FCDisablePoint] = []
    calibration_bindings: Optional[dict[str, int]] = Field(
        None, description="可选：改绑逐通道校准版本（派生新版本；旧分析不变）；空对象 {} 回到 legacy")
    note: str = ""


class FCCompareRequest(BaseModel):
    analysis_ids: list[int] = []
    valve_tag: Optional[str] = None
