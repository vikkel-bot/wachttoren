from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from watchtower.storage import SQLiteStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate Watchtower signals to one signal per asset per UTC hour.")
    parser.add_argument(
        "--db",
        default=os.getenv("WATCHTOWER_DB", "watchtower.db"),
        help="Path to the Watchtower SQLite database. Defaults to WATCHTOWER_DB or watchtower.db.",
    )
    args = parser.parse_args()

    store = SQLiteStore(args.db)
    db_path = Path(args.db)
    if not db_path.exists() or not _table_exists(db_path, "signals"):
        store.init_schema()
    report = store.migrate_signal_deduplication()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _table_exists(db_path: Path, table: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    return row is not None


if __name__ == "__main__":
    raise SystemExit(main())
