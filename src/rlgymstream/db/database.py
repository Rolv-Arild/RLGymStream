"""SQLite database layer."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from contextlib import contextmanager
from typing import Generator

from rlgymstream.db.models import Bot, Rating, Match

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    config_path TEXT NOT NULL,
    logo_path TEXT,
    description TEXT NOT NULL DEFAULT '',
    fun_fact TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    supported_modes TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS ratings (
    bot_id INTEGER NOT NULL,
    mode TEXT NOT NULL,
    mu REAL NOT NULL DEFAULT 25.0,
    sigma REAL NOT NULL DEFAULT 8.333333333333334,
    matches_played INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bot_id, mode),
    FOREIGN KEY (bot_id) REFERENCES bots(id)
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    map_name TEXT NOT NULL DEFAULT '',
    timestamp TEXT NOT NULL,
    team_blue_ids TEXT NOT NULL,
    team_orange_ids TEXT NOT NULL,
    score_blue INTEGER NOT NULL DEFAULT 0,
    score_orange INTEGER NOT NULL DEFAULT 0,
    winner TEXT NOT NULL DEFAULT '',
    duration_seconds REAL NOT NULL DEFAULT 0.0
);
"""


class Database:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ── Bots ──────────────────────────────────────────────────────────

    def upsert_bot(self, bot: Bot) -> Bot:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO bots (name, author, config_path, logo_path, description, fun_fact, language, supported_modes, enabled)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       author=excluded.author,
                       config_path=excluded.config_path,
                       logo_path=excluded.logo_path,
                       description=excluded.description,
                       fun_fact=excluded.fun_fact,
                       language=excluded.language,
                       supported_modes=excluded.supported_modes,
                       enabled=excluded.enabled
                   RETURNING id""",
                (bot.name, bot.author, bot.config_path, bot.logo_path,
                 bot.description, bot.fun_fact, bot.language,
                 bot.supported_modes, int(bot.enabled)),
            )
            bot.id = cur.fetchone()[0]
        return bot

    def get_bot(self, bot_id: int) -> Bot | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
            return _row_to_bot(row) if row else None

    def get_bot_by_name(self, name: str) -> Bot | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM bots WHERE name=?", (name,)).fetchone()
            return _row_to_bot(row) if row else None

    def get_all_bots(self, enabled_only: bool = True) -> list[Bot]:
        with self._conn() as conn:
            q = "SELECT * FROM bots"
            if enabled_only:
                q += " WHERE enabled=1"
            rows = conn.execute(q).fetchall()
            return [_row_to_bot(r) for r in rows]

    # ── Ratings ───────────────────────────────────────────────────────

    def get_rating(self, bot_id: int, mode: str) -> Rating:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM ratings WHERE bot_id=? AND mode=?",
                (bot_id, mode),
            ).fetchone()
            if row:
                return _row_to_rating(row)
            # Return default — import here to avoid circular imports
            from rlgymstream.matchmaking.ratings import get_default_mu, get_default_sigma
            return Rating(bot_id=bot_id, mode=mode,
                          mu=get_default_mu(mode), sigma=get_default_sigma(mode))

    def get_ratings_for_mode(self, mode: str) -> list[Rating]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM ratings WHERE mode=? ORDER BY (mu - 3*sigma) DESC",
                (mode,),
            ).fetchall()
            return [_row_to_rating(r) for r in rows]

    def save_rating(self, rating: Rating) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO ratings (bot_id, mode, mu, sigma, matches_played)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(bot_id, mode) DO UPDATE SET
                       mu=excluded.mu,
                       sigma=excluded.sigma,
                       matches_played=excluded.matches_played""",
                (rating.bot_id, rating.mode, rating.mu, rating.sigma,
                 rating.matches_played),
            )

    # ── Matches ───────────────────────────────────────────────────────

    def save_match(self, match: Match) -> Match:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO matches
                   (mode, map_name, timestamp, team_blue_ids, team_orange_ids,
                    score_blue, score_orange, winner, duration_seconds)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   RETURNING id""",
                (match.mode, match.map_name, match.timestamp,
                 match.team_blue_ids, match.team_orange_ids,
                 match.score_blue, match.score_orange,
                 match.winner, match.duration_seconds),
            )
            match.id = cur.fetchone()[0]
        return match

    def get_match_count(self) -> int:
        """Return the total number of matches in the database."""
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM matches").fetchone()
            return row[0]

    def get_recent_matches(self, limit: int | None = 20, mode: str | None = None) -> list[Match]:
        with self._conn() as conn:
            q = "SELECT * FROM matches"
            params: list = []
            if mode:
                q += " WHERE mode=?"
                params.append(mode)
            q += " ORDER BY id DESC"
            if limit is not None:
                q += " LIMIT ?"
                params.append(limit)
            rows = conn.execute(q, params).fetchall()
            return [_row_to_match(r) for r in rows]

    def get_bot_record(self, bot_id: int, mode: str) -> tuple[int, int, int]:
        """Return (wins, losses, draws) for a bot in a mode."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT team_blue_ids, team_orange_ids, winner FROM matches WHERE mode=?",
                (mode,),
            ).fetchall()
        bid = str(bot_id)
        wins = losses = draws = 0
        for row in rows:
            blue_ids = row["team_blue_ids"].split(",")
            orange_ids = row["team_orange_ids"].split(",")
            in_blue = bid in blue_ids
            in_orange = bid in orange_ids
            if not (in_blue or in_orange):
                continue
            winner = row["winner"]
            if winner == "draw":
                draws += 1
            elif (winner == "blue" and in_blue) or (winner == "orange" and in_orange):
                wins += 1
            else:
                losses += 1
        return wins, losses, draws

    def get_head_to_head(self, bot_a_id: int, bot_b_id: int,
                         mode: str | None = None) -> dict:

        """Return win/loss/draw between two bots (works for 1v1 and team modes)."""
        with self._conn() as conn:
            q = "SELECT * FROM matches WHERE 1=1"
            params: list = []
            if mode:
                q += " AND mode=?"
                params.append(mode)
            rows = conn.execute(q, params).fetchall()

        a_str = str(bot_a_id)
        b_str = str(bot_b_id)
        wins_a, wins_b, draws = 0, 0, 0
        matches_list: list[Match] = []

        for row in rows:
            m = _row_to_match(row)
            blue_ids = set(m.team_blue_ids.split(","))
            orange_ids = set(m.team_orange_ids.split(","))
            a_in_blue = a_str in blue_ids
            a_in_orange = a_str in orange_ids
            b_in_blue = b_str in blue_ids
            b_in_orange = b_str in orange_ids

            if not ((a_in_blue or a_in_orange) and (b_in_blue or b_in_orange)):
                continue
            # They must be on opposite teams
            if (a_in_blue and b_in_blue) or (a_in_orange and b_in_orange):
                continue

            matches_list.append(m)
            if m.winner == "draw":
                draws += 1
            elif m.winner == "blue":
                if a_in_blue:
                    wins_a += 1
                else:
                    wins_b += 1
            elif m.winner == "orange":
                if a_in_orange:
                    wins_a += 1
                else:
                    wins_b += 1

        return {
            "wins_a": wins_a,
            "wins_b": wins_b,
            "draws": draws,
            "total": len(matches_list),
            "matches": matches_list[-10:],  # last 10
        }


# ── Row converters ────────────────────────────────────────────────────

def _row_to_bot(row: sqlite3.Row) -> Bot:
    return Bot(
        id=row["id"], name=row["name"], author=row["author"],
        config_path=row["config_path"], logo_path=row["logo_path"],
        description=row["description"], fun_fact=row["fun_fact"],
        language=row["language"], supported_modes=row["supported_modes"],
        enabled=bool(row["enabled"]),
    )


def _row_to_rating(row: sqlite3.Row) -> Rating:
    return Rating(
        bot_id=row["bot_id"], mode=row["mode"],
        mu=row["mu"], sigma=row["sigma"],
        matches_played=row["matches_played"],
    )


def _row_to_match(row: sqlite3.Row) -> Match:
    return Match(
        id=row["id"], mode=row["mode"], map_name=row["map_name"],
        timestamp=row["timestamp"],
        team_blue_ids=row["team_blue_ids"],
        team_orange_ids=row["team_orange_ids"],
        score_blue=row["score_blue"], score_orange=row["score_orange"],
        winner=row["winner"], duration_seconds=row["duration_seconds"],
    )

