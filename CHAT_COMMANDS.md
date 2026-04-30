# Twitch Chat Commands

All commands use the `!` prefix. Most have a 5-second per-user cooldown.

Many commands accept an optional **mode** as the last argument.
Mode shortcuts: `1v1`, `1s`, `ones`, `2v2`, `2s`, `twos`, `3v3`, `3s`, `threes`, `solo2v2`, `solo2s`, `solo3v3`, `solo3s`.

---

## General

| Command | Description |
|---|---|
| `!help` | List all available commands |
| `!stats` | Total matches played, number of active bots, number of modes |
| `!modes` | Show the active mode rotation |
| `!uptime` | How long the bot has been running, session and all-time match counts |

## Bot Info

| Command | Description |
|---|---|
| `!mmr <bot>` | Show a bot's MMR across all modes |
| `!bot <name> [mode]` | Bot info — author, description, win/loss record (optionally for one mode) |
| `!winrate <bot> [mode]` | Win rate per mode, or for a specific mode |
| `!streak <bot> [mode]` | Current win/loss streak (optionally per mode) |
| `!pos <bot> [mode]` | Show a bot's leaderboard position (alias: `!position`) |

## Leaderboard

| Command | Description |
|---|---|
| `!lb [mode]` | Top 5 leaderboard (default: 1v1). Alias: `!leaderboard` |
| `!best [mode]` | Show the #1 bot per mode, or for a specific mode |

## Match Info

| Command | Description |
|---|---|
| `!match` | Current match info — teams, score, phase, map |
| `!last [N] [mode] [bot]` | Last 1–3 match results, optionally filtered by mode and/or bot name |
| `!map` | Current map name |

## Head-to-Head

| Command | Description |
|---|---|
| `!h2h <botA> vs <botB>` | Per-mode head-to-head breakdown (default) |
| `!h2h <botA> vs <botB> <mode>` | Head-to-head in a specific mode (e.g. `1v1`) |
| `!h2h <botA> vs <botB> overall` | Aggregated record across all modes |
| `!h2h <botA> vs <botB> standard` | Record in standard modes (1v1, 2v2, 3v3) |
| `!h2h <botA> vs <botB> solo` | Record in solo queue modes (Solo 2v2, Solo 3v3) |
| `!h2h current [mode]` | Head-to-head for the current match's bots |

## Goal Stats

| Command | Description |
|---|---|
| `!goals <botA> vs <botB>` | Per-mode goal totals, averages, and total match duration |
| `!goals <botA> vs <botB> <mode>` | Goals in a specific mode |
| `!goals <botA> vs <botB> overall` | Aggregated goals across all modes |
| `!goals <botA> vs <botB> standard` | Goals in standard modes |
| `!goals <botA> vs <botB> solo` | Goals in solo queue modes |

## Predictions

| Command | Description |
|---|---|
| `!predict` | Win probability for the current live match |
| `!predict current` | Same, explicitly |
| `!predict <A> vs <B> [mode]` | Predict a 1v1 matchup (defaults to 1v1 mode) |
| `!predict <A>, <B> vs <C>, <D>` | Predict a solo queue matchup (auto-detects mode from team size) |
| `!predict <A>, <B>, <C> vs <D>, <E>, <F> [mode]` | Predict with explicit teams and mode |
| `!predict <MMR> vs <MMR>` | Predict from raw MMR values (e.g. `!predict 1500 vs 1200`) |

## Live Stats (Stats API)

| Command | Description |
|---|---|
| `!boost` | Show live boost levels of all bots in the current match |
| `!speed` | Show live speeds of all bots (with ⚡ for supersonic) |
| `!live` | Live in-match stats summary — goals, assists, saves, demos per bot. Alias: `!livestats` |
| `!goalspeed` | Show the last goal's speed, scorer, and assister |

---

## Examples

```
!mmr Nexto
!winrate Necto 1v1
!streak Nexto 2s
!pos Ripple 3v3
!lb solo2v2
!h2h Nexto vs Necto
!h2h Nexto vs Necto 1v1
!h2h Nexto vs Necto overall
!h2h Nexto vs Necto standard
!goals Nexto vs Necto
!goals Nexto vs Necto 1v1
!last 3 3v3
!predict Nexto vs Necto
!predict Nexto, Necto vs Ripple, Ordis
!predict 1500 vs 1200
!predict A, B, C vs D, E, F solo3v3
!bot Nexto 2v2
!last 3 Nexto
!last 2 1v1 Necto
!boost
!speed
!live
!goalspeed
```

## Notes

- **Bot names** are matched case-insensitively with fuzzy "did you mean?" suggestions.
- **Mode** is always optional and appears as the last argument. Without it, commands show data across all modes (or default to 1v1 for predictions).
- **`!predict`** with no arguments shows the prediction for the current live match. With arguments, it computes a prediction from the bots' current ratings.
- **Cooldowns**: most commands have a 5-second per-user cooldown. `!stats`, `!modes`, and `!uptime` have a 10-second per-channel cooldown.
- **Help**: any command supports `help` as an argument to show usage (e.g. `!h2h help`, `!predict help`).

