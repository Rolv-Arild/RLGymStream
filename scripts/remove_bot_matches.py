"""Remove all matches and ratings for a specific bot.

Deletes every match the bot participated in (on either team) and removes
its ratings.  Run recalculate_ratings.py afterwards if you want to
recompute the remaining bots' ratings from scratch.

Usage:
    python scripts/remove_bot_matches.py "Bot Name"
    python scripts/remove_bot_matches.py "Bot Name" --dry-run
    python scripts/remove_bot_matches.py "Bot Name" --db data/rlgymstream.db
"""

import sys
from pathlib import Path

from rlgymstream.db.database import Database


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv

    if not args:
        print("Usage: python scripts/remove_bot_matches.py \"Bot Name\" [--dry-run] [--db path]")
        sys.exit(1)

    bot_name = args[0]

    db_path = Path("data/rlgymstream.db")
    for i, a in enumerate(sys.argv[1:]):
        if a == "--db" and i + 2 < len(sys.argv):
            db_path = Path(sys.argv[i + 2])

    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    db = Database(db_path)
    bot = db.get_bot_by_name(bot_name)
    if bot is None:
        print(f"Bot not found: {bot_name}")
        all_bots = db.get_all_bots(enabled_only=False)
        print(f"Available bots: {', '.join(b.name for b in all_bots)}")
        sys.exit(1)

    assert bot.id is not None
    bot_id = bot.id
    print(f"Bot: {bot.name} (id={bot_id}, enabled={bot.enabled})")

    # Find all matches involving this bot
    with db._conn() as conn:
        rows = conn.execute("SELECT * FROM matches ORDER BY id ASC").fetchall()

    involved = []
    for row in rows:
        blue_ids = [int(x) for x in row[4].split(",")]
        orange_ids = [int(x) for x in row[5].split(",")]
        if bot_id in blue_ids or bot_id in orange_ids:
            involved.append(row)

    print(f"Matches involving {bot.name}: {len(involved)}")
    print(f"Total matches in DB: {len(rows)}")

    # Show match breakdown by mode
    modes = {}
    for row in involved:
        mode = row[1]
        modes[mode] = modes.get(mode, 0) + 1
    for mode, count in sorted(modes.items()):
        print(f"  {mode}: {count} matches")

    if dry_run:
        print("\nDry run -- no changes made.")
        return

    confirm = input(f"\nDelete {len(involved)} matches and all ratings for {bot.name}? [y/N] ")
    if confirm.lower() != "y":
        print("Cancelled.")
        return

    # Delete matches involving this bot
    with db._conn() as conn:
        for row in involved:
            conn.execute("DELETE FROM matches WHERE id=?", (row[0],))
        print(f"Deleted {len(involved)} matches.")

    # Delete ratings for this bot
    with db._conn() as conn:
        conn.execute("DELETE FROM ratings WHERE bot_id=?", (bot_id,))
        print(f"Deleted ratings for {bot.name}.")

    print(f"\nDone. Run 'python scripts/recalculate_ratings.py' to recompute ratings.")


if __name__ == "__main__":
    main()

