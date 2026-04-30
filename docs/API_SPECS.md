# API SPECIFICATION

REST endpoints and WebSocket events exposed by the backend.

Status tags:

* `[current]` — implemented today.
* `[target]` — planned (see `TARGET_STATE.md`). Implement these without
  changing the behaviour of the current endpoints.

See also: `DATA_MODEL.md` (shapes), `GAME_FLOW.md` (phase gates),
`CURRENT_STATE.md` (exact AS-IS behaviour).

---

## 1. REST Endpoints

All request and response bodies are JSON. Errors return
`{"detail": "<message>"}` with `404` for "Game not found", `400` for
domain errors, and `422` for Pydantic validation failures.

Every successful response whose payload contains `game` sends the
**sanitized** projection (`backend/routes/websocket.py:game_wire`) —
`recovery_token` is always stripped, all other `Game` fields (including
`last_base_delta`, `last_bonus_delta`, `narrator_confirmed`, etc.) are
serialized, and the projection is augmented with `available_actions` and
`card_range` (the only two fields that are not on the Pydantic `Game` model).

### 1.1 Discovery

| Method | Path         | Status  | Body / Params        | Returns                                                  |
|--------|--------------|---------|----------------------|----------------------------------------------------------|
| GET    | `/rulesets`  | current | —                    | `[{"name": "casual"}, {"name": "high_risk"}, {"name": "standard"}]` — automatically reflects all registered rulesets. |
| GET    | `/health`    | current | —                    | `{"status": "ok"}`                                       |
| GET    | `/`          | current | —                    | Serves `frontend/index.html`                             |
| GET    | `/app.js`    | current | —                    | Serves `frontend/app.js`                                 |
| GET    | `/join/{game_id}` | current | —               | Serves `frontend/index.html` (deep-link entry point). The frontend detects the `/join/{game_id}` path at startup and pre-fills the room code field with the extracted `game_id`. |

### 1.2 Game lifecycle

| Method | Path              | Status  | Body                                        | Returns                                   |
|--------|-------------------|---------|---------------------------------------------|-------------------------------------------|
| POST   | `/create_game`    | current | `{ruleset?: str, votes_per_player?: int}` (defaults `"standard"`, `1`; `votes_per_player` 1 or 2) | `{"game_id": str, "qr_code": str}` — `qr_code` is a base64 PNG data URI (`data:image/png;base64,...`) encoding the URL `https://{DIXIT_APP_DOMAIN}/join/{game_id}`. Fails `400` on unknown `ruleset`. `qr_code` is **never** included in WS broadcasts or any other REST response (stripped by `game_wire`). |
| POST   | `/join_game`      | current | `{game_id, nickname}`                       | `{player_id, game_id, recovery_token, game}` — the **only** response carrying `recovery_token` |
| POST   | `/start_game`     | current | `{game_id, player_id}` (host only)          | `{game}` — requires ≥ 3 players, transitions `LOBBY → SELECT_NARRATOR` |
| POST   | `/select_narrator`| current | `{game_id, player_id, narrator_id}` (host)  | `{game}` — **phase stays** `SELECT_NARRATOR`; sets `narrator_id`, `narrator_confirmed = false`. Room WS: `narrator_selected` |
| POST   | `/confirm_narrator` | current | `{game_id, player_id}` (narrator only)   | `{game}` — sets `narrator_confirmed = true`. Room WS: `narrator_confirmed` |
| POST   | `/next_phase`     | current | `{game_id, player_id}` (host only)          | `{game}` — generic advance; see §1.5 for side-effects |

### 1.3 Player actions

| Method | Path              | Status  | Body                                        | Returns / notes                           |
|--------|-------------------|---------|---------------------------------------------|-------------------------------------------|
| POST   | `/submit_card`    | current | `{game_id, player_id, card_number}`         | `{game}`. Only allowed in `TURN_SUBMISSION` phase with `submission_step == declaration`. On duplicate card number (detected when all players declared): declarations are reset and `game_error` / `duplicate_cards` is broadcast (HTTP 200 with the post-reset `game`). When all players declare unique cards, `submission_step` auto-advances to `voting`. |
| POST   | `/submit_vote`    | current | `{game_id, player_id, card_number}`         | `{game}`. Only allowed in `TURN_SUBMISSION` phase with `submission_step == voting`. Appends `card_number` to `Player.votes`. Rejected if narrator, wrong phase/step, already at cap, duplicate, own card, or not on table. |
| POST   | `/update_vote`    | current | `{game_id, player_id, card_numbers: list[int]}` | `{game}`. Only allowed in `TURN_SUBMISSION` phase with `submission_step == voting`. Replaces `Player.votes` with the given list. Rejected if wrong phase/step, narrator, `len(card_numbers) > Game.votes_per_player`, duplicates, own card, or any card not on the table. |

Validation (Pydantic):

* `game_id`, `player_id`, `narrator_id`: 1–32 chars, hex `[A-Fa-f0-9]`.
* `nickname`: 1–40 chars (whitespace stripped).
* `card_number`: integer in `[1, 84]`.
* `card_numbers`: 1–2 integers, each in `[1, 84]`, all distinct.
* All bodies use `extra="forbid"`.

### 1.4 Endpoint semantics: submit_vote

| Concern           | Behaviour                                                              |
|-------------------|------------------------------------------------------------------------|
| Field set         | Appends `card_number` to `Player.votes` (list).                        |
| Allowed votes     | Up to `Game.votes_per_player` (1 or 2). Error if already at cap.       |
| Re-submission     | Use `/update_vote` to replace the full vote list at any time.            |

### 1.5 `/next_phase` side-effects

`POST /next_phase` dispatches by the current phase:

| From → To                          | Side effects                                                                                         | WS event emitted               |
|------------------------------------|------------------------------------------------------------------------------------------------------|--------------------------------|
| `SELECT_NARRATOR → TURN_SUBMISSION` | Requires `narrator_id` set, `narrator_confirmed == true`, and the host. Sets `submission_step = declaration`. | `phase_changed`                |
| `TURN_SUBMISSION (voting) → REVEAL_VOTES` | Requires `submission_step == voting` and all active non-narrators to have voted at least 1 card. Clears `submission_step`. | `phase_changed`                |
| `REVEAL_NARRATOR → SCORING`        | `rules_engine.calculate_scores(game)` applies both base and bonus scoring; `score_base_applied` and `score_bonus_applied` flags set to true; `last_base_delta` and `last_bonus_delta` populated. | `scores_updated`               |
| `SCORING → LEADERBOARD`            | —                                                                                                    | `scores_updated`               |
| `NEXT_ROUND → SELECT_NARRATOR`     | Round reset: clears `card_played`, `votes`, `cards_on_table`, scoring flags, `last_base_delta` / `last_bonus_delta`, `narrator_id`, `narrator_confirmed`, `submission_step`. Scores are preserved. | `phase_changed`                |
| Any other transition               | —                                                                                                    | `phase_changed`                |

* `LOBBY → SELECT_NARRATOR` uses only `POST /start_game` (not `/next_phase`).
* `SELECT_NARRATOR → TURN_SUBMISSION` uses `POST /next_phase` **after** the
  host has called `POST /select_narrator` and the chosen narrator has
  called `POST /confirm_narrator`.
* **Declaration → Voting auto-advance**: The transition from
  `submission_step=declaration` to `submission_step=voting` within
  `TURN_SUBMISSION` phase is **automatic** when all players have declared
  unique cards. No host action is required.

---

## 2. WebSocket

Endpoint: `GET /ws/{game_id}` (WebSocket upgrade).

If the game does not exist, the server sends
`{"event": "error", "detail": "Game not found"}` and closes with code
`1008`.

### 2.1 Message envelope

Every server message is a JSON object with an `event` field. Messages
that carry game state also include a sanitized `game` field
(`game_wire` projection; see `DATA_MODEL.md` §3 for injected fields):

```json
{ "event": "phase_changed", "game": { ... } }
```

Errors use:

```json
{ "event": "error", "detail": "<message>" }
```

`game_error` uses an extended shape (see §2.3).

### 2.2 Client → Server

| Event           | Status  | Payload                                    | Effect                                                                                                |
|-----------------|---------|--------------------------------------------|-------------------------------------------------------------------------------------------------------|
| `join_room`     | current | `{}`                                       | Server replies with `game_state` to the socket and refreshes `player.last_seen`.                       |
| `submit_card`   | current | `{player_id, card_number}`                 | Same effect as `POST /submit_card` (including duplicate reset + `game_error`).                         |
| `submit_vote`   | current | `{player_id, card_number}`                 | Same effect as `POST /submit_vote`.                                                                    |
| `update_vote`   | current | `{player_id, card_numbers: list[int]}`    | Same effect as `POST /update_vote`.                                                                    |
| `confirm_narrator` | current | `{player_id}`                            | Same effect as `POST /confirm_narrator`.                                                               |
| `reconnect`     | current | `{player_id, recovery_token}`              | On success: per-socket `game_state` + room `player_reconnected`. On failure: `error` / `recovery_failed` + close `1008`. |
| `ping`          | current | `{player_id}`                              | Server replies `pong` (per-socket) and refreshes `player.last_seen`. Cadence: ~15 s from the client.    |

Notes:

* Any inbound event from a known player updates `last_seen` and flips
  `connected = true`.
* The WebSocket variant of `update_vote` mirrors the REST endpoint for
  parity with `submit_card` / `submit_vote`; the REST endpoints are the
  primary surface.

### 2.3 Server → Client

| Event                  | Status  | Payload                                                                   | When emitted                                          |
|------------------------|---------|---------------------------------------------------------------------------|-------------------------------------------------------|
| `game_state`           | current | `{event, game}`                                                           | Sent to one socket on connect, on `join_room`, and on successful `reconnect`. |
| `player_joined`        | current | `{event, game}`                                                           | Room-wide after `POST /join_game`.                    |
| `narrator_selected`    | current | `{event, game}`                                                           | Room-wide after `POST /select_narrator` (phase still `SELECT_NARRATOR`). |
| `narrator_confirmed`   | current | `{event, game}`                                                           | Room-wide after `POST /confirm_narrator`.            |
| `phase_changed`        | current | `{event, game}`                                                           | Room-wide after a non-scoring phase transition.       |
| `card_submitted`       | current | `{event, game}`                                                           | Room-wide after a card is submitted (REST or WS).     |
| `vote_submitted`       | current | `{event, game}`                                                           | Room-wide after a vote is submitted (`submit_vote`) or updated (`update_vote`). |
| `scores_updated`       | current | `{event, game}`                                                           | Room-wide after entering `SCORING` or `LEADERBOARD`. Scoring UIs use `game.phase` (`SCORING`) with `last_base_delta` / `last_bonus_delta` to show both base and bonus points in a unified view (and cumulative `players[].score` on `LEADERBOARD`). |
| `game_error`           | current | `{event: "game_error", error: <code>, message: <string>, game: <Game>}`    | Room-wide gameplay errors. Current codes: `duplicate_cards`. |
| `player_reconnected`   | current | `{event, game}`                                                           | Room-wide after a successful `reconnect`.             |
| `player_disconnected`  | current | `{event, game}`                                                           | Room-wide when the heartbeat task flips one or more players to `connected=false`. |
| `pong`                 | current | `{event: "pong"}`                                                         | Per-socket reply to `ping`. No `game`, no render.     |
| `error`                | current | `{event: "error", detail: <string>}`                                      | Per-socket. `detail: "recovery_failed"` is followed by close `1008`. |

### 2.4 `game_error` codes

| Code               | Status  | Meaning                                                                                   |
|--------------------|---------|-------------------------------------------------------------------------------------------|
| `duplicate_cards`  | current | Two or more players selected the same card during `TURN_SUBMISSION` declaration step. Declarations have been reset; `game` reflects the post-reset state (every `card_played` and `cards_on_table` cleared, phase still `TURN_SUBMISSION`, `submission_step` still `declaration`). |

Future `game_error` codes should follow the same shape.

---

## 3. Rules

* Every action validates the current phase; out-of-phase calls are
  rejected.
* **Host** may call `POST /next_phase`, `POST /start_game`, and
  `POST /select_narrator`. The **designated narrator** may call
  `POST /confirm_narrator` (or WebSocket `confirm_narrator`) in
  `SELECT_NARRATOR` — that action does not change phase; it is required
  before the host can advance `SELECT_NARRATOR → TURN_SUBMISSION` with
  `/next_phase`.
* `recovery_token` is never included in any broadcast or in any REST
  response apart from `/join_game`.
* The `game` field on every outbound message is the **sanitized**
  `game_wire` projection (with `available_actions` and `card_range`
  injected), never the raw model.
* The backend is the single source of truth. The client must not
  compute scores, derive next-phase eligibility, or assume rule
  constants — it reads them from the broadcast.
