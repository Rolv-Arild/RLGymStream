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
from rlgymstream.db.models import Match as MatchModel, MatchPlayerStats
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
from rlgymstream.stats_api.client import StatsApiClient

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

    # Start Stats API websocket client
    stats_client = StatsApiClient(
        port=config.stats_api_port,
        on_update=lambda: overlay_state.update_live_stats(stats_client.live_stats),
    )
    stats_api_task = asyncio.create_task(stats_client.run())
    logger.info("Stats API client started (port %d)", config.stats_api_port)

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
                               anchored_bot_ids=anchored_bot_ids.get(mode.value, set()),
                               n_prior=config.matchmaker_n_prior,
                               temperature=config.matchmaker_temperature)
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
            # Reset Stats API live data for the new match
            stats_client.reset()
            overlay_state.set_post_match_stats(None)

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
            # Capture postgame advanced stats from Stats API
            if stats_client.live_stats.players:
                # Validate that stats players match the current match bots
                # Use Stats API naming convention for duplicates: "B", "B (2)", etc.
                match_api_names = _stats_api_player_names(setup)
                stats_player_names = set(stats_client.live_stats.players.keys())
                if stats_player_names & match_api_names:
                    raw_stats = stats_client.live_stats.postgame_to_dict()
                    # Filter out any stale players from a previous match
                    raw_stats["players"] = {
                        name: data for name, data in raw_stats["players"].items()
                        if name in match_api_names
                    }
                    overlay_state.set_post_match_stats(raw_stats)
                else:
                    logger.warning(
                        "Stats API players %s don't match current match bots %s — skipping post_match_stats",
                        stats_player_names, match_api_names,
                    )
                    overlay_state.set_post_match_stats(None)
            else:
                overlay_state.set_post_match_stats(None)

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

            # Persist post-game per-player stats from the Stats API
            if stats_client.live_stats.players and match_record.id is not None:
                # Build a name → bot_id map using Stats API naming convention
                name_to_bot_id = _stats_api_name_to_bot_id(setup)
                postgame = stats_client.live_stats.postgame_to_dict()
                player_stats_rows: list[MatchPlayerStats] = []
                for pname, pdata in postgame.get("players", {}).items():
                    if pname not in name_to_bot_id:
                        continue  # skip stale players from previous match
                    player_stats_rows.append(MatchPlayerStats(
                        match_id=match_record.id,
                        bot_id=name_to_bot_id.get(pname),
                        player_name=pname,
                        team_num=pdata.get("team_num", 0),
                        score=pdata.get("score", 0),
                        goals=pdata.get("goals", 0),
                        shots=pdata.get("shots", 0),
                        assists=pdata.get("assists", 0),
                        saves=pdata.get("saves", 0),
                        demos=pdata.get("demos", 0),
                        touches=pdata.get("touches", 0),
                        avg_boost=pdata.get("avg_boost", 0.0),
                        avg_speed=pdata.get("avg_speed", 0.0),
                        pct_supersonic=pdata.get("pct_supersonic", 0.0),
                        pct_ground=pdata.get("pct_ground", 0.0),
                        pct_wall=pdata.get("pct_wall", 0.0),
                        pct_air=pdata.get("pct_air", 0.0),
                        pct_demolished=pdata.get("pct_demolished", 0.0),
                    ))
                db.save_match_player_stats(player_stats_rows)
                logger.info(
                    "Saved %d player stat rows for match #%d",
                    len(player_stats_rows), match_record.id,
                )

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

            # Let the in-game celebration play out
            await _sleep_or_stop(config.celebration_delay, stop_event)

            # Transition to full-screen scoreboard overlay
            # Embed stats directly in match state so they're guaranteed in sync
            match_api_names = _stats_api_player_names(setup)
            stats_snapshot = overlay_state.post_match_stats
            if stats_snapshot and stats_snapshot.get("players"):
                # Filter to only current match players
                filtered_players = {
                    name: data for name, data in stats_snapshot["players"].items()
                    if name in match_api_names
                }
                if filtered_players:
                    stats_snapshot = {**stats_snapshot, "players": filtered_players}
                    match_state.post_match_stats = stats_snapshot
                else:
                    logger.warning(
                        "post_match_stats players %s don't match match bots %s — dropping",
                        set(stats_snapshot["players"].keys()), match_api_names,
                    )
                    match_state.post_match_stats = None
            else:
                match_state.post_match_stats = None
            match_state.phase = "scoreboard"
            overlay_state.update_match(match_state)

            # Show the scoreboard for the remaining post-match time
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
        stats_client.stop()
        stats_api_task.cancel()
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


def _stats_api_player_names(setup: MatchSetup) -> set[str]:
    """Build the set of player names as the Stats API would report them.

    When the same bot name appears more than once in a match, the Stats API
    names them ``"Name"``, ``"Name (2)"``, ``"Name (3)"``, etc.  This
    helper replicates that convention so we can correctly match stats rows
    to match participants.
    """
    counts: dict[str, int] = {}
    names: set[str] = set()
    for bot in setup.team_blue + setup.team_orange:
        counts[bot.name] = counts.get(bot.name, 0) + 1
        n = counts[bot.name]
        names.add(bot.name if n == 1 else f"{bot.name} ({n})")
    return names


def _stats_api_name_to_bot_id(setup: MatchSetup) -> dict[str, int | None]:
    """Map Stats API player names (with duplicate suffixes) to bot IDs."""
    counts: dict[str, int] = {}
    mapping: dict[str, int | None] = {}
    for bot in setup.team_blue + setup.team_orange:
        counts[bot.name] = counts.get(bot.name, 0) + 1
        n = counts[bot.name]
        api_name = bot.name if n == 1 else f"{bot.name} ({n})"
        mapping[api_name] = bot.id
    return mapping


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

