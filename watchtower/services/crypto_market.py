from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Any

from watchtower.domain import MarketSnapshot


class CryptoMarketDataError(ValueError):
    """Raised when a public crypto market-data request cannot be normalized."""


@dataclass(frozen=True, slots=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class CryptoMarketAdapter:
    """Public crypto market-data adapter for Watchtower market snapshots."""

    BITVAVO_BASE_URL = "https://api.bitvavo.com/v2"
    DEFAULT_MARKETS = {
        "BTC": "BTC-EUR",
        "BITCOIN": "BTC-EUR",
        "ETH": "ETH-EUR",
        "ETHER": "ETH-EUR",
        "ETHEREUM": "ETH-EUR",
        "SOL": "SOL-EUR",
        "SOLANA": "SOL-EUR",
        "BTC-EUR": "BTC-EUR",
        "ETH-EUR": "ETH-EUR",
        "SOL-EUR": "SOL-EUR",
        "ETH-BTC": "ETH-BTC",
    }
    SYNTHETIC_MARKETS = {
        "ETH-BTC": ("ETH-EUR", "BTC-EUR"),
    }

    def __init__(
        self,
        fetch_json: Callable[[str], Any] | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        self._fetch_json = fetch_json or self._urlopen_json
        self.timeout_seconds = timeout_seconds

    def fetch_snapshot(self, asset: str, quote: str = "EUR") -> MarketSnapshot:
        market = self.market_code(asset, quote=quote)
        if market in self.SYNTHETIC_MARKETS:
            return self._fetch_synthetic_ratio(market)

        ticker = self._request_json(
            f"{self.BITVAVO_BASE_URL}/ticker/price?market={urllib.parse.quote(market)}"
        )
        candles_raw = self._request_json(
            f"{self.BITVAVO_BASE_URL}/{urllib.parse.quote(market)}/candles?interval=1h&limit=30"
        )

        candles = self._parse_candles(candles_raw)
        price = self._price_from_ticker(ticker) or (candles[-1].close if candles else None)
        if price is None or price <= 0:
            raise CryptoMarketDataError(f"Missing usable price for {market}")

        return self._snapshot_from_candles(market, price, candles)

    def _fetch_synthetic_ratio(self, market: str) -> MarketSnapshot:
        base_market, quote_market = self.SYNTHETIC_MARKETS[market]
        base_ticker = self._request_json(
            f"{self.BITVAVO_BASE_URL}/ticker/price?market={urllib.parse.quote(base_market)}"
        )
        quote_ticker = self._request_json(
            f"{self.BITVAVO_BASE_URL}/ticker/price?market={urllib.parse.quote(quote_market)}"
        )
        base_candles = self._parse_candles(
            self._request_json(f"{self.BITVAVO_BASE_URL}/{urllib.parse.quote(base_market)}/candles?interval=1h&limit=30")
        )
        quote_candles = self._parse_candles(
            self._request_json(f"{self.BITVAVO_BASE_URL}/{urllib.parse.quote(quote_market)}/candles?interval=1h&limit=30")
        )

        base_price = self._price_from_ticker(base_ticker)
        quote_price = self._price_from_ticker(quote_ticker)
        ratio_candles = self._ratio_candles(base_candles, quote_candles)
        if base_price and quote_price and quote_price > 0:
            price = base_price / quote_price
        elif ratio_candles:
            price = ratio_candles[-1].close
        else:
            raise CryptoMarketDataError(f"Missing usable synthetic ratio data for {market}")

        return self._snapshot_from_candles(market, price, ratio_candles)

    def market_code(self, asset: str, quote: str = "EUR") -> str:
        normalized = asset.strip().upper().replace("/", "-").replace("_", "-")
        if not normalized:
            raise CryptoMarketDataError("asset is required")
        if normalized in self.DEFAULT_MARKETS:
            return self.DEFAULT_MARKETS[normalized]
        if "-" in normalized:
            return normalized
        return f"{normalized}-{quote.upper()}"

    def _request_json(self, url: str) -> Any:
        try:
            return self._fetch_json(url)
        except CryptoMarketDataError:
            raise
        except Exception as exc:  # pragma: no cover - network failures are environment-specific
            raise CryptoMarketDataError(f"Crypto market-data request failed: {exc}") from exc

    def _urlopen_json(self, url: str) -> Any:
        request = urllib.request.Request(url, headers={"User-Agent": "watchtower/0.1"})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def _parse_candles(self, payload: Any) -> list[Candle]:
        if not isinstance(payload, list):
            raise CryptoMarketDataError("Crypto candle response must be a list")

        candles: list[Candle] = []
        for row in payload:
            try:
                ts, open_, high, low, close, volume = row[:6]
                candles.append(
                    Candle(
                        timestamp=int(ts),
                        open=float(open_),
                        high=float(high),
                        low=float(low),
                        close=float(close),
                        volume=float(volume),
                    )
                )
            except (TypeError, ValueError, IndexError) as exc:
                raise CryptoMarketDataError("Crypto candle response contains an invalid row") from exc

        candles = [candle for candle in candles if candle.close > 0 and candle.high > 0 and candle.low > 0]
        return sorted(candles, key=lambda candle: candle.timestamp)

    def _price_from_ticker(self, payload: Any) -> float | None:
        if isinstance(payload, dict):
            value = payload.get("price")
        elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
            value = payload[0].get("price")
        else:
            value = None
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return None

    def _ratio_candles(self, base_candles: list[Candle], quote_candles: list[Candle]) -> list[Candle]:
        base_by_ts = {candle.timestamp: candle for candle in base_candles}
        quote_by_ts = {candle.timestamp: candle for candle in quote_candles}
        candles: list[Candle] = []
        for timestamp in sorted(base_by_ts.keys() & quote_by_ts.keys()):
            base = base_by_ts[timestamp]
            quote = quote_by_ts[timestamp]
            if min(quote.open, quote.high, quote.low, quote.close) <= 0:
                continue
            candles.append(
                Candle(
                    timestamp=timestamp,
                    open=base.open / quote.open,
                    high=base.high / quote.low,
                    low=base.low / quote.high,
                    close=base.close / quote.close,
                    volume=base.volume,
                )
            )
        return candles

    def _snapshot_from_candles(self, market: str, price: float, candles: list[Candle]) -> MarketSnapshot:
        if len(candles) < 2:
            return MarketSnapshot(asset=market, price=price, benchmark_change_1d_pct=0.0)

        last = candles[-1]
        previous = candles[-2]
        day_anchor = candles[-25] if len(candles) >= 25 else candles[0]
        ranges = [abs(candle.high - candle.low) / candle.close for candle in candles if candle.close > 0]
        volumes = [candle.volume for candle in candles if candle.volume >= 0]

        change_1h = self._pct_change(last.close, previous.close)
        change_1d = self._pct_change(last.close, day_anchor.close)
        volatility_zscore = self._zscore(ranges[-1], ranges[:-1])
        volume_zscore = self._zscore(volumes[-1], volumes[:-1])

        return MarketSnapshot(
            asset=market,
            price=price,
            change_15m_pct=round(change_1h / 4.0, 4),
            change_1h_pct=round(change_1h, 4),
            change_1d_pct=round(change_1d, 4),
            volume_zscore=round(volume_zscore, 4),
            volatility_zscore=round(volatility_zscore, 4),
            trend_1h=round(self._trend(change_1h, scale=2.0), 4),
            trend_1d=round(self._trend(change_1d, scale=6.0), 4),
            sector_change_1d_pct=round(change_1d, 4),
            benchmark_change_1d_pct=round(change_1d, 4),
        )

    @staticmethod
    def _pct_change(value: float, anchor: float) -> float:
        if anchor <= 0:
            return 0.0
        return ((value - anchor) / anchor) * 100.0

    @staticmethod
    def _zscore(value: float, history: list[float]) -> float:
        history = [item for item in history if math.isfinite(item)]
        if len(history) < 2:
            return 0.0
        stdev = pstdev(history)
        if stdev == 0:
            return 0.0
        return (value - mean(history)) / stdev

    @staticmethod
    def _trend(change_pct: float, scale: float) -> float:
        return max(-1.0, min(1.0, change_pct / scale))
