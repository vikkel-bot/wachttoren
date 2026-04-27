from __future__ import annotations

import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from uuid import uuid4

from watchtower.domain import MarketSnapshot, NewsEvent, utc_now
from watchtower.services.resolver import AssetResolver


POSITIVE_WORDS = {
    "beat",
    "beats",
    "breakout",
    "growth",
    "higher",
    "profit",
    "raise",
    "raises",
    "record",
    "surge",
    "upgraded",
}
NEGATIVE_WORDS = {
    "downgrade",
    "downgraded",
    "fall",
    "falls",
    "fraud",
    "loss",
    "miss",
    "misses",
    "probe",
    "recall",
    "risk",
    "slump",
    "warning",
}


def naive_sentiment(text: str) -> float:
    words = set(re.findall(r"[a-z]+", text.lower()))
    positive = len(words & POSITIVE_WORDS)
    negative = len(words & NEGATIVE_WORDS)
    if positive == negative:
        return 0.0
    return max(-1.0, min(1.0, (positive - negative) / 4.0))


def parse_datetime(value: str | None) -> datetime:
    if not value:
        return utc_now()
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return utc_now()


class ConnectorRegistry:
    def __init__(self, resolver: AssetResolver | None = None) -> None:
        self.resolver = resolver or AssetResolver()

    def list_connectors(self) -> list[dict]:
        return [
            {
                "name": "mock-news",
                "type": "news",
                "requires": [],
                "description": "Deterministic demo news events for local testing.",
            },
            {
                "name": "mock-regional-news",
                "type": "news",
                "requires": [],
                "description": "Region-aware demo news events for exchange pipeline testing.",
            },
            {
                "name": "rss",
                "type": "news",
                "requires": ["feed_url"],
                "description": "Fetches public RSS/Atom-like feeds through urllib.",
            },
            {
                "name": "mock-market",
                "type": "market",
                "requires": [],
                "description": "Deterministic demo market snapshot for local testing.",
            },
            {
                "name": "mock-regional-market",
                "type": "market",
                "requires": [],
                "description": "Region-aware demo market snapshot for exchange pipeline testing.",
            },
        ]

    def fetch_news(
        self,
        connector: str,
        asset: str,
        limit: int = 10,
        feed_url: str | None = None,
        exchange: str | None = None,
    ) -> list[NewsEvent]:
        connector = connector.lower()
        resolved_asset = self.resolver.resolve(asset)
        if connector in {"mock-news", "mock-regional-news"}:
            return self._mock_news(resolved_asset, limit, exchange=exchange)
        if connector == "rss":
            if not feed_url:
                raise ValueError("feed_url is required for rss connector")
            return self._rss_news(resolved_asset, feed_url, limit)
        raise ValueError(f"Unknown news connector: {connector}")

    def fetch_market(self, connector: str, asset: str, exchange: str | None = None) -> MarketSnapshot:
        connector = connector.lower()
        resolved_asset = self.resolver.resolve(asset)
        if connector not in {"mock-market", "mock-regional-market"}:
            raise ValueError(f"Unknown market connector: {connector}")
        profile = self._market_profile(exchange)
        return MarketSnapshot(
            asset=resolved_asset,
            price=profile["price"],
            change_15m_pct=profile["change_15m_pct"],
            change_1h_pct=profile["change_1h_pct"],
            change_1d_pct=profile["change_1d_pct"],
            volume_zscore=profile["volume_zscore"],
            volatility_zscore=profile["volatility_zscore"],
            trend_1h=profile["trend_1h"],
            trend_1d=profile["trend_1d"],
            sector_change_1d_pct=profile["sector_change_1d_pct"],
            benchmark_change_1d_pct=profile["benchmark_change_1d_pct"],
            resistance_distance_pct=profile["resistance_distance_pct"],
        )

    def _mock_news(self, asset: str, limit: int, exchange: str | None = None) -> list[NewsEvent]:
        exchange_tag = exchange.upper() if exchange else "GLOBAL"
        examples = [
            (
                f"{asset} beats expectations on {exchange_tag} watch",
                f"Fresh {exchange_tag} report suggests improving momentum and stronger forward guidance.",
                0.72,
                ["earnings", "guidance", exchange_tag.lower()],
            ),
            (
                f"{asset} faces local volatility as traders wait for confirmation",
                "The initial move is mixed while market participants watch volume, currency and sector strength.",
                0.08,
                ["market_context", exchange_tag.lower()],
            ),
            (
                f"Analysts flag downside risk for {asset} in regional trading",
                "A cautious note points to margin pressure, currency sensitivity and slower near-term demand.",
                -0.45,
                ["analyst", "risk", exchange_tag.lower()],
            ),
        ]
        events = []
        for headline, summary, sentiment, tags in examples[: max(1, min(limit, len(examples)))]:
            events.append(
                NewsEvent(
                    id=f"evt_{uuid4().hex[:12]}",
                    asset=asset,
                    headline=headline,
                    summary=summary,
                    source="mock-news",
                    sentiment=sentiment,
                    novelty=0.72,
                    relevance=0.82,
                    tags=tags,
                )
            )
        return events

    def _market_profile(self, exchange: str | None) -> dict[str, float]:
        exchange_code = (exchange or "GLOBAL").upper()
        defaults = {
            "price": 100.0,
            "change_15m_pct": 0.25,
            "change_1h_pct": 0.75,
            "change_1d_pct": 1.4,
            "volume_zscore": 1.8,
            "volatility_zscore": 0.9,
            "trend_1h": 0.42,
            "trend_1d": 0.35,
            "sector_change_1d_pct": 0.6,
            "benchmark_change_1d_pct": 0.45,
            "resistance_distance_pct": 2.0,
        }
        overrides = {
            "NASDAQ": {"volume_zscore": 2.4, "trend_1h": 0.62, "trend_1d": 0.48, "benchmark_change_1d_pct": 0.75},
            "AEX": {"volume_zscore": 1.7, "trend_1h": 0.38, "sector_change_1d_pct": 0.85, "benchmark_change_1d_pct": 0.35},
            "JPX": {"change_1d_pct": 1.8, "volatility_zscore": 1.35, "benchmark_change_1d_pct": 0.9},
            "HKEX": {"change_1d_pct": 1.7, "volatility_zscore": 1.55, "benchmark_change_1d_pct": 0.7},
            "SSE": {"change_1d_pct": 1.25, "volatility_zscore": 1.2, "benchmark_change_1d_pct": 0.55},
            "JSE": {"volume_zscore": 1.35, "volatility_zscore": 1.45, "benchmark_change_1d_pct": 0.25},
            "NGX": {"volume_zscore": 1.1, "volatility_zscore": 1.8, "benchmark_change_1d_pct": 0.35},
            "EGX": {"volume_zscore": 1.2, "volatility_zscore": 1.7, "benchmark_change_1d_pct": 0.4},
            "NSE_KE": {"volume_zscore": 1.15, "volatility_zscore": 1.55, "benchmark_change_1d_pct": 0.2},
            "CSE_MA": {"volume_zscore": 1.1, "volatility_zscore": 1.35, "benchmark_change_1d_pct": 0.15},
            "COMEX": {"volume_zscore": 2.1, "volatility_zscore": 1.25, "trend_1h": 0.5, "benchmark_change_1d_pct": 0.45},
            "NYMEX": {"volume_zscore": 2.0, "volatility_zscore": 1.85, "trend_1h": 0.35, "benchmark_change_1d_pct": 0.2},
            "ICE": {"volume_zscore": 1.9, "volatility_zscore": 1.65, "trend_1h": 0.32, "benchmark_change_1d_pct": 0.25},
            "LME": {"volume_zscore": 1.7, "volatility_zscore": 1.35, "trend_1h": 0.28, "benchmark_change_1d_pct": 0.15},
            "SHFE": {"volume_zscore": 1.8, "volatility_zscore": 1.45, "trend_1h": 0.3, "benchmark_change_1d_pct": 0.35},
        }
        return {**defaults, **overrides.get(exchange_code, {})}

    def _rss_news(self, asset: str, feed_url: str, limit: int) -> list[NewsEvent]:
        with urllib.request.urlopen(feed_url, timeout=8) as response:
            raw_xml = response.read()

        root = ET.fromstring(raw_xml)
        items = root.findall(".//item")
        if not items:
            items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

        events: list[NewsEvent] = []
        for item in items[: max(1, min(limit, 50))]:
            title = self._xml_text(item, "title")
            summary = self._xml_text(item, "description") or self._xml_text(item, "{http://www.w3.org/2005/Atom}summary")
            link = self._xml_text(item, "link")
            if not link:
                link_node = item.find("{http://www.w3.org/2005/Atom}link")
                link = link_node.attrib.get("href", "") if link_node is not None else ""
            published_at = parse_datetime(
                self._xml_text(item, "pubDate")
                or self._xml_text(item, "published")
                or self._xml_text(item, "{http://www.w3.org/2005/Atom}published")
            )
            text = f"{title} {summary}"
            relevance = 0.85 if asset.upper() in text.upper() else 0.45
            source_name = urllib.parse.urlparse(feed_url).netloc or "rss"
            events.append(
                NewsEvent(
                    id=f"evt_{uuid4().hex[:12]}",
                    asset=asset,
                    headline=title or "Untitled feed item",
                    summary=summary,
                    source=source_name,
                    url=link or None,
                    published_at=published_at,
                    sentiment=naive_sentiment(text),
                    novelty=0.55,
                    relevance=relevance,
                    tags=["rss"],
                )
            )
        return events

    def _xml_text(self, item: ET.Element, name: str) -> str:
        node = item.find(name)
        if node is None or node.text is None:
            return ""
        return node.text.strip()
