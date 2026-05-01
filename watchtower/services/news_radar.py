from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import uuid4

from watchtower.domain import NewsEvent, utc_now
from watchtower.services.assets import ListedAssetUniverse
from watchtower.services.connectors import naive_sentiment


@dataclass(frozen=True, slots=True)
class RadarSource:
    name: str
    kind: str
    url: str
    cost_eur_month: float
    reliability: float
    enabled: bool = True
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "url": self.url,
            "cost_eur_month": self.cost_eur_month,
            "reliability": self.reliability,
            "enabled": self.enabled,
            "notes": self.notes,
        }


class NewsRadar:
    DEFAULT_QUERIES = [
        "inflation OR central bank OR interest rates OR recession OR PMI",
        "oil OR brent OR WTI OR natural gas OR OPEC",
        "gold OR copper OR silver OR aluminium OR steel",
        "semiconductor OR chips OR AI OR data center OR ASML OR Nvidia OR TSMC",
        "China markets OR Japan markets OR India markets OR Taiwan markets OR Korea markets",
        "South Africa markets OR Nigeria markets OR Egypt markets OR Kenya markets OR commodities",
    ]

    DEFAULT_RSS_SOURCES = [
        RadarSource(
            name="Federal Reserve",
            kind="official_rss",
            url="https://www.federalreserve.gov/feeds/press_all.xml",
            cost_eur_month=0.0,
            reliability=0.96,
            notes="Official US central bank releases.",
        ),
        RadarSource(
            name="ECB",
            kind="official_rss",
            url="https://www.ecb.europa.eu/rss/press.html",
            cost_eur_month=0.0,
            reliability=0.96,
            notes="Official European Central Bank releases.",
        ),
        RadarSource(
            name="bbc-business",
            kind="headline_rss",
            url="https://feeds.bbci.co.uk/news/business/rss.xml",
            cost_eur_month=0.0,
            reliability=0.82,
            notes="Broad international business headlines.",
        ),
        RadarSource(
            name="bbc-technology",
            kind="headline_rss",
            url="https://feeds.bbci.co.uk/news/technology/rss.xml",
            cost_eur_month=0.0,
            reliability=0.82,
            notes="Broad international technology headlines.",
        ),
    ]

    THEME_KEYWORDS = {
        "rates": ["rate", "rates", "central bank", "fed", "ecb", "yield", "bond"],
        "inflation": ["inflation", "cpi", "ppi", "prices", "wage", "tariff"],
        "energy": ["oil", "brent", "wti", "gas", "lng", "opec", "crude"],
        "metals": ["gold", "silver", "copper", "aluminium", "aluminum", "steel", "rebar"],
        "technology": ["semiconductor", "chip", "chips", "ai", "data center", "asml", "nvidia", "tsmc"],
        "asia": ["china", "japan", "india", "taiwan", "korea", "hong kong", "singapore"],
        "africa": ["south africa", "nigeria", "egypt", "kenya", "morocco", "jse", "ngx"],
        "risk": ["war", "sanction", "conflict", "default", "crisis", "geopolitical", "risk"],
        "growth": ["growth", "pmi", "manufacturing", "exports", "consumer", "demand"],
    }

    NEGATIVE_CONTEXT = {
        "warning",
        "slowdown",
        "recession",
        "risk",
        "crisis",
        "conflict",
        "sanction",
        "default",
        "miss",
        "downgrade",
        "cuts",
    }

    POSITIVE_CONTEXT = {
        "growth",
        "beat",
        "surge",
        "record",
        "raise",
        "upgrade",
        "rebound",
        "easing",
        "stimulus",
        "higher",
    }

    def __init__(self, asset_universe: ListedAssetUniverse, monthly_budget_eur: float = 25.0) -> None:
        self.asset_universe = asset_universe
        self.monthly_budget_eur = monthly_budget_eur

    def config(self) -> dict[str, Any]:
        return {
            "monthly_budget_eur": self.monthly_budget_eur,
            "estimated_monthly_cost_eur": 0.0,
            "budget_remaining_eur": self.monthly_budget_eur,
            "policy": "free_sources_first",
            "enabled_sources": [source.to_dict() for source in self.DEFAULT_RSS_SOURCES],
            "free_sources": [
                {
                    "name": "GDELT DOC 2.0",
                    "kind": "global_headline_search",
                    "cost_eur_month": 0.0,
                    "reliability": 0.78,
                    "notes": "Global, multilingual headline discovery through a public API.",
                },
                *[source.to_dict() for source in self.DEFAULT_RSS_SOURCES],
            ],
            "disabled_paid_sources": [
                {
                    "name": "X API",
                    "kind": "social_signal",
                    "cost_eur_month": "pay_per_use",
                    "enabled": False,
                    "notes": "Kept off while the radar budget cap is EUR25/month.",
                },
                {
                    "name": "NewsAPI production",
                    "kind": "news_api",
                    "cost_eur_month": 449.0,
                    "enabled": False,
                    "notes": "Useful later, too expensive for the first proof phase.",
                },
            ],
        }

    def scan(
        self,
        mode: str = "rss",
        queries: list[str] | None = None,
        rss_urls: list[str] | None = None,
        limit: int = 25,
        max_per_source: int = 5,
        timespan: str = "1d",
    ) -> dict[str, Any]:
        mode = mode.lower()
        limit = max(1, min(limit, 100))
        max_per_source = max(1, min(max_per_source, 25))
        warnings: list[str] = []
        events: list[NewsEvent] = []

        if mode == "mock":
            events.extend(self._mock_events(limit))
        elif mode in {"free", "gdelt", "rss"}:
            if mode in {"free", "gdelt"}:
                for query in (queries or self.DEFAULT_QUERIES):
                    try:
                        events.extend(self._fetch_gdelt(query, max_per_source, timespan))
                    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                        warnings.append(f"GDELT query failed: {query} ({exc})")
            if mode in {"free", "rss"}:
                sources = self._rss_sources(rss_urls)
                for source in sources:
                    try:
                        events.extend(self._fetch_rss(source, max_per_source))
                    except (OSError, TimeoutError, ET.ParseError, ValueError) as exc:
                        warnings.append(f"RSS source failed: {source.name} ({exc})")
        else:
            raise ValueError("mode must be mock, free, gdelt, or rss")

        ranked = self._rank_events(self._dedupe(events))[:limit]
        return {
            "mode": mode,
            "budget": self.config(),
            "count": len(ranked),
            "warnings": warnings,
            "themes": self._theme_counts(ranked),
            "events": ranked,
        }

    def dashboard_summary(self, recent_events: list[dict[str, Any]]) -> dict[str, Any]:
        radar_events = [
            event for event in recent_events if event.get("metadata", {}).get("radar")
        ]
        return {
            "budget": self.config(),
            "recent_events": radar_events[:12],
            "themes": self._theme_counts_from_payloads(radar_events),
            "source_count": len(self.DEFAULT_RSS_SOURCES) + 1,
        }

    def _fetch_gdelt(self, query: str, limit: int, timespan: str) -> list[NewsEvent]:
        params = {
            "query": query,
            "mode": "artlist",
            "maxrecords": str(limit),
            "timespan": timespan,
            "sort": "datedesc",
            "format": "json",
        }
        url = f"https://api.gdeltproject.org/api/v2/doc/doc?{urllib.parse.urlencode(params)}"
        data = self._read_json(url)
        events: list[NewsEvent] = []
        for article in data.get("articles", [])[:limit]:
            headline = str(article.get("title") or "").strip()
            if not headline:
                continue
            source = str(article.get("domain") or "gdelt").strip()
            published_at = self._parse_gdelt_datetime(article.get("seendate"))
            summary = str(article.get("sourceCountry") or "Global headline via GDELT.")
            events.append(
                self._build_event(
                    headline=headline,
                    summary=summary,
                    source=source,
                    url=article.get("url"),
                    published_at=published_at,
                    source_reliability=0.78,
                    source_kind="gdelt",
                )
            )
        return events

    def _fetch_rss(self, source: RadarSource, limit: int) -> list[NewsEvent]:
        request = urllib.request.Request(source.url, headers={"User-Agent": "WatchtowerNewsRadar/0.1"})
        with urllib.request.urlopen(request, timeout=10) as response:
            raw_xml = response.read()

        root = ET.fromstring(raw_xml)
        items = root.findall(".//item")
        atom_items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        nodes = items or atom_items

        events: list[NewsEvent] = []
        for item in nodes[:limit]:
            title = self._xml_text(item, "title") or self._xml_text(item, "{http://www.w3.org/2005/Atom}title")
            summary = (
                self._xml_text(item, "description")
                or self._xml_text(item, "summary")
                or self._xml_text(item, "{http://www.w3.org/2005/Atom}summary")
            )
            link = self._xml_text(item, "link")
            if not link:
                link_node = item.find("{http://www.w3.org/2005/Atom}link")
                link = link_node.attrib.get("href", "") if link_node is not None else ""
            published_at = self._parse_rss_datetime(
                self._xml_text(item, "pubDate")
                or self._xml_text(item, "published")
                or self._xml_text(item, "{http://www.w3.org/2005/Atom}published")
                or self._xml_text(item, "{http://www.w3.org/2005/Atom}updated")
            )
            if title:
                events.append(
                    self._build_event(
                        headline=title,
                        summary=summary,
                        source=source.name,
                        url=link or None,
                        published_at=published_at,
                        source_reliability=source.reliability,
                        source_kind=source.kind,
                    )
                )
        return events

    def _build_event(
        self,
        headline: str,
        summary: str,
        source: str,
        url: str | None,
        published_at: datetime,
        source_reliability: float,
        source_kind: str,
    ) -> NewsEvent:
        text = f"{headline} {summary}"
        asset_info = self.asset_universe.resolve(text)
        themes = self._themes(text)
        urgency = self._urgency(published_at)
        relevance = self._relevance(text, themes, bool(asset_info))
        sentiment = self._sentiment(text)
        impact_score = round(
            min(
                1.0,
                (relevance * 0.36)
                + (source_reliability * 0.26)
                + (urgency * 0.22)
                + (min(len(themes), 4) / 4 * 0.16),
            ),
            4,
        )
        asset = asset_info["symbol"] if asset_info else self._macro_asset(themes)
        metadata = {
            "radar": {
                "impact_score": impact_score,
                "impact": self._impact_label(impact_score),
                "urgency": urgency,
                "themes": themes,
                "source_kind": source_kind,
                "source_reliability": source_reliability,
                "matched_asset": asset_info,
            }
        }
        return NewsEvent(
            id=f"evt_{uuid4().hex[:12]}",
            asset=asset,
            headline=headline,
            summary=summary,
            source=source,
            url=url,
            published_at=published_at,
            sentiment=sentiment,
            novelty=0.62,
            relevance=relevance,
            tags=["news_radar", *themes],
            metadata=metadata,
        )

    def _mock_events(self, limit: int) -> list[NewsEvent]:
        now = utc_now()
        examples = [
            (
                "Fed officials signal rates may stay higher as inflation risk persists",
                "Bond yields rise while traders reassess growth and technology valuations.",
                "mock-radar",
                0.9,
            ),
            (
                "Copper jumps as data center and grid demand forecasts improve",
                "Industrial metals strengthen on electrification and AI infrastructure demand.",
                "mock-radar",
                0.86,
            ),
            (
                "South African resource shares watch gold and oil volatility",
                "JSE-linked miners react to commodity price swings and currency pressure.",
                "mock-radar",
                0.82,
            ),
            (
                "Taiwan semiconductor supply chain gains on stronger AI chip orders",
                "Asia technology shares track improved demand for advanced chips.",
                "mock-radar",
                0.84,
            ),
        ]
        events = []
        for headline, summary, source, reliability in examples[:limit]:
            events.append(
                self._build_event(
                    headline=headline,
                    summary=summary,
                    source=source,
                    url=None,
                    published_at=now,
                    source_reliability=reliability,
                    source_kind="mock",
                )
            )
        return events

    def _rss_sources(self, rss_urls: list[str] | None) -> list[RadarSource]:
        if not rss_urls:
            return list(self.DEFAULT_RSS_SOURCES)
        return [
            RadarSource(
                name=urllib.parse.urlparse(url).netloc or "custom-rss",
                kind="custom_rss",
                url=url,
                cost_eur_month=0.0,
                reliability=0.7,
                notes="User supplied RSS source.",
            )
            for url in rss_urls
        ]

    def _read_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"User-Agent": "WatchtowerNewsRadar/0.1"})
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))

    def _rank_events(self, events: list[NewsEvent]) -> list[NewsEvent]:
        return sorted(
            events,
            key=lambda event: (
                event.metadata.get("radar", {}).get("impact_score", 0.0),
                event.relevance,
                event.published_at,
            ),
            reverse=True,
        )

    def _dedupe(self, events: list[NewsEvent]) -> list[NewsEvent]:
        seen: set[str] = set()
        deduped: list[NewsEvent] = []
        for event in events:
            key = self._dedupe_key(event)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(event)
        return deduped

    def _dedupe_key(self, event: NewsEvent) -> str:
        if event.url:
            return event.url.lower().strip()
        return re.sub(r"[^a-z0-9]+", "", event.headline.lower())[:90]

    def _themes(self, text: str) -> list[str]:
        normalized = text.lower()
        themes = []
        for theme, keywords in self.THEME_KEYWORDS.items():
            if any(keyword in normalized for keyword in keywords):
                themes.append(theme)
        return sorted(set(themes))

    def _sentiment(self, text: str) -> float:
        base = naive_sentiment(text)
        words = set(re.findall(r"[a-z]+", text.lower()))
        positive = len(words & self.POSITIVE_CONTEXT)
        negative = len(words & self.NEGATIVE_CONTEXT)
        return round(max(-1.0, min(1.0, base + ((positive - negative) * 0.12))), 4)

    def _relevance(self, text: str, themes: list[str], has_asset: bool) -> float:
        score = 0.38 + (0.08 * min(len(themes), 4))
        if has_asset:
            score += 0.2
        if any(word in text.lower() for word in ["market", "shares", "stocks", "futures", "yields"]):
            score += 0.1
        return round(min(1.0, score), 4)

    def _urgency(self, published_at: datetime) -> float:
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        age_seconds = max(0.0, (utc_now() - published_at.astimezone(timezone.utc)).total_seconds())
        if age_seconds <= 2 * 60 * 60:
            return 1.0
        if age_seconds <= 12 * 60 * 60:
            return 0.82
        if age_seconds <= 36 * 60 * 60:
            return 0.68
        return 0.45

    def _impact_label(self, impact_score: float) -> str:
        if impact_score >= 0.78:
            return "high"
        if impact_score >= 0.62:
            return "medium"
        return "low"

    def _macro_asset(self, themes: list[str]) -> str:
        if "energy" in themes:
            return "WTI"
        if "metals" in themes:
            return "COPPER"
        if "technology" in themes:
            return "NVDA"
        if "rates" in themes or "inflation" in themes:
            return "GLOBAL"
        return "GLOBAL"

    def _theme_counts(self, events: list[NewsEvent]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in events:
            for theme in event.metadata.get("radar", {}).get("themes", []):
                counts[theme] = counts.get(theme, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def _theme_counts_from_payloads(self, events: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in events:
            for theme in event.get("metadata", {}).get("radar", {}).get("themes", []):
                counts[theme] = counts.get(theme, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def _parse_rss_datetime(self, value: str | None) -> datetime:
        if not value:
            return utc_now()
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        except (TypeError, ValueError):
            return utc_now()

    def _parse_gdelt_datetime(self, value: Any) -> datetime:
        text = str(value or "").strip()
        if not text:
            return utc_now()
        for fmt in ("%Y%m%d%H%M%S", "%Y%m%dT%H%M%SZ"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return utc_now()

    def _xml_text(self, item: ET.Element, name: str) -> str:
        node = item.find(name)
        if node is None or node.text is None:
            return ""
        return node.text.strip()
