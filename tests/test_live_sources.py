from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from watchtower.domain import MarketSnapshot, NewsEvent
from watchtower.services.assets import ListedAssetUniverse
from watchtower.services.connectors import ConnectorRegistry, YahooFinanceMarketConnector
from watchtower.services.exchanges import ExchangeUniverse
from watchtower.services.live_refresh import PeriodicWatchtowerScheduler, WatchtowerLiveRefresher
from watchtower.storage import SQLiteStore


def test_bbc_rss_connector_parses_real_url_and_source(monkeypatch):
    xml = b"""
    <rss><channel><item>
      <title>Apple shares surge after profit beat</title>
      <description>Apple reports stronger demand.</description>
      <link>https://example.com/apple-profit</link>
      <pubDate>Tue, 28 Apr 2026 10:00:00 GMT</pubDate>
    </item></channel></rss>
    """

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return xml

    def fake_urlopen(request, timeout: int):
        assert timeout == 10
        assert request.full_url == "https://feeds.bbci.co.uk/news/business/rss.xml"
        return FakeResponse()

    monkeypatch.setattr("watchtower.services.connectors.urllib.request.urlopen", fake_urlopen)

    events = ConnectorRegistry().fetch_news("bbc-business", "AAPL", limit=1)

    assert len(events) == 1
    assert events[0].source == "bbc-business"
    assert events[0].asset == "AAPL"
    assert events[0].url == "https://example.com/apple-profit"
    assert events[0].relevance >= 0.45


def test_yfinance_market_connector_maps_commodities_and_builds_ohlcv_snapshot():
    captured: dict[str, str] = {}

    class FakeHistory:
        empty = False

        def iterrows(self):
            yield 0, {"Open": 98.0, "High": 101.0, "Low": 97.0, "Close": 100.0, "Volume": 900}
            yield 1, {"Open": 100.0, "High": 104.0, "Low": 99.0, "Close": 102.0, "Volume": 1200}

    def fake_history(ticker: str, period: str, interval: str) -> FakeHistory:
        captured["ticker"] = ticker
        captured["period"] = period
        captured["interval"] = interval
        return FakeHistory()

    connector = YahooFinanceMarketConnector(fetch_history=fake_history)

    snapshot = connector.fetch("GOLD")

    assert captured == {"ticker": "GLD", "period": "5d", "interval": "1d"}
    assert snapshot.asset == "GOLD"
    assert snapshot.price == 102.0
    assert snapshot.open == 100.0
    assert snapshot.high == 104.0
    assert snapshot.low == 99.0
    assert snapshot.close == 102.0
    assert snapshot.volume == 1200.0
    assert snapshot.change_1d_pct == 2.0


def test_scheduler_tick_fetches_bbc_and_yfinance_then_scores_signal():
    db_path = Path.cwd() / f"test_live_refresh_{uuid4().hex}.sqlite"
    store = SQLiteStore(db_path)
    store.init_schema()
    try:
        store.upsert_watchlist_item(
            {
                "exchange": "NASDAQ",
                "asset": "AAPL",
                "region": "North America",
                "currency": "USD",
                "enabled": True,
                "min_entry_score": 0.5,
                "min_confidence": 0.5,
                "max_signals_per_hour": 5,
                "notes": "test watchlist",
            }
        )

        class FakeConnectors:
            def fetch_news(self, connector: str, asset: str, limit: int = 50, **_kwargs: Any) -> list[NewsEvent]:
                if connector != "bbc-business":
                    return []
                return [
                    NewsEvent(
                        id="evt-bbc-apple",
                        asset=asset,
                        headline="Apple shares surge after new AI demand",
                        summary="Apple demand lifts technology shares.",
                        source="bbc-business",
                        url="https://example.com/apple-ai",
                        published_at=datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc),
                        sentiment=0.35,
                        relevance=0.65,
                    )
                ]

            def fetch_market(self, connector: str, asset: str, exchange: str | None = None) -> MarketSnapshot:
                assert connector == "yfinance-market"
                assert asset == "AAPL"
                assert exchange == "NASDAQ"
                return MarketSnapshot(
                    asset=asset,
                    price=102.0,
                    open=100.0,
                    high=103.0,
                    low=99.0,
                    close=102.0,
                    volume=1200.0,
                    change_1h_pct=2.0,
                    change_1d_pct=1.5,
                    volume_zscore=1.4,
                    trend_1h=0.4,
                    trend_1d=0.3,
                    timestamp=datetime(2026, 4, 28, 10, 5, tzinfo=timezone.utc),
                )

        def fake_score(event: NewsEvent, market: MarketSnapshot, exchange: Any, asset_info: dict[str, Any] | None) -> dict[str, Any]:
            assert event.source == "bbc-business"
            assert event.asset == "AAPL"
            assert market.close == 102.0
            assert exchange.code == "NASDAQ"
            assert asset_info and asset_info["symbol"] == "AAPL"
            now = datetime(2026, 4, 28, 10, 5, tzinfo=timezone.utc)
            return {
                "id": "sig-original",
                "event_id": event.id,
                "asset": "AAPL",
                "exchange": "NASDAQ",
                "region": "North America",
                "asset_class": "equity",
                "direction": "long",
                "entry_score": 0.72,
                "confidence": 0.66,
                "time_window": "1h",
                "reason": "test score",
                "risk_flags": [],
                "components": {},
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=1)).isoformat(),
            }

        exchange_universe = ExchangeUniverse()
        refresher = WatchtowerLiveRefresher(
            store=store,
            connectors=FakeConnectors(),  # type: ignore[arg-type]
            asset_universe=ListedAssetUniverse(exchange_universe),
            exchange_universe=exchange_universe,
            score_signal=fake_score,
        )
        scheduler = PeriodicWatchtowerScheduler(refresher, enabled=False)

        report = scheduler.tick(force=True)

        assert report["news_articles"] == 1
        assert report["assets_updated"] == 1
        assert report["signals_created"] == 1
        events = store.list_events(limit=5, asset="AAPL")
        assert events[0]["source"] == "bbc-business"
        assert events[0]["url"] == "https://example.com/apple-ai"
        signals = store.list_signals(limit=5, asset="AAPL")
        assert signals[0]["direction"] == "long"
        assert signals[0]["event_id"] == events[0]["id"]
    finally:
        db_path.unlink(missing_ok=True)
