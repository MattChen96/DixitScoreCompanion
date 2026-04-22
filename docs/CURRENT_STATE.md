# CURRENT STATE (AS-IS)

Exact description of how the app works **today**, as implemented in the
codebase. For deep implementation notes see `APP_STATE.md`; for planned
behaviour see `TARGET_STATE.md`.

---

## 1. Game phases currently implemented

Ten phases, in this strict order (loop at the end):

```
LOBBY
  → SELECT_NARRATOR
  → PLAY_CARDS
  → VOTE
  → REVEAL_VOTES
  → REVEAL_NARRATOR
  → SCORE_BASE
  → SCORE_BONUS
  → LEADERBOARD
  → NEXT_ROUND
  → SELECT_NARRATOR   (loop)
```

Source of truth: `backend/services/state_machine.py:ALLOWED_TRANSITIONS`
and `backend/models/game_phase.py`.

Who advances each phase:

| Phase             | Transition trigger                           | Controller |
|-------------------|----------------------------------------------|------------|
| LOBBY             | `POST /start_game` (≥ 3 players)             | Host       |
| SELECT_NARRATOR   | `POST /select_narrator`                      | Host       |
| PLAY_CARDS        | `POST /next_phase`                           | Host       |
| VOTE              | `POST /next_phase`                           | Host       |
| REVEAL_VOTES      | `POST /next_phase`                           | Host       |
| REVEAL_NARRATOR   | `POST /next_phase` → triggers base scoring   | Host       |
| SCORE_BASE        | `POST /next_phase` → triggers bonus scoring  | Host       |
| SCORE_BONUS       | `POST /next_phase`                           | Host       |
| LEADERBOARD       | `POST /next_phase`                           | Host       |
| NEXT_ROUND        | `POST /next_phase` → round reset             | Host       |

`/next_phase` is the generic advance. `LOBBY` and `SELECT_NARRATOR` use
dedicated endpoints (`/start_game`, `/select_narrator`) because they
require explicit host input.

---

## 2. Voting system

* **1 or 2 votes per non-narrator player** (`Player.votes: list[int]`).
  The cap is `Game.votes_per_player` (1 or 2, set at game creation, default 1).
  The narrator cannot vote.
* **Votes are editable** until the host locks them. While
  `Game.votes_locked == false`, a player may call `POST /update_vote`
  to replace their vote list, or `POST /submit_vote` to add a single vote
  up to the cap.
* **Vote locking** (`POST /lock_votes`, host only): sets
  `Game.votes_locked = true`. Further `submit_vote` / `update_vote` calls
  are then rejected. Locking is **optional** — the host can advance
  `VOTE → REVEAL_VOTES` via `POST /next_phase` at any time, with or
  without locking first.
* A player **cannot vote their own card** (server-enforced; own-card
  buttons are disabled and labelled `(yours)` in the frontend).
* A player **cannot vote the same card twice** even when 2 votes are
  allowed.
* The vote target must be a card number currently `on the table`.
* **Vote preview (UI state)**: the frontend intercepts card selection and
  shows a VOTE_PREVIEW confirmation screen before calling `update_vote`.
  Clicking "Change" returns to the grid; clicking "Confirm" submits.
* **Vote lock is cleared** on round reset (`NEXT_ROUND → SELECT_NARRATOR`).

---

## 3. Scoring system

### 3.1 How points are calculated

All scoring lives in the rules engine
(`backend/rules/<ruleset>.py` + `backend/rules/config/<ruleset>.json`)
and is invoked from `game_service.next_phase` when the transition lands
on `SCORE_BASE` or `SCORE_BONUS`.

Three rulesets are available; the game picks one at creation time
(`Game.ruleset`, default `"standard"`).

| Ruleset     | Base (all/none correct)          | Base (some correct)                | Bonus per vote received |
|-------------|----------------------------------|------------------------------------|-------------------------|
| `standard`  | narrator 0, others +2            | narrator +3, correct +3, others 0  | +1                      |
| `high_risk` | narrator **-2**, others +3       | narrator +5, correct +5, others 0  | +2                      |
| `casual`    | narrator +1, others +2           | narrator +2, correct +2, others 0  | +1                      |

Base scoring happens on entering `SCORE_BASE`; bonus scoring on entering
`SCORE_BONUS`. Idempotency flags (`score_base_applied`,
`score_bonus_applied`) prevent double application.

### 3.2 How points are displayed

* `SCORE_BASE` renders a **base-points panel**: a list of
  `nickname +N` rows sorted by delta descending. Players with a zero
  delta are shown dimmed. (`frontend/app.js:renderScoring("base")`)
* `SCORE_BONUS` renders the same layout as a **bonus-points panel**.
  (`frontend/app.js:renderScoring("bonus")`)
* Both panels show only numbers — no explanation of the scoring rules.
* `LEADERBOARD` renders sorted cumulative totals
  (`frontend/app.js:renderLeaderboard`).
* `REVEAL_NARRATOR` and `NEXT_ROUND` render a generic "Host advances
  when ready" panel.
* The host sees a Continue button (enabled when `next_phase` is in
  `available_actions`). Non-hosts see the panel without the button.
* Per-round deltas are stored on the game as `Game.last_base_delta`
  and `Game.last_bonus_delta` (dicts of `player_id → points`),
  populated by the rules engine and cleared on round reset.

---

## 4. Other implemented mechanics

Covered in detail in `APP_STATE.md`. In brief:

* **Host** = first player to join (`Game.host_id` never changes).
* **Minimum 3 players** to start.
* **Card numbers** are integers in `[1, 84]` (`MIN/MAX_CARD_NUMBER`).
  The frontend reads the range from `Game.card_range` in every
  broadcast; nothing is hardcoded client-side.
* **Unique cards per round**: if two players submit the same number the
  round is invalidated (every `card_played` and `cards_on_table` is
  cleared, phase stays `PLAY_CARDS`) and the room is notified with a
  `game_error`/`duplicate_cards` event.
* **Reconnect**: a `recovery_token` is returned only by `/join_game`,
  stored in `localStorage`, and replayed on every WebSocket open. A
  successful reconnect restores score, `card_played`, and `votes`
  byte-for-byte.
* **Heartbeat**: client pings every ~15 s. A background task flips
  silent players (>45 s) to `connected = false` and broadcasts
  `player_disconnected`. Players are **never** removed from the game.
* **Stall policy**: the game never aborts on disconnects. Disconnected
  non-narrators do not block progress (gates use the active players
  list). The narrator, however, must have a card on the table for
  scoring to run.

---

## 5. Known limitations and gaps

### 5.1 Gameplay limitations

* **Host disconnect stalls the game.** No host migration / handoff.

### 5.2 UX gaps

* `REVEAL_VOTES`, `REVEAL_NARRATOR`, `SCORE_BASE`, `SCORE_BONUS`, and
  `NEXT_ROUND` all show the same waiting/leaderboard panel. The UI does
  not visually distinguish what the table is currently doing.
* No card thumbnails / images — players only see numbers. This is
  intentional (the physical deck is on the table), but new players can
  find it dry.
* No narrator hint on the lobby screen after SELECT_NARRATOR; the
  chosen narrator is shown but there is no confirmation that _you_ are
  the narrator until PLAY_CARDS.
* No explicit "connection lost" banner; when the socket drops the
  client silently retries every 2 s.
* No rule-picker UI. `POST /create_game {ruleset}` works, but the
  frontend only creates games with the default ruleset.

### 5.3 Engineering gaps

* No automated test suite in the repository (correctness is verified
  via a smoke script referenced in `APP_STATE.md` and by manual play).
* No rate limiting, no audit log.
* `game_id` collision is statistically improbable but not guarded;
  a collision would silently overwrite.
* CORS is wide open (`*`) — fine for LAN, not suitable as-is for public
  deployment.

---

## 6. Where to look in the code

| Concern                         | File                                             |
|---------------------------------|--------------------------------------------------|
| Phase enum                      | `backend/models/game_phase.py`                   |
| Phase transitions               | `backend/services/state_machine.py`              |
| All mutations / game logic      | `backend/services/game_service.py`               |
| Heartbeat / disconnect task     | `backend/services/heartbeat.py`                  |
| REST endpoints                  | `backend/routes/game.py`                         |
| WebSocket endpoint & broadcast  | `backend/routes/websocket.py`                    |
| Scoring                         | `backend/rules/standard_dixit.py` (+ siblings)   |
| Scoring values                  | `backend/rules/config/*.json`                    |
| Frontend rendering              | `frontend/app.js` (`render*` functions)          |
| Frontend shell / styles         | `frontend/index.html`                            |
