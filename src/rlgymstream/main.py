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
import subprocess
import sys
from datetime import datetime, timezone

import uvicorn

from rlgymstream.config import AppConfig, MatchMode
from rlgymstream.db.database import Database
from rlgymstream.db.models import Match as MatchModel
from rlgymstream.match.bot_discovery import discover_bots
from rlgymstream.match.launcher import MatchLauncher
from rlgymstream.matchmaking.matchmaker import MatchSetup, pick_match, pick_mode
from rlgymstream.matchmaking.ratings import get_leaderboard, update_ratings, predict_win_probability, configure_defaults
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
# Set up our own logger hierarchy independently of root, so uvicorn's
# logging reconfiguration doesn't swallow our messages.
logger = logging.getLogger("rlgymstream")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_handler)


def main() -> None:
    """CLI entry point."""
    config = AppConfig.from_toml()
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        logger.info("Shutting down.")


async def run(config: AppConfig) -> None:
    """Async entry: start overlay server + match loop."""
    configure_defaults(config.default_mu, config.default_sigma)
    db = Database(config.db_path)
    overlay_state = OverlayState()
    overlay_state.total_matches = db.get_match_count()
    launcher = MatchLauncher(config)

    # Discover bots
    bots = discover_bots(config.bot_sources, db)
    logger.info("Found %d enabled bot(s)", len(bots))
    if not bots:
        logger.error(
            "No bots found – check bot_sources in rlgymstream.toml and restart.",
        )
        sys.exit(1)

    # Apply anchored ratings — seed fixed mu/sigma for configured bots
    # Maps mode value → set of bot IDs that are anchored in that mode
    anchored_bot_ids: dict[str, set[int]] = {m.value: set() for m in config.mode_rotation}
    for anchor in config.anchored_ratings:
        bot = db.get_bot_by_name(anchor.bot_name)
        if bot is None:
            logger.warning("Anchored bot not found: %s", anchor.bot_name)
            continue
        assert bot.id is not None
        # Determine which modes this anchor applies to
        if anchor.modes:
            target_modes = [m for m in config.mode_rotation if m.value in anchor.modes]
        else:
            target_modes = list(config.mode_rotation)
        for mode in target_modes:
            anchored_bot_ids[mode.value].add(bot.id)
            r = db.get_rating(bot.id, mode.value)
            r.mu = anchor.mu
            r.sigma = anchor.sigma
            db.save_rating(r)
        mode_names = ", ".join(m.value for m in target_modes)
        logger.info(
            "Anchored %s at mu=%.2f, sigma=%.2f (MMR=%d) for [%s]",
            anchor.bot_name, anchor.mu, anchor.sigma,
            _rating_to_mmr(anchor.mu), mode_names,
        )

    # Populate initial leaderboards
    _refresh_leaderboards(db, overlay_state, config.mode_rotation, anchored_bot_ids)

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

    # Start Twitch chatbot as a separate subprocess
    # Use a list so the monitor can update the reference on restart
    chatbot_procs: list[subprocess.Popen] = []
    chatbot_monitor_task: asyncio.Task | None = None
    if config.twitch_channel and config.twitch_client_id and config.twitch_client_secret:
        chatbot_procs.append(_start_chatbot_subprocess())
        chatbot_monitor_task = asyncio.create_task(
            _monitor_chatbot(chatbot_procs, stop_event)
        )
    else:
        logger.info(
            "Twitch chatbot disabled (need twitch channel, client_id, and client_secret)"
        )

    # Match loop — resume counter from database
    match_counter = db.get_match_count()
    logger.info("Resuming from match #%d", match_counter)
    last_map: str | None = None
    try:
        # Show leaderboard at startup before the first match
        _refresh_leaderboards(db, overlay_state, config.mode_rotation, anchored_bot_ids)
        overlay_state.update_match(OverlayMatchState(phase="idle"))
        await _sleep_or_stop(config.leaderboard_delay, stop_event)

        while not stop_event.is_set():

            # Pick mode
            mode = pick_mode(config.mode_rotation, match_counter)

            # Matchmake
            setup = pick_match(db, mode, last_map=last_map,
                               sigma_priority_chance=config.sigma_priority_chance,
                               anchored_bot_ids=anchored_bot_ids.get(mode.value, set()))
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
            logger.info("Map: %s", setup.display_map_name)

            # ── Pre-game phase ───────────────────────────────────────
            match_state = _build_match_state(setup, match_counter, "pregame", db,
                                             anchored_bot_ids=anchored_bot_ids.get(mode.value, set()))

            # Compute win probabilities
            blue_ids = [b.id for b in setup.team_blue]
            orange_ids = [b.id for b in setup.team_orange]
            win_probs = predict_win_probability(db, mode.value, blue_ids, orange_ids,
                                                is_solo_queue=mode.is_solo_queue)
            match_state.win_probabilities = [round(p, 3) for p in win_probs]

            overlay_state.update_match(match_state)

            # Clear stale H2H, then set for standard modes
            overlay_state.update_head_to_head({})
            if not mode.is_solo_queue:
                _update_h2h(db, setup, overlay_state, mode.value)

            # Capture pre-match MMRs for delta calculation
            pre_mmrs: dict[int, int] = {}
            for b in match_state.team_blue + match_state.team_orange:
                pre_mmrs[b.id] = b.mmr

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
            # Persist match result
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
            update_ratings(db, mode.value, blue_ids, orange_ids, result.winner,
                          is_solo_queue=mode.is_solo_queue,
                          anchored_bot_ids=anchored_bot_ids.get(mode.value, set()))

            # Compute MMR deltas (post - pre)
            mmr_deltas: dict[int, int] = {}
            for b in setup.team_blue + setup.team_orange:
                assert b.id is not None
                r = db.get_rating(b.id, mode.value)
                post_mmr = _rating_to_mmr(r.mu)
                mmr_deltas[b.id] = post_mmr - pre_mmrs.get(b.id, post_mmr)

            # Update match state with postgame info (badges stay visible)
            match_state.phase = "postgame"
            match_state.score_blue = result.score_blue
            match_state.score_orange = result.score_orange
            match_state.winner = result.winner
            match_state.mmr_deltas = mmr_deltas
            overlay_state.update_match(match_state)

            # Let viewers see the in-game scoreboard + MMR deltas
            await _sleep_or_stop(config.post_match_delay, stop_event)

            # Update overlay recent results
            overlay_state.add_recent_result({
                "blue_names": " & ".join(b.name for b in setup.team_blue),
                "orange_names": " & ".join(b.name for b in setup.team_orange),
                "score_blue": result.score_blue,
                "score_orange": result.score_orange,
                "winner": result.winner,
                "mode": mode.display_name,
                "map": setup.display_map_name,
            })

            # Refresh leaderboards and show the idle/leaderboard screen.
            _refresh_leaderboards(db, overlay_state, config.mode_rotation, anchored_bot_ids)
            overlay_state.update_match(OverlayMatchState(phase="idle"))

            # Show leaderboard for a bit before the next match
            await _sleep_or_stop(config.leaderboard_delay, stop_event)


    except Exception:
        logger.exception("Fatal error in match loop")
    finally:
        launcher.shutdown()
        if chatbot_monitor_task is not None:
            chatbot_monitor_task.cancel()
        if chatbot_procs:
            proc = chatbot_procs[-1]
            if proc.poll() is None:
                logger.info("Stopping chatbot subprocess (pid=%d)", proc.pid)
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        server.should_exit = True
        await server_task


# ── Helpers ──────────────────────────────────────────────────────────


def _build_match_state(
    setup: MatchSetup,
    match_number: int,
    phase: str,
    db: Database,
    anchored_bot_ids: set[int] | None = None,
) -> OverlayMatchState:
    _anchored = anchored_bot_ids or set()

    def bot_info(bot, mode_val):
        assert bot.id is not None
        r = db.get_rating(bot.id, mode_val)
        wins, losses, _draws = db.get_bot_record(bot.id, mode_val)
        return OverlayBotInfo(
            id=bot.id,
            name=bot.name,
            author=bot.author,
            description=bot.description,
            fun_fact=bot.fun_fact,
            language=bot.language,
            mmr=_rating_to_mmr(r.mu),
            mu=round(r.mu, 1),
            sigma=round(r.sigma, 1),
            matches_played=r.matches_played,
            wins=wins,
            losses=losses,
            logo_path=bot.logo_path,
            anchored=bot.id in _anchored,
        )

    return OverlayMatchState(
        phase=phase,
        mode=setup.mode.value,
        mode_display=setup.mode.display_name,
        map_name=setup.display_map_name,
        team_blue=[bot_info(b, setup.mode.value) for b in setup.team_blue],
        team_orange=[bot_info(b, setup.mode.value) for b in setup.team_orange],
        match_number=match_number,
    )


def _refresh_leaderboards(
    db: Database,
    overlay_state: OverlayState,
    modes: list[MatchMode],
    anchored_bot_ids: dict[str, set[int]] | None = None,
) -> None:
    _anchored = anchored_bot_ids or {}
    boards: dict[str, list[OverlayLeaderboardEntry]] = {}
    for mode in modes:
        mode_anchored = _anchored.get(mode.value, set())
        lb = get_leaderboard(db, mode.value)
        boards[mode.value] = [
            OverlayLeaderboardEntry(
                rank=i + 1,
                bot_name=entry["bot"].name,
                author=entry["bot"].author,
                mmr=_rating_to_mmr(entry["mu"]),
                mu=entry["mu"],
                sigma=entry["sigma"],
                matches_played=entry["rating"].matches_played,
                anchored=entry["bot"].id in mode_anchored,
            )
            for i, entry in enumerate(lb)
        ]
    overlay_state.update_leaderboards(boards)


def _update_h2h(
    db: Database,
    setup: MatchSetup,
    overlay_state: OverlayState,
    mode: str,
) -> None:
    """Update head-to-head data for standard modes (bot A vs bot B)."""
    bot_a = setup.team_blue[0]
    bot_b = setup.team_orange[0]
    if bot_a.id == bot_b.id:
        return  # same bot on both sides, no H2H
    assert bot_a.id is not None and bot_b.id is not None
    h2h = db.get_head_to_head(bot_a.id, bot_b.id, mode=mode)
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


def _rating_to_mmr(mu: float) -> int:
    """Convert OpenSkill mu to Rocket League–style MMR.

    Formula: MMR = 20 × mu + 100.  This is the exact formula
    Rocket League uses.  Does NOT reflect actual in-game rank.
    """
    return round(20 * mu + 100)


# ── Chatbot subprocess management ───────────────────────────────────


_CHATBOT_RESTART_DELAY = 5.0  # seconds before restarting a crashed chatbot


def _start_chatbot_subprocess() -> subprocess.Popen:
    """Launch ``python -m rlgymstream.chat`` as a child process."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "rlgymstream.chat"],
        # Inherit stdout/stderr so logs are visible (or redirect to file)
    )
    logger.info("Chatbot subprocess started (pid=%d)", proc.pid)
    return proc


async def _monitor_chatbot(
    procs: list[subprocess.Popen],
    stop_event: asyncio.Event,
) -> None:
    """Watch the chatbot subprocess; restart it if it exits unexpectedly."""
    while not stop_event.is_set():
        proc = procs[-1]
        retcode = proc.poll()
        if retcode is not None:
            if stop_event.is_set():
                break
            logger.warning(
                "Chatbot subprocess exited (code=%s), restarting in %.0fs…",
                retcode, _CHATBOT_RESTART_DELAY,
            )
            await _sleep_or_stop(_CHATBOT_RESTART_DELAY, stop_event)
            if stop_event.is_set():
                break
            procs.append(_start_chatbot_subprocess())
        await _sleep_or_stop(2.0, stop_event)


if __name__ == "__main__":
    main()

