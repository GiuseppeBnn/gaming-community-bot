# Guess The Game & Sound Quest — design

Data: 2026-07-27 · Branch: `test_giu` · Stato: ☑ **completato il 2026-07-28** — eseguito da
[`2026-07-27-guess-the-game.md`](../plans/2026-07-27-guess-the-game.md) (motore, giudice in quattro
stadi, FSM di creazione, chiusura programmabile, reveal); hardening successivi in
[`2026-07-28-guess-sound-hardening-design.md`](2026-07-28-guess-sound-hardening-design.md).

Due nuovi eventi admin-driven, giocati **in privato** col bot: si indovina un
videogioco da un'**immagine** (`guess`, "Guess The Game") o da un **audio**
(`sound`, "Sound Quest"). Vince chi ci arriva in **meno tentativi**; a parità di
tentativi, chi ci mette **meno tempo**. Le risposte libere sono giudicate da un
LLM, perché «GTA SA» e «Grand Theft Auto San Andreas» sono la stessa cosa e una
`==` non lo sa.

---

## 0. Verdetto sul refactoring (la domanda che veniva prima)

**Il codice non è spaghetti e non va ristrutturato.** Non è un complimento a
caso: è la conclusione di una misura.

| Segnale | Misura | Lettura |
|---|---|---|
| Punto d'estensione eventi | registro `handlers/event_types`, dispatch senza `if/elif` (§18.2, regola 25) | aggiungere un tipo = una spec + una riga |
| Funzioni ≥ 60 righe | 18 su ~400 | fisiologico; sono quasi tutte money-path lunghe *perché* prudenti |
| Ordine dei router | dichiarato in un posto solo, **verificato da un test** | non è convenzione, è vincolo |
| Commit nei service | mai (§5), convenzione uniforme | il chiamante possiede la transazione |
| Coverage | 99.87% senza Postgres | ogni riga nuova dovrà essere coperta |

Il progetto ha già **previsto** queste due feature: `progress_service.GAME_LABELS`
contiene `"guess": "Guess The Game"` e `"sound": "Sound Quest"`, e il docstring di
`GamePodium` li dichiara «forward-declared». I trofei `podium_count` /
`first_place_count` si accendono da soli con una chiamata a `record_podium`.

### Le due sole modifiche strutturali che faccio, e perché sono necessarie

**1. `services/prizes.py`** — sposto lì le due funzioni **pure** oggi in
`quiz_service`: `participation_floor(consolation)` e
`consolation_amounts(n, top, floor)`. `quiz_service` le re-esporta, quindi
nessun chiamante cambia e i test esistenti (`test_quiz_prizes.py`,
`test_payout.py`) restano identici.

*Perché necessaria*: la scala premi (podio + consolazione lineare fino a un
pavimento) è la stessa identica per i due nuovi giochi. Senza lo spostamento le
alternative sono duplicare ~40 righe di logica **che paga monete**, oppure fare
`from services.quiz_service import ...` dentro `guess_service`, cioè far
dipendere un gioco da un altro. *Rischio*: nullo — funzioni pure, niente SQL,
niente stato. Commit isolato, verde prima di toccare altro.

**2. `ai_service`: una seconda porta d'ingresso, non una modifica alla prima.**
`generate_completion` è tarata sull'intrattenimento (temperature 0.9, testo
libero) ed è coperta e funzionante: **non si tocca**. Aggiungo
`judge_equivalence(...)`, deterministica e a output vincolato (§3).

### Cosa ho valutato e ho deciso di NON fare

- **`common.cmd_start` (190 righe, ~20 rami di deep-link)** — è l'unico vero
  outlier strutturale, e trasformarlo in tabella sarebbe un miglioramento
  reale: renderebbe *strutturale* l'invariante «ogni payload admin ri-verifica
  `is_admin`», oggi ripetuta a mano 7 volte. **Non lo faccio ora.** I due
  payload nuovi (`guess_<id>`, `sound_<id>`) sono **pubblici** — li gioca
  chiunque nel gruppo — quindi non aggiungono superficie di sicurezza e il
  beneficio del refactor non si materializza per *questa* feature. Restano due
  rami identici per forma a `quiz_<id>`. Annotato come candidato per dopo.
- **`handlers/betting.py` (774 righe)** — il file più grosso, ma nessuna delle
  due feature lo tocca. Rifattorizzare codice che non stai modificando è come
  cambiare gomme a un'auto parcheggiata.
- **Generalizzare la FSM di creazione quiz** — la tentazione è forte (i 4 step
  premi sono identici). Ma astrarre due FSM aiogram su router diversi produce
  indirezione peggiore della ripetizione. Riuso il **pattern** e le costanti di
  default, non il codice.

---

## 1. Un motore, due giochi

Guess The Game e Sound Quest differiscono **solo** per: il tipo di media
salvato, il metodo Bot API per rimandarlo, e le etichette. Tutto il resto —
tentativi, tempo, suggerimenti, giudizio, classifica, premi, XP, trofei — è
identico.

**Quindi: un solo service, un solo package handler, un solo modello, due
registrazioni nel registro.**

```
services/guess_service.py        motore DB (no-commit, §5)
services/guess_judge.py          normalizzazione + regole locali + giudice AI
handlers/guess/                  _shared · creation · lifecycle · play
handlers/event_types/guess_type.py   GuessType(kind=...) → istanziato 2 volte
```

```python
# register_builtin()
register(QuizType())
register(GuessType(kind="guess"))   # 🖼️ Guess The Game
register(GuessType(kind="sound"))   # 🔊 Sound Quest
register(PollType())
register(BetType())
```

Duplicare il gioco due volte significherebbe duplicare due volte un percorso che
**paga monete**. Questa è la decisione di design più importante del documento.

---

## 2. Schema DB (tre tabelle nuove, zero migrazioni)

`create_all()` crea le tabelle nuove da solo: `_MIGRATIONS` serve **solo** per
colonne aggiunte a tabelle esistenti. Nessuna riga da aggiungere lì.

### `guess_rounds`

| Colonna | Tipo | Note |
|---|---|---|
| `id` | PK | |
| `kind` | `String(16)` | `guess` \| `sound` — **è anche il `game_key`** dei trofei |
| `title` | `String(256)` | |
| `creator_tg_id` | `BigInteger` | |
| `status` | `String(16)` | `draft` → `ready` → `running` → `finished` |
| `group_id` | `BigInteger?` | |
| `media_file_id` | `String(256)` | file_id Telegram, rimandato al play |
| `media_kind` | `String(16)` | `photo` \| `audio` \| `voice` |
| `answer` | `String(200)` | risposta canonica, scritta dall'admin |
| `aliases_json` | `String(1024)` | JSON `list[str]`, grafie extra accettate |
| `hints_json` | `String(2048)` | JSON `[{"after": 3, "text": "…"}]` |
| `max_attempts` | `Integer` | ≥ 1 |
| `time_limit_seconds` | `Integer` | limite **per giocatore**, 0 = nessuno |
| `prize_first/second/third/consolation/min` | `Integer` | identici al quiz |
| `created_at`/`started_at`/`finished_at` | | |

I suggerimenti stanno in JSON e non in tabella figlia: sono piccoli, si leggono
sempre tutti insieme, non si interrogano mai da soli — esattamente il criterio
per cui `QuizQuestion.options_json` è JSON.

### `guess_sessions` — `UniqueConstraint(round_id, user_tg_id)`

`started_at`, `solved_at?`, `solved_attempts?`, `solve_ms?`, `attempts_used`,
`unverified_count`.

Serve una tabella perché il cronometro parte **quando l'utente apre il gioco**,
prima del primo tentativo, e deve sopravvivere a un restart.

### `guess_attempts` — `UniqueConstraint(round_id, user_tg_id, attempt_no)`

`attempt_no` (1-based), `raw_answer`, `normalized`, `verdict`
(`correct|wrong|unverified`), `source`
(`exact|alias|shape|ai|cache|unavailable`), `elapsed_ms`, `created_at`.

`normalized` è anche la **chiave di cache** dei verdetti (§3.4). `raw_answer`
serve all'audit admin: chiudendo il round si vede cosa è stato scartato.

---

## 3. Il giudice — la parte che deve essere «solida, non lasca, non ingannabile»

Quattro stadi, dal più economico al più costoso. L'AI vede solo il centro
ambiguo.

### 3.1 Normalizzazione (locale, gratis)

minuscolo · accenti via · punteggiatura via · spazi collassati · numeri romani →
arabi · rumore di edizione via (`remastered`, `definitive edition`, `goty`,
`hd`, `remake`) · clip a 80 caratteri.

### 3.2 Accettazione locale (gratis, autorevole)

Match esatto del normalizzato contro la risposta canonica **o** contro un alias
scritto dall'admin → **CORRETTA**, senza chiamare l'AI.

> È questo che rende il gioco robusto a un'AI irraggiungibile: la risposta
> giusta scritta bene vince **sempre**, anche con Groq giù.

### 3.3 Rifiuto locale per forma (gratis)

Un titolo di videogioco è corto. Se il normalizzato è < 2 caratteri, > 60
caratteri o > 8 parole → **SBAGLIATA**, senza AI.

Non è un trucco anti-injection travestito: è una regola onesta («la risposta
deve avere la forma di un titolo»). Che poi tagli fuori la quasi totalità dei
payload di prompt-injection, che sono lunghi e prolissi, è un effetto
collaterale gradito.

### 3.4 Cache dei verdetti (gratis dopo la prima volta)

Prima di chiamare l'AI: esiste già un `guess_attempts` con lo stesso
`(round_id, normalized)`? Allora **quel** verdetto, senza chiamata.

Tre effetti, tutti voluti: **equità** (due utenti che scrivono la stessa cosa
ricevono la stessa risposta — non negoziabile in un gioco che paga), **costo**
(il rate limit free tier smette di essere un problema), **determinismo**.

### 3.5 Il giudice AI

Modello: **`openai/gpt-oss-120b`** su Groq. Scelto perché è uno dei due modelli
su cui Groq supporta lo **structured output in modalità `strict`** (decoding
vincolato: il modello *non può* emettere altro che lo schema). Free tier: 30
RPM, 1.000 RPD, 8K TPM, 200K TPD — con chiamate da ~300 token il tetto reale è
qualche centinaio di giudizi al giorno, e la cache di §3.4 li collassa.
Configurabile via `groq_judge_model` senza toccare codice.

`groq_model` resta `llama-3.3-70b-versatile` per i comandi di intrattenimento:
tarati, coperti, non li tocco.

- `temperature = 0`, `max_tokens = 20`
- schema: `{"corretta": boolean}` — **e nient'altro**
- il testo del giocatore viaggia **normalizzato** (niente `{`, `}`, `:`, a capo:
  la superficie d'iniezione è già quasi azzerata dalla normalizzazione), avvolto
  nei delimitatori `<<<CONTENUTO>>>` già usati da `fun_ai`
- il system prompt dichiara che quel testo è materiale inerte, mai istruzioni

**Regola di sicurezza non negoziabile: l'output testuale del modello non
raggiunge mai il giocatore.** Si estrae un booleano e si butta il resto. È ciò
che rende impossibile usare un'iniezione per farsi rivelare la risposta. Lo
schema senza campo `reason` esiste apposta: non c'è niente da far trapelare.

Il prompt codifica la regola che l'utente ha chiesto esplicitamente:

> Accetta sigle note (GTA SA = Grand Theft Auto San Andreas), traduzioni, ordine
> diverso, refusi evidenti, numeri romani o arabi equivalenti (FF7 = Final
> Fantasy VII), sottotitoli di edizione presenti o assenti.
> **Rifiuta** chi nomina solo la serie senza il capitolo giusto (per «GTA San
> Andreas», «GTA» da solo è SBAGLIATO) e chi nomina un capitolo diverso.

Casi come «GTA» per «GTA San Andreas» vanno all'AI e non a una regola locale:
scrivere in codice una euristica «prefisso proprio ⇒ sbagliato» produrrebbe
falsi negativi su titoli dove il prefisso *è* il titolo. Il giudizio
serie-vs-capitolo è esattamente il lavoro per cui serve un LLM.

### 3.6 Quando l'AI non risponde

Un retry singolo su 429/5xx con backoff breve. Se ancora niente: verdetto
`unverified`.

Il tentativo **viene registrato** (la riga esiste: è ciò che limita il
brute-force) ma **non viene contato**, fino a un massimo di 3 bonus per utente
per round:

```
tentativi_rimasti = max_attempts + min(unverified_count, 3) - attempts_used
```

Il ragionamento sulle alternative, perché è il punto in cui è facile sbagliare:

- *Non registrare il tentativo* → canale di invio illimitato: con Groq giù un
  utente prova centinaia di titoli sperando di centrare il match esatto locale
  (§3.2, che resta attivo). Il limite tentativi evapora proprio quando siamo
  più deboli. **Scartata.**
- *Contarlo comunque* → con un 429 (probabile in burst sul free tier) si brucia
  il tentativo di qualcuno per un problema nostro. **Ingiusto.**
- *Bonus limitato a 3* → il tetto resta finito, l'utente non può provocare i
  429 a proprio vantaggio (ci arriverebbe esaurendo i propri tentativi), e
  l'ingiustizia è tappata. **Scelta.**

L'utente legge una frase onesta: «non sono riuscito a verificare la risposta,
riprova — questo tentativo non conta».

### 3.7 Anti-flood

Cooldown per utente fra un tentativo e l'altro (`guess_answer_cooldown_seconds`,
default 3s) sul bucket condiviso `utils.cooldown`, come ogni altro throttle del
bot. Più un tetto di concorrenza sulle chiamate al giudice, perché 30 RPM sono
30 RPM.

---

## 4. Ciclo di vita

### Creazione (admin, privato, da `ev:new:guess` / `ev:new:sound`)

titolo → **media** → *il bot rimanda indietro il media come anteprima* →
risposta corretta → alias (opzionale) → tentativi (preset 3/5/10 o custom) →
tempo (nessuno / 2m / 5m / 10m / custom) → suggerimenti (loop «dopo N tentativi:
testo», o «fine») → premi (gli stessi 4 step del quiz) → riepilogo → pubblica
(`ready`).

L'anteprima non è cortesia: **è la verifica che il `file_id` sia
ri-inviabile**, fatta nell'unico momento in cui l'admin può ancora rimediare.
Un `file_id` che fallisce al momento del gioco è il modo peggiore di scoprirlo.

### Avvio

`start_now` o scheduler. Annuncio nel gruppo con bottone deep-link
`?start=guess_<id>`, poi `running` — **prima l'annuncio, poi lo stato**, come
`open_quiz`: un invio fallito lascia un round `ready`, non uno `running` di cui
nessuno sa niente.

**Nel gruppo il media non si posta.** Si gioca in privato; mostrarlo lì
significherebbe farlo discutere e risolvere in chat. L'annuncio è un teaser.

### Gioco (privato)

Deep-link → guardie (round esistente, `running`, non già risolto) → sessione
(idempotente sul vincolo unico) → invio del media + regole (tentativi, scadenza
in orario assoluto) → stato FSM `GuessPlayStates.answering` con `round_id`.

Ogni messaggio di testo in quello stato è un tentativo: cooldown → scadenza →
tentativi residui → giudizio → registrazione → risposta. Se corretta:
transizione **in SQL**

```sql
UPDATE guess_sessions SET solved_at = :now, solved_attempts = :n, solve_ms = :ms
WHERE round_id = :r AND user_tg_id = :u AND solved_at IS NULL
```

`rowcount == 0` ⇒ già risolto (doppio tap): non si riassegna niente. È lo stesso
schema di `claim_close` e obbedisce alla regola 22 («le mutazioni di stato si
decidono in SQL, non in Python»).

I suggerimenti si consegnano quando `attempts_used` raggiunge la soglia
dichiarata.

**Il limite di tempo è stateless**: `started_at + time_limit_seconds`, controllato
a ogni invio. Niente task asyncio, niente mappa in memoria, sopravvive ai
restart — una **semplificazione** rispetto ai timer del quiz, resa possibile dal
fatto che qui il cronometro è uno per sessione e non uno per domanda. Il
giocatore riceve la scadenza come orario assoluto all'inizio, così non aspetta
un «tempo scaduto!» che nessun timer manderà.

### Chiusura

Conferma admin → `claim_close` in UPDATE condizionale → classifica dei
risolutori per `(solved_attempts ASC, solve_ms ASC)` → premi → XP → trofei
(`record_podium(game_key=kind, rank, round_id)`) → podio nel gruppo **con il
reveal**: media + risposta corretta.

`reset` («Riproponi») azzera sessioni e tentativi e riporta a `ready`;
`delete` rimuove tutto e annulla i task schedulati pendenti. Entrambi come il
quiz, con le stesse conferme `ev:ask*`.

### Programmazione

Nessun meccanismo nuovo: `ScheduledTask` con `task_type` = `guess` / `sound` e
`ref_id`. La chiusura automatica opzionale riusa il pattern già collaudato delle
scommesse — stesso `task_type` con `payload_json = {"action": "close"}`.

---

## 5. Premi, XP, trofei

**Classifica**: solo chi risolve. Ordine `(tentativi ASC, tempo ASC)`, esattamente
la richiesta.

**Monete**: 1°/2°/3° dai premi per rango; dal 4° in giù consolazione lineare che
scende da `prize_consolation` a `prize_min` — la scala condivisa di
`services/prizes.py`. Chi esaurisce i tentativi senza risolvere **non prende
monete**: qui «finisher» vuol dire «ha indovinato», ed è ciò che dà senso a
«meno tentativi, meglio è».

**XP** (non cappato, è evento admin-gated): partecipazione a chi manda almeno un
tentativo, bonus a chi risolve, bonus podio ai primi tre.

**Trofei**: `record_podium(kind, rank, round_id)` e il motore esistente fa il
resto — `podium_count` e `first_place_count` sono già parametrizzati per
`game_key` e `GAME_LABELS` ha già le due voci. **Zero modifiche al motore
trofei.**

---

## 6. Config nuova

```
groq_judge_model              openai/gpt-oss-120b
guess_judge_timeout_seconds   12
guess_default_attempts        5
guess_default_time_limit_seconds  300
guess_answer_cooldown_seconds 3
guess_max_unverified_bonus    3
guess_xp_participation / guess_xp_solved / guess_xp_podium_{first,second,third}
guess_default_{first,second,third,consolation}
```

`tests/unit/test_no_dead_config.py` verifica che ogni setting sia usato: se una
di queste resta orfana, il test lo dice.

---

## 7. Test (il gate è al 99%, ogni riga nuova va coperta)

**Unit** — tabella di normalizzazione (accenti, romani, rumore di edizione);
gate di forma; albero decisionale del giudice con `ai_service` finto; cache dei
verdetti; contabilità tentativi incluso il bonus `unverified` e il suo tetto;
scala premi.

**Integration** — FSM di creazione completa e ogni suo rifiuto; avvio e
annuncio; sessione di gioco (risolve / esaurisce / scade / già risolto / round
chiuso mentre gioca); doppio tap sulla risposta giusta (deve pagare una volta
sola); chiusura con podio, premi, XP, trofei; reset e delete; lancio e chiusura
programmati; deep-link; gating admin su creazione/avvio/chiusura.

**Sicurezza** — una risposta di prompt-injection viene giudicata sbagliata **e**
l'output del modello non compare mai in un messaggio all'utente; un non-admin non
crea, non avvia, non chiude.

Le gare vere (due sessioni concorrenti sulla stessa riga) vanno nei test `pg`:
su SQLite in-memory con `StaticPool` due sessioni condividono la transazione e
non sono esprimibili.

---

## 8. Rischi noti

| Rischio | Mitigazione |
|---|---|
| `file_id` non più risolvibile | anteprima in creazione; fallimento al play → messaggio chiaro + log, il round è modificabile |
| Rate limit Groq in burst | cache verdetti + cooldown + tetto di concorrenza + retry singolo + `unverified` con bonus |
| Giudizio AI sbagliato | alias espliciti dell'admin; `raw_answer` in audit sulla scheda del round; il match locale non passa mai dall'AI |
| Prompt injection | testo normalizzato, gate di forma, delimitatori, schema `strict` senza campo libero, output mai mostrato |
| Stato FSM senza TTL | è stato di *gioco*, non admin: al massimo consente di inviare tentativi a un round che risponderà «chiuso» |
