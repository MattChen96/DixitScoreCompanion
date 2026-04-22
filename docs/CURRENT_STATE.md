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

* **Exactly 1 vote per non-narrator player** (`Player.vote: int | None`).
  The narrator cannot vote.
* **Votes are NOT editable.** Once a player submits a vote, the server
  rejects further `submit_vote` calls with
  `"This player has already voted this round"` (`game_service.submit_vote`
  line 239–240).
* A player **cannot vote their own card**: the server rejects the
  submission, and the frontend renders the player's own card as a
  disabled button labelled `(yours)` (`frontend/app.js:renderVote`,
  CSS `.vote-grid button.own-card`).
* The vote target must be a card number currently `on the table`.
* The host moves from VOTE to REVEAL_VOTES via `POST /next_phase`. The
  transition is gated on **every active (connected) non-narrator having
  voted** — disconnected players do not block the transition.
* There is no "vote preview" step; clicking a card submits immediately.
* There is no "vote lock" concept on the server side.

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

* The frontend **does not** render a dedicated scoring animation or a
  per-round delta view.
* Phases `SCORE_BASE`, `SCORE_BONUS`, and `LEADERBOARD` all render the
  same screen: a sorted list of `{nickname, total score}`
  (`frontend/app.js:renderLeaderboard`, lines ~604–630). `REVEAL_VOTES`,
  `REVEAL_NARRATOR`, and `NEXT_ROUND` render a generic
  "Host advances when ready" panel.
* The host sees a Continue button enabled whenever the current phase
  accepts a host advance (driven by `state.game.available_actions`).
  Non-hosts see only the waiting/leaderboard screen.
* There are no point-increment animations, no "Player A +3" lines, no
  distinction in the UI between base and bonus. Only the new cumulative
  total is visible after each host advance.

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
  successful reconnect restores score, `card_played`, and `vote`
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

* **Single vote only** per player — Dixit natively supports one vote,
  but some house-rule variants with 6–7 players allow 2 votes; this is
  not supported today.
* **Votes are immutable** once submitted. A misclick forces the player
  to live with the wrong vote.
* **No vote preview / confirmation step**. Clicks are final.
* **No host-controlled vote locking.** The host only advances phases;
  there is no intermediate "votes are locked" state between submission
  and reveal.
* **No progressive scoring display.** `SCORE_BASE` and `SCORE_BONUS`
  both just render the leaderboard; there is no per-round
  `+N` breakdown.
* **No per-player vote history** visible in UI. During `REVEAL_VOTES`
  the server exposes `cards_on_table` and each player's `vote`, but the
  current frontend does not render "who voted what".
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
