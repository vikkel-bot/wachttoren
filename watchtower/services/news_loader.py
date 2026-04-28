from __future__ import annotations

from datetime import datetime
from typing import Any

from watchtower.services.connectors import ConnectorRegistry, RateLimitError
from watchtower.storage import SQLiteStore


class HistoricalNewsLoader:
    """
    Loads historical news for a backtest window.

    Alpaca is used as the high-volume historical source. Alpha Vantage is used
    as the sentiment source while the daily request budget allows it.
    """

    def __init__(self, connectors: ConnectorRegistry | None = None) -> None:
        self.connectors = connectors or ConnectorRegistry()

    def load_for_backtest(
        self,
        assets: list[str],
        from_dt: datetime,
        to_dt: datetime,
        store: SQLiteStore,
    ) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "articles_loaded": 0,
            "sentiment_scored": 0,
            "assets_covered": [],
            "errors": {},
        }

        for asset in assets:
            loaded_for_asset = 0
            try:
                alpaca_events = self.connectors.fetch_historical_news(
                    connector="alpaca-news",
                    asset=asset,
                    from_dt=from_dt,
                    to_dt=to_dt,
                    limit=500,
                )
                for event in alpaca_events:
                    store.save_event(event)
                loaded_for_asset += len(alpaca_events)
                stats["articles_loaded"] += len(alpaca_events)
            except ValueError as exc:
                stats["errors"][asset] = f"alpaca-news: {exc}"

            try:
                alpha_events = self.connectors.fetch_historical_news(
                    connector="alphavantage-news",
                    asset=asset,
                    from_dt=from_dt,
                    to_dt=to_dt,
                    limit=50,
                )
                for event in alpha_events:
                    store.save_event(event)
                loaded_for_asset += len(alpha_events)
                stats["articles_loaded"] += len(alpha_events)
                stats["sentiment_scored"] += len(alpha_events)
            except RateLimitError as exc:
                stats["errors"][f"{asset}:alphavantage-news"] = str(exc)
            except ValueError as exc:
                stats["errors"].setdefault(asset, f"alphavantage-news: {exc}")

            if loaded_for_asset:
                stats["assets_covered"].append(asset.upper())

        stats["assets_covered"] = sorted(set(stats["assets_covered"]))
        return stats
