# Prompt di Implementazione - Fix Nickname Host e Card Range

## Contesto Progetto

Dixit Score Companion è un'app fullstack per gestire partite di Dixit con scoring automatico.

### Stack Tecnico
- **Backend**: Python 3.x + FastAPI
- **Frontend**: Vanilla JavaScript (HTML/CSS/JS)
- **Stato**: REST API + WebSocket per broadcast real-time
- **Dati**: In-memory store (backend/store.py)

### Architettura Pertinente
- `backend/models/game.py`: Modello dati Game e Player
- `backend/services/game_service.py`: Logica di business (create_game, join_game, ecc)
- `backend/routes/game.py`: Endpoint REST
- `backend/routes/websocket.py`: Broadcast WebSocket (game_wire function)
- `frontend/app.js`: UI e logica di gioco

---

## Fix 1: Creatore della Lobby come Host Automatico

### Problema Attuale
1. `/create_game` crea un gioco vuoto (senza giocatori)
2. Quando il primo giocatore chiama `/join_game`, diventa automaticamente host
3. **Issue**: Il creatore della lobby potrebbe non essere il primo a joinare, quindi non diventa host

### Requisito
1. **Prima di creare il gioco**, chiedere al creatore il suo nickname
2. Dopo aver creato il gioco, **aggiungere automaticamente il creatore come primo giocatore**
3. Il creatore diventa **host della partita**
4. Tornare al client il game_id, QR code E il player_id (come con `/join_game`)

### Implementazione Richiesta

#### Backend (`backend/services/game_service.py` + `backend/routes/game.py`)

1. **Modificare `CreateGameRequest`** in `backend/routes/game.py`:
   ```python
   class CreateGameRequest(_Body):
       nickname: NicknameField  # Nuovo: nickname del creatore
       ruleset: str = Field(default="standard", min_length=1, max_length=32)
       votes_per_player: int = Field(default=1, ge=1, le=2)
   ```

2. **Creare una nuova funzione in `backend/services/game_service.py`**:
   ```python
   def create_game_with_host(nickname: str, ruleset: str = "standard", votes_per_player: int = 1) -> tuple[Game, Player]:
       """Create a new game and add the creator as the first player (host)."""
       # Genera il game_id
       # Crea il Game
       # Crea il Player con il nickname fornito
       # Assegna il Player come host_id
       # Aggiunge il Player alla lista dei giocatori
       # Genera e assegna il QR code
       # Salva il gioco nello store
       # Ritorna (game, player)
   ```

3. **Modificare l'endpoint `/create_game`** in `backend/routes/game.py`:
   ```python
   @router.post("/create_game")
   def create_game(body: CreateGameRequest = CreateGameRequest()) -> dict[str, Any]:
       try:
           game, player = game_service.create_game_with_host(
               nickname=body.nickname,
               ruleset=body.ruleset,
               votes_per_player=body.votes_per_player
           )
       except ValueError as exc:
           raise _http_from_value_error(exc) from exc
       return {
           "game_id": game.id,
           "player_id": player.id,
           "recovery_token": player.recovery_token,  # Come in /join_game
           "qr_code": game.qr_code
       }
   ```

4. **Rivedere `join_game()`** in `backend/services/game_service.py`:
   - Il primo giocatore non sarà più quello che crea il game
   - Il check `if not game.players: game.host_id = player_id` rimane per sicurezza (caso edge)

#### Frontend (`frontend/app.js`)

1. **Modificare `renderJoin()`**: Aggiungere un pulsante/area per "Crea nuova partita"

2. **Modificare `renderLobbyCreated()`** o creare una nuova view:
   - Se l'utente ha creato un game e riceve nickname richiesto, mostrare il QR e il room code
   - **Importante**: Dopo aver creato il game, l'utente dovrebbe essere subito connesso (no bisogno di join!)

3. **Logica di creazione**:
   ```javascript
   // Nel gestore del bottone "Crea nuova partita"
   api("/create_game", { nickname: userNickname })
       .then(function(data) {
           state.gameId = data.game_id;
           state.playerId = data.player_id;
           state.recoveryToken = data.recovery_token;
           // Adesso l'utente è già dentro il gioco come host
           saveSession();
           connectWs();
           render();
       })
   ```

---

## Fix 2: Card Range Dinamico (da 1 a N, dove N = numero di giocatori)

### Problema Attuale
1. Nel frontend, `cardRange()` ritorna `{ min: 1, max: 84 }` (hardcoded)
2. Tutti i giocatori vedono sempre 84 numeri di carta disponibili
3. **Issue**: Nella fase di dichiarazione, dovrebbero poter dichiarare solo numeri da 1 a N (dove N = numero di giocatori)

### Requisito
1. I numeri di carta disponibili per la dichiarazione = **numero di giocatori nella partita**
2. Es: 5 giocatori → numerare le carte da 1 a 5
3. Il range deve essere **calcolato dinamicamente** quando cambia il numero di giocatori

### Implementazione Richiesta

#### Backend

1. **Aggiungere campo al modello `Game`** in `backend/models/game.py`:
   ```python
   class Game(BaseModel):
       # ... campi esistenti ...
       card_range: dict[str, int] = Field(default_factory=lambda: {"min": 1, "max": 84})
   ```

2. **Creare una funzione helper in `backend/services/game_service.py`**:
   ```python
   def _update_card_range(game: Game) -> None:
       """Calculate card_range based on current number of players."""
       num_players = len(game.players)
       game.card_range = {
           "min": 1,
           "max": max(num_players, 1)  # Almeno 1 per sicurezza
       }
   ```

3. **Aggiornare `card_range` nei seguenti punti**:
   - In `join_game()`: dopo aver aggiunto il player
   - In `create_game_with_host()`: dopo aver aggiunto il player host
   - In `_reset_round_after_next()`: (opzionale, ma buona pratica)

   Esempio:
   ```python
   def join_game(game_id: str, nickname: str) -> tuple[Game, Player]:
       game = _require_game(game_id)
       # ... validazioni ...
       player_id = uuid.uuid4().hex[:8]
       player = Player(id=player_id, nickname=nickname)
       if not game.players:
           game.host_id = player_id
       game.players.append(player)
       _update_card_range(game)  # ← Aggiungi questa riga
       store.set_game(game_id, game)  # Assicurati che sia salvato
       return game, player
   ```

4. **Assicurarsi che `card_range` sia incluso in `game_wire()`** in `backend/routes/websocket.py`:
   - Se `game_wire()` usa `game.dict()` o `model_dump()`, `card_range` sarà automaticamente incluso
   - Se filtra manualmente i campi, assicurarsi di includere `card_range`

5. **Validazione in `submit_card()`** in `backend/services/game_service.py`:
   - Controllare che il `card_number` sia dentro il range della partita:
   ```python
   def submit_card(game_id: str, player_id: str, card_number: int) -> Game:
       game = _require_game(game_id)
       player = _require_player(game, player_id)
       # ... altre validazioni ...
       
       # Validare che il card_number sia nel range dinamico
       card_range = game.card_range
       if card_number < card_range["min"] or card_number > card_range["max"]:
           raise ValueError(
               f"card_number must be between {card_range['min']} and {card_range['max']} "
               f"for this game, got {card_number}."
           )
       # ... resto della logica ...
   ```

#### Frontend

Il frontend **non richiede modifiche** perché:
- `cardRange()` legge già da `state.game.card_range`
- Una volta che il backend invia il `card_range` nel broadcast, il frontend lo userà automaticamente
- Se `card_range` non è presente, fallback a `{ min: 1, max: 84 }`

---

## Dettagli di Integrazione

### WebSocket Broadcasting
- Quando il `card_range` cambia, il backend deve notificare tutti i client (già fatto tramite `notify_game_room()`)
- I client riceveranno il nuovo `card_range` e aggiorneranno la UI

### Ordine di Implementazione Consigliato
1. Fix 1 (parte backend): Modificare `create_game()` e aggiungere `create_game_with_host()`
2. Fix 1 (parte frontend): Aggiornare il flusso di creazione game
3. Fix 2 (parte backend): Aggiungere `card_range` al modello Game e alla logica
4. Fix 2 (validazione): Aggiornare `submit_card()` per validare il range
5. Test end-to-end

### Edge Cases da Considerare
- Giocatore che si disconnette durante LOBBY: il `card_range` deve aggiustarsi se necessario
- Game che inizia: il `card_range` è locked per quella partita
- Solo nel LOBBY i giocatori possono aggiungersi: assicurarsi che il range si aggiorni solo lì

---

## Testing Suggerito

1. **Fix 1**: 
   - Creare un game con nickname "Alice"
   - Verificare che "Alice" sia player nel game e host
   - Verificare che altri giocatori possono joinare

2. **Fix 2**:
   - 3 giocatori → range [1, 3]
   - 5 giocatori → range [1, 5]
   - Tentare di submitare card fuori range → errore
   - Tentare card dentro range → successo

---

## Annotazioni Importanti

- **recovery_token**: Deve essere ritornato SOLO a `/create_game` (al creatore) e a `/join_game`, mai nei broadcast
- **card_range**: Deve essere incluso in ogni broadcast game_state (tramite game_wire)
- **Phase Lock**: Il `card_range` è rilevante solo in LOBBY e TURN_SUBMISSION (declaration step)
