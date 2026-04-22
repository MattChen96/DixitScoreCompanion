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
| Game phase  | Server        | `LOBBY`, `PLAY_CARDS`, `VOTE`, …  | Drives rules, validation, scoring         |
| UI state    | Client        | `VOTE_PREVIEW`                    | Formatting / interaction only             |

**Game phases** are a strict enum
(`backend/models/game_phase.py:GamePhase`), enforced by
`backend/services/state_machine.py:ALLOWED_TRANSITIONS`.

**UI states** are local to the frontend and never travel over the wire.
Entering or leaving a UI state must not mutate server state.

---

## 2. Phase enum (server)

```
LOBBY
SELECT_NARRATOR
PLAY_CARDS
VOTE
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
| `SELECT_NARRATOR` | —                                                | `POST /select_narrator {narrator_id}`          | `/select_narrator`                                  | `PLAY_CARDS`     |
| `PLAY_CARDS`      | `POST /submit_card {card_number}` — each active player including the narrator picks a **unique** card number | `POST /next_phase`                             | `/next_phase` (validation: all active players played, narrator has played, all card numbers unique) | `VOTE`           |
| `VOTE`            | `POST /submit_vote {card_number}` — non-narrator players only; narrator may not vote; must not be own card; must be a card currently on the table. **Target**: editable until host locks (`POST /update_vote`); **Current**: immutable. | **Target**: `POST /lock_votes` then `POST /next_phase`. **Current**: `POST /next_phase` directly. | `/next_phase` (validation: all active non-narrators have voted; target: `votes_locked == true`) | `REVEAL_VOTES`   |
| `REVEAL_VOTES`    | —                                                | `POST /next_phase`                             | `/next_phase`                                       | `REVEAL_NARRATOR`|
| `REVEAL_NARRATOR` | —                                                | `POST /next_phase`                             | `/next_phase` (server applies **base** scores via rules engine) | `SCORE_BASE`     |
| `SCORE_BASE`      | —                                                | `POST /next_phase`                             | `/next_phase` (server applies **bonus** scores via rules engine) | `SCORE_BONUS`    |
| `SCORE_BONUS`     | —                                                | `POST /next_phase`                             | `/next_phase`                                       | `LEADERBOARD`    |
| `LEADERBOARD`     | —                                                | `POST /next_phase`                             | `/next_phase`                                       | `NEXT_ROUND`     |
| `NEXT_ROUND`      | —                                                | `POST /next_phase`                             | `/next_phase` (server resets round data: `card_played`, `vote[s]`, `cards_on_table`, scoring flags, `votes_locked`) | `SELECT_NARRATOR` |

Rules:

* **Only the host** may trigger phase transitions (`host_id` is set to
  the first joiner and never changes).
* **Players** may only call `submit_card` / `submit_vote` (and the
  target-state `update_vote`) during the corresponding phase; every
  other call in a wrong phase is rejected.
* `/next_phase` is the generic advance. `LOBBY → SELECT_NARRATOR` and
  `SELECT_NARRATOR → PLAY_CARDS` are the two exceptions and use
  dedicated endpoints so the host's explicit inputs (start / narrator
  choice) are captured.

---

## 4. UI states

UI states are documented here so all clients render consistently, but
they have **no wire representation**.

### 4.1 `VOTE_PREVIEW` (client, during server phase `VOTE`)

Entered when a player taps a card in the vote grid.

* Shows the selected card(s) in large format.
* Supports 1 or 2 selected cards (target).
* Actions:
  * **Confirm** → `POST /submit_vote` (first) or `POST /update_vote`
    (subsequent edits, target only) → exits VOTE_PREVIEW back to the
    waiting screen.
  * **Change** → exits VOTE_PREVIEW back to the vote grid with the
    current selection remembered.
* Does **not** change the server phase. If `Game.votes_locked` becomes
  `true` while in this state, the preview becomes read-only until the
  host advances.

### 4.2 Waiting panels

Several server phases render the same UI shell in the current
implementation (`REVEAL_VOTES`, `REVEAL_NARRATOR`, `SCORE_BASE`,
`SCORE_BONUS`, `NEXT_ROUND`). In the target state, each becomes a
distinct rendering:

| Server phase      | UI rendering (target)                                |
|-------------------|------------------------------------------------------|
| `REVEAL_VOTES`    | Per-player vote list (who voted which card(s))       |
| `REVEAL_NARRATOR` | Highlight of the narrator's card                     |
| `SCORE_BASE`      | Progressive **base** deltas (`"Player A +3"` lines)  |
| `SCORE_BONUS`     | Progressive **bonus** deltas                         |
| `LEADERBOARD`     | Sorted cumulative totals                             |
| `NEXT_ROUND`      | Brief "next round starting" panel                    |

The `Game.scoring_step` field (`"base"` | `"bonus"` | `null`) is the
target-state signal that tells the client whether the delta panel
should render base or bonus information.

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
  * Every player (including the narrator) gets **+1 per vote received**
    on the card they played this round.

Other rulesets adjust these numbers (see
`backend/rules/config/high_risk.json`,
`backend/rules/config/casual.json`). The point-by-point tables are in
`CURRENT_STATE.md` §3.1.

---

## 7. Round reset

Triggered when `/next_phase` advances `NEXT_ROUND → SELECT_NARRATOR`.

Clears:

* every `Player.card_played`
* every `Player.vote` (current) / `Player.votes` (target)
* `Game.cards_on_table`
* `Game.score_base_applied` / `Game.score_bonus_applied`
* `Game.votes_locked` (target)
* `Game.scoring_step` (target)

Preserves:

* `Player.score` — cumulative across rounds.
* `Game.players`, `Game.host_id`, `Game.narrator_id` (the narrator
  rotates only via `/select_narrator` in the next phase).
* `Game.ruleset` — immutable.
