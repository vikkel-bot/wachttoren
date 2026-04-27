from __future__ import annotations


class ProviderRegistry:
    def list(self) -> list[dict]:
        return [
            {
                "name": "mock-regional",
                "types": ["news", "market"],
                "status": "implemented",
                "requires": [],
                "best_for": ["local development", "tests", "pipeline validation"],
            },
            {
                "name": "rss",
                "types": ["news"],
                "status": "implemented",
                "requires": ["feed_url"],
                "best_for": ["public newsfeeds", "regional source experiments"],
            },
            {
                "name": "finnhub",
                "types": ["news", "quotes", "earnings"],
                "status": "planned",
                "requires": ["FINNHUB_API_KEY"],
                "best_for": ["equities news", "earnings calendar", "global symbols"],
            },
            {
                "name": "polygon",
                "types": ["quotes", "bars", "news"],
                "status": "planned",
                "requires": ["POLYGON_API_KEY"],
                "best_for": ["US equities intraday", "Nasdaq/NYSE entry timing"],
            },
            {
                "name": "twelve-data",
                "types": ["quotes", "bars"],
                "status": "planned",
                "requires": ["TWELVE_DATA_API_KEY"],
                "best_for": ["broad international equity coverage"],
            },
            {
                "name": "eodhd",
                "types": ["quotes", "bars", "fundamentals"],
                "status": "planned",
                "requires": ["EODHD_API_KEY"],
                "best_for": ["delayed global exchange coverage"],
            },
        ]
