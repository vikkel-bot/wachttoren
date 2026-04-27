from __future__ import annotations

import unittest

from watchtower.domain import MarketSnapshot, NewsEvent
from watchtower.services.outcomes import OutcomeTracker
from watchtower.services.scoring import EntryScorer


class EntryScorerTests(unittest.TestCase):
    def test_positive_news_with_volume_creates_long_signal(self) -> None:
        event = NewsEvent(
            id="evt_1",
            asset="AAPL",
            headline="Apple beats earnings expectations",
            source="example-news",
            sentiment=0.82,
            novelty=0.8,
            relevance=0.9,
            tags=["earnings"],
        )
        market = MarketSnapshot(
            asset="AAPL",
            price=192.4,
            change_15m_pct=0.35,
            volume_zscore=2.2,
            volatility_zscore=1.1,
            trend_1h=0.7,
            trend_1d=0.55,
            sector_change_1d_pct=1.0,
            benchmark_change_1d_pct=0.6,
        )

        signal = EntryScorer().score(event, market)

        self.assertEqual(signal.direction, "long")
        self.assertGreater(signal.entry_score, 0.6)
        self.assertEqual(signal.time_window, "1h")

    def test_extended_move_adds_risk_flag(self) -> None:
        event = NewsEvent(
            id="evt_2",
            asset="NVDA",
            headline="Nvidia announces new product line",
            source="example-news",
            sentiment=0.7,
            novelty=0.7,
            relevance=0.8,
        )
        market = MarketSnapshot(
            asset="NVDA",
            price=950,
            change_15m_pct=4.0,
            volume_zscore=2.8,
            trend_1h=0.9,
            trend_1d=0.8,
        )

        signal = EntryScorer().score(event, market)

        self.assertIn("extended_move", signal.risk_flags)


class OutcomeTrackerTests(unittest.TestCase):
    def test_long_outcome_hit(self) -> None:
        event = NewsEvent(id="evt_3", asset="AAPL", headline="Good news", sentiment=0.8)
        market = MarketSnapshot(asset="AAPL", price=100, volume_zscore=2, trend_1h=0.5)
        signal = EntryScorer().score(event, market)

        outcome = OutcomeTracker().evaluate(signal, entry_price=100, future_price=101, window="15m")

        self.assertTrue(outcome.hit)
        self.assertAlmostEqual(outcome.return_pct, 1.0)


if __name__ == "__main__":
    unittest.main()
