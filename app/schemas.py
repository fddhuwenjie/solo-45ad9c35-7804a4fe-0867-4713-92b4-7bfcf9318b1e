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
