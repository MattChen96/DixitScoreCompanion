# SYSTEM OVERVIEW

High-level description of the Dixit Score Companion.
Read this first. For implementation details see `CURRENT_STATE.md`,
for the planned evolution see `TARGET_STATE.md`.

---

## 1. What the app does

Dixit Score Companion is a **mobile-first web app** used **around a physical
Dixit table**. It replaces pen-and-paper scorekeeping with a shared,
real-time screen on each player's phone:

* players join a game room from their browser (no install);
* the host drives the round forward (narrator choice, reveals, scoring);
* scores are computed by the server and broadcast to everyone.

The physical cards, storytelling, and voting-with-tokens still happen on
the table. The app exists to **track state and score**, not to replace
the game.

---

## 2. Core concept

The flow is **Kahoot-like** but silent: the host has a control screen, the
players have a minimal action screen (play a card number, then vote for a
card number), and the server pushes the current phase to every device.

Game rules follow classic **Dixit** scoring by default, but the rules
engine is pluggable (`standard`, `high_risk`, `casual`) and selected at
game creation.

---

## 3. Key principles

* **No login.** A player only provides a nickname. Identity is a
  server-issued `player_id` + `recovery_token` stored in `localStorage`.
* **Multiplayer, session-based.** Every game is an isolated in-memory
  room identified by a short `game_id`. No cross-game data.
* **Host-controlled phases.** The game never advances automatically on
  a timer. The host (first player to join) clicks to move the round
  forward. Players only act inside the phases that ask them to.
* **Backend is the single source of truth.** The server owns phase,
  scores, votes, and the list of legal actions. The frontend renders
  whatever the server sends and never computes rules locally.
* **No persistence.** Process restart wipes everything. Acceptable for
  a companion tool played in one sitting.
* **No authentication / no database / no external services.**

---

## 4. Main components

### 4.1 Frontend

* Vanilla HTML + CSS + JavaScript (no framework).
* One-page app (`frontend/index.html` + `frontend/app.js`).
* Mobile-first, dark theme, one primary action per screen.
* Pure rendering: reads the current `game` object from the server and
  draws the screen matching the current phase. Phase transitions, card
  bounds, and "can I click this button now" come from server-provided
  `available_actions` and `card_range` fields.

### 4.2 Backend

* FastAPI (Python 3.12 in Docker; 3.9+ supported locally).
* Layered:
  * `models/` — Pydantic data classes (pure data).
  * `services/` — game logic (state machine, mutations, heartbeat).
  * `rules/` — pluggable scoring engines + JSON configs.
  * `routes/` — REST + WebSocket transports.
  * `store.py` — single in-memory `dict[str, Game]`.
* All mutation goes through `services/game_service.py`. Routes never
  mutate state directly.

### 4.3 Real-time communication

* REST for actions that the client initiates
  (`/create_game`, `/join_game`, `/start_game`, `/next_phase`,
  `/select_narrator`, `/submit_card`, `/submit_vote`).
* WebSocket (`/ws/{game_id}`) for push:
  * Every state-changing REST call triggers a broadcast of the full
    sanitized game object to every socket in the room.
  * Heartbeat: the client pings every ~15 s; a background task flips
    silent players to `connected = false` after ~45 s.
  * Reconnect: the client replays its stored `recovery_token` on socket
    open and resumes exactly where it left off (phase, score,
    `card_played`, vote).

The full protocol is documented in `API_SPECS.md`.

---

## 5. Deployment model

* Single Docker container, `uvicorn backend.main:app` on port 8000.
* Intended home: a NAS or laptop on the same Wi-Fi as the players.
* CORS is wide open (`*`) because the app is meant for LAN use.

---

## 6. What this document is **not**

* Not an implementation guide — see `CURRENT_STATE.md`.
* Not a specification of planned features — see `TARGET_STATE.md`.
* Not a rules-of-Dixit reference — see `GAME_FLOW.md` for scoring.
