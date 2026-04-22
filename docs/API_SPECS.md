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
`recovery_token` is always stripped, and the projection is augmented
with `available_actions` and `card_range`.

### 1.1 Discovery

| Method | Path         | Status  | Body / Params        | Returns                                                  |
|--------|--------------|---------|----------------------|----------------------------------------------------------|
| GET    | `/rulesets`  | current | —                    | `[{"name": "casual"}, {"name": "high_risk"}, {"name": "standard"}]` — automatically reflects all registered rulesets. |
| GET    | `/health`    | current | —                    | `{"status": "ok"}`                                       |
| GET    | `/`          | current | —                    | Serves `frontend/index.html`                             |
| GET    | `/app.js`    | current | —                    | Serves `frontend/app.js`                                 |

### 1.2 Game lifecycle

| Method | Path              | Status  | Body                                        | Returns                                   |
|--------|-------------------|---------|---------------------------------------------|-------------------------------------------|
| POST   | `/create_game`    | current | `{ruleset?: str}` (default `"standard"`)   | `{"game_id": str}` — fails `400` on unknown `ruleset` |
| POST   | `/join_game`      | current | `{game_id, nickname}`                       | `{player_id, game_id, recovery_token, game}` — the **only** response carrying `recovery_token` |
| POST   | `/start_game`     | current | `{game_id, player_id}` (host only)          | `{game}` — requires ≥ 3 players, transitions `LOBBY → SELECT_NARRATOR` |
| POST   | `/select_narrator`| current | `{game_id, player_id, narrator_id}` (host)  | `{game}` — transitions `SELECT_NARRATOR → PLAY_CARDS` |
| POST   | `/next_phase`     | current | `{game_id, player_id}` (host only)          | `{game}` — generic advance; see §1.5 for side-effects |

### 1.3 Player actions

| Method | Path              | Status  | Body                                        | Returns / notes                           |
|--------|-------------------|---------|---------------------------------------------|-------------------------------------------|
| POST   | `/submit_card`    | current | `{game_id, player_id, card_number}`         | `{game}`. On duplicate card number: round is reset and `game_error` / `duplicate_cards` is broadcast (HTTP 200 with the post-reset `game`). |
| POST   | `/submit_vote`    | current | `{game_id, player_id, card_number}`         | `{game}`. Appends `card_number` to `Player.votes`. Rejected if narrator, wrong phase, already at cap, duplicate, own card, or not on table. |
| POST   | `/update_vote`    | current | `{game_id, player_id, card_numbers: list[int]}` | `{game}`. Replaces `Player.votes` with the given list. Rejected if wrong phase, narrator, `len(card_numbers) > Game.votes_per_player`, duplicates, own card, or any card not on the table. |

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
| `PLAY_CARDS → VOTE`                | Defence-in-depth duplicate-card check; if duplicates are found, the round is reset (same as `/submit_card`). | `phase_changed` or `game_error`|
| `VOTE → REVEAL_VOTES`              | Requires all active non-narrators to have voted at least 1 card. | `phase_changed`                |
| `REVEAL_NARRATOR → SCORE_BASE`     | `rules_engine.calculate_scores(game)` applies base scoring; `score_base_applied = true`; **target**: `scoring_step = "base"` and `last_base_delta` computed. | `scores_updated`               |
| `SCORE_BASE → SCORE_BONUS`         | `rules_engine.calculate_scores(game)` applies bonus scoring; `score_bonus_applied = true`; **target**: `scoring_step = "bonus"` and `last_bonus_delta` computed. | `scores_updated`               |
| `SCORE_BONUS → LEADERBOARD`        | —                                                                                                    | `scores_updated`               |
| `NEXT_ROUND → SELECT_NARRATOR`     | Round reset: clears `card_played`, `votes`, `cards_on_table`, scoring flags. `scoring_step` (target) is also cleared when implemented. Scores are preserved. | `phase_changed`                |
| Any other transition               | —                                                                                                    | `phase_changed`                |

`LOBBY → SELECT_NARRATOR` and `SELECT_NARRATOR → PLAY_CARDS` cannot be
advanced with `/next_phase`; use `/start_game` and `/select_narrator`.

---

## 2. WebSocket

Endpoint: `GET /ws/{game_id}` (WebSocket upgrade).

If the game does not exist, the server sends
`{"event": "error", "detail": "Game not found"}` and closes with code
`1008`.

### 2.1 Message envelope

Every server message is a JSON object with an `event` field. Messages
that carry game state also include a sanitized `game` field
(`game_wire` projection; see `DATA_MODEL.md` §3):

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
| `phase_changed`        | current | `{event, game}`                                                           | Room-wide after a non-scoring phase transition.       |
| `card_submitted`       | current | `{event, game}`                                                           | Room-wide after a card is submitted (REST or WS).     |
| `vote_submitted`       | current | `{event, game}`                                                           | Room-wide after a vote is submitted (`submit_vote`) or updated (`update_vote`). |
| `scores_updated`       | current | `{event, game}`                                                           | Room-wide after entering `SCORE_BASE`, `SCORE_BONUS`, or `LEADERBOARD`. In the target state, `game.scoring_step` (`"base"` \| `"bonus"`) and `game.last_base_delta` / `last_bonus_delta` drive the progressive-scoring UI. |
| `game_error`           | current | `{event: "game_error", error: <code>, message: <string>, game: <Game>}`    | Room-wide gameplay errors. Current codes: `duplicate_cards`. |
| `player_reconnected`   | current | `{event, game}`                                                           | Room-wide after a successful `reconnect`.             |
| `player_disconnected`  | current | `{event, game}`                                                           | Room-wide when the heartbeat task flips one or more players to `connected=false`. |
| `pong`                 | current | `{event: "pong"}`                                                         | Per-socket reply to `ping`. No `game`, no render.     |
| `error`                | current | `{event: "error", detail: <string>}`                                      | Per-socket. `detail: "recovery_failed"` is followed by close `1008`. |

### 2.4 `game_error` codes

| Code               | Status  | Meaning                                                                                   |
|--------------------|---------|-------------------------------------------------------------------------------------------|
| `duplicate_cards`  | current | Two or more players selected the same card during `PLAY_CARDS`. The round has been reset; `game` reflects the post-reset state (every `card_played` and `cards_on_table` cleared, phase still `PLAY_CARDS`). |

Future `game_error` codes should follow the same shape.

---

## 3. Rules

* Every action validates the current phase; out-of-phase calls are
  rejected.
* Only the host can trigger phase transitions.
* `recovery_token` is never included in any broadcast or in any REST
  response apart from `/join_game`.
* The `game` field on every outbound message is the **sanitized**
  `game_wire` projection (with `available_actions` and `card_range`
  injected), never the raw model.
* The backend is the single source of truth. The client must not
  compute scores, derive next-phase eligibility, or assume rule
  constants — it reads them from the broadcast.
