"""Master Chef Showdown — formal best-of-7 series across 1v1, 3v3, 2v2.

Usage:
    python scripts/showdown.py <bot_a_toml> <bot_b_toml> [--overlay-port 8090]

Runs three best-of-7 series (1v1, 3v3, 2v2 in that order).
First bot to win 2 of the 3 series crowns their maker Master Chef.

No database or rating updates — purely an exhibition match.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import signal
import sys
import time
import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from rlbot import flat
from rlbot.config import load_player_config
from rlbot.managers.match import MatchManager
from rlbot.utils.maps import STANDARD_MAPS, GAME_MAP_TO_UPK

# Add src to path so we can reuse some utilities
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from rlgymstream.matchmaking.matchmaker import format_map_name

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("showdown")


# ── Data models ──────────────────────────────────────────────────────


@dataclass
class BotInfo:
    name: str = ""
    author: str = ""
    description: str = ""
    logo_path: str | None = None
    config_path: str = ""

    @classmethod
    def from_toml(cls, path: Path) -> "BotInfo":
        path = path.resolve()
        with open(path, "rb") as f:
            data = tomllib.load(f)
        settings = data.get("settings", {})
        details = data.get("details", {})
        logo = details.get("logo_file", "")
        logo_path = str(path.parent / logo) if logo else None
        return cls(
            name=settings.get("name", path.parent.name),
            author=details.get("developer", ""),
            description=details.get("description", ""),
            logo_path=logo_path if logo_path and Path(logo_path).is_file() else None,
            config_path=str(path),
        )


@dataclass
class GameResult:
    score_blue: int = 0
    score_orange: int = 0
    winner: str = ""  # "blue" or "orange"
    map_name: str = ""


@dataclass
class SeriesState:
    mode: str = ""              # "1v1", "3v3", "2v2"
    team_size: int = 1
    games: list[GameResult] = field(default_factory=list)
    wins_a: int = 0             # bot A wins in this series
    wins_b: int = 0             # bot B wins in this series
    current_game: int = 0       # 1-based
    series_winner: str = ""     # "" | "a" | "b"
    in_progress: bool = False
    best_of: int = 7

    @property
    def wins_needed(self) -> int:
        return self.best_of // 2 + 1


@dataclass
class ShowdownState:
    """Full overlay state for the showdown."""
    phase: str = "idle"         # idle, series_intro, pregame, live, postgame, series_result, final_result
    bot_a: dict[str, Any] = field(default_factory=dict)
    bot_b: dict[str, Any] = field(default_factory=dict)
    series_order: list[str] = field(default_factory=lambda: ["1v1", "3v3", "2v2"])
    series_states: dict[str, dict] = field(default_factory=dict)  # mode → SeriesState as dict
    current_series: str = ""     # current mode being played
    overall_wins_a: int = 0      # series wins
    overall_wins_b: int = 0
    showdown_winner: str = ""    # "" | "a" | "b"
    map_name: str = ""
    score_blue: int = 0
    score_orange: int = 0
    # For pregame / live: which side is bot_a on
    a_is_blue: bool = True
    countdown_end: float = 0.0  # Unix timestamp when the opening countdown ends
    _version: int = 0

    def bump(self):
        self._version += 1

    def to_json(self) -> str:
        d = {
            "phase": self.phase,
            "bot_a": self.bot_a,
            "bot_b": self.bot_b,
            "series_order": self.series_order,
            "series_states": self.series_states,
            "current_series": self.current_series,
            "overall_wins_a": self.overall_wins_a,
            "overall_wins_b": self.overall_wins_b,
            "showdown_winner": self.showdown_winner,
            "map_name": self.map_name,
            "score_blue": self.score_blue,
            "score_orange": self.score_orange,
            "a_is_blue": self.a_is_blue,
            "countdown_end": self.countdown_end,
            "version": self._version,
        }
        return json.dumps(d)


# ── Overlay server ───────────────────────────────────────────────────

OVERLAY_DIR = Path(__file__).resolve().parent.parent / "src" / "rlgymstream" / "overlay"
STATIC_DIR = OVERLAY_DIR / "static"


def create_showdown_app(state: ShowdownState) -> FastAPI:
    app = FastAPI(title="Master Chef Showdown")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    @app.get("/", response_class=HTMLResponse)
    async def overlay():
        return SHOWDOWN_HTML

    @app.get("/api/state")
    async def api_state():
        return json.loads(state.to_json())

    @app.get("/api/logo/{side}")
    async def bot_logo(side: str):
        info = state.bot_a if side == "a" else state.bot_b
        logo = info.get("logo_path")
        if logo and Path(logo).is_file():
            return FileResponse(logo)
        return HTMLResponse("", status_code=404)

    @app.get("/api/events")
    async def events(request: Request):
        async def gen():
            last_v = -1
            while True:
                if await request.is_disconnected():
                    break
                if state._version != last_v:
                    last_v = state._version
                    yield {"event": "state", "data": state.to_json()}
                await asyncio.sleep(0.3)
        return EventSourceResponse(gen())

    return app


# ── Match launcher (simplified, no database) ────────────────────────

_PHASE_MAP = {
    flat.MatchPhase.Countdown: "countdown",
    flat.MatchPhase.Kickoff: "live",
    flat.MatchPhase.Active: "live",
    flat.MatchPhase.Replay: "replay",
    flat.MatchPhase.Ended: "postgame",
}


async def run_game(
    manager: MatchManager,
    bot_a: BotInfo,
    bot_b: BotInfo,
    team_size: int,
    map_name: str,
    a_is_blue: bool,
    on_phase: Any = None,
    mercy_diff: int = 0,
) -> GameResult:
    """Launch a single game and return the result."""
    blue_bot = bot_a if a_is_blue else bot_b
    orange_bot = bot_b if a_is_blue else bot_a

    players: list[flat.PlayerConfiguration] = []
    for _ in range(team_size):
        players.append(load_player_config(blue_bot.config_path, team=0))
    for _ in range(team_size):
        players.append(load_player_config(orange_bot.config_path, team=1))

    game_map_upk = GAME_MAP_TO_UPK.get(map_name, map_name)
    match_config = flat.MatchConfiguration(
        # launcher=flat.Launcher.Epic,
        player_configurations=players,
        game_map_upk=game_map_upk,
        game_mode=flat.GameMode.Soccar,
        skip_replays=False,
        instant_start=False,
        auto_start_agents=True,
        existing_match_behavior=flat.ExistingMatchBehavior.Restart,
        enable_rendering=flat.DebugRendering.AlwaysOff,
        enable_state_setting=True,
        auto_save_replay=True,
    )

    loop = asyncio.get_running_loop()
    game_finished = asyncio.Event()
    result = GameResult(map_name=map_name)

    def _run():
        last_phase = ""
        hud_cycled = False
        match_started = False
        try:
            manager.start_match(match_config, wait_for_start=True)
            time.sleep(1.0)
            while True:
                packet = manager.packet
                if packet is None:
                    time.sleep(0.1)
                    continue
                phase = packet.match_info.match_phase
                if not match_started:
                    if phase == flat.MatchPhase.Countdown:
                        match_started = True
                    else:
                        time.sleep(0.1)
                        continue
                if not hud_cycled and phase in (
                    flat.MatchPhase.Countdown, flat.MatchPhase.Kickoff, flat.MatchPhase.Active
                ):
                    try:
                        manager.set_game_state(commands=["CycleHUD"])
                    except Exception:
                        pass
                    try:
                        manager.set_game_state(commands=["QueSaveReplay"])
                    except Exception:
                        pass
                    hud_cycled = True

                score_blue = score_orange = 0
                for t in packet.teams:
                    if t.team_index == 0:
                        score_blue = t.score
                    elif t.team_index == 1:
                        score_orange = t.score

                # Mercy rule
                if mercy_diff > 0 and abs(score_blue - score_orange) >= mercy_diff and phase in (
                    flat.MatchPhase.Active, flat.MatchPhase.Kickoff, flat.MatchPhase.GoalScored,
                ):
                    logger.info("Mercy: %d-%d", score_blue, score_orange)
                    result.score_blue = score_blue
                    result.score_orange = score_orange
                    result.winner = "blue" if score_blue > score_orange else "orange"
                    try:
                        manager.stop_match()
                    except Exception:
                        pass
                    loop.call_soon_threadsafe(game_finished.set)
                    return

                overlay_phase = _PHASE_MAP.get(phase, "")
                if overlay_phase and overlay_phase != last_phase:
                    last_phase = overlay_phase
                    if on_phase:
                        loop.call_soon_threadsafe(on_phase, overlay_phase, score_blue, score_orange)

                if phase == flat.MatchPhase.Ended:
                    result.score_blue = score_blue
                    result.score_orange = score_orange
                    if score_blue > score_orange:
                        result.winner = "blue"
                    elif score_orange > score_blue:
                        result.winner = "orange"
                    else:
                        result.winner = "draw"
                    loop.call_soon_threadsafe(game_finished.set)
                    return
                time.sleep(0.1)
        except Exception:
            logger.exception("Error in match thread")
            result.winner = "draw"
            loop.call_soon_threadsafe(game_finished.set)

    task = loop.run_in_executor(None, _run)
    await game_finished.wait()
    try:
        await asyncio.wait_for(task, timeout=15)
    except asyncio.TimeoutError:
        pass
    return result


# ── Main orchestration ───────────────────────────────────────────────

SERIES_MODES = [
    ("1v1", 1),
    ("3v3", 3),
    ("2v2", 2),
]


async def run_showdown(bot_a: BotInfo, bot_b: BotInfo, port: int, quick: bool = False,
                       resume: dict[str, tuple[int, int]] | None = None,
                       skip_opening: bool = False) -> None:
    # In quick mode: Bo3 series, short delays
    best_of = 3 if quick else 7
    mercy = 1 if quick else 0
    t_opening = 10 if quick else 180
    t_series_intro = 5 if quick else 60
    t_pregame = 5 if quick else 30
    t_postgame = 5 if quick else 60
    t_series_result = 5 if quick else 60

    if quick:
        logger.info("⚡ Quick mode: Bo%d series, short delays", best_of)

    resume = resume or {}

    state = ShowdownState()
    state.bot_a = {
        "name": bot_a.name, "author": bot_a.author,
        "description": bot_a.description, "logo_path": bot_a.logo_path,
    }
    state.bot_b = {
        "name": bot_b.name, "author": bot_b.author,
        "description": bot_b.description, "logo_path": bot_b.logo_path,
    }
    for mode, _ in SERIES_MODES:
        state.series_states[mode] = asdict(SeriesState(mode=mode))

    # Start overlay server
    app = create_showdown_app(state)
    server_cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(server_cfg)
    server_task = asyncio.create_task(server.serve())
    logger.info("Showdown overlay at http://127.0.0.1:%d", port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    manager = MatchManager()

    try:
        # ── Opening screen ───────────────────────────────────────────
        if not skip_opening and not resume:
            state.phase = "idle"
            state.countdown_end = time.time() + t_opening
            state.bump()
            await _sleep(t_opening, stop)

        for mode, team_size in SERIES_MODES:
            if stop.is_set():
                break

            series = SeriesState(mode=mode, team_size=team_size, in_progress=True, best_of=best_of)

            # Apply resume scores if provided
            if mode in resume:
                ra, rb = resume[mode]
                series.wins_a = ra
                series.wins_b = rb
                series.current_game = ra + rb
                logger.info("Resuming %s at %d-%d", mode, ra, rb)
                # If this series is already decided, mark it and skip
                if ra >= series.wins_needed:
                    series.series_winner = "a"
                    state.overall_wins_a += 1
                    series.in_progress = False
                    state.series_states[mode] = asdict(series)
                    state.current_series = mode
                    logger.info("  %s already won by %s (%d-%d)", mode, bot_a.name, ra, rb)
                    continue
                elif rb >= series.wins_needed:
                    series.series_winner = "b"
                    state.overall_wins_b += 1
                    series.in_progress = False
                    state.series_states[mode] = asdict(series)
                    state.current_series = mode
                    logger.info("  %s already won by %s (%d-%d)", mode, bot_b.name, ra, rb)
                    continue

            # ── Series intro ─────────────────────────────────────────
            state.current_series = mode
            state.series_states[mode] = asdict(series)
            state.phase = "series_intro"
            state.bump()
            logger.info("=" * 60)
            logger.info("SERIES: %s (Best of %d)", mode, series.best_of)
            await _sleep(t_series_intro, stop)
            if stop.is_set():
                break

            used_maps: list[str] = []

            while series.wins_a < series.wins_needed and series.wins_b < series.wins_needed:
                if stop.is_set():
                    break
                series.current_game = len(series.games) + 1

                # Bot A is always blue, bot B is always orange
                a_is_blue = True
                state.a_is_blue = True

                # Pick map (avoid repeat)
                candidates = [m for m in STANDARD_MAPS if m not in used_maps[-2:]] or STANDARD_MAPS
                chosen_map = random.choice(candidates)
                used_maps.append(chosen_map)
                state.map_name = format_map_name(chosen_map)

                # ── Pregame ──────────────────────────────────────────
                state.phase = "pregame"
                state.score_blue = 0
                state.score_orange = 0
                state.series_states[mode] = asdict(series)
                state.bump()
                logger.info(
                    "  Game %d: %s (blue) vs %s (orange) on %s",
                    series.current_game,
                    bot_a.name if a_is_blue else bot_b.name,
                    bot_b.name if a_is_blue else bot_a.name,
                    format_map_name(chosen_map),
                )
                await _sleep(t_pregame, stop)
                if stop.is_set():
                    break

                # ── Play ─────────────────────────────────────────────
                def _on_phase(phase, sb, so):
                    state.phase = phase if phase != "postgame" else "live"
                    state.score_blue = sb
                    state.score_orange = so
                    state.bump()

                state.phase = "live"
                state.bump()

                game_result = await run_game(
                    manager, bot_a, bot_b, team_size, chosen_map, a_is_blue,
                    on_phase=_on_phase, mercy_diff=mercy,
                )

                # Determine who won in terms of A/B
                if game_result.winner == "draw":
                    logger.info("  Draw — replaying game")
                    continue

                if (game_result.winner == "blue" and a_is_blue) or \
                   (game_result.winner == "orange" and not a_is_blue):
                    game_winner_ab = "a"
                    series.wins_a += 1
                else:
                    game_winner_ab = "b"
                    series.wins_b += 1

                game_result_dict = asdict(game_result)
                game_result_dict["winner_ab"] = game_winner_ab
                series.games.append(game_result)

                logger.info(
                    "  Result: %d-%d → %s wins (series: %d-%d)",
                    game_result.score_blue, game_result.score_orange,
                    bot_a.name if game_winner_ab == "a" else bot_b.name,
                    series.wins_a, series.wins_b,
                )

                # Postgame — show in-game scoreboard
                state.phase = "postgame"
                state.score_blue = game_result.score_blue
                state.score_orange = game_result.score_orange
                state.series_states[mode] = asdict(series)
                state.bump()
                await _sleep(t_postgame, stop)

            # ── Series result ────────────────────────────────────────
            if series.wins_a >= series.wins_needed:
                series.series_winner = "a"
                state.overall_wins_a += 1
            elif series.wins_b >= series.wins_needed:
                series.series_winner = "b"
                state.overall_wins_b += 1

            series.in_progress = False
            state.series_states[mode] = asdict(series)
            state.phase = "series_result"
            state.bump()
            logger.info(
                "  Series %s winner: %s (%d-%d). Overall: %d-%d",
                mode,
                bot_a.name if series.series_winner == "a" else bot_b.name,
                series.wins_a, series.wins_b,
                state.overall_wins_a, state.overall_wins_b,
            )
            await _sleep(t_series_result, stop)

        # ── Final result ─────────────────────────────────────────────
        if state.overall_wins_a >= 2:
            state.showdown_winner = "a"
        elif state.overall_wins_b >= 2:
            state.showdown_winner = "b"
        state.phase = "final_result"
        state.bump()
        winner_bot = bot_a if state.showdown_winner == "a" else bot_b
        logger.info("=" * 60)
        logger.info("🏆 MASTER CHEF: %s (with %s)", winner_bot.author, winner_bot.name)
        logger.info("=" * 60)

        # Keep final screen up until manually stopped
        await stop.wait()

    except Exception:
        logger.exception("Fatal error in showdown")
    finally:
        try:
            manager.shut_down()
        except Exception:
            pass
        server.should_exit = True
        await server_task


async def _sleep(seconds: float, stop: asyncio.Event):
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


# ── Showdown overlay HTML ────────────────────────────────────────────

SHOWDOWN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Master Chef Showdown</title>
<link rel="stylesheet" href="/static/styles.css?v=showdown">
<style>
body {
    background: transparent;
    font-family: 'Poppins', sans-serif;
    color: #f0f0f0;
    width: 1920px;
    height: 1080px;
    overflow: hidden;
    margin: 0;
}
#overlay {
    width: 1920px;
    height: 1080px;
    position: relative;
}
.sd-layer {
    position: absolute;
    inset: 0;
    display: none;
    align-items: center;
    justify-content: center;
    transition: opacity 0.4s ease;
    opacity: 0;
}
.sd-layer.active {
    display: flex;
    opacity: 1;
}

/* ── Idle / Opening ─────────────────────── */
.sd-opening {
    background: var(--bg);
    flex-direction: column;
    gap: 24px;
}
.sd-title {
    font-family: 'Orbitron', sans-serif;
    font-size: 52px;
    color: var(--accent);
    letter-spacing: 8px;
    text-transform: uppercase;
    text-shadow: 0 0 30px rgba(123, 47, 242, 0.5);
}
.sd-subtitle {
    font-size: 22px;
    color: var(--text-dim);
    letter-spacing: 2px;
}
.sd-vs-block {
    display: flex;
    align-items: center;
    gap: 60px;
    margin-top: 20px;
}
.sd-bot-card {
    text-align: center;
    max-width: 400px;
}
.sd-bot-logo {
    width: 120px;
    height: 120px;
    border-radius: 16px;
    object-fit: contain;
    background: rgba(255,255,255,0.05);
    margin: 0 auto 12px;
}
.sd-bot-name {
    font-size: 32px;
    font-weight: 700;
}
.sd-bot-author {
    font-size: 14px;
    color: var(--text-dim);
}
.sd-bot-desc {
    font-size: 14px;
    color: var(--text-dim);
    margin-top: 8px;
    line-height: 1.4;
}
.sd-vs-text {
    font-family: 'Orbitron', sans-serif;
    font-size: 40px;
    color: var(--text-dim);
    opacity: 0.5;
}
.sd-format {
    font-size: 16px;
    color: var(--text-dim);
    margin-top: 20px;
    text-align: center;
    line-height: 1.6;
}
.sd-countdown {
    font-family: 'Orbitron', sans-serif;
    font-size: 32px;
    color: var(--gold);
    margin-top: 24px;
    letter-spacing: 4px;
}

/* ── Series Intro ───────────────────────── */
.sd-series-intro {
    background: var(--bg);
    flex-direction: column;
    gap: 24px;
}
.sd-series-mode {
    font-family: 'Orbitron', sans-serif;
    font-size: 60px;
    color: var(--accent);
    letter-spacing: 8px;
}
.sd-series-label {
    font-size: 24px;
    color: var(--text-dim);
    letter-spacing: 3px;
}
.sd-overall-score {
    font-family: 'Orbitron', sans-serif;
    font-size: 36px;
    margin-top: 12px;
}
.sd-overall-a { color: var(--blue); }
.sd-overall-b { color: var(--orange); }

/* ── Pregame ────────────────────────────── */
.sd-pregame {
    background: var(--bg);
    flex-direction: column;
    padding: 40px 80px;
}
.sd-pre-header {
    text-align: center;
    margin-bottom: 20px;
}
.sd-pre-mode {
    font-family: 'Orbitron', sans-serif;
    font-size: 28px;
    color: var(--accent);
    letter-spacing: 4px;
}
.sd-pre-map { font-size: 14px; color: var(--text-dim); margin-top: 4px; }
.sd-pre-game-num { font-size: 13px; color: var(--text-dim); }
.sd-pre-teams {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 48px;
    flex: 1;
}
.sd-pre-side {
    text-align: center;
    max-width: 500px;
}
.sd-pre-side-label {
    font-family: 'Orbitron', sans-serif;
    font-size: 14px;
    letter-spacing: 4px;
    margin-bottom: 12px;
}
.sd-pre-side.blue .sd-pre-side-label { color: var(--blue); }
.sd-pre-side.orange .sd-pre-side-label { color: var(--orange); }
.sd-pre-bot-logo {
    width: 96px;
    height: 96px;
    border-radius: 12px;
    object-fit: contain;
    background: rgba(255,255,255,0.05);
    margin: 0 auto 8px;
}
.sd-pre-bot-name { font-size: 28px; font-weight: 700; }
.sd-pre-vs {
    font-family: 'Orbitron', sans-serif;
    font-size: 28px;
    color: var(--text-dim);
    opacity: 0.5;
}
.sd-series-score-bar {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 16px;
    margin-top: 20px;
}
.sd-score-dot {
    width: 20px; height: 20px;
    border-radius: 50%;
    background: rgba(255,255,255,0.1);
    border: 2px solid rgba(255,255,255,0.2);
}
.sd-score-dot.won-a { background: var(--blue); border-color: var(--blue); }
.sd-score-dot.won-b { background: var(--orange); border-color: var(--orange); }

/* ── Live badges ────────────────────────── */
.sd-live-badges {
    position: absolute;
    top: 16px;
    z-index: 10;
}
.sd-live-badges.left { left: 16px; }
.sd-live-badges.right { right: 16px; }
.sd-badge {
    background: rgba(12, 6, 22, 0.85);
    border-radius: 10px;
    padding: 10px 18px;
    min-width: 160px;
}
.sd-badge.blue { border-left: 3px solid var(--blue); }
.sd-badge.orange { border-right: 3px solid var(--orange); text-align: right; }
.sd-badge-name { font-size: 20px; font-weight: 600; }
.sd-badge-series {
    font-family: 'Orbitron', sans-serif;
    font-size: 14px;
    color: var(--text-dim);
    margin-top: 2px;
}

/* ── Series result ──────────────────────── */
.sd-series-result {
    background: var(--bg);
    flex-direction: column;
    gap: 20px;
}
.sd-result-winner {
    font-family: 'Orbitron', sans-serif;
    font-size: 42px;
    color: var(--gold);
}
.sd-result-score {
    font-size: 24px;
    color: var(--text-dim);
}

/* ── Final result ───────────────────────── */
.sd-final {
    background: var(--bg);
    flex-direction: column;
    gap: 24px;
}
.sd-crown { font-size: 80px; }
.sd-final-title {
    font-family: 'Orbitron', sans-serif;
    font-size: 48px;
    color: var(--gold);
    letter-spacing: 6px;
    text-shadow: 0 0 30px rgba(251, 191, 36, 0.4);
}
.sd-final-name {
    font-size: 36px;
    font-weight: 700;
}
.sd-final-scores {
    display: flex;
    gap: 40px;
    margin-top: 12px;
}
.sd-final-series-score {
    text-align: center;
}
.sd-final-series-mode {
    font-family: 'Orbitron', sans-serif;
    font-size: 14px;
    color: var(--accent-light);
    letter-spacing: 2px;
}
.sd-final-series-val {
    font-size: 22px;
    margin-top: 4px;
}
</style>
</head>
<body>
<div id="overlay">
    <div id="layer-idle" class="sd-layer sd-opening"></div>
    <div id="layer-series_intro" class="sd-layer sd-series-intro"></div>
    <div id="layer-pregame" class="sd-layer sd-pregame"></div>
    <div id="layer-live-blue" class="sd-live-badges left" style="display:none;"></div>
    <div id="layer-live-orange" class="sd-live-badges right" style="display:none;"></div>
    <div id="layer-series_result" class="sd-layer sd-series-result"></div>
    <div id="layer-final_result" class="sd-layer sd-final"></div>
</div>
<script src="/static/overlay.js?v=showdown"></script>
<script>
const FADE = 300;
let _curPhase = null;

function escHtml(s) {
    const d = document.createElement("div");
    d.textContent = s || "";
    return d.innerHTML;
}

function logoUrl(side) { return `/api/logo/${side}`; }

function _allLayers() {
    return [
        document.getElementById("layer-idle"),
        document.getElementById("layer-series_intro"),
        document.getElementById("layer-pregame"),
        document.getElementById("layer-live-blue"),
        document.getElementById("layer-live-orange"),
        document.getElementById("layer-series_result"),
        document.getElementById("layer-final_result"),
    ];
}

function _hideAll() {
    for (const el of _allLayers()) {
        el.style.opacity = "0";
        el.style.display = "none";
        el.classList.remove("active");
    }
}

function _show(el, display) {
    display = display || "flex";
    el.style.display = display;
    void el.offsetHeight;
    el.style.opacity = "1";
    el.classList.add("active");
}

function phaseGroup(p) {
    if (p === "countdown" || p === "live" || p === "replay") return "live";
    if (p === "postgame") return "postgame_badges";
    return p;
}

window.renderState = function(state) {
    const pg = phaseGroup(state.phase);
    const a = state.bot_a;
    const b = state.bot_b;
    const curSeries = state.series_states[state.current_series] || {};

    if (pg !== _curPhase) {
        _hideAll();
        _curPhase = pg;
    }

    if (pg === "idle") {
        const el = document.getElementById("layer-idle");
        // Countdown timer
        let countdownHtml = "";
        if (state.countdown_end > 0) {
            countdownHtml = `<div id="sd-countdown" class="sd-countdown"></div>`;
        }
        el.innerHTML = `
            <img class="sd-bot-logo" src="/static/rlgym.png" alt="" style="width:64px;height:64px;">
            <div class="sd-title">MASTER CHEF SHOWDOWN</div>
            <div class="sd-subtitle">Best of 3 Series — 1v1 · 3v3 · 2v2</div>
            <div class="sd-vs-block">
                <div class="sd-bot-card">
                    ${a.logo_path ? `<img class="sd-bot-logo" src="${logoUrl('a')}" alt="">` : ""}
                    <div class="sd-bot-name">${escHtml(a.name)}</div>
                    <div class="sd-bot-author">by ${escHtml(a.author)}</div>
                    ${a.description ? `<div class="sd-bot-desc">${escHtml(a.description)}</div>` : ""}
                </div>
                <div class="sd-vs-text">VS</div>
                <div class="sd-bot-card">
                    ${b.logo_path ? `<img class="sd-bot-logo" src="${logoUrl('b')}" alt="">` : ""}
                    <div class="sd-bot-name">${escHtml(b.name)}</div>
                    <div class="sd-bot-author">by ${escHtml(b.author)}</div>
                    ${b.description ? `<div class="sd-bot-desc">${escHtml(b.description)}</div>` : ""}
                </div>
            </div>
            <div class="sd-format">
                Three best-of-7 series: <strong>1v1</strong>, then <strong>3v3</strong>, then <strong>2v2</strong><br>
                First to win 2 series crowns their maker <span style="color:var(--gold);">Master Chef</span> 👨‍🍳
            </div>
            ${countdownHtml}`;
        _show(el);
        // Start countdown ticker
        if (state.countdown_end > 0) _startCountdown(state.countdown_end);

    } else if (pg === "series_intro") {
        const el = document.getElementById("layer-series_intro");
        const modeLabel = state.current_series.toUpperCase();
        const seriesIdx = state.series_order.indexOf(state.current_series) + 1;
        el.innerHTML = `
            <img src="/static/rlgym.png" alt="" style="width:48px;height:48px;opacity:0.7;">
            <div class="sd-series-label">SERIES ${seriesIdx} OF 3</div>
            <div class="sd-series-mode">${escHtml(modeLabel)}</div>
            <div class="sd-series-label">BEST OF ${curSeries.best_of || 7}</div>
            <div class="sd-overall-score">
                <span class="sd-overall-a">${escHtml(a.name)}</span>
                <span style="color:var(--text-dim);"> ${state.overall_wins_a} - ${state.overall_wins_b} </span>
                <span class="sd-overall-b">${escHtml(b.name)}</span>
            </div>`;
        _show(el);

    } else if (pg === "pregame") {
        const el = document.getElementById("layer-pregame");
        const aIsBlue = state.a_is_blue;
        const blueBot = aIsBlue ? a : b;
        const orangeBot = aIsBlue ? b : a;
        const blueSide = aIsBlue ? "a" : "b";
        const orangeSide = aIsBlue ? "b" : "a";

        // Series score dots
        const wa = curSeries.wins_a || 0;
        const wb = curSeries.wins_b || 0;
        const winsNeeded = Math.floor((curSeries.best_of || 7) / 2) + 1;
        let dots = "";
        for (let i = 0; i < winsNeeded; i++) {
            dots += `<div class="sd-score-dot ${i < wa ? 'won-a' : ''}"></div>`;
        }
        dots += `<span style="font-size:14px;color:var(--text-dim);margin:0 4px;">—</span>`;
        for (let i = 0; i < winsNeeded; i++) {
            dots += `<div class="sd-score-dot ${i < wb ? 'won-b' : ''}"></div>`;
        }

        el.innerHTML = `
            <div class="sd-pre-header">
                <div class="sd-pre-mode">${escHtml(state.current_series.toUpperCase())} — GAME ${curSeries.current_game || 1}</div>
                <div class="sd-pre-map">${escHtml(state.map_name)}</div>
            </div>
            <div class="sd-pre-teams">
                <div class="sd-pre-side blue">
                    <div class="sd-pre-side-label">BLUE</div>
                    ${blueBot.logo_path ? `<img class="sd-pre-bot-logo" src="${logoUrl(blueSide)}" alt="">` : ""}
                    <div class="sd-pre-bot-name">${escHtml(blueBot.name)}</div>
                </div>
                <div class="sd-pre-vs">VS</div>
                <div class="sd-pre-side orange">
                    <div class="sd-pre-side-label">ORANGE</div>
                    ${orangeBot.logo_path ? `<img class="sd-pre-bot-logo" src="${logoUrl(orangeSide)}" alt="">` : ""}
                    <div class="sd-pre-bot-name">${escHtml(orangeBot.name)}</div>
                </div>
            </div>
            <div class="sd-series-score-bar">${dots}</div>
            <div style="text-align:center;margin-top:12px;">
                <span style="color:var(--blue);">${escHtml(a.name)}</span>
                <span style="color:var(--text-dim);font-family:'Orbitron',sans-serif;font-size:20px;"> ${wa} - ${wb} </span>
                <span style="color:var(--orange);">${escHtml(b.name)}</span>
            </div>`;
        _show(el);

    } else if (pg === "live" || pg === "postgame_badges") {
        const blueEl = document.getElementById("layer-live-blue");
        const orangeEl = document.getElementById("layer-live-orange");
        const aIsBlue = state.a_is_blue;
        const blueBot = aIsBlue ? a : b;
        const orangeBot = aIsBlue ? b : a;
        const wa = curSeries.wins_a || 0;
        const wb = curSeries.wins_b || 0;

        const oa = state.overall_wins_a || 0;
        const ob = state.overall_wins_b || 0;

        function seriesLines(isA) {
            let lines = "";
            for (const mode of state.series_order) {
                const s = state.series_states[mode] || {};
                const swa = s.wins_a || 0;
                const swb = s.wins_b || 0;
                const myW = isA ? swa : swb;
                const thW = isA ? swb : swa;
                const isCurrent = mode === state.current_series;
                const style = isCurrent ? "color:#f0f0f0;" : "";
                let indicator = "";
                if (s.series_winner) {
                    const won = (s.series_winner === "a") === isA;
                    indicator = won ? ' <span style="color:#4ade80;">✓</span>' : ' <span style="color:#f87171;">✗</span>';
                }
                lines += `<div class="sd-badge-series" style="${style}">${mode.toUpperCase()} ${myW}-${thW}${indicator}</div>`;
            }
            return lines;
        }

        const blueIsA = aIsBlue;
        blueEl.innerHTML = `<div class="sd-badge blue">
            <div class="sd-badge-name">${escHtml(blueBot.name)}</div>
            ${seriesLines(blueIsA)}
        </div>`;
        orangeEl.innerHTML = `<div class="sd-badge orange">
            <div class="sd-badge-name">${escHtml(orangeBot.name)}</div>
            ${seriesLines(!blueIsA)}
        </div>`;
        _show(blueEl);
        _show(orangeEl);

    } else if (pg === "series_result") {
        const el = document.getElementById("layer-series_result");
        const wa = curSeries.wins_a || 0;
        const wb = curSeries.wins_b || 0;
        const winner = curSeries.series_winner === "a" ? a.name : b.name;
        el.innerHTML = `
            <div class="sd-pre-mode">${escHtml(state.current_series.toUpperCase())} SERIES</div>
            <div class="sd-result-winner">${escHtml(winner)} wins!</div>
            <div class="sd-result-score">${wa} - ${wb}</div>
            <div class="sd-overall-score" style="margin-top:24px;">
                <span style="font-size:16px;color:var(--text-dim);letter-spacing:2px;">OVERALL</span><br>
                <span class="sd-overall-a">${escHtml(a.name)}</span>
                <span style="color:var(--text-dim);"> ${state.overall_wins_a} - ${state.overall_wins_b} </span>
                <span class="sd-overall-b">${escHtml(b.name)}</span>
            </div>`;
        _show(el);

    } else if (pg === "final_result") {
        const el = document.getElementById("layer-final_result");
        const winner = state.showdown_winner === "a" ? a : b;
        // Build per-series scores
        let scoresHtml = "";
        for (const mode of state.series_order) {
            const s = state.series_states[mode] || {};
            if (!s.series_winner) continue;
            const wa = s.wins_a || 0;
            const wb = s.wins_b || 0;
            const sw = s.series_winner === "a" ? a.name : b.name;
            scoresHtml += `<div class="sd-final-series-score">
                <div class="sd-final-series-mode">${escHtml(mode.toUpperCase())}</div>
                <div class="sd-final-series-val">${wa} - ${wb}</div>
            </div>`;
        }
        el.innerHTML = `
            <div class="sd-crown">👨‍🍳</div>
            <div class="sd-final-title">MASTER CHEF</div>
            ${winner.logo_path ? `<img class="sd-bot-logo" src="${logoUrl(state.showdown_winner)}" alt="" style="width:100px;height:100px;">` : ""}
            <div class="sd-final-name">${escHtml(winner.author)}</div>
            <div style="font-size:16px;color:var(--text-dim);">with ${escHtml(winner.name)}</div>
            <div style="font-size:22px;margin-top:8px;">
                <span class="sd-overall-a">${state.overall_wins_a}</span>
                <span style="color:var(--text-dim);"> — </span>
                <span class="sd-overall-b">${state.overall_wins_b}</span>
                <span style="color:var(--text-dim);font-size:14px;"> series</span>
            </div>
            <div class="sd-final-scores">${scoresHtml}</div>`;
        _show(el);
    }
/* ── Utilities ─── */
let _countdownInterval = null;
function _startCountdown(endTimestamp) {
    if (_countdownInterval) clearInterval(_countdownInterval);
    function tick() {
        const el = document.getElementById("sd-countdown");
        if (!el) { clearInterval(_countdownInterval); return; }
        const remaining = Math.max(0, Math.ceil(endTimestamp - Date.now() / 1000));
        const m = Math.floor(remaining / 60);
        const s = remaining % 60;
        el.textContent = "Starting in " + m + ":" + String(s).padStart(2, "0");
        if (remaining <= 0) {
            el.textContent = "Starting...";
            clearInterval(_countdownInterval);
        }
    }
    tick();
    _countdownInterval = setInterval(tick, 500);
}
};
</script>
</body>
</html>"""


# ── CLI ──────────────────────────────────────────────────────────────

async def run_preview(port: int, bot_a: BotInfo | None, bot_b: BotInfo | None) -> None:
    """Start the overlay with mock data and cycle through every phase."""
    a = bot_a or BotInfo(name="AlphaBot", author="Alice", description="A very smart bot that uses deep reinforcement learning.")
    b = bot_b or BotInfo(name="BetaBot", author="Bob", description="A classic rule-based bot with excellent mechanics.")

    state = ShowdownState()
    state.bot_a = {"name": a.name, "author": a.author, "description": a.description, "logo_path": a.logo_path}
    state.bot_b = {"name": b.name, "author": b.author, "description": b.description, "logo_path": b.logo_path}
    for mode, _ in SERIES_MODES:
        state.series_states[mode] = asdict(SeriesState(mode=mode))

    app = create_showdown_app(state)
    server_cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(server_cfg)
    server_task = asyncio.create_task(server.serve())
    logger.info("Preview overlay at http://127.0.0.1:%d — cycling through phases…", port)

    stop = asyncio.Event()

    try:
        while not stop.is_set():
            # 1. Opening
            state.phase = "idle"
            state.bump()
            logger.info("Phase: idle (opening)")
            await _sleep(8, stop)

            # 2. Series intro — 1v1
            state.current_series = "1v1"
            state.phase = "series_intro"
            state.bump()
            logger.info("Phase: series_intro (1v1)")
            await _sleep(6, stop)

            # 3. Pregame
            state.a_is_blue = True
            state.map_name = "DFH Stadium"
            ss = SeriesState(mode="1v1", team_size=1, current_game=1, in_progress=True)
            state.series_states["1v1"] = asdict(ss)
            state.phase = "pregame"
            state.score_blue = 0
            state.score_orange = 0
            state.bump()
            logger.info("Phase: pregame")
            await _sleep(8, stop)

            # 4. Live
            state.phase = "live"
            state.score_blue = 2
            state.score_orange = 1
            state.bump()
            logger.info("Phase: live")
            await _sleep(6, stop)

            # 5. Postgame badges
            state.phase = "postgame"
            state.score_blue = 3
            state.score_orange = 1
            state.bump()
            logger.info("Phase: postgame")
            await _sleep(6, stop)

            # 6. Series result
            ss.wins_a = 4
            ss.wins_b = 2
            ss.series_winner = "a"
            ss.in_progress = False
            state.series_states["1v1"] = asdict(ss)
            state.overall_wins_a = 1
            state.phase = "series_result"
            state.bump()
            logger.info("Phase: series_result")
            await _sleep(8, stop)

            # 7. Final result
            state.overall_wins_a = 2
            state.overall_wins_b = 1
            state.showdown_winner = "a"
            ss3 = SeriesState(mode="3v3", wins_a=2, wins_b=4, series_winner="b")
            ss2 = SeriesState(mode="2v2", wins_a=4, wins_b=3, series_winner="a")
            state.series_states["3v3"] = asdict(ss3)
            state.series_states["2v2"] = asdict(ss2)
            state.phase = "final_result"
            state.bump()
            logger.info("Phase: final_result")
            await _sleep(10, stop)

            # Reset for next loop
            state.overall_wins_a = 0
            state.overall_wins_b = 0
            state.showdown_winner = ""
            for mode, _ in SERIES_MODES:
                state.series_states[mode] = asdict(SeriesState(mode=mode))
            logger.info("Cycling again…")

    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        server.should_exit = True
        await server_task


def main():
    parser = argparse.ArgumentParser(description="Master Chef Showdown")
    parser.add_argument("bot_a", nargs="?", help="Path to bot A's .toml config")
    parser.add_argument("bot_b", nargs="?", help="Path to bot B's .toml config")
    parser.add_argument("--overlay-port", type=int, default=8090,
                        help="Port for the showdown overlay (default: 8090)")
    parser.add_argument("--preview", action="store_true",
                        help="Preview the overlay with mock data (no games launched)")
    parser.add_argument("--quick", action="store_true",
                        help="Dry-run: Bo1 series with short delays to verify the full flow")
    parser.add_argument("--1v1", dest="score_1v1", metavar="A-B",
                        help="Resume 1v1 series at this score (e.g. 3-2)")
    parser.add_argument("--3v3", dest="score_3v3", metavar="A-B",
                        help="Resume 3v3 series at this score (e.g. 4-1)")
    parser.add_argument("--2v2", dest="score_2v2", metavar="A-B",
                        help="Resume 2v2 series at this score (e.g. 0-0)")
    parser.add_argument("--skip-opening", action="store_true",
                        help="Skip the opening countdown screen")
    args = parser.parse_args()

    bot_a = BotInfo.from_toml(Path(args.bot_a)) if args.bot_a else None
    bot_b = BotInfo.from_toml(Path(args.bot_b)) if args.bot_b else None

    if args.preview:
        logger.info("Starting overlay preview…")
        asyncio.run(run_preview(args.overlay_port, bot_a, bot_b))
    else:
        if not bot_a or not bot_b:
            parser.error("bot_a and bot_b are required (unless using --preview)")

        # Parse resume scores
        resume: dict[str, tuple[int, int]] = {}
        for mode_key, attr in [("1v1", "score_1v1"), ("3v3", "score_3v3"), ("2v2", "score_2v2")]:
            val = getattr(args, attr)
            if val:
                try:
                    a_w, b_w = val.split("-")
                    resume[mode_key] = (int(a_w), int(b_w))
                except ValueError:
                    parser.error(f"Invalid score format for --{mode_key}: '{val}' (expected A-B, e.g. 3-2)")

        logger.info("Master Chef Showdown: %s vs %s", bot_a.name, bot_b.name)
        asyncio.run(run_showdown(bot_a, bot_b, args.overlay_port, quick=args.quick,
                                 resume=resume, skip_opening=args.skip_opening))


if __name__ == "__main__":
    main()
