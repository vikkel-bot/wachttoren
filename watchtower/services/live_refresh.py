from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from watchtower.domain import MarketSnapshot, NewsEvent, utc_now
from watchtower.services.assets import ListedAssetUniverse
from watchtower.services.connectors import BBC_RSS_FEEDS, ConnectorRegistry
from watchtower.services.exchanges import ExchangeUniverse
from watchtower.storage import SQLiteStore

ScoreSignal = Callable[[NewsEvent, MarketSnapshot, Any, dict[str, Any] | None], dict[str, Any]]

_CRYPTO_ARTICLE_KEYWORDS = {
    "bitcoin",
    "btc",
    "ethereum",
    "ether",
    "eth",
    "crypto",
    "cryptocurrency",
    "blockchain",
    "stablecoin",
    "defi",
    "web3",
    "solana",
}

_COMPANY_OR_SECTOR_KEYWORDS = {
    "company",
    "companies",
    "shares",
    "stock",
    "stocks",
    "sector",
    "technology",
    "telecom",
    "telecommunications",
    "earnings",
    "profit",
    "revenue",
    "vodafone",
    "apple",
    "nvidia",
    "semiconductor",
}


class WatchtowerLiveRefresher:
    """Periodic real-source refresh for Watchtower production data."""

    def __init__(
        self,
        store: SQLiteStore,
        connectors: ConnectorRegistry,
        asset_universe: ListedAssetUniverse,
        exchange_universe: ExchangeUniverse,
        score_signal: ScoreSignal,
        *,
        news_interval: timedelta = timedelta(minutes=30),
        market_interval: timedelta = timedelta(minutes=15),
        logger: logging.Logger | None = None,
    ) -> None:
        self.store = store
        self.connectors = connectors
        self.asset_universe = asset_universe
        self.exchange_universe = exchange_universe
        self.score_signal = score_signal
        self.news_interval = news_interval
        self.market_interval = market_interval
        self.logger = logger or logging.getLogger("watchtower.live_refresh")
        self._last_news_at: datetime | None = None
        self._last_market_at: datetime | None = None
        self._lock = threading.Lock()

    def tick(self, now: datetime | None = None, *, force: bool = False) -> dict[str, Any]:
        moment = self._normalize(now or utc_now())
        with self._lock:
            watchlist = self.store.list_watchlist(enabled_only=True)
            matched_events: list[NewsEvent] = []
            markets: dict[str, tuple[dict[str, Any], MarketSnapshot]] = {}
            errors: list[str] = []

            should_fetch_news = force or self._due(self._last_news_at, self.news_interval, moment)
            should_fetch_market = force or self._due(self._last_market_at, self.market_interval, moment)

            if should_fetch_news:
                matched_events, news_errors = self._refresh_news(watchlist)
                errors.extend(news_errors)
                self._last_news_at = moment

            if should_fetch_market:
                markets, market_errors = self._refresh_markets(watchlist)
                errors.extend(market_errors)
                self._last_market_at = moment

            signals_created = self._generate_signals(watchlist, markets, matched_events)
            report = {
                "news_articles": len(matched_events),
                "assets_updated": len(markets),
                "signals_created": signals_created,
                "errors": errors,
            }
            self.logger.info(
                "Watchtower refresh | nieuws: %s artikelen | assets: %s bijgewerkt",
                report["news_articles"],
                report["assets_updated"],
            )
            return report

    def _refresh_news(self, watchlist: list[dict[str, Any]]) -> tuple[list[NewsEvent], list[str]]:
        matched: list[NewsEvent] = []
        errors: list[str] = []
        for connector in BBC_RSS_FEEDS:
            try:
                events = self.connectors.fetch_news(connector, "GLOBAL", limit=50)
            except Exception as exc:
                message = f"{connector} refresh failed: {exc}"
                self.logger.warning(message)
                errors.append(message)
                continue
            for event in events:
                for item in watchlist:
                    asset_event = self._match_event_to_watchlist(event, item)
                    if asset_event is None:
                        continue
                    matched.append(asset_event)
                    self.store.save_event(asset_event)
        return matched, errors

    def _refresh_markets(self, watchlist: list[dict[str, Any]]) -> tuple[dict[str, tuple[dict[str, Any], MarketSnapshot]], list[str]]:
        markets: dict[str, tuple[dict[str, Any], MarketSnapshot]] = {}
        errors: list[str] = []
        for item in watchlist:
            asset = str(item.get("asset") or "").upper()
            if not asset:
                continue
            try:
                market = self.connectors.fetch_market("yfinance-market", asset, exchange=item.get("exchange"))
            except Exception as exc:
                message = f"yfinance market refresh skipped for {asset}: {exc}"
                self.logger.warning(message)
                errors.append(message)
                continue
            markets[self._watch_key(item)] = (item, market)
            self.store.save_market_snapshot(market)
        return markets, errors

    def _generate_signals(
        self,
        watchlist: list[dict[str, Any]],
        markets: dict[str, tuple[dict[str, Any], MarketSnapshot]],
        matched_events: list[NewsEvent],
    ) -> int:
        if not markets:
            return 0

        events_by_asset: dict[str, list[NewsEvent]] = {}
        for event in matched_events:
            events_by_asset.setdefault(event.asset.upper(), []).append(event)

        created = 0
        for item in watchlist:
            key = self._watch_key(item)
            if key not in markets:
                continue
            market_item, market = markets[key]
            asset = str(market_item.get("asset") or "").upper()
            events = events_by_asset.get(asset)
            if not events:
                events = self._recent_stored_events(asset)
            for event in events[:3]:
                requested_exchange = str(market_item.get("exchange") or "GLOBAL").upper()
                asset_info_obj = self._listed_asset(requested_exchange, asset)
                exchange_code = asset_info_obj.exchange if asset_info_obj else requested_exchange
                exchange = self.exchange_universe.get(exchange_code)
                asset_info = asset_info_obj.to_dict() if asset_info_obj else None
                try:
                    signal = self.score_signal(event, market, exchange, asset_info)
                except Exception as exc:
                    self.logger.warning("signal generation skipped for %s: %s", asset, exc)
                    continue
                signal["id"] = self._signal_id(event, market)
                signal["event_id"] = event.id
                signal["source_field"] = signal.get("source_field") or signal.get("asset_class") or "equity"
                saved = self.store.save_signal(signal)
                if saved.get("id") == signal["id"]:
                    created += 1
        return created

    def _match_event_to_watchlist(self, event: NewsEvent, item: dict[str, Any]) -> NewsEvent | None:
        keywords = self._keywords(item)
        text = f"{event.headline} {event.summary}".lower()
        asset_class = self._asset_class(item)
        crypto_article = self._is_crypto_article(text)
        company_or_sector_article = self._is_company_or_sector_article(text)
        if asset_class == "crypto" and not crypto_article:
            return None
        if asset_class != "equity" and company_or_sector_article and not crypto_article:
            return None
        if not self._matches_keywords(text, keywords):
            return None
        asset = str(item.get("asset") or "").upper()
        if not asset:
            return None
        metadata = dict(event.metadata)
        metadata["matched_watchlist"] = {
            "exchange": str(item.get("exchange") or "GLOBAL").upper(),
            "asset": asset,
            "keywords": sorted(keywords),
        }
        metadata.setdefault(
            "radar",
            {
                "impact_score": round(max(event.relevance, abs(event.sentiment), 0.5), 4),
                "impact": "medium" if event.relevance >= 0.6 else "low",
                "urgency": 0.7,
                "themes": ["live_rss"],
                "source_kind": "headline_rss",
                "source_reliability": 0.82 if event.source.startswith("bbc-") else 0.7,
                "matched_asset": {
                    "exchange": str(item.get("exchange") or "GLOBAL").upper(),
                    "symbol": asset,
                },
            },
        )
        event_id = self._event_id(event, asset)
        return replace(
            event,
            id=event_id,
            asset=asset,
            relevance=max(event.relevance, 0.72),
            tags=sorted(set([*event.tags, "watchlist_match", "news_radar", "live_rss"])),
            metadata=metadata,
        )

    def _keywords(self, item: dict[str, Any]) -> set[str]:
        exchange_code = str(item.get("exchange") or "").upper()
        asset = str(item.get("asset") or "").upper()
        keywords = {asset.lower()}
        if "-" in asset:
            keywords.add(asset.split("-", 1)[0].lower())
        listed = self._listed_asset(exchange_code, asset)
        if listed:
            data = listed.to_dict()
            keywords.add(str(data.get("name") or "").lower())
            keywords.update(str(alias).lower() for alias in data.get("aliases", []) if alias)
        return {keyword for keyword in keywords if len(keyword) >= 3}

    def _asset_class(self, item: dict[str, Any]) -> str:
        explicit = str(item.get("asset_class") or "").lower().strip()
        if explicit:
            return explicit
        exchange_code = str(item.get("exchange") or "").upper()
        asset = str(item.get("asset") or "").upper()
        listed = self._listed_asset(exchange_code, asset)
        if listed:
            return listed.asset_class.lower()
        if exchange_code in {"BITVAVO", "BINANCE", "COINBASE", "KRAKEN"}:
            return "crypto"
        if asset.endswith(("-EUR", "-USD", "-BTC")) and asset.split("-", 1)[0] in {"BTC", "ETH", "SOL"}:
            return "crypto"
        return "equity"

    def _listed_asset(self, exchange_code: str, asset: str):
        if exchange_code:
            listed = self.asset_universe.get(exchange_code, asset)
            if listed:
                return listed
        resolved = self.asset_universe.resolve(asset)
        if not resolved:
            return None
        return self.asset_universe.get(str(resolved.get("exchange") or ""), str(resolved.get("symbol") or asset))

    def _matches_keywords(self, text: str, keywords: set[str]) -> bool:
        return any(self._keyword_in_text(text, keyword) for keyword in keywords)

    def _keyword_in_text(self, text: str, keyword: str) -> bool:
        normalized = keyword.lower().strip()
        if not normalized:
            return False
        escaped = re.escape(normalized)
        return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text) is not None

    def _is_crypto_article(self, text: str) -> bool:
        return any(self._keyword_in_text(text, keyword) for keyword in _CRYPTO_ARTICLE_KEYWORDS)

    def _is_company_or_sector_article(self, text: str) -> bool:
        return any(self._keyword_in_text(text, keyword) for keyword in _COMPANY_OR_SECTOR_KEYWORDS)

    def _recent_stored_events(self, asset: str) -> list[NewsEvent]:
        events: list[NewsEvent] = []
        for payload in self.store.list_events(limit=3, asset=asset):
            try:
                events.append(self._event_from_payload(payload))
            except (TypeError, ValueError):
                continue
        return events

    def _event_from_payload(self, payload: dict[str, Any]) -> NewsEvent:
        published_at = payload.get("published_at")
        if isinstance(published_at, str):
            published_at = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        if not isinstance(published_at, datetime):
            published_at = utc_now()
        return NewsEvent(
            id=str(payload["id"]),
            asset=str(payload["asset"]).upper(),
            headline=str(payload.get("headline") or ""),
            summary=str(payload.get("summary") or ""),
            source=str(payload.get("source") or "unknown"),
            url=payload.get("url"),
            published_at=published_at,
            sentiment=float(payload.get("sentiment") or 0.0),
            novelty=float(payload.get("novelty") or 0.5),
            relevance=float(payload.get("relevance") or 0.5),
            tags=list(payload.get("tags") or []),
            metadata=dict(payload.get("metadata") or {}),
        )

    def _watch_key(self, item: dict[str, Any]) -> str:
        return f"{str(item.get('exchange') or 'GLOBAL').upper()}:{str(item.get('asset') or '').upper()}"

    def _event_id(self, event: NewsEvent, asset: str) -> str:
        raw = f"{event.id}|{asset}|{event.url or ''}"
        return f"evt_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]}"

    def _signal_id(self, event: NewsEvent, market: MarketSnapshot) -> str:
        raw = f"{event.id}|{market.asset}|{market.timestamp.isoformat()}"
        return f"sig_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]}"

    def _due(self, last_run: datetime | None, interval: timedelta, now: datetime) -> bool:
        return last_run is None or now - last_run >= interval

    def _normalize(self, value: datetime) -> datetime:
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


class PeriodicWatchtowerScheduler:
    def __init__(
        self,
        refresher: WatchtowerLiveRefresher,
        *,
        tick_seconds: float = 60.0,
        enabled: bool = True,
        logger: logging.Logger | None = None,
    ) -> None:
        self.refresher = refresher
        self.tick_seconds = max(1.0, float(tick_seconds))
        self.enabled = enabled
        self.logger = logger or logging.getLogger("watchtower.live_refresh")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, *, run_immediately: bool = False) -> None:
        if not self.enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        if self._thread and not self._thread.is_alive():
            self.logger.error("Watchtower refresh scheduler thread was stopped; restarting.")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(run_immediately,),
            name="watchtower-live-refresh",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def tick(self, *, force: bool = False) -> dict[str, Any]:
        return self.refresher.tick(force=force)

    def _run(self, run_immediately: bool) -> None:
        try:
            if run_immediately:
                self._tick_safe()
            while not self._stop.wait(self.tick_seconds):
                self._tick_safe()
        except Exception:
            self.logger.exception("Watchtower refresh scheduler stopped unexpectedly.")
        finally:
            self._thread = None

    def _tick_safe(self) -> None:
        started_at = datetime.now(timezone.utc)
        t0 = time.monotonic()
        self.logger.info("Watchtower refresh start | at=%s", started_at.isoformat())
        try:
            report = self.refresher.tick()
        except Exception as exc:
            self.logger.warning("Watchtower refresh overgeslagen door fout: %s", exc)
            self.logger.info(
                "Watchtower refresh einde | status=failed tijd=%.1fs fout=%s",
                time.monotonic() - t0,
                exc,
            )
        else:
            self.logger.info(
                "Watchtower refresh einde | status=ok tijd=%.1fs nieuws=%s assets=%s signalen=%s fouten=%s",
                time.monotonic() - t0,
                int(report.get("news_articles") or 0),
                int(report.get("assets_updated") or 0),
                int(report.get("signals_created") or 0),
                len(report.get("errors") or []),
            )
