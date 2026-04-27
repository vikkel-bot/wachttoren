from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class NewsEvent:
    id: str
    asset: str
    headline: str
    summary: str = ""
    source: str = "unknown"
    url: str | None = None
    published_at: datetime = field(default_factory=utc_now)
    sentiment: float = 0.0
    novelty: float = 0.5
    relevance: float = 0.5
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MarketSnapshot:
    asset: str
    price: float
    change_15m_pct: float = 0.0
    change_1h_pct: float = 0.0
    change_1d_pct: float = 0.0
    volume_zscore: float = 0.0
    volatility_zscore: float = 0.0
    trend_1h: float = 0.0
    trend_1d: float = 0.0
    sector_change_1d_pct: float = 0.0
    benchmark_change_1d_pct: float = 0.0
    support_distance_pct: float | None = None
    resistance_distance_pct: float | None = None
    timestamp: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class EntrySignal:
    id: str
    event_id: str
    asset: str
    direction: str
    entry_score: float
    confidence: float
    time_window: str
    reason: str
    risk_flags: list[str]
    components: dict[str, float]
    created_at: datetime = field(default_factory=utc_now)
    expires_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class OutcomeEvaluation:
    signal_id: str
    window: str
    entry_price: float
    future_price: float
    return_pct: float
    hit: bool
    evaluated_at: datetime = field(default_factory=utc_now)
    notes: str = ""
