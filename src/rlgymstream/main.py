"""RLGymStream – main orchestration loop.

Entry point that:
1. Discovers bots
2. Starts the overlay web server
3. Runs an infinite match loop: matchmake → launch → collect → rate → display
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone

import uvicorn

from rlgymstream.config import AppConfig, MatchMode
from rlgymstream.db.database import Database
from rlgymstream.db.models import Match as MatchModel
from rlgymstream.match.bot_discovery import discover_bots
from rlgymstream.match.launcher import MatchLauncher
from rlgymstream.matchmaking.matchmaker import MatchSetup, pick_match, pick_mode
from rlgymstream.matchmaking.ratings import get_leaderboard, update_ratings
from rlgymstream.overlay.server import create_overlay_app
from rlgymstream.overlay.state import (
    OverlayBotInfo,
    OverlayLeaderboardEntry,
    OverlayMatchState,
    OverlayState,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("rlgymstream")


def main() -> None:
    """CLI entry point."""
    config = AppConfig.from_toml()
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        logger.info("Shutting down.")


async def run(config: AppConfig) -> None:
    """Async entry: start overlay server + match loop."""
    db = Database(config.db_path)
    overlay_state = OverlayState()
    launcher = MatchLauncher(config)

    # Discover bots
    bots = discover_bots(config.bot_sources, db)
    logger.info("Found %d enabled bot(s)", len(bots))
    if not bots:
        logger.error(
            "No bots found – check bot_sources in rlgymstream.toml and restart.",
        )
        sys.exit(1)

    # Populate initial leaderboards
    _refresh_leaderboards(db, overlay_state, config.mode_rotation)

    # Start overlay web server in background
    overlay_app = create_overlay_app(overlay_state)
    server_cfg = uvicorn.Config(
        overlay_app,
        host=config.overlay_host,
        port=config.overlay_port,
        log_level="warning",
    )
    server = uvicorn.Server(server_cfg)
    server_task = asyncio.create_task(server.serve())
    logger.info(
        "Overlay server started at http://%s:%d",
        config.overlay_host,
        config.overlay_port,
    )

    # Graceful shutdown
    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        stop_event.set()
        server.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    # Match loop
    match_counter = 0
    last_map: str | None = None
    try:
        while not stop_event.is_set():
            # Re-discover bots each cycle (hot reload)
            bots = discover_bots(config.bot_sources, db)
            if not bots:
                logger.warning("No bots available, waiting 30s…")
                await _sleep_or_stop(30, stop_event)
                continue

            # Pick mode
            mode = pick_mode(config.mode_rotation, match_counter)

            # Matchmake
            setup = pick_match(db, mode, last_map=last_map)
            if setup is None:
                logger.warning(
                    "Not enough bots for %s (need %d, have %d), skipping…",
                    mode.display_name,
                    mode.min_bots_required,
                    len(bots),
                )
                match_counter += 1
                await _sleep_or_stop(5, stop_event)
                continue

            match_counter += 1
            last_map = setup.map_name
            logger.info("="*60)
            logger.info("MATCH #%d – %s", match_counter, mode.display_name)
            logger.info(
                "Blue: %s  vs  Orange: %s",
                [b.name for b in setup.team_blue],
                [b.name for b in setup.team_orange],
            )
            logger.info("Map: %s", setup.map_name)

            # ── Pre-game phase ───────────────────────────────────────
            match_state = _build_match_state(setup, match_counter, "pregame", db)
            overlay_state.update_match(match_state)

            # Head-to-head (meaningful for standard modes: 1v1, 2v2, 3v3
            # where each team is a single bot type)
            if not mode.is_solo_queue:
                _update_h2h(db, setup, overlay_state)

            # Show pregame for a minimum duration, but it stays until
            # the game itself transitions (countdown/kickoff/live).
            await _sleep_or_stop(config.pre_match_delay, stop_event)
            if stop_event.is_set():
                break

            # ── Launch match — overlay follows real in-game phases ────
            def _on_phase(phase: str, score_blue: int, score_orange: int) -> None:
                match_state.phase = phase
                match_state.score_blue = score_blue
                match_state.score_orange = score_orange
                overlay_state.update_match(match_state)

            result = await launcher.run_match(setup, on_phase_change=_on_phase)

            logger.info(
                "Result: Blue %d – %d Orange → %s",
                result.score_blue,
                result.score_orange,
                result.winner.upper(),
            )

            # ── Post-game ─────────────────────────────────────────────
            # The in-game scoreboard handles the post-match display.

            # Persist match result
            blue_ids = [b.id for b in setup.team_blue]
            orange_ids = [b.id for b in setup.team_orange]

            match_record = MatchModel(
                mode=mode.value,
                map_name=setup.map_name,
                timestamp=datetime.now(timezone.utc).isoformat(),
                team_blue_ids=",".join(str(i) for i in blue_ids),
                team_orange_ids=",".join(str(i) for i in orange_ids),
                score_blue=result.score_blue,
                score_orange=result.score_orange,
                winner=result.winner,
                duration_seconds=result.duration_seconds,
            )
            db.save_match(match_record)

            # Update OpenSkill ratings
            update_ratings(db, mode.value, blue_ids, orange_ids, result.winner)

            # Update overlay recent results
            overlay_state.add_recent_result({
                "blue_names": " & ".join(b.name for b in setup.team_blue),
                "orange_names": " & ".join(b.name for b in setup.team_orange),
                "score_blue": result.score_blue,
                "score_orange": result.score_orange,
                "winner": result.winner,
                "mode": mode.display_name,
                "map": setup.map_name,
            })

            # Refresh leaderboards BEFORE setting idle so the
            # idle state already contains up-to-date data.
            _refresh_leaderboards(db, overlay_state, config.mode_rotation)

            # Now show idle with the leaderboard
            overlay_state.update_match(OverlayMatchState(phase="idle"))

            # Wait before next match so viewers can see the in-game scoreboard
            await _sleep_or_stop(config.post_match_delay, stop_event)


    except Exception:
        logger.exception("Fatal error in match loop")
    finally:
        launcher.shutdown()
        server.should_exit = True
        await server_task


# ── Helpers ──────────────────────────────────────────────────────────


def _build_match_state(
    setup: MatchSetup,
    match_number: int,
    phase: str,
    db: Database,
) -> OverlayMatchState:
    def bot_info(bot, mode_val):
        assert bot.id is not None
        r = db.get_rating(bot.id, mode_val)
        display = round(r.display_rating, 1)
        wins, losses, _draws = db.get_bot_record(bot.id, mode_val)
        return OverlayBotInfo(
            id=bot.id,
            name=bot.name,
            author=bot.author,
            description=bot.description,
            fun_fact=bot.fun_fact,
            language=bot.language,
            rating=display,
            mmr=_rating_to_mmr(display),
            mu=round(r.mu, 1),
            sigma=round(r.sigma, 1),
            matches_played=r.matches_played,
            wins=wins,
            losses=losses,
            logo_path=bot.logo_path,
        )

    return OverlayMatchState(
        phase=phase,
        mode=setup.mode.value,
        mode_display=setup.mode.display_name,
        map_name=setup.map_name,
        team_blue=[bot_info(b, setup.mode.value) for b in setup.team_blue],
        team_orange=[bot_info(b, setup.mode.value) for b in setup.team_orange],
        match_number=match_number,
    )


def _refresh_leaderboards(
    db: Database,
    overlay_state: OverlayState,
    modes: list[MatchMode],
) -> None:
    boards: dict[str, list[OverlayLeaderboardEntry]] = {}
    for mode in modes:
        lb = get_leaderboard(db, mode.value)
        boards[mode.value] = [
            OverlayLeaderboardEntry(
                rank=i + 1,
                bot_name=entry["bot"].name,
                author=entry["bot"].author,
                rating=entry["display_rating"],
                mmr=_rating_to_mmr(entry["display_rating"]),
                mu=entry["mu"],
                sigma=entry["sigma"],
                matches_played=entry["rating"].matches_played,
            )
            for i, entry in enumerate(lb)
        ]
    overlay_state.update_leaderboards(boards)


def _update_h2h(
    db: Database,
    setup: MatchSetup,
    overlay_state: OverlayState,
) -> None:
    """Update head-to-head data for standard modes (bot A vs bot B)."""
    bot_a = setup.team_blue[0]
    bot_b = setup.team_orange[0]
    if bot_a.id == bot_b.id:
        return  # same bot on both sides, no H2H
    assert bot_a.id is not None and bot_b.id is not None
    h2h = db.get_head_to_head(bot_a.id, bot_b.id)
    overlay_state.update_head_to_head({
        "bot_a_name": bot_a.name,
        "bot_b_name": bot_b.name,
        "wins_a": h2h["wins_a"],
        "wins_b": h2h["wins_b"],
        "draws": h2h["draws"],
        "total": h2h["total"],
    })


async def _sleep_or_stop(seconds: float, stop_event: asyncio.Event) -> None:
    """Sleep for *seconds* but wake early if stop_event is set."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


def _rating_to_mmr(rating: float) -> int:
    """Convert OpenSkill display rating to Rocket League–style MMR.

    Formula: MMR = 20 × rating + 1000.  This is purely cosmetic and does
    NOT reflect the bots' actual in-game rank.
    """
    return round(20 * rating + 1000)


if __name__ == "__main__":
    main()

