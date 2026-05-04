"""Launch and monitor RLBot v5 matches."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable

from rlbot import flat
from rlbot.config import load_player_config
from rlbot.managers.match import MatchManager
from rlbot.utils.maps import GAME_MAP_TO_UPK

from rlgymstream.config import AppConfig
from rlgymstream.matchmaking.matchmaker import MatchSetup

logger = logging.getLogger(__name__)

# MatchPhase values we care about, mapped to overlay phase names.
# Pregame is handled by the orchestrator before run_match is called.
_PHASE_MAP = {
    flat.MatchPhase.Countdown: "countdown",
    flat.MatchPhase.Kickoff: "live",
    flat.MatchPhase.Active: "live",
    flat.MatchPhase.Replay: "replay",
    flat.MatchPhase.Ended: "postgame",
}


@dataclass
class MatchResult:
    score_blue: int = 0
    score_orange: int = 0
    winner: str = ""  # "blue", "orange", "draw"
    duration_seconds: float = 0.0


# Callback type: (overlay_phase, score_blue, score_orange) → None
PhaseCallback = Callable[[str, int, int], None]


class MatchLauncher:
    """Manages starting, monitoring, and collecting results from RLBot v5 matches.

    A single MatchManager is reused across matches so the RLBotServer
    connection stays open.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._manager: MatchManager | None = None

    # ── public API ────────────────────────────────────────────────────

    async def run_match(
        self,
        setup: MatchSetup,
        on_phase_change: PhaseCallback | None = None,
    ) -> MatchResult:
        """Launch a match and wait for it to finish.

        *on_phase_change* is called (from the event loop thread) whenever the
        in-game phase changes, with ``(overlay_phase, score_blue, score_orange)``.
        """
        logger.info(
            "Launching %s match: %s vs %s on %s",
            setup.mode.display_name,
            [b.name for b in setup.team_blue],
            [b.name for b in setup.team_orange],
            setup.map_name,
        )

        match_config = self._build_match_config(setup)
        start_time = time.monotonic()

        loop = asyncio.get_running_loop()
        game_finished = asyncio.Event()
        final_result = MatchResult()

        def _run_match() -> None:
            """Blocking work executed in a thread."""
            manager = self._get_manager()
            last_overlay_phase = ""
            hud_cycled = False
            match_started = False  # True once we've seen Countdown (fresh match)

            try:
                manager.start_match(match_config, wait_for_start=True)
                time.sleep(1.0)

                while True:
                    packet = manager.packet
                    if packet is None:
                        time.sleep(0.1)
                        continue

                    game_phase = packet.match_info.match_phase

                    # Wait for Countdown to confirm this is a fresh match.
                    # This prevents reading stale scores from a mercy'd match.
                    if not match_started:
                        if game_phase == flat.MatchPhase.Countdown:
                            match_started = True
                        else:
                            time.sleep(0.1)
                            continue

                    # Cycle HUD and queue replay save once when match first goes live
                    if not hud_cycled and game_phase in (
                        flat.MatchPhase.Countdown,
                        flat.MatchPhase.Kickoff,
                        flat.MatchPhase.Active,
                    ):
                        try:
                            # Send twice to get clean feed. We use our own overlay
                            manager.set_game_state(commands=["CycleHUD"])
                            manager.set_game_state(commands=["CycleHUD"])
                            logger.info("Sent CycleHUD")
                        except Exception:
                            logger.warning("Failed to send CycleHUD", exc_info=True)
                        try:
                            manager.set_game_state(commands=["QueSaveReplay"])
                            logger.info("Sent QueSaveReplay")
                        except Exception:
                            logger.warning("Failed to send QueSaveReplay", exc_info=True)
                        hud_cycled = True

                    # Read scores from packet
                    score_blue = 0
                    score_orange = 0
                    for team_info in packet.teams:
                        if team_info.team_index == 0:
                            score_blue = team_info.score
                        elif team_info.team_index == 1:
                            score_orange = team_info.score

                    # Mercy rule: end early if goal difference is too large
                    mercy_diff = self._config.mercy_goal_diff
                    if (
                        abs(score_blue - score_orange) >= mercy_diff
                        and game_phase in (
                            flat.MatchPhase.Active,
                            flat.MatchPhase.Kickoff,
                            flat.MatchPhase.GoalScored,
                        )
                    ):
                        logger.info(
                            "Mercy rule triggered: %d-%d (diff=%d)",
                            score_blue, score_orange,
                            abs(score_blue - score_orange),
                        )
                        final_result.score_blue = score_blue
                        final_result.score_orange = score_orange
                        final_result.duration_seconds = time.monotonic() - start_time
                        if score_blue > score_orange:
                            final_result.winner = "blue"
                        else:
                            final_result.winner = "orange"
                        try:
                            manager.stop_match()
                        except Exception:
                            logger.debug("Failed to stop match", exc_info=True)
                        loop.call_soon_threadsafe(game_finished.set)
                        return

                    # Map game phase → overlay phase and notify on change
                    overlay_phase = _PHASE_MAP.get(game_phase, "")
                    if overlay_phase and overlay_phase != last_overlay_phase:
                        last_overlay_phase = overlay_phase
                        if on_phase_change is not None:
                            loop.call_soon_threadsafe(
                                on_phase_change, overlay_phase, score_blue, score_orange
                            )

                    if game_phase == flat.MatchPhase.Ended:
                        final_result.score_blue = score_blue
                        final_result.score_orange = score_orange
                        final_result.duration_seconds = time.monotonic() - start_time
                        if score_blue > score_orange:
                            final_result.winner = "blue"
                        elif score_orange > score_blue:
                            final_result.winner = "orange"
                        else:
                            final_result.winner = "draw"

                        loop.call_soon_threadsafe(game_finished.set)
                        return

                    time.sleep(0.1)

            except Exception:
                logger.exception("Error inside match thread")
                final_result.winner = "draw"
                final_result.duration_seconds = time.monotonic() - start_time
                loop.call_soon_threadsafe(game_finished.set)

        thread_task = loop.run_in_executor(None, _run_match)
        await game_finished.wait()

        try:
            await asyncio.wait_for(thread_task, timeout=15)
        except asyncio.TimeoutError:
            pass

        return final_result

    def shutdown(self) -> None:
        """Shut down the RLBotServer and clean up."""
        if self._manager is not None:
            try:
                self._manager.shut_down()
            except Exception:
                logger.debug("Error shutting down MatchManager", exc_info=True)
            self._manager = None

    # ── private helpers ───────────────────────────────────────────────

    def _get_manager(self) -> MatchManager:
        if self._manager is None:
            self._manager = MatchManager()
        return self._manager

    def _build_match_config(self, setup: MatchSetup) -> flat.MatchConfiguration:
        players: list[flat.PlayerConfiguration] = []

        for bot in setup.team_blue:
            players.append(load_player_config(bot.config_path, team=0))

        for bot in setup.team_orange:
            players.append(load_player_config(bot.config_path, team=1))

        game_map_upk = GAME_MAP_TO_UPK.get(setup.map_name, setup.map_name)

        return flat.MatchConfiguration(
            # launcher=flat.Launcher.Epic,  # Auto detect
            player_configurations=players,
            game_map_upk=game_map_upk,
            game_mode=flat.GameMode.Soccar,
            skip_replays=True,
            instant_start=False,
            auto_start_agents=True,
            existing_match_behavior=flat.ExistingMatchBehavior.Restart,
            enable_rendering=flat.DebugRendering.AlwaysOff,
            enable_state_setting=True,
            auto_save_replay=True,
        )
