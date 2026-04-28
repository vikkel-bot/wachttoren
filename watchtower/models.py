from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from watchtower.domain import MarketSnapshot, NewsEvent, utc_now


class NewsEventIn(BaseModel):
    id: str | None = None
    asset: str | None = None
    exchange: str | None = None
    headline: str
    summary: str = ""
    source: str = "unknown"
    url: str | None = None
    published_at: datetime | None = None
    sentiment: float = Field(default=0.0, ge=-1.0, le=1.0)
    novelty: float = Field(default=0.5, ge=0.0, le=1.0)
    relevance: float = Field(default=0.5, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_domain(self, asset: str) -> NewsEvent:
        return NewsEvent(
            id=self.id or f"evt_{uuid4().hex[:12]}",
            asset=asset,
            headline=self.headline,
            summary=self.summary,
            source=self.source,
            url=self.url,
            published_at=self.published_at or utc_now(),
            sentiment=self.sentiment,
            novelty=self.novelty,
            relevance=self.relevance,
            tags=self.tags,
            metadata=self.metadata,
        )


class MarketSnapshotIn(BaseModel):
    asset: str
    exchange: str | None = None
    price: float = Field(gt=0)
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
    timestamp: datetime | None = None

    def to_domain(self, asset: str | None = None) -> MarketSnapshot:
        return MarketSnapshot(
            asset=(asset or self.asset).upper(),
            price=self.price,
            change_15m_pct=self.change_15m_pct,
            change_1h_pct=self.change_1h_pct,
            change_1d_pct=self.change_1d_pct,
            volume_zscore=self.volume_zscore,
            volatility_zscore=self.volatility_zscore,
            trend_1h=self.trend_1h,
            trend_1d=self.trend_1d,
            sector_change_1d_pct=self.sector_change_1d_pct,
            benchmark_change_1d_pct=self.benchmark_change_1d_pct,
            support_distance_pct=self.support_distance_pct,
            resistance_distance_pct=self.resistance_distance_pct,
            timestamp=self.timestamp or utc_now(),
        )


class SignalEvaluationIn(BaseModel):
    event: NewsEventIn
    market: MarketSnapshotIn


class OutcomeIn(BaseModel):
    signal_id: str
    entry_price: float = Field(gt=0)
    future_price: float = Field(gt=0)
    window: str = "15m"
    hit_threshold_pct: float = 0.2


class ConnectorFetchIn(BaseModel):
    connector: str = "mock-news"
    asset: str = "AAPL"
    exchange: str | None = None
    limit: int = Field(default=10, ge=1, le=50)
    feed_url: str | None = None
    from_dt: datetime | None = None
    to_dt: datetime | None = None
    ingest: bool = True


class NewsRadarScanIn(BaseModel):
    mode: str = "mock"
    queries: list[str] = Field(default_factory=list)
    rss_urls: list[str] = Field(default_factory=list)
    limit: int = Field(default=25, ge=1, le=100)
    max_per_source: int = Field(default=5, ge=1, le=25)
    timespan: str = "1d"
    ingest: bool = True


class MarketConnectorFetchIn(BaseModel):
    connector: str = "mock-market"
    asset: str = "AAPL"
    exchange: str | None = None
    ingest: bool = True


class WatchlistItemIn(BaseModel):
    asset: str
    exchange: str = "GLOBAL"
    region: str | None = None
    currency: str | None = None
    timezone: str | None = None
    enabled: bool = True
    min_entry_score: float = Field(default=0.7, ge=0.0, le=1.0)
    min_confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    max_signals_per_hour: int = Field(default=5, ge=1, le=100)
    notes: str = ""


class ColonyConfigIn(BaseModel):
    enabled: bool = True
    webhook_url: str | None = None
    dry_run: bool = True
    min_entry_score: float = Field(default=0.7, ge=0.0, le=1.0)
    min_confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    max_batch_size: int = Field(default=25, ge=1, le=250)


class ColonyDispatchIn(BaseModel):
    dry_run: bool | None = None
    limit: int = Field(default=25, ge=1, le=250)


class EntityResolveIn(BaseModel):
    text: str
    exchange: str | None = None


class PipelineRunIn(BaseModel):
    exchange: str | None = None
    region: str | None = None
    news_connector: str = "mock-news"
    market_connector: str = "mock-market"
    max_assets: int = Field(default=25, ge=1, le=250)
    max_events_per_asset: int = Field(default=1, ge=1, le=10)
    persist: bool = True


class GlobalPipelineRunIn(BaseModel):
    exchanges: list[str] | None = None
    regions: list[str] | None = None
    asset_classes: list[str] | None = None
    news_connector: str = "mock-regional-news"
    market_connector: str = "mock-regional-market"
    max_assets_per_exchange: int = Field(default=5, ge=1, le=100)
    max_events_per_asset: int = Field(default=1, ge=1, le=10)
    persist: bool = True
    seed_watchlist: bool = True


class WatchlistSeedIn(BaseModel):
    exchange: str | None = None
    region: str | None = None
    asset_class: str | None = None
    max_assets: int = Field(default=25, ge=1, le=250)
    min_entry_score: float = Field(default=0.7, ge=0.0, le=1.0)
    min_confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    enabled: bool = True


class ColonyTradeOutcome(BaseModel):
    feedback_type: Literal["TRADE_OUTCOME", "SIGNAL_SKIP"]
    signal_id: str | None = None
    asset: str
    biome: Literal["CRYPTO", "EQUITIES"]
    timestamp: str

    # Alleen bij TRADE_OUTCOME
    direction: Literal["LONG", "SHORT"] | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    entry_time: str | None = None
    exit_time: str | None = None
    duration_hours: float | None = None
    pnl_pct: float | None = None
    pnl_eur: float | None = None
    exit_reason: Literal["SL", "TP", "TRAILING", "TTL", "MANUAL"] | None = None
    peak_pnl_pct: float | None = None
    max_drawdown_pct: float | None = None

    # Alleen bij SIGNAL_SKIP
    skip_reason: Literal[
        "REGIME_FILTER", "RISK_FLAG", "LOW_SCORE",
        "MAX_POSITIONS", "DUPLICATE", "NO_BUDGET"
    ] | None = None
    colony_regime: Literal["SIDEWAYS", "TRENDING", "VOLATILE"] | None = None
    risk_flags_received: list[str] = Field(default_factory=list)
    entry_score_received: float | None = None
    confidence_received: float | None = None

    # Altijd
    open_positions_count: int = 0
    portfolio_heat: float = 0.0
    data_quality: Literal["HIGH", "MEDIUM", "LOW"] = "MEDIUM"
    is_replay: bool = False
    replay_id: str | None = None


class ColonyReplayIn(BaseModel):
    source: str
    replay_id: str
    submitted_at: str
    trades: list[ColonyTradeOutcome]
