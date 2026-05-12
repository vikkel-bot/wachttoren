from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from typing import Any


DEFAULT_COLONY_CONFIG = {
    "enabled": True,
    "webhook_url": None,
    "dry_run": False,
    "min_entry_score": 0.50,
    "min_confidence": 0.50,
    "min_entry_score_commodity": 0.35,
    "min_confidence_commodity": 0.35,
    "max_batch_size": 25,
}

_COMMODITY_ASSETS = {"BRENT", "WTI", "NATGAS", "COPPER", "SILVER", "GOLD"}


class ColonyBridge:
    def qualified_signals(
        self,
        signals: list[dict[str, Any]],
        watchlist: list[dict[str, Any]],
        config: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        config = {**DEFAULT_COLONY_CONFIG, **(config or {})}
        active_watchlist = [item for item in watchlist if item.get("enabled", True)]
        watch_by_key = {
            f"{item.get('exchange', 'GLOBAL').upper()}:{item['asset'].upper()}": item
            for item in active_watchlist
        }
        watch_by_asset = {item["asset"].upper(): item for item in active_watchlist}
        require_watchlist = bool(active_watchlist)
        qualified: list[dict[str, Any]] = []

        for signal in signals:
            asset = signal.get("asset", "").upper()
            exchange = signal.get("exchange", "GLOBAL").upper()
            watch_item = watch_by_key.get(f"{exchange}:{asset}") or watch_by_asset.get(asset)
            if require_watchlist and not watch_item:
                continue
            if signal.get("direction") == "neutral":
                continue

            watch_item = watch_item or {}
            if _is_commodity_signal(signal):
                min_entry_score = min(
                    float(watch_item.get("min_entry_score", config["min_entry_score_commodity"])),
                    float(config["min_entry_score_commodity"]),
                )
                min_confidence = min(
                    float(watch_item.get("min_confidence", config["min_confidence_commodity"])),
                    float(config["min_confidence_commodity"]),
                )
            else:
                min_entry_score = watch_item.get("min_entry_score", config["min_entry_score"])
                min_confidence = watch_item.get("min_confidence", config["min_confidence"])
            if signal.get("entry_score", 0.0) < min_entry_score:
                continue
            if signal.get("confidence", 0.0) < min_confidence:
                continue

            enriched = dict(signal)
            enriched["colony_packet"] = {
                "qualified": True,
                "thresholds": {
                    "min_entry_score": min_entry_score,
                    "min_confidence": min_confidence,
                },
                "expired": self._is_expired(signal.get("expires_at")),
            }
            qualified.append(enriched)

        return qualified[: int(config.get("max_batch_size", 25))]

    def build_packet(self, signals: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "packet_type": "watchtower.entry_signals",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(signals),
            "signals": signals,
        }

    def dispatch(self, webhook_url: str | None, packet: dict[str, Any], dry_run: bool = True) -> dict[str, Any]:
        if dry_run:
            return {"status": "dry_run", "sent": False, "packet": packet}
        if not webhook_url:
            return {"status": "skipped", "sent": False, "reason": "webhook_url_missing", "packet": packet}

        body = json.dumps(packet).encode("utf-8")
        request = urllib.request.Request(
            webhook_url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "watchtower-mvp/0.1"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            response_body = response.read().decode("utf-8", errors="replace")
        return {
            "status": "sent",
            "sent": True,
            "http_status": response.status,
            "response": response_body[:1000],
        }

    def _is_expired(self, expires_at: str | None) -> bool:
        if not expires_at:
            return False
        try:
            expires = datetime.fromisoformat(expires_at)
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            return expires < datetime.now(timezone.utc)
        except ValueError:
            return False


def _is_commodity_signal(signal: dict[str, Any]) -> bool:
    asset_class = str(signal.get("asset_class") or signal.get("source_field") or "").lower()
    if asset_class in {"commodity", "commodities"}:
        return True
    asset = str(signal.get("asset") or signal.get("symbol") or "").upper()
    return asset in _COMMODITY_ASSETS
