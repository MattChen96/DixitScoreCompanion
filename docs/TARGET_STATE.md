# TARGET STATE (TO-BE)

The desired final behaviour of the app. This document is **normative for
planning** — a developer or AI tool should be able to implement the
features below without guessing. For what actually runs today see
`CURRENT_STATE.md`.

### Status relative to the repository today

**Already implemented** (this document is partly historical; the code has
caught up in these areas): multi-vote with `Player.votes` and
`Game.votes_per_player` (1 or 2); VOTE_PREVIEW client UI; votes editable
until the host advances (no `votes_locked` / `lock_votes`); REVEAL_VOTES list
UI; per-round `last_base_delta` / `last_bonus_delta` with **unified scoring
screen** showing both `+Δ` base and bonus panels together in `SCORING` phase;
narrator must `confirm_narrator` before `SELECT_NARRATOR → TURN_SUBMISSION`;
**unified turn submission flow** with `TURN_SUBMISSION` phase replacing
separate `PLAY_CARDS` + `VOTE` phases, with `Game.submission_step` tracking
`declaration` → `voting` sub-steps; **automatic progression** from declaration
to voting when all cards validated; **wizard-like UI** with step indicator and
declared-card banner during voting.
For authoritative behaviour use `CURRENT_STATE.md` and `API_SPECS.md`.

**Optional future polish** (not required to match the spec below): a dedicated
`Game.scoring_step` field — the client currently uses `phase` and `last_*_delta`
instead.

---

## 1. Game flow (target)

```
LOBBY
  → SELECT_NARRATOR
  → TURN_SUBMISSION     (sub-steps: declaration → voting)
  → VOTE_PREVIEW        (UI state only, during voting sub-step)
  → REVEAL_VOTES
  → REVEAL_NARRATOR
  → SCORING             (unified: base + bonus together)
  → LEADERBOARD
  → NEXT_ROUND
  → SELECT_NARRATOR     (loop)
```

Notes:

* **TURN_SUBMISSION** is a unified phase with two server-tracked sub-steps:
  - `declaration`: players declare which card they played (visual picker)
  - `voting`: players vote for the narrator's card
  The sub-step auto-advances when all cards are declared and validated.
* **VOTE_PREVIEW** is a *client-side UI state*, not a server phase. It
  lives entirely between "player picks a card" and "player confirms the
  vote". The server phase stays `TURN_SUBMISSION` (voting step) throughout.
* **SCORING** is a unified phase that combines base and bonus scoring into
  one view. The host advances once from SCORING to LEADERBOARD. Both base
  and bonus points are displayed in visually separated sections, with
  optional progressive reveal in the UI.

---

## 2. Turn submission system (target)

The turn submission is a single guided flow with two steps in the same UI:

### 2.1 Declaration step
* Player declares which card they played
* Visual card-number picker (grid of 1-84)
* Once all players submitted: validate declared cards
* If duplicate card declarations exist: show error, repeat declaration step
* When validation succeeds: auto-advance to voting step (no host action)

### 2.2 Voting step
* Each non-narrator player can cast **1 or 2 votes** per round. The
  server is the authority: `Player.votes` is a list of card numbers;
  `Game.votes_per_player` (1 or 2, set at `create_game`) caps its length.
* **Votes are editable** for the whole voting step. A player may add,
  remove, or replace entries via `submit_vote` / `update_vote` until
  the **host** advances to `REVEAL_VOTES`. Votes become final on that
  transition — there is no separate host lock action.
* A player **cannot vote their own card** (the "own card" slot is
  rendered but non-selectable).
* A player **cannot vote the same card twice** when 2 votes are
  allowed — the two votes must be distinct card numbers.
* **Before confirmation**, the selected cards are shown in a preview
  along with the player's declared card for clarity.

---

## 3. Vote preview (UI state)

After a player picks one or two card numbers, the app enters the
**VOTE_PREVIEW** UI state (server phase stays `TURN_SUBMISSION`, voting step):

* The selected card(s) are shown in **large format** (thumbnails or
  number tiles, bigger than the vote-grid buttons).
* The player's **declared card** is shown in a banner to clearly
  distinguish "your played card" from "your vote".
* The UI supports displaying **one or two** selected cards side by side.
* Two actions are offered:
  * **Confirm** — commits via `POST /update_vote` (atomic list);
    `POST /submit_vote` can add one card from the grid. The player
    then sees the submitted-vote view (with persistent large vote
    preview and declared-card banner) until the host advances.
  * **Change** — returns to the vote grid without submitting.
* While the voting step is active, the player can re-enter the preview
  from the submitted screen to change their selection.

This state is entirely client-side; no server event is required to
enter or leave it.

---

## 4. Reveal votes (target)

The REVEAL_VOTES screen must visualise **who voted what**:

* For every player in the game, show the card(s) they voted.
* Must support multiple votes per player (render the list, not a single
  value).
* Must be consistent with `Player.votes: list[int]` from the data
  model.
* The narrator is displayed separately (they did not vote) and the
  narrator's card is highlighted, but the narrator identity is **not
  yet revealed as the narrator's card** until the next phase
  (`REVEAL_NARRATOR`). The "which card was the narrator's" reveal is a
  dedicated step.

---

## 5. Scoring system (target)

### 5.1 Server-side

* Scoring logic is **unchanged**. It lives in the rules engine
  (`backend/rules/*`) and is ruleset-dependent. See `GAME_FLOW.md`.
* `SCORING` is a single phase on the server. Both base and bonus scoring
  are applied when entering this phase. Idempotency guards
  (`score_base_applied`, `score_bonus_applied`) are preserved.

### 5.2 UI representation

From the client's point of view, scoring is **unified** and displays
**both base and bonus deltas together**:

* After entering `SCORING` (host advance from `REVEAL_NARRATOR`), the app
  renders a **unified scoring panel**: a list of player deltas for both
  base and bonus sections.
* The two sections are visually separated with section headers:
  - "Base points this round" showing base deltas
  - "Bonus points this round" showing bonus deltas
* Players are sorted by delta descending within each section.
* Players with no change may be omitted or dimmed.
* The UI uses the **phase** (`SCORING`) plus `last_base_delta` /
  `last_bonus_delta` (these fields are populated when entering SCORING).
* Both panels show **only numbers**. No explanation of the scoring rules
  is shown in the UI.
* The cumulative total is revealed on `LEADERBOARD`, which is a separate
  phase and a separate screen.

### 5.3 Required wire data

**Implemented:** `last_base_delta` and `last_bonus_delta` on `Game`,
populated by the rules engine when entering `SCORING` and cleared on
round reset. The `scores_updated` WebSocket event carries the updated
`game` blob.

---

## 6. UX principles (target)

* **Simple interface.** One primary action per screen. No sidebars,
  no multi-column layouts.
* **Minimal input per player.** Players only pick a card number, pick
  vote(s), and confirm. Everything else is host-driven.
* **Host-driven progression.** No timers, no automatic phase advance.
  The host always clicks to move on.
* **Mobile-first.** Designed for phones held vertically; readable at
  arm's length; tap targets ≥ 44 px.
* **App supports the game, does not replace it.** The physical cards,
  storytelling, and the act of voting with Dixit's wooden tokens stay
  on the table. The app just tracks numbers and state.
* **Companion mode for the host.** The host screen shows useful
  overviews (who has played / voted / is connected) without exposing
  secret information (nobody's vote is leaked to the host before
  `REVEAL_VOTES`).

---

## 7. Preserved constraints

These remain non-negotiable (see `PROJECT_RULES.md` and
`ARCHITECTURE.md`):

* No login / no authentication.
* No database / no persistence.
* No external services.
* Backend is the single source of truth; no client-side game logic.
* Mobile-first, vanilla HTML + JS frontend; no frameworks.
* Rules remain pluggable (`standard`, `high_risk`, `casual`, …).

---

## 8. Summary: planning document vs. repository (2026)

This table is retained for **history**; many rows are **done** in code.
See `CURRENT_STATE.md` for the live matrix.

| Concept                 | Notes (today) |
|-------------------------|---------------|
| Votes per player        | **Done** — `Game.votes_per_player` 1 or 2 |
| Vote mutability         | **Done** — editable until host advances from `VOTE` |
| Vote lock / `lock_votes` | **Not implemented** (superseded by host-advance finality) |
| Vote preview            | **Done** — client `VOTE_PREVIEW` |
| Reveal votes            | **Done** — per-player list in `REVEAL_VOTES` |
| Scoring UI              | **Done** — `+Δ` base/bonus from `last_*_delta` |
| Scoring step marker     | **Not in model** — use `phase` + deltas |

All additions must be implemented **without** breaking the reconnect
flow, the heartbeat / stall policy, or the rules-engine abstraction.
