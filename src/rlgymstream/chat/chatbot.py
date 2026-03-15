"""Twitch chatbot — responds to viewer commands with bot stats and match info.

Uses twitchio v3 with EventSub websockets for chat.

Commands:
    !help              — list available commands
    !mmr <bot>         — show a bot's MMR across all modes
    !rank <bot> [mode] — show a bot's rank position
    !lb [mode]         — show top 5 leaderboard (default: 1v1)
    !top [mode]        — show the #1 bot per mode (or one mode)
    !match             — show current/last match info
    !h2h <botA> <botB> — head-to-head record
    !bot <name>        — bot info (author, description, win/loss)
    !stats             — total matches, number of bots, etc.
    !winrate <bot>     — win rate per mode
    !streak <bot>      — current win/loss streak
    !last [N]          — last 1-3 match results
    !predict           — win probability for current match
    !map               — current map name
    !modes             — active mode rotation
    !uptime            — how long the bot has been running
"""

from __future__ import annotations

import difflib
import logging
import time
from typing import TYPE_CHECKING

import twitchio  # noqa: F401 — needed at runtime for eventsub
from twitchio import eventsub
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

    # ── Commands ─────────────────────────────────────────────

    @commands.command(name="help")
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_help(self, ctx: commands.Context) -> None:
        await ctx.send(
            "📋 !mmr · !rank · !lb · !top · !match · !h2h · !bot "
            "· !stats · !winrate · !streak · !last · !predict · !map · !modes · !uptime"
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

    @commands.command(name="lb", aliases=["leaderboard"])
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_lb(self, ctx: commands.Context, mode_arg: str = "1v1") -> None:
        mode = self._resolve_mode(mode_arg)
        if mode is None:
            await ctx.send(f'Unknown mode "{mode_arg}". Try: 1v1, 2v2, 3v3, solo2v2, solo3v3')
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
        if " vs " in args.lower():
            parts = args.split(" vs " if " vs " in args else " VS ")
            name_a, name_b = parts[0].strip(), parts[1].strip()
        elif " " in args.strip():
            tokens = args.strip().rsplit(" ", 1)
            if len(tokens) == 2:
                name_a, name_b = tokens[0].strip(), tokens[1].strip()
            else:
                await ctx.send("Usage: !h2h <botA> vs <botB>")
                return
        else:
            await ctx.send("Usage: !h2h <botA> vs <botB>")
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

        h2h = self.bot._db.get_head_to_head(bot_a.id, bot_b.id)
        if h2h["total"] == 0:
            await ctx.send(f"{bot_a.name} and {bot_b.name} have never played each other.")
            return

        await ctx.send(
            f"⚔️ {bot_a.name} vs {bot_b.name}: "
            f"{h2h['wins_a']}W-{h2h['draws']}D-{h2h['wins_b']}L "
            f"({h2h['total']} games)"
        )

    @commands.command(name="bot")
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_bot(self, ctx: commands.Context, *, name: str = "") -> None:
        if not name:
            await ctx.send("Usage: !bot <bot name>")
            return
        bot, err = self._find_bot(name)
        if not bot:
            await ctx.send(err)
            return
        assert bot.id is not None

        author = bot.author or "Unknown"
        desc = bot.description[:120] + "…" if len(bot.description) > 120 else bot.description

        total_w, total_l = 0, 0
        for mode in ALL_MODES:
            w, l, _d = self.bot._db.get_bot_record(bot.id, mode)
            total_w += w
            total_l += l

        parts = [f"🤖 {bot.name} by {author}"]
        if desc:
            parts.append(desc)
        parts.append(f"Record: {total_w}W-{total_l}L")
        if bot.language:
            parts.append(f"Lang: {bot.language}")

        await ctx.send(" | ".join(parts))

    @commands.command(name="stats")
    @commands.cooldown(rate=1, per=10, key=commands.BucketType.channel)
    async def cmd_stats(self, ctx: commands.Context) -> None:
        total_matches = self.bot._db.get_match_count()
        all_bots = self.bot._db.get_all_bots(enabled_only=True)
        await ctx.send(
            f"📈 {total_matches} matches played with {len(all_bots)} active bots "
            f"across {len(self.bot._config.mode_rotation)} modes"
        )

    @commands.command(name="winrate", aliases=["wr"])
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_winrate(self, ctx: commands.Context, *, name: str = "") -> None:
        if not name:
            await ctx.send("Usage: !winrate <bot name>")
            return
        bot, err = self._find_bot(name)
        if not bot:
            await ctx.send(err)
            return
        assert bot.id is not None

        parts = []
        for mode in ALL_MODES:
            w, l, d = self.bot._db.get_bot_record(bot.id, mode)
            total = w + l + d
            if total == 0:
                continue
            pct = round(100 * w / total)
            parts.append(f"{MODE_DISPLAY[mode]}: {pct}% ({w}-{l})")

        if parts:
            await ctx.send(f"📊 {bot.name} win rates — {' | '.join(parts)}")
        else:
            await ctx.send(f"{bot.name} has no games played yet.")

    @commands.command(name="rank")
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_rank(self, ctx: commands.Context, *, args: str = "") -> None:
        """!rank <bot> [mode] — show a bot's rank position."""
        if not args:
            await ctx.send("Usage: !rank <bot name> [mode]")
            return

        tokens = args.rsplit(" ", 1)
        mode = None
        if len(tokens) == 2:
            maybe_mode = self._resolve_mode(tokens[1])
            if maybe_mode:
                mode = maybe_mode
                args = tokens[0]

        bot, err = self._find_bot(args)
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

    @commands.command(name="top")
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_top(self, ctx: commands.Context, mode_arg: str = "") -> None:
        """!top [mode] — show the #1 bot per mode, or for a specific mode."""
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
    async def cmd_streak(self, ctx: commands.Context, *, name: str = "") -> None:
        """!streak <bot> — current win/loss streak."""
        if not name:
            await ctx.send("Usage: !streak <bot name>")
            return
        bot, err = self._find_bot(name)
        if not bot:
            await ctx.send(err)
            return
        assert bot.id is not None

        matches = self.bot._db.get_recent_matches(limit=50)
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

        if streak_count == 0:
            await ctx.send(f"{bot.name} has no recent matches.")
        else:
            emoji = "🔥" if streak_type == "W" else "❄️"
            await ctx.send(f"{emoji} {bot.name}: {streak_count} game {'win' if streak_type == 'W' else 'loss'} streak")

    @commands.command(name="last")
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_last(self, ctx: commands.Context, count: str = "1") -> None:
        """!last [N] — show the last 1-3 match results."""
        try:
            n = max(1, min(3, int(count)))
        except ValueError:
            n = 1

        matches = self.bot._db.get_recent_matches(limit=n)
        if not matches:
            await ctx.send("No matches played yet.")
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

    @commands.command(name="predict")
    @commands.cooldown(rate=1, per=5, key=commands.BucketType.chatter)
    async def cmd_predict(self, ctx: commands.Context) -> None:
        """!predict — win probability for current match."""
        with self.bot._overlay._lock:
            m = self.bot._overlay.match

        if m.phase == "idle" or not m.team_blue:
            await ctx.send("No match in progress right now.")
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
            session_matches = self.bot._overlay.total_matches

        if hours > 0:
            time_str = f"{hours}h {minutes}m"
        elif minutes > 0:
            time_str = f"{minutes}m {seconds}s"
        else:
            time_str = f"{seconds}s"

        await ctx.send(f"⏱️ Running for {time_str} — {session_matches} matches this session, {total_matches} all time")


# ── Bot class ────────────────────────────────────────────────────────


class RLGymStreamBot(commands.Bot):
    """Twitch chatbot for RLGymStream using twitchio v3."""

    def __init__(self, config: AppConfig, db: Database, overlay_state: OverlayState):
        self._db = db
        self._overlay = overlay_state
        self._config = config
        self._start_time = time.monotonic()
        self._broadcaster_id: str | None = None

        super().__init__(
            client_id=config.twitch_client_id,
            client_secret=config.twitch_client_secret,
            bot_id=config.twitch_bot_id,
            prefix="!",
        )

    async def setup_hook(self) -> None:
        """Called after login — subscribe to chat events and load commands."""
        # Resolve the broadcaster's user ID from their login name
        broadcaster = await self.fetch_user(login=self._config.twitch_channel)
        if broadcaster is None:
            logger.error("Could not find Twitch user: %s", self._config.twitch_channel)
            return

        self._broadcaster_id = str(broadcaster.id)

        # Subscribe to chat messages via EventSub websocket
        sub = eventsub.ChatMessageSubscription(
            broadcaster_user_id=self._broadcaster_id,
            user_id=self.bot_id,
        )
        await self.subscribe_websocket(payload=sub)

        # Load the commands component
        await self.add_component(ChatCommands(self))
        logger.info("Twitch chatbot ready in channel #%s", self._config.twitch_channel)

    async def event_command_error(self, payload: commands.CommandErrorPayload) -> None:
        if isinstance(payload.exception, commands.CommandNotFound):
            return
        logger.error("Chat command error: %s", payload.exception)


# Keep backward-compatible alias
TwitchChatBot = RLGymStreamBot

