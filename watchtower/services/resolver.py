from __future__ import annotations

import re


class AssetResolver:
    """Small alias resolver for the MVP. Real entity resolution can replace this."""

    DEFAULT_ALIASES = {
        "APPLE": "AAPL",
        "APPLE INC": "AAPL",
        "MICROSOFT": "MSFT",
        "NVIDIA": "NVDA",
        "TESLA": "TSLA",
        "AMAZON": "AMZN",
        "ALPHABET": "GOOGL",
        "GOOGLE": "GOOGL",
        "META": "META",
        "BITCOIN": "BTC",
        "BTC": "BTC",
        "ETHEREUM": "ETH",
        "ETHER": "ETH",
        "ETH": "ETH",
        "SOLANA": "SOL",
        "SOL": "SOL",
    }

    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        self.aliases = {**self.DEFAULT_ALIASES, **(aliases or {})}

    def resolve(self, raw_asset: str | None, text: str = "") -> str:
        if raw_asset:
            candidate = raw_asset.strip().upper()
            return self.aliases.get(candidate, candidate)

        text_upper = text.upper()
        for name, ticker in self.aliases.items():
            if re.search(rf"\b{re.escape(name)}\b", text_upper):
                return ticker

        return "UNKNOWN"
