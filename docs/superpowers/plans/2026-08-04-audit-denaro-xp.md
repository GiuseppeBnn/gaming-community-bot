# Repository-wide Money and XP Audit Execution Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a complete, evidence-backed audit of every money/XP decision and mutation in the repository, then promote only reproduced defects to separate TDD fix tasks without changing the current game balance.

**Architecture:** Audit one transaction domain at a time against the same matrix: transaction owner, SQL decision, lock order, identity-map exposure, idempotency, ledger parity, rollback, and SQLite/PostgreSQL evidence. Keep discovery separate from repair: a suspected defect first gets a deterministic RED reproduction; only then is an exact fix task written and executed. The audit report is the durable source of truth.

**Tech Stack:** Python 3.12 · SQLAlchemy async · SQLite · PostgreSQL 16 (`postgres:16-alpine`) · pytest · Docker · Ruff · mypy.

## Global Constraints

- Work only on branch `test_giu`; never touch `main`.
- Start this plan only after A.1 is complete and its full gates are green.
- Preserve amounts, prizes, costs, payout formulas, caps, capped/uncapped classifications, ranks,
  thresholds, and every current gameplay outcome.
- Services do not commit. Handler/lifecycle callers own commit and rollback.
- Money decisions use SQL predicates and relative arithmetic; `rowcount` resolves races.
- Use `synchronize_session=False` plus targeted refresh after SQL mutation when an ORM instance can
  remain alive.
- Read decisive values as columns, not potentially stale ORM entities.
- Lock only when atomic SQL is insufficient. Canonical order is Event → User → Wallet; multiple
  wallets lock by ascending `tg_id`.
- Mutate `User.xp` only through `xp_service`; keep every XP source's capped/uncapped classification.
- Do not refactor a verified-safe path merely for stylistic uniformity.
- Do not change production code from inspection alone. A fix requires a focused test observed RED.
- Each confirmed defect gets an exact appended implementation task, its own review, and an isolated
  commit. Invoke `superpowers:systematic-debugging` before designing that fix and
  `superpowers:test-driven-development` while implementing it.
- Execute Tasks 1–8 sequentially because they update one audit report and inspect overlapping
  transaction boundaries.

---

## File Structure

| File | Responsibility |
|---|---|
| `docs/superpowers/audits/2026-08-04-denaro-xp.md` | Durable inventory, baseline evidence, findings, test commands, and final risk matrix |
| `tests/integration/test_money_concurrency_pg.py` | Real PostgreSQL races, locks, identity-map and ledger-parity reproductions |
| `tests/integration/test_economy_service.py` | Core credit, debit, transfer and daily behavior |
| `tests/integration/test_economy_locking.py` | Deterministic SQLite transaction and amount guards |
| `tests/integration/test_admin_service.py` | Absolute balance and bulk-credit behavior |
| `tests/integration/test_shop_service.py` | Cosmetic ownership/idempotency behavior |
| `tests/integration/test_consumable_service.py` | Consumable purchase counts and inventory behavior |
| `tests/integration/test_bet_service.py` | Placement, settlement, refund and ledger behavior |
| `tests/integration/test_bet_locking.py` | Betting state-transition and atomic-total behavior |
| `tests/integration/test_quiz_service.py` | Answer idempotency, podium, coin prizes and quiz XP |
| `tests/integration/test_guess_service.py` | Attempt claims, standings, coin prizes and Guess XP |
| `tests/unit/test_xp_service.py` | XP arithmetic, caps, ranks and airdrop behavior |
| `tests/integration/test_xp_admin.py` | Admin XP and audit-log parity |
| Domain production files named in Tasks 3–7 | Read and classify; modify only through a later proven-defect task |

The audit report uses exactly these columns:

```markdown
| ID | Path/function | Asset/state | Transaction owner | SQL decision/arithmetic |
| Lock/order | Identity-map handling | Idempotency/claim | Ledger/audit parity |
| Rollback behavior | SQLite evidence | PostgreSQL evidence | Status/finding |
```

Final `Status/finding` values are only `SAFE`, `DEFECT`, or `NOT_APPLICABLE`; no row may remain
`NEEDS_TEST` when Task 8 closes.

## Defect Promotion Protocol

When a task finds a plausible defect:

1. Add a `NEEDS_TEST` row with the exact interleaving or failure mechanism.
2. Put the smallest reproduction in the domain test file listed above. Use
   `test_money_concurrency_pg.py` when the mechanism requires independent transactions, row locks,
   PostgreSQL isolation, or a real uniqueness race.
3. Run that one test against the pre-fix code. `PASS` changes the row to `SAFE`; do not edit
   production. `FAIL` changes it to `DEFECT` and records the shortest decisive failure.
4. Re-run once to reject timing-only failures. Concurrency tests use barriers, events, explicit
   transaction ordering, or `asyncio.gather`; never use timing sleeps as the correctness mechanism.
5. Invoke `superpowers:systematic-debugging`, identify the root cause, and append a complete fix task
   to this plan with exact files, test name, minimal code change, focused gates, and commit command.
6. Self-review the appended task against the design spec before production code changes.
7. Implement through `superpowers:test-driven-development`; keep the reproduction permanently.

---

### Task 1: Start isolated PostgreSQL and capture both baselines

**Files:**
- Create: `docs/superpowers/audits/2026-08-04-denaro-xp.md`
- Verify: `tests/conftest.py`, `pyproject.toml`

**Interfaces:**
- Consumes: PostgreSQL fixture safety check requiring a database name ending in `_test`.
- Produces: running disposable database and exact SQLite/PostgreSQL baseline recorded in the audit.

- [ ] **Step 1: Verify Docker target and port without changing state**

Run separately:

```bash
docker ps -a --filter name=^/gaming-community-bot-pg-test$ --format '{{.ID}}\t{{.Status}}\t{{.Ports}}'
lsof -nP -iTCP:5433 -sTCP:LISTEN
```

Expected: no unrelated container with that exact name and no listener on `127.0.0.1:5433`. If
either is occupied, do not stop or remove it: choose an explicit unused port/container suffix and
record both in the audit before continuing.

- [ ] **Step 2: Start the disposable database**

When name and port are free, run:

```bash
docker run --rm --name gaming-community-bot-pg-test -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=gamingbot_test -p 127.0.0.1:5433:5432 -d postgres:16-alpine
```

Do not add a volume. Poll readiness with this command, one invocation at a time:

```bash
docker exec gaming-community-bot-pg-test pg_isready -U postgres -d gamingbot_test
```

Expected: `accepting connections`.

- [ ] **Step 3: Prove the fixture safety barrier before destructive schema setup**

Run with an intentionally invalid database name:

```bash
TEST_PG_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/gamingbot .venv/bin/pytest tests/integration/test_money_concurrency_pg.py -x -v
```

Expected: FAIL before schema setup with `does not end in '_test'`. The Compose database is only a
name in this DSN; the dedicated test container has no such database, and the fixture must refuse it
before connecting or dropping tables.

- [ ] **Step 4: Capture the default baseline**

Run separately:

```bash
.venv/bin/pytest -v
.venv/bin/pytest --cov=src --cov-report=term-missing
.venv/bin/ruff check src/ tests/
.venv/bin/mypy
PYTHONPATH=src .venv/bin/python -c 'import main'
```

Record exact pass/skip counts, coverage, tool versions, HEAD, and command exit status in the audit.

- [ ] **Step 5: Capture the PostgreSQL baseline**

Run:

```bash
TEST_PG_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/gamingbot_test .venv/bin/pytest -m pg -v
```

Then run the full suite with PostgreSQL tests enabled:

```bash
TEST_PG_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/gamingbot_test .venv/bin/pytest -v
```

All PostgreSQL tests must run rather than skip. Record both summaries. An infrastructure failure is
diagnosed before any code change.

- [ ] **Step 6: Create and commit the audit header**

Create the report with: approved invariants, Docker name/port/DSN, both baseline summaries, the
matrix header above, and an empty “Confirmed defects” section that states `None at baseline`.

```bash
git add docs/superpowers/audits/2026-08-04-denaro-xp.md
git commit -m "docs: avvia audit denaro e XP"
```

---

### Task 2: Inventory mutation boundaries and transaction ownership

**Files:**
- Modify: `docs/superpowers/audits/2026-08-04-denaro-xp.md`
- Inspect: `src/database/models.py`, `src/middlewares/db_middleware.py`, all Python files under
  `src/services/` and `src/handlers/`

**Interfaces:**
- Consumes: baseline report from Task 1.
- Produces: exhaustive source-to-domain index; every later task checks off its own rows.

- [ ] **Step 1: Locate every direct money/XP expression**

Run each search separately and paste every production match into the report's source index:

```bash
rg -n 'Wallet\.coins|\.values\([^)]*coins|coins\s*[+\-*/]?=' src --glob '*.py'
rg -n 'User\.xp|xp_today|\.values\([^)]*xp|\.xp\s*[+\-*/]?=' src --glob '*.py'
rg -n 'LedgerEntry\(|TransactionType\.|grant_xp\(|set_xp\(|airdrop_xp\(' src --glob '*.py'
rg -n 'award_prizes\(|claim_close\(|resolve_event\(|cancel_event\(|record_podium\(|record_event\(' src --glob '*.py'
```

Classify catalog constants and presentation-only reads as `NOT_APPLICABLE`; never omit them silently.

- [ ] **Step 2: Map every service call to its transaction owner**

Run:

```bash
rg -n 'economy_service\.|xp_service\.|admin_service\.(set_balance|mass_credit)|bet_service\.(place_bet|resolve_event|cancel_event)|award_prizes\(' src/handlers src/services --glob '*.py'
rg -n 'commit\(|rollback\(' src/handlers src/services src/middlewares --glob '*.py'
```

For every mutating service call, name the handler, lifecycle function, scheduler callback, or
middleware that commits it. Flag any service commit except the already documented catalog-sync
startup exception for review.

- [ ] **Step 3: Reconcile inventory counts**

Every production match from Step 1 must map to exactly one of Tasks 3–7. Add a count by domain and a
cross-reference to the matrix row IDs. Repeat all four searches after reconciliation; expected:
zero unclassified production matches.

- [ ] **Step 4: Commit the source index**

```bash
git add docs/superpowers/audits/2026-08-04-denaro-xp.md
git commit -m "docs: inventaria mutazioni economiche"
```

---

### Task 3: Audit core wallets, ledger, daily, transfers, and admin currency

**Files:**
- Modify: `docs/superpowers/audits/2026-08-04-denaro-xp.md`
- Inspect: `src/services/economy_service.py`, `src/services/admin_service.py`,
  `src/handlers/economy.py`, `src/handlers/admin.py`, `src/handlers/admin_dashboard.py`,
  `src/middlewares/db_middleware.py`
- Verify: `tests/integration/test_economy_service.py`, `tests/integration/test_economy_locking.py`,
  `tests/integration/test_economy_handlers.py`, `tests/integration/test_admin_service.py`,
  `tests/integration/test_admin_commands.py`, `tests/integration/test_admin_dashboard_money.py`,
  `tests/integration/test_money_concurrency_pg.py`

**Interfaces:**
- Consumes: mutation/owner index from Task 2.
- Produces: matrix rows for `_add_coins`, `credit`, `debit`, `transfer`, `claim_daily`,
  `set_balance`, `mass_credit`, middleware wallet creation, and every handler commit/rollback path.

- [ ] **Step 1: Verify focused SQLite behavior**

```bash
.venv/bin/pytest tests/integration/test_economy_service.py tests/integration/test_economy_locking.py tests/integration/test_economy_handlers.py tests/integration/test_admin_service.py tests/integration/test_admin_commands.py tests/integration/test_admin_dashboard_money.py -v
```

- [ ] **Step 2: Verify focused PostgreSQL races and ledger parity**

```bash
TEST_PG_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/gamingbot_test .venv/bin/pytest tests/integration/test_money_concurrency_pg.py -k 'Middleware or DailyClaim or Debit or Transfer or AdminSetBalance' -v
```

- [ ] **Step 3: Complete core matrix rows**

For every function named in **Produces**, record the exact SQL predicate/arithmetic, refresh,
locks/order, ledger rows, owner of commit, and behavior if the second half fails. Explicitly prove:
debit cannot overdraw, transfer locks in ascending `tg_id`, transfer ledger sums to zero, daily claim
and its XP/milestone side effects share one handler transaction, absolute set balance holds a wallet
lock, and mass credit creates one ledger row per affected wallet.

- [ ] **Step 4: Promote or close findings, then commit evidence**

Apply the Defect Promotion Protocol to every `NEEDS_TEST` row. When no row remains unresolved:

```bash
git add docs/superpowers/audits/2026-08-04-denaro-xp.md
git commit -m "docs: verifica economia principale"
```

---

### Task 4: Audit shop, consumables, trophies, and progress side effects

**Files:**
- Modify: `docs/superpowers/audits/2026-08-04-denaro-xp.md`
- Inspect: `src/handlers/shop.py`, `src/services/shop_service.py`,
  `src/services/consumable_service.py`, `src/services/badge_service.py`,
  `src/services/progress_service.py`
- Verify: `tests/integration/test_shop_handlers.py`, `tests/integration/test_shop_service.py`,
  `tests/integration/test_shop_home_balance.py`, `tests/integration/test_consumable_service.py`,
  `tests/integration/test_badge_service.py`, `tests/integration/test_progress_service.py`,
  `tests/integration/test_money_concurrency_pg.py`

**Interfaces:**
- Consumes: core economy guarantees from Task 3.
- Produces: matrix rows for cosmetic and consumable purchase pipelines, ownership/consumption
  idempotency, milestone reads, and all balance-dependent trophy decisions.

- [ ] **Step 1: Run focused SQLite coverage**

```bash
.venv/bin/pytest tests/integration/test_shop_handlers.py tests/integration/test_shop_service.py tests/integration/test_shop_home_balance.py tests/integration/test_consumable_service.py tests/integration/test_badge_service.py tests/integration/test_progress_service.py -v
```

- [ ] **Step 2: Run the PostgreSQL purchase race guards**

```bash
TEST_PG_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/gamingbot_test .venv/bin/pytest tests/integration/test_money_concurrency_pg.py -k 'Shop or Purchase' -v
```

- [ ] **Step 3: Complete shop/progress matrix rows**

Trace debit → purchase/consumption row → apply/toggle → milestone → commit for every shop action.
Check that a duplicate callback cannot debit twice, an insufficient debit leaves no purchase row,
the actor is always `callback.from_user.id`, and balance trophies read the post-debit/post-credit
truth. Record `Badge.xp_reward` as `NOT_APPLICABLE`: `STEERING.md §12` explicitly defines it as a
display-only value and trophies do not mutate XP.

- [ ] **Step 4: Promote or close findings, then commit evidence**

Apply the Defect Promotion Protocol; then:

```bash
git add docs/superpowers/audits/2026-08-04-denaro-xp.md
git commit -m "docs: verifica acquisti e progressi"
```

---

### Task 5: Audit betting placement, settlement, refunds, and scheduled transitions

**Files:**
- Modify: `docs/superpowers/audits/2026-08-04-denaro-xp.md`
- Inspect: `src/services/bet_service.py`, `src/handlers/betting.py`,
  `src/handlers/admin_betting.py`, `src/handlers/event_types/bet_type.py`,
  `src/services/schedule_service.py`
- Verify: `tests/integration/test_bet_service.py`, `tests/integration/test_bet_locking.py`,
  `tests/integration/test_betting_handlers.py`, `tests/integration/test_admin_betting.py`,
  `tests/integration/test_bet_type_scheduled.py`, `tests/integration/test_money_concurrency_pg.py`

**Interfaces:**
- Consumes: wallet and XP invariants from Tasks 3–4.
- Produces: matrix rows for event claim/lock order, bet debit, uniqueness, wager totals, participant
  XP, payout, winner XP/counter, refund, scheduler auto-lock, and handler transaction ownership.

- [ ] **Step 1: Run focused SQLite coverage**

```bash
.venv/bin/pytest tests/integration/test_bet_service.py tests/integration/test_bet_locking.py tests/integration/test_betting_handlers.py tests/integration/test_admin_betting.py tests/integration/test_bet_type_scheduled.py -v
```

- [ ] **Step 2: Run every PostgreSQL betting race**

```bash
TEST_PG_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/gamingbot_test .venv/bin/pytest tests/integration/test_money_concurrency_pg.py -k 'Bet or Resolve or Cancel or Lock' -v
```

- [ ] **Step 3: Complete betting matrix rows**

Prove the Event → User → Wallet order for placement, resolve, and cancel; the conditional state
claim before payout/refund; a fresh query for pending bets rather than a loaded collection; atomic
`total_wagered`; one bet/participant-XP grant per event; entire-pot payout including rounding
leftover; exact ledger parity; and rollback of status, bet rows, XP and wallet movements on failure.
Compare scheduler and admin entry points so they cannot bypass the same state guard.

- [ ] **Step 4: Promote or close findings, then commit evidence**

Apply the Defect Promotion Protocol; then:

```bash
git add docs/superpowers/audits/2026-08-04-denaro-xp.md
git commit -m "docs: verifica transazioni scommesse"
```

---

### Task 6: Audit quiz and Guess/Sound rewards and idempotent closure

**Files:**
- Modify: `docs/superpowers/audits/2026-08-04-denaro-xp.md`
- Inspect: `src/services/quiz_service.py`, `src/handlers/quiz/play.py`,
  `src/handlers/quiz/lifecycle.py`, `src/services/guess_service.py`,
  `src/handlers/guess/play.py`, `src/handlers/guess/lifecycle.py`,
  `src/services/progress_service.py`
- Verify: `tests/integration/test_quiz_service.py`, `tests/integration/test_quiz_play_session.py`,
  `tests/integration/test_quiz_lifecycle.py`, `tests/integration/test_guess_service.py`,
  `tests/integration/test_guess_play.py`, `tests/integration/test_guess_lifecycle.py`,
  `tests/integration/test_progress_service.py`, `tests/integration/test_money_concurrency_pg.py`

**Interfaces:**
- Consumes: core credit and XP guarantees.
- Produces: matrix rows for answer/attempt uniqueness, close claims, coin schedules, participation,
  correctness/solve/podium XP, podium progress, rollback, and notification-after-commit behavior.

- [ ] **Step 1: Run focused SQLite coverage**

```bash
.venv/bin/pytest tests/integration/test_quiz_service.py tests/integration/test_quiz_play_session.py tests/integration/test_quiz_lifecycle.py tests/integration/test_guess_service.py tests/integration/test_guess_play.py tests/integration/test_guess_lifecycle.py tests/integration/test_progress_service.py -v
```

- [ ] **Step 2: Run PostgreSQL reward and close races**

```bash
TEST_PG_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/gamingbot_test .venv/bin/pytest tests/integration/test_money_concurrency_pg.py -k 'Quiz or Guess or Prize or Close' -v
```

- [ ] **Step 3: Complete game-reward matrix rows**

For quiz and Guess/Sound separately, prove that only one close claim reaches `award_prizes`, all
coin/XP/progress writes share its transaction, duplicate answers/attempts cannot amplify rewards,
podium and consolation schedules preserve their current values, no-finisher behavior is explicit,
and notification failure cannot undo or repeat a committed reward. Record every XP source as
uncapped, matching `STEERING.md §12.1`.

- [ ] **Step 4: Promote or close findings, then commit evidence**

Apply the Defect Promotion Protocol; then:

```bash
git add docs/superpowers/audits/2026-08-04-denaro-xp.md
git commit -m "docs: verifica premi dei giochi"
```

---

### Task 7: Audit XP caps, admin XP, ranks, and every source call site

**Files:**
- Modify: `docs/superpowers/audits/2026-08-04-denaro-xp.md`
- Inspect: `src/services/xp_service.py`, `src/handlers/economy.py`, `src/handlers/admin.py`,
  `src/handlers/admin_dashboard.py`, `src/services/bet_service.py`,
  `src/services/quiz_service.py`, `src/services/guess_service.py`,
  `src/handlers/leaderboard.py`, `src/handlers/common.py`, `src/handlers/badges.py`
- Verify: `tests/unit/test_xp_service.py`, `tests/integration/test_xp_admin.py`,
  `tests/integration/test_admin_dashboard_money.py`, `tests/integration/test_leaderboard_screens.py`,
  `tests/integration/test_money_concurrency_pg.py`

**Interfaces:**
- Consumes: source behavior already traced in Tasks 3, 5, and 6.
- Produces: one matrix row per `XpSource` caller plus `grant_xp`, `set_xp`, `airdrop_xp`, cached
  `rank_slug`, daily counters, and XP leaderboard reads.

- [ ] **Step 1: Prove the mutation boundary is complete**

Run:

```bash
rg -n 'User\.xp|\.xp\s*[+\-*/]?=|\.values\([^)]*xp' src --glob '*.py'
rg -n 'grant_xp\(|set_xp\(|airdrop_xp\(' src --glob '*.py'
```

Expected classification: all mutations are inside `src/services/xp_service.py`; other matches are
reads, call sites, or catalog/model declarations. Every call site must supply an explicit `XpSource`
and `capped=` value.

- [ ] **Step 2: Run focused SQLite XP coverage**

```bash
.venv/bin/pytest tests/unit/test_xp_service.py tests/integration/test_xp_admin.py tests/integration/test_admin_dashboard_money.py tests/integration/test_leaderboard_screens.py -v
```

- [ ] **Step 3: Run PostgreSQL XP races**

```bash
TEST_PG_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/gamingbot_test .venv/bin/pytest tests/integration/test_money_concurrency_pg.py -k 'Xp or XP or Daily or Airdrop' -v
```

- [ ] **Step 4: Complete XP matrix rows**

Prove concurrent capped grants cannot exceed the daily cap, uncapped grants cannot lose updates,
airdrop cannot overwrite a concurrent grant, `set_xp` has defined absolute-write serialization,
`xp_today` resets on the same local-day boundary as `/daily`, rank cache follows every mutation,
admin audit amounts match actual changes, and leaderboard/profile reads do not rely on stale values.
Keep `daily` capped and every event/admin source uncapped.

- [ ] **Step 5: Promote or close findings, then commit evidence**

Apply the Defect Promotion Protocol; then:

```bash
git add docs/superpowers/audits/2026-08-04-denaro-xp.md
git commit -m "docs: verifica mutazioni XP"
```

---

### Task 8: Close every matrix row and run final gates

**Files:**
- Modify: `docs/superpowers/audits/2026-08-04-denaro-xp.md`, `STEERING.md` only if a proven fix
  introduced a new normative invariant
- Verify: entire repository

**Interfaces:**
- Consumes: all domain matrices and any appended defect-fix tasks.
- Produces: final audit with no unresolved rows, verified unchanged balance rules, and reproducible
  SQLite/PostgreSQL evidence.

- [ ] **Step 1: Repeat the whole-repository mutation searches**

Run the four Task 2 searches again. Cross-check every result against a matrix ID. Expected: zero
unclassified production matches and zero `NEEDS_TEST` statuses.

- [ ] **Step 2: Verify economic constants did not change**

Run:

```bash
git diff --word-diff=porcelain HEAD~1 -- src/config_data src/services/prizes.py catalogs
git diff --word-diff=porcelain --merge-base origin/test_giu HEAD -- src/config_data src/services/prizes.py catalogs
```

The first comparison covers the last task; the merge-base comparison covers the audit branch. Review
every match and record that no amount, prize, payout formula, XP cap, source classification, rank,
or threshold changed. Use the actual pre-audit commit recorded in Task 1 instead of the merge-base if
the branch already contained unrelated changes in those files.

- [ ] **Step 3: Run final PostgreSQL and global gates**

Run separately:

```bash
TEST_PG_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/gamingbot_test .venv/bin/pytest -m pg -v
TEST_PG_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/gamingbot_test .venv/bin/pytest -v
TEST_PG_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/gamingbot_test .venv/bin/pytest --cov=src --cov-report=term-missing
.venv/bin/ruff check src/ tests/
.venv/bin/mypy
PYTHONPATH=src .venv/bin/python -c 'import main'
git diff --check
```

Expected: zero failures, all `pg` tests executed, coverage at least 99%, Ruff clean, configured mypy
clean, import exit 0, and no whitespace errors.

- [ ] **Step 4: Finalize and commit the audit**

The report must contain: baseline/final summaries, all matrix rows, confirmed defects and their fix
commits, verified-safe paths, residual risks with explicit reasons, and a statement that gameplay
values were preserved.

```bash
git add docs/superpowers/audits/2026-08-04-denaro-xp.md STEERING.md
git commit -m "docs: chiude audit denaro e XP"
```

- [ ] **Step 5: Stop the disposable container**

After all test processes finish and the report records their output:

```bash
docker stop gaming-community-bot-pg-test
```

Because it was created with `--rm` and no volume, stopping it removes only the disposable container
and its test data. If Task 1 selected a suffixed name, use that exact recorded name.

---

### Task 3A (appended on 06 Aug 2026 — confirmed defect): Unify the `/daily` claim and its XP side effects into one transaction

**Origin:** Task 3, CORE-4a. The plan asked to prove the daily claim and its XP/milestone
side effects share one handler transaction. Inspection showed the claim commits at
`handlers/economy.py:112` **before** the capped-participation XP grant (`:126`) and milestone check
(`:130`) which commit at `:133`. Reproduction
`tests/integration/test_economy_handlers.py::TestDaily::test_a_failed_xp_side_effect_discards_the_claim`
fails deterministically (2/2 runs): a failure between the two commits leaves the coin reward, the
`last_daily_claim` marker, the streak and the `daily_reward` ledger entry committed while the XP is
lost forever (the marker blocks a retry). Violates the atomicity invariant: a protected error must
not leave reward/claim/XP in disagreement.

**Files:**
- Fix: `src/handlers/economy.py`
- Test (permanent reproduction, already RED): `tests/integration/test_economy_handlers.py`

**Root cause:** the reward `claim_daily` credit is committed ahead of the XP/milestone block, so
they run in two transactions instead of one. The canonical in-repo pattern (transfer, settlement,
quiz/guess close) keeps every money/XP write in one transaction committed at the end.

**Minimal change:** in `cmd_daily`, remove the commit after `claim_daily`, move the capped XP grant
and the milestone check inside the same `try`, and commit once at the end:

```python
try:
    reward, streak = await economy_service.claim_daily(db_session, message.from_user.id)
    xp_res = await xp_service.grant_xp(
        db_session, message.from_user.id, settings.xp_per_daily_claim,
        XpSource.daily, capped=True,
    )
    newly_earned = await badge_service.check_and_award_milestones(
        db_session, message.from_user.id
    )
    await db_session.commit()
except DailyAlreadyClaimedError as e:
    ...
```

The `DailyAlreadyClaimedError` / `WalletNotFoundError` branches are already reached before any DB
write, so they need no rollback. On any other exception nothing commits and the middleware session
close discards the whole unit. No user-facing message or reward value changes.

**Focused gates:**
```bash
.venv/bin/pytest tests/integration/test_economy_handlers.py -v
.venv/bin/pytest tests/integration/test_economy_service.py tests/integration/test_economy_locking.py tests/integration/test_admin_dashboard_money.py -v
.venv/bin/pytest tests/unit/test_xp_service.py -v
TEST_PG_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/gamingbot_test .venv/bin/pytest tests/integration/test_money_concurrency_pg.py -k 'Daily' -v
```

**Global gates:** full `--cov=src` (≥99%), `ruff check src/ tests/`, `mypy`,
`PYTHONPATH=src python -c 'import main'`.

**Commit:**
```bash
git add src/handlers/economy.py tests/integration/test_economy_handlers.py
git commit -m "fix: il premio giornaliero e i suoi side effect XP viaggiano in un'unica transazione"
```

---

## Plan Self-Review Record

- Scope is split from A.1: callback conversion completes first; this plan audits money/XP second.
- Every direct `Wallet.coins`, `User.xp`, `xp_today`, ledger, payout/refund, shop, daily, transfer,
  admin, quiz, Guess/Sound, trophy/progress, and payment-gating path maps to Tasks 2–7.
- Real races and locks use PostgreSQL; deterministic identity-map cases may use SQLite or one
  SQLAlchemy session when that exactly reproduces the mechanism.
- The fixture can destroy only a database ending in `_test`; `DB_URL` never points at the container.
- No production change is authorized without repeatable RED evidence and a newly written exact fix
  task. Safe paths remain unchanged and are still recorded.
- Current balance values and rules remain preserved; rebalancing stays outside this plan.
