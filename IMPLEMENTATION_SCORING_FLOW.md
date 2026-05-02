# Feature: Immediate Scoring & Next Round Flow

## Overview
Semplificare il flusso di gioco eliminando le schermate intermedie fra il reveal dei voti e l'inizio dei punti, e fra la leaderboard e il nuovo round. Attualmente il giocatore vede:
1. REVEAL_VOTES → REVEAL_NARRATOR (schermata intermedia) → SCORING
2. LEADERBOARD → [schermata intermedia] → NEXT_ROUND

Il nuovo flusso deve essere:
1. REVEAL_VOTES → SCORING (senza intermedia)
2. LEADERBOARD → NEXT_ROUND (senza intermedia)

---

## Current Behavior

### Flusso attuale:
```
TURN_SUBMISSION (voting step)
         ↓ [host clicca "Continue"]
    REVEAL_VOTES (mostra le votazioni di ogni giocatore)
         ↓ [host clicca "Continue"]
    REVEAL_NARRATOR (schermata intermedia, mostra la carta del narratore)
         ↓ [host clicca "Continue"]
    SCORING (mostra i punti guadagnati base + bonus)
         ↓ [host clicca "Continue"]
    LEADERBOARD (mostra la classifica aggiornata)
         ↓ [host clicca "Continue"]
    NEXT_ROUND (stato intermedio)
         ↓ [automatico dopo ~1-2 sec o con avanzamento host]
    TURN_SUBMISSION (nuovo round)
```

### Limitazioni:
- REVEAL_NARRATOR è una schermata che serve solo a mostrare la carta del narratore (già visibile nella REVEAL_VOTES)
- Fra LEADERBOARD e NEXT_ROUND c'è una schermata inutile
- Troppi click del host
- UX troppo frammentata

---

## Target Behavior

### Nuovo flusso:
```
TURN_SUBMISSION (voting step)
         ↓ [host clicca "Continue"]
    REVEAL_VOTES (mostra le votazioni)
         ↓ [automatico: server transisce direttamente a SCORING]
    SCORING (mostra i punti guadagnati base + bonus)
         ↓ [host clicca "Continue"]
    LEADERBOARD (mostra la classifica)
         ↓ [automatico: server transisce direttamente a NEXT_ROUND]
    TURN_SUBMISSION (nuovo round — host vede subito la schermata di dichiarazione)
```

### Requisiti:
- ✅ La transizione REVEAL_VOTES → SCORING è **automatica lato server** (non richiede un click del host)
- ✅ La transizione LEADERBOARD → NEXT_ROUND è **automatica lato server**
- ✅ Il frontend non mostra mai REVEAL_NARRATOR (fase rimane nel modello ma non viene mai renderizzata)
- ✅ L'action `next_phase` non è disponibile durante REVEAL_VOTES (perché auto-advance)
- ✅ L'action `next_phase` è disponibile durante LEADERBOARD (host può controllare il timing)

---

## Implementation Requirements

### Backend (backend/services/game_service.py)

#### 1. Transizione automatica REVEAL_VOTES → SCORING

In `next_phase()`, quando il server riceve `POST /next_phase` **dalla fase TURN_SUBMISSION (voting step)**:

**Current behavior:**
- TURN_SUBMISSION (voting) → REVEAL_VOTES

**New behavior:**
- TURN_SUBMISSION (voting) → REVEAL_VOTES → **[auto-advance to SCORING]**
- Nel `next_phase()`, dopo aver transito a REVEAL_VOTES, fare **immediatamente un secondo step** che transisce a SCORING
- Eseguire il calcolo dei punti (base + bonus) come se fosse stato chiamato manualmente
- Tornare lo stato SCORING al client (il frontend non vedrà mai REVEAL_VOTES sullo schermo)

**Alternative (più pulita):**
- Modificare la state machine in modo che `TURN_SUBMISSION (voting)` → `SCORING` **direttamente**
- Rimuovere REVEAL_VOTES dal flusso automatico, mantenerlo come fase "opzionale/debug" che l'host potrebbe visitare manualmente in futuro (ma non nel flusso principale)
- Decidere quale approccio sia più coerente con l'architettura

#### 2. Transizione automatica LEADERBOARD → NEXT_ROUND

In `next_phase()`, quando il server riceve `POST /next_phase` **dalla fase LEADERBOARD**:

**Current behavior:**
- LEADERBOARD → NEXT_ROUND

**New behavior:**
- LEADERBOARD → NEXT_ROUND → **[auto-advance to TURN_SUBMISSION]**
- Eseguire il reset del round (clear card_played, votes, deltas, etc.)
- Ruotare il narratore dalla narrator_queue
- Tornare lo stato TURN_SUBMISSION al client (il frontend passerà direttamente alla schermata di dichiarazione)

#### 3. Aggiornare available_actions()

- Durante REVEAL_VOTES: **non includere "next_phase"** (sarà automatico)
- Durante LEADERBOARD: **includere "next_phase"** (host mantiene il controllo del timing)

---

## Frontend (frontend/app.js)

### 1. Rimuovere/comentare renderizzazione di REVEAL_NARRATOR

Nel blocco `render()` che dispatcha le fasi:

```javascript
} else if (ph === "REVEAL_NARRATOR") {
  // Non renderizzare questa fase — il server la salterà
  renderWaiting("This phase is not shown.");
}
```

Alternativa: rimuovere completamente il check per REVEAL_NARRATOR, così se arriva (per bug o vecchi client) fallisce in modo visibile.

### 2. Frontend riceve già il flusso corretto

Se il backend implementa correttamente:
- `next_phase` da TURN_SUBMISSION (voting) ritornerà `{game: {phase: "SCORING", ...}}`
- `next_phase` da LEADERBOARD ritornerà `{game: {phase: "TURN_SUBMISSION", ...}}`

Il frontend non ha bisogno di logica speciale — semplicemente renderizzerà la fase ricevuta dal server.

---

## Data Model

**No changes required** al modello Game — REVEAL_NARRATOR rimane come fase enumerata (per retrocompatibilità), ma non viene mai visitata nel flusso principale.

---

## Testing Checklist

- [ ] Un turno completo: TURN_SUBMISSION (voting) → [click Continue] → REVEAL_VOTES → [automatico] → SCORING (punti visibili)
- [ ] Fine turno: LEADERBOARD → [click Continue] → NEXT_ROUND → [automatico] → TURN_SUBMISSION (nuovo narratore, schermata di dichiarazione)
- [ ] Host vede solo i click necessari (2 click per turno: 1 per votazione, 1 per leaderboard)
- [ ] Narratore cambia correttamente nel nuovo round
- [ ] Punti base e bonus sono calcolati correttamente
- [ ] Il reset del round (clear card_played, votes) funziona

---

## Notes

- La fase REVEAL_NARRATOR può rimanere nel modello per scopi di debugging o estensioni future, ma non fa parte del main game loop
- Se in futuro si vuole una "pausa narratore reveal", si potrebbe aggiungere una flag `skip_narrator_reveal: bool` al Game model
- L'auto-advance deve avvenire lato server (non lato client) per evitare race condition e desincronizzazioni
