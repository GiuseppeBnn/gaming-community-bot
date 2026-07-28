# Guess The Game & Sound Quest — riparazione del giudice, dei tentativi e della UX

**Data:** 2026-07-28
**Stato:** approvato
**Tocca:** §19.b di STEERING (va aggiornato), `services/ai_service.py`,
`services/guess_judge.py`, `services/guess_service.py`, `handlers/guess/*`,
`handlers/event_types/guess_type.py`, `config_data/config.py`

---

## 1. Il problema, come si è manifestato

Segnalazione: «i due giochi sono pieni di bug, la UX è pessima e contro
intuitiva, e il controllo con IA è rotto qualsiasi risposta dai».

I log di produzione danno la causa esatta:

```
services.ai_service - WARNING - Giudice: status 400 —
  {"error":{"message":"Failed to validate JSON...","code":"json_validate_failed",
            "failed_generation":""}}
services.guess_judge - WARNING - Giudice irraggiungibile sul round 1: status 400
```

`failed_generation: ""` — **la generazione è vuota**. `groq_judge_model` è
`openai/gpt-oss-120b`, un modello di *reasoning*: i token di ragionamento
consumano lo stesso budget di `max_tokens`, che vale **20**
(`ai_service._JUDGE_MAX_TOKENS`). Il ragionamento esaurisce il budget, al canale
`content` non resta un token, Groq valida `""` contro lo schema strict e
risponde **400 `json_validate_failed`**.

Il 400 non è in `_JUDGE_RETRY_STATUSES`, e giustamente: la regola «una 4xx
significa che la richiesta è sbagliata, rimandarla brucia solo quota» era
*letteralmente vera* — la richiesta era sbagliata. Quindi nessun retry,
`AIServiceError` immediato, `Verdict(verified=False)` ogni singola volta.

Il match locale (stadio 2) funziona: `Grand Theft Auto: San Andreas` →
`exact=True`, verificato. Quindi «qualsiasi risposta» significa **tutto tranne
la risposta scritta carattere per carattere**, che è il 99% del gioco reale.

> **Perché i test non l'hanno preso.** 335 test passano. Ogni test del giudice
> costruisce un corpo di risposta ben formato con una fake session: il payload
> *in uscita* non è mai stato confrontato con ciò che il modello scelto può
> effettivamente produrre. Il test che mancava non è sul parsing, è sul
> **budget**: nessuno asseriva che `max_tokens` fosse abbastanza per il modello
> configurato. È il buco che questa spec chiude per primo.

## 2. Difetti trovati durante l'analisi (non segnalati)

| # | File | Difetto | Conseguenza |
|---|------|---------|-------------|
| B1 | `guess_service.attempts_left` | Il cap `guess_max_unverified_bonus` limita il **bonus**, non il consumo | Oltre il cap ogni `unverified` mangia un tentativo vero. Col giudice rotto il giocatore finisce i tentativi **senza che una sola risposta sia mai stata giudicata** |
| B2 | `guess_service.record_attempt` | Suggerimento agganciato a `after == attempt_no` esatto, e `attempt_no` avanza anche sugli `unverified` | Un `unverified` che cade sulla soglia si mangia il suggerimento per sempre |
| B3 | `guess_type.execute_scheduled` | Il branch `action == "close"` è **irraggiungibile**: nessun codice crea quel task | I round restano `running` per sempre. STEERING §19.b lo documenta come funzionante |
| B4 | `lifecycle.close_round` | `send_media` e l'annuncio del podio nello stesso `try` | Un `file_id` morto e **il gruppo non vede il podio**: premi pagati, nessuno informato |
| B5 | `guess_judge._ROMAN` | `x → 10` | `Mega Man X` e `Mega Man 10` normalizzano identici: **falso positivo su un percorso che paga monete** |
| B6 | `play.fsm_answer` | Il gioco è gated sul solo stato FSM, con `fsm_storage="memory"` | Dopo un restart le risposte cadono nel vuoto in silenzio (nei log: `Update ... is not handled`) |

## 3. Principi che il fix non deve violare

Restano invarianti da §19.b, e ogni modifica sotto li rispetta:

- l'accettazione locale viene **prima** dell'AI ed è autoritativa; il modello può
  solo *promuovere* un match mancato, mai ribaltarne uno riuscito;
- l'output testuale del modello **non raggiunge mai un giocatore**: si estrae un
  booleano e si butta il resto;
- l'ordine delle guardie in `play` è portante: cooldown → risolto → scadenza →
  tentativi → giudice;
- il service non committa (§5); le transizioni di stato sono UPDATE condizionali
  e `rowcount == 0` significa «gara persa» (§22).

---

## 4. Il giudice

### 4.1 Il budget

```python
_JUDGE_MAX_TOKENS = 512        # era 20
_JUDGE_REASONING_EFFORT = "low"
```

`reasoning_effort: "low"` entra nel payload. Su `gpt-oss-*` riduce i token di
ragionamento invece di sopprimerli — che è quello che serve: il giudizio è una
domanda binaria su due stringhe corte, non ha bisogno di catene lunghe. Il
modello resta `gpt-oss-120b` perché il **constrained decoding strict** è la
garanzia su cui §19.b è costruita, e lo supportano solo `gpt-oss-20b/120b`.

512 non è un numero tirato a caso: è il tetto sotto cui una risposta binaria con
ragionamento breve sta comodamente, e sopra cui pagheremmo latenza per niente.

### 4.2 Il fallimento diventa leggibile

`json_validate_failed` smette di essere «irraggiungibile». Diventa un ramo suo,
loggato con il codice vero:

```python
if code == "json_validate_failed":
    logger.error(
        "Giudice: generazione non conforme allo schema (max_tokens=%s, modello=%s). "
        "Se failed_generation è vuoto il budget token è troppo basso per il "
        "reasoning del modello.", _JUDGE_MAX_TOKENS, settings.groq_judge_model)
```

Questo è il pezzo che è costato di più a diagnosticare, e non deve costare due
volte. La distinzione è per **chi legge i log**, non per il chiamante: verso
`guess_judge` resta un `AIServiceError`, perché per il gioco «non ho un verdetto»
è una cosa sola. Nessun nuovo tipo di eccezione da gestire a valle.

### 4.3 Il test che mancava

Un test asserisce che `_JUDGE_MAX_TOKENS` è compatibile con un modello di
reasoning (soglia esplicita, con il *perché* nel messaggio) e che il payload
porta `reasoning_effort`. È il test che avrebbe preso questo bug al primo giro.

Più un test sul 400 `json_validate_failed`: deve alzare `AIServiceError` **senza
retry** (il comportamento attuale è corretto e va bloccato contro le regressioni).

## 5. I tentativi (B1) — il giocatore non paga i nostri guasti

**Regola:** un `unverified` non consuma **mai** un tentativo vero. Non «fino a
un cap»: mai.

Il cap serve ancora, ma cambia mestiere: non limita più il rimborso, limita
**quante risposte non giudicate accettiamo prima di fermarci**. Superato il cap
il tentativo viene **rifiutato prima del giudice**, con un messaggio onesto:

> ⚠️ Il giudice non risponde in questo momento. I tuoi tentativi sono salvi —
> riprova fra poco.

Questo chiude entrambe le alternative che §19.b aveva già valutato e scartato:
non c'è canale di invio illimitato (il cap + il cooldown lo chiudono) e non
addebitiamo al giocatore un nostro 429/400.

`attempt_no` resta **monotono** — è parte della chiave unica
`(round, user, attempt_no)`, e due `unverified` di fila con lo stesso numero
sarebbero un `IntegrityError`. Quindi restano due contatori distinti, ed è
volutamente così:

- `attempts_used` — quante righe esistono (guida `attempt_no`, monotono);
- `unverified_count` — quante di quelle non sono un verdetto.

Il budget è `max_attempts - (attempts_used - unverified_count)`. La formula
attuale (`max_attempts + min(unverified, cap) - used`) diventa quella togliendo
il `min`, e il `min` si sposta nella guardia nuova.

## 6. I suggerimenti (B2)

Il suggerimento si aggancia al conteggio **verificato**, non al numero di riga:

```python
verified_used = attempts_used - unverified_count
hint = next((t for a, t in hints if a == verified_used), None)
```

Con la §5 sopra, `verified_used` avanza solo quando un verdetto è arrivato
davvero, quindi non ha buchi e l'uguaglianza esatta torna a essere corretta.

## 7. L'auto-close (B3)

`open_round` programma la chiusura, riusando `schedule_service.schedule_task`
esattamente come `bet_service.schedule_close` — stesso `task_type = kind`, stesso
`payload = {"action": "close"}`, **nessun task-type nuovo**. Il ramo che
`guess_type.execute_scheduled` già contiene smette di essere codice morto.

Il *quando* è l'unica decisione nuova. L'orologio dei tentativi è **per
giocatore** e parte quando quel giocatore apre il link, quindi non esiste un
istante derivabile in cui «sono scaduti tutti». Le opzioni valutate:

1. `started_at + time_limit + grace` fisso — chi entra tardi perde tempo in
   silenzio, e la penalità è invisibile;
2. **un campo di durata del round, esplicito** ← scelto;
3. niente auto-close (stato attuale) — i round restano aperti per sempre.

Vince la 2 perché è l'unica che non mente al giocatore: la durata la decide
l'admin, si vede nella scheda e si vede nell'annuncio di gruppo. Con la scheda
della §8 costa un campo, non uno step in più.

Campo `round_duration_seconds`, default `settings.guess_default_round_duration`
(1800), `0` = chiusura manuale. `delete_round` già cancella i task pendenti;
`close_round` manuale deve cancellarlo anche lui, altrimenti lo scheduler più
tardi trova un round `finished` e logga un fallimento per una cosa andata bene.

## 8. La creazione — da 11 domande in fila a una scheda

### 8.1 Cos'è sbagliato adesso

`creation.py` sono 570 righe, 12 stati FSM e 8 handler quasi identici. Ma il
difetto vero non è la lunghezza: è che **non si torna indietro**. Sbagli la
risposta allo step 3 e le uniche uscite sono andare avanti per altri 8 step o
annullare e rifare tutto. Su un flusso da 11 domande, questo è il difetto.

### 8.2 La forma nuova

Tre domande obbligatorie — le uniche che non hanno un default sensato — poi una
scheda:

```
➕ Crea → titolo → media → risposta → ┌─ SCHEDA ─────────────────────┐
                                      │ [il media, rimandato indietro]│
                                      │ ✅ Risposta: GTA: San Andreas │
                                      │ 🔤 Alias: nessuno             │
                                      │ 🎯 Tentativi: 5               │
                                      │ ⏱️ Tempo: 5 min                │
                                      │ ⏳ Chiusura: dopo 30 min       │
                                      │ 💡 Suggerimenti: 0            │
                                      │ 🏆 800 / 400 / 200 · 80       │
                                      ├──────────────────────────────┤
                                      │ [✏️ campo] … [✅ Pubblica]     │
                                      └──────────────────────────────┘
```

Ogni campo è un bottone. Lo tocchi, lo modifichi, torni alla scheda. I quattro
prompt dei premi diventano un campo solo. Non esiste più uno stato in cui hai
sbagliato qualcosa e non puoi correggerlo.

Il media viene **rimostrato nella scheda**: l'eco in creazione era già la
verifica che il `file_id` sia ri-inviabile (§19.b), e vederlo accanto alla
risposta è anche come ci si accorge di aver allegato il file sbagliato.

### 8.3 Struttura

Un registry di campi, table-driven, sostituisce gli 8 handler:

```python
@dataclass(frozen=True)
class Field:
    key: str
    label: str
    prompt: str
    parse: Callable[[str, dict], tuple[object | None, str | None]]  # (valore, errore)
    show: Callable[[dict], str]
```

Stati: `waiting_title`, `waiting_media`, `waiting_answer`, `editing`, `card`.
**Cinque invece di dodici**, e **un** handler di edit invece di otto.

> Vincolo di progetto: se `creation.py` **cresce**, il design è sbagliato e va
> rifatto, non giustificato. La scheda deve togliere codice.

`parse` riusa i validatori e i cap che già esistono in `_shared` (`too_long`,
`_MAX_*`): la validazione non si riscrive, si ricolloca.

## 9. Il gioco

- **Stato sempre visibile.** Ogni risposta chiude con
  `🎯 Tentativi 2/5 · ⏱️ scade alle 22:14`. Oggi il giocatore vede i tentativi
  rimasti solo quando sbaglia, e la scadenza solo all'ingresso.
- **Si rientra senza cercare l'annuncio.** Il messaggio di uscita porta un
  bottone `🔄 Riprendi` che rientra nello stato. *Niente comando `/gioca`*: il
  deep-link e il bottone coprono già il caso, e un comando nuovo è una voce di
  help, una registrazione e un handler in più per niente.
- **B4:** media e podio in due `try` separati. Il podio è la cosa che deve
  partire; il reveal è un di più, e non deve poter portarselo dietro.
- **B6:** `i`, `v`, `x` singoli escono da `_ROMAN`. `Mega Man X` smette di
  valere `Mega Man 10`. Il costo è che `Final Fantasy X` ↔ `Final Fantasy 10`
  passa dal match locale al giudice — che dopo la §4 funziona — o a un alias.
  Su un percorso che paga monete, un falso positivo in meno vale una chiamata
  AI in più.
- **B6 bis:** i romani multi-lettera (`ii`, `iv`, `vii`…) continuano a foldare:
  lì l'ambiguità non esiste.

## 10. Test

Ogni difetto della §2 entra con il suo test di regressione, scritto **prima**
del fix (TDD):

| Test | Cosa blocca |
|------|-------------|
| budget del giudice ≥ soglia reasoning + `reasoning_effort` nel payload | il bug segnalato |
| 400 `json_validate_failed` → `AIServiceError`, **una sola** chiamata | il retry che brucerebbe quota |
| N `unverified` di fila non consumano tentativi | B1 |
| oltre il cap: rifiuto **prima** del giudice, 0 chiamate al modello | B1 |
| suggerimento sopravvive a un `unverified` sulla soglia | B2 |
| `open_round` crea il task di chiusura; `close_round` lo cancella | B3 |
| `send_media` che alza → il podio parte lo stesso | B4 |
| `Mega Man X` ≠ `Mega Man 10`; `FF VII` = `FF 7` | B5/B6 |
| scheda: modifica di ogni campo e ritorno; pubblicazione | §8 |

I sette test che contano le chiamate al modello per fissare l'ordine delle
guardie (§19.b) **restano e devono continuare a passare**: la guardia nuova
della §5 si inserisce prima del giudice, non al posto di quelle.

## 11. Fuori scope

- Cambiare `groq_model` (§17, intrattenimento): tarato su llama-3.3, non si tocca.
- Migrazioni di schema: nessun campo nuovo sulle tabelle esistenti tranne
  `guess_rounds.round_duration_seconds`.
- Riscrivere il quiz: condivide `services/prizes.py` e basta.
