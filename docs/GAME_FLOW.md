# GAME FLOW

This document defines the game **state machine**: phases, transitions,
allowed actions per phase, and who controls each transition.

It also distinguishes between **game phases** (authoritative,
server-side) and **UI states** (client-side rendering concerns).

* For what actually runs today, see `CURRENT_STATE.md`.
* For the planned additions, see `TARGET_STATE.md`.

---

## 1. Game phases vs. UI states

| Kind        | Lives on      | Examples                          | Purpose                                   |
|-------------|---------------|-----------------------------------|-------------------------------------------|
| Game phase  | Server        | `LOBBY`, `TURN_SUBMISSION`, …     | Drives rules, validation, scoring         |
| Sub-step    | Server        | `declaration`, `voting`           | Sub-state within TURN_SUBMISSION          |
| UI state    | Client        | `VOTE_PREVIEW`                    | Formatting / interaction only             |

**Game phases** are a strict enum
(`backend/models/game_phase.py:GamePhase`), enforced by
`backend/services/state_machine.py:ALLOWED_TRANSITIONS`.

**Sub-steps** exist within `TURN_SUBMISSION` phase, tracked by
`Game.submission_step` (`declaration` or `voting`).

**UI states** are local to the frontend and never travel over the wire.
Entering or leaving a UI state must not mutate server state.

---

## 2. Phase enum (server)

```
LOBBY
SELECT_NARRATOR
TURN_SUBMISSION      (sub-steps: declaration → voting)
REVEAL_VOTES
REVEAL_NARRATOR
SCORE_BASE
SCORE_BONUS
LEADERBOARD
NEXT_ROUND
```

Every `Game` starts in `LOBBY` and loops from `NEXT_ROUND` back to
`SELECT_NARRATOR`.

---

## 3. Phases — actions, actors, transitions

| Phase             | Player actions                                   | Host actions                                   | Transition trigger                                  | → Next phase     |
|-------------------|--------------------------------------------------|------------------------------------------------|-----------------------------------------------------|------------------|
| `LOBBY`           | `POST /join_game` (any number)                   | `POST /start_game` (≥ 3 players required)      | `/start_game`                                       | `SELECT_NARRATOR`|
| `SELECT_NARRATOR` | Narrator calls `POST /confirm_narrator` once chosen | `POST /select_narrator {narrator_id}` then `POST /next_phase` (after narrator confirms) | `/next_phase` (validation: `Game.narrator_confirmed == true`) | `TURN_SUBMISSION` |
| `TURN_SUBMISSION` (declaration) | `POST /submit_card {card_number}` — each active player including the narrator picks a **unique** card number | — (no host action) | Auto-advance when all declared + validated | `TURN_SUBMISSION` (voting) |
| `TURN_SUBMISSION` (voting) | `POST /submit_vote {card_number}` or `POST /update_vote {card_numbers}` — non-narrator only; freely editable until host advances | `POST /next_phase` | `/next_phase` (validation: all active non-narrators have ≥1 vote) | `REVEAL_VOTES` |
| `REVEAL_VOTES`    | —                                                | `POST /next_phase`                             | `/next_phase`                                       | `REVEAL_NARRATOR`|
| `REVEAL_NARRATOR` | —                                                | `POST /next_phase`                             | `/next_phase` (server applies **base** scores via rules engine) | `SCORE_BASE`     |
| `SCORE_BASE`      | —                                                | `POST /next_phase`                             | `/next_phase` (server applies **bonus** scores via rules engine) | `SCORE_BONUS`    |
| `SCORE_BONUS`     | —                                                | `POST /next_phase`                             | `/next_phase`                                       | `LEADERBOARD`    |
| `LEADERBOARD`     | —                                                | `POST /next_phase`                             | `/next_phase`                                       | `NEXT_ROUND`     |
| `NEXT_ROUND`      | —                                                | `POST /next_phase`                             | `/next_phase` (round reset: see §7) | `SELECT_NARRATOR` |

Rules:

* **Only the host** may trigger phase transitions via `POST /next_phase`
  (and host-only endpoints `start_game`, `select_narrator`). The
  **designated narrator** must call `confirm_narrator` in
  `SELECT_NARRATOR` before the host can advance to `TURN_SUBMISSION`.
* **Players** may only call `submit_card` / `submit_vote` /
  `update_vote` / `confirm_narrator` during the corresponding phase
  and sub-step; every other call is rejected.
* `/next_phase` is the generic advance. `LOBBY → SELECT_NARRATOR` uses
  a dedicated endpoint (`/start_game`).
* **Declaration → Voting auto-advance**: When all active players have
  declared their cards and no duplicates exist, the server automatically
  changes `submission_step` from `declaration` to `voting`. The host
  does NOT need to trigger this transition.
* **Duplicate card handling**: If duplicate cards are detected after all
  players have declared, all declarations are reset and players must
  re-declare. The phase stays `TURN_SUBMISSION` with `submission_step =
  declaration`.

---

## 4. UI states

UI states are documented here so all clients render consistently, but
they have **no wire representation**.

### 4.1 `VOTE_PREVIEW` (client, during `TURN_SUBMISSION` voting step)

Entered when a player taps a card in the vote grid.

* Shows the selected card(s) in large format.
* Shows the player's declared card in a banner for clarity.
* Supports 1 or 2 selected cards (target).
* Actions:
  * **Confirm** → `POST /update_vote` (replaces the full vote list) →
    exits VOTE_PREVIEW to the submitted-vote view; `POST /submit_vote`
    can still append one card at a time from the grid without preview.
  * **Change** → exits VOTE_PREVIEW back to the vote grid with the
    current selection remembered.
* Does **not** change the server phase. If the host advances while a
  player is in preview, the client re-renders to the new phase on the
  next broadcast.

> **Implementation note (current):** `renderVotePreview` is implemented as
> a client-side UI state driven by the module-level `pendingVote` variable
> in `frontend/app.js`. "Confirm" calls `POST /update_vote` to atomically
> set the player's vote list. "Change" returns to the vote grid. Players
> can re-enter the preview from the waiting screen at any time to change
> their selection.

### 4.2 Phase-specific UI (current `frontend/app.js`)

| Server phase        | UI rendering (implemented) |
|---------------------|------------------------------|
| `REVEAL_VOTES`      | `renderRevealVotes` — per-player vote list; narrator’s card is not shown as a vote. |
| `REVEAL_NARRATOR`   | `renderHostContinue` — generic "host advances when ready" (physical table reveal). |
| `SCORE_BASE`        | `renderScoring("base")` — rows from `Game.last_base_delta`.  |
| `SCORE_BONUS`       | `renderScoring("bonus")` — rows from `Game.last_bonus_delta`.  |
| `LEADERBOARD`       | `renderLeaderboard` — sorted cumulative `Player.score` totals.  |
| `NEXT_ROUND`        | `renderHostContinue` — generic "host advances when ready".   |

The client does **not** use a `scoring_step` field on `Game` — it uses
`game.phase` (`SCORE_BASE` vs `SCORE_BONUS`) to pick the panel and the
relevant `last_*_delta` map.

---

## 5. Stall / disconnect policy

The game **never aborts** or reassigns roles because a player went
offline.

* A player is **active** while `Player.connected == true`. The
  heartbeat task (`backend/services/heartbeat.py`) flips `connected` to
  `false` after ~45 s of silence but **never removes** the player from
  `Game.players`.
* "Everyone has played" / "everyone has voted" gates use the **active**
  player list. A disconnected non-narrator does not block progress.
* The **narrator must have a card on the table** before scoring is
  allowed. If the narrator drops before playing, the round pauses
  until they reconnect and play. No auto-override.
* The **host** role never migrates. If the host drops, host-only
  actions stall until they reconnect.
* On reconnect, the player's previous state (phase position, score,
  `card_played`, `vote[s]`) is preserved exactly.

See `APP_STATE.md` for the WebSocket-level `reconnect`, `ping`,
`player_reconnected`, `player_disconnected`, and `pong` events.

---

## 6. Scoring rules (ruleset-dependent)

Scoring is encapsulated in the rules engine
(`backend/rules/*` + `backend/rules/config/*.json`) and varies by
ruleset. The ruleset is chosen at `POST /create_game` and is immutable
for the life of the game.

The **standard** (default) ruleset implements classic Dixit scoring:

* **Base** — on entering `SCORE_BASE`:
  * If **all** or **none** of the non-narrator players guessed the
    narrator's card: narrator gets **0**, every other player gets
    **+2**.
  * Otherwise: narrator gets **+3**, every correct guesser gets **+3**,
    others get **0**.
* **Bonus** — on entering `SCORE_BONUS`:
  * Every **non-narrator** player gets **+1 per vote received** on the
    card they played this round. The narrator is excluded from bonus
    scoring.

Other rulesets adjust these numbers (see
`backend/rules/config/high_risk.json`,
`backend/rules/config/casual.json`). The point-by-point tables are in
`CURRENT_STATE.md` §3.1.

---

## 7. Round reset

Triggered when `/next_phase` advances `NEXT_ROUND → SELECT_NARRATOR`.

Clears:

* every `Player.card_played`
* every `Player.votes`
* `Game.cards_on_table`
* `Game.score_base_applied` / `Game.score_bonus_applied`
* `Game.last_base_delta` / `Game.last_bonus_delta`
* `Game.narrator_id` — narrator is re-selected each round
* `Game.narrator_confirmed` — confirmation resets with each new narrator
* `Game.submission_step` — reset to `None` (will be set to `declaration` on next TURN_SUBMISSION entry)

Preserves:

* `Player.score` — cumulative across rounds.
* `Game.players`, `Game.host_id` — immutable for the game's lifetime.
* `Game.ruleset` — immutable.
