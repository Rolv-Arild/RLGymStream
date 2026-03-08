"""Query head-to-head match history between two bots."""

import sys
from pathlib import Path

from rlgymstream.db.database import Database


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <bot_a_name> <bot_b_name>")
        sys.exit(1)

    name_a, name_b = sys.argv[1], sys.argv[2]
    db = Database(Path("data/rlgymstream.db"))

    a = db.get_bot_by_name(name_a)
    b = db.get_bot_by_name(name_b)

    if not a:
        print(f'Bot "{name_a}" not found')
        sys.exit(1)
    if not b:
        print(f'Bot "{name_b}" not found')
        sys.exit(1)

    h2h = db.get_head_to_head(a.id, b.id)
    print(f"{a.name} vs {b.name}: {h2h['wins_a']}W - {h2h['draws']}D - {h2h['wins_b']}L  ({h2h['total']} total)")
    print()

    for m in h2h["matches"]:
        blue_ids = m.team_blue_ids.split(",")
        a_side = "blue" if str(a.id) in blue_ids else "orange"
        print(
            f"  [{m.mode}] {m.map_name}: Blue {m.score_blue}-{m.score_orange} Orange"
            f"  (winner: {m.winner}, {a.name} on {a_side})"
        )


if __name__ == "__main__":
    main()

