from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class ScanRequest(BaseModel):
    since_day: Optional[date] = None


class MetricThreshold(BaseModel):
    pct_threshold: float
    volume_floor: int
    n_samples: int
    dynamic: bool


class ScanResponse(BaseModel):
    scanned: int
    new_candidates: int
    thresholds: dict[str, MetricThreshold]


class InvestigateRequest(BaseModel):
    metric: str
    day: date
    anomaly_candidate_id: Optional[str] = None


class AskContext(BaseModel):
    metric: Optional[str] = None
    day: Optional[date] = None
    dimension: Optional[str] = None
    value: Optional[str] = None


class AskRequest(BaseModel):
    question: str
    context: Optional[AskContext] = None


# Streaming/incremental ingest - the complement to scripts/load_data.sh's
# bulk file-drop path. Same ad_events columns/constraints as
# configs/clickhouse/01-schema.sql; advertiser_id stays a plain string
# (empty string, not null, on an unfilled request - matches the raw data's
# own convention so both ingest paths produce identical rows).
class EventIn(BaseModel):
    event_time: datetime
    app_id: str
    geo_device_id: str
    advertiser_id: str = ""
    ad_format: Literal["banner", "interstitial", "native", "rewarded", "video"]
    is_filled: Literal[0, 1]
    is_impression: Literal[0, 1]
    is_click: Literal[0, 1]
    revenue: float = 0.0


class IngestEventsRequest(BaseModel):
    events: list[EventIn] = Field(min_length=1)


class IngestEventsResponse(BaseModel):
    inserted: int
