from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from watchtower.domain import MarketSnapshot, NewsEvent
from watchtower.main import _backtest_signal_payload, _score_signal, asset_universe, exchange_universe
from watchtower.models import MarketSnapshotIn, NewsEventIn, SignalEvaluationIn
from watchtower.services.connectors import ConnectorRegistry, RateLimitError
from watchtower.services.seed_signals import append_seed_signals, seed_signals_path
from watchtower.storage import to_jsonable


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    from_dt = _parse_datetime(args.from_dt)
    to_dt = _parse_datetime(args.to_dt)
    assets = _assets(args.assets)
    connectors = ConnectorRegistry()
    output_path = Path(args.output) if args.output else seed_signals_path()

    all_signals: list[dict[str, Any]] = []
    exit_code = 0
    print(f"Seed periode: {from_dt.isoformat()} -> {to_dt.isoformat()}")
    print(f"Assets: {', '.join(assets)}")
    print(f"Exchange: {args.exchange}")
    print(f"Dry-run: {args.dry_run}")

    for asset in assets:
        try:
            result = seed_asset(
                asset=asset,
                exchange=args.exchange,
                from_dt=from_dt,
                to_dt=to_dt,
                connectors=connectors,
                dry_run=args.dry_run,
            )
        except Exception as exc:
            exit_code = 1
            print(f"{asset}: seed mislukt: {exc}")
            continue

        all_signals.extend(result["signals"])
        print(_asset_summary(asset, result))
        if args.dry_run:
            print(f"Zou {result['candidate_articles']} signalen genereren voor {result['articles_found']} artikelen")

    if args.dry_run:
        print("Dry-run klaar: geen signalen gegenereerd en geen files geschreven.")
        return exit_code

    if all_signals:
        written_path = append_seed_signals(all_signals, output_path)
        print(f"Seed signalen geschreven: {written_path}")
    else:
        print("Geen seed signalen gegenereerd; seed file niet gewijzigd.")

    print(f"Totaal gegenereerde signalen: {len(all_signals)}")
    return exit_code


def seed_asset(
    asset: str,
    exchange: str,
    from_dt: datetime,
    to_dt: datetime,
    connectors: ConnectorRegistry,
    dry_run: bool = False,
) -> dict[str, Any]:
    news_events = connectors.fetch_historical_news(
        connector="eodhd-news",
        asset=asset,
        from_dt=from_dt,
        to_dt=to_dt,
        limit=500,
    )
    sentiment_events = _fetch_sentiment_events(connectors, asset, from_dt, to_dt)
    relevant_events = [event for event in news_events if event.relevance >= 0.3]

    result: dict[str, Any] = {
        "articles_found": len(news_events),
        "candidate_articles": len(relevant_events),
        "sentiment_events": len(sentiment_events),
        "signals_generated": 0,
        "avg_entry_score": None,
        "avg_sentiment": _avg([event.sentiment for event in sentiment_events]),
        "signals": [],
    }
    if dry_run:
        return result

    market = connectors.fetch_market("bitvavo-public", asset, exchange=exchange)
    generated: list[dict[str, Any]] = []
    entry_scores: list[float] = []
    article_sentiments: list[float] = []

    for event in relevant_events:
        enriched_event = _with_backfilled_sentiment(event, sentiment_events)
        article_sentiments.append(enriched_event.sentiment)
        payload = _evaluation_payload(enriched_event, market)
        exchange_obj = exchange_universe.get(exchange)
        asset_info_obj = asset_universe.get(exchange, enriched_event.asset) if exchange_obj else None
        asset_info = asset_info_obj.to_dict() if asset_info_obj else None
        signal = _score_signal(
            payload.event.to_domain(enriched_event.asset),
            payload.market.to_domain(market.asset),
            exchange_obj,
            asset_info,
        )
        signal = _as_historical_signal(signal, enriched_event)
        if not signal.get("direction"):
            continue
        # neutral signalen worden WEL opgeslagen in seed context
        # backtest beslist zelf of het iets mee doet
        normalized = _backtest_signal_payload(signal)
        generated.append(normalized)
        entry_scores.append(float(normalized.get("entry_score") or 0.0))

    result["signals"] = generated
    result["signals_generated"] = len(generated)
    result["avg_entry_score"] = _avg(entry_scores)
    result["avg_sentiment"] = _avg(article_sentiments) if article_sentiments else result["avg_sentiment"]
    return result


def _fetch_sentiment_events(
    connectors: ConnectorRegistry,
    asset: str,
    from_dt: datetime,
    to_dt: datetime,
) -> list[NewsEvent]:
    try:
        return connectors.fetch_historical_news(
            connector="alphavantage-news",
            asset=asset,
            from_dt=from_dt,
            to_dt=to_dt,
            limit=100,
        )
    except RateLimitError as exc:
        print(f"{asset}: Alpha Vantage rate limit bereikt: {exc}")
        return []
    except ValueError as exc:
        print(f"{asset}: Alpha Vantage sentiment niet beschikbaar: {exc}")
        return []


def _with_backfilled_sentiment(event: NewsEvent, sentiment_events: list[NewsEvent]) -> NewsEvent:
    sentiment = _sentiment_for_event(event, sentiment_events)
    if sentiment is None:
        return event
    tags = sorted(set([*event.tags, "alpha_vantage_sentiment"]))
    metadata = {
        **event.metadata,
        "seed_sentiment_source": "alphavantage-news",
        "original_sentiment": event.sentiment,
    }
    return replace(event, sentiment=sentiment, tags=tags, metadata=metadata)


def _sentiment_for_event(event: NewsEvent, sentiment_events: list[NewsEvent]) -> float | None:
    if not sentiment_events:
        return None
    same_day = [
        candidate.sentiment
        for candidate in sentiment_events
        if candidate.published_at.date() == event.published_at.date()
    ]
    if same_day:
        return _avg(same_day)
    closest = min(
        sentiment_events,
        key=lambda candidate: abs((candidate.published_at - event.published_at).total_seconds()),
    )
    if abs((closest.published_at - event.published_at).total_seconds()) <= 3 * 24 * 3600:
        return closest.sentiment
    return _avg([candidate.sentiment for candidate in sentiment_events])


def _evaluation_payload(event: NewsEvent, market: MarketSnapshot) -> SignalEvaluationIn:
    return SignalEvaluationIn(
        event=NewsEventIn(**to_jsonable(event)),
        market=MarketSnapshotIn(**to_jsonable(market)),
    )


def _as_historical_signal(signal: dict[str, Any], event: NewsEvent) -> dict[str, Any]:
    signal = to_jsonable(signal)
    timestamp = event.published_at if event.published_at.tzinfo else event.published_at.replace(tzinfo=timezone.utc)
    signal["timestamp"] = timestamp.isoformat()
    signal["created_at"] = timestamp.isoformat()
    signal["expires_at"] = (timestamp + _expiry_delta(signal.get("time_window"))).isoformat()
    signal["seed_source"] = "watchtower_historical_news_seed"
    signal["sentiment_quality"] = "av_backfilled" if event.metadata.get("seed_sentiment_source") else "eodhd_default"
    return signal


def _expiry_delta(time_window: Any) -> timedelta:
    windows = {
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
        "1d": timedelta(days=1),
    }
    return windows.get(str(time_window), timedelta(hours=1))


def _asset_summary(asset: str, result: dict[str, Any]) -> str:
    return (
        f"{asset}: artikelen={result['articles_found']} "
        f"kandidaten={result['candidate_articles']} "
        f"sentiment_events={result['sentiment_events']} "
        f"signalen={result['signals_generated']} "
        f"avg_entry_score={_fmt(result['avg_entry_score'])} "
        f"avg_sentiment={_fmt(result['avg_sentiment'])}"
    )


def _fmt(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.4f}"


def _avg(values: list[float]) -> float | None:
    return round(mean(values), 4) if values else None


def _assets(value: str) -> list[str]:
    assets = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not assets:
        raise ValueError("--assets must contain at least one asset")
    return assets


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed historical Watchtower backtest signals from EODHD + Alpha Vantage.")
    parser.add_argument("--from", dest="from_dt", required=True)
    parser.add_argument("--to", dest="to_dt", required=True)
    parser.add_argument("--assets", required=True, help="Comma-separated assets, e.g. BTC-EUR,ETH-EUR,SOL-EUR")
    parser.add_argument("--exchange", default="BITVAVO")
    parser.add_argument("--output", help="Optional output path. Defaults to watchtower/data/backtest_signals_seed.jsonl.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
