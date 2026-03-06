# RLGymStream

A Twitch stream platform that automatically runs Rocket League bot competitions
using **RLBot v5**, tracks match results with **OpenSkill** (Plackett-Luce)
ratings, and serves real-time OBS-compatible overlays.

## Features

- **Automated match loop** — matchmake → launch → collect results → update ratings → repeat
- **5 competition modes** — 1v1, 2v2, 3v3, Solo Queue 2v2, Solo Queue 3v3
  - Standard modes use unique bots per team
  - Solo Queue allows the same bot to appear on both teams
- **OpenSkill ratings** — separate Plackett-Luce rating (μ/σ) per bot per mode
- **Head-to-head tracking** — win/loss/draw records between any two bots
- **OBS overlays** — live-updating web pages for match info, leaderboards, and H2H
- **Multi-source bot discovery** — point at multiple directories/repos with include/exclude glob filters
- **RLBot validation** — every `bot.toml` is validated with `rlbot.config.load_player_config` at discovery
- **Hot-reload** — add/remove bots between matches without restarting
- **SQLite persistence** — all results and ratings survive restarts

## Prerequisites

- **Python 3.12+**
- **Rocket League** (running via Epic/Steam)
- **RLBot v5** — the RLBotServer executable must be installed
  ([instructions](https://github.com/RLBot/RLBot/wiki))
- **OBS Studio** (for streaming) with Browser Source support

## Setup

```bash
# Clone the repo
git clone <repo-url> RLGymStream
cd RLGymStream

# Install with uv (recommended)
uv sync

# Or with pip
pip install -e .
```

## Configuration

All settings live in **`rlgymstream.toml`** in the project root:

```toml
overlay_port = 8080
post_match_delay = 15
pre_match_delay = 10
mode_rotation = ["1v1", "2v2", "3v3", "solo_2v2", "solo_3v3"]

# List specific bot config files by path:
[[bot_sources]]
path = "C:/repos/RLGymPack"
bots = [
    "necto/bot.toml",              # standard bot.toml
    "nexto/bot.toml",
    "Byte/bob_build/Byte/bot.toml", # nested path
    "ripple/v1.bot.toml",           # prefixed config name
]

# Or discover all *bot.toml files recursively, excluding some folders:
[[bot_sources]]
path = "C:/repos/community-bots"
exclude = ["broken_bot", "deprecated"]
```

### Bot sources

Each `[[bot_sources]]` entry points at a directory (typically a cloned repo).

- **`bots`** (recommended) — list relative paths to bot config `.toml` files.
  Files can be named `bot.toml` or use a prefix like `v1.bot.toml`.
- If **`bots` is omitted**, every file matching `*bot.toml` under `path` is
  discovered recursively. Use **`exclude`** to skip folder names.
  Use **`exclude`** to skip folder names (matched with `fnmatch` against the relative path).

Every discovered `bot.toml` is validated with `rlbot.config.load_player_config`.
Invalid configs are skipped with a warning.

### Mode tags

Bots declare which modes they support via `[details].tags` in their `bot.toml`:

```toml
[details]
tags = ["1v1", "teamplay"]   # teamplay → 2v2 + 3v3
```

Recognised tags: `1v1`, `2v2`, `3v3`, `teamplay` (implies 2v2 + 3v3).
Bots with no tags are assumed to support all modes.

### Environment variable overrides

| Variable | Description |
|---|---|
| `RLGYMSTREAM_DB_PATH` | Override SQLite database path |
| `RLGYMSTREAM_OVERLAY_PORT` | Override overlay server port |

## Running

```bash
# Start the orchestrator + overlay server
rlgymstream

# Or run directly
python -m rlgymstream.main
```

This will:
1. Read `rlgymstream.toml` for bot sources and settings
2. Scan all sources, validate each `bot.toml` with RLBot, register valid bots
3. Start the overlay web server on `http://127.0.0.1:8080`
4. Begin the automatic match loop (cycling through all configured modes)

## OBS Overlay Setup

Add a single **Browser Source** in OBS:

| URL | Size |
|---|---|
| `http://127.0.0.1:8080/` | **1920×1080** |

The overlay is phase-aware and stays out of the way of in-game UI
(avoids bottom-left scoreboard, bottom-right boost, top-center timer):

| Phase | What's shown |
|---|---|
| **Pre-match** | Centered showcase — bot name, author, language, MMR, description, fun fact, head-to-head record (1v1), and the current mode leaderboard |
| **Live** | Minimal badges in the top-left (blue) and top-right (orange) corners showing bot names + MMR |
| **Post-match** | Winner banner centred on screen, plus the corner badges |
| **Idle** | Nothing (fully transparent) |

MMR is displayed as **20 × rating + 100** (Rocket League-style) with a
disclaimer that it does not reflect actual in-game rank.

The overlay has a **transparent background** and updates in real-time via
Server-Sent Events (SSE).

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/state` | Full JSON snapshot (match, leaderboards, recent results, H2H) |
| `GET /api/events` | SSE stream — pushes a `state` event whenever anything changes |

## Architecture

```
rlgymstream.toml                  # bot sources, overlay port, mode rotation, delays
src/rlgymstream/
├── config.py                     # MatchMode enum, BotSource, AppConfig (TOML loader)
├── main.py                       # Orchestration loop entry point
├── db/
│   ├── models.py                 # Bot, Rating, Match dataclasses
│   └── database.py               # SQLite layer with upsert/query helpers
├── match/
│   ├── bot_discovery.py          # Multi-source scanning + RLBot validation + tag parsing
│   └── launcher.py               # Reusable MatchManager, load_player_config, packet polling
├── matchmaking/
│   ├── matchmaker.py             # Rating-proximity bot selection, mode filtering, map rotation
│   └── ratings.py                # OpenSkill PlackettLuce wrapper
└── overlay/
    ├── server.py                 # FastAPI app with SSE + JSON API
    ├── state.py                  # Thread-safe shared state
    ├── static/styles.css         # Phase-aware 1920×1080 overlay styles
    ├── static/overlay.js         # SSE client + helper utilities
    └── templates/overlay.html    # Single page: pregame showcase → live badges → postgame banner
```

## Rating System

Uses [OpenSkill](https://github.com/vivek-uka/openskill.py) with the
**Plackett-Luce** model:

- Each bot starts at **μ=25, σ=8.33** per mode
- Display rating = **μ − 3σ** (conservative estimate)
- Supports team-based rating updates (2v2, 3v3)
- Win probability prediction via `model.predict_win()`

## License

MIT
