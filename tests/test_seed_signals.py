from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

import scripts.seed_backtest_signals as seed_script
import watchtower.main as main_module
from watchtower.domain import MarketSnapshot, NewsEvent


def test_seed_script_dry_run_writes_no_file(monkeypatch, tmp_path, capsys):
    fake = _FakeConnectors()
    monkeypatch.setattr(seed_script, "ConnectorRegistry", lambda: fake)
    output = tmp_path / "seed.jsonl"

    rc = seed_script.main([
        "--from", "2026-03-01T00:00:00Z",
        "--to", "2026-04-28T00:00:00Z",
        "--assets", "BTC-EUR",
        "--exchange", "BITVAVO",
        "--output", str(output),
        "--dry-run",
    ])

    assert rc == 0
    assert not output.exists()
    captured = capsys.readouterr().out
    assert "Zou 1 signalen genereren voor 1 artikelen" in captured


def test_seed_script_generates_signals_from_mock_news(monkeypatch, tmp_path):
    fake = _FakeConnectors()
    monkeypatch.setattr(seed_script, "ConnectorRegistry", lambda: fake)
    monkeypatch.setattr(seed_script, "CryptoMarketAdapter", lambda: _FakeCryptoMarket())
    monkeypatch.setattr(seed_script, "_score_signal", _fake_score_signal)
    output = tmp_path / "seed.jsonl"

    rc = seed_script.main([
        "--from", "2026-03-01T00:00:00Z",
        "--to", "2026-04-28T00:00:00Z",
        "--assets", "BTC-EUR",
        "--exchange", "BITVAVO",
        "--output", str(output),
    ])

    assert rc == 0
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["asset"] == "BTC-EUR"
    assert rows[0]["direction"] == "long"
    assert rows[0]["timestamp"] == "2026-03-15T10:00:00+00:00"
    assert rows[0]["entry_score"] == 0.81
    assert rows[0]["seed_market_quality"] == "historical"


def test_backtest_seed_endpoint_filters_asset(monkeypatch, tmp_path):
    _write_seed_file(tmp_path)
    monkeypatch.setenv("WATCHTOWER_DATA_DIR", str(tmp_path))
    client = TestClient(main_module.app)

    resp = client.get("/backtest/signals/seed?asset=BTC-EUR")

    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert {item["asset"] for item in data["signals"]} == {"BTC-EUR"}


def test_backtest_seed_endpoint_filters_min_entry_score(monkeypatch, tmp_path):
    _write_seed_file(tmp_path)
    monkeypatch.setenv("WATCHTOWER_DATA_DIR", str(tmp_path))
    client = TestClient(main_module.app)

    resp = client.get("/backtest/signals/seed?min_entry_score=0.7")

    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert all(item["entry_score"] >= 0.7 for item in data["signals"])


def test_backtest_seed_endpoint_filters_time_range(monkeypatch, tmp_path):
    _write_seed_file(tmp_path)
    monkeypatch.setenv("WATCHTOWER_DATA_DIR", str(tmp_path))
    client = TestClient(main_module.app)

    resp = client.get("/backtest/signals/seed?from_dt=2026-03-20T00:00:00Z&to_dt=2026-04-01T00:00:00Z")

    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["signals"][0]["signal_id"] == "seed-eth"


def test_backtest_seed_endpoint_empty_file_has_zero_count(monkeypatch, tmp_path):
    monkeypatch.setenv("WATCHTOWER_DATA_DIR", str(tmp_path))
    client = TestClient(main_module.app)

    resp = client.get("/backtest/signals/seed")

    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    assert resp.json()["signals"] == []


class _FakeConnectors:
    def fetch_historical_news(self, connector, asset, from_dt=None, to_dt=None, limit=500):
        if connector == "eodhd-news":
            return [
                NewsEvent(
                    id="evt-eodhd-btc",
                    asset=asset,
                    headline="Bitcoin seed article",
                    source="EODHD",
                    published_at=datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc),
                    sentiment=0.0,
                    relevance=0.8,
                    novelty=0.5,
                )
            ]
        if connector == "alphavantage-news":
            return [
                NewsEvent(
                    id="evt-av-btc",
                    asset=asset,
                    headline="Bitcoin sentiment article",
                    source="Alpha Vantage",
                    published_at=datetime(2026, 3, 15, 11, 0, tzinfo=timezone.utc),
                    sentiment=0.6,
                    relevance=0.9,
                )
            ]
        return []

    def fetch_market(self, connector, asset, exchange=None):
        return MarketSnapshot(
            asset=asset,
            price=60000.0,
            volume_zscore=2.0,
            trend_1h=0.5,
            trend_1d=0.4,
            benchmark_change_1d_pct=1.0,
        )


class _FakeCryptoMarket:
    def fetch_historical_snapshot(self, asset, at, interval="1h"):
        return MarketSnapshot(
            asset=asset,
            price=60000.0,
            volume_zscore=1.0,
            trend_1h=0.4,
            trend_1d=0.0,
            benchmark_change_1d_pct=0.0,
        )


def _fake_score_signal(event, market, exchange, asset_info):
    return {
        "id": f"sig-{event.id}",
        "event_id": event.id,
        "asset": market.asset,
        "direction": "long",
        "entry_score": 0.81,
        "confidence": 0.74,
        "time_window": "1h",
        "reason": "mock seed signal",
        "risk_flags": [],
        "components": {},
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc),
        "exchange": exchange.code if exchange else "BITVAVO",
        "region": exchange.region if exchange else "Crypto",
        "asset_class": "crypto",
        "source_field": "crypto",
        "linked_assets": ["NASDAQ:QQQ"],
        "linked_markets": [],
        "intermarket_drivers": ["btc_vs_qqq_risk_beta"],
    }


def _write_seed_file(data_dir: Path) -> None:
    signals = [
        {
            "signal_id": "seed-btc-low",
            "asset": "BTC-EUR",
            "direction": "long",
            "timestamp": "2026-03-10T10:00:00+00:00",
            "entry_score": 0.55,
        },
        {
            "signal_id": "seed-btc-high",
            "asset": "BTC-EUR",
            "direction": "long",
            "timestamp": "2026-04-10T10:00:00+00:00",
            "entry_score": 0.82,
        },
        {
            "signal_id": "seed-eth",
            "asset": "ETH-EUR",
            "direction": "long",
            "timestamp": "2026-03-25T10:00:00+00:00",
            "entry_score": 0.75,
        },
    ]
    path = data_dir / "backtest_signals_seed.jsonl"
    path.write_text("\n".join(json.dumps(item) for item in signals), encoding="utf-8")
