# DATA MODEL

## Player

* id: string
* nickname: string
* score: int
* card_played: int | null
  * During PLAY_CARDS, `card_played` MUST be unique across all players in
    the same game. A duplicate submission triggers a round reset
    (see GAME_FLOW.md).
* vote: int | null
* recovery_token: string
  * Persistent client identifier used for reconnect. Returned to the
    joining client in the `/join_game` response and stored in
    `localStorage`. **Never** appears in broadcasts, in any other REST
    response, or in the `game` field of `/join_game`; the wire projection
    strips it from every outbound payload.
* connected: bool
  * Runtime liveness flag. Set to `true` on join, on every client action,
    and on every ping. The heartbeat task flips it to `false` when
    `now - last_seen > HEARTBEAT_TIMEOUT_S`. Players are never removed
    from the game.
* last_seen: float
  * Unix timestamp (seconds). Updated by any client activity.

---

## Game

* id: string
* players: list
* narrator_id: string | null
* phase: string
* cards_on_table: list[int]
* ruleset: string (default = "standard")
  * Selects which rules implementation (e.g. `StandardDixitRules`) to use
    for scoring and move validation. Set at game creation, immutable after.

---

## Storage

* In-memory dictionary:
  games = { game_id: Game }
