"""TCP client for the Psyonix Rocket League Stats API.

The Stats API opens a raw TCP socket that streams newline-delimited JSON
messages.  Despite the documentation calling it a "web socket", there is
no HTTP upgrade or WebSocket framing — it is plain TCP.

This client connects via asyncio streams, parses each JSON message, and
updates a shared LiveMatchStats instance that the overlay reads.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

from rlgymstream.stats_api.models import (
    EventEntry,
    LiveMatchStats,
    PlayerLiveStats,
)

logger = logging.getLogger("rlgymstream.stats_api")

# The Stats API Speed field is already in km/h — no conversion needed.
# (Supersonic ≈ 82.8 km/h, max ball speed ≈ 216 km/h.)

# Maximum size of a single JSON message (bytes).  Goal events with many
# players can be large; 4 MB is generous.
_MAX_MESSAGE_SIZE = 4 * 1024 * 1024


class StatsApiClient:
    """Async TCP client for the Psyonix Stats API.

    Usage::

        client = StatsApiClient(port=49123)
        task = asyncio.create_task(client.run())
        # ... later ...
        client.stop()
        await task
    """

    def __init__(
        self,
        port: int = 49123,
        on_update: Callable[[], None] | None = None,
    ) -> None:
        self._port = port
        self._on_update = on_update  # called after each state update
        self._live_stats = LiveMatchStats()
        self._running = False
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    @property
    def live_stats(self) -> LiveMatchStats:
        return self._live_stats

    # ── Lifecycle ─────────────────────────────────────────────

    async def run(self) -> None:
        """Connect and listen forever with auto-reconnect."""
        self._running = True
        backoff = 1.0
        max_backoff = 30.0

        while self._running:
            try:
                self._reader, self._writer = await asyncio.open_connection(
                    "localhost", self._port,
                )
                logger.info("Connected to Stats API at localhost:%d", self._port)
                backoff = 1.0
                await self._listen()
            except ConnectionRefusedError:
                if not self._running:
                    break
                if backoff <= 2.0:
                    logger.debug(
                        "Stats API not available at port %d, retrying in %.0fs…",
                        self._port, backoff,
                    )
            except (ConnectionResetError, asyncio.IncompleteReadError):
                if not self._running:
                    break
                logger.debug("Stats API connection lost, reconnecting…")
            except Exception:
                if not self._running:
                    break
                logger.warning("Stats API error, reconnecting…", exc_info=True)
            finally:
                self._close_writer()

            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, max_backoff)

    def stop(self) -> None:
        """Signal the client to disconnect and stop."""
        self._running = False
        self._close_writer()

    def _close_writer(self) -> None:
        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    def reset(self) -> None:
        """Clear live stats for a new match."""
        self._live_stats.clear()

    # ── Message handling ──────────────────────────────────────

    async def _listen(self) -> None:
        """Read streaming JSON messages from the TCP socket.

        The Stats API streams concatenated JSON objects with no guaranteed
        delimiter.  We use brace-depth counting to find object boundaries,
        which handles any whitespace/newline style between messages.
        """
        assert self._reader is not None
        buf = b""
        while self._running:
            chunk = await self._reader.read(65536)
            if not chunk:
                # Server closed the connection
                return
            buf += chunk

            # Extract complete JSON objects using brace counting.
            # Each top-level message is a { ... } object.
            while buf:
                # Skip any leading whitespace / newlines between messages
                stripped = buf.lstrip()
                if not stripped:
                    buf = b""
                    break
                if stripped[0:1] != b"{":
                    # Discard bytes until we find the start of a JSON object
                    idx = stripped.find(b"{")
                    if idx == -1:
                        buf = b""
                        break
                    stripped = stripped[idx:]
                buf = stripped

                # Count braces to find the end of the JSON object
                end = self._find_json_end(buf)
                if end == -1:
                    # Incomplete object — wait for more data
                    break

                raw = buf[:end]
                buf = buf[end:]

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                event = msg.get("Event", "")
                data = msg.get("Data", {})

                # The Stats API double-encodes the Data field as a JSON
                # string rather than a nested object.  Decode it.
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except json.JSONDecodeError:
                        data = {}

                handler = self._handlers.get(event)
                if handler:
                    handler(self, data)

                if self._on_update:
                    self._on_update()

            # Safety: if buffer grows too large without a complete object, discard
            if len(buf) > _MAX_MESSAGE_SIZE:
                logger.warning("Stats API buffer overflow (%d bytes), discarding", len(buf))
                buf = b""

    @staticmethod
    def _find_json_end(buf: bytes) -> int:
        """Return the index just past the first complete top-level JSON object.

        Uses brace counting.  Handles strings (skips braces inside quotes)
        and escaped characters.  Returns -1 if the object is incomplete.
        """
        depth = 0
        in_string = False
        escape = False
        for i, b in enumerate(buf):
            if escape:
                escape = False
                continue
            if b == 0x5C and in_string:  # backslash
                escape = True
                continue
            if b == 0x22:  # double quote
                in_string = not in_string
                continue
            if in_string:
                continue
            if b == 0x7B:  # {
                depth += 1
            elif b == 0x7D:  # }
                depth -= 1
                if depth == 0:
                    return i + 1
        return -1

    # ── Event handlers ────────────────────────────────────────

    def _handle_update_state(self, data: dict) -> None:
        """Process periodic UpdateState tick."""
        match_guid = data.get("MatchGuid", "")
        if match_guid and self._live_stats.match_guid and match_guid != self._live_stats.match_guid:
            # New match — clear old data
            self._live_stats.clear()
        self._live_stats.match_guid = match_guid

        # Update game state
        game_data = data.get("Game", {})
        g = self._live_stats.game
        g.time_seconds = game_data.get("TimeSeconds", g.time_seconds)
        g.is_overtime = game_data.get("bOvertime", g.is_overtime)
        g.is_replay = game_data.get("bReplay", g.is_replay)
        g.has_winner = game_data.get("bHasWinner", g.has_winner)
        g.winner = game_data.get("Winner", g.winner)
        g.arena = game_data.get("Arena", g.arena)
        g.frame = game_data.get("Frame", g.frame)
        g.elapsed = game_data.get("Elapsed", g.elapsed)
        g.has_target = game_data.get("bHasTarget", g.has_target)
        target = game_data.get("Target")
        if target:
            g.target_name = target.get("Name", "")
            g.target_team_num = target.get("TeamNum", -1)
        elif not g.has_target:
            g.target_name = ""
            g.target_team_num = -1

        ball = game_data.get("Ball", {})
        if ball:
            g.ball_speed = ball.get("Speed", g.ball_speed)
            g.ball_team_num = ball.get("TeamNum", g.ball_team_num)

        # Update team scores from game data
        for team in game_data.get("Teams", []):
            team_num = team.get("TeamNum", -1)
            score = team.get("Score", 0)
            if team_num == 0:
                g.score_blue = score
            elif team_num == 1:
                g.score_orange = score

        # Update per-player stats
        # Sort by TeamNum so blue (0) is processed before orange (1) — this
        # ensures our global duplicate naming matches the overlay's ordering.
        players_sorted = sorted(data.get("Players", []), key=lambda x: x.get("TeamNum", 0))
        for p in players_sorted:
            name = p.get("Name", "")
            if not name:
                continue
            team_num = p.get("TeamNum", 0)

            # Key by team:name to unambiguously identify each player slot,
            # even when cross-team duplicates share the same base name.
            key = f"{team_num}:{name}"

            if key not in self._live_stats.players:
                self._live_stats.players[key] = PlayerLiveStats(name=name)

            ps = self._live_stats.players[key]
            ps.team_num = team_num
            ps.shortcut = p.get("Shortcut", ps.shortcut)

            # Cumulative stats (always present)
            ps.score = p.get("Score", ps.score)
            ps.goals = p.get("Goals", ps.goals)
            ps.shots = p.get("Shots", ps.shots)
            ps.assists = p.get("Assists", ps.assists)
            ps.saves = p.get("Saves", ps.saves)
            ps.touches = p.get("Touches", ps.touches)
            ps.car_touches = p.get("CarTouches", ps.car_touches)
            ps.demos = p.get("Demos", ps.demos)

            # SPECTATOR-only fields (may not be present).
            # CONDITIONAL fields like bDemolished and bSupersonic are
            # *absent* when false rather than explicitly set to false,
            # so we default to the "off" state when missing.
            has_spectator_data = "Speed" in p or "Boost" in p
            if "Speed" in p:
                ps.speed = p["Speed"]
            if "Boost" in p:
                ps.boost = p["Boost"]
            ps.is_boosting = p.get("bBoosting", False)
            ps.is_powersliding = p.get("bPowersliding", False)
            ps.is_demolished = p.get("bDemolished", False)
            ps.is_supersonic = p.get("bSupersonic", False)
            if "bHasCar" in p:
                ps.has_car = p["bHasCar"]

            # Ground/wall/air are SPECTATOR fields — only update when
            # spectator data is present, otherwise they'd default to
            # ground=True which skews the postgame percentages.
            if has_spectator_data:
                ps.is_on_ground = p.get("bOnGround", False)
                ps.is_on_wall = p.get("bOnWall", False)

            # Attacker info
            attacker = p.get("Attacker")
            if attacker and ps.is_demolished:
                ps.attacker_name = attacker.get("Name", "")
            elif not ps.is_demolished:
                ps.attacker_name = ""

            # Accumulate for postgame analytics (only during live play, not replay,
            # and only when we have spectator data so percentages are accurate)
            if not g.is_replay and has_spectator_data:
                ps.accumulate_tick()

    def _handle_goal_scored(self, data: dict) -> None:
        scorer = data.get("Scorer", {})
        assister = data.get("Assister", {})
        goal_speed = data.get("GoalSpeed", 0)

        event = EventEntry(
            timestamp=time.time(),
            event_type="goal",
            event_name="Goal",
            primary=scorer.get("Name", ""),
            team_num=scorer.get("TeamNum", -1),
            secondary=assister.get("Name", "") if assister else "",
            details={
                "goal_speed": round(goal_speed, 1),
                "goal_speed_kmh": round(goal_speed, 1),
                "goal_time": data.get("GoalTime", 0),
            },
        )
        self._live_stats.add_event(event)

    def _handle_statfeed_event(self, data: dict) -> None:
        main_target = data.get("MainTarget", {})
        secondary = data.get("SecondaryTarget", {})
        event_name = data.get("Type", data.get("EventName", ""))

        event = EventEntry(
            timestamp=time.time(),
            event_type="statfeed",
            event_name=event_name,
            primary=main_target.get("Name", ""),
            team_num=main_target.get("TeamNum", -1),
            secondary=secondary.get("Name", "") if secondary else "",
        )
        self._live_stats.add_event(event)

    def _handle_ball_hit(self, data: dict) -> None:
        players = data.get("Players", [])
        ball = data.get("Ball", {})
        if players:
            player = players[0]
            event = EventEntry(
                timestamp=time.time(),
                event_type="ball_hit",
                event_name="Ball Hit",
                primary=player.get("Name", ""),
                team_num=player.get("TeamNum", -1),
                details={
                    "pre_hit_speed": ball.get("PreHitSpeed", 0),
                    "post_hit_speed": ball.get("PostHitSpeed", 0),
                },
            )
            # Don't add ball hits to event feed (too frequent) — just log for stats
            # self._live_stats.add_event(event)

    def _handle_countdown_begin(self, data: dict) -> None:
        self._live_stats.match_guid = data.get("MatchGuid", self._live_stats.match_guid)
        event = EventEntry(
            timestamp=time.time(),
            event_type="countdown",
            event_name="Countdown",
        )
        self._live_stats.add_event(event)

    def _handle_match_created(self, data: dict) -> None:
        guid = data.get("MatchGuid", "")
        if guid != self._live_stats.match_guid:
            self._live_stats.clear()
            self._live_stats.match_guid = guid

    def _handle_match_ended(self, data: dict) -> None:
        winner_num = data.get("WinnerTeamNum", -1)
        self._live_stats.game.has_winner = True
        self._live_stats.game.winner = "Blue" if winner_num == 0 else "Orange" if winner_num == 1 else ""

    def _handle_match_destroyed(self, data: dict) -> None:
        pass  # Keep stats around for postgame display

    def _handle_crossbar_hit(self, data: dict) -> None:
        last_touch = data.get("BallLastTouch", {})
        event = EventEntry(
            timestamp=time.time(),
            event_type="crossbar",
            event_name="Crossbar Hit",
            primary=last_touch.get("Name", ""),
            team_num=last_touch.get("TeamNum", -1),
            details={"ball_speed": data.get("BallSpeed", 0)},
        )
        self._live_stats.add_event(event)

    def _handle_goal_replay_start(self, data: dict) -> None:
        self._live_stats.game.is_replay = True

    def _handle_goal_replay_end(self, data: dict) -> None:
        self._live_stats.game.is_replay = False

    # Handler dispatch table
    _handlers: dict[str, Callable[[StatsApiClient, dict], None]] = {
        "UpdateState": _handle_update_state,
        "GoalScored": _handle_goal_scored,
        "StatfeedEvent": _handle_statfeed_event,
        "BallHit": _handle_ball_hit,
        "CountdownBegin": _handle_countdown_begin,
        "MatchCreated": _handle_match_created,
        "MatchEnded": _handle_match_ended,
        "MatchDestroyed": _handle_match_destroyed,
        "CrossbarHit": _handle_crossbar_hit,
        "GoalReplayStart": _handle_goal_replay_start,
        "GoalReplayEnd": _handle_goal_replay_end,
    }

