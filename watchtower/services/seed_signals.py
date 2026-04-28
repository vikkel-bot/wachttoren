from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def seed_signals_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    data_dir = Path(os.getenv("WATCHTOWER_DATA_DIR", "data"))
    return data_dir / "backtest_signals_seed.jsonl"


def append_seed_signals(signals: list[dict[str, Any]], path: str | Path | None = None) -> Path:
    out_path = seed_signals_path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as handle:
        for signal in signals:
            handle.write(json.dumps(signal, ensure_ascii=False) + "\n")
    return out_path


def read_seed_signals(path: str | Path | None = None) -> list[dict[str, Any]]:
    in_path = seed_signals_path(path)
    if not in_path.exists():
        return []

    signals: list[dict[str, Any]] = []
    try:
        lines = in_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("signals"), list):
            signals.extend(item for item in payload["signals"] if isinstance(item, dict))
        elif isinstance(payload, dict):
            signals.append(payload)
    return signals


def filter_seed_signals(
    signals: list[dict[str, Any]],
    asset: str | None = None,
    from_dt: str | None = None,
    to_dt: str | None = None,
    min_entry_score: float = 0.0,
    limit: int = 250,
) -> list[dict[str, Any]]:
    asset_upper = asset.upper() if asset else None
    start = _parse_ts(from_dt)
    end = _parse_ts(to_dt)
    filtered: list[dict[str, Any]] = []

    for signal in signals:
        if asset_upper and str(signal.get("asset") or signal.get("symbol") or "").upper() != asset_upper:
            continue
        if float(signal.get("entry_score") or 0.0) < min_entry_score:
            continue
        timestamp = _parse_ts(signal.get("timestamp") or signal.get("created_at"))
        if (start or end) and timestamp is None:
            continue
        if start and timestamp and timestamp < start:
            continue
        if end and timestamp and timestamp > end:
            continue
        filtered.append(signal)
        if len(filtered) >= limit:
            break
    return filtered


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
