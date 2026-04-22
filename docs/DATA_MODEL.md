# DATA MODEL

Canonical data structures used by the backend. Every field is served
as JSON over REST and over the WebSocket `game` payload, except
`recovery_token` which is **only** returned once by `POST /join_game`
and stripped from every other projection (`game_wire`).

Two markers are used throughout this document:

* `[current]` — field exists in the code today.
* `[target]` — field is planned (`TARGET_STATE.md`) and **not yet**
  implemented.

See `CURRENT_STATE.md` §1 and `TARGET_STATE.md` §1 for the game-flow
context.

---

## 1. Player

| Field              | Type             | Status   | Description |
|--------------------|------------------|----------|-------------|
| `id`               | `str`            | current  | 8-char lowercase hex (uuid4 prefix). Stable for the life of the game. |
| `nickname`         | `str`            | current  | 1–40 chars. Trimmed at the request boundary. Display name only — not used for identity. |
| `score`            | `int`            | current  | Cumulative across rounds. Never reset during a game; only increments (or decrements, under `high_risk`) during `SCORE_BASE` / `SCORE_BONUS`. |
| `card_played`      | `int \| null`    | current  | Card number this player played this round. Must be unique across all players during `PLAY_CARDS`; cleared on round reset. |
| `vote`             | `int \| null`    | current  | Card number the player voted this round. `null` for the narrator. Cleared on round reset. **Superseded by `votes` in the target state.** |
| `votes`            | `list[int]`      | target   | Card numbers the player voted this round. Length 0, 1, or 2 (see `Game.votes_per_player`). Duplicates are rejected; own card is rejected. `null`/empty for the narrator. Cleared on round reset. |
| `connected`        | `bool`           | current  | Liveness flag. `true` on join and on any inbound event; flipped to `false` by the heartbeat task after ~45 s of silence. Players are **never removed**. |
| `last_seen`        | `float`          | current  | Unix timestamp (seconds). Updated by `ping`, `reconnect`, `submit_card`, `submit_vote`. |
| `recovery_token`   | `str`            | current  | Server-generated (uuid4 hex). **Returned only by `POST /join_game`**; stripped by `game_wire` from every other response and every WebSocket broadcast. Stored by the client in `localStorage` and replayed on the WS `reconnect` event. |

Minimum Player shape required for any future state to remain valid:

* `id`, `nickname`, `connected`, `score`, and the vote representation
  (`vote` today, `votes` in target).

---

## 2. Game

| Field                    | Type              | Status   | Description |
|--------------------------|-------------------|----------|-------------|
| `id`                     | `str`             | current  | 8-char uppercase hex (uuid4 prefix). |
| `phase`                  | `GamePhase` (enum as string) | current | One of the ten phases from `GAME_FLOW.md` §2. Serialized as the string value (e.g. `"PLAY_CARDS"`). |
| `players`                | `list[Player]`    | current  | Insertion order = join order. |
| `host_id`                | `str \| null`     | current  | Set to the first joiner; never changes. Only the host can trigger phase transitions. |
| `narrator_id`            | `str \| null`     | current  | Set in `SELECT_NARRATOR`; persists until the next round. |
| `cards_on_table`         | `list[int]`       | current  | Submission order. Cleared on round reset. |
| `ruleset`                | `str`             | current  | Scoring ruleset (`"standard"` \| `"high_risk"` \| `"casual"`). Default `"standard"`. Immutable after game creation. |
| `score_base_applied`     | `bool`            | current  | Idempotency guard: has base scoring been applied this round? |
| `score_bonus_applied`    | `bool`            | current  | Idempotency guard: has bonus scoring been applied this round? |
| `votes_locked`           | `bool`            | target   | `false` by default. When `true`, further `submit_vote` / `update_vote` calls are rejected. Set by `POST /lock_votes` (host only). Cleared on round reset. |
| `votes_per_player`       | `int`             | target   | Max votes a non-narrator player may cast (1 or 2). Derived from player count and/or ruleset; exposed so the frontend can render the vote grid correctly. |
| `scoring_step`           | `"base" \| "bonus" \| null` | target | Tells the client which progressive-scoring panel to render. Set to `"base"` when the server applies base scores, `"bonus"` on bonus, and cleared on round reset. |

---

## 3. Computed wire-only fields

These are **not** stored on the model; they are added by
`backend/routes/websocket.py:game_wire` to every outbound projection so
the frontend stays rule-agnostic.

| Field                | Type                 | Status  | Description |
|----------------------|----------------------|---------|-------------|
| `available_actions`  | `list[str]`          | current | Action names currently legal (e.g. `["submit_card", "next_phase"]`). Computed by `game_service.available_actions`. Clients check this to enable/disable buttons. |
| `card_range`         | `{min: int, max: int}` | current | Allowed card-number range (`1..84` for classic Dixit). Computed from `backend/models/constants.py`. |
| `last_base_delta`    | `dict[str, int]`     | target  | Per-player base-score delta from the last `SCORE_BASE` application, keyed by `player_id`. Used to render `"Player A +3"` lines. |
| `last_bonus_delta`   | `dict[str, int]`     | target  | Per-player bonus-score delta from the last `SCORE_BONUS` application, keyed by `player_id`. |

The `last_*_delta` fields are one acceptable way to transport
progressive-scoring deltas. An equivalent alternative is a
`scoring_events` list on the `scores_updated` WebSocket event. Either
is fine as long as the frontend does not compute deltas from previous
totals.

---

## 4. In-memory store

```python
# backend/store.py
games: dict[str, Game] = {}

def get_game(game_id: str) -> Game | None: ...
def set_game(game_id: str, game: Game) -> None: ...
def delete_game(game_id: str) -> None: ...
```

* One dictionary per process. Not thread-safe and does not need to be
  (FastAPI runs handlers on a single asyncio loop and all mutations are
  short synchronous blocks).
* No persistence. Process restart wipes every game.

---

## 5. Validation conventions (Pydantic)

* All models use `extra="forbid"` — unknown fields are rejected.
* `game_id`, `player_id`, `narrator_id`: 1–32 chars, hex
  (`[A-Fa-f0-9]`).
* `nickname`: 1–40 chars (whitespace stripped at the request boundary).
* `card_number`: integer in `[MIN_CARD_NUMBER, MAX_CARD_NUMBER]` =
  `[1, 84]`.

---

## 6. Lifecycle summary

* **Create**: `POST /create_game {ruleset?}` → new `Game` in `LOBBY`.
* **Join**: `POST /join_game` → appends a `Player`; first joiner becomes
  `host_id`.
* **Round reset** (`NEXT_ROUND → SELECT_NARRATOR`) clears:
  * every `Player.card_played`
  * every `Player.vote` (current) / `Player.votes` (target)
  * `Game.cards_on_table`
  * `Game.score_base_applied`, `Game.score_bonus_applied`
  * `Game.votes_locked`, `Game.scoring_step` (target)
* **Preserved across rounds**: `Player.score`, `Game.players`,
  `Game.host_id`, `Game.ruleset`.
