"""SQLite database layer."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from contextlib import contextmanager
from typing import Generator

from rlgymstream.db.models import Bot, Rating, Match, MatchPlayerStats

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

CREATE TABLE IF NOT EXISTS match_player_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL,
    bot_id INTEGER,
    player_name TEXT NOT NULL DEFAULT '',
    team_num INTEGER NOT NULL DEFAULT 0,
    score INTEGER NOT NULL DEFAULT 0,
    goals INTEGER NOT NULL DEFAULT 0,
    shots INTEGER NOT NULL DEFAULT 0,
    assists INTEGER NOT NULL DEFAULT 0,
    saves INTEGER NOT NULL DEFAULT 0,
    demos INTEGER NOT NULL DEFAULT 0,
    touches INTEGER NOT NULL DEFAULT 0,
    avg_boost REAL NOT NULL DEFAULT 0.0,
    avg_speed REAL NOT NULL DEFAULT 0.0,
    pct_supersonic REAL NOT NULL DEFAULT 0.0,
    pct_ground REAL NOT NULL DEFAULT 0.0,
    pct_wall REAL NOT NULL DEFAULT 0.0,
    pct_air REAL NOT NULL DEFAULT 0.0,
    pct_demolished REAL NOT NULL DEFAULT 0.0,
    FOREIGN KEY (match_id) REFERENCES matches(id),
    FOREIGN KEY (bot_id) REFERENCES bots(id)
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

    def get_pairwise_h2h(self, mode: str) -> dict[tuple[int, int], tuple[int, int]]:
        """Return pairwise win counts for every bot pair in a mode.

        Built in a single pass through the match history.
        Keys are ``(min_id, max_id)``.
        Values are ``(wins_for_min_id, wins_for_max_id)``.
        Draws are ignored.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT team_blue_ids, team_orange_ids, winner FROM matches WHERE mode=?",
                (mode,),
            ).fetchall()

        h2h: dict[tuple[int, int], list[int]] = {}
        for row in rows:
            winner = row["winner"]
            if winner not in ("blue", "orange"):
                continue
            blue_ids = {int(x) for x in row["team_blue_ids"].split(",")}
            orange_ids = {int(x) for x in row["team_orange_ids"].split(",")}
            for blue_id in blue_ids:
                for orange_id in orange_ids:
                    if blue_id == orange_id:
                        continue
                    key = (min(blue_id, orange_id), max(blue_id, orange_id))
                    if key not in h2h:
                        h2h[key] = [0, 0]
                    winning_id = blue_id if winner == "blue" else orange_id
                    if winning_id == key[0]:
                        h2h[key][0] += 1
                    else:
                        h2h[key][1] += 1

        return {k: (v[0], v[1]) for k, v in h2h.items()}

    # ── Match Player Stats ────────────────────────────────────────────

    def save_match_player_stats(self, stats: list[MatchPlayerStats]) -> None:
        """Persist post-game per-player stats for a match."""
        if not stats:
            return
        with self._conn() as conn:
            conn.executemany(
                """INSERT INTO match_player_stats
                   (match_id, bot_id, player_name, team_num,
                    score, goals, shots, assists, saves, demos, touches,
                    avg_boost, avg_speed, pct_supersonic, pct_ground,
                    pct_wall, pct_air, pct_demolished)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (s.match_id, s.bot_id, s.player_name, s.team_num,
                     s.score, s.goals, s.shots, s.assists, s.saves,
                     s.demos, s.touches, s.avg_boost, s.avg_speed,
                     s.pct_supersonic, s.pct_ground, s.pct_wall,
                     s.pct_air, s.pct_demolished)
                    for s in stats
                ],
            )

    def get_match_player_stats(self, match_id: int) -> list[MatchPlayerStats]:
        """Return all player stats rows for a given match."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM match_player_stats WHERE match_id=? ORDER BY team_num, id",
                (match_id,),
            ).fetchall()
            return [_row_to_match_player_stats(r) for r in rows]

    def get_bot_avg_stats(self, bot_id: int, mode: str | None = None,
                          limit: int | None = None) -> dict | None:
        """Return averaged post-game stats for a bot across recent matches.

        Returns None if no stats exist.
        """
        with self._conn() as conn:
            q = """
                SELECT
                    COUNT(*) as n,
                    AVG(mps.score) as avg_score,
                    AVG(mps.goals) as avg_goals,
                    AVG(mps.shots) as avg_shots,
                    AVG(mps.assists) as avg_assists,
                    AVG(mps.saves) as avg_saves,
                    AVG(mps.demos) as avg_demos,
                    AVG(mps.touches) as avg_touches,
                    AVG(mps.avg_boost) as avg_boost,
                    AVG(mps.avg_speed) as avg_speed,
                    AVG(mps.pct_supersonic) as avg_pct_supersonic,
                    AVG(mps.pct_ground) as avg_pct_ground,
                    AVG(mps.pct_wall) as avg_pct_wall,
                    AVG(mps.pct_air) as avg_pct_air
                FROM match_player_stats mps
                JOIN matches m ON mps.match_id = m.id
                WHERE mps.bot_id = ?
            """
            params: list = [bot_id]
            if mode:
                q += " AND m.mode = ?"
                params.append(mode)
            if limit:
                # Average over only the N most recent matches
                q = q.replace(
                    "FROM match_player_stats mps",
                    "FROM (SELECT * FROM match_player_stats WHERE bot_id = ? ORDER BY match_id DESC LIMIT ?) mps",
                )
                params = [bot_id, limit] + params[1:]  # bot_id + limit first, then mode
            row = conn.execute(q, params).fetchone()
            if not row or row["n"] == 0:
                return None
            return {
                "matches": row["n"],
                "avg_score": round(row["avg_score"], 1),
                "avg_goals": round(row["avg_goals"], 2),
                "avg_shots": round(row["avg_shots"], 2),
                "avg_assists": round(row["avg_assists"], 2),
                "avg_saves": round(row["avg_saves"], 2),
                "avg_demos": round(row["avg_demos"], 2),
                "avg_touches": round(row["avg_touches"], 1),
                "avg_boost": round(row["avg_boost"], 1),
                "avg_speed": round(row["avg_speed"], 1),
                "pct_supersonic": round(row["avg_pct_supersonic"], 1),
                "pct_ground": round(row["avg_pct_ground"], 1),
                "pct_wall": round(row["avg_pct_wall"], 1),
                "pct_air": round(row["avg_pct_air"], 1),
            }

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
        goals_a, goals_b = 0, 0
        total_duration = 0.0
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
            total_duration += m.duration_seconds

            # Attribute team goals to each bot
            if a_in_blue:
                goals_a += m.score_blue
                goals_b += m.score_orange
            else:
                goals_a += m.score_orange
                goals_b += m.score_blue

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
            "goals_a": goals_a,
            "goals_b": goals_b,
            "total_duration": total_duration,
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


def _row_to_match_player_stats(row: sqlite3.Row) -> MatchPlayerStats:
    return MatchPlayerStats(
        id=row["id"], match_id=row["match_id"],
        bot_id=row["bot_id"], player_name=row["player_name"],
        team_num=row["team_num"], score=row["score"],
        goals=row["goals"], shots=row["shots"],
        assists=row["assists"], saves=row["saves"],
        demos=row["demos"], touches=row["touches"],
        avg_boost=row["avg_boost"], avg_speed=row["avg_speed"],
        pct_supersonic=row["pct_supersonic"], pct_ground=row["pct_ground"],
        pct_wall=row["pct_wall"], pct_air=row["pct_air"],
        pct_demolished=row["pct_demolished"],
    )

