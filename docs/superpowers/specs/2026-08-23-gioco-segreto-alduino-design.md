# Il gioco segreto di Alduino — design v2

**Data:** 2026-08-23 · **Branch:** `test_giu` · **Stato:** implementato secondo il piano;
`TWENTYQ_V2_ENABLED` resta `false` fino al rollout esplicito.

Questa specifica sostituisce le regole di gioco, lifecycle, provider ed economia
della prima versione descritta in
[`2026-08-12-alduino-venti-domande-design.md`](2026-08-12-alduino-venti-domande-design.md).
Restano valide l'architettura del catalogo, l'estrazione bilanciata, la discovery
pubblica e la separazione fra runtime generico e strategia `twentyq` definite in
quel documento. Gli invarianti economici e transazionali restano quelli di
[`2026-08-04-audit-denaro-xp-design.md`](2026-08-04-audit-denaro-xp-design.md)
e di `STEERING.md`.

---

## 1. Obiettivo

Trasformare l'attuale gioco globale da 20 domande e 3 tentativi in un evento
collaborativo adatto a un gruppo di circa 50 persone:

- ogni utente dispone di una quota personale, senza sottrarre opportunità agli
  altri;
- ogni partecipazione valida riceve XP alla chiusura, anche senza vittoria;
- in caso di vittoria un montepremi crescente col numero dei partecipanti viene
  diviso in parti perfettamente uguali;
- domande e tentativi errati riducono gradualmente il montepremi, premiando
  l'efficienza collettiva;
- Gemini e Groq gratuiti sono le corsie primarie; OpenRouter a pagamento è un
  fallback limitato, osservabile e incapace di decidere una vincita;
- partite, quote, scadenze e pagamenti rimangono corretti sotto concorrenza,
  riavvii ed errori parziali.

Il nome pubblico diventa **«Il gioco segreto di Alduino»**. La chiave interna
`twentyq` non cambia, così scheduler, registry e dati esistenti restano
compatibili.

## 2. Confini

### Incluso

- regole v2, quote personali e scadenza;
- ricompense CoInn/XP e relativo audit;
- catena strutturata Gemini → Groq → OpenRouter;
- contesto AI limitato, cache dei duplicati e telemetria priva di prompt;
- creazione amministrativa, card, comando pubblico e manuale utenti;
- migrazioni additive e compatibilità completa con le partite legacy;
- test reali PostgreSQL per gare, lock, rollback e idempotenza;
- harness locale per calibrare prompt e modelli con API reali;
- avvio locale Docker e configurazione `.env` sicura.

### Escluso

- addestrare o modificare i pesi dei modelli: qui «fine tuning» significa
  calibrazione misurata di prompt, schema, modello e routing;
- cambiare catalogo IGDB, estrazione bilanciata o fallback integrato;
- assegnare bonus economici al vincitore;
- usare l'AI per stabilire se un tentativo è corretto;
- introdurre classifiche o trofei specifici del gioco;
- riscrivere servizi economici, scheduler o help oltre alle estensioni necessarie.

## 3. Alternative valutate

1. **Quote personali e durata, scelta.** Scala al gruppo senza una risorsa
   globale monopolizzabile. Il costo è una chiusura temporale obbligatoria.
2. **Aumentare i vecchi limiti globali, scartata.** Sposta soltanto il problema:
   gli utenti più rapidi continuano a consumare le possibilità di tutti.
3. **Roster prenotato prima dell'avvio, scartato.** Aggiunge attrito, esclude chi
   arriva dopo e contraddice l'obiettivo di premiare la partecipazione spontanea.

Per il premio è scelta una formula lineare, leggibile e verificabile. Un jackpot
fisso diluirebbe la quota quando entra una persona nuova e incentiverebbe a
escluderla; formule esponenziali o a scaglioni sono più difficili da spiegare e
introducono salti arbitrari.

Per l'AI è scelta una catena di provider. Solo Gemini espone il gioco a quote e
availability di un singolo servizio; usare OpenRouter per ogni turno spenderebbe
credito anche quando una corsia gratuita adatta è disponibile.

## 4. Regole di gioco

### 4.1 Creazione e durata

L'admin crea il gioco dall'hub `/eventi`. Oltre al titolo pubblico sceglie:

- durata di 2, 6, 12 o 24 ore;
- una data/ora assoluta futura;
- il massimo CoInn per partecipante, con default 100.

La UI propone **12 ore** come scelta raccomandata. Per una durata relativa,
`expires_at` viene calcolato dall'avvio effettivo, non dall'ora programmata. Una
scadenza assoluta già trascorsa impedisce l'avvio senza mutare lo stato. Il
massimo premio deve essere un intero fra 1 e il limite configurabile
`TWENTYQ_MAX_COINS_PER_PARTICIPANT`, default 1.000.

Una durata relativa salva `duration_seconds` mentre la partita è `ready` e
valorizza `expires_at` nel passaggio a `running`. Una scadenza personalizzata
salva direttamente `expires_at` e lascia `duration_seconds=NULL`; non viene
spostata se un avvio programmato parte in ritardo.
I timestamp sono persistiti in UTC secondo la convenzione della repo e resi in
`Europe/Rome` nelle UI.

Il target continua a essere estratto dal catalogo locale persistito. Titolo,
alias e dossier vengono fotografati nella partita: nessuna chiamata AI o IGDB
avviene durante l'avvio e un sync successivo non modifica il segreto.

### 4.2 Interazione e quote

Si gioca rispondendo alla card ancora nel gruppo:

- testo normale: domanda classificabile con `sì`, `no` o `forse`;
- `RISPOSTA: <titolo>`: tentativo di soluzione.

Una domanda il cui scopo principale è proporre direttamente un titolo non può
pagare la penalità più economica delle domande. Un guard sintattico locale e il
verdetto strutturato `usa_risposta` la rifiutano senza consumare nulla e
invitano il mittente a reinviarla con `RISPOSTA:`. Il modello può soltanto
richiedere questa conferma esplicita: non converte il testo in tentativo, non
consuma quota e non decide la vittoria.

Per ogni utente:

- massimo **5 domande valide**;
- massimo **2 tentativi validi**.

Non esiste un cap aggregato per le partite v2. I contatori aggregati restano
statistiche e non chiudono il gioco. Esaurire la propria quota non influenza
quella altrui.

Un turno consuma quota soltanto dopo essere stato persistito con un verdetto
valido. Non consumano quota: input vuoto/invalido, throttling, partita occupata o
chiusa, provider falliti, schema invalido, lease perso e risposta arrivata dopo
la scadenza.

### 4.3 Duplicati e partecipazione valida

Una normalizzazione conservativa applica Unicode NFKC, `casefold`, collasso
degli spazi e punteggiatura equivalente. Non tenta equivalenza semantica.

- una domanda normalizzata già risolta nella stessa partita riusa il verdetto
  persistito, senza chiamare l'AI;
- una domanda duplicata non crea un turno, non consuma quota e non rende
  partecipante il mittente;
- un tentativo normalizzato già effettuato viene rifiutato gratuitamente: non
  consuma quota, non riduce il premio e non rende partecipante il mittente;
- il normale rate limiter del bot continua a proteggere anche questi percorsi
  gratuiti.

È **partecipante valido** ogni Telegram ID distinto con almeno un turno valido e
persistito di tipo `question` o `guess`. Il tentativo vincente vale come
partecipazione. Una persona che ha solo inviato duplicati o richieste fallite
non rientra nella divisione né riceve XP.

La partecipazione resta aperta fino alla chiusura: una persona che entra tardi e
compie un'azione valida aggiunge anche la propria quota potenziale al pool. Non
esiste un roster prenotato o bloccato.

### 4.4 Vittoria e chiusura

Un tentativo viene confrontato **localmente** con titolo e alias normalizzati.
Un modello non può autorizzare una vittoria o un pagamento. Il primo tentativo
corretto chiude atomicamente la partita; non applica penalità.

Una partita v2 termina soltanto per:

- `victory`: primo tentativo corretto;
- `expired`: raggiungimento della scadenza;
- `admin_closed`: chiusura manuale.

Allo scadere non viene rivelato o pagato un tentativo tardivo. Il segreto viene
mostrato nella card terminale. Una partita `ready` cancellata prima dell'avvio
non ha partecipanti né ricompense.

## 5. Economia e XP

### 5.1 Formula CoInn

Definizioni:

- `n`: partecipanti validi distinti;
- `q`: domande valide, non duplicate;
- `w`: tentativi validi errati;
- `B`: massimo CoInn configurato per partecipante.

Per evitare floating point si usano interi e basis point:

```text
base      = n * B
minimum   = ceil(base * 3000 / 10000)
penalty   = floor(B * (600*q + 2000*w) / 10000)
pool      = max(minimum, base - penalty)
share, remainder = divmod(pool, n)
```

Con `n=0` la formula non viene valutata: il settlement è `void`, tutti gli
importi sono zero e non esistono allocazioni.

Con `B = 100` la forma leggibile è:

```text
pool = max(30*n, 100*n - 6*q - 20*w)
```

Quindi ogni domanda costa 6 CoInn al pool e ogni tentativo errato 20; il
minimo complessivo resta il 30% della base. Scalando `B`, minimo e penalità
mantengono automaticamente le stesse proporzioni. Il calcolo usa BIGINT con
controlli espliciti prima della moltiplicazione.

Ogni partecipante riceve `share`. `remainder` resta intenzionalmente non
distribuito e viene registrato: assegnarlo a qualcuno violerebbe l'uguaglianza.
Il vincitore ottiene riconoscimento nella card, non un bonus economico.

I CoInn vengono accreditati **soltanto su `victory`**. Il settlement conserva
`computed_pool`, cioè il risultato della formula, e `paid_pool`: quest'ultimo è
uguale a `computed_pool` su vittoria e zero su `expired`/`admin_closed`. Con zero
partecipanti entrambi sono zero e non si crea alcuna allocazione.

Esempi col default:

| Partecipanti | Scenario | Pool | Quota uguale |
|---:|---|---:|---:|
| 5 | 5 domande, 0 errori | 470 | 94 |
| 10 | 20 domande, 10 errori | 680 | 68 |
| 50 | 250 domande, 99 errori, poi vittoria | 1.520 | 30 (resto 20) |

Il pavimento matematico di 1.500 richiederebbe 100 tentativi errati e quindi
nessuna vittoria; non sarebbe pagato. In una partita vinta, il caso più costoso
lascia almeno 30 CoInn interi a testa.

### 5.2 XP di partecipazione

Ogni partecipante valido riceve **10 XP** su `victory`, `expired` e
`admin_closed`. Gli XP:

- non diminuiscono con domande o tentativi;
- sono assegnati una sola volta per partita;
- usano la nuova sorgente `XpSource.twentyq`;
- sono `uncapped`, come gli altri eventi creati dagli admin;
- passano esclusivamente da `xp_service`.

### 5.3 Atomicità e audit

La chiusura, le allocazioni, i crediti CoInn, gli XP e i rispettivi audit
appartengono a **una sola transazione PostgreSQL**. I service non committano;
l'handler o l'esecutore schedulato possiede commit/rollback.

I CoInn usano `economy_service.credit` con il nuovo
`TransactionType.ai_game_reward`. `LedgerEntry.reference_id` resta `NULL`,
perché quel campo è una foreign key verso `betting_events`; la relazione tipata
con la partita vive nell'allocazione del settlement.

Prima di pagare vengono verificati tutti gli utenti e wallet. Se ne manca uno,
l'intera chiusura fallisce e rimane ritentabile: nessuno viene escluso
silenziosamente alterando il divisore. I wallet vengono acquisiti in ordine
crescente di `tg_id`, dopo il claim della partita, rispettando l'ordine canonico
Event → User → Wallet.

## 6. Esperienza utente e documentazione

### 6.1 Card condivisa

La card mostra sempre valori derivati dalla policy persistita:

- nome «Il gioco segreto di Alduino» e titolo pubblico dell'evento;
- scadenza assoluta e tempo residuo;
- regola «5 domande · 2 tentativi per persona»;
- partecipanti validi, domande e tentativi errati aggregati;
- massimo e minimo per partecipante;
- quota per partecipante se il gioco fosse vinto in quel momento;
- 10 XP garantiti a ogni partecipante valido alla chiusura;
- ultimi sei turni validi, senza caricare l'intero ledger.

Il pool visualizzato è una proiezione: può crescere quando entra un nuovo
partecipante. La card lo dice esplicitamente. Dopo ogni azione la risposta
personale indica domande e tentativi residui. Una quota esaurita produce un
messaggio specifico, non un generico «gioco occupato».

La card terminale distingue vittoria, scadenza e chiusura admin; mostra segreto,
partecipanti, conteggi, XP e vincitore quando presente. Su vittoria mostra anche
formula, quota e resto non distribuito; negli altri casi dichiara chiaramente
«CoInn: 0 — gioco non indovinato» senza presentare il pool teorico come premio.

Un fallback Telegram non editabile invia una nuova card. Lo spostamento
dell'anchor è condizionato al vecchio message ID, così un refresh vecchio non
può sovrascriverne uno più recente.

### 6.2 Help pubblico

Si aggiunge `/gioco_alduino` alla categoria pubblica `🏆 Progressione` in
`help_content.py`. Lo stesso `CommandDoc` alimenta:

- `/comandi`;
- `/spiega_comando gioco_alduino` e relativo deep-link;
- reference di Alduino.

Il nome viene inoltre aggiunto esplicitamente alle attuali liste dei menu
Telegram privato e di gruppo. Non si generalizza il menu né si finge che oggi
derivi da `CommandDoc`: quel refactor globale resta fuori scope.

Il comando illustra quote, duplicati, durata, partecipazione, XP e premio con un
esempio semplice. Se esiste una partita attiva, nel gruppo aggiunge stato e
quota personale del richiedente. La UI di creazione, la card e l'help usano un
renderer comune della policy; numeri e formula non vengono copiati in tre
stringhe indipendenti.

README e `STEERING.md` documentano rispettivamente il comportamento pubblico e
i nuovi invarianti tecnici. Questa spec resta la spiegazione di prodotto e
architettura, non una seconda sorgente runtime delle costanti.

## 7. Architettura e modello dati

### 7.1 Aggregate e policy

Il runtime esistente rimane diviso in:

1. `AIGameSession`: lifecycle e lease generico;
2. `AIGameTurn`: ledger append-only dei soli turni validi;
3. `TwentyQuestionsGame`: segreto e stato della strategia;
4. settlement/allocation: policy economica fotografata e liquidazione.

`AIGameTurn` è la fonte di verità per quota personale e partecipanti. I contatori
aggregati di `TwentyQuestionsGame` sono soltanto proiezioni statistiche. Non si
introduce una tabella partecipanti duplicata.

I service restituiscono DTO immutabili e motivazioni tipizzate, non booleani o
`None` ambigui. Le categorie minime sono:

```text
claimed | reused | recorded | rejected
busy | closed | expired | question_quota | guess_quota | duplicate_guess |
lost_claim | invalid_input | providers_unavailable
```

Le query decisive leggono proiezioni scalari; il nuovo codice non usa
`populate_existing=True` per correggere entità stale.

### 7.2 Modifiche a tabelle esistenti

`ai_game_sessions` aggiunge:

- `duration_seconds BIGINT NULL`;
- `expires_at TIMESTAMP NULL`;
- `finish_reason VARCHAR(32) NULL`;
- `archived_at TIMESTAMP NULL`, senza introdurre un nuovo stato terminale;
- `pending_user_tg_id BIGINT NULL` e `pending_kind VARCHAR(16) NULL`, associati
  al lease e sempre puliti insieme a token/timestamp.

`ai_game_turns` aggiunge:

- `normalized_input_hash CHAR(64) NULL`, SHA-256 dell'UTF-8 normalizzato;
- indice `(session_id, user_tg_id, kind)` per le quote;
- vincolo univoco `(session_id, kind, normalized_input_hash)` per i nuovi turni.

Il testo grezzo resta in `input_text`, limitato prima del salvataggio. L'hash a
lunghezza fissa evita che NFKC/casefold espandano una stringa oltre la colonna o
il limite di un indice. Su un match hash, il servizio rinormalizza il testo
grezzo persistito e conferma l'uguaglianza prima di riusare il verdetto. Un
eventuale mismatch viene registrato e rifiutato senza quota, mai trattato come
duplicato né inserito contro il vincolo. Le righe legacy mantengono hash `NULL`,
ammesso più volte dal database.

`twenty_questions_games` aggiunge:

- `rules_version INTEGER NOT NULL`, backfill `1`;
- `questions_per_user INTEGER NULL`;
- `guesses_per_user INTEGER NULL`.

I vecchi `question_limit` e `guess_limit` diventano nullable. Le righe esistenti
conservano 20/3; le nuove v2 salvano `NULL`, 5 e 2. I vecchi contatori restano
necessari per completare senza regressioni le sessioni legacy.

`scheduled_tasks` aggiunge `retry_count INTEGER NOT NULL DEFAULT 0`. È un campo
generico ma viene usato in questa fase soltanto dai task interni `expire`; gli
altri task mantengono la semantica corrente.

### 7.3 Nuove tabelle

`ai_game_reward_settlements`, una riga univoca per sessione v2:

- policy versionata: `B`, basis point 3000/600/2000 e XP 10;
- stato `pending | settled | void`;
- motivo terminale e conteggi finali `n/q/w`;
- base, penalità, `computed_pool`, `paid_pool`, quota e resto;
- timestamp di creazione e liquidazione.

La riga nasce con la partita, non alla chiusura: le regole non possono cambiare
a metà evento. `settled` indica qualsiasi liquidazione con partecipanti,
compresi `expired` e `admin_closed` che pagano solo XP; `void` è riservato
esclusivamente a zero partecipanti e quindi zero accrediti.

`ai_game_reward_allocations`, unique `(session_id, user_tg_id)`:

- CoInn e XP assegnati;
- timestamp;
- riferimento al settlement.

`ai_game_provider_attempts`, audit senza prompt:

- sessione, operazione, provider e modello;
- versione prompt/schema;
- esito o classe di errore e latenza;
- token/costo quando disponibili;
- nessun testo utente, dossier, titolo, username o Telegram ID del mittente.

`ai_feature_budget_periods`, unique `(period, feature)`:

- cap, speso e riservato in micro-USD;
- aggiornamento atomico insieme al budget mensile globale.

Le foreign key del settlement impediscono la cancellazione di una partita v2
avviata. Dopo la chiusura, l'azione amministrativa valorizza `archived_at` e la
nasconde dalle viste normali, preservando l'audit che giustifica wallet e XP.
Una bozza `ready` mai avviata resta eliminabile eliminando esplicitamente prima
il suo settlement ancora `pending`; nessun `CASCADE` può cancellare un
settlement terminale. Il comportamento di cancellazione legacy rimane invariato.

### 7.4 Migrazione e legacy

`Base.metadata.create_all()` crea le nuove tabelle. Le colonne, i vincoli e gli
indici aggiunti a tabelle esistenti passano da `_MIGRATIONS`, con SQL PostgreSQL
additivo e idempotente.

- tutte le righe esistenti diventano `rules_version=1`;
- sessioni `running` esistenti mantengono 20/3, `expires_at=NULL` e nessuna
  ricompensa;
- sessioni già finite ricevono `finish_reason=legacy`;
- nessuna riga legacy riceve retroattivamente un settlement;
- le nuove creazioni sono sempre v2 quando la feature è abilitata.

La feature flag `TWENTYQ_V2_ENABLED`, sicura per default a `false`, disabilita
**nuove creazioni** quando falsa, ma non impedisce di giocare o chiudere sessioni
legacy. Non crea nuove partite v1.

## 8. Lifecycle concorrente

### 8.1 Domanda con rete esterna

1. Validare gruppo, input e sessione.
2. Se la scadenza è raggiunta, tentare la chiusura `expired` prima di altro.
3. Calcolare quota personale dal ledger e cercare il duplicato.
4. Acquisire con SQL condizionale il lease, già associato a utente e tipo turno.
5. Committare il breve claim.
6. Chiamare la catena provider senza connessioni DB aperte.
7. Con un verdetto valido, ricontrollare token, stato, scadenza e quota; appendere
   il turno e liberare il lease nella stessa transazione.
8. Su fallimento, liberare il lease senza consumare quota.
9. Committare prima di modificare o inviare messaggi Telegram.

Il completamento aggiorna `next_turn_no` e inserisce il turno atomico. Un token
recuperato o una risposta arrivata dopo chiusura restituisce `lost_claim` e non
scrive nulla.

### 8.2 Tentativo locale

Il tentativo non apre rete. Validazione, quota, deduplica, append del turno,
eventuale vittoria e settlement avvengono in una sola transazione breve. Il
percorso usa lo stesso claim condizionale della sessione o un equivalente update
atomico; non effettua read-check-write Python sulla decisione concorrente.

### 8.3 Terminalizzazione unica

`victory`, `expired` e `admin_closed` convergono in una sola operazione
idempotente:

1. salvare e `flush()`-are l'eventuale turno vincente (`autoflush=False`);
2. vincere `running → finished` tramite `UPDATE ... WHERE status='running'`;
3. calcolare partecipanti e formula con letture scalari;
4. verificare User/Wallet e acquisire lock in ordine stabile;
5. inserire allocazioni univoche;
6. accreditare wallet e XP con aritmetica SQL relativa;
7. segnare il settlement `settled` o `void`;
8. lasciare il commit al chiamante.

Prima del commit un errore annulla stato, turno vincente, allocazioni, saldi, XP
e ledger. Dopo il commit stato e vincoli univoci rendono un replay innocuo. Una
notifica Telegram è best effort dopo il commit; un eventuale transactional
outbox non rientra in questa fase.

L'operazione restituisce un `TerminalResult` immutabile con esito e dati già
calcolati, senza usare `Bot`. Vittoria, callback manuale, scadenza difensiva e
scheduler seguono tutti lo stesso contratto: service → commit →
`publish_terminal(result)`. L'attuale helper manuale che chiude e invia Telegram
prima del commit viene quindi separato. Se il commit fallisce, nessun chiamante
pubblica un esito terminale.

### 8.4 Scadenza e scheduler

All'avvio viene creato un task interno `twentyq` con payload
`{"action":"expire"}`. Chiusure programmate amministrative continuano a usare
`{"action":"close"}`. Ogni interazione verifica comunque `expires_at`, perché
il polling può essere in ritardo.

Le chiusure cancellano i task pendenti della sessione quando possibile. Un task
duplicato o già terminale produce uno skip, non un errore operativo. Per
rispettare il post-commit Telegram, l'esecutore conserva il `TerminalResult`,
committa task e gioco, poi invoca la capability di pubblicazione; gli altri tipi
evento non cambiano comportamento.

Un errore non intenzionale durante un task interno `expire` non viene marcato
definitivamente `failed`. Lo scheduler fa rollback, incrementa `retry_count` e
riporta lo stesso task a `pending` con backoff persistente di 1, 2, 4, 8… minuti,
fino a un massimo di 60 minuti fra tentativi. Il creatore viene avvisato al primo
errore e periodicamente dopo; il retry continua fino a terminalizzazione o
intervento operativo. Se anche il salvataggio del retry fallisce, il task
originale rimane pending e il tick successivo lo riprende. Questa semantica è
attivata soltanto per `payload.internal=true` e `action=expire`; gli altri errori
scheduled restano `failed` come oggi.

## 9. Catena AI strutturata

### 9.1 Confine autorevole

La catena AI classifica **soltanto le domande**. Non seleziona il gioco, non
riceve i campi autorevoli `answer`/`aliases` e non giudica i tentativi. Riceve il
dossier necessario, che per sua natura può rendere riconoscibile il gioco.
L'output ammesso rimane il solo enum chiuso
`si | no | forse | usa_risposta`; il testo Telegram è costruito localmente.
`usa_risposta` è un rifiuto gratuito, non un tentativo. Questo elimina per
costruzione sia leakage testuale sia pagamenti decisi da un modello.

`StructuredAIProvider` resta la porta comune. Gli adapter Gemini, Groq e
OpenRouter implementano una singola richiesta; retry, fallback, deadline e
circuit breaker appartengono al router, non alle strategie.

Ordine predefinito:

1. Gemini free, modello configurabile;
2. Groq free, default `openai/gpt-oss-20b`, JSON Schema strict;
3. OpenRouter paid, default `deepseek/deepseek-v4-flash-0731`, JSON Schema
   strict.

Il modello Groq del gioco è separato sia da `groq_model` entertainment sia da
`groq_judge_model` dei Guess Game. `ALDUINO_PROVIDER` continua a governare solo
la chat conversazionale.

### 9.2 Fallback ed errori

Si passa al provider successivo per:

- quota/rate limit;
- timeout, rete e 5xx;
- chiave mancante o autenticazione/configurazione errata;
- rifiuto, risposta vuota, JSON o schema invalido;
- enum estraneo o output oltre i limiti.

Ogni provider viene tentato una volta. Default: timeout 8 secondi per Gemini e
Groq, 12 per OpenRouter, deadline complessiva 25 secondi. Sono valori
configurabili.

Un circuit breaker in memoria onora `Retry-After`; senza header usa 60 secondi
per rate limit/transienti e 15 minuti per quota o configurazione. Il suo stato
non è business data: un riavvio può azzerarlo senza compromettere correttezza.

Se falliscono tutti, l'utente riceve un errore temporaneo neutro; lease e quota
restano integri. Gli errori tecnici completi vanno solo nei log strutturati.

### 9.3 Prompt e contesto

Il prompt contiene esclusivamente:

- dossier canonico immutabile, senza i campi separati `answer` e `aliases`;
- domanda corrente troncata a 500 caratteri e marcata come dato non attendibile;
- contesto deterministico entro un doppio limite di turni e caratteri.

Non si promette di redigere dal testo naturale ogni occorrenza del titolo: i
dossier integrati, CSV o IGDB possono contenerla o renderla ovvia. La barriera
di sicurezza è non inviare i campi autorevoli separati e accettare dal modello
soltanto l'enum chiuso, che non dispone di un canale testuale per divulgarli.

Il contesto seleziona gli ultimi turni unici e, quando utile, precedenti con
overlap lessicale, poi li riordina cronologicamente. Default: massimo 24 turni e
12.000 caratteri complessivi. Il ledger DB resta completo e non viene riassunto
da un altro modello.

System prompt e JSON utente restano separati. Nessun display name, username,
Telegram ID, group ID o messaggio estraneo al gioco lascia il bot. Corpi grezzi,
thought e prompt non vengono loggati.

### 9.4 OpenRouter

La corsia a pagamento estende le protezioni già presenti:

- prenotazione atomica del worst case prima della rete;
- contabilizzazione dell'uso reale o conservativa su esito incerto;
- `response_format` JSON Schema strict;
- `require_parameters=true`;
- `data_collection=deny` e ZDR obbligatorio;
- reasoning escluso/minimo, temperatura bassa e output corto;
- modello consentito e tetti prezzo espliciti;
- nessun fallback OpenRouter verso modelli fuori lista.

Il cap interno globale è 5 USD/mese. Due budget di feature, applicati
atomicamente insieme al globale, riservano:

- 4 USD a `twentyq`;
- 1 USD a chat e funzioni secondarie OpenRouter.

La somma dei cap feature non può superare il globale. Il limite mensile della
chiave OpenRouter resta 5 USD come ultima barriera esterna; auto top-up e carta
sono configurazioni dell'account, non del bot.

Un cap globale o di feature pari a zero **disabilita** quella corsia paid; non
disattiva mai la contabilità. L'audit dei tentativi free è best effort e non può
bloccare un verdetto valido, mentre prenotazione e settlement dei costi
OpenRouter restano fail-closed e autorevoli.

### 9.5 Calibrazione locale

Un comando CLI esplicito riproduce un dataset versionato di dossier, cronologia,
domanda e verdetto atteso. Non crea sessioni, turni, quote o ricompense. Può
eseguire un provider o l'intera catena e produce soltanto metriche aggregate:

- conformità schema;
- accuratezza sui casi etichettati;
- coerenza con domande precedenti;
- latenza;
- fallback e classi di errore;
- token e costo.

Le API reali non vengono mai chiamate da pytest o CI. OpenRouter viene incluso
solo con flag esplicito e dopo il controllo del budget.

Riferimenti correnti: [Groq Structured Outputs](https://console.groq.com/docs/structured-outputs),
[Groq rate limits](https://console.groq.com/docs/rate-limits),
[OpenRouter Structured Outputs](https://openrouter.ai/docs/guides/features/structured-outputs),
[OpenRouter ZDR](https://openrouter.ai/docs/guides/features/zdr) e
[DeepSeek V4 Flash](https://openrouter.ai/deepseek/deepseek-v4-flash-0731).
Modelli, prezzi e limiti sono configurazione operativa da rivalidare prima del
deploy, non invarianti hardcoded nel dominio.

## 10. Error handling e sicurezza

- Provider indisponibile: nessun turno, nessuna quota consumata.
- OpenRouter senza budget o contabilità non verificabile: paid lane fail-closed;
  la partita resta attiva.
- Risposta AI malformata: scartata; il corpo non raggiunge log o utenti.
- Lease perso: risposta ignorata e turno non contato.
- Scadenza durante una richiesta: la terminalizzazione vince; il completamento
  tardivo non scrive.
- Errore durante payout: rollback totale; vittoria/admin close sono ritentabili
  dal chiamante, mentre expiry usa il retry persistente dello scheduler.
- Crash dopo commit e prima di Telegram: economia corretta; card recuperabile da
  `/gioco_alduino` o hub admin.
- Card non editabile: nuova card e anchor move condizionale.
- Nessun provider configurato: l'avvio di una nuova partita viene rifiutato
  senza cambiare `ready`.
- Segreti `.env`: mai stampati, committati o inclusi nelle fixture.

## 11. Strategia di test

Ogni modifica comportamentale segue RED → GREEN → refactor. Provider reali sono
sostituiti da fake deterministici.

### 11.1 Unitari

- formula default/custom, basis point, overflow, minimo, divisione e resto;
- normalizzazione e duplicati di domande/tentativi;
- guard locale e verdetto `usa_risposta` contro tentativi mascherati da domanda;
- conteggio quote personali e partecipazione;
- renderer condiviso e tutte le ragioni terminali;
- parser durata e scadenza;
- router provider, timeout, breaker e classificazione errori;
- schema strict, enum invalido, limiti prompt e assenza di PII/segreto;
- budget globale e ripartizione 4/1;
- help registry, manuale, deep-link e menu comandi.

### 11.2 Integrazione SQLite

- creazione v2 e snapshot policy;
- flussi domanda, cache, tentativo e quote;
- vittoria/expiry/admin close con allocazioni;
- sessioni legacy 20/3 invariate;
- scheduler e post-commit Telegram con bot fake;
- migrazioni applicate due volte.

### 11.3 PostgreSQL reale

- due azioni simultanee sul quinto quesito o secondo tentativo dello stesso
  utente;
- stesso input concorrente e vincolo univoco sull'hash normalizzato;
- due vincitori, vittoria contro expiry e vittoria contro admin close;
- risposta provider dopo recupero lease o chiusura;
- settlement ripetuto: una sola allocation e un solo ledger per utente;
- eccezione dopo il k-esimo credito: rollback di stato, saldi, XP, allocation e
  ledger, seguito da retry riuscito;
- User/Wallet mancante: nessun pagamento parziale;
- settlement sovrapposti sugli stessi wallet senza deadlock;
- parità fra delta wallet, ledger e somma delle allocazioni;
- zero partecipanti, resto e XP-only close;
- retry persistente dell'expiry dopo un payout fallito e ripresa dopo restart;
- nessuna pubblicazione Telegram prima del commit in tutti i terminal path;
- migrazione da schema precedente con righe ready/running/finished.

I test usano un PostgreSQL 16 usa-e-getta dedicato, senza volume, database
`gamingbot_test` su `127.0.0.1:5433`. `TEST_PG_URL` viene passato soltanto a
pytest; `DB_URL` del bot non viene mai puntato al database distruttivo dei test.
La fixture continua a rifiutare database il cui nome non termini con `_test`.

### 11.4 Gate finali

- suite completa con coverage almeno 99%;
- marker `pg` su PostgreSQL 16;
- `ruff check src/ tests/`;
- mypy configurato;
- import smoke;
- test migrazioni PostgreSQL;
- confronto coi comandi correnti di GitHub Actions.

## 12. Configurazione e rollout locale

Variabili nuove o esplicitate, con valori non segreti:

```dotenv
TWENTYQ_V2_ENABLED=true
TWENTYQ_PROVIDER_ORDER=gemini,groq,openrouter
TWENTYQ_GEMINI_MODEL=gemini-3.5-flash
TWENTYQ_GROQ_MODEL=openai/gpt-oss-20b
TWENTYQ_OPENROUTER_MODEL=deepseek/deepseek-v4-flash-0731
TWENTYQ_OPENROUTER_BUDGET_USD=4.00
OPENROUTER_OTHER_BUDGET_USD=1.00
AI_MONTHLY_BUDGET_USD=5.00
TWENTYQ_MAX_COINS_PER_PARTICIPANT=1000
```

Le chiavi sono `GEMINI_API_KEY`, `GROQ_API_KEY` e `OPENROUTER_API_KEY`, impostate
solo nella `.env` gitignored. L'ordine salta provider senza chiave. La
configurazione valida che ordine, modelli e somma budget siano coerenti.

Sequenza locale:

1. allineamento già effettuato di `test_giu` a `test`;
2. PostgreSQL/Redis runtime tramite Docker Compose;
3. PostgreSQL test separato e suite completa;
4. migrazioni e bot con provider fake;
5. preflight del token Telegram di test, senza mostrarlo né modificare webhook
   non autorizzati;
6. eval reale Gemini e Groq;
7. eval OpenRouter esplicitamente abilitato e sotto budget;
8. partita privata v2, verifica di quote, fallback, expiry e settlement;
9. osservazione dei log senza prompt prima di qualsiasi deploy.

Il deploy abilita prima migrazioni e lettura legacy, poi la feature v2 per nuove
partite. Un rollback applicativo lascia colonne/tabelle additive innocue e i
dati legacy leggibili.

## 13. Criteri di completamento

Il lavoro è completo quando:

- nessuna nuova partita usa cap globali;
- 5/2 è applicato per utente anche sotto concorrenza;
- duplicati e fallimenti non consumano quota né qualificano partecipanti;
- ogni partecipante riceve esattamente 10 XP a qualsiasi chiusura running;
- una vittoria paga una quota CoInn identica a tutti, una sola volta;
- expiry/admin close non pagano CoInn;
- nessun modello decide segreto, vittoria o premio;
- un titolo proposto come domanda non aggira quota o penalità dei tentativi;
- la catena gratuita-first e i budget 4/1 sono verificati;
- le sessioni legacy continuano col comportamento precedente;
- card, help e creazione derivano dalla stessa policy;
- migrazioni, suite SQLite/PostgreSQL, coverage e gate statici sono verdi;
- il bot gira localmente con Docker e le API reali sono state calibrate senza
  esporre segreti.
