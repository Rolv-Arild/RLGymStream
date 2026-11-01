# Rocket League Stats API

This document outlines capabilities of the Rocket League Game Data API. First, players must ask the game to enable this feature by editing their `TAStatsAPI.ini` (or `DefaultStatsAPI.ini` if it doesn't exist), explained below. Once active, this feature will open a TCP and/or WebSocket on the player's machine that emits gameplay data and events as well as ingests certain commands. Third-party programs can ingest this data to power a variety of applications, such as custom broadcaster HUDs.

---

## Overview

The Stats API broadcasts JSON messages over a local socket while a match is in progress. Messages are sent both at a configurable periodic rate and when specific match events occur. Event data is always emitted on the same tick that the event occurs, regardless of the user's `PacketSendRate`.

> **Note:** All configuration must be done before the client starts — changes to the ini while the client is running require a restart.

**Field visibility:**

* Fields marked **CONDITIONAL** are only present when relevant.
* Fields marked **SPECTATOR** are only sent while spectating.
* Fields marked **TEAM** are only sent when you're on the same team as the player they describe.
* Fields marked **REPLAY** are only relevant during replays.

---

## Configuration

Edit `<Install Dir>\TAGame\Config\TAStatsAPI.ini` (or `DefaultStatsAPI.ini` if it doesn't exist) under the `[TAGame.MatchStatsExporter_TA]` section before launching the client.

| Setting | Type | Default | Description |
| --- | --- | --- | --- |
| `PacketSendRate` | float | 0 (disabled) | Number of UpdateState packets broadcast per second. Capped at 120; 0 disables the feature. |
| `Port` | int | 49123 | Local port the client listens on for TCP connections. Set to 0 to disable. Must be different from `WebPort`. |
| `WebPort` | int | 49124 | Local port the client listens on to open a WebSocket. Set to 0 to disable. Must be different from `Port`. |

---

## Message Format

Every message follows this envelope structure:

```json
{
  "Event": "EventName",
  "Data": { /* event-specific payload */ }
}

```

---

## Tick

### `UpdateState`

Sent X amount of times per second based on the player's `PacketSendRate` preference.

**Example:**

```json
{
  "Event": "UpdateState",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6",
    "Players": [
      {
        "Name": "PlayerA",
        "PrimaryId": "Steam|123|0",
        "Shortcut": 1,
        "TeamNum": 0,
        "Score": 125,
        "Goals": 1,
        "Shots": 2,
        "Assists": 0,
        "Saves": 1,
        "Touches": 14,
        "CarTouches": 3,
        "Demos": 0,
        "Loadout": [
          "body_grain",
          "Skin_bartees",
          "Wheel_SoccerBall",
          "Boost_AlphaReward",
          "None",
          "None"
        ],
        "bHasCar": true,
        "Speed": 1200,
        "Boost": 45,
        "bBoosting": true,
        "bOnGround": true,
        "bOnWall": false,
        "bPowersliding": false,
        "bDemolished": true,
        "Attacker": {
          "Name": "PlayerB",
          "Shortcut": 2,
          "TeamNum": 1
        },
        "bSupersonic": true,
        "PickupClass": "SpecialPickup_GrapplingHook_TA"
      }
    ],
    "Game": {
      "Teams": [
        {
          "Name": "Blue",
          "TeamNum": 0,
          "Score": 1,
          "ColorPrimary": "0000FF",
          "ColorSecondary": "0000AA"
        }
      ],
      "PlaylistId": 11,
      "TimeSeconds": 180,
      "bOvertime": false,
      "Frame": 120,
      "Elapsed": 50.2,
      "Ball": {
        "Speed": 850.5,
        "TeamNum": 0
      },
      "bReplay": false,
      "bHasWinner": true,
      "Winner": "Blue",
      "Arena": "Stadium_P",
      "bHasTarget": true,
      "Target": {
        "Name": "PlayerA",
        "Shortcut": 1,
        "TeamNum": 0
      }
    }
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `Players` | array | One entry per player in the match. |
| `Name` | string | Display name. |
| `PrimaryId` | string | Platform identifier in the format `Platform |
| `Shortcut` | int | Spectator shortcut number. |
| `TeamNum` | int | Team index (0 = Blue, 1 = Orange). |
| `Score` | int | Total match score. |
| `Goals` | int | Goals scored this match. |
| `Shots` | int | Shot attempts this match. |
| `Assists` | int | Assists earned this match. |
| `Saves` | int | Saves made this match. |
| `Touches` | int | Total ball touches. |
| `CarTouches` | int | Touches by the car body (not ball). |
| `Demos` | int | Demolitions inflicted. |
| `Loadout` | array<string> | Asset names of the equipped loadout for the player's current team sorted by product slot. |
| `bHasCar` | bool | **SPECTATOR** True if the player currently has a vehicle. |
| `Speed` | float | **SPECTATOR** Vehicle speed in Unreal Units/second. |
| `Boost` | int | **SPECTATOR** Boost amount 0–100. |
| `bBoosting` | bool | **SPECTATOR** True if the player is currently boosting. |
| `bOnGround` | bool | **SPECTATOR** True if at least 3 wheels are touching the world. |
| `bOnWall` | bool | **SPECTATOR** True if the vehicle is on a wall. |
| `bPowersliding` | bool | **SPECTATOR** True if the player is holding handbrake. |
| `bDemolished` | bool | **SPECTATOR** True if the vehicle is currently destroyed. |
| `bSupersonic` | bool | **SPECTATOR** True if the vehicle is at supersonic speed. |
| `PickupClass` | string | **SPECTATOR** Class name of the player's currently available Rumble pickup. Omitted when not playing with rumble powerups. |
| `Attacker` | object | **CONDITIONAL** The player who demolished this player. Present only when demolished. |
| `Game` | object | Match metadata. |
| `Teams` | array | One entry per team, ordered by TeamNum. |
| `PlaylistId` | int | Playlist Id of the current match. |
| `TimeSeconds` | int | Seconds remaining in the match. |
| `bOvertime` | bool | True if the match is in overtime. |
| `Ball` | object | Current ball state. |
| `bReplay` | bool | True if a goal replay or history replay is active. |
| `bHasWinner` | bool | True if a team has won. |
| `Winner` | string | Name of the winning team. Empty string if no winner yet. |
| `Arena` | string | Asset name of the current map (e.g. `"Stadium_P"`). |
| `bHasTarget` | bool | True if the client is currently viewing a specific vehicle. |
| `Target` | object | **CONDITIONAL** Player currently being viewed. |
| `Frame` | int | **CONDITIONAL** Current frame number if a replay is active. |
| `Elapsed` | float | **CONDITIONAL** Seconds elapsed since game start if a replay is active. |

---

## Events

### `BallHit`

Sent one frame after the ball is hit.

**Example:**

```json
{
  "Event": "BallHit",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6",
    "Players": [
      {
        "Name": "PlayerA",
        "Shortcut": 1,
        "TeamNum": 0
      }
    ],
    "Ball": {
      "PreHitSpeed": 0,
      "PostHitSpeed": 1450.2,
      "Location": {
        "X": -512,
        "Y": 100,
        "Z": 200
      }
    }
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `Players` | array | Players that hit the ball that frame. |
| `Ball` | object | Ball state at the moment of the hit. |
| `PreHitSpeed` | float | Ball speed before the hit (Unreal Units/second). |
| `PostHitSpeed` | float | Ball speed after the hit (Unreal Units/second). |
| `Location` | vector | World position (X, Y, Z) of the ball at impact. |
| `MatchGuid` | string | Only set for online or LAN matches. |

### `BoostPickup` (**SPECTATOR**)

Sent when a vehicle collects a boost pad or pill.

**Example:**

```json
{
  "Event": "BoostPickup",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6",
    "Player": {
      "Name": "PlayerA",
      "Shortcut": 1,
      "TeamNum": 0
    },
    "Location": {
      "X": -3072,
      "Y": 0,
      "Z": 73
    },
    "BoostAmount": 100,
    "BoostType": "BoostType_Pill",
    "bReplay": false
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `MatchGuid` | string | Only set for online or LAN matches. |
| `Player` | object | The player who collected the boost. |
| `Location` | vector | World location of the pickup, as `{ X, Y, Z }`. |
| `BoostAmount` | float | Amount of boost granted by the pickup. |
| `BoostType` | string | Pickup class: `BoostType_Pad` (small pad), `BoostType_Pill` (full/big boost). |
| `bReplay` | bool | True if the pickup occurred during replay playback. |

### `ClockUpdatedSeconds`

Sent when the in-game clock has changed.

**Example:**

```json
{
  "Event": "ClockUpdatedSeconds",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6",
    "TimeSeconds": 180,
    "bOvertime": false
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `TimeSeconds` | int | Seconds remaining in the match. |
| `bOvertime` | bool | True if the game is in overtime. |
| `MatchGuid` | string | Only set for online or LAN matches. |

### `CountdownBegin`

Sent at the start of each round when the countdown starts.

**Example:**

```json
{
  "Event": "CountdownBegin",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `MatchGuid` | string | Only set for online or LAN matches. |

### `CrossbarHit`

Sent when the ball hits a crossbar.

**Example:**

```json
{
  "Event": "CrossbarHit",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6",
    "BallLocation": {
      "X": 120,
      "Y": -2944,
      "Z": 320
    },
    "BallSpeed": 870.3,
    "ImpactForce": 127.5,
    "BallLastTouch": {
      "Player": {
        "Name": "PlayerA",
        "Shortcut": 1,
        "TeamNum": 0
      },
      "Speed": 120
    }
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `BallSpeed` | float | Ball speed on impact. |
| `ImpactForce` | float | Impact force of the ball relative to the crossbar normal. |
| `BallLastTouch` | object | The last touch of the ball before the crossbar hit. |
| `BallLocation` | vector | World position (X, Y, Z) of the ball when the impact occurred. |
| `MatchGuid` | string | Only set for online or LAN matches. |

### `GoalReplayEnd`

Sent when a goal replay ends.

**Example:**

```json
{
  "Event": "GoalReplayEnd",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `MatchGuid` | string | Only set for online or LAN matches. |

### `GoalReplayStart`

Sent when a goal replay starts.

**Example:**

```json
{
  "Event": "GoalReplayStart",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `MatchGuid` | string | Only set for online or LAN matches. |

### `GoalReplayWillEnd`

Sent when the ball explodes during a goal replay. If the replay is skipped this event will not fire.

**Example:**

```json
{
  "Event": "GoalReplayWillEnd",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `MatchGuid` | string | Only set for online or LAN matches. |

### `GoalScored`

Sent when a goal is scored.

**Example:**

```json
{
  "Event": "GoalScored",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6",
    "GoalSpeed": 87.3,
    "GoalTime": 127.5,
    "ImpactLocation": {
      "X": 0,
      "Y": -2944,
      "Z": 320
    },
    "Scorer": {
      "Name": "PlayerA",
      "Shortcut": 1,
      "TeamNum": 0
    },
    "Assister": {
      "Name": "PlayerC",
      "Shortcut": 3,
      "TeamNum": 0
    },
    "BallLastTouch": {
      "Player": {
        "Name": "PlayerA",
        "Shortcut": 1,
        "TeamNum": 0
      },
      "Speed": 125
    }
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `GoalSpeed` | float | Speed of the ball (Unreal Units/second) when it crossed the goal line. |
| `GoalTime` | float | Length of the previous round in seconds. |
| `ImpactLocation` | vector | World position (X, Y, Z) of the ball when the goal was scored. |
| `Scorer` | object | The player who scored the goal. |
| `BallLastTouch` | object | The last touch of the ball before the goal. |
| `Assister` | object | **CONDITIONAL** Same shape as Scorer. Present only when an assist was recorded. |
| `MatchGuid` | string | Only set for online or LAN matches. |

### `MatchCreated`

Sent when all teams are created and replicated.

**Example:**

```json
{
  "Event": "MatchCreated",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `MatchGuid` | string | Only set for online or LAN matches. |

### `MatchDestroyed`

Sent when leaving the game.

**Example:**

```json
{
  "Event": "MatchDestroyed",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `MatchGuid` | string | Only set for online or LAN matches. |

### `MatchEnded`

Sent when the match ends and a winner is chosen.

**Example:**

```json
{
  "Event": "MatchEnded",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6",
    "WinnerTeamNum": 0
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `MatchGuid` | string | Only set for online or LAN matches. |
| `WinnerTeamNum` | int | Team index of the winning team. |

### `MatchInitialized`

Sent when the first countdown starts.

**Example:**

```json
{
  "Event": "MatchInitialized",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `MatchGuid` | string | Only set for online or LAN matches. |

### `MatchPaused`

Sent when the game is paused by a match admin.

**Example:**

```json
{
  "Event": "MatchPaused",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `MatchGuid` | string | Only set for online or LAN matches. |

### `MatchUnpaused`

Sent when the game is unpaused by a match admin.

**Example:**

```json
{
  "Event": "MatchUnpaused",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `MatchGuid` | string | Only set for online or LAN matches. |

### `PlayerJoined`

Sent when a player is added to the current match.

**Example:**

```json
{
  "Event": "PlayerJoined",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6",
    "PlayerName": "PlayerA",
    "PrimaryId": "Steam|123|0"
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `MatchGuid` | string | Only set for online or LAN matches. |
| `PlayerName` | string | Display name of the player who joined. |
| `PrimaryId` | string | Platform identifier in the format `Platform |

### `PlayerLeft`

Sent when a player is removed from the current match.

**Example:**

```json
{
  "Event": "PlayerLeft",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6",
    "PlayerName": "PlayerA",
    "PrimaryId": "Steam|123|0"
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `MatchGuid` | string | Only set for online or LAN matches. |
| `PlayerName` | string | Display name of the player who left. |
| `PrimaryId` | string | Platform identifier in the format `Platform |

### `PodiumStart`

Sent when the game enters the podium state after the match ends.

**Example:**

```json
{
  "Event": "PodiumStart",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `MatchGuid` | string | Only set for online or LAN matches. |

### `ReplayCreated`

Sent when a replay is initialized. Does not pertain to goal replays, only replays you load via the Match History menu.

**Example:**

```json
{
  "Event": "ReplayCreated",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6",
    "FileName": "Stadium_P_2026-06-05_18-42",
    "Date": "2026-06-05 18:42:13"
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `MatchGuid` | string | Only set for online or LAN matches. |
| `FileName` | string | File name of the replay. |
| `Date` | string | Timestamp the replay was created. |

### `RoundStarted`

Sent when the game enters the active state (after the countdown finishes).

**Example:**

```json
{
  "Event": "RoundStarted",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `MatchGuid` | string | Only set for online or LAN matches. |

### `StatfeedEvent`

Sent when someone earns a stat.

**Example:**

```json
{
  "Event": "StatfeedEvent",
  "Data": {
    "MatchGuid": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6",
    "EventName": "Demolish",
    "Type": "Demolition",
    "MainTarget": {
      "Name": "PlayerA",
      "Shortcut": 1,
      "TeamNum": 0
    },
    "SecondaryTarget": {
      "Name": "PlayerB",
      "Shortcut": 2,
      "TeamNum": 1
    }
  }
}

```

| Field | Type | Description |
| --- | --- | --- |
| `EventName` | string | Asset name of the StatEvent (e.g. `"Demolish"`, `"Save"`). |
| `Type` | string | Localized display label for the stat (e.g. `"Demolition"`). |
| `MainTarget` | object | Player who earned the stat. |
| `MatchGuid` | string | Only set for online or LAN matches. |
| `SecondaryTarget` | object | **CONDITIONAL** Player involved in the stat (e.g. the demolished player). Same shape as `MainTarget`. |

---

## Commands

Commands sent from the socket for the client to ingest. Each is a JSON object with a `Command` field naming the command and a `Data` object holding the variables below.

**Command Format:**

```json
{
  "Command": "CommandName",
  "Data": { /* command-specific variables */ }
}

```

### `ChangePOV` (**SPECTATOR**, **REPLAY**)

Changes the spectator/replay viewpoint. At least one of `Focus` or `Perspective` must be supplied; both may be combined.

| Variable | Type | Required | Description |
| --- | --- | --- | --- |
| `Focus` | string | One of | What to look at — the ball or a specific player. Limited values (see below). |
| `Perspective` | string | One of | Camera mode to switch to. Limited values (see below). |

**Focus — accepted values:**

| Value | Meaning |
| --- | --- |
| `"Ball"` | Focus the ball. |
| `"<shortcut>"` | A player's spectator shortcut number as a string (e.g. `"1"`, `"2"`). Must be all digits; any other value is rejected. |

**Perspective — accepted values:**

| Value | Mode | Description |
| --- | --- | --- |
| `"Fly"` | Fly | Freely move around the map. |
| `"SoftAttach"` | Soft Attach | Follow the position of a player or ball. |
| `"HardAttach"` | Hard Attach | Follow the position and rotation of a player. |
| `"PlayerView"` | Player View | View from a player's perspective. Applied automatically when Focus targets a player and no Perspective is given. |
| `"AutoCam"` | Auto Cam | Automatically spectates. |
| `"Camera_Director"` | Director | Automatically switches between player views. |

**Example:**

```json
{
  "Command": "ChangePOV",
  "Data": {
    "Focus": "1",
    "Perspective": "PlayerView"
  }
}

```

### `LoadReplay`

Loads and begins playback of a replay by file name or by file path.

| Variable | Type | Required | Description |
| --- | --- | --- | --- |
| `FileName` | string | One of | Name of the replay file to play. Takes precedence over `Path` when both are set. |
| `Path` | string | One of | File path of a replay to play. Used when `FileName` is empty. |

**Example:**

```json
{
  "Command": "LoadReplay",
  "Data": {
    "FileName": "Stadium_P_2026-06-05_18-42"
  }
}

```

### `SeekReplay` (**REPLAY**)

Jumps replay playback to a target frame or time.

| Variable | Type | Required | Description |
| --- | --- | --- | --- |
| `Frame` | int | One of | Target frame number. Takes precedence over `TimeSeconds` when set. |
| `TimeSeconds` | float | One of | Target time in seconds. |

**Example:**

```json
{
  "Command": "SeekReplay",
  "Data": {
    "TimeSeconds": 120.5
  }
}

```

### `SetGameSpeed` (**REPLAY**)

Sets replay playback speed.

| Variable | Type | Required | Description |
| --- | --- | --- | --- |
| `Speed` | float | Yes | Playback multiplier. 1.0 = normal, 0.5 = half speed, 2.0 = double. Must be ≥ 0. |

**Example:**

```json
{
  "Command": "SetGameSpeed",
  "Data": {
    "Speed": 0.5
  }
}

```

### `SetHUDVisibility`

Shows or hides the in-game HUD.

| Variable | Type | Required | Description |
| --- | --- | --- | --- |
| `bVisible` | bool | Yes | `true` shows the HUD; `false` hides it. |

**Example:**

```json
{
  "Command": "SetHUDVisibility",
  "Data": {
    "bVisible": false
  }
}

```

### `SetMatchPaused`

Pauses or unpauses the current match or replay.

| Variable | Type | Required | Description |
| --- | --- | --- | --- |
| `bPaused` | bool | Yes | `true` pauses; `false` resumes. |

**Example:**

```json
{
  "Command": "SetMatchPaused",
  "Data": {
    "bPaused": true
  }
}

```