# aiogram-dialog: converrebbe? — analisi

**Data:** 2026-08-01 · **File non tracciato**, materiale di valutazione.

Stato attuale: solo `aiogram==3.13.1`. Nessun `aiogram_dialog`, nessun `DialogManager`, zero
occorrenze in `src/` e `tests/`. Ultima versione della libreria: **2.6.0** (marzo 2026),
compatibile con aiogram 3.x.

La prima risposta che ho dato — «non conviene» — era una risposta sul **costo**, non sul
beneficio. Questa è l'analisi che mancava.

---

## 1. Quanto codice è, oggi, impianto UI

Misurato, non stimato:

| cosa | quantità |
|---|---|
| righe in `src/handlers/` | 10.909 |
| righe in `src/keyboards/` | 475 |
| handler `@router.callback_query` | 120 |
| handler `@router.message` | 91 |
| `StatesGroup` | 20 |
| `InlineKeyboardBuilder()` istanziati | 69 |
| chiamate `edit_message_text` / `edit_text` | 74 |
| punti che fanno il parsing di `callback.data` | 50 |
| guardie `isdigit()` su id presi da una callback | 20 |
| `show_alert=True` (in larga parte: bottone stantìo) | 84 |
| punti di paginazione a mano | 4 (in 3 file) |

Non tutta questa roba sparirebbe. Ma **è la superficie su cui la libreria lavora**, ed è grande.

## 2. Dove porterebbe un vantaggio vero

### 2.1 Il pannello unico — il vantaggio più concreto

`_panel()` in [creation.py:492](../../src/handlers/guess/creation.py#L492) è, letteralmente, un
`MessageManager` scritto a mano: tiene `card_message_id` nello stato FSM, prova `edit_message_text`,
e se Telegram rifiuta (messaggio vecchio, cancellato, contenuto identico) ricade su un invio nuovo
aggiornando l'id. È scritto bene ed è testato — ma è infrastruttura, e la libreria ce l'ha nativa.

**Il bug di stamattina è la prova.** Sostituendo l'immagine dalla scheda, il pannello veniva
modificato *sul posto*, quindi restava **sopra** i due messaggi nuovi (upload + eco): l'admin
vedeva una foto senza bottoni e il flusso sembrava morto. Con aiogram-dialog quel bug **non è
esprimibile**: il media sta *dentro* la finestra (`DynamicMedia`), scheda e foto sono **un solo
messaggio**, e non c'è nessun ordine di messaggi da azzeccare.

Questa non è una classe di bug chiusa: è una classe di bug che *si ripresenta* ogni volta che una
schermata nuova mette messaggi in chat.

### 2.2 Il parsing delle callback e i bottoni stantii

Oggi ogni schermata inventa la sua grammatica — `guess_new:edit:<campo>`, `ev:sched:<tipo>:<id>:close`,
`quiz_edit:nav:<id>:<i>` — e ogni handler la ri-parsa e la ri-valida: 50 punti di parsing, 20
`isdigit()`, e una fetta degli 84 `show_alert` che dicono «Scheda scaduta, ricomincia» o «Quel
numero non è disponibile».

Con i widget, l'id lo genera la libreria e il dato vive nel **contesto del dialogo**, non nella
stringa del bottone. Sparirebbero: il parsing, il limite dei 64 byte, e gran parte della difesa
contro il bottone premuto da una schermata di tre modifiche fa — perché quella schermata non esiste
più, il dialogo ha una sua identità e uno stack.

### 2.3 Le tastiere

69 `InlineKeyboardBuilder()` più 475 righe di `keyboards/`. Il grosso è dichiarativo già adesso
(«questi bottoni, disposti 2-2-1»), quindi la traduzione in `Group`/`Row`/`Column` è quasi
meccanica e **si accorcia**: le tastiere condizionali — quelle piene di `if status == "running"`,
come [guess_type.py:146-164](../../src/handlers/event_types/guess_type.py#L146) — diventano widget
con `when=`, che è una riga invece di un ramo.

### 2.4 Le liste che scorrono

Solo 4 punti (dashboard admin, editing quiz, lista quiz), ma sono i più noiosi: `ScrollingGroup` li
fa sparire. Vantaggio piccolo perché il progetto ne ha pochi — non è questo che sposta l'ago.

### 2.5 Il registro `FIELDS`: già a metà strada

[creation.py:227](../../src/handlers/guess/creation.py#L227) descrive ogni campo con etichetta,
prompt, parser e renderer, e un solo handler li serve tutti. **È già la stessa idea**: dichiarare i
campi invece di scrivere un handler per campo. Chi l'ha scritto è arrivato per conto suo a
un pezzo di aiogram-dialog.

Questo taglia in due sensi: dimostra che il pattern è quello giusto, **e** che il guadagno lì è
minore di quanto sembri — quel lavoro è già fatto e già pagato.

## 3. Dove non porterebbe niente

### 3.1 Mai, nemmeno riscrivendo

- **`services/`** — SQL, denaro, XP, transizioni di stato. Non lo tocca. È dove sta il valore.
- **I giochi in gruppo** — `quiz/play.py`, `guess/play.py`, `betting.py`: quiz a poll Telegram,
  tentativi che arrivano come messaggi liberi in privato, annunci nel gruppo. Un dialogo possiede
  **un** messaggio in **una** chat con **un** utente; questi flussi sono guidati dagli eventi e
  vivono in gruppo. Piegarli a dialoghi vuol dire cambiare il gioco per far contenta la libreria,
  e il gioco è il prodotto.
- **`fun_ai.py`** (387 righe) — otto comandi, nessuna UI.
- **Scheduler, backup, moderazione, deep link, `/help`, onboarding.**
- **`handlers/errors.py`** su `dp.errors`.

Regola pratica: aiogram-dialog paga dove un admin **naviga**, non dove un utente **gioca**.

### 3.2 «Ortogonale» solo finché non lo si tocca: l'hub eventi

Prima avevo messo `handlers/event_types/` fra le cose fuori portata, guardando com'è **adesso**.
È una risposta incompleta: è fuori portata *a contratto invariato*.

Oggi il protocollo `EventType` espone `render_detail(item)`, che ritorna **testo già impaginato +
tastiera già costruita**. Finché è così, ogni tipo-evento si porta dietro la propria
impaginazione e l'hub non può che stampare quello che riceve — quindi sì, ortogonale.

Se quella firma ritornasse **i dati** invece del messaggio (un dict: titolo, stato, contatori,
azioni disponibili), l'hub diventerebbe **un solo dialogo** con finestre parametriche, e un
tipo-evento nuovo smetterebbe di dover sapere come si disegna una scheda. Il registro resta —
è la scelta giusta e non si tocca — ma la sua superficie di presentazione sparisce.

È un cambio contenuto (una firma nel protocollo, più i due tipi che la implementano) con una resa
alta. **Va nella lista dei candidati**, non fuori.

## 3-bis. E Redis? — la correzione al punto sul costo dei render

Avevo scritto che i `getter` girano a ogni ridisegno, non a ogni evento, e che una schermata con
una query per click ne farebbe una per render. Vero, ma raccontato male: Redis c'è, e cambia la
risposta a metà.

**Cosa fa Redis qui, di preciso.** `redis==5.2.0` è in `requirements.txt`, `docker-compose.yml`
lo tira su (`redis:7-alpine`, 64 MB, `allkeys-lru`) e `.env.example` imposta `FSM_STORAGE=redis`.
`main._build_storage()` lo usa come **storage FSM**: ci finiscono stato e dati della conversazione.
**Non è una cache di query**: nessuna riga di `services/` passa da lì, e non esiste caching dei
dati letti dal DB in tutto il progetto.

Quindi:

- **Il costo per-render è di Postgres, e Redis non lo tocca.** Un `getter` che fa
  `select(...)` a ogni ridisegno colpisce il DB, punto.
- **Ma il rimedio passa da Redis lo stesso.** In aiogram-dialog i dati si mettono in
  `dialog_manager.dialog_data`, cioè nel contesto del dialogo — che è **esattamente** ciò che
  vive nello storage FSM, quindi in Redis. Si legge dal DB quando il dato cambia e si ridisegna
  dalla cache del dialogo. È il pattern normale della libreria, non un trucco.
- **Con `FSM_STORAGE=redis`, lo stack dei dialoghi sopravvive al riavvio.** Watchtower ricrea il
  container a ogni immagine nuova: con `MemoryStorage` ogni scheda aperta muore lì, con Redis no.
  Per una UI a dialoghi questo vale più che per l'FSM di adesso.

**Ma prima va sciolto un nodo che esiste già oggi, indipendentemente da questa libreria.**
`STEERING.md:74` dice, in grassetto, «resta `memory`, ed è una scelta, non una svista», con una
ragione precisa: `_build_storage` intercetta solo l'`ImportError` del pacchetto, **non** una
connessione fallita — quindi con Redis irraggiungibile il bot non degrada, **non parte**.
Solo che `.env.example:13` spedisce `FSM_STORAGE=redis`, e `redis` è fra le dipendenze installate.
**Il documento normativo e l'esempio che un deployer copia dicono cose opposte**, e chi ha ragione
dipende dal `.env` di produzione, che da qui non si vede.

Da chiarire comunque, migrazione o no. Se la risposta è «in produzione gira su Redis», allora
STEERING §2 è da correggere e il fallback su connessione fallita è un difetto aperto — con i
dialoghi diventerebbe più grave, perché lì nello storage non c'è solo lo stato: c'è la UI.

## 4. Dove costerebbe, e non poco

### 4.1 I test — il costo vero

2067 test. Quelli sulle schermate chiamano gli handler **come funzioni normali**, passando
`_Msg`/`_Cb` finti e leggendo `bot.screen` per sapere cosa vede l'admin. È un impianto costruito
apposta, veloce (~70s tutto), e verifica cose non banali: «tre modifiche non postano una seconda
scheda», «il media si manda una volta sola».

Con aiogram-dialog quella logica non è più in funzioni chiamabili: sta in finestre dichiarative e
nei `getter`. Si testa con `aiogram_dialog.test_tools.MockMessageManager`, che è un impianto
**diverso**. Non è un adattamento, è una riscrittura della parte più preziosa della suite. E la
copertura è a gate 99%.

### 4.2 Due paradigmi in casa

Una migrazione parziale — l'unica sensata — lascia per mesi metà bot a dialoghi e metà a handler.
Chi legge deve conoscere entrambi. Va messo in conto, non nascosto.

### 4.3 STEERING

Gli invarianti del pannello sono normativi (§19.b: «la scheda è **un** messaggio», l'eccezione del
media, la pulizia dei messaggi morti). Andrebbero riscritti, non cancellati: le *ragioni* restano
valide, cambia chi le applica.

### 4.4 Una dipendenza in più, con un manutentore solo

`aiogram_dialog` è sostanzialmente un progetto a singolo autore. Non è un difetto — è un rischio da
mettere sul piatto: ci si lega alla sua cadenza di rilascio per la compatibilità con aiogram.

## 5. Risposta secca alle tue domande

> **Non snellisce?**

Sì, ma **non uniformemente**. Snellisce dove il codice è impianto UI ripetuto: pannello,
parsing callback, tastiere condizionali, difesa dai bottoni stantii. Non snellisce services,
giochi, scheduler, registro eventi — cioè la maggioranza.

> **Non semplifica, anche solo per arrivare dove siamo?**

Per **arrivarci**, no: siamo già arrivati, e ripagare un lavoro fatto non è semplificare.
Per **quello che viene dopo**, sì: ogni schermata nuova costerebbe meno, e una classe di bug
(ordine dei messaggi, pannello sepolto, bottone di una schermata morta) smetterebbe di esistere
invece di essere evitata a mano ogni volta.

> **Dove porterebbe vantaggi parzialmente?**

In ordine di resa, dal più conveniente:

| # | area | perché | righe oggi | serve toccare il contratto? |
|---|---|---|---|---|
| 1 | `handlers/guess/creation.py` | media **dentro** la finestra: uccide alla radice il bug di stamattina. 11 callback, 7 stati, il pannello a mano | 898 | no |
| 2 | `handlers/admin_dashboard.py` | 18 callback di pura navigazione ad albero: il caso da manuale | 675 | no |
| 3 | `handlers/quiz/creation.py` + `quiz/editing.py` | 26 callback, navigazione fra domande, paginazione a mano | 1.129 | no |
| 4 | `handlers/shop.py` | 13 callback: sfoglia catalogo → conferma acquisto | 457 | no |
| 5 | hub eventi + `event_types/` | un dialogo solo con finestre parametriche al posto di N impaginazioni | 385 + 415 | **sì**: `render_detail` deve ritornare dati, non messaggi (§3.2) |

**Fuori** dai candidati, e non per pigrizia ma per natura: tutti i `play.py`, `betting.py`,
`fun_ai.py`, scheduler, backup, moderazione (§3.1).

## 6. Cosa farei

Non una migrazione. **Uno spike su `guess/creation.py`**, cioè il caso #1: è il file più grosso,
è quello con il media, ed è quello che ha appena prodotto un bug che la libreria renderebbe
impossibile.

Lo spike risponde alle uniche tre domande che contano davvero, e nessuna delle tre si risponde a
tavolino:

1. Le righe **calano davvero**, o si spostano soltanto in `getter` e widget?
2. Quanto costa **riscrivere i test** di quel flusso con `MockMessageManager`? Moltiplicato per
   cinque aree, è il conto vero della migrazione.
3. Quante query fa davvero un flusso a regime, mettendo i dati in `dialog_data` invece di
   rileggerli a ogni render (§3-bis)? Si misura contando gli statement, non discutendone.

Se lo spike va bene, i candidati 2-4 seguono uno alla volta. Il **5 va per ultimo comunque**: è
l'unico che chiede di cambiare un contratto (`render_detail`), quindi si affronta quando il resto
ha già dimostrato che la libreria regge — non prima. Se lo spike va male, si è perso un flusso,
non il bot — e la risposta è documentata invece che opinata.

**Da chiarire subito, comunque, perché non dipende da questa decisione:** `STEERING.md:74` dice
`memory`, `.env.example:13` dice `redis`. Uno dei due è sbagliato oggi (§3-bis).

**Da non fare adesso, comunque:** manca una settimana e mezza allo spegnimento di
`llama-3.3-70b-versatile` (16 agosto). Prima quello va in produzione e si guarda come si comporta
qwen sul campo; poi, semmai, lo spike.
