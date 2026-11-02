"""Copy the stream database to data/backups/<name>_<YYYYmmdd_HHMMSS>.db.

Uses SQLite's backup API, so the copy is consistent even while the stream
is writing (WAL included).  Keeps the newest --keep backups of this
database and deletes older ones.  The database path comes from
rlgymstream.toml (or RLGYMSTREAM_DB_PATH), like the stream itself.

Usage:
    python scripts/backup_db.py                 # keep the newest 30
    python scripts/backup_db.py --keep 10
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tomllib
from datetime import datetime
from pathlib import Path

from rlgymstream.config import AppConfig


def db_path_from_config(config: Path) -> Path:
    """The database path the stream would use.

    Reads only db_path, not the full config: AppConfig.from_toml checks that
    every bot source exists, which isn't true before the bots are installed.
    """
    path = AppConfig().db_path
    if config.exists():
        with open(config, "rb") as f:
            path = Path(tomllib.load(f).get("db_path", path))
    if env := os.environ.get("RLGYMSTREAM_DB_PATH"):
        path = Path(env)
    return path


def backup(db_path: Path, backup_dir: Path, keep: int) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"{db_path.stem}_{datetime.now():%Y%m%d_%H%M%S}.db"

    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        result = dst.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if result != "ok":
        raise RuntimeError(f"Backup {dest} failed its integrity check: {result}")

    # Timestamps sort chronologically, so the oldest come first.
    backups = sorted(backup_dir.glob(f"{db_path.stem}_????????_??????.db"))
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink()
        print(f"Deleted old backup {old}")
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", type=int, default=30, help="backups to keep (default 30, 0 = all)")
    parser.add_argument("--config", default="rlgymstream.toml", help="config file (default rlgymstream.toml)")
    args = parser.parse_args()

    db_path = db_path_from_config(Path(args.config))
    if not db_path.exists():
        print(f"No database at {db_path}, nothing to back up")
        return 0
    dest = backup(db_path, db_path.parent / "backups", args.keep)
    print(f"Backed up {db_path} to {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
