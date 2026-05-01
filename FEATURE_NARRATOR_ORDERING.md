# Feature: Narrator Ordering Selection (Drag & Drop)

## Overview
Cambiare il modo di scegliere il narratore: anziché scegliere turno per turno durante il gioco, l'host selectiona l'ordine completo dei narratori **subito dopo l'avvio della partita**, tramite un'interfaccia drag-and-drop.

---

## Current Behavior (TO-BE REPLACED)

### Flusso attuale:
1. **LOBBY** → Giocatori si uniscono
2. **SELECT_NARRATOR** → Ogni turno:
   - Host calls `POST /select_narrator {narrator_id}`
   - Designated narrator calls `POST /confirm_narrator`
   - Host calls `POST /next_phase` per avanzare a TURN_SUBMISSION
3. **TURN_SUBMISSION** → Gioco continua
4. **[Fine turno]** → Torna a SELECT_NARRATOR per il turno successivo

### Limitazioni:
- Processo ripetitivo
- Richiede conferma del narratore ogni turno
- L'host non ha visibilità complessiva dell'ordine


---

## Target Behavior (NEW)

### Flusso nuovo:
1. **LOBBY** → Giocatori si uniscono (≥3 giocatori)
2. **Host clicks "Start Game"** → `POST /start_game`
3. **NEW PHASE: NARRATOR_ORDERING** → UI drag-and-drop:
   - Host vede lista di tutti i giocatori
   - Host trascina i giocatori per ordinarli (primo = narratore turno 1, secondo = narratore turno 2, etc.)
   - Ordine circolare: dopo l'ultimo narratore, ricomincia dal primo
   - Host clicca "Confirm" → `POST /set_narrator_queue {narrator_ids: [id1, id2, id3, ...]}`
4. **Fase auto-advance** → Server transisce da NARRATOR_ORDERING a TURN_SUBMISSION
   - Il primo giocatore nella queue diventa `narrator_id`
   - **NO confirmation needed** ← cambio cruciale
5. **Gioco procede normalmente** fino a fine turno
6. **NEXT_ROUND** → Server auto-advance a TURN_SUBMISSION, chiama `.pop(0)` + `.append()` sulla narrator_queue
   - Il narratore cambia automaticamente
   - **NO SELECT_NARRATOR phase anymore** ← la fase sparisce dal loop

### Vantaggi:
- ✅ Setup one-time, non ripetitivo
- ✅ L'host ha controllo/visibilità completa
- ✅ Niente confirmazioni ripetute
- ✅ UX più fluida


---

## Data Model Changes

### Game model (backend/models/game.py)

Aggiungi al dataclass `Game`:

```python
# [NEW] Queue of narrator player IDs, in order (circular)
# Empty until NARRATOR_ORDERING phase completes.
# After /set_narrator_queue, this is locked for the game.
narrator_queue: list[str] = Field(default_factory=list)

# [NEW] Index into narrator_queue for current round (0-based)
# Increments on each NEXT_ROUND → TURN_SUBMISSION transition
narrator_queue_index: int = 0

# [MODIFIED] Remove or deprecate:
# - narrator_confirmed: bool  (already exists, but will be unused)
#   Can leave in model for backward compat, but never set/check after this change.
```

### Round Reset Logic (backend/services/game_service.py)

When transitioning `NEXT_ROUND → TURN_SUBMISSION`:

```python
def next_round_to_turn_submission(game: Game):
    """
    Called on NEXT_ROUND → TURN_SUBMISSION transition.
    Auto-advance narrator from queue; no manual selection needed.
    """
    # Rotate narrator index
    if game.narrator_queue:
        game.narrator_queue_index = (game.narrator_queue_index + 1) % len(game.narrator_queue)
        next_narrator_id = game.narrator_queue[game.narrator_queue_index]
        game.narrator_id = next_narrator_id
    
    # Clear round-specific state
    game.cards_on_table = []
    game.last_base_delta = {}
    game.last_bonus_delta = {}
    game.score_base_applied = False
    game.score_bonus_applied = False
    for player in game.players:
        player.card_played = None
        player.votes = []
    
    # No need to reset narrator_confirmed anymore
```


---

## Game Flow Changes

### Updated Phase Enum

**backend/models/game_phase.py**

```python
class GamePhase(str, Enum):
    LOBBY = "LOBBY"
    NARRATOR_ORDERING = "NARRATOR_ORDERING"     # [NEW]
    TURN_SUBMISSION = "TURN_SUBMISSION"
    REVEAL_VOTES = "REVEAL_VOTES"
    REVEAL_NARRATOR = "REVEAL_NARRATOR"
    SCORING = "SCORING"
    LEADERBOARD = "LEADERBOARD"
    NEXT_ROUND = "NEXT_ROUND"
```

### State Machine (backend/services/state_machine.py)

Update `ALLOWED_TRANSITIONS`:

```python
ALLOWED_TRANSITIONS = {
    GamePhase.LOBBY: [GamePhase.NARRATOR_ORDERING],           # LOBBY → NARRATOR_ORDERING (via /start_game)
    GamePhase.NARRATOR_ORDERING: [GamePhase.TURN_SUBMISSION], # NARRATOR_ORDERING → TURN_SUBMISSION (auto after /set_narrator_queue)
    GamePhase.TURN_SUBMISSION: [GamePhase.REVEAL_VOTES],
    GamePhase.REVEAL_VOTES: [GamePhase.REVEAL_NARRATOR],
    GamePhase.REVEAL_NARRATOR: [GamePhase.SCORING],
    GamePhase.SCORING: [GamePhase.LEADERBOARD],
    GamePhase.LEADERBOARD: [GamePhase.NEXT_ROUND],
    GamePhase.NEXT_ROUND: [GamePhase.TURN_SUBMISSION],        # NEXT_ROUND → TURN_SUBMISSION (auto-advance, no SELECT_NARRATOR)
}
```

### Removed Phase
- **SELECT_NARRATOR** — deleted from the loop entirely ✗


---

## API Changes

### New Endpoint: POST /set_narrator_queue

**Controller:** Host only  
**Phase:** Must be in `NARRATOR_ORDERING`  
**Request body:**

```json
{
  "narrator_ids": ["player_id_1", "player_id_2", "player_id_3"]
}
```

**Validation:**
- `len(narrator_ids)` == `len(game.players)` (must include tutti i giocatori)
- Tutti gli IDs nel `narrator_ids` devono appartenere a giocatori attuali
- No duplicates in `narrator_ids`

**Response:**
- Success: 200 OK + updated `game` object (wireformat)
- Error: 400 Bad Request (if validation fails)

**Side-effects:**
- Set `game.narrator_queue = narrator_ids`
- Set `game.narrator_queue_index = 0`
- Set `game.narrator_id = narrator_ids[0]` (primo turno)
- Auto-transition `game.phase = NARRATOR_ORDERING → TURN_SUBMISSION`
- Broadcast updated `game` via WebSocket

### Modified Endpoint: POST /start_game

**New behavior:**
- Old: `LOBBY → SELECT_NARRATOR`
- **New: `LOBBY → NARRATOR_ORDERING`**

Change in `backend/routes/game.py:start_game`:

```python
@router.post("/start_game")
async def start_game(game_id: str):
    game = store.get_game(game_id)
    if len(game.players) < 3:
        raise HTTPException(status_code=400, detail="Need ≥3 players")
    
    # Validate host
    player = game_service.get_player_by_id(game, request_host_id)
    if player.id != game.host_id:
        raise HTTPException(status_code=403, detail="Only host can start")
    
    # OLD: game.phase = GamePhase.SELECT_NARRATOR
    # NEW:
    game.phase = GamePhase.NARRATOR_ORDERING
    
    store.set_game(game_id, game)
    broadcast_to_room(...)
    return game_wire(game)
```

### Removed Endpoint: POST /select_narrator

- **Delete entirely** ✗ (no longer needed)

### Removed Endpoint: POST /confirm_narrator

- **Delete entirely** ✗ (no longer needed)
- **BUT:** Can optionally keep it as a no-op for backward compatibility if clients are hardcoded to call it (will simply be ignored if not in SELECT_NARRATOR phase).


---

## Frontend Changes

### New UI Screen: Narrator Ordering

**When:** `game.phase === "NARRATOR_ORDERING"`

**Layout (mobile-first):**
```
┌─────────────────────────────────┐
│  Select Narrator Order          │
├─────────────────────────────────┤
│  Drag to reorder (Round 1→2→3)  │
│                                 │
│  ┌───────────────────────────┐  │
│  │ 🔘 Player A  (drag)       │  │  ← draggable item
│  ├───────────────────────────┤  │
│  │ 🔘 Player B  (drag)       │  │
│  ├───────────────────────────┤  │
│  │ 🔘 Player C  (drag)       │  │
│  └───────────────────────────┘  │
│                                 │
│           [✓ CONFIRM]           │
└─────────────────────────────────┘
```

**Interactions:**
- Each player is a draggable list item
- Host can reorder by drag-and-drop (touch or mouse)
- On mobile: use native HTML5 drag-drop + touch events (or a lightweight library like `Sortable.js` if needed)
- After ordering, click "CONFIRM" → `POST /set_narrator_queue`

**JavaScript logic in frontend/app.js:**

```javascript
// renderNarratorOrdering(game) — called when game.phase === "NARRATOR_ORDERING"
function renderNarratorOrdering(game) {
  const html = `
    <div class="narrator-ordering">
      <h2>Select Narrator Order</h2>
      <p>Drag to reorder (Round 1→2→3)</p>
      
      <ul id="narrator-list" class="draggable-list">
        ${game.players.map((player, idx) => `
          <li class="narrator-item" draggable="true" data-player-id="${player.id}">
            <span class="rank">${idx + 1}</span>
            <span class="name">${player.nickname}</span>
            <span class="drag-handle">☰</span>
          </li>
        `).join('')}
      </ul>
      
      ${game.host_id === state.playerId ? `
        <button class="btn-primary" onclick="confirmNarratorOrder()">
          Confirm Order
        </button>
      ` : `
        <p class="info">Waiting for host to set narrator order...</p>
      `}
    </div>
  `;
  
  renderPhaseContent(html);
  attachDragListeners();
}

function attachDragListeners() {
  const list = document.getElementById("narrator-list");
  let draggedItem = null;
  
  list.addEventListener("dragstart", (e) => {
    draggedItem = e.target.closest(".narrator-item");
    e.target.classList.add("dragging");
  });
  
  list.addEventListener("dragover", (e) => {
    e.preventDefault();
    const afterElement = getDragAfterElement(list, e.clientY);
    if (afterElement == null) {
      list.appendChild(draggedItem);
    } else {
      list.insertBefore(draggedItem, afterElement);
    }
  });
  
  list.addEventListener("dragend", (e) => {
    e.target.classList.remove("dragging");
  });
}

function getDragAfterElement(container, y) {
  const draggableElements = [...container.querySelectorAll(".narrator-item:not(.dragging)")];
  return draggableElements.reduce((closest, child) => {
    const box = child.getBoundingClientRect();
    const offset = y - box.top - box.height / 2;
    if (offset < 0 && offset > closest.offset) {
      return { offset, element: child };
    } else {
      return closest;
    }
  }, { offset: Number.NEGATIVE_INFINITY }).element;
}

async function confirmNarratorOrder() {
  const list = document.getElementById("narrator-list");
  const narrator_ids = Array.from(list.querySelectorAll(".narrator-item"))
    .map(el => el.dataset.playerId);
  
  const response = await fetch(`/set_narrator_queue?game_id=${state.gameId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ narrator_ids })
  });
  
  if (!response.ok) {
    showError("Failed to set narrator order");
    return;
  }
  
  // Server will broadcast updated game via WebSocket
  // and auto-transition to TURN_SUBMISSION
}
```

**CSS (frontend/app.js or style section):**

```css
.narrator-ordering {
  padding: 1rem;
}

.draggable-list {
  list-style: none;
  padding: 0;
  margin: 1rem 0;
}

.narrator-item {
  display: flex;
  align-items: center;
  padding: 1rem;
  margin: 0.5rem 0;
  background: #333;
  border-radius: 8px;
  cursor: move;
  user-select: none;
  border: 2px solid transparent;
  transition: all 0.2s;
}

.narrator-item:hover {
  background: #444;
}

.narrator-item.dragging {
  opacity: 0.5;
  border-color: #0da;
  transform: scale(0.98);
}

.rank {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 2.5rem;
  height: 2.5rem;
  background: #0da;
  color: #000;
  border-radius: 50%;
  font-weight: bold;
  margin-right: 1rem;
  font-size: 1rem;
}

.drag-handle {
  margin-left: auto;
  opacity: 0.5;
  font-size: 1.2rem;
}

.narrator-item:hover .drag-handle {
  opacity: 1;
}

.narrator-item .name {
  flex: 1;
  font-size: 1rem;
  font-weight: 500;
}
```

### Main App.js Render Logic

Update `render()` function to handle new phase:

```javascript
function render(game) {
  switch (game.phase) {
    case "LOBBY":
      renderLobby(game);
      break;
    
    case "NARRATOR_ORDERING":  // [NEW]
      renderNarratorOrdering(game);
      break;
    
    case "TURN_SUBMISSION":
      renderTurnSubmission(game);
      break;
    
    // ... rest of phases
  }
}
```

### Remove Old UI References

- **DELETE** any `renderSelectNarrator()` function
- **DELETE** any "Confirm Narrator" button / UI
- **DELETE** calls to `/confirm_narrator` (search for it in app.js)


---

## Backend Implementation Checklist

### 1. Data Model (backend/models/game.py)
- [ ] Add `narrator_queue: list[str]` field
- [ ] Add `narrator_queue_index: int` field
- [ ] Document both fields

### 2. Game Phase Enum (backend/models/game_phase.py)
- [ ] Add `NARRATOR_ORDERING = "NARRATOR_ORDERING"`
- [ ] Remove `SELECT_NARRATOR` (or keep for logging)

### 3. State Machine (backend/services/state_machine.py)
- [ ] Update `ALLOWED_TRANSITIONS` dict
  - `LOBBY → NARRATOR_ORDERING`
  - `NARRATOR_ORDERING → TURN_SUBMISSION`
  - `NEXT_ROUND → TURN_SUBMISSION` (not to SELECT_NARRATOR)
- [ ] Update any validation or phase-checking logic

### 4. Game Service (backend/services/game_service.py)
- [ ] Add `set_narrator_queue(game, narrator_ids)` method
  - Validate narrator_ids
  - Set `game.narrator_queue`
  - Set `game.narrator_queue_index = 0`
  - Set `game.narrator_id = narrator_ids[0]`
  - Transition phase to TURN_SUBMISSION
- [ ] Modify `next_round_to_turn_submission()` to rotate narrator from queue (instead of waiting for /select_narrator)
- [ ] Modify `available_actions()` to:
  - When phase is NARRATOR_ORDERING: only host can call `/set_narrator_queue`
  - Remove any logic checking `narrator_confirmed` (or deprecate it)

### 5. Routes (backend/routes/game.py)
- [ ] Modify `/start_game` endpoint to transition to `NARRATOR_ORDERING` instead of `SELECT_NARRATOR`
- [ ] Add new `/set_narrator_queue` endpoint
  - Validate request
  - Call `game_service.set_narrator_queue()`
  - Broadcast updated game
  - Return 200 + game
- [ ] Delete `/select_narrator` endpoint (or keep as deprecated no-op)
- [ ] Delete `/confirm_narrator` endpoint (or keep as deprecated no-op)

### 6. WebSocket (backend/routes/websocket.py)
- [ ] Ensure `game_wire()` still works with new fields
- [ ] No changes needed here (all fields are auto-serialized)

### 7. Testing
- [ ] Unit test: `set_narrator_queue()` validation
- [ ] Integration test: full flow LOBBY → NARRATOR_ORDERING → TURN_SUBMISSION → ... → NEXT_ROUND → TURN_SUBMISSION
- [ ] Edge case: narrator_queue rotation after last player cycles back to first


---

## Frontend Implementation Checklist

### 1. App.js Updates
- [ ] Add `renderNarratorOrdering(game)` function
- [ ] Add drag-drop event listeners
- [ ] Add `confirmNarratorOrder()` function
- [ ] Update main `render()` switch statement
- [ ] Remove old `renderSelectNarrator()` function
- [ ] Remove calls to `/confirm_narrator`

### 2. CSS/Styling
- [ ] Add `.narrator-ordering` container styles
- [ ] Add `.draggable-list` and `.narrator-item` styles
- [ ] Add drag-over / drag-active visual feedback
- [ ] Ensure mobile-friendly (44px+ touch targets, no horizontal scroll)

### 3. Testing
- [ ] Manual: reorder players via drag-drop on mobile and desktop
- [ ] Manual: confirm order → should auto-advance to TURN_SUBMISSION with correct narrator
- [ ] Manual: complete a round, verify NEXT_ROUND → TURN_SUBMISSION rotates narrator correctly


---

## Migration / Rollout

1. **Backward Compatibility:**
   - Old endpoints (`/select_narrator`, `/confirm_narrator`) can be kept as no-ops
   - Old clients will fail on NARRATOR_ORDERING phase but can be updated independently

2. **Testing Flow:**
   - Smoke test: create game, start, set narrator order, play a round, verify rotation
   - Load test: ensure narrator_queue operations don't block

3. **Documentation Updates:**
   - Update `GAME_FLOW.md` to remove SELECT_NARRATOR
   - Update `API_SPECS.md` with new `/set_narrator_queue` endpoint
   - Update `CURRENT_STATE.md` with new flow


---

## Summary of Changes

| Component | Change | Impact |
|-----------|--------|--------|
| **Data Model** | Add `narrator_queue`, `narrator_queue_index` | Medium |
| **Game Phase** | Add NARRATOR_ORDERING, remove SELECT_NARRATOR | High |
| **State Machine** | Rewrite transitions (NARRATOR_ORDERING replaces SELECT_NARRATOR) | High |
| **API** | Add `/set_narrator_queue`, deprecate `/select_narrator` & `/confirm_narrator` | High |
| **Frontend UI** | New drag-drop screen, remove narrator confirmation | High |
| **Game Logic** | Auto-rotation on NEXT_ROUND, no manual confirmation | High |

**Complexity Level:** Medium-High (affects core game flow)  
**Estimated Effort:** 4-6 hours (backend + frontend + testing)


---

## Notes & Gotchas

1. **Circular Queue:** After the last narrator, the first one plays again. This is automatic via modulo arithmetic in `next_round_to_turn_submission()`.

2. **UI Feedback:** Ensure the frontend shows the current narrator's rank/position clearly during gameplay (e.g., "Round 1 — Player B is narrator").

3. **Validation:** Always validate that `narrator_queue` contains all active players and no duplicates.

4. **Broadcasting:** After `/set_narrator_queue`, the server broadcasts the new `game` object. The client receives it and auto-renders TURN_SUBMISSION.

5. **Host Disconnect:** If the host disconnects during NARRATOR_ORDERING, the ordering remains pending until the host reconnects (no auto-advance).

6. **Player Join After Start:** Once `/start_game` is called, no new players can join (standard rules). This is enforced elsewhere but noted here for clarity.
