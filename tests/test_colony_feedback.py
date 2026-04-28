from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

import watchtower.main as main_module
from watchtower.main import app
from watchtower.models import MarketSnapshotIn, NewsEventIn
from watchtower.services.scoring import EntryScorer
from watchtower.storage import SQLiteStore, to_jsonable


def _fresh_store() -> SQLiteStore:
    db_path = Path(tempfile.mkdtemp()) / f"test_{uuid4().hex}.sqlite"
    store = SQLiteStore(db_path)
    store.init_schema()
    return store


class ColonyFeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _fresh_store()
        self._original_store = main_module.store
        main_module.store = self.store
        self.client = TestClient(app)

    def tearDown(self) -> None:
        main_module.store = self._original_store

    def _trade(self, **kwargs) -> dict:
        base = {
            "feedback_type": "TRADE_OUTCOME",
            "asset": "BTC-EUR",
            "biome": "CRYPTO",
            "timestamp": "2026-04-27T10:00:00Z",
            "direction": "LONG",
            "entry_price": 60000.0,
            "exit_price": 62000.0,
            "pnl_pct": 0.033,
            "exit_reason": "TP",
        }
        base.update(kwargs)
        return base

    def _create_signal(self, asset: str = "BTC-EUR") -> dict:
        scorer = EntryScorer()
        event = NewsEventIn(
            asset=asset,
            headline="Test headline",
            summary="Test summary",
            sentiment=0.8,
            novelty=0.7,
            relevance=0.9,
        ).to_domain(asset)
        market = MarketSnapshotIn(
            asset=asset,
            price=60000.0,
            change_1d_pct=2.5,
            volume_zscore=2.0,
        ).to_domain(asset)
        signal = scorer.score(event, market)
        return self.store.save_signal(to_jsonable(signal))

    def test_trade_outcome_without_signal_id(self) -> None:
        resp = self.client.post("/colony/feedback", json=self._trade())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["accepted"])
        self.assertFalse(data["signal_linked"])
        self.assertIn("feedback_id", data)

    def test_trade_outcome_with_valid_signal_id(self) -> None:
        saved_signal = self._create_signal()
        resp = self.client.post("/colony/feedback", json=self._trade(signal_id=saved_signal["id"]))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["accepted"])
        self.assertTrue(data["signal_linked"])

        outcomes = self.store.list_outcomes()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["signal_id"], saved_signal["id"])

    def test_signal_skip(self) -> None:
        resp = self.client.post("/colony/feedback", json={
            "feedback_type": "SIGNAL_SKIP",
            "asset": "ETH-EUR",
            "biome": "CRYPTO",
            "timestamp": "2026-04-27T10:00:00Z",
            "skip_reason": "REGIME_FILTER",
            "colony_regime": "SIDEWAYS",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["accepted"])

    def test_replay_10_trades(self) -> None:
        trades = [self._trade(asset=f"ASSET-{i}") for i in range(10)]
        resp = self.client.post("/colony/feedback/replay", json={
            "source": "colony_v2_ledger",
            "replay_id": "test-replay-001",
            "submitted_at": "2026-04-27T10:00:00Z",
            "trades": trades,
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["accepted"], 10)
        self.assertEqual(data["skipped_duplicate"], 0)

    def test_replay_duplicate_skipped(self) -> None:
        trades = [self._trade(asset=f"ASSET-{i}") for i in range(10)]
        payload = {
            "source": "colony_v2_ledger",
            "replay_id": "test-replay-dup",
            "submitted_at": "2026-04-27T10:00:00Z",
            "trades": trades,
        }
        self.client.post("/colony/feedback/replay", json=payload)
        resp = self.client.post("/colony/feedback/replay", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["skipped_duplicate"], 10)
        self.assertEqual(data["accepted"], 0)

    def test_stats_calibration_inactive_below_5(self) -> None:
        for i in range(4):
            self.client.post("/colony/feedback", json=self._trade(asset=f"ASSET-{i}"))
        resp = self.client.get("/colony/feedback/stats")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["calibration_active"])

    def test_stats_calibration_active_at_5(self) -> None:
        for i in range(5):
            self.client.post("/colony/feedback", json=self._trade(asset=f"ASSET-{i}"))
        resp = self.client.get("/colony/feedback/stats")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["calibration_active"])

    def test_outcomes_evaluate_endpoint_untouched(self) -> None:
        resp = self.client.post("/outcomes/evaluate", json={
            "signal_id": "nonexistent",
            "entry_price": 100.0,
            "future_price": 110.0,
        })
        self.assertEqual(resp.status_code, 404)

    def test_backtest_signals_endpoint_exports_colony_ready_payload(self) -> None:
        signal = self._create_signal(asset="BTC-EUR")
        signal["asset_class"] = "crypto"
        signal["linked_assets"] = ["NASDAQ:QQQ", "FX:DXY", "BITVAVO:ETH-BTC"]
        self.store.save_signal(signal)

        resp = self.client.get("/backtest/signals?asset_class=crypto")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["export_type"], "signals")
        self.assertEqual(data["count"], 1)
        exported = data["signals"][0]
        self.assertEqual(exported["signal_id"], signal["id"])
        self.assertEqual(exported["asset"], "BTC-EUR")
        self.assertEqual(exported["asset_class"], "crypto")
        self.assertEqual(exported["source_field"], "crypto")
        self.assertIn("NASDAQ:QQQ", exported["linked_assets"])


if __name__ == "__main__":
    unittest.main()
