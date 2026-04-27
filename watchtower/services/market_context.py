from __future__ import annotations

from watchtower.domain import MarketSnapshot


def clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


class MarketContextEngine:
    def detect_regime(self, market: MarketSnapshot) -> str:
        if market.volatility_zscore >= 2.5:
            return "volatile"
        if market.benchmark_change_1d_pct > 0.7 and market.trend_1d > 0.25:
            return "bull"
        if market.benchmark_change_1d_pct < -0.7 and market.trend_1d < -0.25:
            return "bear"
        return "sideways"

    def regime_report(self, market: MarketSnapshot) -> dict:
        volatile_score = clamp(market.volatility_zscore / 3.0)
        bull_score = clamp(0.45 + (market.benchmark_change_1d_pct * 0.18) + (market.trend_1d * 0.35))
        bear_score = clamp(0.45 - (market.benchmark_change_1d_pct * 0.18) - (market.trend_1d * 0.35))
        sideways_score = clamp(1.0 - max(abs(market.benchmark_change_1d_pct) / 2.2, abs(market.trend_1d), volatile_score * 0.8))
        regime = self.detect_regime(market)
        scores = {
            "bull": round(bull_score, 4),
            "bear": round(bear_score, 4),
            "sideways": round(sideways_score, 4),
            "volatile": round(volatile_score, 4),
        }
        confidence = scores.get(regime, max(scores.values()))
        drivers = []
        if market.volatility_zscore >= 2.5:
            drivers.append("volatility_zscore_above_2_5")
        if market.benchmark_change_1d_pct > 0.7:
            drivers.append("benchmark_strength")
        if market.benchmark_change_1d_pct < -0.7:
            drivers.append("benchmark_weakness")
        if market.trend_1d > 0.25:
            drivers.append("asset_uptrend")
        if market.trend_1d < -0.25:
            drivers.append("asset_downtrend")
        if not drivers:
            drivers.append("mixed_or_range_bound_context")
        return {
            "asset": market.asset,
            "regime": regime,
            "confidence": round(confidence, 4),
            "scores": scores,
            "drivers": drivers,
        }

    def volume_confirmation(self, market: MarketSnapshot) -> float:
        return clamp(market.volume_zscore / 3.0)

    def trend_alignment(self, market: MarketSnapshot, direction: str) -> float:
        direction_sign = -1.0 if direction == "short" else 1.0
        asset_trend = ((market.trend_1h * 0.6) + (market.trend_1d * 0.4)) * direction_sign
        market_context = ((market.sector_change_1d_pct * 0.6) + (market.benchmark_change_1d_pct * 0.4)) * direction_sign
        return clamp(0.5 + (asset_trend * 0.35) + (market_context * 0.08))

    def extension_penalty(self, market: MarketSnapshot, direction: str) -> float:
        direction_sign = -1.0 if direction == "short" else 1.0
        move = market.change_15m_pct * direction_sign
        if move <= 0.75:
            return 0.0
        return clamp((move - 0.75) / 4.0, 0.0, 0.35)

    def structure_bonus(self, market: MarketSnapshot, direction: str) -> float:
        if direction == "long" and market.resistance_distance_pct is not None:
            return clamp(market.resistance_distance_pct / 4.0, 0.0, 0.15)
        if direction == "short" and market.support_distance_pct is not None:
            return clamp(market.support_distance_pct / 4.0, 0.0, 0.15)
        return 0.05
