"""View raw ratings for all bots in the database."""

import sys
from pathlib import Path

from rlgymstream.db.database import Database


def main():
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/rlgymstream.db")
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    db = Database(db_path)
    bots = {b.id: b.name for b in db.get_all_bots()}

    for mode in ["1v1", "2v2", "3v3", "solo_2v2", "solo_3v3"]:
        ratings = db.get_ratings_for_mode(mode)
        if not ratings:
            continue
        print(f"\n=== {mode} ===")
        print(f"{'Bot':<25} {'mu':>8} {'sigma':>8} {'mu-3s':>8} {'MMR':>6} {'Games':>5}")
        print("-" * 65)
        for r in sorted(ratings, key=lambda x: x.mu - 3 * x.sigma, reverse=True):
            name = bots.get(r.bot_id, f"#{r.bot_id}")
            display = r.mu - 3 * r.sigma
            mmr = round(20 * display + 1000)
            print(f"{name:<25} {r.mu:8.2f} {r.sigma:8.2f} {display:8.2f} {mmr:6d} {r.matches_played:5d}")


if __name__ == "__main__":
    main()

