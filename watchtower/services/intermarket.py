from __future__ import annotations

from typing import Any

from watchtower.domain import MarketSnapshot
from watchtower.services.exchanges import Exchange


class IntermarketEngine:
    CRYPTO_LINKS = {
        "BTC": {
            "drivers": ["btc_vs_qqq_risk_beta", "btc_vs_dxy_usd_liquidity", "crypto_24_7_lead_lag"],
            "linked_markets": ["Crypto", "NASDAQ", "FX"],
            "linked_assets": ["NASDAQ:QQQ", "FX:DXY", "BITVAVO:ETH-BTC", "BITVAVO:ETH-EUR"],
            "thesis": "BTC is treated as a 24/7 risk bridge: compare it with QQQ for growth appetite and DXY for USD liquidity pressure.",
        },
        "ETH": {
            "drivers": ["eth_btc_rotation", "btc_beta", "crypto_liquidity", "risk_appetite"],
            "linked_markets": ["Crypto", "NASDAQ", "FX"],
            "linked_assets": ["BITVAVO:BTC-EUR", "BITVAVO:ETH-BTC", "NASDAQ:QQQ", "FX:DXY"],
            "thesis": "ETH needs confirmation from BTC trend, ETH/BTC rotation and broader risk appetite before conviction rises.",
        },
        "ETH-BTC": {
            "drivers": ["eth_btc_ratio", "smart_contract_rotation", "crypto_relative_strength"],
            "linked_markets": ["Crypto", "NASDAQ"],
            "linked_assets": ["BITVAVO:BTC-EUR", "BITVAVO:ETH-EUR", "NASDAQ:QQQ"],
            "thesis": "ETH/BTC is a relative-strength ratio that helps distinguish broad crypto beta from ETH-specific rotation.",
        },
    }

    COMMODITY_LINKS = {
        "GOLD": {
            "drivers": ["real_rates", "usd_strength", "risk_off_flows"],
            "linked_markets": ["NASDAQ", "AEX", "JSE"],
            "linked_assets": ["COMEX:SILVER", "JSE:NPN"],
            "thesis": "Gold often acts as a risk and rates signal for equities and resource markets.",
        },
        "SILVER": {
            "drivers": ["industrial_demand", "solar_electronics", "ai_hardware", "usd_strength"],
            "linked_markets": ["NASDAQ", "TWSE", "KRX", "AEX"],
            "linked_assets": ["COMEX:GOLD", "TWSE:2330", "KRX:005930", "AEX:ASML"],
            "thesis": "Silver links monetary metals with electronics and solar demand.",
        },
        "COPPER": {
            "drivers": ["industrial_growth", "electrification", "data_centers", "china_demand"],
            "linked_markets": ["SSE", "SZSE", "TWSE", "KRX", "AEX", "JSE"],
            "linked_assets": ["TWSE:2330", "KRX:005930", "AEX:ASML", "JSE:PRX"],
            "thesis": "Copper is a broad read-through for industrial growth, electrification and semiconductor supply chains.",
        },
        "ALUMINIUM": {
            "drivers": ["lightweighting", "grid_infrastructure", "industrial_power_costs"],
            "linked_markets": ["AEX", "SSE", "SZSE", "JSE"],
            "linked_assets": ["LME:COPPER", "SHFE:CU", "NASDAQ:TSLA"],
            "thesis": "Aluminium connects lightweight manufacturing, grid build-outs and power-cost pressure.",
        },
        "CU": {
            "drivers": ["china_demand", "industrial_growth", "electrification"],
            "linked_markets": ["SSE", "SZSE", "HKEX", "JSE"],
            "linked_assets": ["SHFE:CU", "SSE:600519", "JSE:PRX"],
            "thesis": "Shanghai copper is a local China-demand read-through for Asia and resource-sensitive markets.",
        },
        "WTI": {
            "drivers": ["oil_supply", "inflation_expectations", "energy_demand"],
            "linked_markets": ["NYSE", "NASDAQ", "NSE_IN", "JSE"],
            "linked_assets": ["NYSE:XOM", "NSE_IN:RELIANCE", "ICE:BRENT"],
            "thesis": "WTI can support energy equities while pressuring rate-sensitive growth sectors through inflation.",
        },
        "BRENT": {
            "drivers": ["global_energy_demand", "inflation_expectations", "geopolitics"],
            "linked_markets": ["AEX", "NSE_IN", "JSE", "EGX"],
            "linked_assets": ["NYMEX:WTI", "NSE_IN:RELIANCE"],
            "thesis": "Brent is a global oil benchmark with spillover into inflation, transport and energy equities.",
        },
        "NATGAS": {
            "drivers": ["weather", "lng_demand", "power_prices"],
            "linked_markets": ["NYSE", "AEX", "NSE_IN"],
            "linked_assets": ["NYMEX:WTI", "ICE:BRENT"],
            "thesis": "Natural gas links weather, LNG demand and power-cost pressure to energy and industrial sectors.",
        },
        "RB": {
            "drivers": ["china_construction", "industrial_demand", "infrastructure_cycle"],
            "linked_markets": ["SSE", "SZSE", "HKEX", "JSE"],
            "linked_assets": ["SHFE:CU", "LME:COPPER", "NGX:DANGCEM"],
            "thesis": "Steel rebar is a China construction and infrastructure-cycle signal.",
        },
        "COCOA": {
            "drivers": ["weather", "supply_shock", "consumer_margin_pressure"],
            "linked_markets": ["AEX", "JSE", "NGX"],
            "linked_assets": ["ICE:BRENT"],
            "thesis": "Cocoa can flag supply shocks and margin pressure for consumer staples.",
        },
    }

    SECTOR_LINKS = {
        "Technology": {
            "drivers": ["semiconductor_cycle", "rates", "ai_capex"],
            "linked_markets": ["NASDAQ", "AEX", "TWSE", "KRX"],
            "linked_assets": ["COMEX:SILVER", "LME:COPPER", "TWSE:2330", "KRX:005930"],
            "thesis": "Technology entries should be cross-checked against rates, semiconductors and industrial metals.",
        },
        "Semiconductors": {
            "drivers": ["ai_capex", "chip_cycle", "electronics_metals"],
            "linked_markets": ["NASDAQ", "AEX", "TWSE", "KRX"],
            "linked_assets": ["LME:COPPER", "COMEX:SILVER", "AEX:ASML", "TWSE:2330", "KRX:005930"],
            "thesis": "Semiconductors connect directly to AI capex, electronics demand and metal inputs.",
        },
        "Energy": {
            "drivers": ["oil_prices", "gas_prices", "inflation"],
            "linked_markets": ["NYSE", "NSE_IN", "JSE", "EGX"],
            "linked_assets": ["NYMEX:WTI", "ICE:BRENT", "NYMEX:NATGAS"],
            "thesis": "Energy equities need oil/gas confirmation and inflation awareness.",
        },
        "Materials": {
            "drivers": ["industrial_metals", "china_demand", "construction_cycle"],
            "linked_markets": ["SSE", "SZSE", "JSE", "NGX"],
            "linked_assets": ["LME:COPPER", "SHFE:CU", "SHFE:RB"],
            "thesis": "Materials often follow industrial metals and China demand expectations.",
        },
        "Financials": {
            "drivers": ["rates", "yield_curve", "credit_risk"],
            "linked_markets": ["NASDAQ", "AEX", "JSE", "EGX"],
            "linked_assets": ["COMEX:GOLD"],
            "thesis": "Financials are sensitive to rates, credit risk and risk-off flows.",
        },
        "Automotive": {
            "drivers": ["electrification", "battery_supply_chain", "industrial_metals"],
            "linked_markets": ["NASDAQ", "JPX", "SSE", "SZSE"],
            "linked_assets": ["LME:COPPER", "LME:ALUMINIUM", "NASDAQ:TSLA"],
            "thesis": "Automotive signals should be checked against EV demand and industrial metal inputs.",
        },
    }

    REGION_LINKS = {
        "North America": ["Asia overnight lead", "USD and rates affect global risk appetite"],
        "Europe": ["US futures and energy prices often influence European open", "AEX technology links to semiconductors"],
        "Asia": ["US close often affects Asia open", "China demand affects commodities and resource markets"],
        "Africa": ["Commodity prices and currency pressure affect liquidity-sensitive markets"],
        "Commodities": ["Commodity moves transmit into inflation, sector rotation and resource equities"],
        "Crypto": ["Crypto trades continuously and can lead risk appetite before equity sessions open"],
    }

    def context(self, asset_info: dict[str, Any] | None, exchange: Exchange, market: MarketSnapshot) -> dict[str, Any]:
        symbol = (asset_info or {}).get("symbol", market.asset).upper()
        asset_class = (asset_info or {}).get("asset_class", "equity")
        crypto_symbol = self._crypto_symbol(symbol)
        if asset_class == "crypto" or exchange.market_type == "crypto" or crypto_symbol in self.CRYPTO_LINKS:
            asset_class = "crypto"
        sector = (asset_info or {}).get("sector", "")
        commodity = self.COMMODITY_LINKS.get(symbol)
        crypto = self.CRYPTO_LINKS.get(crypto_symbol)
        sector_link = self.SECTOR_LINKS.get(sector, {})

        drivers: list[str] = []
        linked_assets: list[str] = []
        linked_markets: list[str] = []
        theses: list[str] = []
        flags: list[str] = []

        if commodity:
            drivers.extend(commodity["drivers"])
            linked_assets.extend(commodity["linked_assets"])
            linked_markets.extend(commodity["linked_markets"])
            theses.append(commodity["thesis"])
            flags.append("commodity_cross_market_driver")

        if crypto:
            drivers.extend(crypto["drivers"])
            linked_assets.extend(crypto["linked_assets"])
            linked_markets.extend(crypto["linked_markets"])
            theses.append(crypto["thesis"])
            flags.append("crypto_cross_field_driver")
            if market.volatility_zscore >= 2.0:
                flags.append("crypto_volatility_elevated")
            if market.trend_1d < -0.35:
                flags.append("crypto_risk_appetite_weak")

        if sector_link:
            drivers.extend(sector_link["drivers"])
            linked_assets.extend(sector_link["linked_assets"])
            linked_markets.extend(sector_link.get("linked_markets", []))
            theses.append(sector_link["thesis"])
            flags.append("sector_intermarket_link")

        drivers.extend(self.REGION_LINKS.get(exchange.region, []))
        if exchange.region == "Africa":
            linked_assets.extend(["LME:COPPER", "COMEX:GOLD", "ICE:BRENT"])
        if exchange.region == "Asia":
            linked_assets.extend(["NASDAQ:NVDA", "NASDAQ:MSFT", "SHFE:CU"])
        if exchange.code == "AEX":
            linked_assets.extend(["NASDAQ:NVDA", "TWSE:2330", "KRX:005930", "LME:COPPER"])

        adjustment = self._adjustment(asset_class, sector, exchange, market)
        if adjustment > 0:
            flags.append("intermarket_tailwind")
        elif adjustment < 0:
            flags.append("intermarket_headwind")

        return {
            "asset_class": asset_class,
            "primary_sector": sector or None,
            "drivers": sorted(set(drivers)),
            "linked_assets": sorted(set(linked_assets)),
            "linked_markets": sorted(set(linked_markets)),
            "thesis": " ".join(theses) if theses else "No strong intermarket thesis yet.",
            "score_adjustment": adjustment,
            "flags": sorted(set(flags)),
        }

    def dashboard_links(self) -> list[dict[str, Any]]:
        return [
            {
                "theme": "Tech and AI supply chain",
                "drivers": ["COPPER", "SILVER", "semiconductors", "AI capex"],
                "linked": ["NASDAQ:NVDA", "AEX:ASML", "TWSE:2330", "KRX:005930"],
                "effect": "Stronger metals and chip momentum can confirm tech entries; rising rates can weaken them.",
            },
            {
                "theme": "Energy and inflation",
                "drivers": ["WTI", "BRENT", "NATGAS"],
                "linked": ["NYSE:XOM", "NSE_IN:RELIANCE", "global rates pressure"],
                "effect": "Energy strength can help energy shares, but persistent inflation can hurt long-duration growth.",
            },
            {
                "theme": "Risk-off and resource markets",
                "drivers": ["GOLD", "USD", "real rates"],
                "linked": ["JSE", "EGX", "NGX", "equity risk appetite"],
                "effect": "Gold/risk-off strength can reduce equity conviction and lift defensive or resource-sensitive signals.",
            },
            {
                "theme": "China demand",
                "drivers": ["SHFE:CU", "SSE", "HKEX"],
                "linked": ["LME:COPPER", "JSE", "Asia cyclicals"],
                "effect": "China demand can pull industrial metals, Asian cyclicals and African resource markets in the same direction.",
            },
            {
                "theme": "Electrification and infrastructure",
                "drivers": ["ALUMINIUM", "COPPER", "steel rebar", "grid build-outs"],
                "linked": ["NASDAQ:TSLA", "JPX:7203", "SSE", "SZSE", "JSE"],
                "effect": "Infrastructure and electrification demand can support materials, automotives and selected tech supply chains.",
            },
            {
                "theme": "Crypto risk bridge",
                "drivers": ["BTC vs QQQ", "BTC vs DXY", "ETH/BTC ratio"],
                "linked": ["BITVAVO:BTC-EUR", "BITVAVO:ETH-BTC", "NASDAQ:QQQ", "FX:DXY"],
                "effect": "Crypto momentum can lead risk appetite outside equity hours, while USD strength and ETH/BTC rotation can filter entry quality.",
            },
        ]

    def _adjustment(self, asset_class: str, sector: str, exchange: Exchange, market: MarketSnapshot) -> float:
        adjustment = 0.0
        asset_class = asset_class.lower()
        if asset_class == "commodity" and market.volume_zscore >= 1.8:
            adjustment += 0.02
        if asset_class == "crypto" and market.volume_zscore >= 1.5 and market.trend_1d >= 0.25:
            adjustment += 0.02
        if asset_class == "crypto" and market.volatility_zscore >= 2.2:
            adjustment -= 0.02
        if sector in {"Technology", "Semiconductors"} and market.volume_zscore >= 1.8:
            adjustment += 0.015
        if sector == "Energy" and market.volatility_zscore >= 1.7:
            adjustment -= 0.015
        if exchange.region == "Africa" and market.volatility_zscore >= 1.5:
            adjustment -= 0.02
        return round(adjustment, 4)

    def _crypto_symbol(self, symbol: str) -> str:
        normalized = symbol.upper().replace("/", "-").replace("_", "-")
        if normalized == "ETH-BTC":
            return "ETH-BTC"
        if normalized.startswith("BTC"):
            return "BTC"
        if normalized.startswith("ETH"):
            return "ETH"
        return normalized
