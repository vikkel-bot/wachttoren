from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump())
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    return value


class SQLiteStore:
    def __init__(self, path: str | Path = "watchtower.db") -> None:
        self.path = Path(path)

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    asset TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY,
                    asset TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watchlist (
                    key TEXT PRIMARY KEY,
                    exchange TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    asset TEXT NOT NULL,
                    region TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    min_entry_score REAL NOT NULL,
                    min_confidence REAL NOT NULL,
                    max_signals_per_hour INTEGER NOT NULL,
                    notes TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS colony_feedback (
                    id TEXT PRIMARY KEY,
                    signal_id TEXT,
                    feedback_type TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    biome TEXT NOT NULL,
                    is_replay INTEGER NOT NULL DEFAULT 0,
                    replay_id TEXT,
                    data_quality TEXT NOT NULL DEFAULT 'MEDIUM',
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._ensure_watchlist_schema(conn)

    def save_event(self, event: Any) -> dict[str, Any]:
        payload = to_jsonable(event)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO events (id, asset, created_at, payload) VALUES (?, ?, ?, ?)",
                (payload["id"], payload["asset"], payload["published_at"], json.dumps(payload)),
            )
        return payload

    def save_market_snapshot(self, snapshot: Any) -> dict[str, Any]:
        payload = to_jsonable(snapshot)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO market_snapshots (asset, created_at, payload) VALUES (?, ?, ?)",
                (payload["asset"], payload["timestamp"], json.dumps(payload)),
            )
        return payload

    def save_signal(self, signal: Any) -> dict[str, Any]:
        payload = to_jsonable(signal)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO signals (id, asset, created_at, payload) VALUES (?, ?, ?, ?)",
                (payload["id"], payload["asset"], payload["created_at"], json.dumps(payload)),
            )
        return payload

    def list_events(
        self,
        limit: int = 50,
        asset: str | None = None,
        tag: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        with self._connect() as conn:
            if asset:
                rows = conn.execute(
                    "SELECT payload FROM events WHERE asset = ? ORDER BY created_at DESC LIMIT ?",
                    (asset.upper(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload FROM events ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        events = [json.loads(row["payload"]) for row in rows]
        if tag:
            tag_lower = tag.lower()
            events = [event for event in events if tag_lower in {item.lower() for item in event.get("tags", [])}]
        return events[:limit]

    def save_outcome(self, outcome: Any) -> dict[str, Any]:
        payload = to_jsonable(outcome)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO outcomes (signal_id, created_at, payload) VALUES (?, ?, ?)",
                (payload["signal_id"], payload["evaluated_at"], json.dumps(payload)),
            )
        return payload

    def list_signals(
        self,
        limit: int = 50,
        asset: str | None = None,
        exchange: str | None = None,
        region: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 250))
        with self._connect() as conn:
            if asset:
                rows = conn.execute(
                    "SELECT payload FROM signals WHERE asset = ? ORDER BY created_at DESC LIMIT ?",
                    (asset.upper(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload FROM signals ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        signals = [json.loads(row["payload"]) for row in rows]
        if exchange:
            exchange_code = exchange.upper()
            signals = [signal for signal in signals if signal.get("exchange", "GLOBAL").upper() == exchange_code]
        if region:
            region_lower = region.lower()
            signals = [signal for signal in signals if signal.get("region", "global").lower() == region_lower]
        return signals[:limit]

    def upsert_watchlist_item(self, item: Any) -> dict[str, Any]:
        payload = to_jsonable(item)
        payload["asset"] = payload["asset"].upper()
        payload["exchange"] = payload.get("exchange", "GLOBAL").upper()
        payload.setdefault("region", "global")
        payload.setdefault("currency", "USD")
        payload.setdefault("timezone", "UTC")
        key = self._watchlist_key(payload["exchange"], payload["asset"])
        payload["key"] = key
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO watchlist
                (key, exchange, asset, region, currency, enabled, min_entry_score, min_confidence, max_signals_per_hour, notes, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    payload["exchange"],
                    payload["asset"],
                    payload["region"],
                    payload["currency"],
                    1 if payload["enabled"] else 0,
                    payload["min_entry_score"],
                    payload["min_confidence"],
                    payload["max_signals_per_hour"],
                    payload.get("notes", ""),
                    json.dumps(payload),
                ),
            )
        return payload

    def list_watchlist(
        self,
        enabled_only: bool = False,
        exchange: str | None = None,
        region: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT payload FROM watchlist"
        clauses = []
        params: list[Any] = []
        if enabled_only:
            clauses.append("enabled = ?")
            params.append(1)
        if exchange:
            clauses.append("exchange = ?")
            params.append(exchange.upper())
        if region:
            clauses.append("LOWER(region) = ?")
            params.append(region.lower())
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY exchange ASC, asset ASC"
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def get_watchlist_item(self, asset: str, exchange: str | None = None) -> dict[str, Any] | None:
        with self._connect() as conn:
            if exchange:
                row = conn.execute(
                    "SELECT payload FROM watchlist WHERE key = ?",
                    (self._watchlist_key(exchange, asset),),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT payload FROM watchlist WHERE asset = ? ORDER BY exchange = 'GLOBAL' DESC, exchange ASC LIMIT 1",
                    (asset.upper(),),
                ).fetchone()
        if not row:
            return None
        return json.loads(row["payload"])

    def delete_watchlist_item(self, asset: str, exchange: str | None = None) -> bool:
        with self._connect() as conn:
            if exchange:
                result = conn.execute(
                    "DELETE FROM watchlist WHERE key = ?",
                    (self._watchlist_key(exchange, asset),),
                )
            else:
                result = conn.execute("DELETE FROM watchlist WHERE asset = ?", (asset.upper(),))
        return result.rowcount > 0

    def get_signal(self, signal_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM signals WHERE id = ?", (signal_id,)).fetchone()
        if not row:
            return None
        return json.loads(row["payload"])

    def save_setting(self, key: str, payload: Any) -> dict[str, Any]:
        data = to_jsonable(payload)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, payload) VALUES (?, ?)",
                (key, json.dumps(data)),
            )
        return data

    def get_setting(self, key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM settings WHERE key = ?", (key,)).fetchone()
        if not row:
            return default or {}
        return json.loads(row["payload"])

    def list_outcomes(self, limit: int = 250) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM outcomes ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def learning_summary(self, limit: int = 1000) -> dict[str, Any]:
        limit = max(1, min(limit, 5000))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT o.payload AS outcome_payload, s.payload AS signal_payload
                FROM outcomes o
                LEFT JOIN signals s ON s.id = o.signal_id
                ORDER BY o.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        buckets: dict[str, dict[str, dict[str, Any]]] = {
            "by_asset": {},
            "by_exchange": {},
            "by_region": {},
            "by_window": {},
            "by_direction": {},
        }
        total = 0
        hits = 0
        return_sum = 0.0

        for row in rows:
            outcome = json.loads(row["outcome_payload"])
            signal = json.loads(row["signal_payload"]) if row["signal_payload"] else {}
            total += 1
            hit = bool(outcome.get("hit"))
            hits += 1 if hit else 0
            return_pct = float(outcome.get("return_pct", 0.0))
            return_sum += return_pct
            self._add_metric(buckets["by_asset"], signal.get("asset", "UNKNOWN"), hit, return_pct)
            self._add_metric(buckets["by_exchange"], signal.get("exchange", "GLOBAL"), hit, return_pct)
            self._add_metric(buckets["by_region"], signal.get("region", "global"), hit, return_pct)
            self._add_metric(buckets["by_window"], outcome.get("window", "unknown"), hit, return_pct)
            self._add_metric(buckets["by_direction"], signal.get("direction", "unknown"), hit, return_pct)

        return {
            "total_outcomes": total,
            "hits": hits,
            "hit_rate": round(hits / total, 4) if total else 0.0,
            "avg_return_pct": round(return_sum / total, 4) if total else 0.0,
            "by_asset": self._finalize_metrics(buckets["by_asset"]),
            "by_exchange": self._finalize_metrics(buckets["by_exchange"]),
            "by_region": self._finalize_metrics(buckets["by_region"]),
            "by_window": self._finalize_metrics(buckets["by_window"]),
            "by_direction": self._finalize_metrics(buckets["by_direction"]),
        }

    def dashboard_summary(self) -> dict[str, Any]:
        signals = self.list_signals(limit=75)
        watchlist = self.list_watchlist()
        learning = self.learning_summary(limit=1000)
        qualified = [signal for signal in signals if signal.get("direction") != "neutral"]
        return {
            "signal_count": len(signals),
            "active_watchlist_count": len([item for item in watchlist if item.get("enabled")]),
            "latest_signals": signals[:40],
            "qualified_recent_signals": qualified[:10],
            "learning": learning,
        }

    def save_colony_feedback(self, record: dict) -> dict:
        record_id = f"cf_{uuid.uuid4().hex[:12]}"
        created_at = datetime.now(timezone.utc).isoformat()
        record = dict(record)
        record["id"] = record_id
        record["created_at"] = created_at
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO colony_feedback
                (id, signal_id, feedback_type, asset, biome, is_replay, replay_id, data_quality, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    record.get("signal_id"),
                    record["feedback_type"],
                    record["asset"],
                    record["biome"],
                    1 if record.get("is_replay") else 0,
                    record.get("replay_id"),
                    record.get("data_quality", "MEDIUM"),
                    json.dumps(record),
                    created_at,
                ),
            )
        return record

    def list_colony_feedback(
        self,
        limit: int = 250,
        feedback_type: str | None = None,
        is_replay: bool | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        clauses: list[str] = []
        params: list[Any] = []
        if feedback_type:
            clauses.append("feedback_type = ?")
            params.append(feedback_type)
        if is_replay is not None:
            clauses.append("is_replay = ?")
            params.append(1 if is_replay else 0)
        query = "SELECT payload FROM colony_feedback"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def colony_feedback_stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT feedback_type, is_replay, payload FROM colony_feedback"
            ).fetchall()

        total = len(rows)
        live_trades = 0
        replay_trades = 0
        signal_skips = 0
        asset_stats: dict[str, dict[str, Any]] = {}
        skip_reasons: dict[str, int] = {}

        for row in rows:
            ft = row["feedback_type"]
            is_replay = bool(row["is_replay"])
            payload = json.loads(row["payload"])

            if ft == "TRADE_OUTCOME":
                if is_replay:
                    replay_trades += 1
                else:
                    live_trades += 1
                    asset = payload.get("asset", "UNKNOWN")
                    pnl_pct = payload.get("pnl_pct")
                    if asset not in asset_stats:
                        asset_stats[asset] = {"count": 0, "wins": 0, "pnl_sum": 0.0}
                    asset_stats[asset]["count"] += 1
                    if pnl_pct is not None:
                        asset_stats[asset]["pnl_sum"] += float(pnl_pct)
                        if float(pnl_pct) > 0:
                            asset_stats[asset]["wins"] += 1
            elif ft == "SIGNAL_SKIP":
                signal_skips += 1
                skip_reason = payload.get("skip_reason")
                if skip_reason:
                    skip_reasons[skip_reason] = skip_reasons.get(skip_reason, 0) + 1

        win_rate_per_asset = {}
        avg_pnl_pct_per_asset = {}
        for asset, stats in asset_stats.items():
            count = stats["count"]
            win_rate_per_asset[asset] = round(stats["wins"] / count, 4) if count else 0.0
            avg_pnl_pct_per_asset[asset] = round(stats["pnl_sum"] / count, 4) if count else 0.0

        calibration_active = live_trades >= 5
        if live_trades == 0:
            calibration_basis = "BOOTSTRAPPING"
        elif replay_trades == 0:
            calibration_basis = "LIVE_ONLY"
        else:
            calibration_basis = "LIVE_AND_REPLAY"

        return {
            "total": total,
            "live_trades": live_trades,
            "replay_trades": replay_trades,
            "signal_skips": signal_skips,
            "win_rate_per_asset": win_rate_per_asset,
            "avg_pnl_pct_per_asset": avg_pnl_pct_per_asset,
            "skip_reasons": skip_reasons,
            "calibration_active": calibration_active,
            "calibration_basis": calibration_basis,
        }

    def get_replay_ids(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT replay_id FROM colony_feedback WHERE replay_id IS NOT NULL"
            ).fetchall()
        return {row["replay_id"] for row in rows}

    def _add_metric(self, bucket: dict[str, dict[str, Any]], key: str, hit: bool, return_pct: float) -> None:
        if key not in bucket:
            bucket[key] = {"count": 0, "hits": 0, "return_sum": 0.0}
        bucket[key]["count"] += 1
        bucket[key]["hits"] += 1 if hit else 0
        bucket[key]["return_sum"] += return_pct

    def _finalize_metrics(self, bucket: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        finalized = {}
        for key, value in bucket.items():
            count = value["count"]
            finalized[key] = {
                "count": count,
                "hits": value["hits"],
                "hit_rate": round(value["hits"] / count, 4) if count else 0.0,
                "avg_return_pct": round(value["return_sum"] / count, 4) if count else 0.0,
            }
        return finalized

    def _watchlist_key(self, exchange: str, asset: str) -> str:
        return f"{exchange.upper()}:{asset.upper()}"

    def _ensure_watchlist_schema(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(watchlist)").fetchall()}
        if "key" in columns:
            return

        legacy_rows = conn.execute("SELECT payload FROM watchlist").fetchall()
        conn.execute("ALTER TABLE watchlist RENAME TO watchlist_legacy")
        conn.execute(
            """
            CREATE TABLE watchlist (
                key TEXT PRIMARY KEY,
                exchange TEXT NOT NULL,
                asset TEXT NOT NULL,
                region TEXT NOT NULL,
                currency TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                min_entry_score REAL NOT NULL,
                min_confidence REAL NOT NULL,
                max_signals_per_hour INTEGER NOT NULL,
                notes TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        for row in legacy_rows:
            payload = json.loads(row["payload"])
            payload["asset"] = payload["asset"].upper()
            payload["exchange"] = payload.get("exchange", "GLOBAL").upper()
            payload.setdefault("region", "global")
            payload.setdefault("currency", "USD")
            payload.setdefault("timezone", "UTC")
            payload["key"] = self._watchlist_key(payload["exchange"], payload["asset"])
            conn.execute(
                """
                INSERT OR REPLACE INTO watchlist
                (key, exchange, asset, region, currency, enabled, min_entry_score, min_confidence, max_signals_per_hour, notes, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["key"],
                    payload["exchange"],
                    payload["asset"],
                    payload["region"],
                    payload["currency"],
                    1 if payload.get("enabled", True) else 0,
                    payload.get("min_entry_score", 0.7),
                    payload.get("min_confidence", 0.55),
                    payload.get("max_signals_per_hour", 5),
                    payload.get("notes", ""),
                    json.dumps(payload),
                ),
            )
        conn.execute("DROP TABLE watchlist_legacy")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
