"""Recalculate all ratings by replaying match history.

Use this after fixing a bug in the rating update logic.  It resets every
rating to defaults (mu=25, sigma=25/3, matches_played=0) and then
re-processes every recorded match in chronological order using the
current update_ratings implementation.

Usage:
    python scripts/recalculate_ratings.py              # uses data/rlgymstream.db
    python scripts/recalculate_ratings.py path/to.db   # custom db path
    python scripts/recalculate_ratings.py --dry-run    # preview changes without modifying
"""

import shutil
import sys
import tempfile
from pathlib import Path

from rlgymstream.db.database import Database
from rlgymstream.db.models import Rating
from rlgymstream.matchmaking.ratings import update_ratings


def _get_all_ratings(db: Database) -> dict[tuple[int, str], Rating]:
    """Return {(bot_id, mode): Rating} for every rating in the database."""
    result = {}
    for mode in ["1v1", "2v2", "3v3", "solo_2v2", "solo_3v3"]:
        for r in db.get_ratings_for_mode(mode):
            result[(r.bot_id, r.mode)] = r
    return result


def _replay(db: Database) -> int:
    """Reset ratings and replay all matches. Returns match count."""
    with db._conn() as conn:
        rows = conn.execute("SELECT * FROM matches ORDER BY id ASC").fetchall()

    total = len(rows)
    if total == 0:
        return 0

    with db._conn() as conn:
        conn.execute("DELETE FROM ratings")

    for i, row in enumerate(rows, 1):
        mode = row[1]
        team_blue_ids = [int(x) for x in row[4].split(",")]
        team_orange_ids = [int(x) for x in row[5].split(",")]
        winner = row[8]

        is_solo = mode.startswith("solo_")
        update_ratings(db, mode, team_blue_ids, team_orange_ids, winner,
                       is_solo_queue=is_solo)

        if i % 50 == 0 or i == total:
            print(f"  Replayed {i}/{total} matches...")

    return total


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv

    db_path = Path(args[0]) if args else Path("data/rlgymstream.db")
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    if dry_run:
        print(f"DRY RUN — working on a temporary copy of {db_path}\n")

        # Snapshot current ratings
        db_orig = Database(db_path)
        bots = {b.id: b.name for b in db_orig.get_all_bots()}
        old_ratings = _get_all_ratings(db_orig)

        # Copy database to temp file and replay there
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        tmp_path = Path(tmp.name)
        shutil.copy2(db_path, tmp_path)

        try:
            db_tmp = Database(tmp_path)
            total = _replay(db_tmp)
            if total == 0:
                print("No matches to replay.")
                return

            new_ratings = _get_all_ratings(db_tmp)

            # Show before/after comparison
            all_keys = sorted(set(old_ratings) | set(new_ratings))
            for mode in ["1v1", "2v2", "3v3", "solo_2v2", "solo_3v3"]:
                mode_keys = [k for k in all_keys if k[1] == mode]
                if not mode_keys:
                    continue
                print(f"\n=== {mode} ===")
                print(f"{'Bot':<25} {'old MMR':>8} {'new MMR':>8} {'delta':>7}  {'old σ':>7} {'new σ':>7} {'Δσ':>7}")
                print("-" * 80)
                for key in sorted(mode_keys, key=lambda k: -(new_ratings.get(k, Rating()).mu - 3 * new_ratings.get(k, Rating()).sigma)):
                    bot_id, _ = key
                    name = bots.get(bot_id, f"#{bot_id}")
                    old = old_ratings.get(key, Rating())
                    new = new_ratings.get(key, Rating())
                    old_mmr = round(20 * (old.mu - 3 * old.sigma) + 1000)
                    new_mmr = round(20 * (new.mu - 3 * new.sigma) + 1000)
                    delta = new_mmr - old_mmr
                    d_sigma = new.sigma - old.sigma
                    print(f"{name:<25} {old_mmr:8d} {new_mmr:8d} {delta:+7d}  {old.sigma:7.2f} {new.sigma:7.2f} {d_sigma:+7.2f}")

            print(f"\nNo changes made to {db_path}.")
        finally:
            tmp_path.unlink(missing_ok=True)
    else:
        print(f"Recalculating ratings in {db_path}...")
        db = Database(db_path)
        total = _replay(db)
        if total == 0:
            print("No matches to replay.")
            return

        print(f"\nDone! Recalculated ratings from {total} matches.")

        for mode in ["1v1", "2v2", "3v3", "solo_2v2", "solo_3v3"]:
            ratings = db.get_ratings_for_mode(mode)
            if ratings:
                print(f"\n  {mode}: {len(ratings)} rated bots")


if __name__ == "__main__":
    main()

