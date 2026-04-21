# API SPECIFICATION

## REST Endpoints

POST /create_game
→ returns game_id

POST /join_game
→ input: game_id, nickname
→ returns player_id, game_id, recovery_token, game
  * `recovery_token` is a persistent client identifier (stored by the client
    in `localStorage`). It is ONLY returned here; every broadcast and every
    other REST response strips it. The client sends it back over the
    WebSocket `reconnect` event to resume a session after a tab refresh or
    brief network drop.

POST /start_game

POST /next_phase

POST /select_narrator

POST /submit_card

POST /submit_vote

---

## WebSocket Events

### Client → Server

* join_room
* submit_card
* submit_vote
* reconnect
  * Payload: `{event: "reconnect", data: {player_id, recovery_token}}`.
  * Sent automatically on every WS open when the client has a stored session.
  * On success the server sends a full `game_state` to the reconnecting
    socket and broadcasts `player_reconnected` to the room.
  * On failure (unknown game, unknown player, wrong token) the server
    replies `{event: "error", detail: "recovery_failed"}` and closes the
    socket with code `1008`.
* ping
  * Payload: `{event: "ping", data: {player_id}}`.
  * Client heartbeat, sent every ~15 seconds. The server updates
    `player.last_seen` and flips `player.connected = True`. Any other
    inbound event (`submit_card`, `submit_vote`, `reconnect`) has the same
    liveness effect.

---

### Server → Client

* player_joined
* phase_changed
* card_submitted
* vote_submitted
* scores_updated
* game_state
* game_error
  * Room-wide gameplay error. Payload: `{event: "game_error", error: <code>, message: <human text>, game: <Game>}`.
  * Current codes: `duplicate_cards` — two or more players selected the same card during PLAY_CARDS; the round has been reset and `game` reflects the post-reset state.
* player_reconnected
  * Payload: `{event: "player_reconnected", game: <Game>}`. Broadcast to
    the whole room right after a successful `reconnect`. Every client
    re-renders from `game` (e.g. to drop the "offline" label for that
    player).
* player_disconnected
  * Payload: `{event: "player_disconnected", game: <Game>}`. Broadcast by
    the heartbeat task when one or more players have been silent for
    longer than `HEARTBEAT_TIMEOUT_S` (45 s). `game.players[i].connected`
    is `false` for the affected players; nobody is removed from the game.
* pong
  * Payload: `{event: "pong"}`. Per-socket reply to `ping`. Carries no
    `game` payload and triggers no render; its only purpose is to confirm
    the socket is still live end-to-end.

Every server message that carries `game` sends the **sanitized** `Game`
object (see "recovery_token" below) — never the raw model.

---

## Rules

* Every action must validate current phase
* Reject invalid actions
* `recovery_token` is never included in any broadcast or in any REST
  response apart from `/join_game`.
