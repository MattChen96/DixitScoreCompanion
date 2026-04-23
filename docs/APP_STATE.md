# APP STATE — Single Source of Truth

This document describes what is **actually implemented** in the codebase right now.
It is meant to be the canonical reference for any future development prompt.
If reality drifts from this file, update this file (or the code) until they agree.

---

## 1. Current Architecture

### 1.1 Backend layout

```
backend/
├── main.py                       # FastAPI app, CORS, static frontend, /health, heartbeat lifespan
├── store.py                      # In-memory game registry (the only data store)
├── models/
│   ├── __init__.py               # Re-exports public models + constants
│   ├── constants.py              # GAME_PHASES, MIN/MAX_CARD_NUMBER (1..84)
│   ├── game_phase.py             # GamePhase(str, Enum) — phase order = play order
│   └── game.py                   # Pydantic Player + Game models
├── routes/
│   ├── __init__.py               # (empty package marker)
│   ├── game.py                   # REST endpoints + request schemas
│   └── websocket.py              # /ws/{game_id} endpoint, broadcast + game_wire helpers
├── rules/                        # Rules engine — owns all scoring logic
│   ├── __init__.py               # Re-exports RulesEngine + load_rules
│   ├── base_rules.py             # Abstract RulesEngine base class (ABC)
│   ├── rules_loader.py           # load_rules(name), available_rulesets()
│   ├── config/                   # JSON point values per ruleset
│   │   ├── standard.json         # Classic Dixit scoring
│   │   ├── high_risk.json        # Amplified rewards, narrator penalties
│   │   └── casual.json           # Low-stakes, forgiving
│   ├── standard_dixit.py         # StandardDixitRules → standard.json
│   ├── high_risk_rules.py        # HighRiskRules → high_risk.json
│   └── casual_rules.py           # CasualRules → casual.json
└── services/
    ├── __init__.py               # (empty package marker)
    ├── state_machine.py          # ALLOWED_TRANSITIONS, transition_phase, advance_phase_by_host
    ├── heartbeat.py              # Background task: flip silent players to connected=False
    └── game_service.py           # The only place that mutates game state; resolves the per-game rules engine via _engine_for(game)
```

Layer rules (enforced by import direction):

* `models/` — pure data. Imports nothing from `services` or `routes`.
* `services/` — game logic. Imports `models/` and `store`. Never touches HTTP/WebSocket.
* `routes/` — transport. Imports `services/`. Never mutates state directly.
* `main.py` — composition root. Wires routers and serves the frontend.

### 1.2 Frontend layout

```
frontend/
├── index.html                    # Mobile-first shell, dark theme, phase pill, error region
└── app.js                        # Vanilla JS IIFE: state, REST, WebSocket, render()
```

The frontend renders **purely from `state.game`** received from the server. It never
computes scores or phase transitions locally; it only formats and sends actions.

The frontend is intentionally rule-agnostic:

* No hardcoded phase transition logic. The backend's `available_actions` list
  tells it what the host can do.
* No hardcoded card bounds. The backend's `card_range` provides min/max.
* No active-player counting or vote-in checks. The backend computes these and
  includes the results in `available_actions`.
* Ruleset constants are never embedded in the frontend. All point values, bonus
  multipliers, and scoring gates live in the rules engine config files.

`localStorage` keys: `dixit_game_id`, `dixit_player_id`, `dixit_recovery_token` —
used to survive page reload and to drive automatic WebSocket reconnect. The
token is stored only in `localStorage`; the server never echoes it back in
broadcasts.

### 1.3 WebSocket usage

* One WebSocket per browser tab, opened to `/ws/{game_id}` after `POST /join_game`.
* On connect:
  * If the client has a stored `recovery_token`, it sends
    `{event: "reconnect", data: {player_id, recovery_token}}`. On success the
    server replies with a per-socket `game_state` and broadcasts
    `player_reconnected` to the room; on failure it replies
    `{event: "error", detail: "recovery_failed"}` and closes with code `1008`.
  * Otherwise, it sends `{event: "join_room", data: {}}` and the server
    broadcasts `game_state` to the room (legacy path, still supported).
* Every broadcast carries the **sanitized** `Game` projection
  (`backend/routes/websocket.py:game_wire`) so `recovery_token` never leaks to
  other players. This projection also augments the model with two computed fields:
  - `available_actions` — list of action names legal right now (e.g.
    `["submit_card", "next_phase"]`), empowering the client to know what the
    host can trigger without reimplementing game logic.
  - `card_range` — `{min, max}` for card inputs, so the frontend never hardcodes
    Dixit-specific bounds; useful for supporting alternate card ranges in
    the future.
* Every ~15 s the client sends `{event: "ping", data: {player_id}}`; the server
  replies `{event: "pong"}` and refreshes `player.last_seen`. Any inbound
  game action (`submit_card`, `submit_vote`, `update_vote`, `confirm_narrator`, `reconnect`, etc.) has the same
  liveness effect.
* The heartbeat task (`backend/services/heartbeat.py`) scans every
  `HEARTBEAT_SCAN_S = 10` s. Players silent for more than
  `HEARTBEAT_TIMEOUT_S = 45` s are flipped to `connected = false` and the
  room receives a `player_disconnected` broadcast. Players are **never
  removed** from the game.
* On socket close, the client retries with a 2 s backoff
  (`connectWs` → `onclose`).

### 1.4 State management approach

* Single in-memory dictionary `backend.store.games: dict[str, Game]`.
* Pydantic models are mutated in place (not immutable). The store holds references.
* No locks: FastAPI runs the asyncio loop single-threaded, and every mutation is a
  short synchronous block; broadcasts happen after mutation returns.
* No persistence. Process restart wipes all games.

---

## 2. Game Flow (current implemented version)

### 2.1 Phases implemented

All ten phases from `GAME_FLOW.md` are implemented:

```
LOBBY → SELECT_NARRATOR → PLAY_CARDS → VOTE
      → REVEAL_VOTES → REVEAL_NARRATOR
      → SCORE_BASE → SCORE_BONUS
      → LEADERBOARD → NEXT_ROUND → SELECT_NARRATOR (loop)
```

Source: `backend/services/state_machine.py:ALLOWED_TRANSITIONS`. Every transition
listed there is reachable; no others are allowed.

### 2.2 Phase rules (as enforced)

| Phase            | Who acts | Action enforced                                                     |
|------------------|----------|---------------------------------------------------------------------|
| LOBBY            | Anyone   | `POST /join_game` (first joiner becomes host)                       |
| LOBBY            | Host     | `POST /start_game` (≥ 3 players required) → SELECT_NARRATOR         |
| SELECT_NARRATOR  | Host     | `POST /select_narrator` — sets `narrator_id`, `narrator_confirmed = false`; phase **stays** `SELECT_NARRATOR`. Room WS: `narrator_selected` |
| SELECT_NARRATOR  | Narrator | `POST /confirm_narrator` (or WS `confirm_narrator`) — sets `narrator_confirmed = true`. Room WS: `narrator_confirmed` |
| SELECT_NARRATOR  | Host     | `POST /next_phase` → `PLAY_CARDS` only if `narrator_id` is set and `narrator_confirmed == true` |
| PLAY_CARDS       | Players  | `POST /submit_card` (or WS `submit_card`); card numbers must be unique. Duplicate → round reset + `game_error` broadcast |
| PLAY_CARDS       | Host     | `POST /next_phase` → VOTE. `next_phase` in `available_actions` requires all **active** players to have played; the **narrator** must have a card on the table (stall policy) |
| VOTE             | Non-narr.| `POST /submit_vote` / `POST /update_vote` (or WS equivalents); narrator may not vote    |
| VOTE             | Host     | `POST /next_phase` → REVEAL_VOTES. Gated so every **active** non-narrator has ≥1 vote (server exposes `next_phase` in `available_actions` when satisfied) |
| REVEAL_VOTES     | Host     | `POST /next_phase` → REVEAL_NARRATOR                                |
| REVEAL_NARRATOR  | Host     | `POST /next_phase` → SCORE_BASE (server applies base scores)        |
| SCORE_BASE       | Host     | `POST /next_phase` → SCORE_BONUS (server applies bonus scores)      |
| SCORE_BONUS      | Host     | `POST /next_phase` → LEADERBOARD                                    |
| LEADERBOARD      | Host     | `POST /next_phase` → NEXT_ROUND                                     |
| NEXT_ROUND       | Host     | `POST /next_phase` → SELECT_NARRATOR (server resets round data)     |

`/next_phase` is the generic advance for all phases **except** `LOBBY`, which
uses only `POST /start_game`. In `SELECT_NARRATOR`, the host still uses
`POST /select_narrator` to pick the narrator, and `POST /next_phase` to reach
`PLAY_CARDS` **after** the narrator has `POST /confirm_narrator`.

### 2.3 Scoring (ruleset-dependent; server-only)

All scoring lives in the **rules engine** (`backend/rules/`) and is reached
exclusively via `rules_engine.calculate_scores(game)`.

#### Available Rulesets

* **`standard`** (default) — classic Dixit scoring from official rules.
  Config: `backend/rules/config/standard.json`
  * Base: narrator=0 all|none guessed (others +2), else narrator +3 and correct +3
  * Bonus: +1 per vote received on **non-narrator** players’ cards only (narrator excluded)

* **`high_risk`** — amplified rewards for success, penalties for failure.
  Config: `backend/rules/config/high_risk.json`
  * Base: narrator=-2 if all|none (others +3), else narrator +5 and correct +5
  * Bonus: +2 per vote on **non-narrator** players’ cards only (narrator excluded)

* **`casual`** — forgiving, beginner-friendly scoring.
  Config: `backend/rules/config/casual.json`
  * Base: narrator=+1 if all|none (others +2), else narrator +2 and correct +2
  * Bonus: +1 per vote on **non-narrator** players’ cards only (narrator excluded)

#### Engine Architecture

Each game picks its ruleset and vote cap at creation time
(`POST /create_game {ruleset?, votes_per_player?}`), stored on `Game.ruleset`
and `Game.votes_per_player` (1 or 2), immutable afterwards.

`game_service._engine_for(game)` resolves the ruleset name via
`rules_loader.load_rules(...)` and caches one stateless engine instance per
ruleset name for efficiency. The engine is pre-warmed at `start_game` and
called from `next_phase` when the transition lands on `SCORE_BASE` or
`SCORE_BONUS`.

`create_game` validates the ruleset name immediately so an unknown name
returns a `400` instead of blowing up later.

Routes and services contain **no** scoring logic.

`calculate_scores` dispatches by the current phase and is guarded by the
idempotency flags on `Game` (`score_base_applied`, `score_bonus_applied`).

Base scoring (`phase == SCORE_BASE`):

* Pre-conditions validated: narrator set, narrator has played a card (stall
  policy — applies even if the narrator is disconnected), every **active**
  player has played a card, every **active** non-narrator has voted, no two
  players played the same card number.
* Let `voters` = non-narrator players. Let `correct` = voters who voted for the
  narrator’s card.
  * If `len(correct) == 0` **or** `len(correct) == len(voters)`:
    narrator gets **0**, every other player gets **+2**.
  * Otherwise: narrator gets **+3**, every correct guesser gets **+3**, others **0**.

Bonus scoring (`phase == SCORE_BONUS`):

* For every **non-narrator** player, add the ruleset’s **vote bonus** per vote
  received on the card they played this round. The **narrator is excluded** —
  they do not receive bonus points for votes on their card.
* Requires `score_base_applied == True`.

Round reset (`_reset_round_after_next`, in `game_service`):

* Triggered when `/next_phase` advances `NEXT_ROUND → SELECT_NARRATOR`.
* Clears every `Player.card_played`, `Player.votes`, `Game.cards_on_table`,
  `Game.last_base_delta`, `Game.last_bonus_delta`, both score-applied flags,
  `Game.narrator_id`, and `Game.narrator_confirmed`. Player **scores** and
  `Game.votes_per_player` **are preserved** across rounds.
* Lives in `game_service` (not the rules engine) because it's round lifecycle,
  not rule-dependent logic.

### 2.4 Deviations from `GAME_FLOW.md`

`GAME_FLOW.md` only enumerates phases and high-level actions. The implementation
adds the following concrete rules; none of them contradict the spec:

* Minimum **3 players** to start the game (rejected at `/start_game`).
* The first player to join automatically becomes the **host** (`Game.host_id`).
* Card numbers are restricted to **1..84** (classic Dixit deck).
* Cards on the table are stored in submission order.
* During PLAY_CARDS, every player must select a **unique card number**. If a
  player submits a card already played this round, the service raises
  `DuplicateCardError` and **invalidates the round**: every `card_played` and
  `cards_on_table` is cleared, the phase stays `PLAY_CARDS`, and the room is
  notified with `game_error` / `duplicate_cards`. `next_phase` carries the same
  guard as a defence-in-depth check before the PLAY_CARDS → VOTE transition.
* `LOBBY → SELECT_NARRATOR` cannot use `/next_phase`; it requires `/start_game`.
* `SELECT_NARRATOR → PLAY_CARDS` **does** use `/next_phase` (host) after
  `/select_narrator` and `/confirm_narrator`; it does **not** use
  `/select_narrator` alone to change phase.

---

## 3. API Summary

### 3.1 REST endpoints (implemented in `backend/routes/game.py`)

All bodies are JSON; all responses are JSON. Errors return
`{"detail": "<message>"}` with `404` for "Game not found" and `400` for everything
else (Pydantic validation errors return `422` automatically).

| Method | Path              | Body                                                | Returns                                                        |
|--------|-------------------|-----------------------------------------------------|----------------------------------------------------------------|
| POST   | `/create_game`    | `{ruleset?, votes_per_player?}` (defaults `"standard"`, `1`) | `{"game_id": str}`                                             |
| POST   | `/join_game`      | `{game_id, nickname}`                               | `{player_id, game_id, recovery_token, game}` + WS `player_joined` broadcast |
| POST   | `/start_game`     | `{game_id, player_id}`                              | `{game}` + WS `phase_changed`                                  |
| POST   | `/next_phase`     | `{game_id, player_id}`                              | `{game}` + WS `phase_changed` / `scores_updated` / `game_error` (duplicate guard) |
| POST   | `/select_narrator`| `{game_id, player_id, narrator_id}`                 | `{game}` + WS `narrator_selected` (phase still `SELECT_NARRATOR`) |
| POST   | `/confirm_narrator` | `{game_id, player_id}` (narrator)                | `{game}` + WS `narrator_confirmed`                             |
| POST   | `/submit_card`    | `{game_id, player_id, card_number}`                 | `{game}` + WS `card_submitted`, **or** on duplicate: `{game}` (reset state) + WS `game_error` |
| POST   | `/submit_vote`    | `{game_id, player_id, card_number}`                 | `{game}` + WS `vote_submitted`                                 |
| POST   | `/update_vote`    | `{game_id, player_id, card_numbers}` (1–2 distinct cards) | `{game}` + WS `vote_submitted`                                 |

`/next_phase` emits `scores_updated` when the resulting phase is `SCORE_BASE`,
`SCORE_BONUS`, or `LEADERBOARD`; otherwise `phase_changed`. If the host tries
to advance from PLAY_CARDS while duplicate `card_played` values exist, the
round is reset and `game_error` is broadcast instead.

Duplicate-card behaviour for `/submit_card`: when the submitted `card_number`
already appears in `cards_on_table`, the service clears every `card_played` and
`cards_on_table` (phase stays `PLAY_CARDS`) and the room receives a single
`game_error` broadcast with `error: "duplicate_cards"`. The HTTP response is a
normal `200` carrying the post-reset `game` so the submitting client can
re-render immediately.

Validation (Pydantic):

* `game_id`, `player_id`, `narrator_id`: 1–32 chars, hex `[A-Fa-f0-9]`.
* `nickname`: 1–40 chars (whitespace stripped).
* `card_number`: integer in `[1, 84]`.
* `card_numbers`: 1–2 distinct integers in `[1, 84]` (`/update_vote`).
* All bodies use `extra="forbid"` — unknown fields are rejected.

`recovery_token` is returned **only** here and stored by the client in
`localStorage`. Every other REST response and every WebSocket broadcast
emits the sanitized `Game` projection (`game_wire`) which strips
`recovery_token` from every player.

Static / utility:

* `GET /` → serves `frontend/index.html`.
* `GET /app.js` → serves `frontend/app.js`.
* `GET /health` → `{"status": "ok"}`.

### 3.2 WebSocket events (implemented in `backend/routes/websocket.py`)

Endpoint: `GET /ws/{game_id}` (WebSocket upgrade). If the game does not exist,
the server sends `{"event": "error", "detail": "Game not found"}` and closes
with code `1008`.

Message envelope (every server message): `{"event": <name>, "game": <Game JSON>}`,
except `error` which is `{"event": "error", "detail": "<message>"}`.

| Direction | Event                 | Payload                                                                                  |
|-----------|-----------------------|------------------------------------------------------------------------------------------|
| C → S     | `join_room`           | `{}` (server replies with `game_state`)                                                  |
| C → S     | `submit_card`         | `{player_id, card_number}` — same effect as REST                                         |
| C → S     | `submit_vote`         | `{player_id, card_number}` — same effect as REST                                         |
| C → S     | `update_vote`         | `{player_id, card_numbers}` — same effect as REST                                        |
| C → S     | `confirm_narrator`    | `{player_id}` — same effect as REST `POST /confirm_narrator`                            |
| C → S     | `reconnect`           | `{player_id, recovery_token}` — per-socket `game_state` on success + `player_reconnected` broadcast; `error`/`recovery_failed` + close(1008) on failure |
| C → S     | `ping`                | `{player_id}` — server replies `pong` and refreshes `player.last_seen`                    |
| S → C     | `game_state`          | Sanitized game (sent on connect, on `join_room`, and to the reconnecting socket)          |
| S → C     | `player_joined`       | Sanitized game (after `/join_game`)                                                       |
| S → C     | `narrator_selected`   | Sanitized game (after `POST /select_narrator`)                                            |
| S → C     | `narrator_confirmed`  | Sanitized game (after `POST /confirm_narrator`)                                           |
| S → C     | `phase_changed`       | Sanitized game (after non-scoring transitions)                                            |
| S → C     | `card_submitted`      | Sanitized game (after a card is submitted via REST or WS)                                 |
| S → C     | `vote_submitted`      | Sanitized game (after a vote is submitted via REST or WS)                                 |
| S → C     | `scores_updated`      | Sanitized game (after entering SCORE_BASE / SCORE_BONUS / LEADERBOARD)                    |
| S → C     | `game_error`          | `{error, message, game}` — room-wide gameplay error (e.g. `duplicate_cards`)              |
| S → C     | `player_reconnected`  | Sanitized game (after a successful `reconnect`)                                           |
| S → C     | `player_disconnected` | Sanitized game (after the heartbeat task flips one or more players to `connected=false`) |
| S → C     | `pong`                | `{}` — per-socket reply to `ping`; no `game` payload, no render                           |
| S → C     | `error`               | `{detail}` — sent only to the offending socket. `detail: "recovery_failed"` is followed by close(1008) |

The frontend uses `reconnect` and `join_room` and performs actions via
REST. WebSocket `submit_card`, `submit_vote`, `update_vote`, and
`confirm_narrator` exist for parity and match the REST routes (see
`API_SPECS.md`).

Defined `game_error` codes:

* `duplicate_cards` — two or more players selected the same card during
  PLAY_CARDS. The included `game` shows the reset round (every `card_played`
  and `cards_on_table` cleared, phase still `PLAY_CARDS`).

---

## 4. Data Model (current implementation)

Defined in `backend/models/game.py` using Pydantic v2 (`extra="forbid"`).

### 4.1 `Player`

| Field         | Type        | Default | Notes                                              |
|---------------|-------------|---------|----------------------------------------------------|
| `id`          | `str`       | —       | 8-char lowercase hex (uuid4 prefix)                |
| `nickname`    | `str`       | —       | 1–40 chars; trimmed at request boundary            |
| `score`       | `int`       | `0`     | Cumulative across rounds; never reset              |
| `card_played` | `int \| None` | `None` | Set in PLAY_CARDS, cleared on round reset. **Must be unique across players** during a PLAY_CARDS round; a collision triggers `DuplicateCardError` and resets the round. |
| `votes`       | `list[int]`   | `[]`   | Card numbers voted this round; length capped by `Game.votes_per_player`. Cleared on round reset; empty for narrator. |
| `recovery_token` | `str`     | uuid4 hex | Server-generated. Returned **only** by `/join_game`; stripped from every broadcast and every other REST response by `game_wire`. |
| `connected`   | `bool`      | `True`  | Liveness flag. Flipped to `False` by the heartbeat scan after ~45 s of silence; flipped back to `True` on any inbound event. |
| `last_seen`   | `float`     | `time.time()` | Unix timestamp (seconds). Updated by `ping`, `reconnect`, `submit_card`, `submit_vote`, `update_vote`, `confirm_narrator`, etc. |

### 4.2 `Game`

| Field                 | Type             | Default       | Notes                                                |
|-----------------------|------------------|---------------|------------------------------------------------------|
| `id`                  | `str`            | —             | 8-char uppercase hex (uuid4 prefix)                  |
| `players`             | `list[Player]`   | `[]`          | Insertion order = join order                         |
| `host_id`             | `str \| None`    | `None`        | Set to first joiner; never changes                   |
| `narrator_id`         | `str \| None`    | `None`        | Set in SELECT_NARRATOR; cleared on round reset      |
| `narrator_confirmed`  | `bool`           | `False`       | Set `True` by narrator’s `POST /confirm_narrator`; cleared on round reset |
| `phase`               | `GamePhase`      | `LOBBY`       | One of the ten phases enumerated above               |
| `cards_on_table`      | `list[int]`      | `[]`          | Submission order; cleared on round reset             |
| `ruleset`             | `str`            | `"standard"`  | Chosen at `create_game`; immutable                   |
| `votes_per_player`    | `int`            | `1`           | 1 or 2; set at `create_game`; immutable              |
| `score_base_applied`  | `bool`           | `False`       | Idempotency guard for base scoring                   |
| `score_bonus_applied` | `bool`           | `False`       | Idempotency guard for bonus scoring                  |
| `last_base_delta`     | `dict[str, int]` | `{}`         | Per-player base delta after last `SCORE_BASE` apply; cleared on round reset |
| `last_bonus_delta`    | `dict[str, int]` | `{}`         | Per-player bonus delta after last `SCORE_BONUS` apply; cleared on round reset |

`Game.phase` serializes as its string value (e.g. `"PLAY_CARDS"`) in JSON. There
is no `scoring_step` field — the client uses the phase name plus `last_*_delta`
for scoring screens.

### 4.3 In-memory store

```python
# backend/store.py
games: dict[str, Game] = {}

def get_game(game_id: str) -> Game | None: ...
def set_game(game_id: str, game: Game) -> None: ...
def delete_game(game_id: str) -> None: ...
```

There is exactly one `games` dict per process. It is not thread-safe; it does not
need to be, because FastAPI runs handlers on a single asyncio loop and all
mutations are synchronous and short.

### 4.4 Differences from `DATA_MODEL.md`

`DATA_MODEL.md` is the canonical field list. `APP_STATE` §4 mirrors it; the
`game` JSON from the server is the Pydantic dump plus `available_actions` and
`card_range` from `game_wire` (see `DATA_MODEL.md` §3).

---

## 5. Known Limitations / Assumptions

* **No persistence.** A process restart loses every game. Acceptable per
  `ARCHITECTURE.md` ("No persistence after server restart").
* **No authentication.** Anyone with a `game_id` can join; anyone with a
  `(game_id, player_id)` pair can act as that player. Reconnect is gated by
  a server-issued `recovery_token` returned to the joining client once.
  Matches `PROJECT_RULES.md`.
* **Host never changes.** If the host disconnects, the game **stalls** until
  they reconnect. There is no host-handoff flow, and no auto-abort.
* **Narrator stall.** If the narrator disconnects before playing a card,
  scoring validation refuses to advance the round; the flow resumes when
  they reconnect. Active, connected non-narrators do not block progress —
  only they are required to have played / voted for scoring to pass.
* **No leave/kick.** Players cannot be removed from a game once joined. The
  heartbeat task only flips `connected = false`; it never deletes players.
* **One game per `game_id`.** `game_id` collisions are statistically improbable
  (8 hex chars from uuid4) but not guarded; a collision would silently overwrite.
* **No rate limiting** on REST or WebSocket.
* **CORS is wide open** (`allow_origins=["*"]`). Suitable for a LAN companion
  app per `ARCHITECTURE.md` ("local network or simple URL").
* **WebSocket reconnects** (token-backed) send a per-socket `game_state` to
  the rejoining client and broadcast `player_reconnected` to the rest of
  the room. Heartbeat constants (`backend/services/heartbeat.py`):
  `HEARTBEAT_SCAN_S = 10`, `HEARTBEAT_TIMEOUT_S = 45`. Client ping cadence:
  15 s.
* **Card numbers are integers in 1..84.** A non-classic Dixit deck would require
  raising `MAX_CARD_NUMBER` in `backend/models/constants.py`.
* **Duplicate `card_played` across players is rejected at submission time**: the
  second submission triggers `DuplicateCardError`, the round is invalidated
  (every `card_played` and `cards_on_table` cleared, phase stays `PLAY_CARDS`),
  and the room is notified via `game_error` / `duplicate_cards`. Players then
  replay their cards from scratch. The same guard runs defensively in
  `next_phase` for the PLAY_CARDS → VOTE transition.
* **Frontend** (`frontend/app.js` `render()`): `REVEAL_VOTES` shows a per-player
  vote list; `SCORE_BASE` / `SCORE_BONUS` use `last_base_delta` /
  `last_bonus_delta` panels; `LEADERBOARD` shows cumulative scores;
  `REVEAL_NARRATOR` and `NEXT_ROUND` use a generic "host continues" panel
  (physical table handles the real reveal / round wrap-up between those).
* **Host continues** are enabled when `next_phase` appears in
  `state.game.available_actions` (and the user is the host) — the frontend does
  not count votes or played cards itself.
* **No automated tests** are checked in; correctness is verified ad hoc via the
  smoke script described in `agent-transcripts` and through manual play.

---

## 6. How to Run

```bash
# Local (Python 3.9+ supported; 3.12 in Docker)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# Docker
docker build -t dixit-score-companion .
docker run --rm -p 8000:8000 dixit-score-companion
```

Open `http://localhost:8000` on each player’s phone (same LAN).

---

## 7. Change Discipline

* Any change to game rules **must** update `backend/rules/standard_dixit.py`
  (or `backend/services/state_machine.py` for phase-flow changes) **and** this
  document in the same commit.
* Any new REST endpoint or WebSocket event **must** be reflected in section 3.
* Any new field on `Player` / `Game` **must** be reflected in section 4.
* The backend remains the single source of truth — clients render `state.game`
  from server messages and never compute game logic locally.
