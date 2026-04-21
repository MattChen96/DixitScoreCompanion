# GAME FLOW (STATE MACHINE)

## Phases

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

---

## Rules

* Only host can change phase
* Players can only act in allowed phases
* Server enforces all transitions
* During PLAY_CARDS, every player must select a UNIQUE card number
  * If a duplicate is detected (or somehow present at PLAY_CARDS → VOTE),
    the round is INVALIDATED: every `card_played` and `cards_on_table` is
    cleared, the phase stays PLAY_CARDS, and a `game_error` event with
    `error: "duplicate_cards"` is broadcast to the room. Players then
    replay their cards.

---

## Phase Actions

### LOBBY

* Players join

### SELECT_NARRATOR

* Host selects narrator

### PLAY_CARDS

* Players submit card
* Card numbers must be unique across all players in the round
* On duplicate: round is reset (phase stays PLAY_CARDS); `game_error`
  (`duplicate_cards`) is broadcast

### VOTE

* Players vote

### REVEAL_VOTES

* Show votes

### REVEAL_NARRATOR

* Reveal narrator card

### SCORE_BASE

* Assign base points

### SCORE_BONUS

* Assign bonus points

### LEADERBOARD

* Show ranking

### NEXT_ROUND

* Reset round data

---

## Disconnect / Reconnect

The game never aborts, skips, or re-assigns a role because a player went
offline. The policy is **stall**:

* A player is considered **active** while `Player.connected == true`. The
  heartbeat task (`backend/services/heartbeat.py`) flips `connected` to
  `false` after ~45 s of silence but never removes the player from
  `Game.players`.
* "Everyone has played" / "everyone has voted" gates use the **active**
  player list — a player who drops before playing or voting does not
  block progress for the rest of the room.
* Exception: the **narrator must have a card on the table** before
  scoring is allowed. If the narrator drops before playing, the round
  simply pauses until they reconnect and play; no auto-abort, no
  host-driven override.
* The **host** role never migrates. If the host drops, `/next_phase` and
  host-only actions stall until they return (or a new game is started).
* On reconnect, the player's previous state (phase position, score,
  `card_played`, `vote`) is preserved byte-for-byte; no reset.

See `API_SPECS.md` for the concrete `reconnect`, `ping`,
`player_reconnected`, `player_disconnected`, and `pong` events.
