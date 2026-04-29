from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

import watchtower.main as main_module
from watchtower.main import app
from watchtower.storage import SQLiteStore


def test_dashboard_intelligence_empty_database_has_safe_defaults(monkeypatch, tmp_path):
    _patch_store(monkeypatch, tmp_path)
    monkeypatch.setenv("WATCHTOWER_DATA_DIR", str(tmp_path / "data"))
    client = TestClient(app)

    response = client.get("/dashboard/intelligence")

    assert response.status_code == 200
    data = response.json()
    assert data["positive_signals"] == []
    assert data["recommended_assets"] == []
    assert data["backtest_status"]["seed_signals_available"] == 0
    assert data["backtest_status"]["calibration_basis"] == "BOOTSTRAPPING"
    assert data["intermarket_context"]


def test_dashboard_intelligence_positive_signals_exclude_neutral(monkeypatch, tmp_path):
    store = _patch_store(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    store.save_signal(_signal("sig-long", "BTC-EUR", "long", 0.72, now))
    store.save_signal(_signal("sig-neutral", "ETH-EUR", "neutral", 0.88, now))
    store.save_signal(_signal("sig-short", "SOL-EUR", "short", 0.66, now))
    client = TestClient(app)

    data = client.get("/dashboard/intelligence").json()

    directions = {signal["direction"] for signal in data["positive_signals"]}
    assert directions == {"long", "short"}
    assert all(signal["direction"] != "neutral" for signal in data["positive_signals"])
    assert data["positive_signals"][0]["asset"] == "BTC-EUR"


def test_dashboard_intelligence_recommended_assets_require_min_avg_score(monkeypatch, tmp_path):
    store = _patch_store(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    store.save_signal(_signal("sig-btc-1", "BTC-EUR", "long", 0.8, now))
    store.save_signal(_signal("sig-btc-2", "BTC-EUR", "long", 0.6, now - timedelta(minutes=5)))
    store.save_signal(_signal("sig-eth-low", "ETH-EUR", "long", 0.49, now))
    client = TestClient(app)

    data = client.get("/dashboard/intelligence").json()

    assets = {item["asset"]: item for item in data["recommended_assets"]}
    assert set(assets) == {"BTC-EUR"}
    assert assets["BTC-EUR"]["bias"] == "LONG"
    assert assets["BTC-EUR"]["avg_entry_score"] == 0.7


def test_dashboard_intelligence_counts_seed_and_colony_feedback(monkeypatch, tmp_path):
    store = _patch_store(monkeypatch, tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("WATCHTOWER_DATA_DIR", str(data_dir))
    (data_dir / "backtest_signals_seed.jsonl").write_text(
        '{"signal_id":"seed-1","asset":"BTC-EUR","entry_score":0.6}\n'
        '{"signal_id":"seed-2","asset":"ETH-EUR","entry_score":0.7}\n',
        encoding="utf-8",
    )
    store.save_colony_feedback({
        "feedback_type": "TRADE_OUTCOME",
        "asset": "BTC-EUR",
        "biome": "CRYPTO",
        "is_replay": True,
        "data_quality": "HIGH",
        "payload": {},
    })
    store.save_colony_feedback({
        "feedback_type": "SIGNAL_SKIP",
        "asset": "ETH-EUR",
        "biome": "CRYPTO",
        "is_replay": False,
        "data_quality": "HIGH",
        "skip_reason": "REGIME_FILTER",
    })
    client = TestClient(app)

    data = client.get("/dashboard/intelligence").json()

    status = data["backtest_status"]
    assert status["seed_signals_available"] == 2
    assert status["colony_feedback_received"] == {"trades": 1, "skips": 1}
    assert data["colony_feedback_summary"]["calibration_basis"] == "BOOTSTRAPPING"


def _patch_store(monkeypatch, tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / f"watchtower_{uuid4().hex}.sqlite")
    store.init_schema()
    monkeypatch.setattr(main_module, "store", store)
    return store


def _signal(
    signal_id: str,
    asset: str,
    direction: str,
    score: float,
    created_at: datetime,
) -> dict:
    return {
        "id": signal_id,
        "event_id": f"evt-{signal_id}",
        "asset": asset,
        "exchange": "BITVAVO" if "-" in asset else "NASDAQ",
        "region": "Crypto" if "-" in asset else "North America",
        "direction": direction,
        "entry_score": score,
        "confidence": 0.7,
        "time_window": "1h",
        "reason": "historical dashboard intelligence test signal with enough detail to trim",
        "risk_flags": [],
        "components": {},
        "created_at": created_at.isoformat(),
        "expires_at": (created_at + timedelta(hours=1)).isoformat(),
        "asset_class": "crypto" if "-" in asset else "equity",
    }
