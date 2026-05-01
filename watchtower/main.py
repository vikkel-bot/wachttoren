from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from watchtower.domain import EntrySignal
from watchtower.models import (
    ColonyConfigIn,
    ColonyDispatchIn,
    ColonyReplayIn,
    ColonyTradeOutcome,
    ConnectorFetchIn,
    EntityResolveIn,
    GlobalPipelineRunIn,
    MarketConnectorFetchIn,
    MarketSnapshotIn,
    NewsEventIn,
    NewsRadarScanIn,
    OutcomeIn,
    PipelineRunIn,
    SignalEvaluationIn,
    WatchlistSeedIn,
    WatchlistItemIn,
)
from watchtower.services.assets import ListedAssetUniverse
from watchtower.services.colony import DEFAULT_COLONY_CONFIG, ColonyBridge
from watchtower.services.connectors import ConnectorRegistry, RateLimitError
from watchtower.services.exchanges import ExchangeUniverse, TradingSessionDetector
from watchtower.services.intermarket import IntermarketEngine
from watchtower.services.live_refresh import PeriodicWatchtowerScheduler, WatchtowerLiveRefresher
from watchtower.services.market_context import MarketContextEngine
from watchtower.services.news_radar import NewsRadar
from watchtower.services.news_loader import HistoricalNewsLoader
from watchtower.services.outcomes import OutcomeTracker
from watchtower.services.providers import ProviderRegistry
from watchtower.services.regional_scoring import RegionalEntryScorer
from watchtower.services.resolver import AssetResolver
from watchtower.services.scoring import EntryScorer
from watchtower.services.seed_signals import filter_seed_signals, read_seed_signals, seed_signals_path
from watchtower.storage import SQLiteStore, to_jsonable


app = FastAPI(
    title="Watchtower MVP",
    description="Entry-intelligence service that produces signals, not trades.",
    version="0.1.0",
)

db_path = Path(os.getenv("WATCHTOWER_DB", "watchtower.db"))
store = SQLiteStore(db_path)
resolver = AssetResolver()
scorer = EntryScorer()
outcomes = OutcomeTracker()
connectors = ConnectorRegistry(resolver)
historical_news_loader = HistoricalNewsLoader(connectors)
market_context = MarketContextEngine()
colony_bridge = ColonyBridge()
exchange_universe = ExchangeUniverse()
session_detector = TradingSessionDetector(exchange_universe)
asset_universe = ListedAssetUniverse(exchange_universe)
provider_registry = ProviderRegistry()
regional_scorer = RegionalEntryScorer(scorer, session_detector)
intermarket_engine = IntermarketEngine()
news_radar = NewsRadar(asset_universe, monthly_budget_eur=25.0)
COLONY_CONFIG_KEY = "colony_config"


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


live_refresher = WatchtowerLiveRefresher(
    store=store,
    connectors=connectors,
    asset_universe=asset_universe,
    exchange_universe=exchange_universe,
    score_signal=lambda event, market, exchange, asset_info: _score_signal(event, market, exchange, asset_info),
)
live_scheduler = PeriodicWatchtowerScheduler(
    live_refresher,
    tick_seconds=_env_float("WATCHTOWER_SCHEDULER_TICK_SECONDS", 60.0),
    enabled=_env_flag("WATCHTOWER_SCHEDULER_ENABLED", True),
)


@app.on_event("startup")
def startup() -> None:
    store.init_schema()
    live_scheduler.start(run_immediately=_env_flag("WATCHTOWER_REFRESH_ON_START", False))


@app.on_event("shutdown")
def shutdown() -> None:
    live_scheduler.stop()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "watchtower", "db": str(db_path)}


@app.get("/providers")
def list_providers() -> list[dict]:
    return provider_registry.list()


@app.get("/exchanges")
def list_exchanges(region: str | None = None) -> list[dict]:
    return exchange_universe.list(region=region)


@app.get("/exchanges/regions")
def exchange_regions() -> list[str]:
    return exchange_universe.regions()


@app.get("/assets")
def list_assets(
    exchange: str | None = None,
    region: str | None = None,
    query: str | None = None,
    asset_class: str | None = None,
) -> list[dict]:
    if exchange and not exchange_universe.get(exchange):
        raise HTTPException(status_code=404, detail="exchange not found")
    return asset_universe.list(exchange=exchange, region=region, query=query, asset_class=asset_class)


@app.post("/assets/resolve")
def resolve_asset(payload: EntityResolveIn) -> dict:
    resolved = asset_universe.resolve(payload.text, exchange=payload.exchange)
    if not resolved:
        raise HTTPException(status_code=404, detail="asset not resolved")
    return resolved


@app.get("/exchanges/{code}")
def get_exchange(code: str) -> dict:
    exchange = exchange_universe.get(code)
    if not exchange:
        raise HTTPException(status_code=404, detail="exchange not found")
    return exchange.to_dict()


@app.get("/exchanges/{code}/session")
def exchange_session(code: str, at: str | None = None) -> dict:
    try:
        return session_detector.status(code, _parse_datetime(at))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/exchanges/{code}/watchlist")
def exchange_watchlist(code: str, enabled_only: bool = False) -> list[dict]:
    exchange = exchange_universe.get(code)
    if not exchange:
        raise HTTPException(status_code=404, detail="exchange not found")
    return store.list_watchlist(enabled_only=enabled_only, exchange=exchange.code)


@app.post("/exchanges/{code}/watchlist")
def upsert_exchange_watchlist_item(code: str, payload: WatchlistItemIn) -> dict:
    exchange = exchange_universe.get(code)
    if not exchange:
        raise HTTPException(status_code=404, detail="exchange not found")
    data = payload.model_dump()
    data["exchange"] = exchange.code
    data["asset"] = resolver.resolve(payload.asset)
    return store.upsert_watchlist_item(exchange_universe.enrich_watchlist_item(data))


@app.get("/connectors")
def list_connectors() -> list[dict]:
    return connectors.list_connectors()


@app.post("/connectors/news/fetch")
def fetch_news(payload: ConnectorFetchIn) -> dict:
    try:
        events = connectors.fetch_news(
            connector=payload.connector,
            asset=payload.asset,
            limit=payload.limit,
            feed_url=payload.feed_url,
            exchange=payload.exchange,
            from_dt=payload.from_dt,
            to_dt=payload.to_dt,
        )
    except RateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    event_payloads = []
    for event in events:
        event_payloads.append(store.save_event(event) if payload.ingest else to_jsonable(event))
    return {"connector": payload.connector, "count": len(event_payloads), "events": event_payloads}


@app.get("/news/historical")
def fetch_historical_news(
    asset: str,
    connector: str = "eodhd-news",
    from_dt: str | None = None,
    to_dt: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    ingest: bool = True,
) -> dict:
    try:
        events = connectors.fetch_historical_news(
            connector=connector,
            asset=asset,
            from_dt=_parse_datetime(from_dt),
            to_dt=_parse_datetime(to_dt),
            limit=limit,
        )
    except RateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    event_payloads = [store.save_event(event) if ingest else to_jsonable(event) for event in events]
    return {"connector": connector, "asset": resolver.resolve(asset), "count": len(event_payloads), "events": event_payloads}


@app.get("/news/sentiment")
def fetch_sentiment_news(
    asset: str,
    from_dt: str | None = None,
    to_dt: str | None = None,
    limit: int = Query(default=50, ge=1, le=1000),
    ingest: bool = True,
) -> dict:
    connector = "alphavantage-news"
    try:
        events = connectors.fetch_historical_news(
            connector=connector,
            asset=asset,
            from_dt=_parse_datetime(from_dt),
            to_dt=_parse_datetime(to_dt),
            limit=limit,
        )
    except RateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    event_payloads = [store.save_event(event) if ingest else to_jsonable(event) for event in events]
    avg_sentiment = sum(float(event.sentiment) for event in events) / len(events) if events else 0.0
    return {
        "connector": connector,
        "asset": resolver.resolve(asset),
        "count": len(event_payloads),
        "avg_sentiment": round(avg_sentiment, 4),
        "events": event_payloads,
    }


@app.get("/news-radar/config")
def get_news_radar_config() -> dict:
    return news_radar.config()


@app.get("/news-radar/events")
def list_news_radar_events(limit: int = Query(default=25, ge=1, le=250)) -> list[dict]:
    return store.list_events(limit=limit, tag="news_radar")


@app.post("/news-radar/scan")
def scan_news_radar(payload: NewsRadarScanIn) -> dict:
    try:
        report = news_radar.scan(
            mode=payload.mode,
            queries=payload.queries or None,
            rss_urls=payload.rss_urls or None,
            limit=payload.limit,
            max_per_source=payload.max_per_source,
            timespan=payload.timespan,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    event_payloads = []
    for event in report["events"]:
        event_payloads.append(store.save_event(event) if payload.ingest else to_jsonable(event))
    return {
        **report,
        "events": event_payloads,
        "ingested": payload.ingest,
    }


@app.post("/connectors/market/fetch")
def fetch_market(payload: MarketConnectorFetchIn) -> dict:
    try:
        snapshot = connectors.fetch_market(connector=payload.connector, asset=payload.asset, exchange=payload.exchange)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    snapshot_payload = store.save_market_snapshot(snapshot) if payload.ingest else to_jsonable(snapshot)
    return {"connector": payload.connector, "snapshot": snapshot_payload}


@app.post("/events/news")
def ingest_news_event(payload: NewsEventIn) -> dict:
    asset = resolver.resolve(payload.asset, f"{payload.headline} {payload.summary}")
    event = payload.to_domain(asset)
    return store.save_event(event)


@app.post("/market/snapshots")
def ingest_market_snapshot(payload: MarketSnapshotIn) -> dict:
    asset = resolver.resolve(payload.asset)
    snapshot = payload.to_domain(asset)
    return store.save_market_snapshot(snapshot)


@app.post("/regime/detect")
def detect_regime(payload: MarketSnapshotIn) -> dict:
    asset = resolver.resolve(payload.asset)
    snapshot = payload.to_domain(asset)
    return market_context.regime_report(snapshot)


@app.post("/signals/evaluate")
def evaluate_signal(payload: SignalEvaluationIn) -> dict:
    asset = resolver.resolve(payload.event.asset or payload.market.asset, f"{payload.event.headline} {payload.event.summary}")
    event = payload.event.to_domain(asset)
    market = payload.market.to_domain(asset)
    exchange_code = _resolve_exchange(payload.event.exchange or payload.market.exchange)
    exchange = exchange_universe.get(exchange_code) if exchange_code else None
    asset_info = asset_universe.get(exchange.code, asset).to_dict() if exchange and asset_universe.get(exchange.code, asset) else None

    store.save_event(event)
    store.save_market_snapshot(market)
    signal_payload = _score_signal(event, market, exchange, asset_info)
    return store.save_signal(signal_payload)


@app.get("/signals")
def list_signals(
    limit: int = Query(default=50, ge=1, le=250),
    asset: str | None = None,
    exchange: str | None = None,
    region: str | None = None,
) -> list[dict]:
    resolved_asset = resolver.resolve(asset) if asset else None
    return store.list_signals(limit=limit, asset=resolved_asset, exchange=exchange, region=region)


@app.get("/watchlist")
def list_watchlist(enabled_only: bool = False, exchange: str | None = None, region: str | None = None) -> list[dict]:
    if exchange and exchange.upper() != "GLOBAL" and not exchange_universe.get(exchange):
        raise HTTPException(status_code=404, detail="exchange not found")
    return store.list_watchlist(enabled_only=enabled_only, exchange=exchange, region=region)


@app.post("/watchlist")
def upsert_watchlist_item(payload: WatchlistItemIn) -> dict:
    data = payload.model_dump()
    if data["exchange"].upper() != "GLOBAL" and not exchange_universe.get(data["exchange"]):
        raise HTTPException(status_code=404, detail="exchange not found")
    data["asset"] = resolver.resolve(payload.asset)
    return store.upsert_watchlist_item(exchange_universe.enrich_watchlist_item(data))


@app.post("/watchlist/seed")
def seed_watchlist(payload: WatchlistSeedIn) -> dict:
    if payload.exchange and not exchange_universe.get(payload.exchange):
        raise HTTPException(status_code=404, detail="exchange not found")
    assets = asset_universe.list(exchange=payload.exchange, region=payload.region, asset_class=payload.asset_class)[: payload.max_assets]
    created = []
    for listed_asset in assets:
        data = {
            "exchange": listed_asset["exchange"],
            "asset": listed_asset["symbol"],
            "region": exchange_universe.require(listed_asset["exchange"]).region,
            "currency": listed_asset["currency"],
            "asset_class": listed_asset["asset_class"],
            "enabled": payload.enabled,
            "min_entry_score": payload.min_entry_score,
            "min_confidence": payload.min_confidence,
            "max_signals_per_hour": 5,
            "notes": "seeded from listed asset universe",
        }
        created.append(store.upsert_watchlist_item(exchange_universe.enrich_watchlist_item(data)))
    return {"count": len(created), "items": created}


@app.get("/watchlist/{asset}")
def get_watchlist_item(asset: str, exchange: str | None = None) -> dict:
    resolved_asset = resolver.resolve(asset)
    item = store.get_watchlist_item(resolved_asset, exchange=exchange)
    if not item:
        raise HTTPException(status_code=404, detail="watchlist item not found")
    return item


@app.delete("/watchlist/{asset}")
def delete_watchlist_item(asset: str, exchange: str | None = None) -> dict:
    resolved_asset = resolver.resolve(asset)
    deleted = store.delete_watchlist_item(resolved_asset, exchange=exchange)
    if not deleted:
        raise HTTPException(status_code=404, detail="watchlist item not found")
    return {"deleted": True, "asset": resolved_asset, "exchange": exchange}


@app.get("/signals/{signal_id}")
def get_signal(signal_id: str) -> dict:
    signal = store.get_signal(signal_id)
    if not signal:
        raise HTTPException(status_code=404, detail="signal not found")
    return signal


@app.post("/outcomes/evaluate")
def evaluate_outcome(payload: OutcomeIn) -> dict:
    signal_payload = store.get_signal(payload.signal_id)
    if not signal_payload:
        raise HTTPException(status_code=404, detail="signal not found")

    signal = EntrySignal(
        id=signal_payload["id"],
        event_id=signal_payload["event_id"],
        asset=signal_payload["asset"],
        direction=signal_payload["direction"],
        entry_score=signal_payload["entry_score"],
        confidence=signal_payload["confidence"],
        time_window=signal_payload["time_window"],
        reason=signal_payload["reason"],
        risk_flags=signal_payload["risk_flags"],
        components=signal_payload["components"],
    )
    outcome = outcomes.evaluate(
        signal=signal,
        entry_price=payload.entry_price,
        future_price=payload.future_price,
        window=payload.window,
        hit_threshold_pct=payload.hit_threshold_pct,
    )
    return store.save_outcome(outcome)


@app.get("/learning/summary")
def learning_summary(limit: int = Query(default=1000, ge=1, le=5000)) -> dict:
    return store.learning_summary(limit=limit)


@app.get("/learning/recommendations")
def learning_recommendations(limit: int = Query(default=1000, ge=1, le=5000)) -> dict:
    summary = store.learning_summary(limit=limit)
    recommendations = []
    if summary["total_outcomes"] < 10:
        recommendations.append("Collect at least 10 labelled outcomes before trusting source or asset rankings.")
    best_assets = sorted(
        summary["by_asset"].items(),
        key=lambda item: (item[1]["hit_rate"], item[1]["avg_return_pct"], item[1]["count"]),
        reverse=True,
    )
    if best_assets:
        asset, metrics = best_assets[0]
        recommendations.append(
            f"Best current asset bucket is {asset} with hit_rate={metrics['hit_rate']} and avg_return_pct={metrics['avg_return_pct']}."
        )
    return {"summary": summary, "recommendations": recommendations}


@app.post("/pipeline/run")
def run_pipeline(payload: PipelineRunIn) -> dict:
    if payload.exchange and not exchange_universe.get(payload.exchange):
        raise HTTPException(status_code=404, detail="exchange not found")

    watchlist = store.list_watchlist(enabled_only=True, exchange=payload.exchange, region=payload.region)[: payload.max_assets]
    signals = []
    errors = []
    evaluated_assets = 0
    for item in watchlist:
        exchange = exchange_universe.get(item["exchange"])
        if not exchange:
            continue
        evaluated_assets += 1
        asset_info_obj = asset_universe.get(exchange.code, item["asset"])
        asset_info = asset_info_obj.to_dict() if asset_info_obj else None
        try:
            events = connectors.fetch_news(
                connector=payload.news_connector,
                asset=item["asset"],
                exchange=exchange.code,
                limit=payload.max_events_per_asset,
            )
            market = connectors.fetch_market(
                connector=payload.market_connector,
                asset=item["asset"],
                exchange=exchange.code,
            )
        except ValueError as exc:
            errors.append({"exchange": exchange.code, "asset": item["asset"], "detail": str(exc)})
            continue

        if payload.persist:
            store.save_market_snapshot(market)
        for event in events:
            if payload.persist:
                store.save_event(event)
            signal = _score_signal(event, market, exchange, asset_info)
            if payload.persist:
                signal = store.save_signal(signal)
            signals.append(signal)

    return {
        "evaluated_assets": evaluated_assets,
        "signals_created": len(signals),
        "failed_assets": len(errors),
        "errors": errors,
        "signals": signals,
    }


@app.post("/pipeline/run-global")
def run_global_pipeline(payload: GlobalPipelineRunIn) -> dict:
    exchange_filter = {code.upper() for code in payload.exchanges or []}
    region_filter = {region.lower() for region in payload.regions or []}
    class_filter = {asset_class.lower() for asset_class in payload.asset_classes or []}
    for exchange_code in exchange_filter:
        if not exchange_universe.get(exchange_code):
            raise HTTPException(status_code=404, detail=f"exchange not found: {exchange_code}")

    grouped_assets: dict[str, list[dict]] = {}
    for listed_asset in asset_universe.list():
        exchange = exchange_universe.get(listed_asset["exchange"])
        if not exchange:
            continue
        if exchange_filter and exchange.code not in exchange_filter:
            continue
        if region_filter and exchange.region.lower() not in region_filter:
            continue
        if class_filter and listed_asset.get("asset_class", "").lower() not in class_filter:
            continue
        grouped_assets.setdefault(exchange.code, []).append(listed_asset)

    signals = []
    errors = []
    evaluated_assets = 0
    for exchange_code, listed_assets in sorted(grouped_assets.items()):
        exchange = exchange_universe.require(exchange_code)
        for listed_asset in listed_assets[: payload.max_assets_per_exchange]:
            evaluated_assets += 1
            if payload.seed_watchlist:
                watch_item = {
                    "exchange": exchange.code,
                    "asset": listed_asset["symbol"],
                    "region": exchange.region,
                    "currency": listed_asset["currency"],
                    "asset_class": listed_asset["asset_class"],
                    "enabled": True,
                    "min_entry_score": 0.7,
                    "min_confidence": 0.55,
                    "max_signals_per_hour": 5,
                    "notes": "seeded by global pipeline",
                }
                store.upsert_watchlist_item(exchange_universe.enrich_watchlist_item(watch_item))
            try:
                events = connectors.fetch_news(
                    connector=payload.news_connector,
                    asset=listed_asset["symbol"],
                    exchange=exchange.code,
                    limit=payload.max_events_per_asset,
                )
                market = connectors.fetch_market(
                    connector=payload.market_connector,
                    asset=listed_asset["symbol"],
                    exchange=exchange.code,
                )
            except ValueError as exc:
                errors.append({"exchange": exchange.code, "asset": listed_asset["symbol"], "detail": str(exc)})
                continue

            if payload.persist:
                store.save_market_snapshot(market)
            for event in events:
                if payload.persist:
                    store.save_event(event)
                signal = _score_signal(event, market, exchange, listed_asset)
                if payload.persist:
                    signal = store.save_signal(signal)
                signals.append(signal)

    return {
        "evaluated_assets": evaluated_assets,
        "signals_created": len(signals),
        "failed_assets": len(errors),
        "errors": errors,
        "signals": signals,
    }


@app.get("/colony/config")
def get_colony_config() -> dict:
    return {**DEFAULT_COLONY_CONFIG, **store.get_setting(COLONY_CONFIG_KEY)}


@app.put("/colony/config")
def update_colony_config(payload: ColonyConfigIn) -> dict:
    return store.save_setting(COLONY_CONFIG_KEY, payload.model_dump())


@app.get("/colony/signals")
def colony_signals(limit: int = Query(default=50, ge=1, le=250)) -> dict:
    config = get_colony_config()
    signals = store.list_signals(limit=limit)
    watchlist = store.list_watchlist(enabled_only=True)
    qualified = colony_bridge.qualified_signals(signals, watchlist, config)
    return colony_bridge.build_packet(qualified)


@app.get("/backtest/signals")
def backtest_signals(
    limit: int = Query(default=1000, ge=1, le=5000),
    asset: str | None = None,
    asset_class: str | None = None,
    exchange: str | None = None,
    region: str | None = None,
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
) -> dict:
    resolved_asset = resolver.resolve(asset) if asset else None
    signals = store.export_signals(
        limit=limit,
        asset=resolved_asset,
        asset_class=asset_class,
        exchange=exchange,
        region=region,
        from_ts=from_ts,
        to_ts=to_ts,
    )
    normalized = [
        _backtest_signal_payload(signal)
        for signal in signals
        if signal.get("direction") != "neutral"
    ]
    return {
        "source": "watchtower",
        "export_type": "signals",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(normalized),
        "filters": {
            "asset": resolved_asset,
            "asset_class": asset_class,
            "exchange": exchange,
            "region": region,
            "from": from_ts,
            "to": to_ts,
        },
        "signals": normalized,
    }


@app.get("/backtest/signals/seed")
def list_seed_signals(
    asset: str | None = None,
    from_dt: str | None = None,
    to_dt: str | None = None,
    min_entry_score: float = Query(default=0.0, ge=0.0, le=1.0),
    limit: int = Query(default=250, ge=1, le=1000),
) -> dict:
    signals = filter_seed_signals(
        read_seed_signals(),
        asset=resolver.resolve(asset) if asset else None,
        from_dt=from_dt,
        to_dt=to_dt,
        min_entry_score=min_entry_score,
        limit=limit,
    )
    return {
        "source": "watchtower",
        "export_type": "seed_signals",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(signals),
        "filters": {
            "asset": resolver.resolve(asset) if asset else None,
            "from_dt": from_dt,
            "to_dt": to_dt,
            "min_entry_score": min_entry_score,
        },
        "signals": signals,
    }


@app.post("/colony/dispatch")
def colony_dispatch(payload: ColonyDispatchIn) -> dict:
    config = get_colony_config()
    dry_run = config["dry_run"] if payload.dry_run is None else payload.dry_run
    packet = colony_signals(limit=payload.limit)
    try:
        return colony_bridge.dispatch(config.get("webhook_url"), packet, dry_run=dry_run)
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"webhook dispatch failed: {exc}") from exc


@app.post("/colony/feedback")
def colony_feedback(payload: ColonyTradeOutcome) -> dict:
    record = payload.model_dump()
    saved = store.save_colony_feedback(record)

    signal_linked = False
    if payload.signal_id and payload.feedback_type == "TRADE_OUTCOME":
        signal_payload = store.get_signal(payload.signal_id)
        if signal_payload and payload.entry_price and payload.exit_price:
            signal = EntrySignal(
                id=signal_payload["id"],
                event_id=signal_payload["event_id"],
                asset=signal_payload["asset"],
                direction=signal_payload["direction"],
                entry_score=signal_payload["entry_score"],
                confidence=signal_payload["confidence"],
                time_window=signal_payload["time_window"],
                reason=signal_payload["reason"],
                risk_flags=signal_payload["risk_flags"],
                components=signal_payload["components"],
            )
            outcome = outcomes.evaluate(
                signal=signal,
                entry_price=payload.entry_price,
                future_price=payload.exit_price,
                window="colony_trade",
                hit_threshold_pct=0.01,
            )
            store.save_outcome(outcome)
            signal_linked = True

    return {"accepted": True, "feedback_id": saved["id"], "signal_linked": signal_linked}


@app.post("/colony/feedback/replay")
def colony_feedback_replay(payload: ColonyReplayIn) -> dict:
    known = store.get_replay_ids()
    if payload.replay_id in known:
        return {
            "replay_id": payload.replay_id,
            "accepted": 0,
            "skipped_duplicate": len(payload.trades),
            "assets_affected": [],
        }

    accepted = 0
    assets_affected: set[str] = set()
    for trade in payload.trades:
        record = trade.model_dump()
        record["is_replay"] = True
        record["replay_id"] = payload.replay_id
        store.save_colony_feedback(record)
        accepted += 1
        assets_affected.add(trade.asset)

    return {
        "replay_id": payload.replay_id,
        "accepted": accepted,
        "skipped_duplicate": 0,
        "assets_affected": sorted(assets_affected),
    }


@app.get("/colony/feedback/stats")
def colony_feedback_stats() -> dict:
    return store.colony_feedback_stats()


@app.get("/dashboard/summary")
def dashboard_summary() -> dict:
    watched_exchanges = sorted({item.get("exchange", "GLOBAL") for item in store.list_watchlist(enabled_only=True)})
    best_equity_entries = _build_best_entries("equity")
    best_commodity_entries = _build_best_entries("commodity")
    best_crypto_entries = _build_best_entries("crypto")
    global_mood = _global_market_mood(best_equity_entries + best_commodity_entries + best_crypto_entries)
    return {
        **store.dashboard_summary(),
        "colony_config": get_colony_config(),
        "connectors": connectors.list_connectors(),
        "exchanges": exchange_universe.list(),
        "asset_class_counts": _asset_class_counts(),
        "best_equity_entries": best_equity_entries,
        "best_commodity_entries": best_commodity_entries,
        "best_crypto_entries": best_crypto_entries,
        "global_mood": global_mood,
        "news_radar": news_radar.dashboard_summary(store.list_events(limit=50)),
        "intermarket_links": intermarket_engine.dashboard_links(),
        "watched_exchanges": watched_exchanges,
        "market_sessions": [
            session_detector.status(exchange["code"])
            for exchange in exchange_universe.list()
        ],
    }


@app.get("/dashboard/intelligence")
def dashboard_intelligence() -> dict:
    return _dashboard_intelligence()


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    summary = dashboard_summary()
    intelligence = dashboard_intelligence()
    positive_signal_rows = "".join(
        _positive_signal_item(signal) for signal in intelligence["positive_signals"]
    ) or "<p class='empty'>Geen actieve signalen</p>"
    recommended_asset_rows = "".join(
        _recommended_asset_item(asset) for asset in intelligence["recommended_assets"]
    ) or "<p class='empty'>Geen aanbevolen assets</p>"
    backtest_status_html = _backtest_status_card(intelligence["backtest_status"])
    latest_rows = "".join(
        _signal_row(signal) for signal in summary["latest_signals"]
    ) or "<tr><td colspan='9'>No signals yet</td></tr>"
    session_rows = "".join(
        _session_row(session) for session in summary["market_sessions"]
    ) or "<tr><td colspan='5'>No watched exchange sessions yet</td></tr>"
    coverage_rows = "".join(
        _coverage_row(region, exchanges)
        for region, exchanges in _group_exchanges_by_region(summary["exchanges"]).items()
    )
    asset_rows = "".join(
        _asset_region_row(region, assets)
        for region, assets in _group_assets_by_region(asset_universe.list()).items()
    )
    equity_entry_rows = "".join(
        _best_entry_row(entry) for entry in summary["best_equity_entries"]
    ) or "<tr><td colspan='8'>No equity entries available</td></tr>"
    commodity_entry_rows = "".join(
        _best_entry_row(entry) for entry in summary["best_commodity_entries"]
    ) or "<tr><td colspan='8'>No commodity entries available</td></tr>"
    radar_rows = "".join(
        _news_radar_row(event) for event in summary["news_radar"]["recent_events"]
    ) or "<tr><td colspan='8'>No radar events yet</td></tr>"
    intermarket_rows = "".join(
        _intermarket_context_row(link) for link in intelligence["intermarket_context"]
    )
    learning = summary["learning"]
    mood = summary["global_mood"]
    mood_state = escape(str(mood["state"]))
    radar = summary["news_radar"]
    radar_budget = radar["budget"]
    html = f"""
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Watchtower</title>
      <style>
        :root {{
          color-scheme: light dark;
          --bg: #f7f8fa;
          --panel: #ffffff;
          --ink: #17202a;
          --muted: #5c6b7a;
          --line: #d8dee6;
          --accent: #16697a;
          --good: #147d4f;
          --warn: #a05a00;
          --mood-bg: #fff7e6;
        }}
        body.mood-up {{
          --bg: #eef8f0;
          --panel: #ffffff;
          --mood-bg: #dff3e5;
        }}
        body.mood-sideways {{
          --bg: #fff8ec;
          --panel: #ffffff;
          --mood-bg: #ffe8bd;
        }}
        body.mood-down {{
          --bg: #fff1f1;
          --panel: #ffffff;
          --mood-bg: #ffdada;
        }}
        body {{
          margin: 0;
          font-family: Arial, sans-serif;
          background: var(--bg);
          color: var(--ink);
        }}
        main {{
          max-width: 1180px;
          margin: 0 auto;
          padding: 28px 18px 48px;
        }}
        header {{
          display: flex;
          align-items: baseline;
          justify-content: space-between;
          gap: 16px;
          margin-bottom: 20px;
        }}
        h1, h2 {{
          margin: 0;
          letter-spacing: 0;
        }}
        h1 {{
          font-size: 28px;
        }}
        h2 {{
          font-size: 18px;
          margin-bottom: 10px;
        }}
        .grid {{
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
          gap: 12px;
          margin-bottom: 18px;
        }}
        .intelligence {{
          padding: 0;
          overflow: hidden;
          border-color: #c9d5df;
        }}
        .intel-title {{
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          padding: 16px 16px 12px;
          border-bottom: 1px solid var(--line);
        }}
        .intel-title h2 {{
          margin-bottom: 0;
        }}
        .intel-columns {{
          display: grid;
          grid-template-columns: repeat(3, minmax(0, 1fr));
        }}
        .intel-col {{
          min-height: 230px;
          padding: 14px 16px 16px;
          border-right: 1px solid var(--line);
        }}
        .intel-col:last-child {{
          border-right: 0;
        }}
        .intel-col.positive {{
          background: #eef8f1;
        }}
        .intel-col.recommended {{
          background: #eef6fb;
        }}
        .intel-col.status {{
          background: #f4f6f8;
        }}
        .intel-heading {{
          font-size: 12px;
          font-weight: 700;
          text-transform: uppercase;
          margin-bottom: 10px;
        }}
        .intel-item {{
          border-top: 1px solid color-mix(in srgb, var(--line) 75%, transparent);
          padding: 10px 0;
        }}
        .intel-item:first-of-type {{
          border-top: 0;
          padding-top: 0;
        }}
        .intel-main {{
          display: flex;
          align-items: baseline;
          justify-content: space-between;
          gap: 10px;
          font-weight: 700;
        }}
        .intel-meta {{
          margin-top: 5px;
          color: var(--muted);
          font-size: 12px;
          line-height: 1.35;
        }}
        .intel-reason {{
          margin-top: 6px;
          font-size: 13px;
          line-height: 1.35;
        }}
        .status-grid {{
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: 10px;
        }}
        .status-cell {{
          border-top: 1px solid var(--line);
          padding-top: 8px;
        }}
        .status-number {{
          display: block;
          font-size: 20px;
          font-weight: 700;
        }}
        .empty {{
          color: var(--muted);
          margin: 8px 0 0;
        }}
        .metric, section {{
          background: var(--panel);
          border: 1px solid var(--line);
          border-radius: 8px;
        }}
        .metric {{
          padding: 14px;
          min-height: 78px;
        }}
        .metric.mood {{
          background: var(--mood-bg);
        }}
        .label {{
          color: var(--muted);
          font-size: 12px;
          text-transform: uppercase;
        }}
        .value {{
          font-size: 24px;
          font-weight: 700;
          margin-top: 8px;
        }}
        section {{
          padding: 16px;
          overflow-x: auto;
          margin-bottom: 18px;
        }}
        table {{
          width: 100%;
          border-collapse: collapse;
          min-width: 720px;
        }}
        th, td {{
          border-bottom: 1px solid var(--line);
          padding: 10px 8px;
          text-align: left;
          font-size: 14px;
        }}
        th {{
          color: var(--muted);
          font-size: 12px;
          text-transform: uppercase;
        }}
        .long {{
          color: var(--good);
          font-weight: 700;
        }}
        .short {{
          color: var(--warn);
          font-weight: 700;
        }}
        .high {{
          color: #a42828;
          font-weight: 700;
        }}
        .medium {{
          color: #8a5a00;
          font-weight: 700;
        }}
        .low {{
          color: var(--muted);
          font-weight: 700;
        }}
        .chips {{
          display: flex;
          flex-wrap: wrap;
          gap: 6px;
        }}
        .chip {{
          display: inline-flex;
          align-items: center;
          min-height: 24px;
          border: 1px solid var(--line);
          border-radius: 6px;
          padding: 2px 7px;
          font-size: 12px;
          background: color-mix(in srgb, var(--panel) 88%, var(--accent) 12%);
        }}
        .entry-table th:nth-child(3),
        .entry-table td:nth-child(3) {{
          min-width: 180px;
        }}
        @media (max-width: 780px) {{
          header {{
            display: block;
          }}
          .grid {{
            grid-template-columns: repeat(2, minmax(0, 1fr));
          }}
          .intel-columns {{
            grid-template-columns: 1fr;
          }}
          .intel-col {{
            border-right: 0;
            border-bottom: 1px solid var(--line);
          }}
        }}
      </style>
    </head>
    <body class="mood-{mood_state}">
      <main>
        <header>
          <h1>Watchtower</h1>
          <span>{escape(str(db_path))}</span>
        </header>
        <section class="intelligence">
          <div class="intel-title">
            <h2>Signal Intelligence Panel</h2>
            <span class="label">Wat test positief en waarom</span>
          </div>
          <div class="intel-columns">
            <div class="intel-col positive">
              <div class="intel-heading">Positieve Signalen</div>
              {positive_signal_rows}
            </div>
            <div class="intel-col recommended">
              <div class="intel-heading">Aanbevolen Assets</div>
              {recommended_asset_rows}
            </div>
            <div class="intel-col status">
              <div class="intel-heading">Backtest Status</div>
              {backtest_status_html}
            </div>
          </div>
        </section>
        <div class="grid">
          <div class="metric"><div class="label">Recent Signals</div><div class="value">{summary["signal_count"]}</div></div>
          <div class="metric mood"><div class="label">Global Mood</div><div class="value">{escape(str(mood["label"]))}</div></div>
          <div class="metric"><div class="label">Watchlist</div><div class="value">{summary["active_watchlist_count"]}</div></div>
          <div class="metric"><div class="label">Watched Markets</div><div class="value">{len(summary["watched_exchanges"])}</div></div>
          <div class="metric"><div class="label">Exchange Coverage</div><div class="value">{len(summary["exchanges"])}</div></div>
          <div class="metric"><div class="label">Equities</div><div class="value">{summary["asset_class_counts"].get("equity", 0)}</div></div>
          <div class="metric"><div class="label">Commodities</div><div class="value">{summary["asset_class_counts"].get("commodity", 0)}</div></div>
          <div class="metric"><div class="label">News Budget</div><div class="value">EUR {radar_budget["monthly_budget_eur"]}</div></div>
          <div class="metric"><div class="label">News Cost</div><div class="value">EUR {radar_budget["estimated_monthly_cost_eur"]}</div></div>
          <div class="metric"><div class="label">Outcome Hit Rate</div><div class="value">{learning["hit_rate"]}</div></div>
          <div class="metric"><div class="label">Avg Return %</div><div class="value">{learning["avg_return_pct"]}</div></div>
        </div>
        <section>
          <h2>News Radar</h2>
          <table>
            <thead>
              <tr><th>Impact</th><th>Source</th><th>Asset</th><th>Sentiment</th><th>Relevance</th><th>Themes</th><th>Published</th><th>Headline</th></tr>
            </thead>
            <tbody>{radar_rows}</tbody>
          </table>
        </section>
        <section>
          <h2>Best Equity Entries</h2>
          <table class="entry-table">
            <thead>
              <tr><th>Market</th><th>Symbol</th><th>Name</th><th>Direction</th><th>Score</th><th>Confidence</th><th>Window</th><th>Flags</th></tr>
            </thead>
            <tbody>{equity_entry_rows}</tbody>
          </table>
        </section>
        <section>
          <h2>Best Commodity Entries</h2>
          <table class="entry-table">
            <thead>
              <tr><th>Market</th><th>Symbol</th><th>Name</th><th>Direction</th><th>Score</th><th>Confidence</th><th>Window</th><th>Flags</th></tr>
            </thead>
            <tbody>{commodity_entry_rows}</tbody>
          </table>
        </section>
        <section>
          <h2>Exchange Coverage</h2>
          <table>
            <thead>
              <tr><th>Region</th><th>Count</th><th>Markets</th></tr>
            </thead>
            <tbody>{coverage_rows}</tbody>
          </table>
        </section>
        <section>
          <h2>Asset Universe</h2>
          <table>
            <thead>
              <tr><th>Region</th><th>Assets</th><th>Examples</th></tr>
            </thead>
            <tbody>{asset_rows}</tbody>
          </table>
        </section>
        <section>
          <h2>Latest Signals</h2>
          <table>
            <thead>
              <tr><th>Exchange</th><th>Asset</th><th>Class</th><th>Direction</th><th>Score</th><th>Confidence</th><th>Drivers</th><th>Links</th><th>Reason</th></tr>
            </thead>
            <tbody>{latest_rows}</tbody>
          </table>
        </section>
        <section>
          <h2>Intermarket Links</h2>
          <table>
            <thead>
              <tr><th>Driver</th><th>Richting</th><th>Sterkte</th><th>Uitleg</th></tr>
            </thead>
            <tbody>{intermarket_rows}</tbody>
          </table>
        </section>
        <section>
          <h2>Market Sessions</h2>
          <table>
            <thead>
              <tr><th>Exchange</th><th>Region</th><th>Session</th><th>Trading</th><th>Next Open</th></tr>
            </thead>
            <tbody>{session_rows}</tbody>
          </table>
        </section>
      </main>
    </body>
    </html>
    """
    return HTMLResponse(html)


@app.get("/mock/signal")
def mock_signal() -> dict:
    event = NewsEventIn(
        asset="AAPL",
        headline="Apple beats earnings expectations and raises guidance",
        summary="Revenue and margins came in above analyst estimates.",
        source="example-news",
        sentiment=0.82,
        novelty=0.76,
        relevance=0.9,
        tags=["earnings", "guidance"],
    ).to_domain("AAPL")
    market = MarketSnapshotIn(
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
    ).to_domain("AAPL")
    signal = scorer.score(event, market)
    return to_jsonable(signal)


def _dashboard_intelligence() -> dict:
    recent_signals = store.list_signals(limit=250)
    latest_24h = _signals_since(recent_signals, timedelta(hours=24))[:50]
    feedback_summary = store.colony_feedback_stats()
    return {
        "positive_signals": _positive_signals(recent_signals),
        "recommended_assets": _recommended_assets(latest_24h),
        "backtest_status": _backtest_status(recent_signals, feedback_summary),
        "colony_feedback_summary": feedback_summary,
        "intermarket_context": _dashboard_intermarket_context(intermarket_engine.dashboard_links()),
    }


def _signals_since(signals: list[dict], window: timedelta) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - window
    recent = []
    for signal in signals:
        timestamp = _dashboard_ts(signal)
        if timestamp is not None and timestamp >= cutoff:
            recent.append(signal)
    return recent


def _positive_signals(signals: list[dict]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    positive = []
    for signal in signals:
        direction = str(signal.get("direction") or "").lower()
        if direction not in {"long", "short"}:
            continue
        timestamp = _dashboard_ts(signal)
        if timestamp and timestamp < cutoff:
            continue
        positive.append(_positive_signal_payload(signal, timestamp))

    positive.sort(key=lambda item: (float(item["entry_score"]), float(item["confidence"])), reverse=True)
    return positive[:10]


def _positive_signal_payload(signal: dict, timestamp: datetime | None) -> dict:
    reason = str(signal.get("reason") or "")
    direction = str(signal.get("direction") or "").lower()
    return {
        "signal_id": signal.get("id") or signal.get("signal_id"),
        "asset": str(signal.get("asset") or signal.get("symbol") or "").upper(),
        "direction": direction,
        "entry_score": round(_safe_float(signal.get("entry_score")), 4),
        "confidence": round(_safe_float(signal.get("confidence")), 4),
        "time_window": signal.get("time_window"),
        "reason": reason,
        "reason_preview": reason[:80],
        "timestamp": timestamp.isoformat() if timestamp else signal.get("created_at") or signal.get("timestamp"),
    }


def _recommended_assets(signals: list[dict]) -> list[dict]:
    buckets: dict[str, dict[str, Any]] = {}
    for signal in signals:
        asset = str(signal.get("asset") or signal.get("symbol") or "").upper()
        if not asset:
            continue
        bucket = buckets.setdefault(
            asset,
            {
                "asset": asset,
                "name": _asset_display_name(signal),
                "exchange": signal.get("exchange"),
                "long_signals": 0,
                "short_signals": 0,
                "signal_count": 0,
                "score_sum": 0.0,
                "confidence_sum": 0.0,
                "latest_signal": None,
                "latest_reason": "",
            },
        )
        direction = str(signal.get("direction") or "").lower()
        if direction == "long":
            bucket["long_signals"] += 1
        elif direction == "short":
            bucket["short_signals"] += 1
        bucket["signal_count"] += 1
        bucket["score_sum"] += _safe_float(signal.get("entry_score"))
        bucket["confidence_sum"] += _safe_float(signal.get("confidence"))
        timestamp = _dashboard_ts(signal)
        if timestamp and (bucket["latest_signal"] is None or timestamp > bucket["latest_signal"]):
            bucket["latest_signal"] = timestamp
            bucket["latest_reason"] = str(signal.get("reason") or "")

    recommendations = []
    for bucket in buckets.values():
        count = int(bucket["signal_count"])
        if count <= 0:
            continue
        avg_score = bucket["score_sum"] / count
        if avg_score < 0.5:
            continue
        long_count = int(bucket["long_signals"])
        short_count = int(bucket["short_signals"])
        if long_count > short_count:
            bias = "LONG"
        elif short_count > long_count:
            bias = "SHORT"
        else:
            bias = "MIXED"
        recommendations.append(
            {
                "asset": bucket["asset"],
                "name": bucket["name"],
                "exchange": bucket["exchange"],
                "bias": bias,
                "long_signals": long_count,
                "short_signals": short_count,
                "signal_count": count,
                "avg_entry_score": round(avg_score, 4),
                "avg_confidence": round(bucket["confidence_sum"] / count, 4),
                "latest_signal": bucket["latest_signal"].isoformat() if bucket["latest_signal"] else None,
                "reason_preview": str(bucket.get("latest_reason") or "")[:100],
            }
        )

    recommendations.sort(key=lambda item: (item["avg_entry_score"], item["avg_confidence"]), reverse=True)
    return recommendations


def _backtest_status(recent_signals: list[dict], feedback_summary: dict) -> dict:
    seed_signals = read_seed_signals()
    seed_path = seed_signals_path()
    latest_seed_run = None
    if seed_path.exists():
        latest_seed_run = datetime.fromtimestamp(seed_path.stat().st_mtime, timezone.utc).isoformat()
    seven_day_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    live_signals_7d = sum(
        1
        for signal in recent_signals
        if (timestamp := _dashboard_ts(signal)) is not None and timestamp >= seven_day_cutoff
    )
    trades = int(feedback_summary.get("live_trades", 0)) + int(feedback_summary.get("replay_trades", 0))
    skips = int(feedback_summary.get("signal_skips", 0))
    return {
        "seed_signals_available": len(seed_signals),
        "latest_seed_run": latest_seed_run,
        "live_signals_7d": live_signals_7d,
        "calibration_basis": feedback_summary.get("calibration_basis", "BOOTSTRAPPING"),
        "colony_feedback_received": {
            "trades": trades,
            "skips": skips,
        },
    }


def _dashboard_intermarket_context(links: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for link in links:
        theme = str(link.get("theme") or "")
        effect = str(link.get("effect") or "")
        linked = [str(item) for item in link.get("linked", [])]
        for driver in link.get("drivers", []):
            driver_text = str(driver)
            rows.append(
                {
                    "theme": theme,
                    "driver": driver_text,
                    "direction": _intermarket_direction(driver_text, effect),
                    "strength": _intermarket_strength(driver_text, theme),
                    "explanation": _intermarket_explanation(driver_text, effect),
                    "linked": linked,
                }
            )
    return rows


def _intermarket_direction(driver: str, effect: str) -> str:
    text = f"{driver} {effect}".lower()
    if "dxy" in text or "usd" in text:
        return "negatieve druk bij USD-sterkte"
    if "gold" in text or "risk-off" in text or "real rates" in text:
        return "risk-off filter"
    if "qqq" in text or "risk appetite" in text or "btc vs qqq" in text:
        return "risk-on bevestiging"
    if "eth/btc" in text:
        return "crypto rotatie"
    if any(item in text for item in ["copper", "silver", "aluminium", "semiconductors"]):
        return "cyclische bevestiging"
    if any(item in text for item in ["wti", "brent", "natgas"]):
        return "energie en inflatie"
    return "context"


def _intermarket_strength(driver: str, theme: str) -> str:
    text = f"{driver} {theme}".lower()
    if any(item in text for item in ["btc", "dxy", "qqq", "semiconductors", "ai"]):
        return "hoog"
    if any(item in text for item in ["copper", "gold", "wti", "brent"]):
        return "medium"
    return "laag"


def _intermarket_explanation(driver: str, effect: str) -> str:
    if "BTC vs DXY" in driver:
        return "BTC vs DXY: negatief gecorreleerd; stijgende DXY zet vaak druk op BTC."
    if "BTC vs QQQ" in driver:
        return "BTC vs QQQ: gedeelde groei- en risk-on beta; bevestigt of remt crypto entries."
    if "ETH/BTC" in driver:
        return "ETH/BTC: meet rotatie binnen crypto; helpt brede beta van ETH-specifieke kracht scheiden."
    return effect


def _asset_display_name(signal: dict) -> str:
    asset = str(signal.get("asset") or signal.get("symbol") or "").upper()
    exchange = str(signal.get("exchange") or "").upper()
    if exchange:
        try:
            listed = asset_universe.get(exchange, asset)
        except Exception:
            listed = None
        if listed:
            return str(listed.to_dict().get("name") or asset)
    return asset


def _dashboard_ts(signal: dict) -> datetime | None:
    return _parse_dashboard_ts(signal.get("created_at") or signal.get("timestamp"))


def _parse_dashboard_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _positive_signal_item(signal: dict) -> str:
    direction = escape(str(signal.get("direction", "")))
    score = f"{float(signal.get('entry_score') or 0.0):.2f}"
    confidence = f"{float(signal.get('confidence') or 0.0):.2f}"
    return (
        "<div class='intel-item'>"
        "<div class='intel-main'>"
        f"<span>{escape(str(signal.get('asset', '')))}</span>"
        f"<span class='{direction}'>{direction.upper()} {score}</span>"
        "</div>"
        f"<div class='intel-meta'>conf {confidence} · {escape(str(signal.get('time_window') or 'n/a'))}</div>"
        f"<div class='intel-reason'>{escape(str(signal.get('reason_preview') or ''))}</div>"
        "</div>"
    )


def _recommended_asset_item(asset: dict) -> str:
    latest = asset.get("latest_signal") or "n/a"
    return (
        "<div class='intel-item'>"
        "<div class='intel-main'>"
        f"<span>{escape(str(asset.get('name') or asset.get('asset') or ''))}</span>"
        f"<span>{escape(str(asset.get('bias', 'MIXED')))}</span>"
        "</div>"
        "<div class='intel-meta'>"
        f"score {float(asset.get('avg_entry_score') or 0.0):.2f} · "
        f"conf {float(asset.get('avg_confidence') or 0.0):.2f} · "
        f"{int(asset.get('signal_count') or 0)} signalen"
        "</div>"
        f"<div class='intel-reason'>Laatste: {escape(str(latest))}</div>"
        f"<div class='intel-reason'>Waarom: {escape(str(asset.get('reason_preview') or 'score en bias boven drempel'))}</div>"
        "</div>"
    )


def _backtest_status_card(status: dict) -> str:
    feedback = status.get("colony_feedback_received") or {}
    return (
        "<div class='status-grid'>"
        f"<div class='status-cell'><span class='label'>Seed signalen</span><span class='status-number'>{int(status.get('seed_signals_available') or 0)}</span></div>"
        f"<div class='status-cell'><span class='label'>Live 7 dagen</span><span class='status-number'>{int(status.get('live_signals_7d') or 0)}</span></div>"
        f"<div class='status-cell'><span class='label'>Trades</span><span class='status-number'>{int(feedback.get('trades') or 0)}</span></div>"
        f"<div class='status-cell'><span class='label'>Skips</span><span class='status-number'>{int(feedback.get('skips') or 0)}</span></div>"
        "</div>"
        f"<div class='intel-item'><div class='intel-meta'>Calibration basis</div><div class='intel-main'>{escape(str(status.get('calibration_basis') or 'BOOTSTRAPPING'))}</div></div>"
        f"<div class='intel-item'><div class='intel-meta'>Laatste seed run</div><div class='intel-reason'>{escape(str(status.get('latest_seed_run') or 'n/a'))}</div></div>"
    )


def _signal_row(signal: dict) -> str:
    direction = escape(str(signal.get("direction", "")))
    drivers = ", ".join(signal.get("intermarket_drivers", [])[:3])
    links = ", ".join([*signal.get("linked_markets", []), *signal.get("linked_assets", [])][:5])
    return (
        "<tr>"
        f"<td>{escape(str(signal.get('exchange', 'GLOBAL')))}</td>"
        f"<td>{escape(str(signal.get('asset', '')))}</td>"
        f"<td>{escape(str(signal.get('asset_class', 'equity')))}</td>"
        f"<td class='{direction}'>{direction}</td>"
        f"<td>{escape(str(signal.get('entry_score', '')))}</td>"
        f"<td>{escape(str(signal.get('confidence', '')))}</td>"
        f"<td>{escape(drivers)}</td>"
        f"<td>{escape(links)}</td>"
        f"<td>{escape(str(signal.get('reason', '')))}</td>"
        "</tr>"
    )


def _news_radar_row(event: dict) -> str:
    radar = event.get("metadata", {}).get("radar", {})
    impact = escape(str(radar.get("impact", "low")))
    themes = "".join(
        f"<span class='chip'>{escape(str(theme))}</span>"
        for theme in radar.get("themes", [])[:5]
    )
    return (
        "<tr>"
        f"<td class='{impact}'>{impact}</td>"
        f"<td>{escape(str(event.get('source', '')))}</td>"
        f"<td>{escape(str(event.get('asset', 'GLOBAL')))}</td>"
        f"<td>{escape(str(event.get('sentiment', '')))}</td>"
        f"<td>{escape(str(event.get('relevance', '')))}</td>"
        f"<td><div class='chips'>{themes}</div></td>"
        f"<td>{escape(str(event.get('published_at', '')))}</td>"
        f"<td>{escape(str(event.get('headline', '')))}</td>"
        "</tr>"
    )


def _session_row(session: dict) -> str:
    return (
        "<tr>"
        f"<td>{escape(str(session.get('exchange', '')))}</td>"
        f"<td>{escape(str(session.get('region', '')))}</td>"
        f"<td>{escape(str(session.get('session', '')))}</td>"
        f"<td>{escape(str(session.get('is_trading', '')))}</td>"
        f"<td>{escape(str(session.get('next_open_at', '')))}</td>"
        "</tr>"
    )


def _group_exchanges_by_region(exchanges: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for exchange in exchanges:
        grouped.setdefault(exchange.get("region", "Unknown"), []).append(exchange)
    return dict(sorted(grouped.items(), key=lambda item: item[0]))


def _group_assets_by_region(assets: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for asset in assets:
        exchange = exchange_universe.get(asset["exchange"])
        region = exchange.region if exchange else "Unknown"
        grouped.setdefault(region, []).append(asset)
    return dict(sorted(grouped.items(), key=lambda item: item[0]))


def _coverage_row(region: str, exchanges: list[dict]) -> str:
    chips = "".join(
        f"<span class='chip'>{escape(exchange['code'])}</span>"
        for exchange in sorted(exchanges, key=lambda item: item["code"])
    )
    return (
        "<tr>"
        f"<td>{escape(region)}</td>"
        f"<td>{len(exchanges)}</td>"
        f"<td><div class='chips'>{chips}</div></td>"
        "</tr>"
    )


def _asset_region_row(region: str, assets: list[dict]) -> str:
    examples = "".join(
        f"<span class='chip'>{escape(asset['exchange'])}:{escape(asset['symbol'])}</span>"
        for asset in sorted(assets, key=lambda item: (item["exchange"], item["symbol"]))[:12]
    )
    return (
        "<tr>"
        f"<td>{escape(region)}</td>"
        f"<td>{len(assets)}</td>"
        f"<td><div class='chips'>{examples}</div></td>"
        "</tr>"
    )


def _intermarket_link_row(link: dict) -> str:
    drivers = "".join(f"<span class='chip'>{escape(driver)}</span>" for driver in link.get("drivers", []))
    linked = "".join(f"<span class='chip'>{escape(item)}</span>" for item in link.get("linked", []))
    return (
        "<tr>"
        f"<td>{escape(str(link.get('theme', '')))}</td>"
        f"<td><div class='chips'>{drivers}</div></td>"
        f"<td><div class='chips'>{linked}</div></td>"
        f"<td>{escape(str(link.get('effect', '')))}</td>"
        "</tr>"
    )


def _intermarket_context_row(link: dict) -> str:
    return (
        "<tr>"
        f"<td>{escape(str(link.get('driver', '')))}</td>"
        f"<td>{escape(str(link.get('direction', '')))}</td>"
        f"<td>{escape(str(link.get('strength', '')))}</td>"
        f"<td>{escape(str(link.get('explanation', '')))}</td>"
        "</tr>"
    )


def _backtest_signal_payload(signal: dict) -> dict:
    timestamp = signal.get("timestamp") or signal.get("created_at")
    asset = str(signal.get("asset") or signal.get("symbol") or "").upper()
    asset_class = str(signal.get("asset_class") or _infer_asset_class(asset)).lower()
    direction = str(signal.get("direction") or "").lower()
    return {
        "signal_id": signal.get("signal_id") or signal.get("id"),
        "id": signal.get("id"),
        "asset": asset,
        "symbol": asset,
        "direction": direction,
        "timestamp": timestamp,
        "created_at": signal.get("created_at"),
        "expires_at": signal.get("expires_at"),
        "entry_score": signal.get("entry_score", 0.0),
        "confidence": signal.get("confidence", 0.0),
        "asset_class": asset_class,
        "source_field": signal.get("source_field") or asset_class,
        "exchange": signal.get("exchange"),
        "region": signal.get("region"),
        "time_window": signal.get("time_window"),
        "risk_flags": signal.get("risk_flags", []),
        "linked_assets": signal.get("linked_assets", []),
        "linked_markets": signal.get("linked_markets", []),
        "intermarket_drivers": signal.get("intermarket_drivers", []),
        "intermarket_context": signal.get("intermarket_context", {}),
        "components": signal.get("components", {}),
        "reason": signal.get("reason", ""),
        "seed_source": signal.get("seed_source"),
        "sentiment_quality": signal.get("sentiment_quality"),
        "seed_market_quality": signal.get("seed_market_quality"),
        "seed_market_timestamp": signal.get("seed_market_timestamp"),
        "watchtower_export_version": "signals.v1",
    }


def _infer_asset_class(asset: str) -> str:
    return "crypto" if "-" in asset else "equity"


def _score_signal(event, market, exchange, asset_info: dict | None) -> dict:
    signal = regional_scorer.score(event, market, exchange, asset_info=asset_info)
    if not exchange:
        return signal

    context = intermarket_engine.context(asset_info, exchange, market)
    adjustment = float(context.get("score_adjustment", 0.0))
    signal["intermarket_context"] = context
    signal["linked_assets"] = context.get("linked_assets", [])
    signal["linked_markets"] = context.get("linked_markets", [])
    signal["intermarket_drivers"] = context.get("drivers", [])
    signal["asset_class"] = context.get("asset_class", signal.get("asset_class", "equity"))
    signal["source_field"] = signal["asset_class"]
    signal["components"]["intermarket_adjustment"] = adjustment
    signal["entry_score"] = round(_clamp_score(float(signal["entry_score"]) + adjustment), 4)
    signal["confidence"] = round(_clamp_score(float(signal["confidence"]) + (adjustment * 0.5)), 4)
    signal["risk_flags"] = sorted(set(signal.get("risk_flags", []) + context.get("flags", [])))
    if context.get("thesis") and context["thesis"] != "No strong intermarket thesis yet.":
        signal["reason"] = f"{signal['reason']} Intermarket: {context['thesis']}"
    return signal


def _clamp_score(value: float) -> float:
    return max(0.0, min(1.0, value))


def _build_best_entries(asset_class: str) -> list[dict]:
    entries: list[dict] = []
    best_by_exchange: dict[str, dict] = {}
    for signal in store.list_signals(limit=250):
        signal_asset_class = str(signal.get("asset_class") or _infer_asset_class(str(signal.get("asset") or ""))).lower()
        if signal_asset_class != asset_class:
            continue
        exchange_code = str(signal.get("exchange") or "GLOBAL").upper()
        asset = str(signal.get("asset") or "").upper()
        entry = dict(signal)
        listed = asset_universe.get(exchange_code, asset) if exchange_code != "GLOBAL" else None
        entry["asset_name"] = listed.to_dict().get("name") if listed else asset
        entry["benchmark_change_1d_pct"] = float(entry.get("benchmark_change_1d_pct") or 0.0)
        if exchange_code not in best_by_exchange or _entry_rank(entry) > _entry_rank(best_by_exchange[exchange_code]):
            best_by_exchange[exchange_code] = entry
    entries.extend(best_by_exchange.values())
    return sorted(entries, key=_entry_rank, reverse=True)


def _entry_rank(signal: dict) -> tuple[float, float]:
    return (float(signal.get("entry_score", 0.0)), float(signal.get("confidence", 0.0)))


def _global_market_mood(entries: list[dict]) -> dict:
    if not entries:
        return {"state": "sideways", "label": "Sideways", "average_benchmark_change_1d_pct": 0.0}
    average = sum(float(entry.get("benchmark_change_1d_pct", 0.0)) for entry in entries) / len(entries)
    if average >= 0.25:
        state = "up"
        label = "Global markets up"
    elif average <= -0.25:
        state = "down"
        label = "Global markets down"
    else:
        state = "sideways"
        label = "Global markets sideways"
    return {
        "state": state,
        "label": label,
        "average_benchmark_change_1d_pct": round(average, 4),
    }


def _asset_class_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for listed_asset in asset_universe.list():
        asset_class = listed_asset.get("asset_class", "unknown")
        counts[asset_class] = counts.get(asset_class, 0) + 1
    return counts


def _best_entry_row(entry: dict) -> str:
    direction = escape(str(entry.get("direction", "")))
    risk_flags = ", ".join(entry.get("risk_flags", [])[:4])
    return (
        "<tr>"
        f"<td>{escape(str(entry.get('exchange', '')))}</td>"
        f"<td>{escape(str(entry.get('asset', '')))}</td>"
        f"<td>{escape(str(entry.get('asset_name', '')))}</td>"
        f"<td class='{direction}'>{direction}</td>"
        f"<td>{escape(str(entry.get('entry_score', '')))}</td>"
        f"<td>{escape(str(entry.get('confidence', '')))}</td>"
        f"<td>{escape(str(entry.get('time_window', '')))}</td>"
        f"<td>{escape(risk_flags)}</td>"
        "</tr>"
    )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _resolve_exchange(value: str | None) -> str | None:
    if not value:
        return None
    return value.upper()
