"""全行程诊断请求模型。"""

from typing import Literal, Optional

from pydantic import BaseModel, Field

from ..common.schemas import (
    RangeSpec, SeriesData, UncertaintyInput)


class SeriesSet(BaseModel):
    command: SeriesData
    position: SeriesData
    pressure: SeriesData


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
        None, description="校准有效期 ISO 日期（legacy 模式），过期则测试不得用于维修结论；"
                          "提供 calibration_bindings 后以逐通道链为准")
    calibration_bindings: Optional[dict[str, int]] = Field(
        None, description="逐通道校准版本绑定 {通道: 校准版本id}；提供后进入逐通道链模式，"
                          "全部测量通道都必须绑定有效版本。旧字段 calibration_valid_until 仅在"
                          "未提供绑定时生效")
    uncertainty: Optional[UncertaintyInput] = Field(
        None, description="测量不确定度评估输入（各通道分辨率/准确度/零点漂移/时钟抖动/校准）；"
                          "未提供时分析沿用中心值结果，指标明确标为未评估")


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
