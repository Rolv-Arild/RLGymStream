"""Data models for the Psyonix Stats API live game data."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class PlayerLiveStats:
    """Real-time stats for a single player, updated from UpdateState."""

    name: str = ""
    team_num: int = 0           # 0 = Blue, 1 = Orange
    shortcut: int = 0

    # Cumulative match stats (always present)
    score: int = 0
    goals: int = 0
    shots: int = 0
    assists: int = 0
    saves: int = 0
    touches: int = 0
    car_touches: int = 0
    demos: int = 0

    # SPECTATOR-only real-time fields
    speed: float = 0.0          # Unreal Units/second
    boost: int = 0              # 0–100
    is_boosting: bool = False
    is_on_ground: bool = False
    is_on_wall: bool = False
    is_powersliding: bool = False
    is_demolished: bool = False
    is_supersonic: bool = False
    has_car: bool = True

    # Attacker info when demolished (name of the player who demo'd)
    attacker_name: str = ""

    # ── Accumulated stats for postgame analytics ──

    # Running accumulators (updated each tick)
    _tick_count: int = field(default=0, repr=False)
    _boost_sum: float = field(default=0.0, repr=False)
    _speed_sum: float = field(default=0.0, repr=False)
    _supersonic_ticks: int = field(default=0, repr=False)
    _ground_ticks: int = field(default=0, repr=False)
    _wall_ticks: int = field(default=0, repr=False)
    _air_ticks: int = field(default=0, repr=False)
    _demolished_ticks: int = field(default=0, repr=False)
    _boosting_ticks: int = field(default=0, repr=False)
    _powersliding_ticks: int = field(default=0, repr=False)
    _boost_consumed: float = field(default=0.0, repr=False)
    _prev_boost: int = field(default=-1, repr=False)
    _first_tick_time: float = field(default=0.0, repr=False)
    _last_tick_time: float = field(default=0.0, repr=False)

    def accumulate_tick(self) -> None:
        """Called once per UpdateState to build up averages."""
        self._tick_count += 1
        now = time.time()
        if self._first_tick_time == 0.0:
            self._first_tick_time = now
        self._last_tick_time = now
        self._boost_sum += self.boost
        self._speed_sum += self.speed
        # Track boost consumed (decrease in boost between ticks)
        if self._prev_boost >= 0 and self.boost < self._prev_boost:
            self._boost_consumed += self._prev_boost - self.boost
        self._prev_boost = self.boost
        if self.is_boosting:
            self._boosting_ticks += 1
        if self.is_supersonic:
            self._supersonic_ticks += 1
        if self.is_on_wall:
            self._wall_ticks += 1
        elif self.is_on_ground:
            self._ground_ticks += 1
        else:
            self._air_ticks += 1
        if self.is_demolished:
            self._demolished_ticks += 1
        if self.is_powersliding:
            self._powersliding_ticks += 1

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "team_num": self.team_num,
            "score": self.score,
            "goals": self.goals,
            "shots": self.shots,
            "assists": self.assists,
            "saves": self.saves,
            "touches": self.touches,
            "car_touches": self.car_touches,
            "demos": self.demos,
            "speed": round(self.speed, 1),
            "boost": self.boost,
            "is_boosting": self.is_boosting,
            "is_on_ground": self.is_on_ground,
            "is_on_wall": self.is_on_wall,
            "is_powersliding": self.is_powersliding,
            "is_demolished": self.is_demolished,
            "is_supersonic": self.is_supersonic,
            "has_car": self.has_car,
            "attacker_name": self.attacker_name,
        }

    def postgame_summary(self) -> dict:
        """Return accumulated averages for postgame display."""
        t = max(self._tick_count, 1)
        duration_minutes = max(self._last_tick_time - self._first_tick_time, 1.0) / 60.0
        return {
            "name": self.name,
            "team_num": self.team_num,
            "score": self.score,
            "goals": self.goals,
            "shots": self.shots,
            "assists": self.assists,
            "saves": self.saves,
            "touches": self.touches,
            "car_touches": self.car_touches,
            "demos": self.demos,
            "avg_boost": round(self._boost_sum / t, 1),
            "avg_speed": round(self._speed_sum / t, 1),
            "pct_supersonic": round(100 * self._supersonic_ticks / t, 1),
            "pct_ground": round(100 * self._ground_ticks / t, 1),
            "pct_wall": round(100 * self._wall_ticks / t, 1),
            "pct_air": round(100 * self._air_ticks / t, 1),
            "pct_demolished": round(100 * self._demolished_ticks / t, 1),
            "bpm": round(self._boost_consumed / duration_minutes, 1),
            # Raw frame counts for database storage
            "total_frames": self._tick_count,
            "frames_boosting": self._boosting_ticks,
            "frames_ground": self._ground_ticks,
            "frames_wall": self._wall_ticks,
            "frames_air": self._air_ticks,
            "frames_supersonic": self._supersonic_ticks,
            "frames_demolished": self._demolished_ticks,
            "frames_powersliding": self._powersliding_ticks,
            "boost_consumed": round(self._boost_consumed, 1),
        }


@dataclass
class GameLiveState:
    """Real-time game metadata from UpdateState."""

    time_seconds: int = 300     # seconds remaining
    is_overtime: bool = False
    ball_speed: float = 0.0
    ball_team_num: int = -1     # -1 = neutral, 0 = Blue, 1 = Orange
    score_blue: int = 0
    score_orange: int = 0
    is_replay: bool = False
    has_winner: bool = False
    winner: str = ""
    arena: str = ""
    frame: int = 0
    elapsed: float = 0.0
    has_target: bool = False
    target_name: str = ""
    target_team_num: int = -1

    def to_dict(self) -> dict:
        return {
            "time_seconds": self.time_seconds,
            "is_overtime": self.is_overtime,
            "ball_speed": round(self.ball_speed, 1),
            "ball_team_num": self.ball_team_num,
            "score_blue": self.score_blue,
            "score_orange": self.score_orange,
            "is_replay": self.is_replay,
            "has_winner": self.has_winner,
            "winner": self.winner,
            "arena": self.arena,
            "has_target": self.has_target,
            "target_name": self.target_name,
            "target_team_num": self.target_team_num,
        }


@dataclass
class EventEntry:
    """A single event from the Stats API event feed."""

    timestamp: float = field(default_factory=time.time)
    event_type: str = ""        # "goal", "statfeed", "ball_hit", etc.
    event_name: str = ""        # e.g. "Demolish", "Save", "Epic Save", "Goal"
    primary: str = ""           # main player name
    secondary: str = ""         # secondary player name (e.g. assister, demo victim)
    team_num: int = -1          # team of the primary player
    details: dict = field(default_factory=dict)  # extra data (goal_speed, etc.)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "event_name": self.event_name,
            "primary": self.primary,
            "secondary": self.secondary,
            "team_num": self.team_num,
            "details": self.details,
        }


# Maximum number of events to keep in the ticker
_MAX_EVENTS = 50


@dataclass
class LiveMatchStats:
    """Aggregated live stats for the current match."""

    players: dict[str, PlayerLiveStats] = field(default_factory=dict)
    game: GameLiveState = field(default_factory=GameLiveState)
    events: list[EventEntry] = field(default_factory=list)
    match_guid: str = ""

    def clear(self) -> None:
        """Reset for a new match."""
        self.players.clear()
        self.game = GameLiveState()
        self.events.clear()
        self.match_guid = ""


    def add_event(self, event: EventEntry) -> None:
        # The Stats API fires StatfeedEvent once per player in the match
        # for the same action (e.g. "Aerial Goal" × 4 in a 2v2).
        # Deduplicate: skip if an identical event_type+event_name+primary
        # was added within the last 0.5 seconds.
        if self.events:
            for recent in reversed(self.events[-5:]):
                if (time.time() - recent.timestamp) > 0.5:
                    break
                if (recent.event_type == event.event_type
                        and recent.event_name == event.event_name
                        and recent.primary == event.primary
                        and recent.secondary == event.secondary):
                    return  # duplicate, skip

        self.events.append(event)
        if len(self.events) > _MAX_EVENTS:
            self.events = self.events[-_MAX_EVENTS:]

    def to_dict(self) -> dict:
        return {
            "players": {k: v.to_dict() for k, v in self.players.items()},
            "game": self.game.to_dict(),
            "events": [e.to_dict() for e in self.events],
        }

    def postgame_to_dict(self) -> dict:
        """Return postgame summary with accumulated per-player analytics."""
        return {
            "players": {k: v.postgame_summary() for k, v in self.players.items()},
            "events": [e.to_dict() for e in self.events],
        }

