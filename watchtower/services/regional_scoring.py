from __future__ import annotations

from datetime import datetime

from typing import Any

from watchtower.domain import MarketSnapshot, NewsEvent
from watchtower.services.exchanges import Exchange, TradingSessionDetector
from watchtower.services.market_context import clamp
from watchtower.services.scoring import EntryScorer
from watchtower.storage import to_jsonable


class RegionalEntryScorer:
    def __init__(self, base_scorer: EntryScorer, session_detector: TradingSessionDetector) -> None:
        self.base_scorer = base_scorer
        self.session_detector = session_detector

    def score(self, event: NewsEvent, market: MarketSnapshot, exchange: Exchange | None, asset_info: dict | None = None, as_of: datetime | None = None) -> dict[str, Any]:
        base = to_jsonable(self.base_scorer.score(event, market, as_of))
        if not exchange:
            return base

        session = self.session_detector.status(exchange.code)
        flags = list(base.get("risk_flags", []))
        adjustments: dict[str, float] = {}

        score = float(base["entry_score"])
        confidence = float(base["confidence"])
        if not session["is_trading"]:
            score -= 0.08
            confidence -= 0.1
            flags.append("market_closed")
            adjustments["market_closed"] = -0.08
        elif not session["is_regular"]:
            score -= 0.04
            confidence -= 0.05
            flags.append("extended_or_auction_session")
            adjustments["non_regular_session"] = -0.04

        region_adjustment, region_flags = self._region_adjustment(exchange, market)
        score += region_adjustment
        confidence += region_adjustment * 0.5
        flags.extend(region_flags)
        if region_adjustment:
            adjustments["region_profile"] = round(region_adjustment, 4)

        if asset_info:
            sector = asset_info.get("sector")
            if sector:
                base["sector"] = sector
            asset_class = asset_info.get("asset_class")
            if asset_class:
                base["asset_class"] = asset_class
                if asset_class == "commodity":
                    flags.append("commodity_macro_sensitive")
                    base["components"]["commodity_context"] = 1.0

        base["exchange"] = exchange.code
        base["region"] = exchange.region
        base["currency"] = exchange.currency
        base["market_session"] = session
        base["entry_score"] = round(clamp(score), 4)
        base["confidence"] = round(clamp(confidence), 4)
        base["risk_flags"] = sorted(set(flags))
        base["components"]["regional_adjustment"] = round(region_adjustment, 4)
        base["components"]["session_is_regular"] = 1.0 if session["is_regular"] else 0.0
        if adjustments:
            base["adjustments"] = adjustments
        base["reason"] = f"{base['reason']} Exchange context: {exchange.code} {session['session']} session."
        return base

    def _region_adjustment(self, exchange: Exchange, market: MarketSnapshot) -> tuple[float, list[str]]:
        flags: list[str] = []
        adjustment = 0.0

        if exchange.region == "North America":
            if market.volume_zscore >= 2.0 and abs(market.trend_1h) >= 0.45:
                adjustment += 0.035
                flags.append("momentum_volume_supported")
        elif exchange.region == "Europe":
            flags.append("cross_market_us_context_needed")
            if market.sector_change_1d_pct > market.benchmark_change_1d_pct:
                adjustment += 0.015
        elif exchange.region == "Asia":
            flags.append("overnight_gap_sensitive")
            if abs(market.change_1d_pct) > 1.5:
                adjustment -= 0.025
        elif exchange.region == "Africa":
            flags.append("liquidity_spread_sensitive")
            if market.volume_zscore < 1.5:
                adjustment -= 0.045
            if market.volatility_zscore >= 1.6:
                adjustment -= 0.025

        return adjustment, flags
