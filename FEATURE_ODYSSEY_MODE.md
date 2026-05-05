# Feature: Odyssey Mode (Multi-Vote Ruleset)

## Overview

Aggiungere un nuovo ruleset **Odyssey** in cui ogni giocatore non-narratore può votare
fino a **2 carte diverse** per round. Se un giocatore sceglie di rischiare votando
una **sola carta** e indovina correttamente, guadagna **4 punti** anziché 3.

L'host sceglie il ruleset al momento della creazione della partita tramite un selettore
nella UI di creazione. Non è possibile cambiare ruleset a partita avviata.

---

## Comportamento Attuale (da estendere)

- Il backend supporta già `votes_per_player` da 1 a 2 nel modello `Game`
- La UI di voto supporta già il multi-voto tramite `renderVoteGridWithBanner`
  (legge `state.game.votes_per_player` dal game state)
- Il punteggio base è calcolato in `standard_dixit.py::_apply_score_base`
  senza distinguere quanti voti ha usato il giocatore

---

## Target Behavior

### Nuove Regole — Odyssey

| Situazione | Punteggio |
|---|---|
| Giocatore vota 1 carta e indovina | **4 punti** |
| Giocatore vota 2 carte e indovina | **3 punti** |
| Narratore: almeno un giocatore indovina ma non tutti | **3 punti** |
| Nessuno / tutti indovinano (fail) | standard (vedi `standard.json`) |
| Bonus voti ricevuti sulla propria carta | +1 a voto (invariato) |

> Il campo `votes_per_player` sul Game viene forzato a 2 quando il ruleset
> è `"odyssey"`, indipendentemente da quanto inviato dal client.

### Creazione Partita (UI)

- Nella schermata di creazione comparirà una sezione **"Modalità di gioco"**
- L'host può scegliere tra i ruleset disponibili (recuperati da `GET /rulesets`)
- Default: `standard`
- Quando l'host seleziona `odyssey`, i 2 voti per giocatore sono impliciti
  (non serve un controllo separato)

---

## Flusso UI del Voto in Odyssey

### Problema di design

In modalità Odyssey (`votes_per_player = 2`) il giocatore ha **due comportamenti
possibili che valgono punti diversi**:

- vuole puntare alto → sceglie 1 carta (rischio maggiore, +4 se corretto)
- vuole giocare sicuro → sceglie 2 carte (rischio minore, +3 se corretto)

La UI deve rendere questa scelta deliberata e non accidentale.

### Soluzione consigliata: selezione esplicita con conferma

**Non** cambiare la carta votata tramite toggle / deselect. Invece:

1. Il giocatore vede la griglia di tutte le carte sul tavolo (meno la sua)
2. Può selezionare **1 o 2 carte** (le carte selezionate si evidenziano)
3. Sotto alla griglia compaiono due pulsanti distinti:
   - **"Vota 1 carta (+4 se giusto)"** → abilitato solo se esattamente 1 carta selezionata
   - **"Vota 2 carte (+3 se giusto)"** → abilitato solo se esattamente 2 carte selezionate
4. Alla pressione di uno dei due pulsanti appare una schermata di conferma
   (stessa logica del VOTE_PREVIEW attuale) con riepilogo della scelta
5. L'utente può annullare e tornare alla griglia

**Perché non il toggle/deselect semplice?**

La scelta di usare 1 o 2 voti in Odyssey ha impatto diretto sul punteggio, quindi
deve essere **intenzionale**. Con un semplice tap su una carta già selezionata per
deselezionarla, è facile inviare 1 voto per sbaglio pensando di starne selezionando 2.
I due pulsanti distinti con il premio esplicitamente mostrato rendono la differenza
visibile e riducono gli errori.

**Gestione del cambio idea**

Se il giocatore è già in VOTE_PREVIEW e vuole cambiare:

- Il pulsante "Modifica" (già esistente) lo riporta alla griglia con la selezione
  precedente ancora evidenziata, così non deve ricominciare da zero
- Può deselezionare una carta toccandola di nuovo e poi premere il pulsante
  corretto

**Regola UX importante**

Il giocatore non può mai votare 0 carte: il backend rifiuta comunque `card_numbers`
vuoto, ma la UI deve tenere i pulsanti di conferma disabilitati finché almeno
1 carta è selezionata.

---

## Data Model Changes

Nessuna modifica al modello `Game` o `Player`. Il campo `votes_per_player`
esiste già. L'informazione su quanti voti il singolo giocatore ha usato si deriva
già da `len(player.votes)` al momento dello scoring.

---

## Backend Changes

### 1. Nuovo ruleset config — `backend/rules/config/odyssey.json`

```json
{
  "correct_guess_points": 3,
  "correct_guess_single_vote_points": 4,
  "narrator_points": 3,
  "fail_all_points": 0,
  "fail_others_points": 2,
  "vote_bonus": 1
}
```

> `correct_guess_single_vote_points` è il campo aggiuntivo letto solo da
> `OdysseyRules`. I campi standard rimangono per retrocompatibilità con
> la classe base.

### 2. Nuovo modulo — `backend/rules/odyssey_rules.py`

Eredita da `StandardDixitRules`. Override di:

- `__init__`: carica `odyssey.json` e legge il campo extra
- `_apply_score_base`: nell'assegnazione dei punti ai giocatori corretti,
  distingue in base a `len(player.votes)`:
  - `== 1` → `correct_guess_single_vote_points` (4)
  - `>= 2` → `correct_guess_points` (3)
  - La logica di fail (tutti/nessuno) rimane invariata

```python
# Pseudocodice per il branch che cambia:
if n_correct == 0 or n_correct == n_voters:
    # identico allo standard
else:
    narrator.score += self._cfg.narrator_points
    for p in sorted(correct, key=lambda x: x.id):
        if len(p.votes) == 1:
            p.score += self._cfg.correct_guess_single_vote_points  # 4
        else:
            p.score += self._cfg.correct_guess_points              # 3
```

### 3. Registry — `backend/rules/rules_loader.py`

Aggiungere `"odyssey"` alla funzione `_registry()`:

```python
from backend.rules.odyssey_rules import OdysseyRules  # noqa: PLC0415
return {
    "standard": StandardDixitRules,
    "high_risk": HighRiskRules,
    "casual": CasualRules,
    "odyssey": OdysseyRules,
}
```

### 4. Normalizzazione — `backend/services/game_service.py`

In **entrambe** `create_game()` e `create_game_with_host()`, dopo la validazione
del ruleset, aggiungere:

```python
# Odyssey implica 2 voti per giocatore; normalizza per sicurezza
if ruleset == "odyssey":
    votes_per_player = 2
```

Questo garantisce consistenza anche se il client invia `votes_per_player=1`
con ruleset `"odyssey"`.

---

## Frontend Changes

### 1. Selezione ruleset in creazione — `frontend/app.js` → `renderCreate()`

Aggiungere alla UI di creazione (dopo il campo nickname) una sezione di scelta
modalità. I ruleset disponibili possono essere hardcoded o recuperati da
`GET /rulesets` alla prima apertura della schermata.

Nomi visualizzati suggeriti:

| `ruleset` API | Label UI | Descrizione breve |
|---|---|---|
| `standard` | Standard | Regole classiche Dixit |
| `casual` | Casual | Più permissivo, per principianti |
| `high_risk` | High Risk | Premi alti, penalità severe |
| `odyssey` | Odyssey | Vota 1 o 2 carte, premi diversi |

La chiamata a `/create_game` va modificata per includere i campi selezionati:

```javascript
api('/create_game', {
  nickname: nick,
  ruleset: selectedRuleset,       // "odyssey" | "standard" | ...
  votes_per_player: selectedRuleset === 'odyssey' ? 2 : 1
})
```

> `votes_per_player` può anche essere omesso: il backend normalizza comunque
> per odyssey. Inviarlo esplicito è buona pratica per chiarezza.

### 2. UI di voto in Odyssey — `frontend/app.js`

La funzione `renderVoteGridWithBanner` (e la sua variante inline nella sezione
multi-voto attorno alla riga 1151) va modificata per distinguere il caso Odyssey
da un generico `votes_per_player === 2`:

Condizione: `votesPerPlayer === 2 && game.ruleset === 'odyssey'`

Comportamento atteso:
- Mostra griglia selezione (uguale a oggi)
- Sostituisce il singolo "Confirm X votes" con due pulsanti:
  - **"Punta tutto: vota 1 carta (+4)"** — abilitato se `selected.length === 1`
  - **"Gioca sicuro: vota 2 carte (+3)"** — abilitato se `selected.length === 2`
- Entrambi i pulsanti chiamano `/update_vote` con la selezione corrente
  e poi avanzano a VOTE_PREVIEW (come oggi)

Per `votes_per_player === 2` con altri ruleset (non-Odyssey), il comportamento
rimane invariato rispetto ad oggi (un solo pulsante "Confirm").

---

## Testing Suggerito

### Backend

1. Creare una partita con `ruleset="odyssey"` → verificare `votes_per_player=2`
2. Creare una partita con `ruleset="odyssey"` e `votes_per_player=1` → deve essere normalizzato a 2
3. Score con 1 voto corretto → giocatore prende 4 punti
4. Score con 2 voti, uno dei quali corretto → giocatore prende 3 punti
5. Score fail_all (tutti indovinano) → punteggi come standard
6. Score fail_none (nessuno indovina) → punteggi come standard

### Frontend

1. Schermata creazione mostra selettore ruleset
2. Selezionare Odyssey → il payload inviato include `ruleset: "odyssey"`
3. In fase di voto con ruleset odyssey: i due pulsanti distinti sono visibili
4. Con 1 carta selezionata: "Punta tutto" abilitato, "Gioca sicuro" disabilitato
5. Con 2 carte selezionate: entrambi abilitati, ma solo il pulsante corretto
   invia la selezione giusta

---

## Edge Cases

- **Giocatore disconnesso prima di votare**: il backend ignora i disconnessi
  nello scoring (`_active_sorted`). Nessuna modifica richiesta.
- **Giocatore vota 2 carte con ruleset standard**: niente di speciale, la
  classe `StandardDixitRules` non guarda `len(player.votes)` per il punteggio
  base. Invariato.
- **Cambio ruleset dopo la creazione**: non supportato. Il ruleset è immutabile
  dopo `create_game`. Nessuna gestione richiesta.
- **Odyssey + fail_all (tutti indovinano)**: i punti del narratore e dei
  giocatori seguono la stessa logica `standard` (0 al narratore, 2 agli
  altri). Il bonus da voto singolo non si applica in questo caso perché il
  ramo corretto non viene eseguito.

---

## Ordine di Implementazione Consigliato

1. `backend/rules/config/odyssey.json` — nuovo file config
2. `backend/rules/odyssey_rules.py` — nuova classe con override scoring
3. `backend/rules/rules_loader.py` — registrazione nel registry
4. `backend/services/game_service.py` — normalizzazione `votes_per_player` per odyssey
5. `frontend/app.js` → `renderCreate()` — aggiungere selettore ruleset
6. `frontend/app.js` → logica di voto — aggiungere i due pulsanti distinti per Odyssey
7. Test end-to-end
