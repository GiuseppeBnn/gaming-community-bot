# Fondamenta della presentazione — design

**Data:** 2026-08-03 · **Branch:** `test_giu` · **Sostituisce come lavoro attivo:**
[2026-08-02-refactoring-aiogram-dialog-design.md](2026-08-02-refactoring-aiogram-dialog-design.md),
il cui gate ha detto **stop** (§10 di quel documento).

---

## 0. Come si riprende, se la sessione è nuova

1. Leggi questo file per intero. È scritto per essere autosufficiente: i fatti hanno accanto il
   comando per ri-verificarli, perché i numeri invecchiano e le sessioni si compattano.
2. Leggi la **tabella di stato** (§10) per sapere dove siamo.
3. Leggi `CLAUDE.md` e `STEERING.md`: restano normativi. Questo documento **non** deroga a nessuna
   loro regola tranne quelle che dichiara esplicitamente di riscrivere (§3.2).
4. Il piano eseguibile, quando esiste, sta in `docs/superpowers/plans/`.

**Cosa è già successo, in due righe.** L'analisi del 2026-08-01
([analisi-aiogram-dialog.md](../analisi-aiogram-dialog.md)) chiedeva se convenisse adottare
`aiogram-dialog`. Una Fase 0 di fondamenta e una fetta verticale di prova hanno risposto: la
libreria funziona in questo stack, e costa più di quanto renda. La fetta è stata rimossa
(commit `93de43a`). **Questo documento riparte dalla stessa diagnosi e la risolve in casa**, con
quello che aiogram e la stdlib danno già.

---

## 1. La diagnosi

Il problema non è che manca un framework. È che **questo progetto ha inventato le astrazioni giuste
e non le ha condivise.**

- `_panel()` in `handlers/guess/creation.py:499` è un gestore di pannello scritto bene, testato, e
  usato **in un file solo**.
- Il registro `FIELDS` in `handlers/guess/creation.py:227` è dichiaratività fatta in casa — un
  handler solo che serve tutti i campi — e vive **in un file solo**.
- `forget_message`, che serve a chiunque tenga un messaggio in vita, sta in
  `handlers/guess/_shared.py:118`, cioè **dentro un gioco**.

Il risultato è che aggiungere una schermata costa come la prima volta, e che gli stessi bug tornano
perché non esiste un posto solo dove sistemarli.

### 1.1 I numeri, con il comando per ri-misurarli

Misurati il 2026-08-03 su `src/`, dopo la rimozione della fetta.

| cosa | quantità | comando |
|---|---|---|
| righe in `src/handlers/` | 10.909 | `find src/handlers -name '*.py' \| xargs wc -l \| tail -1` |
| righe in `src/keyboards/` | 475 | `find src/keyboards -name '*.py' \| xargs wc -l \| tail -1` |
| handler `@router.callback_query` | 120 | `grep -rn '@router.callback_query' src/handlers \| wc -l` |
| punti che parsano `callback.data` | 66 | `grep -rn 'callback.data' src/handlers \| grep -c 'split\|startswith\|\['` |
| guardie `isdigit()` | 20 | `grep -rn 'isdigit()' src/handlers \| wc -l` |
| `show_alert=True` | 84 | `grep -rn 'show_alert=True' src/handlers \| wc -l` |
| `InlineKeyboardBuilder()` | 69 | `grep -rn 'InlineKeyboardBuilder()' src/handlers src/keyboards \| wc -l` |
| **usi di `CallbackData`** | **0** | `grep -rn 'CallbackData' src \| wc -l` |
| grammatiche di callback distinte | ~20 | `grep -rhoE '"[a-z_]+:[a-z_]*' --include='*.py' src \| sed 's/"//' \| cut -d: -f1 \| sort \| uniq -c \| sort -rn` |
| file di test che usano finti `_Msg`/`_Cb` | 29 su 113 | `grep -rln 'class _Msg\|class _Cb\|bot.screen' tests \| wc -l` |

Le grammatiche più usate: `ev` (63), `adm` (60), `quiz_new` (53), `shop` (38), `admin_bet` (32),
`guess_new` (25), `quiz_edit` (22), `sched` (17), `bet` (15).

I file più grossi: `guess/creation.py` 898 · `betting.py` 774 · `quiz/creation.py` 758 ·
`admin.py` 717 · `admin_dashboard.py` 675.

**Il pannello, implementato tre volte con quattro nomi:** `bet_active_msg_id`
(`handlers/betting.py`, 4 punti) · `card_message_id` + `media_message_id`
(`handlers/guess/creation.py`) · `kb_message_id` (`handlers/guess/play.py`).

### 1.2 Base di partenza

`pytest` → **2098 passed, 30 skipped**. Coverage **99,67 %** (gate `fail_under = 99`).
`ruff check src/ tests/` e `mypy` a **zero findings**. Branch `test_giu`, albero pulito.

---

## 2. Scope

Questo documento copre **il lavoro A: le fondamenta della presentazione**. I quattro dolori che
l'utente ha indicato sono stati decomposti in tre lavori, perché non stanno in un piano solo:

| | lavoro | copre | quando |
|---|---|---|---|
| **A** | fondamenta della presentazione | «ogni schermata ricomincia da zero», «gli stessi bug tornano», e come **conseguenza** una fetta di «i file sono troppo grossi» | **adesso, questo documento** |
| **B** | split dei file grossi | «i file sono troppo grossi», per quel che resta dopo A | dopo A, guidato dai confini che A scopre |
| **C** | inline search | «la navigazione è scomoda», dove paga davvero | indipendente da A e B, ha un prerequisito di sicurezza (§9.1) |

**B va dopo A e non prima**, per un motivo, non per gusto: una fetta della ciccia dei file *è*
l'impianto reinventato. Spezzarli prima significa distribuire codice duplicato su più file — più
file, stessa duplicazione.

---

## 3. Vincoli

### 3.1 Mano libera, ma con una rete che resta tesa

L'utente ha scelto esplicitamente **mano libera**: conta il risultato finale, le schermate possono
cambiare forma, i test delle schermate si riscrivono insieme al codice.

Questo **non** significa lavorare senza rete. Durante A.1 restano invariati i risultati osservabili
su denaro, XP, gating admin, ordine dei router e transizioni di stato. I test che invocano
direttamente un callback handler possono essere adattati per passare l'oggetto `CallbackData`
tipizzato iniettato da aiogram; le loro asserzioni economiche e di autorizzazione devono restare
equivalenti.

I file di test non sono immutabili per principio. Un test su denaro o XP può cambiare quando serve
a esprimere meglio la stessa garanzia oppure, nel successivo audit dedicato, a riprodurre un difetto
concreto. A.1 non cambia importi, formule, cap giornalieri, classificazioni capped/uncapped o regole
di business. Un cambiamento di quella natura richiede il processo separato descritto in
[`2026-08-04-audit-denaro-xp-design.md`](2026-08-04-audit-denaro-xp-design.md).

Restano obbligatori i gate: `pytest` verde, coverage ≥ 99%, `ruff` e `mypy` senza findings prima di
ogni commit. Un test economico, di gating o di ordine router rosso durante A.1 è una regressione da
correggere, non un nuovo risultato atteso.

### 3.2 Cosa cambia in STEERING, e cosa no

Restano validi e non si toccano: la sessione iniettata come `db_session` (§4), i service che non
committano (§5), l'ordine dei middleware (§6) e dei router (§7), il denaro deciso in SQL (§22
regola 22), `xp_service` unico mutatore (§12.1), `group_registry` invece di `settings.group_id`
(§13), il check admin via `filters.admin_filter` (§8), il registro dei tipi-evento (§18.2).

**Vanno riscritti nello stesso commit che li cambia:**

- **§19.b** — gli invarianti del pannello («la scheda è **un** messaggio», l'eccezione del media, la
  pulizia dei messaggi morti). Le *ragioni* restano valide: cambia chi le fa rispettare, dalla
  disciplina di chi scrive al modulo `utils/panel.py` (§5).
- **§22 regola 20** — l'escaping HTML. La regola non cambia, ma guadagna un posto dove vivere: le
  viste pure (§6) *sono* il presentation layer, quindi l'escaping sta lì e da nessun'altra parte.

---

## 4. Sezione 1 — la grammatica delle callback

### 4.1 Cosa c'è oggi

```python
_, _, action, raw = callback.data.split(":")     # handlers/events.py:131
page = max(0, int(callback.data.split(":")[2]))  # handlers/admin_dashboard.py:268
```

Sessantasei punti come questi, venti guardie `isdigit()`, spacchettamento posizionale. Se qualcuno
aggiunge un segmento, la riga sbaglia in silenzio. Il limite dei 64 byte del payload si rispetta a
occhio.

### 4.2 Cosa ci va

`CallbackData`, la factory tipizzata che **aiogram ha già in casa** e che qui non è mai stata usata.
Una classe per famiglia di schermate — circa venti, una per ciascuna grammatica che oggi è una
convenzione non scritta:

```python
class EventCb(CallbackData, prefix="ev"):
    action: str
    task_type: str
    item_id: int


@router.callback_query(EventCb.filter(F.action == "open"))
async def open_event(callback: CallbackQuery, callback_data: EventCb, db_session) -> None:
    ...  # callback_data.item_id è già int, già validato
```

Muoiono: lo `split`, l'`int()`, le venti `isdigit()`, la classe di bug «ho contato male i due
punti», e il limite dei 64 byte diventa un errore in test invece di un bottone rotto in produzione.

### 4.3 Il cambio di comportamento, dichiarato prima e non dopo

Oggi una callback **malformata** arriva all'handler e riceve un `show_alert`. Con la factory il
parse avviene nel **filtro**: una callback malformata non fa match, quindi scivola oltre e finisce
nel fallback di `common`. Se nessuno risponde, l'utente resta con la rotellina che gira.

**Decisione:** serve un catch-all in `common.router` che risponda a qualunque `callback_query` non
gestita con un testo breve — **non** un `answer()` muto: quello toglie la rotellina ma lascia
l'utente a chiedersi se ha premuto o no. Testo fisso, italiano, sulla falsariga di «Questo bottone
non è più valido». Va scritto **insieme** alla prima conversione, non dopo, altrimenti si introduce
una regressione visibile mentre si pulisce.

`common.router` è già **ultimo** in `ROUTERS` (invariante asserito da `tests/unit/test_router_order.py`),
quindi il posto esiste già ed è quello giusto.

### 4.4 Cosa questa sezione **non** risolve

I bottoni **stantii**. `CallbackData` uccide il malformato, non lo scaduto: un bottone è stantìo
perché il messaggio vecchio è ancora lì, ed è un problema di pannello. Si chiude in §5, alla radice,
invece che con l'ennesimo `show_alert`.

### 4.5 Forma: una classe per famiglia, non per azione

Una classe per famiglia (`EventCb`, `ShopCb`, `QuizNewCb`…) con un campo `action`, filtrata con
`F.action == "…"`. L'alternativa — un prefisso e una classe per singola azione — moltiplicherebbe
le classi per 120 senza aggiungere niente. La forma scelta ricalca come il codice è **già**
organizzato.

### 4.6 Completezza verificata sulla struttura reale

Due guardie impediscono che factory e filtri divergano: nessun producer può ricostruire a mano un
prefisso già tipizzato e ogni azione costruita deve raggiungere almeno un filtro registrato. La
seconda guardia non può assumere che `action` sia l'unico campo obbligatorio: alcune factory
richiedono anche identificativi numerici.

Prima di aggiungere le factory restanti, il test costruisce quindi un'istanza valida per ogni classe
usando i campi dichiarati dalla factory. Assegna all'azione il valore sotto esame e agli altri campi
obbligatori sentinelle deterministiche compatibili con il tipo. Una prova per mutazione rimuove o
altera temporaneamente un filtro e deve rendere il test rosso; solo dopo il ripristino si procede
alle conversioni. In questo modo il guard verifica il cablaggio vivo, non soltanto la possibilità di
istanziare le classi più semplici.

---

## 5. Sezione 2 — un pannello solo

### 5.1 `utils/panel.py`, un modulo, non una gerarchia

Possiede tre cose che oggi sono sparse in tre file con quattro nomi (§1.1):

1. **L'id del messaggio nello stato FSM.** Una chiave per *nome* di pannello, con default `"panel"`:
   un flusso che ne tiene due aperti li distingue passando un nome, invece di inventarsi una chiave
   nuova come si fa oggi. Nota che il secondo id di `guess/creation.py` — `media_message_id` — non
   serve più: nel nuovo pannello il media sta **dentro** la finestra (foto con caption e tastiera,
   un messaggio solo), che è precisamente il punto di §5.2.
2. **Modifica-o-manda**, con il fallback già scritto in `_panel()` — che va spostato, non riscritto:
   Telegram rifiuta una modifica se il messaggio è vecchio, cancellato, o se il contenuto è
   identico, e quel caso è già gestito bene.
3. **Il passaggio testo ↔ media.**

### 5.2 Perché il punto 3 vale il modulo

Telegram **non può** trasformare un messaggio di testo in uno con foto modificandolo: va cancellato
e rimandato. Sbagliare quell'ordine è esattamente il bug che ha aperto tutta questa analisi — il
pannello modificato sul posto restava **sopra** i messaggi nuovi, e l'admin si trovava davanti una
foto senza bottoni con il flusso apparentemente morto.

Quella logica non è difficile. Il problema è che oggi sta in un file solo, e ogni schermata nuova
che mette un media in chat ha diritto di sbagliarla da capo. Scritta e testata **una volta**, quel
diritto sparisce — e con esso la fetta di bottoni stantii che esistono solo perché un messaggio
vecchio è rimasto in giro.

### 5.3 Vincoli che il modulo porta con sé

- **La caption di una foto sta in 1024 caratteri**, il testo in 4096. Una scheda con media ha un
  tetto più basso: va verificato in test, non scoperto in chat.
- `forget_message` si sposta da `handlers/guess/_shared.py` a `utils/panel.py`, dove tutti lo
  vedono. I chiamanti attuali si aggiornano nello stesso commit.

### 5.4 Cosa il modulo **non** copre

I quindici file che fanno `edit_message_text` **senza** tenere un id nello stato non hanno un
pannello: modificano il messaggio della callback che li ha appena chiamati, ed è già una riga sola.
Restano come sono. Il modulo serve ai flussi a più passi — creazione, editing, scommessa attiva —
non alla navigazione.

---

## 6. Sezione 3 — il rendering separato dall'I/O

### 6.1 Il problema

Un handler oggi fa tre mestieri nello stesso corpo: **decide**, **impagina**, **parla con
Telegram**. Da lì discendono tutti e tre i sintomi:

- i file da 900 righe;
- i 29 file di test che si portano dietro `_Msg`/`_Cb` finti e leggono `bot.screen` per sapere cosa
  vede l'admin;
- `render_detail` nel protocollo `EventType`, che ritorna **un messaggio già impaginato**: l'hub non
  può fare altro che stampare quello che riceve.

### 6.2 La mossa

Una vista è una funzione **pura**: dati dentro, `(testo, tastiera)` fuori. Niente `bot`, niente
`await`, niente I/O.

```python
def render_card(data: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    ...
```

L'handler torna a fare un mestiere solo:

```python
await panel.show(bot, chat_id, state, *render_card(data))
```

Niente classi base, niente registro di viste, niente motore di template. Una funzione che ritorna
una tupla. Se un'area cresce, le sue viste vanno in un `views.py` accanto all'handler; se è
piccola, restano nello stesso file separate da un commento. Non si impone.

### 6.3 Tre conseguenze, non tre lavori

**I test di cosa vede l'utente diventano chiamate di funzione normali.** `assert "Titolo" in
render_card(d)[0]`: niente finti, niente bot, niente async. Dei 29 file con i finti a mano restano
solo quelli che verificano davvero l'**I/O** («ha modificato o ha rimandato?»), che è il mestiere di
`utils/panel.py`: testato una volta, non ventinove.

**I file si spezzano lungo una linea che esiste già** invece di una inventata — vista pura da una
parte, handler sottile dall'altra. Il dolore «file troppo grossi» si chiude in buona parte come
conseguenza, ed è il motivo per cui il lavoro B viene dopo (§2).

**`render_detail` che ritorna dati invece di un messaggio** — il candidato #5 dell'analisi, quello
«ad alta resa» — smette di essere un cambio a sé: è questa stessa idea applicata a `event_types/`.
Un tipo-evento nuovo smette di dover sapere come si disegna una scheda. Il registro `EventType`
resta: è una scelta giusta e non si tocca (STEERING §18.2). Sparisce solo la sua superficie di
presentazione.

### 6.4 Da dove si parte: `admin_dashboard.py`

Diciotto callback di **pura navigazione**: niente media, niente denaro, niente wizard. Prova l'idea
delle viste senza confonderla con altro.

Partire da `guess/creation.py` — il più grosso, quello col media — proverebbe due cose insieme e, in
caso di guaio, non si saprebbe quale delle due ha ceduto. `guess/creation.py` viene **subito dopo**,
come conferma su un flusso a più passi, e da lì si continua con `quiz/creation.py` +
`quiz/editing.py`, poi `shop.py`, poi l'hub eventi (che è l'unico a cambiare un contratto, §6.3).

---

## 7. L'ordine di lavoro, e perché è quello

**Ibrido: orizzontale per le cose meccaniche, verticale per il ridisegno.**

1. **Orizzontale — `CallbackData` su tutto il bot** (§4), incluso il catch-all di `common` (§4.3).
2. **Orizzontale — `utils/panel.py`** (§5), con i tre chiamanti attuali convertiti.
3. **Verticale — le viste**, un'area alla volta (§6.4), verde prima di passare alla successiva.

Il motivo del passo 1 e 2 in orizzontale è preciso e viene dallo spike appena buttato: **una
grammatica condivisa è condivisa solo se la usano tutti.** Convertire le callback area per area
lascerebbe due grammatiche in casa per mesi — che è esattamente il costo dei «due paradigmi in
parallelo» per cui `aiogram-dialog` non è passato. Ripeterlo con codice nostro non lo renderebbe
più economico.

Il motivo del passo 3 in verticale è l'opposto: cambia la **forma** del codice e dei test, quindi
vuole una prova prima di essere generalizzato — o si finisce a progettare un'astrazione senza
consumatori.

### 7.1 Dove si è fermato davvero il primo piano

Il piano completato
[`2026-08-03-a1-callback-tipizzate.md`](../plans/2026-08-03-a1-callback-tipizzate.md) ha uno scope
più stretto di quello anticipato dalla prima versione di questa sezione: copre **solo una prima
ondata di A.1**, non A.1 intera, A.2 o A.3a.

La prima ondata ha consegnato il catch-all delle callback non gestite e ha convertito **2 delle 15
famiglie/file handler** inventariate per A.1: `handlers/schedule.py` e `handlers/events.py`. Per
`EventCb` ha convertito nello stesso lavoro anche **tutti i produttori attuali** trovati fuori da
`events.py` (`handlers/event_types/`, `handlers/guess/creation.py`, `handlers/quiz/editing.py` e
`keyboards/admin_dashboard_kb.py`), perché convertire un consumer lasciando un producer raw crea un
bottone morto anche con la suite verde.

La prima ondata dichiara 3 factory centrali: `SchedCb`, `EventCb` e `PollCreateCb`. Il follow-up ne
ha aggiunte 18: A.1 chiusa contiene quindi **21 classi**, non 18, per tutte le **15/15 famiglie di
handler** e tutti i producer callback correnti. La completezza resta protetta dalle quattro guardie
strutturali in `tests/unit/test_callbacks.py`: scan dei prefissi, assenza di wire payload manuali,
scan delle azioni e raggiungibilita' di ogni azione costruita da un filtro registrato.

Il follow-up
[`2026-08-04-a1-callback-tipizzate-restanti.md`](../plans/2026-08-04-a1-callback-tipizzate-restanti.md)
ha completato A.1 e basta. La chiusura ha riverificato l'assenza di parsing manuale in `handlers/`,
gli unici `F.data` come deny admin derivati dai prefissi delle classi, la parita' delle asserzioni
economiche/XP rispetto alla base `3726038`, i gate di routing e l'intera suite. I 30 skip dipendono
dall'assenza di `TEST_PG_URL`, non sono test disabilitati. `utils/panel.py` (A.2) e la prova delle
viste in `admin_dashboard.py` (A.3a) restano lavori non avviati e separati.

---

## 8. Fuori scope, dichiarato

- **Logica dei `services/` durante A.1**: SQL, denaro, XP e transizioni di stato non cambiano nella
  conversione delle callback. Saranno analizzati separatamente subito dopo A.1 secondo la spec
  dell'audit; non sono esclusi dal programma complessivo.
- **Ridisegno dei giochi in gruppo**: `quiz/play.py`, `guess/play.py` e `betting.py` vengono toccati
  in A.1 per convertire consumer e producer callback, ma il gameplay e i relativi valori non vengono
  ridisegnati.
- **`fun_ai.py`**: otto comandi, nessuna UI.
- **Ridisegno di scheduler e onboarding**: la loro conversione callback è in scope A.1; funzionalità
  ulteriori restano fuori scope. La factory onboarding sarà documentata nella sezione normativa
  esatta `STEERING.md` **§16.1 «Onboarding iniziale (`RulesCb`, prefisso `rules`)»**.
- **Backup, moderazione, deep link e `/help`**, salvo producer callback esplicitamente inventariati.
- **`handlers/errors.py`** su `dp.errors`.
- **Nessuna dipendenza nuova.** Tutto quel che serve è in aiogram o nella stdlib.
- **Alembic, `pg_insert`, `get_settings()`**: tre scelte già prese (STEERING §22), non si riaprono.

---

## 9. Rischi noti

### 9.1 `GroupGuard` non vede le `InlineQuery` — prerequisito del lavoro C

`_chat_type()` (`src/middlewares/group_guard.py:65-71`) riconosce solo `Message` e `CallbackQuery`.
Per una `InlineQuery` ritorna `None`, e il ramo «gate solo le chat private» la lascia **passare
senza controlli**. `BanGuard` reggerebbe (usa `event_from_user`), `GroupGuard` no.

**Conseguenza:** accendere la inline mode oggi significa che chiunque su Telegram, anche fuori dal
gruppo, può interrogare il bot. Va chiuso **prima** di qualunque handler `inline_query`, non insieme.

Non è un rischio del lavoro A — nessun passo di A tocca la inline mode — ma è scritto qui perché è
stato scoperto qui, e perché il lavoro C non deve ripartire senza saperlo.

### 9.2 La riscrittura dei test è dove la mano libera morde

Il passo 3 (§6) riscrive i test delle schermate area per area. La mitigazione è §3.1: la rete su
denaro, XP, gating e ordine router resta tesa, e ogni area chiude verde prima che si apra la
successiva. Se un'area si rivelasse più grossa del previsto, si ferma **lì**: le aree già convertite
restano, perché ognuna è indipendente dalle altre.

### 9.3 Il catch-all delle callback può mangiare troppo

Il catch-all di §4.3 vive in `common.router`, che è ultimo — quindi vede solo ciò che nessun altro
ha gestito. Il rischio non è che intercetti troppo, ma che **maschera** un handler rotto: una
callback che smette di fare match per un errore di conversione riceverebbe una risposta educata
invece di rimanere muta, e nessuno se ne accorgerebbe. **Mitigazione:** il catch-all logga a
`WARNING` con il payload, e da lì passa dagli alert admin (STEERING §26). Un bottone che smette di
funzionare si presenta da solo.

### 9.4 Il tetto di 1024 caratteri sulle caption

Vedi §5.3. Una scheda che oggi sta comoda in 4096 caratteri come testo potrebbe non starci come
caption di una foto. Va coperto da un test nel modulo `panel`, non lasciato al caso.

---

## 10. Tabella di stato — **aggiornare qui**

Una sessione nuova legge questa tabella per sapere dove siamo.

| passo | descrizione | stato |
|---|---|---|
| — | Design approvato (questo documento) | ☑ 2026-08-03 |
| — | **Piano A.1, prima ondata** — `2026-08-03-a1-callback-tipizzate.md`: catch-all + `schedule.py` + `events.py` e tutti i producer Events attuali (§7.1) | ☑ 2026-08-04 |
| A.1 | `CallbackData` su tutto il bot + catch-all in `common` (§4): **15/15 famiglie handler convertite**, 21 factory centrali e tutti i producer callback correnti convertiti | ☑ 2026-08-04 |
| — | **Follow-up A.1 completato** — `2026-08-04-a1-callback-tipizzate-restanti.md`: le 13 famiglie restanti + tutti i loro producer (§7.1) | ☑ 2026-08-04 |
| A.2 | `utils/panel.py` + i tre chiamanti convertiti (§5) | ☐ |
| A.3a | Viste — `admin_dashboard.py` (la prova, §6.4) | ☐ |
| A.3b | Viste — `guess/creation.py` (la conferma su un wizard) | ☐ |
| A.3c | Viste — `quiz/creation.py` + `quiz/editing.py` | ☐ |
| A.3d | Viste — `shop.py` | ☐ |
| A.3e | Viste — hub eventi + `event_types/` (**cambia `render_detail`**) | ☐ |
| B | Split dei file grossi, guidato dai confini scoperti da A | ☐ lavoro separato |
| C | Inline search, dove paga (prerequisito: §9.1) | ☐ lavoro separato |

Legenda: ☐ da fare · ▣ in corso · ☑ fatta · ✗ abbandonata (con il perché, in una riga sotto).

---

## 11. Il lavoro C, per non perderlo

Non è in scope qui, ma la valutazione è stata fatta e va conservata.

**Paga** dove si sceglie **uno da una lista lunga**: eventi, quiz da modificare, catalogo del
negozio. Lì cancella i quattro punti di paginazione a mano e un pezzo di albero di bottoni. È
nativa, non aggiunge dipendenze.

**Non paga** nei flussi di **creazione**: una query inline non ha stato, è una ricerca, non un
wizard.

**Vincoli, tutti da progettare prima e non durante:**

- va accesa in BotFather (`/setinline`); il feedback sul risultato scelto vuole anche
  `/setinlinefeedback`;
- parte **a ogni battitura**, quindi una query al DB per tasto se non si mette una cache;
- il risultato scelto viene **postato come messaggio**: in gruppo sarebbe pubblico;
- **`GroupGuard` non la vede** (§9.1). Questo va chiuso per primo.

L'utente ha posto la condizione: inline search **solo dove paga davvero e come aggiunta sicura**.
Un handler `inline_query` che non passi da un check di autorizzazione esplicito viola quella
condizione, indipendentemente da quanto sia comodo.
