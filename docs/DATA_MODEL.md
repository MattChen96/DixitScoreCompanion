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

**Note:** There is no `scoring_step` field on `Game`. The client
distinguishes **base** vs **bonus** scoring UIs from the current phase
(`SCORE_BASE` vs `SCORE_BONUS`).

---

## 1. Player

| Field              | Type             | Status   | Description |
|--------------------|------------------|----------|-------------|
| `id`               | `str`            | current  | 8-char lowercase hex (uuid4 prefix). Stable for the life of the game. |
| `nickname`         | `str`            | current  | 1–40 chars. Trimmed at the request boundary. Display name only — not used for identity. |
| `score`            | `int`            | current  | Cumulative across rounds. Never reset during a game; only increments (or decrements, under `high_risk`) during `SCORE_BASE` / `SCORE_BONUS`. |
| `card_played`      | `int \| null`    | current  | Card number this player played this round. Must be unique across all players during `PLAY_CARDS`; cleared on round reset. |
| `votes`            | `list[int]`      | current  | Card numbers the player voted this round. Length 0, 1, or 2 (capped by `Game.votes_per_player`). Duplicates are rejected; own card is rejected. Empty for the narrator. Cleared on round reset. |
| `connected`        | `bool`           | current  | Liveness flag. `true` on join and on any inbound event; flipped to `false` by the heartbeat task after ~45 s of silence. Players are **never removed**. |
| `last_seen`        | `float`          | current  | Unix timestamp (seconds). Updated by `ping`, `reconnect`, `submit_card`, `submit_vote`, `update_vote`, `confirm_narrator`, and other player-touching actions. |
| `recovery_token`   | `str`            | current  | Server-generated (uuid4 hex). **Returned only by `POST /join_game`**; stripped by `game_wire` from every other response and every WebSocket broadcast. Stored by the client in `localStorage` and replayed on the WS `reconnect` event. |

Minimum Player shape required for any future state to remain valid:

* `id`, `nickname`, `connected`, `score`, `votes`.

---

## 2. Game

| Field                    | Type              | Status   | Description |
|--------------------------|-------------------|----------|-------------|
| `id`                     | `str`             | current  | 8-char uppercase hex (uuid4 prefix). |
| `phase`                  | `GamePhase` (enum as string) | current | One of the ten phases from `GAME_FLOW.md` §2. Serialized as the string value (e.g. `"PLAY_CARDS"`). |
| `players`                | `list[Player]`    | current  | Insertion order = join order. |
| `host_id`                | `str \| null`     | current  | Set to the first joiner; never changes. Only the host can trigger `POST /next_phase` and `POST /select_narrator`. |
| `narrator_id`            | `str \| null`     | current  | Set by the host in `SELECT_NARRATOR` (`POST /select_narrator`); cleared on round reset. |
| `narrator_confirmed`     | `bool`            | current  | `False` after `select_narrator`; set `True` by the designated narrator via `POST /confirm_narrator`. Host can advance `SELECT_NARRATOR → PLAY_CARDS` with `/next_phase` only when this is `True`. Cleared on round reset. |
| `cards_on_table`         | `list[int]`       | current  | Submission order. Cleared on round reset. |
| `ruleset`                | `str`             | current  | Scoring ruleset (`"standard"` \| `"high_risk"` \| `"casual"`). Default `"standard"`. Immutable after game creation. |
| `score_base_applied`     | `bool`            | current  | Idempotency guard: has base scoring been applied this round? |
| `score_bonus_applied`    | `bool`            | current  | Idempotency guard: has bonus scoring been applied this round? |
| `votes_per_player`       | `int`             | current  | Max votes a non-narrator player may cast (1 or 2). Set at game creation (`POST /create_game {votes_per_player}`). Default `1`. Exposed in every broadcast so the frontend can render the vote grid correctly. |
| `last_base_delta`        | `dict[str, int]`  | current  | Per-player point change from the last `SCORE_BASE` application, keyed by `player_id`. Populated by the rules engine; cleared on round reset. Used for the base scoring UI. |
| `last_bonus_delta`       | `dict[str, int]`  | current  | Per-player point change from the last `SCORE_BONUS` application, keyed by `player_id`. Populated by the rules engine; cleared on round reset. Used for the bonus scoring UI. |

---

## 3. Computed wire-only fields

These are **not** on the Pydantic `Game` / `Player` models; they are
added by `backend/routes/websocket.py:game_wire` to every outbound
projection (merged into the `game` object after
`model_dump` strips `recovery_token`):

| Field                | Type                 | Status  | Description |
|----------------------|----------------------|---------|-------------|
| `available_actions`  | `list[str]`          | current | Action names currently legal (e.g. `["submit_card", "next_phase"]`). Computed by `game_service.available_actions`. Clients check this to enable/disable buttons. |
| `card_range`         | `{min: int, max: int}` | current | Allowed card-number range (`1..84` for classic Dixit). Computed from `backend/models/constants.py`. |

All other fields in the wire `game` object come from the serialized
`Game` model (including `last_base_delta`, `last_bonus_delta`,
`narrator_confirmed`, etc.).

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

* **Create**: `POST /create_game {ruleset?, votes_per_player?}` → new
  `Game` in `LOBBY`.
* **Join**: `POST /join_game` → appends a `Player`; first joiner becomes
  `host_id`.
* **Round reset** (`NEXT_ROUND → SELECT_NARRATOR`) clears:
  * every `Player.card_played`
  * every `Player.votes`
  * `Game.cards_on_table`
  * `Game.score_base_applied`, `Game.score_bonus_applied`
  * `Game.last_base_delta`, `Game.last_bonus_delta`
  * `Game.narrator_id` and `Game.narrator_confirmed`
* **Preserved across rounds**: `Player.score`, `Game.players`,
  `Game.host_id`, `Game.ruleset`, `Game.votes_per_player`.
