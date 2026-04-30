# Rocket League Stats API

This document outlines capabilities of the Rocket League Game Data API. First, players must ask the game to enable this feature by editing their `DefaultStatsAPI.ini`, explained below. Once active, this feature will open a web socket on the player's machine that emits gameplay data and events. Third party programs can ingest this data to power a variety of applications, such as custom broadcaster HUDs.

---

## Overview

The Stats API broadcasts JSON messages over a local socket while a match is in progress. Messages are sent both at a configurable periodic rate and when specific match events occur. Event data is always emitted on the same tick that the event occurs, regardless of the user's `PacketSendRate`.

> **Note:** All configuration must be done before the client starts — changes to the ini while the client is running require a restart.

**Field visibility:**
* Fields marked **CONDITIONAL** are only present when relevant.
* Fields marked **SPECTATOR** are only present if the client is spectating or on the player's team.

---

## Configuration

Edit `<Install Dir>\TAGame\Config\DefaultStatsAPI.ini` before launching the client.

| Setting | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| **PacketSendRate** | float | 0 (disabled) | Number of UpdateState packets broadcast per second. Must be >0 to enable the websocket. Capped at 120. |
| **Port** | int | 49123 | Local port the socket listens on. |

---

## Message Format

Every message follows this envelope structure:

```json
{
  "Event": "EventName",
  "Data":  { /* event-specific payload */ }
}
```

---

## Tick

### `UpdateState`
Sent X amount of times per second based on the player's `PacketSendRate` preference.

**Example Payload:**
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
        "bSupersonic": true
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

**Fields:**
| Field | Type | Description |
| :--- | :--- | :--- |
| **Players** | array | One entry per player in the match. |
| Name | string | Display name. |
| PrimaryId | string | Platform identifier in the format `Platform|Uid|Splitscreen` (e.g. "Steam\|123\|0"). |
| Shortcut | int | Spectator shortcut number. |
| TeamNum | int | Team index (0 = Blue, 1 = Orange). |
| Score | int | Total match score. |
| Goals | int | Goals scored this match. |
| Shots | int | Shot attempts this match. |
| Assists | int | Assists earned this match. |
| Saves | int | Saves made this match. |
| Touches | int | Total ball touches. |
| CarTouches | int | Touches by the car body (not ball). |
| Demos | int | Demolitions inflicted. |
| bHasCar | bool | **SPECTATOR** True if the player currently has a vehicle. |
| Speed | float | **SPECTATOR** Vehicle speed in Unreal Units/second. |
| Boost | int | **SPECTATOR** Boost amount 0–100. |
| bBoosting | bool | **SPECTATOR** True if the player is currently boosting. |
| bOnGround | bool | **SPECTATOR** True if at least 3 wheels are touching the world. |
| bOnWall | bool | **SPECTATOR** True if the vehicle is on a wall. |
| bPowersliding | bool | **SPECTATOR** True if the player is holding handbrake. |
| bDemolished | bool | **SPECTATOR** True if the vehicle is currently destroyed. |
| bSupersonic | bool | **SPECTATOR** True if the vehicle is at supersonic speed. |
| Attacker | object | **CONDITIONAL** The player who demolished this player. Present only when demolished. |
| Game | object | Match metadata. |
| Teams | array | One entry per team, ordered by TeamNum. |
| TimeSeconds | int | Seconds remaining in the match. |
| bOvertime | bool | True if the match is in overtime. |
| Ball | object | Current ball state. |
| bReplay | bool | True if a goal replay or history replay is active. |
| bHasWinner | bool | True if a team has won. |
| Winner | string | Name of the winning team. Empty string if no winner yet. |
| Arena | string | Asset name of the current map (e.g. "Stadium_P"). |
| bHasTarget | bool | True if the client is currently viewing a specific vehicle. |
| Target | object | **CONDITIONAL** Player currently being viewed. |

---

## Events

### `BallHit`
Sent one frame after the ball is hit.
* **Fields**: `Players` (array of name, shortcut, teamnum), `Ball` (PreHitSpeed, PostHitSpeed, Location), `MatchGuid`.

### `ClockUpdatedSeconds`
Sent when the in-game clock has changed.
* **Fields**: `TimeSeconds`, `bOvertime`, `MatchGuid`.

### `CountdownBegin`
Sent at the start of each round when the countdown starts.
* **Fields**: `MatchGuid`.

### `CrossbarHit`
Sent when the ball hits a crossbar.
* **Fields**: `BallSpeed`, `ImpactForce`, `BallLastTouch` (Player object, Speed), `BallLocation`, `MatchGuid`.

### `GoalReplayEnd`
Sent when a goal replay ends.
* **Fields**: `MatchGuid`.

### `GoalReplayStart`
Sent when a goal replay starts.
* **Fields**: `MatchGuid`.

### `GoalReplayWillEnd`
Sent when the ball explodes during a goal replay. If the replay is skipped this event will not fire.
* **Fields**: `MatchGuid`.

### `GoalScored`
Sent when a goal is scored.
* **Fields**: `GoalSpeed`, `GoalTime`, `ImpactLocation`, `Scorer` (Player object), `Assister` (**CONDITIONAL** Player object), `BallLastTouch` (Player object, Speed), `MatchGuid`.

### `MatchCreated`
Sent when all teams are created and replicated.
* **Fields**: `MatchGuid`.

### `MatchDestroyed`
Sent when leaving the game.
* **Fields**: `MatchGuid`.

### `MatchEnded`
Sent when the match ends and a winner is chosen.
* **Fields**: `WinnerTeamNum`, `MatchGuid`.

### `MatchInitialized`
Sent when the first countdown starts.
* **Fields**: `MatchGuid`.

### `MatchPaused` / `MatchUnpaused`
Sent when the game is paused or unpaused by a match admin.
* **Fields**: `MatchGuid`.

### `PodiumStart`
Sent when the game enters the podium state after the match ends.
* **Fields**: `MatchGuid`.

### `ReplayCreated`
Sent when a replay is initialized (loaded via Match History, does not pertain to goal replays).
* **Fields**: `MatchGuid`.

### `RoundStarted`
Sent when the game enters the active state (after the countdown finishes).
* **Fields**: `MatchGuid`.

### `StatfeedEvent`
Sent when someone earns a stat.
* **Fields**: `EventName` (e.g., "Demolish", "Save"), `Type` (Localized label), `MainTarget` (Player object), `SecondaryTarget` (**CONDITIONAL** Player object), `MatchGuid`.