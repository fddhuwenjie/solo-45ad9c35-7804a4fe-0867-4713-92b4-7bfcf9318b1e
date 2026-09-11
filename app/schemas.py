"""请求/响应模型。"""

from typing import Literal, Optional

from pydantic import BaseModel, Field


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
        None, description="校准有效期 ISO 日期，过期则测试不得用于维修结论")


class AnalyzeRequest(BaseModel):
    author: str = "auto"


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
    note: str = ""


class SLCompareRequest(BaseModel):
    analysis_ids: list[int] = []
    valve_tag: Optional[str] = None
