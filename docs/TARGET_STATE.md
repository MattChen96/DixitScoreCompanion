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
UI; per-round `last_base_delta` / `last_bonus_delta` with progressive
`+Δ` panels in `SCORE_BASE` / `SCORE_BONUS`; narrator must `confirm_narrator`
before `SELECT_NARRATOR → PLAY_CARDS`. For authoritative behaviour use
`CURRENT_STATE.md` and `API_SPECS.md`.

**Optional future polish** (not required to match the spec below): a dedicated
`Game.scoring_step` field — the client currently uses `phase` and `last_*_delta`
instead.

---

## 1. Game flow (target)

```
LOBBY
  → SELECT_NARRATOR
  → PLAY_CARDS
  → VOTE
  → VOTE_PREVIEW        (UI state only)
  → REVEAL_VOTES
  → REVEAL_NARRATOR
  → SCORING             (progressive: base → bonus)
  → LEADERBOARD
  → NEXT_ROUND
  → SELECT_NARRATOR     (loop)
```

Notes:

* **VOTE_PREVIEW** is a *client-side UI state*, not a server phase. It
  lives entirely between "player picks a card" and "player confirms the
  vote". The server phase stays `VOTE` throughout.
* **SCORING (progressive)** replaces the current two distinct phases
  `SCORE_BASE` + `SCORE_BONUS` **from the UI's point of view**. On the
  server they remain two separate steps (base, then bonus) so that
  idempotency and the rules-engine contract do not change. The host
  advances once to reveal base points and once more to reveal bonus
  points.

See `GAME_FLOW.md` for the full phase/transition/actor matrix.

---

## 2. Voting system (target)

* Each non-narrator player can cast **1 or 2 votes** per round. The
  server is the authority: `Player.votes` is a list of card numbers;
  `Game.votes_per_player` (1 or 2, set at `create_game`) caps its length.
* **Votes are editable** for the whole `VOTE` phase. A player may add,
  remove, or replace entries via `submit_vote` / `update_vote` until
  the **host** advances to `REVEAL_VOTES`. Votes become final on that
  transition — there is no separate host lock action in the current
  implementation. See `CURRENT_STATE.md` §2.
* A player **cannot vote their own card** (the "own card" slot is
  rendered but non-selectable).
* A player **cannot vote the same card twice** when 2 votes are
  allowed — the two votes must be distinct card numbers.
* **Before confirmation**, the selected cards are shown in a preview
  (see §3).

---

## 3. Vote preview (UI state)

After a player picks one or two card numbers, the app enters the
**VOTE_PREVIEW** UI state (server phase still `VOTE`):

* The selected card(s) are shown in **large format** (thumbnails or
  number tiles, bigger than the vote-grid buttons).
* The UI supports displaying **one or two** selected cards side by side.
* Two actions are offered:
  * **Confirm** — typically commits via `POST /update_vote` (atomic list);
    `POST /submit_vote` can add one card from the grid. The player
    then sees the submitted-vote view until the host advances.
  * **Change** — returns to the vote grid without submitting.
* While the phase is still `VOTE`, the player can re-enter the preview
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
* `SCORE_BASE` and `SCORE_BONUS` remain two separate phases on the
  server. Idempotency guards (`score_base_applied`,
  `score_bonus_applied`) are preserved.

### 5.2 UI representation

From the client's point of view, scoring is **progressive** and
displays **only the deltas**:

* After entering `SCORE_BASE` (host advance), the app renders a
  **base-points panel**: a list of `nickname +Δ` entries (e.g.
  `"Player A +3"`, `"Player B +0"`). Players with no change may be
  omitted or dimmed.
* After entering `SCORE_BONUS` (next host advance), the app renders a
  **bonus-points panel** in the same format.
* The UI uses the **phase** (`SCORE_BASE` vs `SCORE_BONUS`) plus
  `last_base_delta` / `last_bonus_delta` (no `scoring_step` field on
  `Game` today).
* Both panels show **only numbers**. No explanation of the scoring
  rules is shown in the UI (no "because all/none guessed", no "+1 per
  vote received"). The help, if any, lives outside the gameplay
  screens.
* The cumulative total is revealed on `LEADERBOARD`, which is a
  separate phase and a separate screen.

### 5.3 Required wire data

**Implemented:** `last_base_delta` and `last_bonus_delta` on `Game`,
populated by the rules engine and cleared on round reset. The
`scores_updated` WebSocket event carries the updated `game` blob.

**Optional later:** a `scoring_step` field or `scoring_events` stream
if a future client needs an extra discriminator beyond `phase` — not
present in the current model.

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
