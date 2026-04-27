# Watchtower op USB en andere PC installeren

Dit pakket is bedoeld voor Windows.

## USB-pakket maken

Op deze PC:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\package_usb.ps1
```

Daarna staat het pakket hier:

- `dist\WatchtowerUSB`
- `dist\WatchtowerUSB.zip`

Zet de hele map `WatchtowerUSB` of de zip op je USB-stick.

## Installeren op een andere PC

1. Open de USB-stick.
2. Dubbelklik op `INSTALLEER_WATCHTOWER.bat`.
3. De installer kopieert Watchtower naar `%LOCALAPPDATA%\Watchtower`.
4. De installer maakt een snelkoppeling op het bureaublad: `Watchtower Dashboard`.
5. Start Watchtower via die snelkoppeling.

## Vereisten

De doel-PC heeft Python 3.11 of nieuwer nodig.

Als Python nog niet geinstalleerd is:

- Installeer Python vanaf https://www.python.org/downloads/
- Vink tijdens installatie `Add python.exe to PATH` aan.
- Start daarna `INSTALLEER_WATCHTOWER.bat` opnieuw.

## Zonder internet op de doel-PC

De normale installer haalt dependencies via internet op. Wil je offline installeren, maak dan eerst een wheelhouse:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\download_wheels.ps1
powershell.exe -ExecutionPolicy Bypass -File .\scripts\package_usb.ps1
```

Kopieer daarna het nieuwe USB-pakket. De installer gebruikt automatisch de lokale `wheels` map.

## Starten en stoppen

Na installatie:

- Starten: bureaubladsnelkoppeling `Watchtower Dashboard`
- Stoppen: `STOP_WATCHTOWER.bat` in de installatiemap

Dashboard:

- http://127.0.0.1:8011/dashboard

API docs:

- http://127.0.0.1:8011/docs

## Data

Watchtower bewaart zijn database lokaal in:

```text
%LOCALAPPDATA%\Watchtower\data\watchtower.db
```

Maak een backup van die map als je resultaten en learning-data wilt bewaren.
