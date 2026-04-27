from __future__ import annotations

import re
from dataclasses import dataclass

from watchtower.services.exchanges import ExchangeUniverse


@dataclass(frozen=True, slots=True)
class ListedAsset:
    symbol: str
    name: str
    exchange: str
    sector: str
    currency: str
    asset_class: str
    aliases: list[str]

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.symbol}"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "symbol": self.symbol,
            "name": self.name,
            "exchange": self.exchange,
            "sector": self.sector,
            "currency": self.currency,
            "asset_class": self.asset_class,
            "aliases": self.aliases,
        }


class ListedAssetUniverse:
    def __init__(self, exchanges: ExchangeUniverse) -> None:
        self.exchanges = exchanges
        self._assets = _seed_assets(exchanges)

    def list(
        self,
        exchange: str | None = None,
        region: str | None = None,
        query: str | None = None,
        asset_class: str | None = None,
    ) -> list[dict]:
        assets = self._assets
        if exchange:
            exchange_code = exchange.upper()
            assets = [asset for asset in assets if asset.exchange == exchange_code]
        if asset_class:
            class_normalized = asset_class.lower()
            assets = [asset for asset in assets if asset.asset_class.lower() == class_normalized]
        if region:
            region_lower = region.lower()
            assets = [
                asset
                for asset in assets
                if (self.exchanges.get(asset.exchange) and self.exchanges.get(asset.exchange).region.lower() == region_lower)
            ]
        if query:
            pattern = query.lower()
            assets = [
                asset
                for asset in assets
                if pattern in asset.symbol.lower()
                or pattern in asset.name.lower()
                or any(pattern in alias.lower() for alias in asset.aliases)
            ]
        return [asset.to_dict() for asset in sorted(assets, key=lambda item: (item.exchange, item.symbol))]

    def get(self, exchange: str, symbol: str) -> ListedAsset | None:
        key = f"{exchange.upper()}:{symbol.upper()}"
        for asset in self._assets:
            if asset.key == key:
                return asset
        return None

    def resolve(self, text: str, exchange: str | None = None) -> dict | None:
        candidates = self._assets
        if exchange:
            exchange_code = exchange.upper()
            candidates = [asset for asset in candidates if asset.exchange == exchange_code]

        text_upper = text.upper()
        best: tuple[int, ListedAsset] | None = None
        for asset in candidates:
            names = [asset.symbol, asset.name, *asset.aliases]
            for name in names:
                if not name:
                    continue
                if re.search(rf"\b{re.escape(name.upper())}\b", text_upper):
                    score = len(name)
                    if best is None or score > best[0]:
                        best = (score, asset)

        return best[1].to_dict() if best else None


def asset(
    symbol: str,
    name: str,
    exchange: str,
    sector: str,
    currency: str,
    aliases: list[str] | None = None,
    asset_class: str = "equity",
) -> ListedAsset:
    return ListedAsset(
        symbol=symbol.upper(),
        name=name,
        exchange=exchange.upper(),
        sector=sector,
        currency=currency,
        asset_class=asset_class,
        aliases=aliases or [],
    )


def _seed_assets(exchanges: ExchangeUniverse) -> list[ListedAsset]:
    return [
        asset("ASML", "ASML Holding", "AEX", "Technology", "EUR", ["ASML Holding NV"]),
        asset("ADYEN", "Adyen", "AEX", "Financial Technology", "EUR"),
        asset("INGA", "ING Group", "AEX", "Financials", "EUR", ["ING"]),
        asset("AAPL", "Apple", "NASDAQ", "Technology", "USD", ["Apple Inc"]),
        asset("MSFT", "Microsoft", "NASDAQ", "Technology", "USD", ["Microsoft Corporation"]),
        asset("NVDA", "NVIDIA", "NASDAQ", "Semiconductors", "USD", ["Nvidia Corporation"]),
        asset("TSLA", "Tesla", "NASDAQ", "Consumer Discretionary", "USD", ["Tesla Inc"]),
        asset("JPM", "JPMorgan Chase", "NYSE", "Financials", "USD", ["JPMorgan", "JP Morgan"]),
        asset("XOM", "Exxon Mobil", "NYSE", "Energy", "USD", ["Exxon"]),
        asset("7203", "Toyota Motor", "JPX", "Automotive", "JPY", ["Toyota"]),
        asset("6758", "Sony Group", "JPX", "Consumer Electronics", "JPY", ["Sony"]),
        asset("9988", "Alibaba Group", "HKEX", "Ecommerce", "HKD", ["Alibaba"]),
        asset("0700", "Tencent", "HKEX", "Technology", "HKD", ["Tencent Holdings"]),
        asset("600519", "Kweichow Moutai", "SSE", "Consumer Staples", "CNY", ["Moutai"]),
        asset("000001", "Ping An Bank", "SZSE", "Financials", "CNY", ["Ping An Bank"]),
        asset("RELIANCE", "Reliance Industries", "NSE_IN", "Energy", "INR", ["Reliance"]),
        asset("INFY", "Infosys", "NSE_IN", "Technology", "INR", ["Infosys Limited"]),
        asset("TCS", "Tata Consultancy Services", "BSE_IN", "Technology", "INR", ["Tata Consultancy"]),
        asset("D05", "DBS Group", "SGX", "Financials", "SGD", ["DBS"]),
        asset("005930", "Samsung Electronics", "KRX", "Technology", "KRW", ["Samsung"]),
        asset("2330", "Taiwan Semiconductor Manufacturing", "TWSE", "Semiconductors", "TWD", ["TSMC"]),
        asset("NPN", "Naspers", "JSE", "Technology", "ZAR", ["Naspers Limited"]),
        asset("PRX", "Prosus", "JSE", "Technology", "ZAR", ["Prosus NV"]),
        asset("COMI", "Commercial International Bank Egypt", "EGX", "Financials", "EGP", ["CIB Egypt"]),
        asset("DANGCEM", "Dangote Cement", "NGX", "Materials", "NGN", ["Dangote"]),
        asset("SCOM", "Safaricom", "NSE_KE", "Telecom", "KES", ["Safaricom PLC"]),
        asset("ATW", "Attijariwafa Bank", "CSE_MA", "Financials", "MAD", ["Attijariwafa"]),
        asset("GOLD", "Gold Futures", "COMEX", "Precious Metals", "USD", ["gold", "xau"], "commodity"),
        asset("SILVER", "Silver Futures", "COMEX", "Precious Metals", "USD", ["silver", "xag"], "commodity"),
        asset("WTI", "WTI Crude Oil", "NYMEX", "Energy", "USD", ["crude oil", "west texas intermediate"], "commodity"),
        asset("NATGAS", "Natural Gas", "NYMEX", "Energy", "USD", ["natural gas", "henry hub"], "commodity"),
        asset("BRENT", "Brent Crude Oil", "ICE", "Energy", "USD", ["brent oil"], "commodity"),
        asset("COCOA", "Cocoa Futures", "ICE", "Agriculture", "USD", ["cocoa"], "commodity"),
        asset("COPPER", "Copper", "LME", "Base Metals", "USD", ["lme copper"], "commodity"),
        asset("ALUMINIUM", "Aluminium", "LME", "Base Metals", "USD", ["lme aluminium", "aluminum"], "commodity"),
        asset("CU", "Shanghai Copper", "SHFE", "Base Metals", "CNY", ["shfe copper"], "commodity"),
        asset("RB", "Steel Rebar", "SHFE", "Industrial Metals", "CNY", ["rebar"], "commodity"),
    ]
