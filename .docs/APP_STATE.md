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
├── rules/                        # Rules engine — structure only; not yet wired into services
│   ├── __init__.py               # Re-exports RulesEngine + load_rules
│   ├── base_rules.py             # Abstract RulesEngine base class (ABC)
│   ├── rules_loader.py           # load_rules(name) → RulesEngine instance
│   └── standard_dixit.py        # StandardDixitRules stub (no-op); future home of real logic
└── services/
    ├── __init__.py               # (empty package marker)
    ├── state_machine.py          # ALLOWED_TRANSITIONS, transition_phase, advance_phase_by_host
    ├── scoring.py                # apply_score_base, apply_score_bonus, reset_round_after_next
    ├── heartbeat.py              # Background task: flip silent players to connected=False
    └── game_service.py           # The only place that mutates game state
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
  other players.
* Every ~15 s the client sends `{event: "ping", data: {player_id}}`; the server
  replies `{event: "pong"}` and refreshes `player.last_seen`. Any inbound
  game action (`submit_card`, `submit_vote`, `reconnect`) has the same
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
| SELECT_NARRATOR  | Host     | `POST /select_narrator` → PLAY_CARDS                                |
| PLAY_CARDS       | Players  | `POST /submit_card` (or WS `submit_card`); card numbers must be unique. Duplicate → round reset + `game_error` broadcast |
| PLAY_CARDS       | Host     | `POST /next_phase` → VOTE. Scoring validation ignores disconnected players; the **narrator**, however, must have a card on the table (stall policy) |
| VOTE             | Non-narr.| `POST /submit_vote` (or WS `submit_vote`); narrator may not vote    |
| VOTE             | Host     | `POST /next_phase` → REVEAL_VOTES. "All voted" is evaluated against **active** (connected) players only |
| REVEAL_VOTES     | Host     | `POST /next_phase` → REVEAL_NARRATOR                                |
| REVEAL_NARRATOR  | Host     | `POST /next_phase` → SCORE_BASE (server applies base scores)        |
| SCORE_BASE       | Host     | `POST /next_phase` → SCORE_BONUS (server applies bonus scores)      |
| SCORE_BONUS      | Host     | `POST /next_phase` → LEADERBOARD                                    |
| LEADERBOARD      | Host     | `POST /next_phase` → NEXT_ROUND                                     |
| NEXT_ROUND       | Host     | `POST /next_phase` → SELECT_NARRATOR (server resets round data)     |

`/next_phase` is the **only** generic advance. `LOBBY` and `SELECT_NARRATOR` cannot
be advanced this way — they require `/start_game` and `/select_narrator` so the
host’s explicit input is captured.

### 2.3 Scoring (deterministic; server-only)

Computed once when the host enters `SCORE_BASE` and once when entering `SCORE_BONUS`.
Both are guarded by idempotency flags (`score_base_applied`, `score_bonus_applied`).

Base (`apply_score_base`):

* Pre-conditions validated: narrator set, every player has played a card, every
  non-narrator has voted, no two players played the same card number.
* Let `voters` = non-narrator players. Let `correct` = voters who voted for the
  narrator’s card.
  * If `len(correct) == 0` **or** `len(correct) == len(voters)`:
    narrator gets **0**, every other player gets **+2**.
  * Otherwise: narrator gets **+3**, every correct guesser gets **+3**, others **0**.

Bonus (`apply_score_bonus`):

* For every player (including the narrator), add **+1 per vote received** on the
  card they played this round.
* Requires `score_base_applied == True`.

Reset (`reset_round_after_next`):

* Triggered when `/next_phase` advances `NEXT_ROUND → SELECT_NARRATOR`.
* Clears every `Player.card_played`, `Player.vote`, `Game.cards_on_table`, and
  both score-applied flags. Player **scores are preserved** across rounds.

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
* `LOBBY → ...` and `SELECT_NARRATOR → ...` cannot be advanced via `/next_phase`;
  use `/start_game` and `/select_narrator` instead.

---

## 3. API Summary

### 3.1 REST endpoints (implemented in `backend/routes/game.py`)

All bodies are JSON; all responses are JSON. Errors return
`{"detail": "<message>"}` with `404` for "Game not found" and `400` for everything
else (Pydantic validation errors return `422` automatically).

| Method | Path              | Body                                                | Returns                                                        |
|--------|-------------------|-----------------------------------------------------|----------------------------------------------------------------|
| POST   | `/create_game`    | *(none)*                                            | `{"game_id": str}`                                             |
| POST   | `/join_game`      | `{game_id, nickname}`                               | `{player_id, game_id, recovery_token, game}` + WS `player_joined` broadcast |
| POST   | `/start_game`     | `{game_id, player_id}`                              | `{game}` + WS `phase_changed`                                  |
| POST   | `/next_phase`     | `{game_id, player_id}`                              | `{game}` + WS `phase_changed` / `scores_updated` / `game_error` (duplicate guard) |
| POST   | `/select_narrator`| `{game_id, player_id, narrator_id}`                 | `{game}` + WS `phase_changed`                                  |
| POST   | `/submit_card`    | `{game_id, player_id, card_number}`                 | `{game}` + WS `card_submitted`, **or** on duplicate: `{game}` (reset state) + WS `game_error` |
| POST   | `/submit_vote`    | `{game_id, player_id, card_number}`                 | `{game}` + WS `vote_submitted`                                 |

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
| C → S     | `reconnect`           | `{player_id, recovery_token}` — per-socket `game_state` on success + `player_reconnected` broadcast; `error`/`recovery_failed` + close(1008) on failure |
| C → S     | `ping`                | `{player_id}` — server replies `pong` and refreshes `player.last_seen`                    |
| S → C     | `game_state`          | Sanitized game (sent on connect, on `join_room`, and to the reconnecting socket)          |
| S → C     | `player_joined`       | Sanitized game (after `/join_game`)                                                       |
| S → C     | `phase_changed`       | Sanitized game (after non-scoring transitions)                                            |
| S → C     | `card_submitted`      | Sanitized game (after a card is submitted via REST or WS)                                 |
| S → C     | `vote_submitted`      | Sanitized game (after a vote is submitted via REST or WS)                                 |
| S → C     | `scores_updated`      | Sanitized game (after entering SCORE_BASE / SCORE_BONUS / LEADERBOARD)                    |
| S → C     | `game_error`          | `{error, message, game}` — room-wide gameplay error (e.g. `duplicate_cards`)              |
| S → C     | `player_reconnected`  | Sanitized game (after a successful `reconnect`)                                           |
| S → C     | `player_disconnected` | Sanitized game (after the heartbeat task flips one or more players to `connected=false`) |
| S → C     | `pong`                | `{}` — per-socket reply to `ping`; no `game` payload, no render                           |
| S → C     | `error`               | `{detail}` — sent only to the offending socket. `detail: "recovery_failed"` is followed by close(1008) |

The frontend currently uses `join_room` only; it submits cards and votes via
REST. The WS `submit_card` / `submit_vote` handlers exist as required by
`API_SPECS.md` and are functionally equivalent to the REST endpoints (including
the duplicate-card reset + `game_error` broadcast).

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
| `vote`        | `int \| None` | `None` | Set in VOTE, cleared on round reset; null for narrator |
| `recovery_token` | `str`     | uuid4 hex | Server-generated. Returned **only** by `/join_game`; stripped from every broadcast and every other REST response by `game_wire`. |
| `connected`   | `bool`      | `True`  | Liveness flag. Flipped to `False` by the heartbeat scan after ~45 s of silence; flipped back to `True` on any inbound event. |
| `last_seen`   | `float`     | `time.time()` | Unix timestamp (seconds). Updated by `ping`, `reconnect`, `submit_card`, and `submit_vote`. |

### 4.2 `Game`

| Field                 | Type             | Default       | Notes                                                |
|-----------------------|------------------|---------------|------------------------------------------------------|
| `id`                  | `str`            | —             | 8-char uppercase hex (uuid4 prefix)                  |
| `players`             | `list[Player]`   | `[]`          | Insertion order = join order                         |
| `host_id`             | `str \| None`    | `None`        | Set to first joiner; never changes                   |
| `narrator_id`         | `str \| None`    | `None`        | Set in SELECT_NARRATOR; persists until next round    |
| `phase`               | `GamePhase`      | `LOBBY`       | One of the ten phases enumerated above               |
| `cards_on_table`      | `list[int]`      | `[]`          | Submission order; cleared on round reset             |
| `score_base_applied`  | `bool`           | `False`       | Idempotency guard for `apply_score_base`             |
| `score_bonus_applied` | `bool`           | `False`       | Idempotency guard for `apply_score_bonus`            |

`Game.phase` serializes as its string value (e.g. `"PLAY_CARDS"`) in JSON.

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

`DATA_MODEL.md` describes the minimal fields. The implementation adds
**operational fields** that the spec doesn’t enumerate but that the flow needs:

* `Game.host_id` — required by the host-only transition rules.
* `Game.score_base_applied` / `Game.score_bonus_applied` — replay protection.

No fields from the spec are missing.

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
* **Frontend does not show** `REVEAL_VOTES`, `REVEAL_NARRATOR`, `SCORE_BASE`,
  `SCORE_BONUS`, `NEXT_ROUND` as distinct screens — they all render the same
  "Host advances when ready" panel. This matches the "companion to the physical
  game" intent: the actual reveals happen on the table.
* **Host advances PLAY_CARDS → VOTE and VOTE → REVEAL_VOTES manually.** After a
  player has submitted (or is the narrator during VOTE), the host sees the
  same waiting screen with a "Continue" button enabled once everyone has acted
  (`allCardsPlayed()` / `allVotesIn()` in `frontend/app.js`). Non-hosts only
  see the waiting message.
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

* Any change to game rules **must** update `backend/services/scoring.py` or
  `backend/services/state_machine.py` **and** this document in the same commit.
* Any new REST endpoint or WebSocket event **must** be reflected in section 3.
* Any new field on `Player` / `Game` **must** be reflected in section 4.
* The backend remains the single source of truth — clients render `state.game`
  from server messages and never compute game logic locally.
