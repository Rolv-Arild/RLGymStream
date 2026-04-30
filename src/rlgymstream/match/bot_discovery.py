"""Discover bots from TOML config files across multiple source directories.

Uses ``rlbot.config.load_player_config`` to validate each bot.toml so
broken configs are caught at discovery time rather than at match start.
"""

from __future__ import annotations

import fnmatch
import logging
import tomllib
from pathlib import Path

from rlbot.config import load_player_config

from rlgymstream.config import BotSource
from rlgymstream.db.database import Database
from rlgymstream.db.models import Bot

logger = logging.getLogger(__name__)

# Tags in [details].tags that we map to our mode system.
_TAG_TO_MODES = {
    "1v1": "1v1",
    "2v2": "2v2",
    "3v3": "3v3",
    "teamplay": "2v2,3v3",  # "teamplay" implies 2v2 and 3v3
}


def discover_bots(sources: list[BotSource], db: Database) -> list[Bot]:
    """Scan every *BotSource* for bot.toml files, validate them, and upsert
    into the DB.  Bots no longer present in any source are disabled.
    Returns the full list of enabled bots after discovery.
    """
    found_names: set[str] = set()
    for source in sources:
        root = source.path.resolve()
        if not root.exists():
            logger.warning("Bot source directory does not exist: %s", root)
            continue

        toml_paths = _collect_toml_paths(root, source)
        for toml_path in toml_paths:
            try:
                bot = _register_bot(toml_path, db)
                found_names.add(bot.name)
            except Exception:
                logger.warning("Skipping invalid bot config: %s", toml_path, exc_info=True)

    # Disable bots that are no longer in any source
    all_bots = db.get_all_bots(enabled_only=False)
    for bot in all_bots:
        if bot.name not in found_names and bot.enabled:
            bot.enabled = False
            db.upsert_bot(bot)
            logger.info("Disabled bot no longer in config: %s", bot.name)

    logger.info("Discovered %d valid bot config(s)", len(found_names))
    return db.get_all_bots(enabled_only=True)


def _collect_toml_paths(root: Path, source: BotSource) -> list[Path]:
    """Resolve the list of bot config paths for a single BotSource."""
    if source.bots:
        # Explicit list — each entry is a relative path to a .toml file
        # (e.g. "necto/bot.toml" or "ripple/v1.bot.toml").
        paths = []
        for entry in source.bots:
            candidate = root / entry
            if candidate.is_file() and candidate.suffix == ".toml":
                paths.append(candidate)
            else:
                logger.warning("Bot config does not exist or is not a .toml file: %s", candidate)
        return paths
    else:
        # No explicit list — discover recursively, finding any file that
        # ends with "bot.toml" (matches both "bot.toml" and "v1.bot.toml").
        paths = []
        for toml_path in root.rglob("*bot.toml"):
            rel = toml_path.relative_to(root).as_posix()
            if source.exclude and any(
                fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel.split("/")[0], pat)
                for pat in source.exclude
            ):
                logger.debug("Excluded: %s", toml_path)
                continue
            paths.append(toml_path)
        return paths


def _register_bot(toml_path: Path, db: Database) -> Bot:
    """Validate a single bot.toml with RLBot's parser, extract metadata
    including mode tags, then upsert into the DB.  Returns the Bot."""
    abs_path = toml_path.resolve()

    # ── Validate with RLBot ──────────────────────────────────────────
    try:
        load_player_config(abs_path, team=0)
    except Exception as exc:
        raise ValueError(f"RLBot rejected {abs_path}: {exc}") from exc

    # ── Extract metadata ─────────────────────────────────────────────
    with open(abs_path, "rb") as f:
        data = tomllib.load(f)

    settings = data.get("settings", {})
    details = data.get("details", {})
    name = settings.get("name", abs_path.parent.name)
    author = details.get("developer", settings.get("developer", ""))
    description = details.get("description", settings.get("description", ""))
    fun_fact = details.get("fun_fact", "")
    language = details.get("language", "")

    # Logo
    logo_file = details.get("logo_file", settings.get("logo_file", "logo.png"))
    logo_path: str | None = None
    if logo_file:
        lp = abs_path.parent / logo_file
        if lp.exists():
            logo_path = str(lp)

    # Mode tags — read [details].tags and map to our mode system
    raw_tags: list[str] = details.get("tags", [])
    mode_set: set[str] = set()
    for tag in raw_tags:
        mapped = _TAG_TO_MODES.get(tag.lower().strip())
        if mapped:
            for m in mapped.split(","):
                mode_set.add(m)
    supported_modes = ",".join(sorted(mode_set))  # e.g. "1v1,2v2,3v3"

    bot = Bot(
        name=name,
        author=author,
        config_path=str(abs_path),
        logo_path=logo_path,
        description=description,
        fun_fact=fun_fact,
        language=language,
        supported_modes=supported_modes,
        enabled=True,
    )
    db.upsert_bot(bot)
    mode_info = supported_modes or "all modes"
    logger.debug("Registered bot: %s (by %s) [%s] @ %s", name, author, mode_info, abs_path)
    return bot
