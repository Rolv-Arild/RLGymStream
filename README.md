# RLGymStream

A Twitch stream platform that automatically runs Rocket League bot competitions
using **RLBot v5**, tracks match results with **OpenSkill** (Plackett-Luce)
ratings, and serves real-time OBS-compatible overlays.

🔴 **Live at [twitch.tv/rlgym](https://www.twitch.tv/rlgym)**

## Features

- **Automated match loop** — matchmake → launch → collect results → update ratings → repeat
- **5 competition modes** — 1v1, 2v2, 3v3, Solo Queue 2v2, Solo Queue 3v3
  - Standard modes use the same bot duplicated to fill each team
  - Solo Queue allows different bots on the same team (duplicates permitted)
- **Smart matchmaking** — favours competitive matchups based on current ratings
- **OpenSkill ratings** — separate Plackett-Luce rating (μ/σ) per bot per mode
- **Head-to-head tracking** — win/loss/draw records between any two bots, per mode
- **OBS overlay** — a single 1920×1080 browser source with pre-match showcases, live badges, and all-modes leaderboard
- **Multi-source bot discovery** — point at multiple directories/repos with include/exclude filters
- **RLBot validation** — every `bot.toml` is validated with `rlbot.config.load_player_config` at discovery
- **Mercy rule** — matches end early if the goal difference reaches a configurable threshold (default 8)
- **Sigma priority** — configurable chance to prioritize matchups involving the most uncertain bot, helping new bots calibrate faster
- **SQLite persistence** — all results, ratings, and match counter survive restarts

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

Copy the example config and edit it:

```bash
cp rlgymstream.example.toml rlgymstream.toml
```

All settings live in **`rlgymstream.toml`** in the project root:

```toml
overlay_port = 8080
post_match_delay = 15       # seconds to show in-game scoreboard after match
pre_match_delay = 15        # seconds to show pre-match screen
leaderboard_delay = 15      # seconds to show leaderboard between matches
sigma_priority_chance = 0.1 # chance to require the most uncertain bot in a match (0.0–1.0)
mercy_goal_diff = 8         # end match early if goal difference reaches this
mode_rotation = ["1v1", "2v2", "3v3", "solo_2v2", "solo_3v3"]

# List specific bot config files by path:
[[bot_sources]]
path = "/path/to/your/bots"
bots = [
    "necto/bot.toml",
    "nexto/bot.toml",
    "ripple/v1.bot.toml",           # prefixed config names are supported
]

# Or discover all *bot.toml files recursively, excluding some folders:
[[bot_sources]]
path = "/path/to/community-bots"
exclude = ["broken_bot", "deprecated"]
```

### Bot sources

Each `[[bot_sources]]` entry points at a directory (typically a cloned repo).

- **`bots`** (recommended) — list relative paths to bot config `.toml` files.
  Files can be named `bot.toml` or use a prefix like `v1.bot.toml`.
- If **`bots` is omitted**, every file matching `*bot.toml` under `path` is
  discovered recursively.
  Use **`exclude`** to skip folder names (matched with `fnmatch` against the relative path).

Every discovered config is validated with `rlbot.config.load_player_config`.
Invalid configs are skipped with a warning.

### Mode tags

Bots declare which modes they support via `[details].tags` in their `bot.toml`:

```toml
[details]
tags = ["1v1", "teamplay"]   # teamplay → 2v2 + 3v3
```

Recognised tags: `1v1`, `2v2`, `3v3`, `teamplay` (implies 2v2 + 3v3).
Bots with no tags are assumed to support all modes.
Bots only appear on leaderboards for modes they support.

### Environment variable overrides

| Variable | Description |
|---|---|
| `RLGYMSTREAM_DB_PATH` | Override SQLite database path |
| `RLGYMSTREAM_OVERLAY_PORT` | Override overlay server port |
| `RLGYMSTREAM_TWITCH_CHANNEL` | Twitch channel to join |
| `RLGYMSTREAM_TWITCH_CLIENT_ID` | Twitch application client ID |
| `RLGYMSTREAM_TWITCH_CLIENT_SECRET` | Twitch application client secret |
| `RLGYMSTREAM_TWITCH_BOT_ID` | *(optional)* Twitch user ID of bot account — defaults to channel |
| `RLGYMSTREAM_TWITCH_OWNER_ID` | *(optional)* Twitch user ID of channel owner — defaults to channel |

### Twitch Chatbot

Add a `[twitch]` section to enable the chatbot:

```toml
[twitch]
channel = "rlgym"
client_id = ""       # from Twitch Developer Console
client_secret = ""   # from Twitch Developer Console
# bot_id = ""        # optional — defaults to channel's user ID
# owner_id = ""      # optional — defaults to channel's user ID
```

**Setup steps:**
1. Register an application at https://dev.twitch.tv/console — note the **Client ID** and **Client Secret**. Set the OAuth redirect URL to `http://localhost:4343/oauth/callback`.
2. Add `channel`, `client_id`, and `client_secret` to your config (or set as env vars). If the bot account is the same as the channel, you're done with config.
3. Start the app, then authorize **once** in your browser:
   - **Bot account**: `http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot&force_verify=true`
   - **Channel owner**: `http://localhost:4343/oauth?scopes=channel:bot&force_verify=true`
   - If the bot IS the channel account, just do both URLs while logged in as that account.
4. Tokens are saved to `.tio.tokens.json` and refreshed automatically — you only need to authorize once.

The chatbot is disabled when `channel`, `client_id`, or `client_secret` are missing.

When enabled, the chatbot runs as a **separate subprocess** (automatically launched
and monitored by the main process). If it crashes, it restarts after 5 seconds.
You can also run it standalone for debugging:

```bash
python -m rlgymstream.chat
```

This reads the same `rlgymstream.toml` and database, but gets live match state
by polling the overlay HTTP API. The overlay server must be running (i.e. the
main process must be up).

**Available commands** — see [CHAT_COMMANDS.md](CHAT_COMMANDS.md) for full details:

| Command | Description |
|---|---|
| `!help` | List available commands |
| `!mmr <bot>` | Show a bot's MMR across all modes |
| `!lb [mode]` | Top 5 leaderboard (default: 1v1). Modes: `1v1`, `2s`, `3v3`, `solo2v2`, etc. |
| `!match` | Current match info (teams, score, phase) |
| `!h2h <botA> vs <botB>` | Head-to-head win/loss record |
| `!bot <name>` | Bot info — author, description, overall record |
| `!stats` | Total matches played, number of active bots |
| `!winrate <bot>` | Win rate per mode |

Bot names are matched case-insensitively with fuzzy "did you mean?" suggestions.

## Running

```bash
# Start the orchestrator + overlay server
rlgymstream

# Or run directly
python -m rlgymstream.main
```

This will:
1. Read `rlgymstream.toml` for bot sources and settings
2. Scan all sources, validate each config with RLBot, register valid bots
3. Start the overlay web server on `http://127.0.0.1:8080`
4. Begin the automatic match loop (cycling randomly through configured modes)

## OBS Overlay Setup

Add a single **Browser Source** in OBS:

| URL | Size |
|---|---|
| `http://127.0.0.1:8080/` | **1920×1080** |

The overlay is phase-aware and avoids in-game UI
(bottom-left scoreboard, bottom-right boost, top-centre timer):

| Phase | What's shown |
|---|---|
| **Pre-match** | Full-screen showcase — bot names, authors, descriptions, fun facts, MMR, win/loss record, head-to-head (standard modes), and estimated win probabilities |
| **Live** | Minimal badges in top-left (blue) and top-right (orange) showing bot names + MMR |
| **Post-match** | Badges remain visible with MMR gain/loss deltas (game shows its own scoreboard) |
| **Idle** | Full-screen all-modes leaderboard (one column per mode) |

Transitions between phases use smooth crossfades.

### MMR display

MMR is displayed as **20 × μ + 100** (the exact formula Rocket League uses) with a
disclaimer that it does not reflect actual in-game rank. New bots start at
600 MMR (with default μ=25).

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | The overlay page |
| `GET /api/state` | Full JSON snapshot (match, leaderboards, recent results, H2H) |
| `GET /api/events` | SSE stream — pushes a `state` event on every update |

## Architecture

```
rlgymstream.example.toml          # example config (copy to rlgymstream.toml)
src/rlgymstream/
├── config.py                     # MatchMode enum, BotSource, AppConfig (TOML loader)
├── main.py                       # Orchestration loop entry point
├── db/
│   ├── models.py                 # Bot, Rating, Match dataclasses
│   └── database.py               # SQLite layer with upsert/query helpers
├── match/
│   ├── bot_discovery.py          # Multi-source scanning + RLBot validation + tag parsing
│   └── launcher.py               # Reusable MatchManager, packet polling, mercy rule
├── matchmaking/
│   ├── matchmaker.py             # Accept/reject matchmaking, sigma priority, map rotation
│   └── ratings.py                # OpenSkill PlackettLuce wrapper, cross-team duplicate handling
├── chat/
│   ├── __main__.py               # Standalone chatbot entry point (python -m rlgymstream.chat)
│   ├── chatbot.py                # TwitchIO chatbot with viewer commands
│   └── overlay_proxy.py          # HTTP-polling mirror of OverlayState for subprocess use
└── overlay/
    ├── server.py                 # FastAPI app with SSE + JSON API
    ├── state.py                  # Thread-safe shared state
    ├── static/styles.css         # RLGym purple theme, 1920×1080 overlay styles
    ├── static/overlay.js         # SSE client + helper utilities
    ├── static/rlgym.png          # RLGym logo
    └── templates/overlay.html    # Single page: pregame → live badges → leaderboard
```

## Rating System

Uses [OpenSkill](https://openskill.me/en/stable/) with the
**Plackett-Luce** model:

- Each bot starts at **μ=25, σ=8.33** per mode
- MMR = **20 × μ + 100** (the exact formula Rocket League uses, does not reflect in-game rank)
- Minimum σ floor of **2.5** (matches Rocket League)
- Supports team-based rating updates (2v2, 3v3)
- In standard modes, teams are deduplicated for rating (same bot × N → one update)
- Bots appearing on both teams in solo queue have their posteriors consolidated
  using **precision-based merging** (sum μ deltas, sum 1/σ² deltas from the prior)
- **Draws are skipped** — no rating change (draws shouldn't occur in normal
  Rocket League play and likely indicate an error)
- Win probability prediction via `model.predict_win()`
- **Anchored ratings** — bots can be fixed at a known μ/σ (e.g. from
  human calibration) via `[[anchored_ratings]]` in the config.  They still
  participate in matches (opponents are rated normally), but their own
  μ/σ never change.  Anchors can be global or per-mode.  They bypass
  the minimum σ floor.  Anchored bots are marked with 📌 in the overlay.

## Matchmaking

A random threshold is rolled once per match, then random matchups are
generated until one has `p × (1 − p) / 0.25 ≥ threshold` (where `p` is
the predicted win probability for one side).  This favours balanced
matchups while allowing occasional mismatches for variety.  Every 1000
failed attempts the threshold is squared to guarantee convergence.

With `sigma_priority_chance` probability, an additional constraint is added:
the matchup must also include the bot with the highest σ (most uncertain
rating) in the current mode.  This helps new or under-played bots get
calibrated faster without biasing which opponent they face.
