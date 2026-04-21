# PROJECT RULES

## General Principles

* Keep the application SIMPLE
* Avoid over-engineering
* No unnecessary abstractions
* No database (in-memory only)

---

## Architecture

* Backend: FastAPI (Python)
* Real-time: WebSocket
* Frontend: simple HTML + JS (mobile-first)

---

## Code Style

* Clear and readable over clever
* Small functions
* Explicit naming

---

## Game Logic

* Server is the source of truth
* Never trust client input
* All validation happens in backend

---

## State Management

* Use a single in-memory game store (dictionary)
* Each game has a unique ID
* No persistence required

---

## DO NOT

* Add authentication
* Add external services
* Add complex UI frameworks
* Add database

---

## PRIORITY ORDER

1. Game flow works
2. Real-time sync works
3. UI works
4. Then improve UX
