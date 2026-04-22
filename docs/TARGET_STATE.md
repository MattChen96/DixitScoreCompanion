# TARGET STATE (TO-BE)

The desired final behaviour of the app. This document is **normative for
planning** — a developer or AI tool should be able to implement the
features below without guessing. For what actually runs today see
`CURRENT_STATE.md`.

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
  exact number is determined by game configuration (player count and/or
  ruleset). The server is the authority: `Player.votes` is a list of
  card numbers; `Game.votes_per_player` (or an equivalent rule-driven
  value) caps its length.
* **Votes are editable** until the host locks them. A player may add,
  remove, or replace entries in `Player.votes` freely while
  `Game.votes_locked == false`.
* The **host controls vote locking** via a dedicated action
  (`lock_votes`). Once locked:
  * `Game.votes_locked = true`
  * further `submit_vote` / `update_vote` calls are rejected
  * the host can advance `VOTE → REVEAL_VOTES`
  * the lock is cleared during round reset (`NEXT_ROUND → SELECT_NARRATOR`)
* A player **cannot vote their own card** (already enforced today and
  kept as-is — the "own card" slot is rendered but non-selectable).
* A player **cannot vote the same card twice** even when 2 votes are
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
  * **Confirm** — commits the selection by calling
    `POST /submit_vote` (first confirmation) or `POST /update_vote`
    (subsequent edits) and returns the player to the waiting screen.
  * **Change** — returns to the vote grid without submitting.
* As long as votes are not locked, the player can re-enter the preview
  from the waiting screen to change their selection.

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
* The `scoring_step` field on `Game` (`"base"` | `"bonus"` | `null`)
  tells the UI which panel to draw. The server sets this alongside the
  phase transition.
* Both panels show **only numbers**. No explanation of the scoring
  rules is shown in the UI (no "because all/none guessed", no "+1 per
  vote received"). The help, if any, lives outside the gameplay
  screens.
* The cumulative total is revealed on `LEADERBOARD`, which is a
  separate phase and a separate screen.

### 5.3 Required wire data

To compute the delta UI the server must expose, alongside the full game
state:

* `Game.scoring_step: "base" | "bonus" | null`
* per-player **delta** for the step that was just applied, either as:
  * a dedicated projection field (e.g. `last_base_delta`,
    `last_bonus_delta`), or
  * a `scoring_events` stream on the `scores_updated` WebSocket event.

Either approach is acceptable as long as the frontend stays
rule-agnostic.

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

## 8. Summary of new concepts introduced vs. current state

| Concept                 | Current        | Target                                     |
|-------------------------|----------------|--------------------------------------------|
| Votes per player        | 1 (fixed)      | 1 or 2 (rule-/config-driven, server cap)   |
| Vote mutability         | Immutable      | Editable until host locks                  |
| Vote lock               | Does not exist | Host action `lock_votes`; flag `Game.votes_locked` |
| Vote preview            | None           | Required UI state before `submit_vote` / `update_vote` |
| Reveal votes            | No UI          | Shows who voted which card(s)              |
| Scoring UI              | Leaderboard totals | Progressive `+Δ` panels, base then bonus |
| Scoring step marker     | None           | `Game.scoring_step`                        |

All additions must be implemented **without** breaking the reconnect
flow, the heartbeat / stall policy, or the rules-engine abstraction.
