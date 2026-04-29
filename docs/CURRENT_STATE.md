# CURRENT STATE (AS-IS)

Exact description of how the app works **today**, as implemented in the
codebase. For deep implementation notes see `APP_STATE.md`; for planned
behaviour see `TARGET_STATE.md`.

---

## 1. Game phases currently implemented

Nine phases, in this strict order (loop at the end):

```
LOBBY
  → SELECT_NARRATOR
  → TURN_SUBMISSION (declaration → voting)
  → REVEAL_VOTES
  → REVEAL_NARRATOR
  → SCORING (base + bonus unified)
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
| SELECT_NARRATOR   | `POST /select_narrator` (host picks) → narrator calls `POST /confirm_narrator` → `POST /next_phase` (host advances) | Host + Narrator |
| TURN_SUBMISSION (declaration) | Players declare cards → **auto-advances** when all declared + validated | Automatic |
| TURN_SUBMISSION (voting) | `POST /next_phase`                      | Host       |
| REVEAL_VOTES      | `POST /next_phase`                           | Host       |
| REVEAL_NARRATOR   | `POST /next_phase` → triggers scoring        | Host       |
| SCORING           | `POST /next_phase` → applies base and bonus scoring, displays both in unified view | Host |
| LEADERBOARD       | `POST /next_phase`                           | Host       |
| NEXT_ROUND        | `POST /next_phase` → round reset             | Host       |

`/next_phase` is the generic advance for most phases. **Exception:**
`LOBBY → SELECT_NARRATOR` uses only `POST /start_game` (not `/next_phase`).
In `SELECT_NARRATOR`, the host calls `POST /select_narrator` to pick the
narrator (phase stays `SELECT_NARRATOR`), the narrator calls
`POST /confirm_narrator`, then the host advances `SELECT_NARRATOR →
TURN_SUBMISSION` with `POST /next_phase`.

The `TURN_SUBMISSION` phase has two sub-steps tracked by `Game.submission_step`:
- `declaration`: All players (including narrator) declare their card numbers
- `voting`: Non-narrator players vote for the narrator's card

The transition from declaration to voting is **automatic** when all players
have declared and no duplicate cards exist. If duplicates are detected,
declarations are reset and players must re-declare.

---

## 2. Turn submission system

The `TURN_SUBMISSION` phase combines card declaration and voting into a
single guided flow:

### 2.1 Declaration step (`submission_step == "declaration"`)

* All active players (including the narrator) declare which card they played
* Each player calls `POST /submit_card {card_number}`
* Card numbers must be unique across all players
* When all active players have declared:
  - If no duplicates: auto-advances to voting step
  - If duplicates exist: all declarations are reset, players must re-declare

### 2.2 Voting step (`submission_step == "voting"`)

* **1 or 2 votes per non-narrator player** (`Player.votes: list[int]`).
  The cap is `Game.votes_per_player` (1 or 2, set at game creation, default 1).
  The narrator cannot vote.
* **Votes are freely editable** throughout the voting step. A player may
  call `POST /update_vote` to replace their vote list, or
  `POST /submit_vote` to add a single vote up to the cap. There is no
  manual lock step — votes become final when the host advances the phase.
* **Phase transition gate**: the host can advance `TURN_SUBMISSION → REVEAL_VOTES`
  via `POST /next_phase` only once every active non-narrator player has
  cast at least one vote (and `submission_step == voting`). The backend
  enforces this via `available_actions` (which omits `next_phase` until the
  condition is met); the frontend reflects it by enabling/disabling the
  Continue button.
* A player **cannot vote their own card** (server-enforced; own-card
  buttons are disabled and labelled `(yours)` in the frontend).
* A player **cannot vote the same card twice** even when 2 votes are
  allowed.
* The vote target must be a card number currently `on the table`.
* **Vote preview (UI state)**: the frontend intercepts card selection and
  shows a VOTE_PREVIEW confirmation screen before calling `update_vote`.
  The player's declared card is shown in a banner to distinguish it from
  the vote selection. Clicking "Change" returns to the grid; clicking
  "Confirm" submits.

---

## 3. Scoring system

### 3.1 How points are calculated

All scoring lives in the rules engine
(`backend/rules/<ruleset>.py` + `backend/rules/config/<ruleset>.json`)
and is invoked from `game_service.next_phase` when the transition lands
on `SCORING`.

Three rulesets are available; the game picks one at creation time
(`Game.ruleset`, default `"standard"`).

| Ruleset     | Base (all/none correct)          | Base (some correct)                | Bonus per vote received (non-narrator only) |
|-------------|----------------------------------|------------------------------------|---------------------------------------------|
| `standard`  | narrator 0, others +2            | narrator +3, correct +3, others 0  | +1 per vote on their card                   |
| `high_risk` | narrator **-2**, others +3       | narrator +5, correct +5, others 0  | +2 per vote on their card                   |
| `casual`    | narrator +1, others +2           | narrator +2, correct +2, others 0  | +1 per vote on their card                   |

Both base and bonus scoring happen when entering the `SCORING` phase.
Idempotency flags (`score_base_applied`, `score_bonus_applied`) prevent
double application on reconnect or replay.

The narrator is **excluded from bonus scoring** — they do not receive
points for votes cast on their card. Only non-narrator players earn
bonus points.

### 3.2 How points are displayed

* `SCORING` renders a **unified scoring panel** showing both base and
  bonus points in visually separated sections:
  - **Base points section**: `nickname +N` rows sorted by delta descending
  - **Bonus points section**: `nickname +N` rows sorted by delta descending
  - Players with a zero delta are shown dimmed in each section
  - (`frontend/app.js:renderScoring()`)
* Both sections show only numbers — no explanation of the scoring rules.
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
* **Unique cards per round**: if two players submit the same number during
  the declaration step, all declarations are cleared and players must
  re-declare. The phase stays `TURN_SUBMISSION` with `submission_step =
  declaration`, and the room is notified with a `game_error`/`duplicate_cards`
  event.
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

* `REVEAL_VOTES`, `SCORE_BASE`, `SCORE_BONUS`, and `LEADERBOARD` each
  have their own screen (per-player vote list, base deltas, bonus deltas,
  cumulative leaderboard). `REVEAL_NARRATOR` and `NEXT_ROUND` still use a
  generic "host advances when ready" panel — the physical table handles
  the actual card reveal between those steps.
* No card thumbnails / images — players only see numbers. This is
  intentional (the physical deck is on the table), but new players can
  find it dry.
* The narrator confirmation UI is implemented but there is no visible
  countdown or progress indicator for other players while they wait.
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
