from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class SessionBlock:
    name: str
    start: str
    end: str
    is_trading: bool
    is_regular: bool = False
    accepts_orders: bool = False

    @property
    def start_time(self) -> time:
        return time.fromisoformat(self.start)

    @property
    def end_time(self) -> time:
        return time.fromisoformat(self.end)


@dataclass(frozen=True, slots=True)
class Exchange:
    code: str
    name: str
    mic: str
    country: str
    region: str
    timezone: str
    currency: str
    main_index: str
    languages: list[str]
    ticker_suffix: str = ""
    sessions: list[SessionBlock] = field(default_factory=list)
    holiday_dates: list[str] = field(default_factory=list)
    source_url: str = ""
    notes: str = ""
    market_type: str = "equity"

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "mic": self.mic,
            "country": self.country,
            "region": self.region,
            "timezone": self.timezone,
            "currency": self.currency,
            "main_index": self.main_index,
            "languages": self.languages,
            "ticker_suffix": self.ticker_suffix,
            "sessions": [
                {
                    "name": block.name,
                    "start": block.start,
                    "end": block.end,
                    "is_trading": block.is_trading,
                    "is_regular": block.is_regular,
                    "accepts_orders": block.accepts_orders,
                }
                for block in self.sessions
            ],
            "holiday_dates": self.holiday_dates,
            "source_url": self.source_url,
            "notes": self.notes,
            "market_type": self.market_type,
        }


class ExchangeUniverse:
    def __init__(self) -> None:
        self._exchanges = {exchange.code: exchange for exchange in _seed_exchanges()}

    def list(self, region: str | None = None) -> list[dict]:
        exchanges = sorted(self._exchanges.values(), key=lambda item: (item.region, item.code))
        if region:
            region_normalized = region.lower()
            exchanges = [exchange for exchange in exchanges if exchange.region.lower() == region_normalized]
        return [exchange.to_dict() for exchange in exchanges]

    def regions(self) -> list[str]:
        return sorted({exchange.region for exchange in self._exchanges.values()})

    def get(self, code: str) -> Exchange | None:
        return self._exchanges.get(code.upper())

    def require(self, code: str) -> Exchange:
        exchange = self.get(code)
        if not exchange:
            raise ValueError(f"Unknown exchange: {code}")
        return exchange

    def enrich_watchlist_item(self, item: dict) -> dict:
        payload = dict(item)
        exchange_code = payload.get("exchange", "GLOBAL").upper()
        payload["exchange"] = exchange_code
        payload["asset"] = payload["asset"].upper()
        exchange = self.get(exchange_code)
        if exchange:
            payload["region"] = payload.get("region") or exchange.region
            payload["currency"] = payload.get("currency") or exchange.currency
            payload["timezone"] = payload.get("timezone") or exchange.timezone
        else:
            payload["region"] = payload.get("region") or "global"
            payload["currency"] = payload.get("currency") or "USD"
            payload["timezone"] = payload.get("timezone") or "UTC"
        return payload


class TradingSessionDetector:
    def __init__(self, universe: ExchangeUniverse) -> None:
        self.universe = universe

    def status(self, code: str, at: datetime | None = None) -> dict:
        exchange = self.universe.require(code)
        tz = ZoneInfo(exchange.timezone)
        moment = at or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        local_dt = moment.astimezone(tz)
        local_date = local_dt.date()

        if exchange.market_type == "crypto":
            return self._status_payload(exchange, local_dt, True, True, True, "continuous", "crypto_24_7", None, None)

        closed_reason = self._closed_reason(exchange, local_date)
        if closed_reason:
            next_open = self._next_open(exchange, local_dt)
            return self._status_payload(exchange, local_dt, False, False, False, "closed", closed_reason, next_open, None)

        active_block = self._active_block(exchange, local_dt.time())
        if active_block:
            close_at = self._combine(local_date, active_block.end_time, tz)
            next_open = None if active_block.is_trading else self._next_open(exchange, local_dt)
            return self._status_payload(
                exchange=exchange,
                local_dt=local_dt,
                is_trading=active_block.is_trading,
                is_regular=active_block.is_regular,
                accepts_orders=active_block.accepts_orders,
                session=active_block.name,
                reason="inside_session",
                next_open_at=next_open,
                next_close_at=close_at if active_block.is_trading else None,
            )

        next_open = self._next_open(exchange, local_dt)
        return self._status_payload(exchange, local_dt, False, False, False, "closed", "outside_session_hours", next_open, None)

    def _closed_reason(self, exchange: Exchange, local_date: date) -> str | None:
        if exchange.market_type == "crypto":
            return None
        if local_date.weekday() >= 5:
            return "weekend"
        if local_date.isoformat() in exchange.holiday_dates:
            return "holiday"
        return None

    def _active_block(self, exchange: Exchange, local_time: time) -> SessionBlock | None:
        for block in exchange.sessions:
            if block.start_time <= local_time < block.end_time:
                return block
        return None

    def _next_open(self, exchange: Exchange, local_dt: datetime) -> datetime | None:
        tz = ZoneInfo(exchange.timezone)
        trading_blocks = [block for block in exchange.sessions if block.is_trading]
        if not trading_blocks:
            return None

        for day_offset in range(0, 15):
            candidate_date = local_dt.date() + timedelta(days=day_offset)
            if self._closed_reason(exchange, candidate_date):
                continue
            for block in trading_blocks:
                candidate = self._combine(candidate_date, block.start_time, tz)
                if candidate > local_dt:
                    return candidate
        return None

    def _combine(self, local_date: date, local_time: time, tz: ZoneInfo) -> datetime:
        return datetime.combine(local_date, local_time, tzinfo=tz)

    def _status_payload(
        self,
        exchange: Exchange,
        local_dt: datetime,
        is_trading: bool,
        is_regular: bool,
        accepts_orders: bool,
        session: str,
        reason: str,
        next_open_at: datetime | None,
        next_close_at: datetime | None,
    ) -> dict:
        return {
            "exchange": exchange.code,
            "name": exchange.name,
            "region": exchange.region,
            "timezone": exchange.timezone,
            "local_time": local_dt.isoformat(),
            "session": session,
            "reason": reason,
            "is_trading": is_trading,
            "is_regular": is_regular,
            "accepts_orders": accepts_orders,
            "next_open_at": next_open_at.isoformat() if next_open_at else None,
            "next_close_at": next_close_at.isoformat() if next_close_at else None,
        }


def block(name: str, start: str, end: str, is_trading: bool, is_regular: bool = False, accepts_orders: bool = False) -> SessionBlock:
    return SessionBlock(name=name, start=start, end=end, is_trading=is_trading, is_regular=is_regular, accepts_orders=accepts_orders)


def _seed_exchanges() -> list[Exchange]:
    return [
        Exchange(
            code="AEX",
            name="Euronext Amsterdam",
            mic="XAMS",
            country="Netherlands",
            region="Europe",
            timezone="Europe/Amsterdam",
            currency="EUR",
            main_index="AEX",
            languages=["nl", "en"],
            ticker_suffix=".AS",
            sessions=[block("regular", "09:00", "17:30", True, True, True)],
            source_url="https://www.euronext.com/en/trading-calendars-hours",
        ),
        Exchange(
            code="BITVAVO",
            name="Bitvavo",
            mic="BITVAVO",
            country="Netherlands",
            region="Crypto",
            timezone="Europe/Amsterdam",
            currency="EUR",
            main_index="BTC-EUR",
            languages=["nl", "en"],
            sessions=[block("continuous", "00:00", "23:59", True, True, True)],
            source_url="https://api.bitvavo.com/v2",
            notes="Crypto venue seeded as continuous 24/7 market data; execution remains outside Watchtower.",
            market_type="crypto",
        ),
        Exchange(
            code="NASDAQ",
            name="Nasdaq Stock Market",
            mic="XNAS",
            country="United States",
            region="North America",
            timezone="America/New_York",
            currency="USD",
            main_index="Nasdaq Composite",
            languages=["en"],
            sessions=[
                block("pre_market", "04:00", "09:30", True, False, True),
                block("regular", "09:30", "16:00", True, True, True),
                block("post_market", "16:00", "20:00", True, False, True),
            ],
            source_url="https://www.nasdaqtrader.com/",
            notes="Extended-hours liquidity differs materially from regular trading.",
        ),
        Exchange(
            code="NYSE",
            name="New York Stock Exchange",
            mic="XNYS",
            country="United States",
            region="North America",
            timezone="America/New_York",
            currency="USD",
            main_index="NYSE Composite",
            languages=["en"],
            sessions=[block("regular", "09:30", "16:00", True, True, True)],
        ),
        Exchange(
            code="JPX",
            name="Tokyo Stock Exchange",
            mic="XTKS",
            country="Japan",
            region="Asia",
            timezone="Asia/Tokyo",
            currency="JPY",
            main_index="Nikkei 225",
            languages=["ja", "en"],
            ticker_suffix=".T",
            sessions=[
                block("regular_morning", "09:00", "11:30", True, True, True),
                block("lunch_break", "11:30", "12:30", False, False, False),
                block("regular_afternoon", "12:30", "15:30", True, True, True),
            ],
            source_url="https://www.jpx.co.jp/english/equities/trading/domestic/01.html",
        ),
        Exchange(
            code="HKEX",
            name="Hong Kong Exchanges",
            mic="XHKG",
            country="Hong Kong",
            region="Asia",
            timezone="Asia/Hong_Kong",
            currency="HKD",
            main_index="Hang Seng Index",
            languages=["zh", "en"],
            ticker_suffix=".HK",
            sessions=[
                block("pre_open", "09:00", "09:30", False, False, True),
                block("regular_morning", "09:30", "12:00", True, True, True),
                block("lunch_break", "12:00", "13:00", False, False, False),
                block("regular_afternoon", "13:00", "16:00", True, True, True),
                block("closing_auction", "16:00", "16:10", False, False, True),
            ],
            source_url="https://www.hkex.com.hk/Global/Exchange/FAQ/Securities-Market",
        ),
        Exchange(
            code="SSE",
            name="Shanghai Stock Exchange",
            mic="XSHG",
            country="China",
            region="Asia",
            timezone="Asia/Shanghai",
            currency="CNY",
            main_index="SSE Composite",
            languages=["zh", "en"],
            ticker_suffix=".SS",
            sessions=[
                block("opening_auction", "09:15", "09:25", False, False, True),
                block("regular_morning", "09:30", "11:30", True, True, True),
                block("lunch_break", "11:30", "13:00", False, False, False),
                block("regular_afternoon", "13:00", "14:57", True, True, True),
                block("closing_auction", "14:57", "15:00", False, False, True),
            ],
            source_url="https://english.sse.com.cn/start/trading/schedule/",
        ),
        Exchange(
            code="SZSE",
            name="Shenzhen Stock Exchange",
            mic="XSHE",
            country="China",
            region="Asia",
            timezone="Asia/Shanghai",
            currency="CNY",
            main_index="SZSE Component",
            languages=["zh", "en"],
            ticker_suffix=".SZ",
            sessions=[
                block("opening_auction", "09:15", "09:25", False, False, True),
                block("regular_morning", "09:30", "11:30", True, True, True),
                block("lunch_break", "11:30", "13:00", False, False, False),
                block("regular_afternoon", "13:00", "14:57", True, True, True),
                block("closing_auction", "14:57", "15:00", False, False, True),
            ],
        ),
        Exchange(
            code="NSE_IN",
            name="National Stock Exchange of India",
            mic="XNSE",
            country="India",
            region="Asia",
            timezone="Asia/Kolkata",
            currency="INR",
            main_index="Nifty 50",
            languages=["hi", "en"],
            ticker_suffix=".NS",
            sessions=[block("regular", "09:15", "15:30", True, True, True)],
        ),
        Exchange(
            code="BSE_IN",
            name="BSE India",
            mic="XBOM",
            country="India",
            region="Asia",
            timezone="Asia/Kolkata",
            currency="INR",
            main_index="Sensex",
            languages=["hi", "en"],
            ticker_suffix=".BO",
            sessions=[block("regular", "09:15", "15:30", True, True, True)],
        ),
        Exchange(
            code="SGX",
            name="Singapore Exchange",
            mic="XSES",
            country="Singapore",
            region="Asia",
            timezone="Asia/Singapore",
            currency="SGD",
            main_index="Straits Times Index",
            languages=["en", "zh", "ms", "ta"],
            ticker_suffix=".SI",
            sessions=[
                block("regular_morning", "09:00", "12:00", True, True, True),
                block("lunch_break", "12:00", "13:00", False, False, False),
                block("regular_afternoon", "13:00", "17:00", True, True, True),
                block("closing_routine", "17:00", "17:16", False, False, True),
            ],
        ),
        Exchange(
            code="KRX",
            name="Korea Exchange",
            mic="XKRX",
            country="South Korea",
            region="Asia",
            timezone="Asia/Seoul",
            currency="KRW",
            main_index="KOSPI",
            languages=["ko", "en"],
            ticker_suffix=".KS",
            sessions=[block("regular", "09:00", "15:30", True, True, True)],
        ),
        Exchange(
            code="TWSE",
            name="Taiwan Stock Exchange",
            mic="XTAI",
            country="Taiwan",
            region="Asia",
            timezone="Asia/Taipei",
            currency="TWD",
            main_index="TAIEX",
            languages=["zh", "en"],
            ticker_suffix=".TW",
            sessions=[block("regular", "09:00", "13:30", True, True, True)],
            source_url="https://www.twse.com.tw/en/page/products/trading/introduce.html",
        ),
        Exchange(
            code="JSE",
            name="Johannesburg Stock Exchange",
            mic="XJSE",
            country="South Africa",
            region="Africa",
            timezone="Africa/Johannesburg",
            currency="ZAR",
            main_index="FTSE/JSE Top 40",
            languages=["en"],
            ticker_suffix=".JO",
            sessions=[block("regular", "09:00", "17:00", True, True, True)],
            source_url="https://www.jse.co.za/",
        ),
        Exchange(
            code="EGX",
            name="Egyptian Exchange",
            mic="XCAI",
            country="Egypt",
            region="Africa",
            timezone="Africa/Cairo",
            currency="EGP",
            main_index="EGX 30",
            languages=["ar", "en"],
            ticker_suffix=".CA",
            sessions=[block("regular", "10:00", "14:30", True, True, True)],
        ),
        Exchange(
            code="NGX",
            name="Nigerian Exchange",
            mic="XNSA",
            country="Nigeria",
            region="Africa",
            timezone="Africa/Lagos",
            currency="NGN",
            main_index="NGX All-Share Index",
            languages=["en"],
            ticker_suffix=".LG",
            sessions=[
                block("pre_open", "09:00", "09:30", False, False, True),
                block("regular", "09:30", "16:00", True, True, True),
            ],
            notes="Seed uses the extended schedule announced for 2026-04-27 onward.",
        ),
        Exchange(
            code="NSE_KE",
            name="Nairobi Securities Exchange",
            mic="XNAI",
            country="Kenya",
            region="Africa",
            timezone="Africa/Nairobi",
            currency="KES",
            main_index="NSE 20",
            languages=["en", "sw"],
            ticker_suffix=".NR",
            sessions=[block("regular", "09:30", "15:00", True, True, True)],
        ),
        Exchange(
            code="CSE_MA",
            name="Casablanca Stock Exchange",
            mic="XCAS",
            country="Morocco",
            region="Africa",
            timezone="Africa/Casablanca",
            currency="MAD",
            main_index="MASI",
            languages=["ar", "fr", "en"],
            ticker_suffix=".CS",
            sessions=[block("regular", "09:30", "15:30", True, True, True)],
        ),
        Exchange(
            code="COMEX",
            name="COMEX Metals",
            mic="XCEC",
            country="United States",
            region="Commodities",
            timezone="America/New_York",
            currency="USD",
            main_index="Metals Futures",
            languages=["en"],
            sessions=[block("regular", "08:20", "13:30", True, True, True)],
            notes="Seeded pit-style regular hours; electronic sessions are broader.",
            market_type="commodity",
        ),
        Exchange(
            code="NYMEX",
            name="NYMEX Energy",
            mic="XNYM",
            country="United States",
            region="Commodities",
            timezone="America/New_York",
            currency="USD",
            main_index="Energy Futures",
            languages=["en"],
            sessions=[block("regular", "09:00", "14:30", True, True, True)],
            notes="Seeded regular hours; electronic sessions are broader.",
            market_type="commodity",
        ),
        Exchange(
            code="ICE",
            name="ICE Futures",
            mic="IFEU",
            country="United Kingdom",
            region="Commodities",
            timezone="Europe/London",
            currency="USD",
            main_index="ICE Commodity Futures",
            languages=["en"],
            sessions=[block("regular", "08:00", "17:00", True, True, True)],
            market_type="commodity",
        ),
        Exchange(
            code="LME",
            name="London Metal Exchange",
            mic="XLME",
            country="United Kingdom",
            region="Commodities",
            timezone="Europe/London",
            currency="USD",
            main_index="Base Metals",
            languages=["en"],
            sessions=[block("regular", "08:00", "17:00", True, True, True)],
            market_type="commodity",
        ),
        Exchange(
            code="SHFE",
            name="Shanghai Futures Exchange",
            mic="XSGE",
            country="China",
            region="Commodities",
            timezone="Asia/Shanghai",
            currency="CNY",
            main_index="China Commodity Futures",
            languages=["zh", "en"],
            sessions=[
                block("regular_morning", "09:00", "11:30", True, True, True),
                block("lunch_break", "11:30", "13:30", False, False, False),
                block("regular_afternoon", "13:30", "15:00", True, True, True),
            ],
            market_type="commodity",
        ),
    ]
