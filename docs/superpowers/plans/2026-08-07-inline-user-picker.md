# Inline mode — User Picker → Card profilo, Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** attivare l'inline mode del bot come "user picker": si scrive `@bot <nome>`, si trovano
giocatori con match parziale a ogni battuta, si tocca la card e nella chat spunta il profilo
completo (rank, XP, trofei, tag, saldo) del giocatore scelto.

**Architecture:** tre mattoni nuovi + due fix di infrastruttura. (1) `GroupGuard` impara a
gatingare le `InlineQuery` (prerequisito di sicurezza, spec §9.1); (2) `RateLimit` ottiene un
budget dedicato alle query inline (esplodono a ogni battuta); (3) il testo della card profilo
viene estratto in `utils/profile_view.py` condiviso tra `/profilo` e inline; (4) un nuovo router
`handlers/inline_mode.py` risponde alle `InlineQuery` con risultati costruiti da
`admin_service.search_users` (con eager-load) + una cache TTL in-memory per non interrogare il DB
a ogni carattere. Niente dipendenze nuove, niente logica di gioco nuova.

**Tech Stack:** Python 3.12 · aiogram 3.30.0 (`InlineQuery`, `InlineQueryResultArticle`,
`InputTextMessageContent`, `router.inline_query()`) · SQLAlchemy 2.0 async (`selectinload`) ·
pytest (test `async def` nudi).

**Spec:** [2026-08-07-inline-user-picker-design.md](../specs/2026-08-07-inline-user-picker-design.md)

## Global Constraints

- **`from __future__ import annotations`** in ogni modulo nuovo (CLAUDE.md, regola 12).
- **Messaggi all'utente in italiano**, commenti / log / nomi in **inglese**.
- **Import top-level**: `from handlers import inline_mode`, mai `from src.handlers…`.
- **La sessione si inietta come `db_session`**, mai `session: AsyncSession`.
- **I service non committano**: le query qui sono **read-only** (`search_users`), nessun commit.
- **Escaping HTML** via `utils.text.esc` per ogni stringa user-controlled interpolata in HTML
  (regola 20). Nei `title`/`description` degli articoli inline **niente** `esc`: lì Telegram non
  fa parsing HTML.
- **Card profilo: tutto incluso, saldo incluso** (decisione utente). Unica eccezione: la dispensa
  (pantry) **non** va nella card inline — costerebbe 20 query DB per battuta; resta solo in
  `/profilo`. Documentato in `profile_text`.
- **Nessuna dipendenza nuova.** Cache con `collections.OrderedDict` stdlib.
- **Gate, da superare prima di ogni commit:** `pytest` verde, `pytest --cov=src` ≥
  `fail_under = 99`, `ruff check src/ tests/` e `mypy` a **zero findings**.
- **Base di partenza:** branch `test_giu`, merge con origin/test appena integrato.
- **Non toccare `main`.**
- **Intoccabili:** i test su denaro/XP, gating admin e ordine router (`test_router_order.py`
  auto-scopre i router: registrarlo in `ROUTERS` o il test fallisce da solo).
- I numeri di riga citati sono **al 2026-08-07** e si spostano ed iterando: usali per trovare il
  punto, non come indirizzo assoluto.

---

## Fatti verificati (al 2026-08-07), da non ri-verificare

1. **`test_router_order.py` auto-scopre i router**: cammina il package `handlers` e la classe
   `TestRouterRegistry` fallisce se un modulo con `router` non è in `ROUTERS` (`test_router_order.py:47-56`).
   Aggiungere `inline_mode` a `ROUTERS` è l'unica registrazione necessaria; `common` resta ultimo.
2. **`GroupGuard` oggi lascia passare le InlineQuery**: `_chat_type()` (`group_guard.py:65-70`)
   riconosce solo `Message`/`CallbackQuery`; per altro ritorna `None` = "non private" = passa.
   I test esistenti `test_an_event_that_is_neither_message_nor_callback_passes_through`
   (`test_group_guard.py:184-192`) usano `object()`, **non** `InlineQuery`: restano verdi se
   aggiungiamo il ramo InlineQuery senza toccare il comportamento "sconosciuto passa".
3. **`test_group_guard.py` usa oggetti aiogram VERA** con `as_(bot)`, non mock: per testare il
   nuovo ramo inline serve un `InlineQuery` vero (stessa lezione di A.1 sui filtri).
4. **`DbSessionMiddleware` inietta `db_session` in ogni evento** con `event_from_user`
   (`db_middleware.py:30-37`), agnostico rispetto al tipo: su `InlineQuery` aiogram popola
   `event_from_user`, quindi l'handler inline riceve la sessione e l'upsert dell'utente è già
   garantito. Verificato leggendo il codice; il test di Task 4 lo conferma a runtime.
5. **`search_users` (services/admin_service.py:129) non fa eager-load** di `wallet`/`badges`.
   In async accedere a `user.badges` su un risultato lazy → `MissingGreenlet`. Va aggiunto
   `selectinload(User.wallet)` + `selectinload(User.badges)`: chiamanti esistenti `/cerca`,
   dashboard) continuano a funzionare (caricano un po' più di dati, innocuo).
6. **Testo di `/profilo`** = `show_profilo` (`handlers/common.py:272-327`): la stringa esatta è
   quella riprodotta nelle righe 317-325. `profile_text` deve restituire **la stessa stringa**
   quando riceve la pantry, così i test di `/profilo` esistenti restano verdi senza modifiche.
7. **`RateLimit` oggi è 12/10s su un unico dict** (`rate_limit.py:18-19`), e su sforamento per
   eventi non-messaggio resta muto. Il budget inline serve separato: 40/10s, drop silenzioso.

---

## Struttura dei file

| File | Ruolo | Stato |
|---|---|---|
| `src/middlewares/group_guard.py` | Gate membership anche per `InlineQuery` | modify |
| `src/middlewares/rate_limit.py` | Budget inline separato (silenzioso) | modify |
| `src/utils/profile_view.py` | `profile_text(user, pantry=None)` — testo card profilo HTML | create |
| `src/handlers/common.py` | `show_profilo` rifattorizzato su `profile_text` | modify |
| `src/services/admin_service.py` | `search_users` + eager-load wallet/badges | modify |
| `src/handlers/inline_mode.py` | Router inline: cache + search + articoli + answer | create |
| `src/handlers/__init__.py` | `inline_mode.router` in `ROUTERS` prima di `common` | modify |
| `README.md` | Sezione istruzioni `/setinline` BotFather | modify |
| `tests/unit/test_group_guard.py` | Test gate inline | modify |
| `tests/unit/test_rate_limit.py` | Test budget inline | modify |
| `tests/unit/test_profile_view.py` | Test `profile_text` | create |
| `tests/unit/test_inline_mode.py` | Test handler inline (query, cache, articoli) | create |

---

## Task 1: GroupGuard — gate membership sulle InlineQuery

**Files:**
- Modify: `src/middlewares/group_guard.py`
- Test: `tests/unit/test_group_guard.py`

**Interfaces:**
- Consumes: niente (fix infrastruttura).
- Produces: `InlineQuery` gated obbligatoriamente (membro o rifiutato); `_chat_type(InlineQuery)`
  ritorna `"private"`; il rifiuto inline risponde con un articolo "membri soltanto".

- [ ] **Step 1: test che inchiodano il nuovo comportamento (aggiungi a `test_group_guard.py`)**

```python
from aiogram.types import InlineQuery


def _inline(bot, chat_type: str = "private") -> InlineQuery:
    return InlineQuery(
        id="iq1",
        from_user=User(id=USER_ID, is_bot=False, first_name="Tizio"),
        query="giu",
        offset="",
        chat_type=chat_type,
    ).as_(bot)
```

```python
class TestInlineGate:
    async def test_a_member_querying_inline_gets_through(self):
        bot = _FakeBot("member")
        handler = _Handler()

        result = await gg.GroupMemberMiddleware()(handler, _inline(bot), _data(bot))

        assert handler.calls == 1 and result == "handled"
        assert bot.member_checks == [(GROUP_ID, USER_ID)]

    async def test_a_non_member_inline_query_is_stopped(self):
        """Il punto è che l'handler non gira mai: la card profilo non parte per un estraneo."""
        bot = _FakeBot("left")
        handler = _Handler()

        await gg.GroupMemberMiddleware()(handler, _inline(bot), _data(bot))

        assert handler.calls == 0
        # `query.answer(...)` su un InlineQuery `.as_(bot)` produce un request method
        # con `.results[]`; l'articolo di rifiuto ha l'InputMessageContent con il testo.
        assert any(
            "Accesso negato" in getattr(r.input_message_content, "message_text", "")
            for c in bot.calls
            for r in getattr(c, "results", [])
        )

    async def test_inline_is_gated_even_when_typed_inside_a_group_chat(self):
        """chat_type non è fonte affidabile: chiunque digita @bot da una chat straniera.
        Il gate membership decide, non il chat_type della query."""
        bot = _FakeBot("left")
        handler = _Handler()

        await gg.GroupMemberMiddleware()(handler, _inline(bot, "supergroup"), _data(bot))

        assert handler.calls == 0
```

```python
class TestChatType:
    def test_an_inline_query_is_treated_as_private(self):
        """'private' → il gate membership scatta sempre, indipendentemente dal chat_type."""
        assert gg._chat_type(_inline(_FakeBot(), "supergroup")) == "private"
```

- [ ] **Step 2: corsa per verificare il fallimento**

Run: `pytest tests/unit/test_group_guard.py -q`
Expected: i test nuovi falliscono (InlineQuery passa il gate oggi — `handler.calls == 1` violato).

- [ ] **Step 3: implementazione in `group_guard.py`**

```python
from aiogram.types import (
    CallbackQuery,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
    TelegramObject,
    User,
)
```

In `_chat_type` (`group_guard.py:65-70`):

```python
def _chat_type(event: TelegramObject) -> str | None:
    if isinstance(event, Message):
        return event.chat.type
    if isinstance(event, CallbackQuery) and event.message:
        return event.message.chat.type
    # InlineQuery ha un campo chat_type ma non è affidabile: una query si può digitare
    # da qualunque chat. Riuscire sempre "private" forza il gate membership, che è la
    # fonte di verità (regola: i membri di gruppo possono usare il bot).
    if isinstance(event, InlineQuery):
        return "private"
    return None
```

In `_reject` (`group_guard.py:110-118`), ramo per `InlineQuery`:

```python
async def _reject(event: TelegramObject) -> None:
    msg = (
        "⛔ <b>Accesso negato.</b>\n\n"
        "Devi essere membro del gruppo per usare questo bot."
    )
    if isinstance(event, Message):
        await event.answer(msg)
    elif isinstance(event, CallbackQuery):
        await event.answer("⛔ Devi essere membro del gruppo.", show_alert=True)
    elif isinstance(event, InlineQuery):
        # Telegram restituisce gli inline result anche a chi è rifiutato: rispondiamo
        # con un solo articolo informativo e cache_time=0 così il rifiuto non resta in cache.
        await event.answer(
            results=[
                InlineQueryResultArticle(
                    id="denied",
                    title="⛔ Accesso negato",
                    description="Devi essere membro del gruppo per usare questo bot.",
                    input_message_content=InputTextMessageContent(
                        message_text=msg, parse_mode=ParseMode.HTML
                    ),
                )
            ],
            cache_time=0,
            is_personal=True,
        )
```

Aggiungi `from aiogram.enums import ParseMode` agli import. Aggiorna il docstring del modulo
(inglese): gli update `InlineQuery` sono ora gated come le chat private.

- [ ] **Step 4: corsa per verificare il verde**

Run: `pytest tests/unit/test_group_guard.py -q`
Expected: tutti verdi (nuovi + i 19 esistenti; i test `object()` restano verdi).

- [ ] **Step 5: gate e commit**

```bash
ruff check src/middlewares/group_guard.py tests/unit/test_group_guard.py && mypy
git add -A
git commit -m "feat(group_guard): gate le InlineQuery sulla membership del gruppo"
```

---

## Task 2: RateLimit — budget dedicato alle query inline

**Files:**
- Modify: `src/middlewares/rate_limit.py`
- Test: `tests/unit/test_rate_limit.py`

**Interfaces:**
- Produces: `INLINE_MAX_CALLS = 40`, `INLINE_WINDOW_SECONDS = 10.0`; sul supero di budget inline
  il middleware fa `return` silenzioso (nessun messaggio da rispondere).

- [ ] **Step 1: test nuovi (aggiungi a `test_rate_limit.py`)**

```python
from aiogram.types import InlineQuery, User as TgUser
from middlewares.rate_limit import INLINE_MAX_CALLS, INLINE_WINDOW_SECONDS


def _inline_event() -> InlineQuery:
    return InlineQuery(
        id="1",
        from_user=TgUser(id=123, is_bot=False, first_name="Tizio"),
        query="g",
        offset="",
    )
```

```python
class TestInlineBudget:
    async def test_inline_calls_share_an_independent_budget(self):
        """Il budget inline è separato: riempirlo non tocca il budget dei comandi."""
        mw = RateLimitMiddleware()
        handler = AsyncMock(return_value="ok")
        data = _make_data()

        for _ in range(INLINE_MAX_CALLS):
            result = await mw(handler, _inline_event(), data)
            assert result == "ok"
        # Ora il budget dei messaggi è ancora pieno di respiro
        handler.reset_mock()
        result = await mw(handler, AsyncMock(), data)
        assert result == "ok"

    async def test_inline_over_budget_is_dropped_silently(self):
        mw = RateLimitMiddleware()
        handler = AsyncMock(return_value="ok")
        data = _make_data()
        for _ in range(INLINE_MAX_CALLS):
            await mw(handler, _inline_event(), data)
        handler.reset_mock()

        result = await mw(handler, _inline_event(), data)

        assert result is None
        handler.assert_not_called()

    async def test_inline_budget_recovers_after_the_window(self):
        mw = RateLimitMiddleware()
        handler = AsyncMock(return_value="ok")
        data = _make_data()
        for _ in range(INLINE_MAX_CALLS):
            await mw(handler, _inline_event(), data)
        future = time.monotonic() + INLINE_WINDOW_SECONDS + 1.0
        with patch("middlewares.rate_limit.time.monotonic", return_value=future):
            result = await mw(handler, _inline_event(), data)

        assert result == "ok"
```

- [ ] **Step 2: corsa per verificare il fallimento**

Run: `pytest tests/unit/test_rate_limit.py -q`
Expected: i test nuovi falliscono (i 12 della finestra comune non bastano ai 40 inline).

- [ ] **Step 3: implementazione in `rate_limit.py`**

Costanti nuove accanto alle esistenti (`rate_limit.py:18-19`):

```python
# Inline queries fire on every keystroke while the user types "@bot ..." — a much
# higher volume than commands. Give them their own, wider budget, and drop silently
# on overflow: there is no chat message to reply to.
INLINE_MAX_CALLS = 40
INLINE_WINDOW_SECONDS = 10.0
```

Nel costruttore, il secondo dict:

```python
def __init__(self) -> None:
    self._timestamps: dict[int, list[float]] = defaultdict(list)
    self._inline_timestamps: dict[int, list[float]] = defaultdict(list)
    self._op_count = 0
```

`_evict`/`_sweep` esistenti restano **identici** (li chiamano già i test di TestPruning con la
firma `_evict(user_id, now)`): si aggiungono le varianti parametriche e `_evict`/`_sweep`
diventano wrapper diretti su `self._timestamps`.

```python
def _evict_from(self, dct: dict[int, list[float]], user_id: int, now: float,
                window: float) -> list[float]:
    kept = [t for t in dct[user_id] if now - t < window]
    if kept:
        dct[user_id] = kept
    else:
        dct.pop(user_id, None)
    return kept

def _evict(self, user_id: int, now: float,
           window: float = WINDOW_SECONDS) -> list[float]:
    return self._evict_from(self._timestamps, user_id, now, window)

def _sweep_from(self, dct: dict[int, list[float]], now: float, window: float) -> None:
    for uid in list(dct):
        if not [t for t in dct[uid] if now - t < window]:
            dct.pop(uid, None)

def _sweep(self, now: float, window: float = WINDOW_SECONDS) -> None:
    self._sweep_from(self._timestamps, now, window)
```

In `__call__`, scelta del budget per tipo:

```python
is_inline = isinstance(event, InlineQuery)
window = INLINE_WINDOW_SECONDS if is_inline else WINDOW_SECONDS
timestamps = self._inline_timestamps if is_inline else self._timestamps
limit = INLINE_MAX_CALLS if is_inline else MAX_CALLS
```

poi sweep su entrambi i dict quando scatta l'op-count (pruning di chiarro), e:

```python
if self._op_count >= _CLEANUP_EVERY:
    self._op_count = 0
    sweep_now = time.monotonic()
    self._sweep(sweep_now, WINDOW_SECONDS)
    self._sweep_from(self._inline_timestamps, sweep_now, INLINE_WINDOW_SECONDS)
kept = self._evict_from(timestamps, tg_user.id, now, window)
```

```python
if len(kept) >= limit:
    if isinstance(event, InlineQuery):
        return  # silent: no chat message to answer to
    if isinstance(event, Message):
        await event.answer("⚠️ Stai inviando troppi comandi. Aspetta qualche secondo.")
    elif isinstance(event, CallbackQuery):
        await event.answer("⚠️ Troppo veloce! Aspetta qualche secondo.", show_alert=True)
    return

timestamps[tg_user.id].append(now)
return await handler(event, data)
```

Import di `InlineQuery` in cima. L'op-count unico (`_op_count`) fa lo sweep di **entrambi** i dict
quando scatta (pruning di chiaro):

```python
if self._op_count >= _CLEANUP_EVERY:
    self._op_count = 0
    self._sweep(self._timestamps, now, WINDOW_SECONDS)
    self._sweep(self._inline_timestamps, now, INLINE_WINDOW_SECONDS)
```

- [ ] **Step 4: corsa per verificare il verde**

Run: `pytest tests/unit/test_rate_limit.py -q`
Expected: nuovi verdi + esistenti verdi (le firme con default non rompono `_evict(7, now)`).

- [ ] **Step 5: gate e commit**

```bash
ruff check src/middlewares/rate_limit.py tests/unit/test_rate_limit.py && mypy
git add -A
git commit -m "feat(rate_limit): budget dedicato alle inline queries (40/10s, drop silenzioso)"
```

---

## Task 3: `profile_text` condiviso (`utils/profile_view.py`) + refactor di `show_profilo`

**Files:**
- Create: `src/utils/profile_view.py`
- Modify: `src/handlers/common.py:272-327`
- Modify: `src/services/admin_service.py:129-137`
- Test: `tests/unit/test_profile_view.py` (nuovo)

**Interfaces:**
- Produces: `async`… no, **sync**: `def profile_text(user: User, pantry: list[tuple] | None = None) -> str`.
  I caller devono caricare `user` con `selectinload(User.wallet)` e `selectinload(User.badges)`.
- Consumes: `shop_service.render_active_tags(user)`, `xp_service.level_for_xp/rank_for_level/progress_bar`,
  `utils.text.esc`. Il parametro `pantry` è la lista `[(ConsumableItem, int)]` di
  `consumable_service.inventory`; `None` = sezione omessa (caso inline).

- [ ] **Step 1: i test di `profile_text` (crea `tests/unit/test_profile_view.py`)**

```python
"""profile_text — the shared profile card text. One code path for /profilo and the
inline picker, so they cannot drift apart."""

from __future__ import annotations

from aiogram.types import User as TgUser

from database.models import User, Wallet
from utils import profile_view


def _make_user(**kw) -> User:
    defaults = dict(
        tg_id=321, username="mario", full_name="Mario Rossi", xp=500,
        active_tags_json=[], rank_slug=None, wallet=Wallet(tg_id=321, coins=777),
    )
    defaults.update(kw)
    return User(**defaults)


class TestProfileText:
    def test_balance_and_rank_are_included(self):
        text = profile_view.profile_text(_make_user())

        assert "777" in text                       # saldo incluso (decisione utente)
        assert "Livello" in text
        assert "Trofei" in text

    def test_username_is_escaped(self):
        text = profile_view.profile_text(_make_user(username="a<b>"))

        assert "&lt;b&gt;" in text and "<b>a<b>" not in text

    def test_no_username_renders_N_D(self):
        text = profile_view.profile_text(_make_user(username=None))

        assert "N/D" in text

    def test_full_name_is_escaped_even_without_tags(self):
        text = profile_view.profile_text(_make_user(full_name="x<y> & z"))

        assert "x&lt;y&gt;" in text

    def test_pantry_section_appears_only_when_provided(self):
        from services.catalog_loader import ConsumableItem
        pantry = [(ConsumableItem("cons_revive", "Rivivere", "💖", "cons_power", 50, "revive"), 2)]

        with_pantry = profile_view.profile_text(_make_user(), pantry=pantry)
        without = profile_view.profile_text(_make_user())

        assert "Dispensa" in with_pantry and "Dispensa" not in without

    def test_trophies_count_comes_from_badges(self):
        # badges è una relationship: serve un utente con lista pre-caricata via selectinload.
        from database.models import Badge
        user = _make_user()
        user.badges = [Badge(slug="b1"), Badge(slug="b2"), Badge(slug="b3")]

        text = profile_view.profile_text(user)

        assert "Trofei: 3" in text
```

- [ ] **Step 2: corsa per verificare il fallimento**

Run: `pytest tests/unit/test_profile_view.py -q`
Expected: FAIL — `profile_view` non esiste (ImportError).

- [ ] **Step 3: crea `src/utils/profile_view.py`**

```python
"""Shared profile card text (HTML). One code path for `/profilo` and the inline
user picker, so the two can never drift apart. Presentation only: every
user-controlled string goes through `utils.text.esc` here (rule 20)."""

from __future__ import annotations

from database.models import User
from services import xp_service
from services.catalog_loader import ConsumableItem
from services.shop_service import render_active_tags
from utils.text import esc


def profile_text(user: User, pantry: list[tuple[ConsumableItem, int]] | None = None) -> str:
    """Render the full profile card.

    `user` must be loaded with `selectinload(User.wallet)` and
    `selectinload(User.badges)`. `pantry` is the `consumable_service.inventory`
    result; when `None` the pantry section is omitted (inline cards: querying it
    for 20 results per keystroke would be 20 DB round-trips).
    """
    username_display = f"@{esc(user.username)}" if user.username else "N/D"
    badge_count = len(user.badges)
    prog = xp_service.level_for_xp(user.xp)
    rank = xp_service.rank_for_level(prog.level)
    rank_txt = f" · {rank.emoji} {esc(rank.name)}" if rank else ""
    level_line = (
        f"⚡ <b>Livello {prog.level}</b>{rank_txt}\n"
        f"   {xp_service.progress_bar(prog)} "
        f"{prog.xp_into_level:,}/{prog.xp_for_next:,} XP\n"
    )
    tags = render_active_tags(user)
    tag_line = f"🏷️ <b>Tag:</b> {esc(tags)}\n" if tags else ""
    title = esc(user.full_name)
    if tags:
        title = f"{esc(tags)} · {title}"

    pantry_line = ""
    if pantry:
        shown = " · ".join(f"{item.emoji} ×{qty}" for item, qty in pantry[:6])
        more = " …" if len(pantry) > 6 else ""
        pantry_line = f"🎒 <b>Dispensa:</b> {shown}{more}\n"

    return (
        f"🎮 <b>{title}</b>\n\n"
        f"🔖 <b>Username:</b> {username_display}\n\n"
        f"{tag_line}"
        f"{level_line}"
        f"💰 <b>CoInn:</b> <b>{user.wallet.coins:,} 🪙</b>\n"
        f"🏆 <b>Trofei:</b> {badge_count}\n"
        f"{pantry_line}".rstrip("\n")
    )
```

- [ ] **Step 4: refactor di `show_profilo` (common.py:272-327)** — sostituisci il corpo di
  rending con la chiamata a `profile_text`, mantenendo identica la logica di fetch:

```python
async def show_profilo(message: Message, db_session: AsyncSession) -> None:
    """Render the caller's own profile. Public: works in the group (one live reply
    per user via static_reply) and in private. Never shows the Telegram ID — that
    stays exclusive to the admin /info dossier."""
    result = await db_session.execute(
        select(User)
        .where(User.tg_id == message.from_user.id)
        .options(
            selectinload(User.wallet),
            selectinload(User.badges),
        )
    )
    user = result.scalar_one_or_none()

    if user is None or user.wallet is None:
        await message.answer("⚠️ Profilo non trovato. Usa /start per registrarti.")
        return

    pantry = await consumable_service.inventory(db_session, user.tg_id)
    await reply_static(
        message,
        profile_text(user, pantry),
        "profilo",
    )
```

Import in cima a `common.py`: `from utils.profile_view import profile_text`. Rimuovi dal corpo le
righe 290-315 (ora in `profile_view`) e l'import locale di `inventory`/`render_active_tags`
resta dove serve (solo `inventory`). Testo ESATTO identico → nessun test di `/profilo` da toccare.

- [ ] **Step 5: eager-load in `search_users` (admin_service.py:129-137)** — serve al picker
  inline, e rende `user.badges`/`user.wallet` accessibili senza `MissingGreenlet`:

```python
from sqlalchemy.orm import selectinload  # già importato? aggiungi se manca

async def search_users(session: AsyncSession, query: str, limit: int = 15) -> list[User]:
    pattern = f"%{query}%"
    result = await session.execute(
        select(User)
        .where(User.username.ilike(pattern) | User.full_name.ilike(pattern))
        .order_by(User.created_at.desc())
        .limit(limit)
        .options(
            selectinload(User.wallet),
            selectinload(User.badges),
        )
    )
    return list(result.scalars().all())
```

- [ ] **Step 6: corsa per verificare il verde (tante regressioni)**

Run: `pytest tests/unit/test_profile_view.py tests/unit/test_group_guard.py tests/integration/test_profile_public.py tests/integration/test_start_deeplinks.py -q`
Expected: tutti verdi (i test di `/profilo` usano `show_profilo` e devono restare identici nel testo).

- [ ] **Step 7: gate e commit**

```bash
pytest --cov=src --cov-report=term-missing -q   # deve restare ≥ 99
ruff check src/ tests/ && mypy
git add -A
git commit -m "feat(profile_view): estratta card profilo condivisa tra /profilo e inline picker"
```

---

## Task 4: handler inline + registrazione router

**Files:**
- Create: `src/handlers/inline_mode.py`
- Modify: `src/handlers/__init__.py` (`ROUTERS`)
- Test: `tests/unit/test_inline_mode.py` (nuovo)

**Interfaces:**
- Consumes: `admin_service.search_users(db_session, q, limit=20)` (ora con eager-load di
  wallet/badges); `utils.profile_view.profile_text(user)` (senza pantry); `utils.text.esc`
  (per l'articolo "nessun risultato"); `xp_service.level_for_xp/rank_for_level` (emoji rank).
- Produces: `async def user_picker(query, db_session) -> None` su `router.inline_query()`;
  `clear_cache()` per i fixture di test; cache TTL `_RESULT_CACHE` (OrderedDict, max 256, TTL 3s).

- [ ] **Step 1: i test del handler (crea `tests/unit/test_inline_mode.py`)**

```python
"""inline_mode — the inline user picker. Fake query objects mirror the repo's
duck-typed convention (see test_profile_public._FakeMsg)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import handlers.inline_mode as inline_mode


class _FakeInlineQuery:
    def __init__(self, text: str = ""):
        self.from_user = SimpleNamespace(id=111, username="cercante", full_name="Cerco")
        self.query = text
        self.answers = []

    async def answer(self, **kwargs):
        self.answers.append(kwargs)


@pytest.fixture(autouse=True)
def _isolate():
    inline_mode.clear_cache()
    yield
    inline_mode.clear_cache()


@pytest.fixture
async def seeded(session, user_factory):
    await user_factory(tg_id=1, username="giu", full_name="Giuseppe", coins=999)
    await user_factory(tg_id=2, username="gio", full_name="Giovanni", coins=5)
    await user_factory(tg_id=3, username="mario", full_name="Mario Rossi", coins=50)
    await user_factory(tg_id=4, username=None, full_name="SenzaHandler", coins=7)


async def _call(text: str = "gi", session=None):
    query = _FakeInlineQuery(text)
    await inline_mode.user_picker(query, session)
    return query.answers[0]


class TestQueryParsing:
    async def test_empty_query_returns_a_hint(self, seeded, session):
        ans = await _call("", session)

        assert len(ans["results"]) == 1
        assert "lettere" in ans["results"][0].title

    async def test_one_char_query_returns_a_hint(self, seeded, session):
        ans = await _call("g", session)

        assert len(ans["results"]) == 1
        assert "altre lettere" in ans["results"][0].title

    async def test_leading_at_and_whitespace_are_stripped(self, seeded, session):
        ans = await _call("  @gio  ", session)

        # match on "gio"
        assert any(r.id == "2" for r in ans["results"])


class TestMatching:
    async def test_partial_match_on_username(self, seeded, session):
        # "gi" è sottostringa sia di "giu" che di "gio" (username) e di "Giuseppe".
        ans = await _call("gi", session)

        ids = {r.id for r in ans["results"]}
        assert "1" in ids and "2" in ids and "3" not in ids and "4" not in ids

    async def test_partial_match_on_full_name(self, seeded, session):
        ans = await _call("mario ross", session)

        assert any(r.id == "3" for r in ans["results"])

    async def test_no_matches_returns_a_hint(self, seeded, session):
        ans = await _call("zzz", session)

        assert len(ans["results"]) == 1
        assert "Nessun giocatore" in ans["results"][0].title

    async def test_results_are_capped(self, seeded, session):
        for i in range(5, 25):
            await user_factory(tg_id=i, username=f"gius{i}", full_name=f"Utente {i}", coins=0)
        ans = await _call("gius", session)

        assert len(ans["results"]) <= 20


class TestArticleContent:
    async def test_article_contains_the_profile_card_with_balance(self, seeded, session):
        ans = await _call("giu", session)

        content = ans["results"][0].input_message_content
        assert "999" in content.message_text       # saldo incluso
        assert "CoInn" in content.message_text
        assert content.parse_mode.value == "HTML"

    async def test_title_describes_the_player(self, seeded, session):
        ans = await _call("giu", session)

        r = ans["results"][0]
        assert "Giuseppe" in r.title
        assert "giu" in r.description


class TestCaching:
    async def test_identical_query_is_cached(self, seeded, session, monkeypatch):
        # `_search_users` è l'alias di modulo che il handler chiama: si patcha quello.
        calls = {"n": 0}
        real = inline_mode._search_users

        async def counting(session, q, limit=15):
            calls["n"] += 1
            return await real(session, q, limit=limit)

        monkeypatch.setattr(inline_mode, "_search_users", counting)

        await _call("giu", session)
        assert calls["n"] == 1
        assert inline_mode._cache_size() == 1

        await _call("giu", session)
        assert calls["n"] == 1, "seconda query identica → cache, nessuna search"

    async def test_different_query_bypasses_cache(self, seeded, session, monkeypatch):
        calls = {"n": 0}
        real = inline_mode._search_users

        async def counting(session, q, limit=15):
            calls["n"] += 1
            return await real(session, q, limit=limit)

        monkeypatch.setattr(inline_mode, "_search_users", counting)
        await _call("giu", session)
        await _call("gio", session)

        assert calls["n"] == 2

    async def test_short_or_empty_queries_are_not_cached(self, seeded, session):
        await _call("g", session)
        assert inline_mode._cache_size() == 0
```

- [ ] **Step 2: corsa per verificare il fallimento**

Run: `pytest tests/unit/test_inline_mode.py -q`
Expected: FAIL — `inline_mode` non esiste (ImportError).

- [ ] **Step 3: crea `src/handlers/inline_mode.py`**

```python
"""Inline mode: the user picker. `@bot <name>` → type-ahead search over users
(partial match on username/full_name) → tap → the chosen player's full profile
card lands in the chat (balance included, by explicit user decision).

One query per keystroke: a small TTL cache absorbs the repeats Telegram fires for
the same substring. Results are personal to the caller (is_personal=True)."""

from __future__ import annotations

import time
from collections import OrderedDict

from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.types import InlineQuery, InlineQueryResultArticle, InputTextMessageContent
from sqlalchemy.ext.asyncio import AsyncSession

from services import admin_service, xp_service
from utils.profile_view import profile_text

router = Router(name="inline_mode")

_HINT_FOCUS = (
    "🔎 Cerca un giocatore scrivendo il suo nome o @username."
)

# (query_lower) -> (timestamp, results). Ordered so the oldest entry is evicted first.
_RESULT_CACHE: OrderedDict[str, tuple[float, list[InlineQueryResultArticle]]] = OrderedDict()
_CACHE_TTL = 3.0
_CACHE_MAX = 256
_RESULT_LIMIT = 20

# Module alias, patchable in tests: the only place the handler talks to the DB.
_search_users = admin_service.search_users


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def _cache_get(key: str) -> list[InlineQueryResultArticle] | None:
    hit = _RESULT_CACHE.get(key)
    if hit is None:
        return None
    ts, results = hit
    if time.monotonic() - ts > _CACHE_TTL:
        _RESULT_CACHE.pop(key, None)
        return None
    _RESULT_CACHE.move_to_end(key)
    return results


def _cache_set(key: str, results: list[InlineQueryResultArticle]) -> None:
    _RESULT_CACHE[key] = (time.monotonic(), results)
    _RESULT_CACHE.move_to_end(key)
    while len(_RESULT_CACHE) > _CACHE_MAX:
        _RESULT_CACHE.popitem(last=False)


def _cache_size() -> int:
    return len(_RESULT_CACHE)


def clear_cache() -> None:
    _RESULT_CACHE.clear()


# ---------------------------------------------------------------------------
# articles
# ---------------------------------------------------------------------------

def _hint_article(key: str | None, text: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=key or "hint",
        title=text,
        description=_HINT_FOCUS,
        input_message_content=InputTextMessageContent(
            message_text=text, parse_mode=ParseMode.HTML
        ),
    )


def _user_article(user) -> InlineQueryResultArticle:
    handle = f"@{user.username}" if user.username else "nessun @"
    prog = xp_service.level_for_xp(user.xp)
    rank = xp_service.rank_for_level(prog.level)
    rank_emoji = rank.emoji if rank else ""
    return InlineQueryResultArticle(
        id=str(user.tg_id),
        title=f"{rank_emoji} {user.full_name}".strip(),
        description=f"{handle} · 🏆 {len(user.badges)} trofei",
        input_message_content=InputTextMessageContent(
            message_text=profile_text(user),
            parse_mode=ParseMode.HTML,
        ),
    )


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------

@router.inline_query()
async def user_picker(query: InlineQuery, db_session: AsyncSession) -> None:
    raw = query.query.strip().lstrip("@").strip()
    key = raw.lower()

    cached = _cache_get(key)
    if cached is not None:
        await query.answer(results=cached, is_personal=True, cache_time=2)
        return

    if len(raw) < 2:
        await query.answer(
            results=[_hint_article(None, "Scrivi altre lettere per trovare un giocatore…")],
            is_personal=True,
            cache_time=2,
        )
        return

    users = await _search_users(db_session, raw, limit=_RESULT_LIMIT)

    if not users:
        await query.answer(
            results=[_hint_article(
                key,
                f"Nessun giocatore trovato per «{raw}».",
            )],
            is_personal=True,
            cache_time=2,
        )
        return

    articles = [_user_article(u) for u in users]
    _cache_set(key, articles)
    await query.answer(results=articles, is_personal=True, cache_time=2)
```

- [ ] **Step 4: registra il router (`handlers/__init__.py`)**

Aggiungi `inline_mode` all'import e alla tupla `ROUTERS` (`handlers/__init__.py:31-70`), prima di
`common`:

```python
from handlers import (
    admin,
    admin_betting,
    admin_dashboard,
    backup,
    badges,
    betting,
    common,
    economy,
    events,
    fun_ai,
    group_events,
    guess,
    inline_mode,
    leaderboard,
    onboarding,
    quiz,
    schedule,
    shop,
)

ROUTERS: tuple[Router, ...] = (
    group_events.router,
    onboarding.router,
    economy.router,
    admin_betting.router,
    betting.router,
    badges.router,
    leaderboard.router,
    shop.router,
    admin.router,
    admin_dashboard.router,
    events.router,
    quiz.router,
    guess.router,
    schedule.router,
    backup.router,
    fun_ai.router,
    inline_mode.router,
    common.router,          # must stay last (global fallbacks)
)
```

- [ ] **Step 5: corsa per verificare il verde (con test_router_order)**

Run: `pytest tests/unit/test_inline_mode.py tests/unit/test_router_order.py -q`
Expected: verde — `test_router_order` auto-scopre `router` in `inline_mode` e lo trova in `ROUTERS`.

- [ ] **Step 6: verifica che `dp.resolve_used_update_types()` non richieda altro**

Run: `PYTHONPATH=src python -c "import main; print('update types:', main.dp.resolve_used_update_types())"`
Expected: printa `... {'inline_query', ...}` (iscrizione automatica, nessun cambiamento manuale).

- [ ] **Step 7: gate e commit**

```bash
pytest --cov=src --cov-report=term-missing -q   # ≥ 99
ruff check src/ tests/ && mypy
git add -A
git commit -m "feat(inline): user picker con card profilo in chat"
```

---

## Task 5: documentazione operativa

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: niente (docs).
- Produces: sezione "Inline mode" con i passi BotFather.

- [ ] **Step 1: aggiungi la sezione al README**

In `README.md`, nel posto dove si spiegano le configurazioni del bot, aggiungi:

```markdown
## Inline mode (user picker)

Il bot risponde a `@<bot> <nome>` con la ricerca dei giocatori (match parziale su
username e nome reale): toccando una card, nella chat viene postato il profilo completo
(livello, rank, XP, trofei, tag, saldo CoInn) del giocatore scelto — come da
`docs/superpowers/specs/2026-08-07-inline-user-picker-design.md`.

Attivazione (una volta, con @BotFather):
1. `/setinline` → testo: `Cerca un giocatore scrivendo il suo nome o @username.`
2. `/setinlinefeedback` → NON servono in v1 (nessun evento chosen_inline_result).

Sicurezza: il gate membership (`GroupGuard`) si applica anche alle inline query;
chi non è membro del gruppo riceve solo l'articolo "accesso negato".
```

- [ ] **Step 2: verifica che il README non abbia un'altra sezione in conflitto**

Run: `rg -n 'setinline|Inline|inline' README.md`
Expected: solo la nuova sezione (nessun duplicato).

- [ ] **Step 3: gate e commit**

```bash
ruff check src/ tests/ && mypy
git add -A
git commit -m "docs: istruzioni setinline per l'inline mode"
```

---

## Verifica finale (prima di considerare chiuso)

- [ ] `pytest` completo (unit + integration, senza `-m pg`): verde.
- [ ] `pytest --cov=src --cov-report=term-missing`: coverage totale ≥ `fail_under` (99).
- [ ] `pytest -m pg -q` con `TEST_PG_URL` su container usa-e-getta: verde (le modifiche a
      `search_users` toccano il path admin — conferma che i test pg guess/trophy/quiz passino).
- [ ] `ruff check src/ tests/` e `mypy`: zero findings.
- [ ] `PYTHONPATH=src python -c "import main"`: non esplode.
- [ ] BotFather `/setinline` fatto (passo manuale solo dei maintainer, fuori da CI).
- [ ] Aggiornare la **tabella di stato** di
      `docs/superpowers/specs/2026-08-03-fondamenta-presentazione-design.md` (riga 438, lavoro C
      → "Lavoro C in parte: fatto il picker profilo, restano admin-picker/share/catalogo") e il
      paragrafo §9.1 (ora il gap è chiuso). Stesso commit della chiusura.
