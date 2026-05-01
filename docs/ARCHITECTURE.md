# ARCHITECTURE

## Overview

This is a mobile-first web application designed to be used during a physical board game (Dixit).

The app acts as a companion tool, not the main experience.

---

## High-Level Architecture

Client (mobile browser)
↓
FastAPI Backend (Python)
↓
In-memory game state

Communication:

* REST API (basic actions)
* WebSocket (real-time updates)

---

## Client (Frontend)

### Technology

* HTML
* CSS
* Vanilla JavaScript

### Requirements

* Mobile-first design
* Very simple UI
* One primary action per screen
* No complex navigation

### Responsibilities

* Render current game state
* Send user actions to backend
* Listen to WebSocket updates
* Update UI accordingly

---

## Backend

### Technology

* FastAPI (Python)

### Responsibilities

* Manage game state
* Validate all actions
* Enforce game rules via pluggable Rules Engine
* Control game phases
* Broadcast updates via WebSocket

### Important Rule

The backend is the SINGLE SOURCE OF TRUTH.

---

## Rules Engine

Scoring and advanced rule logic is abstracted into pluggable **RulesEngine**
implementations. Each game selects its ruleset at creation time:

* `standard` — classic Dixit scoring (default)
* `high_risk` — higher rewards and penalties
* `casual` — forgiving, beginner-friendly scoring

The Rules Engine:

* Owns scoring logic (`calculate_scores`)
* Is stateless (no per-game mutation)
* Is cached per ruleset name (efficient re-use)
* Is loaded via `rules_loader.load_rules(name)`

Games can be extended in the future with additional rulesets or house-rule
variants without touching the core game flow.

---

## Real-Time Communication

### WebSocket

Used for:

* Player join notifications
* Phase changes
* Votes and card submissions
* Score updates

### Strategy

* On every update, server sends full game state
* Clients do not compute logic locally

---

## Game State

Stored in memory:

```python
games = {
  game_id: Game
}
```

No database required.

---

## Session Model

* Each game = isolated session
* Players identified by player_id
* No authentication
* No persistence after server restart

---

## Deployment Model

* Single container app
* Runs on one server
* Accessible via browser

---

## Mobile Usage

* Players connect via phone browser
* No app installation required
* Ideally accessed via local network or simple URL
* Game creator can share a **QR code** — other players scan it to land on the join page with the room code pre-filled

## Deep-Link Entry Point

The backend serves `index.html` on both `/` and `/join/{game_id}`.
QR codes encode a URL with this path so that scanning on mobile opens the app directly in the join flow for the correct game.
The frontend detects the `/join/{game_id}` path at boot, pre-populates the room code, and cleans the URL with `history.replaceState`.

---

## Future NAS Deployment

* App will run in Docker container
* Accessible in local network
* No cloud dependency required

---

## Non-Goals

* No scalability concerns
* No microservices
* No distributed systems
* No user accounts

---

## Key Design Principle

Simplicity over everything.

The app must be:

* fast to open
* easy to understand
* invisible during gameplay
