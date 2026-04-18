"""Rename a bot in the database.

Usage:
    python scripts/rename_bot.py "Old Name" "New Name"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rlgymstream.config import AppConfig
from rlgymstream.db.database import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="Rename a bot in the database")
    parser.add_argument("old_name", help="Current bot name")
    parser.add_argument("new_name", help="New bot name")
    args = parser.parse_args()

    config = AppConfig.from_toml()
    db = Database(config.db_path)

    bot = db.get_bot_by_name(args.old_name)
    if bot is None:
        print(f"Error: No bot named '{args.old_name}' found in the database.")
        sys.exit(1)

    existing = db.get_bot_by_name(args.new_name)
    if existing is not None:
        print(f"Error: A bot named '{args.new_name}' already exists (id={existing.id}).")
        sys.exit(1)

    # Update the name directly in the database
    with db._conn() as conn:
        conn.execute("UPDATE bots SET name=? WHERE id=?", (args.new_name, bot.id))

    print(f"Renamed bot #{bot.id}: '{args.old_name}' → '{args.new_name}'")


if __name__ == "__main__":
    main()

