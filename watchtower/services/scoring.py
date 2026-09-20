from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from watchtower.domain import EntrySignal, MarketSnapshot, NewsEvent, utc_now
from watchtower.services.market_context import MarketContextEngine, clamp


SOURCE_SCORES = {
    "reuters": 0.95,
    "bloomberg": 0.95,
    "wsj": 0.9,
    "sec": 0.95,
    "company-pr": 0.82,
    "exchange": 0.82,
    "bbc-business": 0.82,
    "bbc-technology": 0.82,
    "example-news": 0.65,
    "unknown": 0.45,
}


class EntryScorer:
    def __init__(self, market_context: MarketContextEngine | None = None) -> None:
        self.market_context = market_context or MarketContextEngine()

    def score(self, event: NewsEvent, market: MarketSnapshot, as_of: datetime | None = None) -> EntrySignal:
        direction = self._direction(event, market)
        source_quality = SOURCE_SCORES.get(event.source.lower(), 0.55)
        freshness = self._freshness_score(event, as_of)
        sentiment_strength = abs(event.sentiment)
        event_quality = clamp(
            (event.relevance * 0.35)
            + (event.novelty * 0.25)
            + (source_quality * 0.25)
            + (freshness * 0.15)
        )

        trend = self.market_context.trend_alignment(market, direction)
        volume = self.market_context.volume_confirmation(market)
        structure = self.market_context.structure_bonus(market, direction)
        timing = clamp((volume * 0.5) + (trend * 0.35) + structure)

        risk_penalty, risk_flags = self._risk(event, market, direction, freshness)
        raw_score = (
            (sentiment_strength * 0.25)
            + (event_quality * 0.30)
            + (trend * 0.20)
            + (timing * 0.25)
            - risk_penalty
        )
        entry_score = clamp(raw_score)

        if entry_score < 0.34 or sentiment_strength < 0.12:
            direction = "neutral"

        confidence = clamp((entry_score * 0.65) + (event_quality * 0.25) + (freshness * 0.10) - (risk_penalty * 0.35))
        time_window = self._time_window(market, event)
        created_at = utc_now()
        expires_at = created_at + self._expiry_delta(time_window)
        reason = self._reason(event, market, direction, trend, volume)

        return EntrySignal(
            id=f"sig_{uuid4().hex[:12]}",
            event_id=event.id,
            asset=market.asset,
            direction=direction,
            entry_score=round(entry_score, 4),
            confidence=round(confidence, 4),
            time_window=time_window,
            reason=reason,
            risk_flags=risk_flags,
            components={
                "sentiment_strength": round(sentiment_strength, 4),
                "event_quality": round(event_quality, 4),
                "freshness": round(freshness, 4),
                "trend_alignment": round(trend, 4),
                "volume_confirmation": round(volume, 4),
                "timing": round(timing, 4),
                "risk_penalty": round(risk_penalty, 4),
            },
            created_at=created_at,
            expires_at=expires_at,
        )

    def _direction(self, event: NewsEvent, market: MarketSnapshot) -> str:
        if event.sentiment >= 0.12:
            return "long"
        if event.sentiment <= -0.12:
            return "short"
        if market.trend_1h > 0.35 and market.volume_zscore > 1.5:
            return "long"
        if market.trend_1h < -0.35 and market.volume_zscore > 1.5:
            return "short"
        return "neutral"

    def _freshness_score(self, event: NewsEvent, as_of: datetime | None = None) -> float:
        reference = as_of or utc_now()
        age_minutes = max(0.0, (reference - event.published_at).total_seconds() / 60)
        if age_minutes <= 15:
            return 1.0
        if age_minutes <= 60:
            return 0.75
        if age_minutes <= 240:
            return 0.45
        return 0.15

    def _risk(self, event: NewsEvent, market: MarketSnapshot, direction: str, freshness: float) -> tuple[float, list[str]]:
        flags: list[str] = []
        penalty = 0.0

        if market.volatility_zscore >= 2.2:
            flags.append("high_volatility")
            penalty += 0.11
        if freshness < 0.5:
            flags.append("stale_news")
            penalty += 0.08
        if event.relevance < 0.45:
            flags.append("low_relevance")
            penalty += 0.08

        extension = self.market_context.extension_penalty(market, direction)
        if extension > 0.0:
            flags.append("extended_move")
            penalty += extension

        if direction == "long" and market.resistance_distance_pct is not None and market.resistance_distance_pct < 0.7:
            flags.append("near_resistance")
            penalty += 0.07
        if direction == "short" and market.support_distance_pct is not None and market.support_distance_pct < 0.7:
            flags.append("near_support")
            penalty += 0.07

        return clamp(penalty, 0.0, 0.55), flags

    def _time_window(self, market: MarketSnapshot, event: NewsEvent) -> str:
        event_tags = {tag.lower() for tag in event.tags}
        if "breaking" in event_tags or market.volume_zscore >= 2.5:
            return "15m"
        if "earnings" in event_tags or "guidance" in event_tags:
            return "1h"
        return "4h"

    def _expiry_delta(self, time_window: str) -> timedelta:
        windows = {
            "15m": timedelta(minutes=15),
            "1h": timedelta(hours=1),
            "4h": timedelta(hours=4),
            "1d": timedelta(days=1),
        }
        return windows.get(time_window, timedelta(hours=1))

    def _reason(self, event: NewsEvent, market: MarketSnapshot, direction: str, trend: float, volume: float) -> str:
        regime = self.market_context.detect_regime(market)
        if direction == "neutral":
            return f"Signal kept neutral: event strength or market confirmation is not high enough in a {regime} regime."
        tone = "positive" if direction == "long" else "negative"
        return (
            f"{tone} event tone with {volume:.2f} volume confirmation and "
            f"{trend:.2f} trend alignment in a {regime} regime."
        )
