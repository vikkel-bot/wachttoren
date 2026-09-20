from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import scripts.seed_backtest_signals as seed_script
from watchtower.domain import MarketSnapshot, NewsEvent
from watchtower.services.crypto_market import CryptoMarketAdapter


def test_fetch_historical_snapshot_maps_closest_bitvavo_candle():
    at = datetime(2026, 3, 15, 10, 20, tzinfo=timezone.utc)
    calls: list[str] = []

    def fake_fetch(url: str):
        calls.append(url)
        return [
            [1773565200000, "100", "110", "95", "105", "10"],
            [1773568800000, "200", "240", "190", "220", "11"],
            [1773572400000, "300", "315", "295", "303", "12"],
        ]

    adapter = CryptoMarketAdapter(fetch_json=fake_fetch, sleeper=lambda _seconds: None)

    snapshot = adapter.fetch_historical_snapshot("BTC-EUR", at)

    assert snapshot is not None
    assert snapshot.asset == "BTC-EUR"
    assert snapshot.price == 220.0
    assert snapshot.change_1h_pct == 10.0
    assert snapshot.volume_zscore == 0.0
    assert snapshot.trend_1h == 1.0
    assert "interval=1h" in calls[0]
    assert "limit=3" in calls[0]


def test_fetch_historical_snapshot_returns_none_when_no_candles():
    adapter = CryptoMarketAdapter(fetch_json=lambda _url: [], sleeper=lambda _seconds: None)

    snapshot = adapter.fetch_historical_snapshot("BTC-EUR", datetime(2026, 3, 15, 10, tzinfo=timezone.utc))

    assert snapshot is None


def test_historical_market_cache_reuses_same_hour_bucket():
    crypto_market = _CountingCryptoMarket()
    cache: dict[tuple[str, str], MarketSnapshot | None] = {}

    for minute in range(10):
        seed_script._fetch_historical_market(
            "BTC-EUR",
            datetime(2026, 3, 15, 10, minute, tzinfo=timezone.utc),
            crypto_market,
            cache,
        )

    assert crypto_market.calls == 1


def test_seed_asset_uses_historical_candles_so_entry_scores_vary(monkeypatch):
    monkeypatch.setattr(seed_script, "_score_signal", _score_from_market)
    crypto_market = _MappedCryptoMarket({
        "2026-03-15T10:00": MarketSnapshot(asset="BTC-EUR", price=60000, trend_1h=0.1, change_1h_pct=0.5),
        "2026-03-15T11:00": MarketSnapshot(asset="BTC-EUR", price=61000, trend_1h=0.8, change_1h_pct=4.0),
    })

    result = seed_script.seed_asset(
        asset="BTC-EUR",
        exchange="BITVAVO",
        from_dt=datetime(2026, 3, 15, tzinfo=timezone.utc),
        to_dt=datetime(2026, 3, 16, tzinfo=timezone.utc),
        connectors=_SeedConnectors(),
        crypto_market=crypto_market,
        candle_cache={},
    )

    scores = {signal["entry_score"] for signal in result["signals"]}
    assert result["historical_candles"] == 2
    assert result["degraded"] == 0
    assert len(scores) == 2
    assert {signal["seed_market_quality"] for signal in result["signals"]} == {"historical"}


def test_seed_asset_marks_degraded_when_historical_candle_missing(monkeypatch):
    monkeypatch.setattr(seed_script, "_score_signal", _score_from_market)

    result = seed_script.seed_asset(
        asset="BTC-EUR",
        exchange="BITVAVO",
        from_dt=datetime(2026, 3, 15, tzinfo=timezone.utc),
        to_dt=datetime(2026, 3, 16, tzinfo=timezone.utc),
        connectors=_SeedConnectors(),
        crypto_market=_MappedCryptoMarket({}),
        candle_cache={},
    )

    assert result["historical_candles"] == 0
    assert result["degraded"] == 2
    assert {signal["seed_market_quality"] for signal in result["signals"]} == {"degraded"}


class _CountingCryptoMarket:
    calls = 0

    def fetch_historical_snapshot(self, asset: str, at: datetime, interval: str = "1h") -> MarketSnapshot:
        self.calls += 1
        return MarketSnapshot(asset=asset, price=60000)


class _MappedCryptoMarket:
    def __init__(self, snapshots: dict[str, MarketSnapshot]) -> None:
        self.snapshots = snapshots

    def fetch_historical_snapshot(self, asset: str, at: datetime, interval: str = "1h") -> MarketSnapshot | None:
        bucket = at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00")
        return self.snapshots.get(bucket)


class _SeedConnectors:
    def fetch_historical_news(self, connector: str, asset: str, from_dt=None, to_dt=None, limit=500):
        if connector == "alphavantage-news":
            return []
        return [
            NewsEvent(
                id="evt-1",
                asset=asset,
                headline="BTC article one",
                source="EODHD",
                published_at=datetime(2026, 3, 15, 10, 10, tzinfo=timezone.utc),
                sentiment=0.6,
                relevance=0.8,
                novelty=0.5,
            ),
            NewsEvent(
                id="evt-2",
                asset=asset,
                headline="BTC article two",
                source="EODHD",
                published_at=datetime(2026, 3, 15, 11, 10, tzinfo=timezone.utc),
                sentiment=0.6,
                relevance=0.8,
                novelty=0.5,
            ),
        ]

    def fetch_market(self, connector: str, asset: str, exchange: str | None = None) -> MarketSnapshot:
        return MarketSnapshot(asset=asset, price=62000, trend_1h=0.3, change_1h_pct=1.5)


def _score_from_market(event: NewsEvent, market: MarketSnapshot, exchange: Any, asset_info: Any, as_of: Any = None) -> dict[str, Any]:
    return {
        "id": f"sig-{event.id}",
        "event_id": event.id,
        "asset": market.asset,
        "direction": "long",
        "entry_score": round(0.4 + float(market.trend_1h), 4),
        "confidence": round(0.5 + float(market.change_1h_pct) / 10.0, 4),
        "time_window": "1h",
        "reason": "market-sensitive test signal",
        "risk_flags": [],
        "components": {},
        "exchange": "BITVAVO",
        "region": "Crypto",
        "asset_class": "crypto",
        "source_field": "crypto",
    }
