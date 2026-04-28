# Watchtower MVP

Watchtower is een losse entry-intelligence service. Hij verzamelt events en marktcontext, maakt daar entry-signalen van, en geeft die signalen door aan een externe colony of bot. De service trade niet zelf en bevat geen trading API keys.

## Wat zit erin

- FastAPI service met health, event, market, signal en outcome endpoints
- Connectorlaag voor mock news, publieke RSS-feeds, Alpha Vantage/EODHD nieuws, mock market snapshots en public Bitvavo crypto market-data
- News Radar voor GDELT/RSS/officiele headlines binnen een EUR25-maandbudget
- Exchange Universe voor AEX, Nasdaq, grote Aziatische beurzen en Afrikaanse beurzen
- Regionale watchlist met thresholds per exchange + asset
- Trading-session detector met lokale timezone en reguliere sessies
- Modulaire scoring engine voor entry timing
- Regime detectie voor bull, bear, sideways en volatile context
- Simpele asset resolver
- SQLite opslag voor events, snapshots, signalen, outcomes, watchlist en settings
- Outcome tracker en learning summary om signalen achteraf te labelen
- Colony output als gefilterd signaalpakket of optionele webhook
- Listed asset universe en entity resolver per beurs
- Onderscheid tussen `equity`, `commodity` en `crypto` assets
- Cross-field context voor `BTC vs QQQ`, `BTC vs DXY` en de `ETH/BTC` ratio
- Provider registry voor mock, RSS en geplande echte data providers
- Pipeline-run endpoint om watched assets automatisch te evalueren
- Dashboard met equity entries, commodity entries, crypto entries en globale marktkleur
- `/backtest/signals` export voor Colony v2 replay/backtests
- `/news/historical` en `/news/sentiment` endpoints voor historische nieuwsdata en sentiment
- Mock/demo flow zonder echte news API of broker

## Installeren

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Starten

```powershell
uvicorn watchtower.main:app --reload
```

Open daarna:

- API docs: http://127.0.0.1:8000/docs
- Health: http://127.0.0.1:8000/health
- Dashboard: http://127.0.0.1:8000/dashboard

## USB/installatiepakket maken

Voor een Windows PC kun je een USB-pakket maken met installer, startscript en bureaubladsnelkoppeling:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\package_usb.ps1
```

Zet daarna `dist\WatchtowerUSB` of `dist\WatchtowerUSB.zip` op een USB-stick. Op de andere PC dubbelklik je op:

```text
INSTALLEER_WATCHTOWER.bat
```

Meer uitleg staat in `INSTALL_USB_NL.md`.

## Snelle demo zonder server

```powershell
python -m watchtower.demo
```

## Signaal evalueren

```powershell
$body = @{
  event = @{
    asset = "AAPL"
    headline = "Apple beats earnings expectations and raises guidance"
    summary = "Revenue and margins came in above analyst estimates."
    source = "example-news"
    sentiment = 0.82
    novelty = 0.76
    relevance = 0.9
    tags = @("earnings", "guidance")
  }
  market = @{
    asset = "AAPL"
    price = 192.4
    change_15m_pct = 0.45
    change_1h_pct = 1.1
    change_1d_pct = 2.3
    volume_zscore = 2.2
    volatility_zscore = 1.1
    trend_1h = 0.7
    trend_1d = 0.55
    sector_change_1d_pct = 1.0
    benchmark_change_1d_pct = 0.6
  }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/signals/evaluate -Body $body -ContentType "application/json"
```

## Nieuwe modules

### 1. Connectors

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/connectors

$body = @{
  connector = "mock-news"
  asset = "AAPL"
  limit = 3
  ingest = $true
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/connectors/news/fetch -Body $body -ContentType "application/json"
```

Voor publieke RSS-feeds:

```powershell
$body = @{
  connector = "rss"
  asset = "AAPL"
  feed_url = "https://example.com/feed.xml"
  limit = 10
} | ConvertTo-Json
```

Voor public crypto market-data via Bitvavo:

```powershell
$body = @{
  connector = "bitvavo-public"
  asset = "BTC-EUR"
  exchange = "BITVAVO"
  ingest = $true
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/connectors/market/fetch -Body $body -ContentType "application/json"
```

Voor historische news/sentiment providers zet je lokaal in `.env`:

```text
ALPHAVANTAGE_API_KEY=<jouw-key>
EODHD_API_KEY=<jouw-key>
```

`.env` wordt niet gecommit. Gebruik `.env.example` als template. Alpha Vantage en EODHD hebben in de gratis tier kleine daglimieten; Watchtower houdt daarom lokaal tellers bij in `data/alphavantage_requests.json` en `data/eodhd_requests.json`.

Historisch EODHD nieuws ophalen en opslaan:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/news/historical?asset=AAPL&connector=eodhd-news&from_dt=2026-04-01T00:00:00Z&to_dt=2026-04-28T00:00:00Z&limit=200"
```

Alpha Vantage nieuws met sentiment ophalen en opslaan:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/news/sentiment?asset=BTC-EUR&from_dt=2026-04-01T00:00:00Z&to_dt=2026-04-28T00:00:00Z&limit=50"
```

### 1b. News Radar onder EUR25 per maand

De radar gebruikt eerst gratis bronnen: GDELT, publieke RSS-feeds en officiele centrale-bankfeeds. Betaalde bronnen zoals X API en NewsAPI productie staan bewust uit tot de radar waarde bewijst.

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/news-radar/config

$body = @{
  mode = "mock"
  limit = 6
  ingest = $true
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/news-radar/scan -Body $body -ContentType "application/json"
Invoke-RestMethod -Uri http://127.0.0.1:8000/news-radar/events
```

Voor echte gratis bronnen:

```powershell
$body = @{
  mode = "free"
  limit = 25
  max_per_source = 5
  timespan = "1d"
  ingest = $true
} | ConvertTo-Json
```

### 2. Watchlist

```powershell
$body = @{
  exchange = "NASDAQ"
  asset = "AAPL"
  enabled = $true
  min_entry_score = 0.72
  min_confidence = 0.6
  max_signals_per_hour = 5
  notes = "core equity"
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/watchlist -Body $body -ContentType "application/json"
Invoke-RestMethod -Uri http://127.0.0.1:8000/watchlist
Invoke-RestMethod -Uri "http://127.0.0.1:8000/watchlist?exchange=NASDAQ"
```

Per beurs kan ook:

```powershell
$body = @{
  asset = "ASML"
  min_entry_score = 0.72
  min_confidence = 0.6
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/exchanges/AEX/watchlist -Body $body -ContentType "application/json"
```

### 3. Outcome learning

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/learning/summary
Invoke-RestMethod -Uri http://127.0.0.1:8000/learning/recommendations
```

### 4. Regime detector

```powershell
$body = @{
  asset = "BTC"
  price = 65000
  volatility_zscore = 3.0
  trend_1d = 0.2
  benchmark_change_1d_pct = 0.4
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/regime/detect -Body $body -ContentType "application/json"
```

### 5. Colony output

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/colony/signals

$body = @{
  dry_run = $true
  limit = 25
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/colony/dispatch -Body $body -ContentType "application/json"
```

### 5b. Backtest signal export

Deze endpoint exporteert immutable Watchtower-signalen in een vorm die Colony v2 kan replayen. Watchtower trade niet zelf; Colony leest de signalen en test ze tegen historische candles.

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/backtest/signals?asset_class=crypto"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/backtest/signals?exchange=BITVAVO&from=2026-04-01T00:00:00Z"
```

### 6. Dashboard

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/dashboard/summary
```

## Exchange Universe

Fase 1 t/m 3 zijn toegevoegd als basis voor globale marktdekking.

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/exchanges
Invoke-RestMethod -Uri http://127.0.0.1:8000/exchanges/regions
Invoke-RestMethod -Uri http://127.0.0.1:8000/exchanges/AEX
Invoke-RestMethod -Uri http://127.0.0.1:8000/exchanges/AEX/session
Invoke-RestMethod -Uri "http://127.0.0.1:8000/exchanges/AEX/session?at=2026-04-27T10:00:00%2B02:00"
```

Seeded markets:

- Europe: `AEX`
- North America: `NASDAQ`, `NYSE`
- Asia: `JPX`, `HKEX`, `SSE`, `SZSE`, `NSE_IN`, `BSE_IN`, `SGX`, `KRX`, `TWSE`
- Africa: `JSE`, `EGX`, `NGX`, `NSE_KE`, `CSE_MA`
- Commodities: `COMEX`, `NYMEX`, `ICE`, `LME`, `SHFE`
- Crypto: `BITVAVO`

Let op: de huidige trading calendar ondersteunt weekends en seeded reguliere sessies. Officiele feestdagen, half-days en lokale uitzonderingen zijn de volgende stap voordat dit voor echte live entry-timing gebruikt wordt.

## Complete MVP Flow

### Providers bekijken

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/providers
```

Geimplementeerd voor deze MVP:

- `mock-news`
- `mock-regional-news`
- `alphavantage-news`
- `eodhd-news`
- `mock-market`
- `mock-regional-market`
- `bitvavo-public`
- `rss`

Gepland als echte providers:

- `finnhub`
- `polygon`
- `twelve-data`
- `eodhd`

### Assets zoeken en resolven

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/assets?exchange=AEX"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/assets?region=Asia"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/assets?asset_class=commodity"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/assets?asset_class=crypto"

$body = @{
  text = "ASML reports stronger chip demand"
  exchange = "AEX"
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/assets/resolve -Body $body -ContentType "application/json"
```

### Watchlist automatisch vullen

```powershell
$body = @{
  exchange = "AEX"
  max_assets = 3
  min_entry_score = 0.7
  min_confidence = 0.55
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/watchlist/seed -Body $body -ContentType "application/json"
```

Voor een hele regio:

```powershell
$body = @{
  region = "Africa"
  max_assets = 10
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/watchlist/seed -Body $body -ContentType "application/json"
```

Voor crypto:

```powershell
$body = @{
  exchange = "BITVAVO"
  asset_class = "crypto"
  max_assets = 4
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/watchlist/seed -Body $body -ContentType "application/json"
```

### Pipeline draaien

```powershell
$body = @{
  exchange = "AEX"
  news_connector = "mock-regional-news"
  market_connector = "mock-regional-market"
  max_assets = 5
  max_events_per_asset = 1
  persist = $true
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/pipeline/run -Body $body -ContentType "application/json"
```

Crypto pipeline met echte public market-data:

```powershell
$body = @{
  exchange = "BITVAVO"
  news_connector = "mock-regional-news"
  market_connector = "bitvavo-public"
  max_assets = 4
  max_events_per_asset = 1
  persist = $true
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/pipeline/run -Body $body -ContentType "application/json"
```

Daarna:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/signals?exchange=AEX"
Invoke-RestMethod -Uri http://127.0.0.1:8000/colony/signals
Invoke-RestMethod -Uri http://127.0.0.1:8000/dashboard/summary
```

Het dashboard toont nu:

- beste equity entry per markt
- beste commodity entry per commodity-markt
- beste crypto entry per crypto-markt
- aparte counts voor equities, commodities en crypto
- globale achtergrondkleur: groen bij brede plus, oranje bij sideways, rood bij brede daling

## Nog Niet Production Ready

Deze MVP is nu compleet als architectuur en lokale workflow, maar nog niet als live trading intelligence systeem. Voor productie ontbreken nog:

- officiele exchange holiday calendars en half-days
- betaalde of betrouwbare intraday market data
- echte news provider keys
- survivorship-bias-vrije historische datasets
- slippage/spread modelling
- monitoring, auth en rate limits
- compliance checks per markt/regio

## MVP principe

De eerste versie is bewust meetbaar in plaats van magisch. Elk signaal krijgt een score, confidence, risk flags en expiry. Daarna kan een outcome worden vastgelegd voor bijvoorbeeld 15m, 1h, 4h of 1d. Zo leert de Watchtower later welke bronnen, eventtypes en marktregimes echt waarde toevoegen.
