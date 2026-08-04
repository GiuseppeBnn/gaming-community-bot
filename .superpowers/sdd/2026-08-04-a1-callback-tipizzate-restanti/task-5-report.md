# Task 5 A.1 — Callback tipizzate della Locanda

Base: `c59d788` · checkout: `test_giu`.

## RED → GREEN

1. Ho aggiunto prima a `tests/unit/test_callbacks.py` i contratti di pack:
   `ShopCb(action="home") == "shop:home:"` e
   `ShopCb(action="exec", key="tag_dragon") == "shop:exec:tag_dragon"`, più il
   rifiuto di `:` in `action` o `key`.
2. RED registrato con `.venv/bin/pytest tests/unit/test_callbacks.py -q`:
   l'import di `ShopCb` falliva perché la classe non esisteva. (Il primo tentativo
   con il `pytest` di sistema si è fermato prima per Python 3.9/pydantic; il RED
   valido è stato poi eseguito con la `.venv` Python 3.12.)
3. GREEN: aggiunta la dichiarazione centrale `ShopCb(CallbackData, prefix="shop")`;
   conversione completa di router, handler e tastiere a `ShopCb.filter(...)` e
   `ShopCb(...).pack()`.
4. Per i sei handler keyed (`buy`, `exec`, `cat`, `cbuy`, `cexec`, `tag`) è stato
   aggiunto un test di confine `key is None`. Il RED di mutazione, con i guard
   rimossi temporaneamente, falliva con l'alert di item non disponibile; dopo il
   ripristino ogni handler risponde con `answer()` e ritorna prima di qualsiasi
   logica di acquisto.

## Invarianti preservate

- Non sono stati modificati servizi, modelli, migrazioni, routing generale o gate.
- I flussi di debito, saldo, ownership, doppio acquisto, `rollback`, ledger,
  `record_purchase`, `record_consumption`, `flush`, milestone e `commit` in
  `shop.py` restano invariati: cambia esclusivamente l'origine della chiave da
  `callback_data.key` al posto del parsing manuale.
- Le asserzioni monetarie, XP, row-count, ledger, purchase, flush, milestone,
  commit e rollback restano inalterate. In
  `test_money_concurrency_pg.py` l'unica chiamata diretta ora passa
  `callback_data=ShopCb(action="exec", key=item.key)` e il fake serializza con
  `.pack()`; nessuna asserzione è stata modificata.

## Gate eseguiti

- Focused: `.venv/bin/pytest tests/unit/test_callbacks.py tests/unit/test_keyboards.py tests/integration/test_shop_handlers.py tests/integration/test_shop_home_balance.py -q` → 128 passed.
- PG: `.venv/bin/pytest tests/integration/test_money_concurrency_pg.py -q` → 21 skipped:
  `TEST_PG_URL` non è presente. Per accordo Task 5 non sono stati creati Docker né
  PostgreSQL; il DB reale resta alla fase audit.
- Structural: scan dei producer letterali `shop:` in `src/` → nessun risultato;
  i test di wiring/action dei callback sono inclusi nel focused run.
- Import: `PYTHONPATH=src .venv/bin/python -c 'import handlers.callbacks, handlers.shop, keyboards.shop_kb'` → exit 0.
- Ruff: `.venv/bin/ruff check src tests` → clean.
- Mypy: `.venv/bin/mypy` → `Success: no issues found in 37 source files`.
- Regressione e coverage: l'intera suite è stata eseguita in blocchi (`tests/unit`
  e tutti i file `tests/integration` con `--cov --cov-append`) per il limite di
  durata del terminale; ogni blocco verde, gli stessi test PG skipped. Report
  combinato: 99,30% (soglia 99%).
- Diff: `git diff --check` → clean.

## Rischi residui

Il test concorrenza PostgreSQL non ha potuto esercitare il lock reale in questa
sessione perché `TEST_PG_URL` è assente. È stato eseguito e ha dichiarato lo skip
atteso; la verifica con DB reale è esplicitamente rimandata all'audit A.1.

## Commit

`refactor: tipizza le callback della locanda`
