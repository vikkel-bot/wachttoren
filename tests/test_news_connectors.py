from __future__ import annotations

import os
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

import watchtower.main as main_module
from watchtower.domain import NewsEvent
from watchtower.main import app
from watchtower.services.connectors import (
    EODHDNewsConnector,
    AlphaVantageNewsConnector,
    ConnectorRegistry,
    RateLimitError,
)
from watchtower.storage import SQLiteStore


def _fresh_store() -> SQLiteStore:
    db_path = Path(tempfile.mkdtemp()) / f"test_news_{uuid4().hex}.sqlite"
    store = SQLiteStore(db_path)
    store.init_schema()
    return store


class AlphaVantageNewsConnectorTests(unittest.TestCase):
    def test_alpha_vantage_response_maps_to_news_event(self) -> None:
        captured: list[str] = []

        def fake_fetch(url: str, headers: dict[str, str] | None) -> dict:
            captured.append(url)
            return {
                "feed": [
                    {
                        "title": "Bitcoin breaks higher",
                        "summary": "Bitcoin rallies as risk appetite improves.",
                        "source": "Example Wire",
                        "url": "https://example.test/btc",
                        "time_published": "20260428T101530",
                        "overall_sentiment_score": "0.44",
                        "overall_sentiment_label": "Bullish",
                        "ticker_sentiment": [
                            {
                                "ticker": "CRYPTO:BTC",
                                "relevance_score": "0.91",
                                "ticker_sentiment_score": "0.49",
                            }
                        ],
                    }
                ]
            }

        connector = AlphaVantageNewsConnector(
            api_key="test-key",
            rate_file=Path(tempfile.mkdtemp()) / "alpha_rate.json",
            fetch_json=fake_fetch,
            sleeper=lambda _: None,
        )

        events = connector.fetch(
            "BTC-EUR",
            limit=50,
            from_dt=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
            to_dt=datetime(2026, 4, 28, 0, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.headline, "Bitcoin breaks higher")
        self.assertEqual(event.source, "Example Wire")
        self.assertEqual(event.published_at.isoformat(), "2026-04-28T10:15:30+00:00")
        self.assertAlmostEqual(event.sentiment, 0.44)
        self.assertAlmostEqual(event.relevance, 0.91)
        self.assertIn("Bullish", event.tags)
        self.assertEqual(event.metadata["ticker"], "CRYPTO:BTC")
        self.assertEqual(event.metadata["ticker_sentiment_score"], 0.49)

        query = urllib.parse.parse_qs(urllib.parse.urlparse(captured[0]).query)
        self.assertEqual(query["function"], ["NEWS_SENTIMENT"])
        self.assertEqual(query["tickers"], ["CRYPTO:BTC"])
        self.assertEqual(query["time_from"], ["20260401T0000"])
        self.assertEqual(query["time_to"], ["20260428T0000"])

    def test_alpha_vantage_rate_limit_guard_blocks_after_daily_budget(self) -> None:
        connector = AlphaVantageNewsConnector(
            api_key="test-key",
            rate_file=Path(tempfile.mkdtemp()) / "alpha_rate.json",
            daily_limit=1,
            fetch_json=lambda _url, _headers: {"feed": []},
            sleeper=lambda _: None,
        )

        self.assertEqual(connector.fetch("AAPL"), [])
        with self.assertRaises(RateLimitError):
            connector.fetch("AAPL")

    def test_alpha_vantage_crypto_ticker_mapping(self) -> None:
        connector = AlphaVantageNewsConnector(api_key="test-key", fetch_json=lambda _url, _headers: {"feed": []})

        self.assertEqual(connector.map_ticker("BTC-EUR"), "CRYPTO:BTC")
        self.assertEqual(connector.map_ticker("ETH-EUR"), "CRYPTO:ETH")
        self.assertEqual(connector.map_ticker("SOL-EUR"), "CRYPTO:SOL")
        self.assertEqual(connector.map_ticker("AAPL"), "AAPL")


class EODHDNewsConnectorTests(unittest.TestCase):
    def test_eodhd_response_maps_to_news_event(self) -> None:
        content = "Bitcoin market context. " * 40

        connector = EODHDNewsConnector(
            api_key="eodhd-key",
            rate_file=Path(tempfile.mkdtemp()) / "eodhd_rate.json",
            fetch_json=lambda _url, _headers: [
                {
                    "title": "Bitcoin historical depth",
                    "content": content,
                    "source": "EODHD Wire",
                    "link": "https://example.test/eodhd-1",
                    "date": "2026-04-27 09:30:00",
                    "symbols": ["BTC-USD.CC"],
                }
            ],
            sleeper=lambda _: None,
        )

        events = connector.fetch("BTC-EUR", limit=1)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.headline, "Bitcoin historical depth")
        self.assertEqual(event.source, "EODHD Wire")
        self.assertEqual(event.url, "https://example.test/eodhd-1")
        self.assertEqual(event.published_at.isoformat(), "2026-04-27T09:30:00+00:00")
        self.assertEqual(event.sentiment, 0.0)
        self.assertEqual(event.novelty, 0.5)
        self.assertEqual(event.relevance, 0.8)
        self.assertEqual(len(event.summary), 500)
        self.assertIn("BTC-USD.CC", event.tags)
        self.assertEqual(event.metadata["connector"], "eodhd-news")

    def test_eodhd_ticker_mapping(self) -> None:
        connector = EODHDNewsConnector(api_key="eodhd-key", fetch_json=lambda _url, _headers: [])

        self.assertEqual(connector.map_ticker("BTC-EUR"), "BTC-USD.CC")
        self.assertEqual(connector.map_ticker("ETH-EUR"), "ETH-USD.CC")
        self.assertEqual(connector.map_ticker("SOL-EUR"), "SOL-USD.CC")
        self.assertEqual(connector.map_ticker("AAPL"), "AAPL.US")
        self.assertEqual(connector.map_ticker("ASML"), "ASML.AS")
        self.assertEqual(connector.map_ticker("MSFT"), "MSFT.US")

    def test_eodhd_rate_limit_guard_blocks_after_daily_budget(self) -> None:
        connector = EODHDNewsConnector(
            api_key="eodhd-key",
            rate_file=Path(tempfile.mkdtemp()) / "eodhd_rate.json",
            daily_limit=1,
            fetch_json=lambda _url, _headers: [],
            sleeper=lambda _: None,
        )

        self.assertEqual(connector.fetch("AAPL"), [])
        with self.assertRaises(RateLimitError):
            connector.fetch("AAPL")

    def test_eodhd_paginates_with_offset(self) -> None:
        calls: list[str] = []

        def fake_fetch(url: str, headers: dict[str, str] | None) -> list[dict]:
            calls.append(url)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            offset = int(query["offset"][0])
            return [
                {
                    "title": f"Article {offset + index}",
                    "content": "Historical article.",
                    "source": "EODHD Wire",
                    "link": f"https://example.test/eodhd-{offset + index}",
                    "date": "2026-04-27 10:00:00",
                    "symbols": ["AAPL.US"],
                }
                for index in range(50 if offset == 0 else 10)
            ]

        connector = EODHDNewsConnector(
            api_key="eodhd-key",
            rate_file=Path(tempfile.mkdtemp()) / "eodhd_rate.json",
            fetch_json=fake_fetch,
            sleeper=lambda _: None,
        )

        events = connector.fetch_historical(
            "AAPL",
            datetime(2026, 4, 1, tzinfo=timezone.utc),
            datetime(2026, 4, 28, tzinfo=timezone.utc),
            max_articles=60,
        )

        self.assertEqual(len(events), 60)
        self.assertEqual(len(calls), 2)
        first_query = urllib.parse.parse_qs(urllib.parse.urlparse(calls[0]).query)
        second_query = urllib.parse.parse_qs(urllib.parse.urlparse(calls[1]).query)
        self.assertEqual(first_query["s"], ["AAPL.US"])
        self.assertEqual(first_query["offset"], ["0"])
        self.assertEqual(first_query["from"], ["2026-04-01"])
        self.assertEqual(first_query["to"], ["2026-04-28"])
        self.assertEqual(second_query["offset"], ["50"])


class NewsEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _fresh_store()
        self._original_store = main_module.store
        self._original_connectors = main_module.connectors
        main_module.store = self.store
        self.client = TestClient(app)

    def tearDown(self) -> None:
        main_module.store = self._original_store
        main_module.connectors = self._original_connectors

    def test_news_historical_endpoint_returns_events(self) -> None:
        event = NewsEvent(
            id="evt_hist_1",
            asset="AAPL",
            headline="Apple historical article",
            source="Fake EODHD",
            published_at=datetime(2026, 4, 20, tzinfo=timezone.utc),
            sentiment=0.0,
        )

        class FakeConnectors:
            def fetch_historical_news(self, **kwargs) -> list[NewsEvent]:
                self.kwargs = kwargs
                return [event]

        fake = FakeConnectors()
        main_module.connectors = fake

        response = self.client.get(
            "/news/historical?asset=AAPL&connector=eodhd-news&from_dt=2026-04-01T00:00:00Z&to_dt=2026-04-28T00:00:00Z"
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["connector"], "eodhd-news")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["events"][0]["headline"], "Apple historical article")
        self.assertEqual(len(self.store.list_events(asset="AAPL")), 1)
        self.assertEqual(fake.kwargs["connector"], "eodhd-news")

    def test_news_sentiment_endpoint_calculates_average_sentiment(self) -> None:
        events = [
            NewsEvent(id="evt_sent_1", asset="BTC-EUR", headline="Good", sentiment=0.6),
            NewsEvent(id="evt_sent_2", asset="BTC-EUR", headline="Mixed", sentiment=-0.2),
        ]

        class FakeConnectors:
            def fetch_historical_news(self, **kwargs) -> list[NewsEvent]:
                self.kwargs = kwargs
                return events

        fake = FakeConnectors()
        main_module.connectors = fake

        response = self.client.get("/news/sentiment?asset=BTC-EUR&limit=50&ingest=false")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["connector"], "alphavantage-news")
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["avg_sentiment"], 0.2)
        self.assertEqual(len(self.store.list_events()), 0)
        self.assertEqual(fake.kwargs["connector"], "alphavantage-news")


class ConnectorAvailabilityTests(unittest.TestCase):
    def test_missing_env_vars_mark_real_news_connectors_unavailable(self) -> None:
        with patch.dict(
            os.environ,
            {"ALPHAVANTAGE_API_KEY": "", "EODHD_API_KEY": ""},
        ):
            registry = ConnectorRegistry()
            connectors = {item["name"]: item for item in registry.list_connectors()}

        self.assertFalse(connectors["alphavantage-news"]["available"])
        self.assertIn("ALPHAVANTAGE_API_KEY", connectors["alphavantage-news"]["unavailable_reason"])
        self.assertFalse(connectors["eodhd-news"]["available"])
        self.assertIn("EODHD_API_KEY", connectors["eodhd-news"]["unavailable_reason"])


if __name__ == "__main__":
    unittest.main()
