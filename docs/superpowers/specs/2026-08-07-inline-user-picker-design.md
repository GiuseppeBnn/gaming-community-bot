# Inline mode — User Picker → Card profilo (design)

> **Stato:** design in revisione. Su questa base si scrive il piano di
> implementazione (`docs/superpowers/plans/2026-08-07-inline-user-picker.md`).

**Data:** 2026-08-07 · **Branch di destinazione:** `test_giu`

## 1. Perché (UX-first)

Il bot oggi ha zero inline mode (tutti i "inline" in `src/` sono `InlineKeyboard`).
I primi due dolori reali di una community:

1. **Raggiungere un utente per nome** è scomodo: serve reply, l'@esatto, o il tg_id
   numerico che nessuno ricorda. Chi **non ha username** è irraggiungibile per
   `/trasferisci` e per i target testuali.
2. **Mettere la card di un giocatore "sul tavolo"**: `/profilo` è privato e mostra solo
   il proprio. In gruppo non c'è modo di mostrare rank/trofei/saldo di un altro.

L'inline mode risolve entrambi con <b>discovery in chat → tap</b>:

```
@bot giu        →  [👤 Giu (🏆 Livello 7)  —  @giu · 🏆 12 trofei]   (a ogni battuta)
                        tap →
     nella chat spunta la card profilo completa di @giu (rank, XP, trofei, tag, saldo)
```

Per il momento la card è **l'unica azione** del picker: niente admin-picker, niente
share-giochi, niente catalogo (quei casi restano nel Lavoro C di
[2026-08-03-fondamenta-presentazione-design.md](2026-08-03-fondamenta-presentazione-design.md)).

## 2. Decisioni prese (con l'utente)

| Decisione | Scelta |
|---|---|
| Chi può usarlo | **Tutti gli utenti** (members-only: serve il gate GroupGuard) |
| Card profilo | **Tutto incluso, saldo incluso** (CoInn visibile in chat) |
| Ricerca | **Match parziale a ogni battuta** (ilike username/full_name) |
| Output | Solo **card profilo** postata in chat; niente altre azioni inline in v1 |
| Codice | Poche ripetizioni, manutenibile; niente spaghetti |
| Esito | Solo il **piano** in markdown, niente codice ora |

## 3. Architettura

### 3.1 Flusso

```
InlineQuery (chat qualunque)
  → middleware: RateLimit (budget inline) → DbSession (upsert atore, già ok) →
    BanGuard (già ok) → GroupGuard (FIX: gate membership obbligatorio)
  → handler inline_mode: router.inline_query
      normalizza query (strip @, trim, lowercase)
      cache lookup (query.lower() → risultati, TTL 3s, LRU bound)
      se vuoto → hint "scrivi un nome…"
      se <2 char → hint "altre lettere…"
      altrimenti → search_users(session, q, limit=20)
      costruisce card con renderer condiviso (utils/profile_view)
      answer_inline_query(results, cache_time=2, is_personal=True, switch_pm=…)
tap sull'articolo
  → InlineQueryResultArticle postata in chat "via @bot" (già fatta da Telegram)
  → ChosenInlineResult (solo se /setinlinefeedback) → v1: nessun handler
```

### 3.2 Componenti

**A. Infrastruttura di sicurezza (prerequisito, indipendente dalla feature)**

1. `src/middlewares/group_guard.py`:
   - `_chat_type()` oggi riconosce solo `Message`/`CallbackQuery`; per `InlineQuery`
     ritorna `None` → il gate "solo chat private" la lascia passare senza controlli
     (spec esistente §9.1).
   - Fix: per `InlineQuery` **il gate è sempre membri-del-gruppo**, indipendentemente
     dal `chat_type` della query (il campo `chat_type` dell'InlineQuery non è affidabile
     per decidere: chiunque può digitare `@bot` da una chat straniera). Si riusa
     `_is_group_member` (cache già esistente, `GROUP_MEMBER_CACHE_TTL`).
   - `_reject()` esteso con il ramo InlineQuery: `answer_inline_query` con un solo
     articolo "⛔ Devi essere membro del gruppo." e `cache_time=0, is_personal=True`.
2. `src/middlewares/rate_limit.py`: budget dedicato inline (più largo del 12/10s dei
   comandi: a ogni battuta esplodono query). `INLINE_MAX_CALLS`/`INLINE_WINDOW`. Su
   sforamento → `return` silenzioso (non c'è un messaggio da rispondere).

**B. Renderer profilo condiviso** (`src/utils/profile_view.py`, nuovo)

- `render_profile_card(user: User) -> str`: la card HTML completa (nome+tag+@handle,
  livello+rank+barra XP, trofei, CoInn). Contiene l'escaping (`utils.text.esc`)
  di ogni stringa user-controlled — presentation layer, come da regola 20.
- `common.show_profilo` viene rifattorizzato per chiamarla: elimina la duplicazione
  col path inline (requisito "poche ripetizioni"). I test di `/profilo` esistenti fanno
  da regressione.

**C. Handler inline** (`src/handlers/inline_mode.py`, nuovo)

- Router con `@router.inline_query()` e (stub documentato) `@router.chosen_inline_result()`.
- Normalizzazione query: `query.strip().lstrip("@")`, vuota/`len<2` → articolo hint.
- Cache TTL in-modulo (`_RESULTS: OrderedDict[str, tuple[float, list[InlineQueryResult]]]`,
  bound per n-entry) → niente query al DB per query ripetute identiche.
- Limite 20 risultati per `answer` (Telegram ne accetta fino a 50; 20 è il giusto per
  un picker a battuta). Ogni articolo:
  - `title`: `👤 {nome}` (con rank emoji), il nome utente per una ricerca fuzzy.
  - `description`: `@{handle} · 🏆 {n} trofei` (o "nessun @handle").
  - `input_message_content`: `InputTextMessageContent(render_profile_card(user), parse_mode=HTML)`.
  - `thumb_url`/`emoji` niente (niente media esterni).
- Query vuota → anche `switch_pm_text`/`switch_pm_parameter` per spingere la scoperta
  ("🔎 Cerca nel bot…" → `/profilo` privato, già esistente) — non implementato in v1.
- Registrato in `ROUTERS` (`handlers/__init__.py`) prima di `common` (invariante "common
  ultimo" salvo). Update types registrati da `dp.resolve_used_update_types()` da soli.

**D. BotFather (manuale, documentato in README)**

- `/setinline` → testo "Cerca un giocatore col suo nome".
- `/setinlinefeedback` → NO in v1 (senza `chosen_inline_result` handler). Si riapre se
  servirà una metrica "quante condivisioni".

## 4. Sicurezza e vincoli

- **GroupGuard**: l'unico accesso non-membro a una InlineQuery è il ramo `_reject` con
  l'articolo "members only". Niente informazioni dal bot a chi non è membro.
- **Saldo in chiaro**: decisione esplicita presa con l'utente (card "tutto incluso").
  La card viva nella chat è la stessa vista da `/profilo`.
- **SQL**: `search_users` è read-only (`ilike` + `limit`), nessun commit (regola 5).
  Se `limit` > 20 si alza solo lì (niente query nuove).
- **Escaping**: `render_profile_card` fa `esc` su username/full_name/nomi trofeo/tag
  (scritto via `utils.text.esc`).
- **Nessuna dipendenza nuova** (regola CLAUDE.md): cache con `OrderedDict` stdlib.
- **Perf**: `resolve_used_update_types` già auto-iscrive `inline_query`; niente polling
  impostato a mano. Budget RateLimit dedicato alle query inline (più largo dei comandi).

## 5. Fuori scope (Lavoro C futuro, non questo)

- Admin-picker da liste (`list_manageable`, dashboard).
- Card condivisione giochi (`quiz_`/`guess_`/`bet_`).
- Card catalogo (trofei/negozio) e cheata-sheet comandi.
- `chosen_inline_result`/metriche di condivisione.

## 6. Rischio / note

- **Gate coverage 99%**: il nuovo codice va coperto branch-per-branch (voto nel piano).
- **Oggetti vera per i test**: come nei fact di A.1, per testare il filtro serve una
  `InlineQuery` **vera** (aiogram) non un finto: `InlineQueryHandler` fa `isinstance`.
- **Chat_type non affidabile**: fonte del gate = membership, non `chat_type`.
- Il `db_session` è disponibile per l'handler inline via `DbSessionMiddleware` (usa
  `event_from_user`, upsert incluso) — da verificare con un test nel piano.
