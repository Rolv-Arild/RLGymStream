"""Twitch chatbot — responds to viewer commands with bot stats and match info.

Uses twitchio v3 with EventSub websockets for chat.

Commands:
    !help                          — list available commands
    !mmr <bot>                     — show a bot's MMR across all modes
    !rating <bot> [mode]           — show raw mu and sigma values
    !pos <bot> [mode]              — show a bot's rank position (alias: !position)
    !lb <mode>                     — show top 5 leaderboard for a mode
    !best [mode]                   — show the #1 bot per mode (or one mode)
    !match                         — show current/last match info
    !h2h <botA> vs <botB> [mode|overall|standard|solo] — head-to-head record
    !h2h current [mode]            — head-to-head for the current match
    !goals <botA> vs <botB> [mode|overall|standard|solo] — head-to-head goal totals + duration
    !bot <name> [mode]             — bot info (author, description, win/loss)
    !info                          — total matches, number of bots, etc.
    !stats [top|bottom|bot] <stat|bot> <mode> — stat leaders or per-bot averages
    !winrate <bot> [mode]          — win rate per mode or for a specific mode
    !streak <bot> [mode]           — current win/loss streak
    !last [N] [mode] [bot]         — last 1-3 match results, filtered by mode and/or bot
    !predict <team1> vs <team2> <mode> — predict any matchup (comma-separated teams)
    !predict <MMR> vs <MMR>        — predict from raw MMR values
    !predict current               — win probability for current match
    !map                           — current map name
    !modes                         — active mode rotation
    !uptime                        — how long the bot has been running

All commands support "help" as an argument (e.g. !h2h help) to show usage.
Mode shortcuts: 1v1, 2v2, 3v3, 1s, 2s, 3s, solo2v2, solo3v3, etc.

Commands that show a single mode's data (!lb, !stats, !predict) require the
mode to be given explicitly — modes are never mixed or silently defaulted.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
from typing import TYPE_CHECKING

import twitchio  # noqa: F401 — needed at runtime for eventsub
from twitchio import authentication, eventsub
from twitchio.ext import commands

if TYPE_CHECKING:
    from rlgymstream.config import AppConfig
    from rlgymstream.db.database import Database
    from rlgymstream.overlay.state import OverlayState

logger = logging.getLogger("rlgymstream.chat")

ALL_MODES = ["1v1", "2v2", "3v3", "solo_2v2", "solo_3v3"]

MODE_DISPLAY = {
    "1v1": "1v1",
    "2v2": "2v2",
    "3v3": "3v3",
    "solo_2v2": "Solo 2v2",
    "solo_3v3": "Solo 3v3",
}

# Aliases viewers might type for modes
MODE_ALIASES = {
    "1v1": "1v1", "1s": "1v1", "ones": "1v1",
    "2v2": "2v2", "2s": "2v2", "twos": "2v2",
    "3v3": "3v3", "3s": "3v3", "threes": "3v3",
    "solo2v2": "solo_2v2", "solo_2v2": "solo_2v2", "solo2s": "solo_2v2",
    "solo3v3": "solo_3v3", "solo_3v3": "solo_3v3", "solo3s": "solo_3v3",
}


def _mmr(mu: float) -> int:
    """Convert mu to display MMR (same formula as main.py)."""
    return round(20 * mu + 100)


MODES_HINT = "1v1, 2v2, 3v3, solo2v2, solo3v3"

_VS_RE = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)


def split_vs(text: str) -> tuple[str, str] | None:
    """Split "A vs B" (any capitalisation, optional "vs.") into (A, B).

    Returns None if there is no " vs " separator.
    """
    parts = _VS_RE.split(text.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    return parts[0].strip(), parts[1].strip()


# ── Component holding all chat commands ──────────────────────────────


class ChatCommands(commands.Component):
    """Component that holds all viewer-facing chat commands."""

    def __init__(self, bot: RLGymStreamBot) -> None:
        self.bot: RLGymStreamBot = bot

    # ── Helpers ──────────────────────────────────────────────

    def _find_bot(self, name: str):
        """Case-insensitive bot lookup with fuzzy fallback."""
        bot = self.bot._db.get_bot_by_name(name)
        if bot:
            return bot, None

        all_bots = self.bot._db.get_all_bots(enabled_only=False)
        name_lower = name.lower()
        for b in all_bots:
            if b.name.lower() == name_lower:
                return b, None

        bot_names = [b.name for b in all_bots]
        matches = difflib.get_close_matches(name, bot_names, n=3, cutoff=0.5)
        if matches:
            suggestions = ", ".join(matches)
            return None, f'Bot "{name}" not found. Did you mean: {suggestions}?'
        return None, f'Bot "{name}" not found.'

    def _resolve_mode(self, arg: str | None) -> str | None:
        if not arg:
            return None
        return MODE_ALIASES.get(arg.lower().strip())

    def _split_name_and_mode(self, args: str) -> tuple[str, str | None]:
        """Try to extract a trailing mode from args. Returns (name, mode_or_None)."""
        tokens = args.rsplit(" ", 1)
        if len(tokens) == 2:
            maybe_mode = self._resolve_mode(tokens[1])
            if maybe_mode:
                return tokens[0].strip(), maybe_mode
        return args.strip(), None

    @staticmethod
    def _is_help(args: str) -> bool:
        """Check if the user is asking for help on a command."""
        return args.strip().lower() in ("help", "?", "-h", "--help")

    # ── Commands ─────────────────────────────────────────────

    @commands.command(name="help")
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_help(self, ctx: commands.Context) -> None:
        await ctx.send(
            "📋 !mmr · !rating · !pos · !lb · !best · !match · !h2h · !goals "
            "· !bot · !info · !winrate · !streak · !last · !predict · !map · !modes · !uptime "
            "· !stats"
        )

    @commands.command(name="mmr")
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_mmr(self, ctx: commands.Context, *, name: str = "") -> None:
        if not name:
            await ctx.send("Usage: !mmr <bot name>")
            return
        bot, err = self._find_bot(name)
        if not bot:
            await ctx.send(err)
            return
        assert bot.id is not None

        parts = []
        for mode in ALL_MODES:
            r = self.bot._db.get_rating(bot.id, mode)
            if r.matches_played > 0:
                parts.append(f"{MODE_DISPLAY[mode]}: {_mmr(r.mu)}")
        if parts:
            await ctx.send(f"📊 {bot.name} — {' | '.join(parts)}")
        else:
            await ctx.send(f"{bot.name} has no rated games yet.")

    @commands.command(name="rating")
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_rating(self, ctx: commands.Context, *, args: str = "") -> None:
        """!rating <bot> [mode] — show raw mu and sigma values."""
        if not args or self._is_help(args):
            await ctx.send("Usage: !rating <bot name> [mode] — show raw μ and σ values")
            return

        name, mode = self._split_name_and_mode(args)
        bot, err = self._find_bot(name)
        if not bot:
            await ctx.send(err)
            return
        assert bot.id is not None

        modes_to_check = [mode] if mode else ALL_MODES
        parts = []
        for m in modes_to_check:
            r = self.bot._db.get_rating(bot.id, m)
            if r.matches_played > 0 or mode:
                parts.append(f"{MODE_DISPLAY[m]}: μ={r.mu:.2f} σ={r.sigma:.2f} ({r.matches_played} games)")
        if parts:
            await ctx.send(f"🔬 {bot.name} — {' | '.join(parts)}")
        else:
            await ctx.send(f"{bot.name} has no rated games yet.")

    @commands.command(name="lb", aliases=["leaderboard"])
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_lb(self, ctx: commands.Context, mode_arg: str = "") -> None:
        if not mode_arg or self._is_help(mode_arg):
            await ctx.send(f"Usage: !lb <mode> — modes: {MODES_HINT}")
            return
        mode = self._resolve_mode(mode_arg)
        if mode is None:
            await ctx.send(f'Unknown mode "{mode_arg}". Try: {MODES_HINT}')
            return

        with self.bot._overlay._lock:
            entries = self.bot._overlay.leaderboards.get(mode, [])

        if not entries:
            await ctx.send(f"No leaderboard data for {MODE_DISPLAY.get(mode, mode)} yet.")
            return

        top = entries[:5]
        lines = [f"#{e.rank} {e.bot_name} ({e.mmr})" for e in top]
        await ctx.send(f"🏆 {MODE_DISPLAY.get(mode, mode)} Top 5: {' · '.join(lines)}")

    @commands.command(name="match")
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_match(self, ctx: commands.Context) -> None:
        with self.bot._overlay._lock:
            m = self.bot._overlay.match

        if m.phase == "idle" or not m.team_blue:
            await ctx.send("No match in progress right now.")
            return

        blue = ", ".join(dict.fromkeys(b.name for b in m.team_blue))
        orange = ", ".join(dict.fromkeys(b.name for b in m.team_orange))
        mode_label = m.mode_display or m.mode

        if m.phase in ("live", "postgame"):
            await ctx.send(
                f"🎮 [{mode_label}] {blue} {m.score_blue}-{m.score_orange} {orange} "
                f"({m.phase}) on {m.map_name}"
            )
        else:
            await ctx.send(f"🎮 [{mode_label}] {blue} vs {orange} ({m.phase}) on {m.map_name}")

    @commands.command(name="h2h", aliases=["headtohead"])
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_h2h(self, ctx: commands.Context, *, args: str = "") -> None:
        """!h2h <botA> vs <botB> [mode|overall|standard|solo] | !h2h current [mode]"""
        if not args or self._is_help(args):
            await ctx.send(
                "Usage: !h2h <botA> vs <botB> [mode|overall|standard|solo] "
                "or !h2h current [mode]"
            )
            return

        # Mode group keywords → list of modes to show
        _MODE_GROUPS: dict[str, list[str] | None] = {
            "overall": None,  # single query with mode=None
            "all": None,
            "standard": ["1v1", "2v2", "3v3"],
            "solo": ["solo_2v2", "solo_3v3"],
        }

        # Check for "current" keyword → use current match
        rest, mode = self._split_name_and_mode(args)

        # Detect mode-group keywords at the end of *rest*
        mode_group: list[str] | None | str = "PER_MODE"  # sentinel: default per-mode
        if mode is None:
            rest_tokens = rest.rsplit(" ", 1)
            if len(rest_tokens) == 2 and rest_tokens[1].lower() in _MODE_GROUPS:
                keyword = rest_tokens[1].lower()
                rest = rest_tokens[0].strip()
                mode_group = _MODE_GROUPS[keyword]
            elif rest.lower().endswith(" overall") or rest.lower().endswith(" all"):
                # edge-case: the keyword is right after "current" or bot name
                pass  # already handled above

        if rest.lower() == "current":
            with self.bot._overlay._lock:
                m = self.bot._overlay.match

            if m.phase == "idle" or not m.team_blue or not m.team_orange:
                await ctx.send("No match in progress.")
                return

            blue_names = list(dict.fromkeys(b.name for b in m.team_blue))
            orange_names = list(dict.fromkeys(b.name for b in m.team_orange))
            if len(blue_names) != 1 or len(orange_names) != 1:
                await ctx.send("H2H for current match only works in standard modes. Use: !h2h <botA> vs <botB>")
                return

            name_a, name_b = blue_names[0], orange_names[0]
        elif (vs := split_vs(rest)) is not None:
            name_a, name_b = vs
        elif " " in rest.strip():
            tokens = rest.strip().rsplit(" ", 1)
            name_a, name_b = tokens[0].strip(), tokens[1].strip()
        else:
            await ctx.send("Usage: !h2h <botA> vs <botB> [mode|overall|standard|solo]")
            return

        bot_a, err_a = self._find_bot(name_a)
        if not bot_a:
            await ctx.send(err_a)
            return
        bot_b, err_b = self._find_bot(name_b)
        if not bot_b:
            await ctx.send(err_b)
            return
        assert bot_a.id is not None and bot_b.id is not None

        # If an explicit mode was parsed (e.g. "1v1"), show just that mode
        if mode:
            h2h = self.bot._db.get_head_to_head(bot_a.id, bot_b.id, mode=mode)
            if h2h["total"] == 0:
                await ctx.send(f"{bot_a.name} and {bot_b.name} have never played each other in {MODE_DISPLAY[mode]}.")
                return
            await ctx.send(
                f"⚔️ {bot_a.name} vs {bot_b.name} in {MODE_DISPLAY[mode]}: "
                f"{h2h['wins_a']}W-{h2h['wins_b']}L ({h2h['total']} games)"
            )
            return

        # "overall" → single aggregated query
        if mode_group is None:
            h2h = self.bot._db.get_head_to_head(bot_a.id, bot_b.id, mode=None)
            if h2h["total"] == 0:
                await ctx.send(f"{bot_a.name} and {bot_b.name} have never played each other.")
                return
            await ctx.send(
                f"⚔️ {bot_a.name} vs {bot_b.name} overall: "
                f"{h2h['wins_a']}W-{h2h['wins_b']}L ({h2h['total']} games)"
            )
            return

        # Per-mode breakdown (default, or "standard"/"solo" group)
        modes_to_check = mode_group if isinstance(mode_group, list) else ALL_MODES
        parts = []
        for m in modes_to_check:
            h2h = self.bot._db.get_head_to_head(bot_a.id, bot_b.id, mode=m)
            if h2h["total"] == 0:
                continue
            parts.append(
                f"{MODE_DISPLAY[m]}: {h2h['wins_a']}W-{h2h['wins_b']}L ({h2h['total']})"
            )

        if parts:
            await ctx.send(f"⚔️ {bot_a.name} vs {bot_b.name} — {' | '.join(parts)}")
        else:
            await ctx.send(f"{bot_a.name} and {bot_b.name} have never played each other.")

    @commands.command(name="bot")
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_bot(self, ctx: commands.Context, *, args: str = "") -> None:
        """!bot <name> [mode] — bot info, optionally with record for a specific mode."""
        if not args or self._is_help(args):
            await ctx.send("Usage: !bot <bot name> [mode] — show bot info and record")
            return

        name, mode = self._split_name_and_mode(args)
        bot, err = self._find_bot(name)
        if not bot:
            await ctx.send(err)
            return
        assert bot.id is not None

        author = bot.author or "Unknown"
        desc = bot.description[:120] + "…" if len(bot.description) > 120 else bot.description

        modes_to_check = [mode] if mode else ALL_MODES
        total_w, total_l = 0, 0
        for m in modes_to_check:
            w, l, _d = self.bot._db.get_bot_record(bot.id, m)
            total_w += w
            total_l += l

        parts = [f"🤖 {bot.name} by {author}"]
        if desc:
            parts.append(desc)
        mode_label = f" ({MODE_DISPLAY[mode]})" if mode else ""
        parts.append(f"Record{mode_label}: {total_w}W-{total_l}L")
        if bot.language:
            parts.append(f"Lang: {bot.language}")

        await ctx.send(" | ".join(parts))

    @commands.command(name="info")
    @commands.cooldown(rate=1, per=10, key=commands.BucketType.channel)
    async def cmd_info(self, ctx: commands.Context) -> None:
        total_matches = self.bot._db.get_match_count()
        all_bots = self.bot._db.get_all_bots(enabled_only=True)
        await ctx.send(
            f"📈 {total_matches} matches played with {len(all_bots)} active bots "
            f"across {len(self.bot._config.mode_rotation)} modes"
        )

    @commands.command(name="winrate", aliases=["wr"])
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_winrate(self, ctx: commands.Context, *, args: str = "") -> None:
        """!winrate <bot> [mode] — win rate per mode or for a specific mode."""
        if not args or self._is_help(args):
            await ctx.send("Usage: !winrate <bot name> [mode] — show win rate")
            return

        name, mode = self._split_name_and_mode(args)
        bot, err = self._find_bot(name)
        if not bot:
            await ctx.send(err)
            return
        assert bot.id is not None

        modes_to_check = [mode] if mode else ALL_MODES
        parts = []
        for m in modes_to_check:
            w, l, _d = self.bot._db.get_bot_record(bot.id, m)
            total = w + l  # draws don't count towards win rate
            if total == 0:
                continue
            pct = round(100 * w / total)
            parts.append(f"{MODE_DISPLAY[m]}: {pct}% ({w}-{l})")

        if parts:
            await ctx.send(f"📊 {bot.name} win rates — {' | '.join(parts)}")
        else:
            await ctx.send(f"{bot.name} has no games played yet.")

    @commands.command(name="pos", aliases=["position"])
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_rank(self, ctx: commands.Context, *, args: str = "") -> None:
        """!pos <bot> [mode] — show a bot's rank position."""
        if not args or self._is_help(args):
            await ctx.send("Usage: !pos <bot name> [mode] — show leaderboard position")
            return

        name, mode = self._split_name_and_mode(args)
        bot, err = self._find_bot(name)
        if not bot:
            await ctx.send(err)
            return
        assert bot.id is not None

        modes_to_check = [mode] if mode else ALL_MODES
        parts = []
        for m in modes_to_check:
            with self.bot._overlay._lock:
                entries = self.bot._overlay.leaderboards.get(m, [])
            for i, e in enumerate(entries):
                if e.bot_name == bot.name:
                    parts.append(f"{MODE_DISPLAY[m]}: #{i + 1}/{len(entries)} ({e.mmr} MMR)")
                    break

        if parts:
            await ctx.send(f"🏅 {bot.name} — {' | '.join(parts)}")
        else:
            await ctx.send(f"{bot.name} is not ranked in any mode yet.")

    @commands.command(name="best")
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_top(self, ctx: commands.Context, mode_arg: str = "") -> None:
        """!best [mode] — show the #1 bot per mode, or for a specific mode."""
        if mode_arg:
            mode = self._resolve_mode(mode_arg)
            if mode is None:
                await ctx.send(f'Unknown mode "{mode_arg}". Try: 1v1, 2v2, 3v3, solo2v2, solo3v3')
                return
            modes_to_show = [mode]
        else:
            modes_to_show = ALL_MODES

        parts = []
        with self.bot._overlay._lock:
            for m in modes_to_show:
                entries = self.bot._overlay.leaderboards.get(m, [])
                if entries:
                    e = entries[0]
                    parts.append(f"{MODE_DISPLAY[m]}: {e.bot_name} ({e.mmr})")

        if parts:
            await ctx.send(f"👑 #1 — {' | '.join(parts)}")
        else:
            await ctx.send("No leaderboard data yet.")

    @commands.command(name="streak")
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_streak(self, ctx: commands.Context, *, args: str = "") -> None:
        """!streak <bot> [mode] — current win/loss streak, optionally per mode."""
        if not args or self._is_help(args):
            await ctx.send("Usage: !streak <bot name> [mode] — show win/loss streak")
            return

        name, mode = self._split_name_and_mode(args)
        bot, err = self._find_bot(name)
        if not bot:
            await ctx.send(err)
            return
        assert bot.id is not None

        matches = self.bot._db.get_recent_matches(limit=None, mode=mode)
        bid = str(bot.id)
        streak_type = ""
        streak_count = 0

        for m in matches:
            blue_ids = m.team_blue_ids.split(",")
            orange_ids = m.team_orange_ids.split(",")
            in_blue = bid in blue_ids
            in_orange = bid in orange_ids
            if not (in_blue or in_orange):
                continue
            if m.winner == "draw":
                break

            won = (m.winner == "blue" and in_blue) or (m.winner == "orange" and in_orange)
            result = "W" if won else "L"

            if not streak_type:
                streak_type = result
            if result == streak_type:
                streak_count += 1
            else:
                break

        mode_label = f" in {MODE_DISPLAY[mode]}" if mode else ""
        if streak_count == 0:
            await ctx.send(f"{bot.name} has no recent matches{mode_label}.")
        else:
            emoji = "🔥" if streak_type == "W" else "❄️"
            await ctx.send(f"{emoji} {bot.name}: {streak_count} game {'win' if streak_type == 'W' else 'loss'} streak{mode_label}")

    @commands.command(name="last")
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_last(self, ctx: commands.Context, *, args: str = "1") -> None:
        """!last [N] [mode] [bot] — show the last 1-3 match results, optionally filtered by mode and/or bot."""
        if self._is_help(args):
            await ctx.send("Usage: !last [N] [mode] [bot] — show last 1-3 matches")
            return
        # Parse optional count, mode, and bot name from args
        tokens = args.strip().split()
        n = 1
        mode = None
        bot_name_parts = []
        for tok in tokens:
            maybe_mode = self._resolve_mode(tok)
            if maybe_mode:
                mode = maybe_mode
            else:
                try:
                    n = max(1, min(3, int(tok)))
                except ValueError:
                    bot_name_parts.append(tok)

        # Resolve bot filter if provided
        filter_bot = None
        if bot_name_parts:
            bot_name = " ".join(bot_name_parts)
            filter_bot, err = self._find_bot(bot_name)
            if not filter_bot:
                await ctx.send(err)
                return

        # Mode is filtered in SQL; when filtering by bot, fetch all matches for that mode
        matches = self.bot._db.get_recent_matches(
            limit=None if filter_bot else n, mode=mode,
        )

        if filter_bot:
            bid = str(filter_bot.id)
            matches = [m for m in matches
                       if bid in m.team_blue_ids.split(",") or bid in m.team_orange_ids.split(",")]
        matches = matches[:n]

        if not matches:
            parts = []
            if mode:
                parts.append(MODE_DISPLAY[mode])
            if filter_bot:
                parts.append(filter_bot.name)
            label = f" for {', '.join(parts)}" if parts else ""
            await ctx.send(f"No matches found{label}.")
            return

        bots = {b.id: b.name for b in self.bot._db.get_all_bots(enabled_only=False)}
        lines = []
        for m in matches:
            blue_names = ", ".join(dict.fromkeys(
                bots.get(int(i), f"#{i}") for i in m.team_blue_ids.split(",")
            ))
            orange_names = ", ".join(dict.fromkeys(
                bots.get(int(i), f"#{i}") for i in m.team_orange_ids.split(",")
            ))
            from rlgymstream.matchmaking.matchmaker import format_map_name
            map_display = format_map_name(m.map_name)
            lines.append(f"{blue_names} {m.score_blue}-{m.score_orange} {orange_names} ({m.mode}, {map_display})")

        await ctx.send(" | ".join(lines))

    @commands.command(name="goals")
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_goals(self, ctx: commands.Context, *, args: str = "") -> None:
        """!goals <botA> vs <botB> [mode|overall|standard|solo] — head-to-head goal totals + total duration."""
        if not args or self._is_help(args):
            await ctx.send("Usage: !goals <botA> vs <botB> [mode|overall|standard|solo]")
            return

        _MODE_GROUPS: dict[str, list[str] | None] = {
            "overall": None,
            "all": None,
            "standard": ["1v1", "2v2", "3v3"],
            "solo": ["solo_2v2", "solo_3v3"],
        }

        rest, mode = self._split_name_and_mode(args)

        # Detect mode-group keywords at the end
        mode_group: list[str] | None | str = "PER_MODE"
        if mode is None:
            rest_tokens = rest.rsplit(" ", 1)
            if len(rest_tokens) == 2 and rest_tokens[1].lower() in _MODE_GROUPS:
                keyword = rest_tokens[1].lower()
                rest = rest_tokens[0].strip()
                mode_group = _MODE_GROUPS[keyword]

        # Parse bot names
        if (vs := split_vs(rest)) is not None:
            name_a, name_b = vs
        elif " " in rest.strip():
            tokens = rest.strip().rsplit(" ", 1)
            name_a, name_b = tokens[0].strip(), tokens[1].strip()
        else:
            await ctx.send("Usage: !goals <botA> vs <botB> [mode|overall|standard|solo]")
            return

        bot_a, err_a = self._find_bot(name_a)
        if not bot_a:
            await ctx.send(err_a)
            return
        bot_b, err_b = self._find_bot(name_b)
        if not bot_b:
            await ctx.send(err_b)
            return
        assert bot_a.id is not None and bot_b.id is not None

        def _fmt_duration(seconds: float) -> str:
            h, rem = divmod(int(seconds), 3600)
            m, s = divmod(rem, 60)
            if h > 0:
                return f"{h}h{m}m"
            return f"{m}m{s}s"

        def _goals_line(h2h: dict, label: str) -> str | None:
            if h2h["total"] == 0:
                return None
            avg_a = h2h["goals_a"] / h2h["total"]
            avg_b = h2h["goals_b"] / h2h["total"]
            dur = _fmt_duration(h2h["total_duration"])
            return (
                f"{label}: {h2h['goals_a']}-{h2h['goals_b']} goals "
                f"({avg_a:.1f}-{avg_b:.1f}/game, {dur} total)"
            )

        # Specific mode
        if mode:
            h2h = self.bot._db.get_head_to_head(bot_a.id, bot_b.id, mode=mode)
            line = _goals_line(h2h, MODE_DISPLAY[mode])
            if not line:
                await ctx.send(f"{bot_a.name} and {bot_b.name} have never played in {MODE_DISPLAY[mode]}.")
                return
            await ctx.send(f"⚽ {bot_a.name} vs {bot_b.name} — {line}")
            return

        # Overall
        if mode_group is None:
            h2h = self.bot._db.get_head_to_head(bot_a.id, bot_b.id, mode=None)
            line = _goals_line(h2h, "Overall")
            if not line:
                await ctx.send(f"{bot_a.name} and {bot_b.name} have never played each other.")
                return
            await ctx.send(f"⚽ {bot_a.name} vs {bot_b.name} — {line}")
            return

        # Per-mode breakdown
        modes_to_check = mode_group if isinstance(mode_group, list) else ALL_MODES
        parts_out = []
        for m in modes_to_check:
            h2h = self.bot._db.get_head_to_head(bot_a.id, bot_b.id, mode=m)
            line = _goals_line(h2h, MODE_DISPLAY[m])
            if line:
                parts_out.append(line)

        if parts_out:
            await ctx.send(f"⚽ {bot_a.name} vs {bot_b.name} — {' | '.join(parts_out)}")
        else:
            await ctx.send(f"{bot_a.name} and {bot_b.name} have never played each other.")

    @commands.command(name="predict")
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_predict(self, ctx: commands.Context, *, args: str = "") -> None:
        """!predict <team1> vs <team2> <mode> | !predict current — win probability.

        Teams can be comma-separated (e.g. for solo modes).  A mode is
        required for bot names; raw MMR values need none:
          !predict Nexto vs Necto 1v1
          !predict A, B vs C, D solo2v2
          !predict 1500 vs 1200
        """
        if not args or self._is_help(args):
            await ctx.send("Usage: !predict <team1> vs <team2> <mode> or !predict current")
            return

        # "current" keyword: show prediction for current live match
        if args.strip().lower() == "current":
            with self.bot._overlay._lock:
                m = self.bot._overlay.match

            if m.phase == "idle" or not m.team_blue:
                await ctx.send("No match in progress.")
                return

            probs = m.win_probabilities
            if not probs or len(probs) < 2:
                await ctx.send("Win probabilities not available for this match.")
                return

            blue = ", ".join(dict.fromkeys(b.name for b in m.team_blue))
            orange = ", ".join(dict.fromkeys(b.name for b in m.team_orange))
            p_blue = round(probs[0] * 100)
            p_orange = round(probs[1] * 100)
            await ctx.send(f"🔮 {blue} {p_blue}% — {p_orange}% {orange}")
            return

        # Strip optional trailing mode
        rest, mode = self._split_name_and_mode(args)

        # Split on " vs "
        vs = split_vs(rest)
        if vs is None:
            await ctx.send("Usage: !predict <team1> vs <team2> <mode>")
            return
        left_str, right_str = vs

        # Parse comma-separated entries per side
        left_entries = [n.strip() for n in left_str.split(",") if n.strip()]
        right_entries = [n.strip() for n in right_str.split(",") if n.strip()]

        if not left_entries or not right_entries:
            await ctx.send("Usage: !predict <team1> vs <team2> <mode>")
            return

        # Check if all entries are numeric (raw MMR mode)
        def _is_number(s: str) -> bool:
            try:
                float(s)
                return True
            except ValueError:
                return False

        if all(_is_number(e) for e in left_entries + right_entries):
            # Raw MMR prediction — convert MMR to mu, use min sigma
            from rlgymstream.matchmaking.ratings import make_rating, _model, MIN_SIGMA

            left_ratings = [make_rating((float(e) - 100) / 20, MIN_SIGMA) for e in left_entries]
            right_ratings = [make_rating((float(e) - 100) / 20, MIN_SIGMA) for e in right_entries]
            probs = _model.predict_win(teams=[left_ratings, right_ratings])

            left_label = ", ".join(left_entries)
            right_label = ", ".join(right_entries)
            p_left = round(probs[0] * 100)
            p_right = round(probs[1] * 100)
            await ctx.send(f"🔮 MMR {left_label} {p_left}% — {p_right}% {right_label}")
            return

        # Bot names — ratings are per mode, so the mode must be explicit
        if not mode:
            await ctx.send(f"Please add a mode, e.g. !predict A vs B 1v1 — modes: {MODES_HINT}")
            return

        left_bots = []
        for name in left_entries:
            bot, err = self._find_bot(name)
            if not bot:
                await ctx.send(err)
                return
            left_bots.append(bot)

        right_bots = []
        for name in right_entries:
            bot, err = self._find_bot(name)
            if not bot:
                await ctx.send(err)
                return
            right_bots.append(bot)

        # Different bots on one team contribute independently (solo-queue
        # style); identical teammates are deduplicated.
        is_solo = (
            mode.startswith("solo_")
            or len(set(b.id for b in left_bots)) > 1
            or len(set(b.id for b in right_bots)) > 1
        )

        from rlgymstream.matchmaking.ratings import predict_win_probability
        left_ids = [b.id for b in left_bots]
        right_ids = [b.id for b in right_bots]
        probs = predict_win_probability(
            self.bot._db, mode,
            left_ids, right_ids,
            is_solo_queue=is_solo,
        )

        left_label = ", ".join(b.name for b in left_bots)
        right_label = ", ".join(b.name for b in right_bots)
        p_left = round(probs[0] * 100)
        p_right = round(probs[1] * 100)
        mode_label = MODE_DISPLAY.get(mode, mode)
        await ctx.send(f"🔮 [{mode_label}] {left_label} {p_left}% — {p_right}% {right_label}")

    @commands.command(name="map")
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_map(self, ctx: commands.Context) -> None:
        """!map — current map name."""
        with self.bot._overlay._lock:
            m = self.bot._overlay.match

        if m.phase == "idle" or not m.map_name:
            await ctx.send("No match in progress right now.")
            return

        await ctx.send(f"🗺️ {m.map_name}")

    @commands.command(name="modes")
    @commands.cooldown(rate=1, per=10, key=commands.BucketType.channel)
    async def cmd_modes(self, ctx: commands.Context) -> None:
        """!modes — active mode rotation."""
        mode_names = [m.display_name for m in self.bot._config.mode_rotation]
        await ctx.send(f"🔄 Mode rotation: {', '.join(mode_names)}")

    @commands.command(name="uptime")
    @commands.cooldown(rate=1, per=10, key=commands.BucketType.channel)
    async def cmd_uptime(self, ctx: commands.Context) -> None:
        """!uptime — how long the bot has been running."""
        elapsed = time.monotonic() - self.bot._start_time
        hours, remainder = divmod(int(elapsed), 3600)
        minutes, seconds = divmod(remainder, 60)

        total_matches = self.bot._db.get_match_count()
        with self.bot._overlay._lock:
            session_matches = self.bot._overlay.session_matches

        if hours > 0:
            time_str = f"{hours}h {minutes}m"
        elif minutes > 0:
            time_str = f"{minutes}m {seconds}s"
        else:
            time_str = f"{seconds}s"

        await ctx.send(f"⏱️ Running for {time_str} — {session_matches} matches this session, {total_matches} all time")

    # ── Bot Stats commands ─────────────────────────────────────

    # Shared stat alias mapping
    _STAT_ALIASES = {
        "goals": "goals", "goal": "goals",
        "assists": "assists", "assist": "assists",
        "saves": "saves", "save": "saves",
        "shots": "shots", "shot": "shots",
        "demos": "demos", "demo": "demos",
        "touches": "touches", "touch": "touches",
        "cartouches": "car_touches", "car_touches": "car_touches",
        "score": "score", "points": "score",
        "boost": "avg_boost", "avgboost": "avg_boost", "avg_boost": "avg_boost",
        "speed": "avg_speed", "avgspeed": "avg_speed", "avg_speed": "avg_speed",
        "supersonic": "pct_supersonic", "sonic": "pct_supersonic", "ss": "pct_supersonic",
        "ground": "pct_ground", "gnd": "pct_ground",
        "wall": "pct_wall",
        "air": "pct_air",
        "demolished": "pct_demolished",
        "bpm": "bpm",
        "boostconsumed": "boost_consumed", "boost_consumed": "boost_consumed",
    }

    def _stat_units(self, col: str) -> str:
        if col == "avg_speed":
            return " kph"
        elif col.startswith("pct_"):
            return "%"
        return ""

    @commands.command(name="stats")
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_stats(self, ctx: commands.Context, *, args: str = "") -> None:
        """!stats [top|bottom|bot] <stat|bot> <mode>

        !stats top <stat> <mode>    — top 5 bots by a stat
        !stats bottom <stat> <mode> — bottom 5 bots by a stat
        !stats bot <name> <mode>    — average stats for a specific bot
        !stats <stat> <mode>        — shortcut for !stats top <stat> <mode>

        Averages are per player slot (a bot filling 3 slots in a 3v3
        contributes 3 samples per match).
        """
        if not args or self._is_help(args):
            await ctx.send(
                "Usage: !stats top/bottom <stat> <mode> | !stats bot <name> <mode> — "
                "Stats: goals, assists, saves, shots, demos, boost, speed, supersonic, bpm, air, wall, ground"
            )
            return

        tokens = args.strip().split()
        subcommand = tokens[0].lower()

        if subcommand == "bot":
            await self._stats_bot(ctx, " ".join(tokens[1:]))
        elif subcommand == "top":
            await self._stats_leaders(ctx, " ".join(tokens[1:]), ascending=False)
        elif subcommand == "bottom":
            await self._stats_leaders(ctx, " ".join(tokens[1:]), ascending=True)
        else:
            # No subcommand — treat as "!stats top <stat> [mode]"
            await self._stats_leaders(ctx, args, ascending=False)

    async def _stats_leaders(self, ctx: commands.Context, args: str, ascending: bool) -> None:
        """Handle !stats top/bottom <stat> <mode>."""
        if not args:
            await ctx.send("Please specify a stat and mode, e.g. !stats top goals 1v1")
            return

        stat_part, mode = self._split_name_and_mode(args)
        col = self._STAT_ALIASES.get(stat_part.lower())
        if not col:
            await ctx.send(f'Unknown stat "{stat_part}". Try: goals, demos, boost, speed, supersonic, bpm, saves, etc.')
            return
        if not mode:
            await ctx.send(f"Please add a mode, e.g. !stats top {stat_part} 1v1 — modes: {MODES_HINT}")
            return

        leaders = self.bot._db.get_stat_leaders(col, mode=mode, limit=5, min_matches=5, ascending=ascending)
        if not leaders:
            await ctx.send(f'No data yet for "{stat_part}" in {MODE_DISPLAY[mode]}.')
            return

        units = self._stat_units(col)
        label = "Bottom" if ascending else "Top"
        display_stat = stat_part.capitalize()
        mode_str = f" ({MODE_DISPLAY[mode]})"
        parts = [f"{i+1}. {r['bot_name']} ({r['value']}{units})" for i, r in enumerate(leaders)]
        await ctx.send(f"🏆 {label} {display_stat}{mode_str}: {' | '.join(parts)}")

    async def _stats_bot(self, ctx: commands.Context, args: str) -> None:
        """Handle !stats bot <name> <mode>."""
        if not args:
            await ctx.send("Usage: !stats bot <bot name> <mode>")
            return

        name, mode = self._split_name_and_mode(args)
        if not mode:
            await ctx.send(f"Please add a mode, e.g. !stats bot {name} 1v1 — modes: {MODES_HINT}")
            return
        bot, err = self._find_bot(name)
        if not bot:
            await ctx.send(err)
            return
        assert bot.id is not None

        stats = self.bot._db.get_bot_avg_stats(bot.id, mode=mode)
        if not stats:
            await ctx.send(f"No stats recorded for {bot.name} in {MODE_DISPLAY[mode]}.")
            return

        mode_str = f" ({MODE_DISPLAY[mode]})"
        n = stats["matches"]
        msg = (
            f"📊 {bot.name}{mode_str} avg over {n} matches: "
            f"{stats['avg_goals']}G {stats['avg_assists']}A {stats['avg_saves']}S "
            f"{stats['avg_demos']}D {stats['avg_shots']} shots | "
            f"Boost: {stats['avg_boost']} ({stats['bpm']} BPM) | "
            f"Speed: {stats['avg_speed']} kph | "
            f"SS: {stats['pct_supersonic']}% | "
            f"Ground/Wall/Air: {stats['pct_ground']}/{stats['pct_wall']}/{stats['pct_air']}%"
        )
        await ctx.send(msg)


# ── Bot class ────────────────────────────────────────────────────────

# Chat watchdog: twitchio can permanently close the EventSub websocket (e.g.
# a reconnect that times out, or a failed resubscribe) while the process keeps
# running, leaving the bot deaf.  If the chat subscription stays missing for
# this many consecutive checks, the bot shuts down so the parent process
# restarts it.
_WATCHDOG_INTERVAL = 30.0  # seconds
_WATCHDOG_MAX_FAILURES = 3


class RLGymStreamBot(commands.Bot):
    """Twitch chatbot for RLGymStream using twitchio v3."""

    def __init__(self, config: AppConfig, db: Database, overlay_state: OverlayState):
        self._db = db
        self._overlay = overlay_state
        self._config = config
        self._start_time = time.monotonic()
        self._watchdog_task: asyncio.Task | None = None
        # Set when the watchdog shuts the bot down because chat was lost.
        self.chat_lost = False

        super().__init__(
            client_id=config.twitch_client_id,
            client_secret=config.twitch_client_secret,
            bot_id=config.twitch_bot_id,
            owner_id=config.twitch_owner_id or config.twitch_bot_id,
            prefix="!",
        )

    async def setup_hook(self) -> None:
        """Called after login — load commands and subscribe to chat.

        Tokens are loaded from .tio.tokens.json by login() before this runs.
        """
        import json

        await self.add_component(ChatCommands(self))

        owner_id = self._config.twitch_owner_id or self.bot_id

        # Read stored tokens to find which user IDs we have tokens for
        try:
            with open(".tio.tokens.json", "rb") as fp:
                tokens = json.load(fp)
        except FileNotFoundError:
            tokens = {}
            logger.info(
                "No .tio.tokens.json found — authorize the bot at "
                "http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot&force_verify=true"
            )

        # Subscribe to chat for each stored user (except the bot itself)
        for user_id in tokens:
            if user_id == self.bot_id:
                continue
            sub = eventsub.ChatMessageSubscription(
                broadcaster_user_id=user_id,
                user_id=self.bot_id,
            )
            try:
                await self.subscribe_websocket(sub)
                logger.info("Subscribed to chat for user ID %s", user_id)
            except Exception as e:
                logger.warning("Failed to subscribe to chat for user %s: %s", user_id, e)

        # If bot == channel owner, we still need to subscribe to our own channel
        if owner_id == self.bot_id and owner_id not in tokens:
            # No token stored yet for this account
            pass
        elif owner_id == self.bot_id:
            sub = eventsub.ChatMessageSubscription(
                broadcaster_user_id=owner_id,
                user_id=self.bot_id,
            )
            try:
                await self.subscribe_websocket(sub)
                logger.info("Subscribed to own channel chat (bot == owner)")
            except Exception as e:
                logger.warning("Failed to subscribe to own chat: %s", e)

        self._watchdog_task = asyncio.create_task(self._chat_watchdog())
        logger.info("Twitch chatbot setup complete")

    def _has_chat_subscription(self) -> bool:
        return any(
            getattr(sub.type, "value", sub.type) == "channel.chat.message"
            for sub in self.websocket_subscriptions().values()
        )

    async def _chat_watchdog(self) -> None:
        """Shut down if the chat subscription disappears and doesn't come back."""
        seen_healthy = False
        failures = 0
        while True:
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            try:
                healthy = self._has_chat_subscription()
            except Exception:
                logger.warning("Chat watchdog check failed", exc_info=True)
                healthy = False

            if healthy:
                if failures:
                    logger.info("Chat subscription is back")
                seen_healthy = True
                failures = 0
                continue
            if not seen_healthy:
                continue  # never subscribed yet (e.g. waiting for OAuth)

            failures += 1
            logger.warning(
                "No active chat subscription (check %d/%d)",
                failures, _WATCHDOG_MAX_FAILURES,
            )
            if failures >= _WATCHDOG_MAX_FAILURES:
                logger.error("Chat subscription lost — shutting down so the chatbot is restarted")
                self.chat_lost = True
                await self.close()
                return

    async def close(self, **options) -> None:
        task = self._watchdog_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        await super().close(**options)

    async def event_websocket_closed(self, payload) -> None:
        # Also fires for routine reconnects; the watchdog decides whether
        # chat is actually gone.
        logger.info("EventSub websocket closed: %s", payload.socket)

    async def event_ready(self) -> None:
        logger.info("Twitch chatbot logged in as: %s", self.user)

    async def event_oauth_authorized(self, payload: authentication.UserTokenPayload) -> None:
        """Called when a user authorizes via the built-in OAuth adapter."""
        await self.add_token(payload.access_token, payload.refresh_token)
        # Save tokens to file immediately so they persist across restarts
        await self.save_tokens()
        logger.info("OAuth token saved for user %s", payload.user_id)

        # Subscribe to chat for this user's channel (even if it's the bot account,
        # since the bot may be the same account as the channel owner)
        sub = eventsub.ChatMessageSubscription(
            broadcaster_user_id=payload.user_id,
            user_id=self.bot_id,
        )
        try:
            await self.subscribe_websocket(sub)
            logger.info("Subscribed to chat for user %s", payload.user_id)
        except Exception as e:
            logger.warning("Failed to subscribe to chat for user %s: %s", payload.user_id, e)

    async def event_command_error(self, payload: commands.CommandErrorPayload) -> None:
        if isinstance(payload.exception, commands.CommandNotFound):
            return
        logger.error("Chat command error: %s", payload.exception)


# Keep backward-compatible alias
TwitchChatBot = RLGymStreamBot

