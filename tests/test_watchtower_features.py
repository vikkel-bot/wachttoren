from __future__ import annotations

import unittest
import os
import json
import sqlite3
from pathlib import Path
from datetime import datetime
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from watchtower.domain import EntrySignal, MarketSnapshot, OutcomeEvaluation
from watchtower.services.assets import ListedAssetUniverse
from watchtower.services.colony import ColonyBridge
from watchtower.services.connectors import ConnectorRegistry
from watchtower.services.crypto_market import CryptoMarketAdapter
from watchtower.services.exchanges import ExchangeUniverse, TradingSessionDetector
from watchtower.services.intermarket import IntermarketEngine
from watchtower.services.market_context import MarketContextEngine
from watchtower.services.news_radar import NewsRadar
from watchtower.services.regional_scoring import RegionalEntryScorer
from watchtower.services.scoring import EntryScorer
from watchtower.storage import SQLiteStore


class ConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_patch = patch.dict(os.environ, {"TEST_MODE": "true"})
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()

    def test_mock_news_connector_returns_events(self) -> None:
        events = ConnectorRegistry().fetch_news("mock-news", "AAPL", limit=2, exchange="NASDAQ")

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].asset, "AAPL")
        self.assertGreater(events[0].relevance, 0.5)

    def test_mock_market_connector_is_exchange_aware(self) -> None:
        snapshot = ConnectorRegistry().fetch_market("mock-market", "AAPL", exchange="NASDAQ")

        self.assertEqual(snapshot.asset, "AAPL")
        self.assertGreaterEqual(snapshot.volume_zscore, 2.0)

    def test_bitvavo_public_connector_normalizes_crypto_snapshot(self) -> None:
        def fake_fetch(url: str):
            if "/ticker/price?market=BTC-EUR" in url:
                return [{"market": "BTC-EUR", "price": "106.0"}]
            return [
                [1, "100", "101", "99", "100", "10"],
                [2, "100", "104", "99", "103", "13"],
                [3, "103", "107", "102", "106", "20"],
            ]

        registry = ConnectorRegistry()
        registry.crypto_market = CryptoMarketAdapter(fetch_json=fake_fetch)

        snapshot = registry.fetch_market("bitvavo-public", "BTC", exchange="BITVAVO")

        self.assertEqual(snapshot.asset, "BTC-EUR")
        self.assertEqual(snapshot.price, 106.0)
        self.assertGreater(snapshot.change_1h_pct, 0)
        self.assertGreater(snapshot.volume_zscore, 0)

    def test_bitvavo_public_connector_builds_synthetic_eth_btc_ratio(self) -> None:
        def fake_fetch(url: str):
            if "/ticker/price?market=ETH-EUR" in url:
                return {"market": "ETH-EUR", "price": "2000.0"}
            if "/ticker/price?market=BTC-EUR" in url:
                return {"market": "BTC-EUR", "price": "50000.0"}
            if "/ETH-EUR/candles" in url:
                return [
                    [1, "1900", "1950", "1880", "1920", "10"],
                    [2, "1920", "2020", "1910", "2000", "12"],
                ]
            if "/BTC-EUR/candles" in url:
                return [
                    [1, "48000", "49000", "47000", "48500", "20"],
                    [2, "48500", "50500", "48000", "50000", "22"],
                ]
            raise AssertionError(f"Unexpected url: {url}")

        registry = ConnectorRegistry()
        registry.crypto_market = CryptoMarketAdapter(fetch_json=fake_fetch)

        snapshot = registry.fetch_market("bitvavo-public", "ETH-BTC", exchange="BITVAVO")

        self.assertEqual(snapshot.asset, "ETH-BTC")
        self.assertAlmostEqual(snapshot.price, 0.04)
        self.assertGreater(snapshot.change_1h_pct, 0)

    def test_mock_connectors_hidden_outside_test_mode(self) -> None:
        with patch.dict(os.environ, {"TEST_MODE": "false"}):
            registry = ConnectorRegistry()
            names = {item["name"] for item in registry.list_connectors()}
            self.assertNotIn("mock-news", names)
            self.assertNotIn("mock-market", names)
            with self.assertRaises(ValueError):
                registry.fetch_news("mock-news", "AAPL")


class WatchlistStorageTests(unittest.TestCase):
    def test_watchlist_roundtrip(self) -> None:
        db_path = Path.cwd() / f"test_watchlist_roundtrip_{uuid4().hex}.sqlite"
        try:
            store = SQLiteStore(db_path)
            store.init_schema()

            item = store.upsert_watchlist_item(
                {
                    "exchange": "NASDAQ",
                    "asset": "aapl",
                    "region": "North America",
                    "currency": "USD",
                    "enabled": True,
                    "min_entry_score": 0.72,
                    "min_confidence": 0.6,
                    "max_signals_per_hour": 4,
                    "notes": "core equity",
                }
            )

            self.assertEqual(item["key"], "NASDAQ:AAPL")
            self.assertEqual(store.get_watchlist_item("AAPL", exchange="NASDAQ")["min_entry_score"], 0.72)
            self.assertEqual(len(store.list_watchlist(enabled_only=True, exchange="NASDAQ")), 1)
        finally:
            db_path.unlink(missing_ok=True)

    def test_learning_summary_calculates_hit_rate(self) -> None:
        db_path = Path.cwd() / f"test_learning_summary_{uuid4().hex}.sqlite"
        try:
            store = SQLiteStore(db_path)
            store.init_schema()
            signal = {
                "id": "sig_1",
                "event_id": "evt_1",
                "asset": "AAPL",
                "exchange": "NASDAQ",
                "region": "North America",
                "direction": "long",
                "entry_score": 0.8,
                "confidence": 0.7,
                "time_window": "15m",
                "reason": "test",
                "risk_flags": [],
                "components": {},
                "created_at": "2026-04-26T10:00:00+00:00",
            }
            store.save_signal(signal)
            store.save_signal({**signal, "id": "sig_2", "asset": "BTC-EUR", "asset_class": "crypto", "created_at": "2026-04-27T10:00:00+00:00"})
            store.save_outcome(
                OutcomeEvaluation(
                    signal_id="sig_1",
                    window="15m",
                    entry_price=100,
                    future_price=101,
                    return_pct=1.0,
                    hit=True,
                )
            )

            summary = store.learning_summary()

            self.assertEqual(summary["total_outcomes"], 1)
            self.assertEqual(summary["hit_rate"], 1.0)
            self.assertEqual(summary["by_asset"]["AAPL"]["avg_return_pct"], 1.0)
            self.assertEqual(summary["by_exchange"]["NASDAQ"]["hit_rate"], 1.0)

            exported = store.export_signals(asset_class="crypto", from_ts="2026-04-27T00:00:00+00:00")
            self.assertEqual(len(exported), 1)
            self.assertEqual(exported[0]["asset"], "BTC-EUR")
        finally:
            db_path.unlink(missing_ok=True)

    def test_save_signal_dedupes_one_asset_per_hour(self) -> None:
        db_path = Path.cwd() / f"test_signal_dedupe_{uuid4().hex}.sqlite"
        try:
            store = SQLiteStore(db_path)
            store.init_schema()
            base_signal = {
                "id": "sig_low",
                "event_id": "evt_low",
                "asset": "ETH-EUR",
                "exchange": "BITVAVO",
                "region": "Crypto",
                "direction": "long",
                "entry_score": 0.55,
                "confidence": 0.6,
                "time_window": "1h",
                "reason": "test",
                "risk_flags": [],
                "components": {},
                "created_at": "2026-04-28T10:12:00+00:00",
            }

            store.save_signal(base_signal)
            store.save_signal(
                {
                    **base_signal,
                    "id": "sig_high",
                    "event_id": "evt_high",
                    "entry_score": 0.72,
                    "created_at": "2026-04-28T10:48:00+00:00",
                }
            )

            signals = store.list_signals(limit=10, asset="ETH-EUR")
            self.assertEqual(len(signals), 1)
            self.assertEqual(signals[0]["id"], "sig_high")
            self.assertEqual(signals[0]["entry_score"], 0.72)
        finally:
            db_path.unlink(missing_ok=True)

    def test_save_signal_ignores_weaker_duplicate_after_stronger_signal(self) -> None:
        db_path = Path.cwd() / f"test_signal_insert_ignore_{uuid4().hex}.sqlite"
        try:
            store = SQLiteStore(db_path)
            store.init_schema()
            strong_signal = {
                "id": "sig_strong",
                "event_id": "evt_strong",
                "asset": "BTC-EUR",
                "exchange": "BITVAVO",
                "region": "Crypto",
                "direction": "long",
                "entry_score": 0.88,
                "confidence": 0.7,
                "time_window": "1h",
                "reason": "strong",
                "risk_flags": [],
                "components": {},
                "created_at": "2026-05-06T07:12:00+00:00",
            }
            weak_signal = {
                **strong_signal,
                "id": "sig_weak",
                "event_id": "evt_weak",
                "entry_score": 0.58,
                "confidence": 0.5,
                "reason": "weak duplicate",
                "created_at": "2026-05-06T07:45:00+00:00",
            }

            store.save_signal(strong_signal)
            returned = store.save_signal(weak_signal)

            signals = store.list_signals(limit=10, asset="BTC-EUR")
            self.assertEqual(len(signals), 1)
            self.assertEqual(signals[0]["id"], "sig_strong")
            self.assertEqual(returned["id"], "sig_strong")
        finally:
            db_path.unlink(missing_ok=True)

    def test_init_schema_migrates_signal_hour_bucket_unique_index(self) -> None:
        db_path = Path.cwd() / f"test_signal_hour_migration_{uuid4().hex}.sqlite"
        low_signal = {
            "id": "sig_low",
            "asset": "BTC-EUR",
            "created_at": "2026-05-06T07:10:00+00:00",
            "entry_score": 0.3,
            "confidence": 0.4,
        }
        high_signal = {
            **low_signal,
            "id": "sig_high",
            "created_at": "2026-05-06T07:55:00+00:00",
            "entry_score": 0.8,
            "confidence": 0.7,
        }
        try:
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE signals (
                        id TEXT PRIMARY KEY,
                        asset TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO signals (id, asset, created_at, payload) VALUES (?, ?, ?, ?)",
                    ("sig_low", "BTC-EUR", low_signal["created_at"], json.dumps(low_signal)),
                )
                conn.execute(
                    "INSERT INTO signals (id, asset, created_at, payload) VALUES (?, ?, ?, ?)",
                    ("sig_high", "BTC-EUR", high_signal["created_at"], json.dumps(high_signal)),
                )
                conn.commit()
            finally:
                conn.close()

            store = SQLiteStore(db_path)
            store.init_schema()

            signals = store.list_signals(limit=10, asset="BTC-EUR")
            self.assertEqual(len(signals), 1)
            self.assertEqual(signals[0]["id"], "sig_high")

            conn = sqlite3.connect(db_path)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(signals)").fetchall()}
                indexes = conn.execute("PRAGMA index_list(signals)").fetchall()
            finally:
                conn.close()

            self.assertIn("hour_bucket", columns)
            unique_indexes = {row[1] for row in indexes if row[2]}
            self.assertIn("idx_signals_asset_hour_utc", unique_indexes)
            self.assertIn("idx_signals_asset_created_hour_utc", unique_indexes)
        finally:
            db_path.unlink(missing_ok=True)

    def test_signal_deduplication_migration_reports_removed_count_and_backup(self) -> None:
        db_path = Path.cwd() / f"test_signal_deduplication_report_{uuid4().hex}.sqlite"
        low_signal = {
            "id": "sig_low",
            "asset": "SOL-EUR",
            "created_at": "2026-05-06T08:05:00+00:00",
            "entry_score": 0.2,
            "confidence": 0.3,
        }
        high_signal = {
            **low_signal,
            "id": "sig_high",
            "created_at": "2026-05-06T08:55:00+00:00",
            "entry_score": 0.9,
            "confidence": 0.8,
        }
        try:
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE signals (
                        id TEXT PRIMARY KEY,
                        asset TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO signals (id, asset, created_at, payload) VALUES (?, ?, ?, ?)",
                    ("sig_low", "SOL-EUR", low_signal["created_at"], json.dumps(low_signal)),
                )
                conn.execute(
                    "INSERT INTO signals (id, asset, created_at, payload) VALUES (?, ?, ?, ?)",
                    ("sig_high", "SOL-EUR", high_signal["created_at"], json.dumps(high_signal)),
                )
                conn.commit()
            finally:
                conn.close()

            store = SQLiteStore(db_path)
            report = store.migrate_signal_deduplication()

            signals = store.list_signals(limit=10, asset="SOL-EUR")
            self.assertEqual(report["removed"], 1)
            self.assertTrue(report["constraint_active"])
            self.assertIsNotNone(report["backup_table"])
            self.assertEqual(len(signals), 1)
            self.assertEqual(signals[0]["id"], "sig_high")

            conn = sqlite3.connect(db_path)
            try:
                backup_count = conn.execute(f'SELECT COUNT(*) FROM "{report["backup_table"]}"').fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(backup_count, 2)
        finally:
            db_path.unlink(missing_ok=True)

    def test_signal_unique_index_blocks_same_asset_hour(self) -> None:
        db_path = Path.cwd() / f"test_signal_unique_index_{uuid4().hex}.sqlite"
        try:
            store = SQLiteStore(db_path)
            store.init_schema()
            signal = {
                "id": "sig_one",
                "asset": "BTC-EUR",
                "created_at": "2026-05-06T07:10:00+00:00",
            }
            with store._connect() as conn:
                conn.execute(
                    "INSERT INTO signals (id, asset, created_at, hour_bucket, payload) VALUES (?, ?, ?, ?, ?)",
                    (
                        "sig_one",
                        "BTC-EUR",
                        "2026-05-06T07:10:00+00:00",
                        "2026-05-06T07:00:00+00:00",
                        json.dumps(signal),
                    ),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO signals (id, asset, created_at, hour_bucket, payload) VALUES (?, ?, ?, ?, ?)",
                        (
                            "sig_two",
                            "BTC-EUR",
                            "2026-05-06T07:30:00+00:00",
                            "2026-05-06T07:00:00+00:00",
                            json.dumps({**signal, "id": "sig_two"}),
                        ),
                    )
        finally:
            db_path.unlink(missing_ok=True)

    def test_insert_or_ignore_duplicate_signal_is_graceful(self) -> None:
        db_path = Path.cwd() / f"test_signal_insert_or_ignore_direct_{uuid4().hex}.sqlite"
        try:
            store = SQLiteStore(db_path)
            store.init_schema()
            signal = {
                "id": "sig_one",
                "asset": "BTC-EUR",
                "created_at": "2026-05-06T07:10:00+00:00",
            }
            with store._connect() as conn:
                conn.execute(
                    "INSERT INTO signals (id, asset, created_at, hour_bucket, payload) VALUES (?, ?, ?, ?, ?)",
                    (
                        "sig_one",
                        "BTC-EUR",
                        "2026-05-06T07:10:00+00:00",
                        "2026-05-06T07:00:00+00:00",
                        json.dumps(signal),
                    ),
                )
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO signals (id, asset, created_at, hour_bucket, payload) VALUES (?, ?, ?, ?, ?)",
                    (
                        "sig_two",
                        "BTC-EUR",
                        "2026-05-06T07:30:00+00:00",
                        "2026-05-06T07:00:00+00:00",
                        json.dumps({**signal, "id": "sig_two"}),
                    ),
                )
                self.assertEqual(cursor.rowcount, 0)

            signals = store.list_signals(limit=10, asset="BTC-EUR")
            self.assertEqual(len(signals), 1)
            self.assertEqual(signals[0]["id"], "sig_one")
        finally:
            db_path.unlink(missing_ok=True)


class ExchangeUniverseTests(unittest.TestCase):
    def test_universe_includes_target_regions(self) -> None:
        universe = ExchangeUniverse()

        codes = {exchange["code"] for exchange in universe.list()}

        self.assertIn("AEX", codes)
        self.assertIn("NASDAQ", codes)
        self.assertIn("HKEX", codes)
        self.assertIn("JSE", codes)
        self.assertIn("COMEX", codes)
        self.assertIn("BITVAVO", codes)

    def test_crypto_exchange_is_continuous_on_weekend(self) -> None:
        universe = ExchangeUniverse()
        detector = TradingSessionDetector(universe)
        moment = datetime(2026, 4, 26, 10, 0, tzinfo=ZoneInfo("Europe/Amsterdam"))

        status = detector.status("BITVAVO", moment)

        self.assertTrue(status["is_trading"])
        self.assertEqual(status["session"], "continuous")

    def test_exchange_session_detects_regular_trading(self) -> None:
        universe = ExchangeUniverse()
        detector = TradingSessionDetector(universe)
        moment = datetime(2026, 4, 27, 10, 0, tzinfo=ZoneInfo("Europe/Amsterdam"))

        status = detector.status("AEX", moment)

        self.assertTrue(status["is_trading"])
        self.assertTrue(status["is_regular"])
        self.assertEqual(status["session"], "regular")

    def test_exchange_session_detects_weekend(self) -> None:
        universe = ExchangeUniverse()
        detector = TradingSessionDetector(universe)
        moment = datetime(2026, 4, 26, 10, 0, tzinfo=ZoneInfo("Europe/Amsterdam"))

        status = detector.status("AEX", moment)

        self.assertFalse(status["is_trading"])
        self.assertEqual(status["reason"], "weekend")

    def test_listed_asset_resolves_by_exchange(self) -> None:
        universe = ExchangeUniverse()
        assets = ListedAssetUniverse(universe)

        resolved = assets.resolve("ASML reports stronger demand", exchange="AEX")

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["key"], "AEX:ASML")

    def test_listed_assets_distinguish_equities_and_commodities(self) -> None:
        universe = ExchangeUniverse()
        assets = ListedAssetUniverse(universe)

        equities = assets.list(asset_class="equity")
        commodities = assets.list(asset_class="commodity")
        crypto = assets.list(asset_class="crypto")

        self.assertTrue(any(item["key"] == "NASDAQ:AAPL" for item in equities))
        self.assertTrue(any(item["key"] == "COMEX:GOLD" for item in commodities))
        self.assertTrue(any(item["key"] == "BITVAVO:BTC-EUR" for item in crypto))


class RegionalScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_patch = patch.dict(os.environ, {"TEST_MODE": "true"})
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()

    def test_regional_scorer_adds_exchange_context(self) -> None:
        universe = ExchangeUniverse()
        detector = TradingSessionDetector(universe)
        scorer = RegionalEntryScorer(EntryScorer(), detector)
        event = ConnectorRegistry().fetch_news("mock-news", "AAPL", limit=1, exchange="NASDAQ")[0]
        market = ConnectorRegistry().fetch_market("mock-market", "AAPL", exchange="NASDAQ")

        signal = scorer.score(event, market, universe.require("NASDAQ"), asset_info={"sector": "Technology"})

        self.assertEqual(signal["exchange"], "NASDAQ")
        self.assertEqual(signal["region"], "North America")
        self.assertIn("market_session", signal)
        self.assertEqual(signal["sector"], "Technology")

    def test_intermarket_engine_links_copper_to_technology(self) -> None:
        universe = ExchangeUniverse()
        market = ConnectorRegistry().fetch_market("mock-market", "COPPER", exchange="LME")

        context = IntermarketEngine().context(
            {"symbol": "COPPER", "asset_class": "commodity", "sector": "Base Metals"},
            universe.require("LME"),
            market,
        )

        self.assertIn("industrial_growth", context["drivers"])
        self.assertIn("AEX:ASML", context["linked_assets"])
        self.assertIn("data_centers", context["drivers"])

    def test_intermarket_engine_links_crypto_cross_fields(self) -> None:
        universe = ExchangeUniverse()
        market = MarketSnapshot(asset="BTC-EUR", price=60000, volume_zscore=2.0, trend_1d=0.4)

        context = IntermarketEngine().context(
            {"symbol": "BTC-EUR", "asset_class": "crypto", "sector": "Crypto"},
            universe.require("BITVAVO"),
            market,
        )

        self.assertEqual(context["asset_class"], "crypto")
        self.assertIn("btc_vs_qqq_risk_beta", context["drivers"])
        self.assertIn("btc_vs_dxy_usd_liquidity", context["drivers"])
        self.assertIn("NASDAQ:QQQ", context["linked_assets"])
        self.assertIn("FX:DXY", context["linked_assets"])

    def test_intermarket_dashboard_links_include_effects(self) -> None:
        links = IntermarketEngine().dashboard_links()

        self.assertTrue(any(link["theme"] == "Electrification and infrastructure" for link in links))
        self.assertTrue(all(link.get("effect") for link in links))


class NewsRadarTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_patch = patch.dict(os.environ, {"TEST_MODE": "true"})
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()

    def test_mock_scan_stays_inside_starter_budget(self) -> None:
        radar = NewsRadar(ListedAssetUniverse(ExchangeUniverse()), monthly_budget_eur=25.0)

        report = radar.scan(mode="mock", limit=4)

        self.assertEqual(report["budget"]["estimated_monthly_cost_eur"], 0.0)
        self.assertLessEqual(report["budget"]["estimated_monthly_cost_eur"], 25.0)
        self.assertGreater(report["count"], 0)
        self.assertTrue(any(event.metadata["radar"]["impact"] in {"high", "medium"} for event in report["events"]))

    def test_news_radar_links_headlines_to_themes_and_assets(self) -> None:
        radar = NewsRadar(ListedAssetUniverse(ExchangeUniverse()))

        report = radar.scan(mode="mock", limit=4)
        copper_event = next(event for event in report["events"] if "Copper jumps" in event.headline)

        self.assertEqual(copper_event.asset, "COPPER")
        self.assertIn("metals", copper_event.metadata["radar"]["themes"])
        self.assertIn("technology", copper_event.metadata["radar"]["themes"])

    def test_mock_scan_blocked_outside_test_mode(self) -> None:
        with patch.dict(os.environ, {"TEST_MODE": "false"}):
            radar = NewsRadar(ListedAssetUniverse(ExchangeUniverse()))
            with self.assertRaises(ValueError):
                radar.scan(mode="mock", limit=4)


class RegimeTests(unittest.TestCase):
    def test_volatile_regime_report(self) -> None:
        report = MarketContextEngine().regime_report(
            MarketSnapshot(asset="BTC", price=65000, volatility_zscore=3.0, trend_1d=0.1)
        )

        self.assertEqual(report["regime"], "volatile")
        self.assertIn("volatility_zscore_above_2_5", report["drivers"])


class ColonyBridgeTests(unittest.TestCase):
    def test_colony_filters_by_watchlist_thresholds(self) -> None:
        bridge = ColonyBridge()
        signals = [
            {
                "id": "sig_good",
                "asset": "AAPL",
                "direction": "long",
                "entry_score": 0.82,
                "confidence": 0.71,
                "expires_at": "2999-01-01T00:00:00+00:00",
            },
            {
                "id": "sig_low",
                "asset": "AAPL",
                "direction": "long",
                "entry_score": 0.5,
                "confidence": 0.71,
                "expires_at": "2999-01-01T00:00:00+00:00",
            },
        ]
        watchlist = [
            {
                "asset": "AAPL",
                "enabled": True,
                "min_entry_score": 0.7,
                "min_confidence": 0.6,
            }
        ]

        qualified = bridge.qualified_signals(signals, watchlist)

        self.assertEqual(len(qualified), 1)
        self.assertEqual(qualified[0]["id"], "sig_good")

    def test_colony_allows_commodity_threshold_floor(self) -> None:
        bridge = ColonyBridge()
        signals = [
            {
                "id": "sig_natgas",
                "asset": "NATGAS",
                "asset_class": "commodity",
                "direction": "long",
                "entry_score": 0.51,
                "confidence": 0.51,
                "expires_at": "2999-01-01T00:00:00+00:00",
            }
        ]
        watchlist = [
            {
                "asset": "NATGAS",
                "enabled": True,
                "min_entry_score": 0.7,
                "min_confidence": 0.55,
            }
        ]

        qualified = bridge.qualified_signals(signals, watchlist)

        self.assertEqual(len(qualified), 1)
        self.assertEqual(qualified[0]["id"], "sig_natgas")


if __name__ == "__main__":
    unittest.main()
