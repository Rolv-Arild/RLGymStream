"""Remove all matches and ratings for a specific bot.

Deletes every match the bot participated in (on either team), removes
its ratings, and then recalculates all remaining ratings from scratch
so the other bots' ratings are consistent.

Usage:
    python scripts/remove_bot_matches.py "Bot Name"
    python scripts/remove_bot_matches.py "Bot Name" --dry-run
    python scripts/remove_bot_matches.py "Bot Name" --db data/rlgymstream.db
"""

import sys
from pathlib import Path

from rlgymstream.config import AppConfig
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
    not_involved = []
    for row in rows:
        blue_ids = [int(x) for x in row[4].split(",")]
        orange_ids = [int(x) for x in row[5].split(",")]
        if bot_id in blue_ids or bot_id in orange_ids:
            involved.append(row)
        else:
            not_involved.append(row)

    print(f"Matches involving {bot.name}: {len(involved)}")
    print(f"Matches not involving {bot.name}: {len(not_involved)}")

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

    # Recalculate all remaining ratings
    print("\nRecalculating ratings from remaining matches...")
    from rlgymstream.matchmaking.ratings import update_ratings, configure_defaults

    config = AppConfig.from_toml()
    configure_defaults(config.default_mu, config.default_sigma)

    ALL_MODES = ["1v1", "2v2", "3v3", "solo_2v2", "solo_3v3"]

    with db._conn() as conn:
        conn.execute("DELETE FROM ratings")

    # Seed anchored bots
    anchored_per_mode: dict[str, set[int]] = {m: set() for m in ALL_MODES}
    for anchor in config.anchored_ratings:
        abot = db.get_bot_by_name(anchor.bot_name)
        if abot and abot.id is not None:
            target_modes = anchor.modes if anchor.modes else ALL_MODES
            for mode in target_modes:
                if mode in anchored_per_mode:
                    anchored_per_mode[mode].add(abot.id)
                    r = db.get_rating(abot.id, mode)
                    r.mu = anchor.mu
                    r.sigma = anchor.sigma
                    db.save_rating(r)

    with db._conn() as conn:
        remaining = conn.execute("SELECT * FROM matches ORDER BY id ASC").fetchall()

    total = len(remaining)
    for i, row in enumerate(remaining, 1):
        mode = row[1]
        team_blue_ids = [int(x) for x in row[4].split(",")]
        team_orange_ids = [int(x) for x in row[5].split(",")]
        winner = row[8]

        is_solo = mode.startswith("solo_")
        update_ratings(db, mode, team_blue_ids, team_orange_ids, winner,
                       is_solo_queue=is_solo,
                       anchored_bot_ids=anchored_per_mode.get(mode, set()))

        if i % 50 == 0 or i == total:
            print(f"  Replayed {i}/{total} matches...")

    print(f"\nDone. Removed {bot.name} from {len(involved)} matches and recalculated ratings from {total} remaining matches.")


if __name__ == "__main__":
    main()

