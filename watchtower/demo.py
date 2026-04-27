from __future__ import annotations

import json

from watchtower.domain import MarketSnapshot, NewsEvent
from watchtower.services.scoring import EntryScorer
from watchtower.storage import to_jsonable


def main() -> None:
    event = NewsEvent(
        id="evt_demo_aapl",
        asset="AAPL",
        headline="Apple beats earnings expectations and raises guidance",
        summary="Revenue and margins came in above analyst estimates.",
        source="example-news",
        sentiment=0.82,
        novelty=0.76,
        relevance=0.9,
        tags=["earnings", "guidance"],
    )
    market = MarketSnapshot(
        asset="AAPL",
        price=192.4,
        change_15m_pct=0.45,
        change_1h_pct=1.1,
        change_1d_pct=2.3,
        volume_zscore=2.2,
        volatility_zscore=1.1,
        trend_1h=0.7,
        trend_1d=0.55,
        sector_change_1d_pct=1.0,
        benchmark_change_1d_pct=0.6,
        resistance_distance_pct=2.4,
    )
    signal = EntryScorer().score(event, market)
    print(json.dumps(to_jsonable(signal), indent=2))


if __name__ == "__main__":
    main()
