# Audit repository-wide di denaro e XP — design

**Data:** 2026-08-04 · **Branch:** `test_giu` · **Prerequisito:** completamento di A.1 in
[`2026-08-03-fondamenta-presentazione-design.md`](2026-08-03-fondamenta-presentazione-design.md)
**Stato:** ☑ **completato il 2026-08-06** — eseguito da
[`2026-08-04-audit-denaro-xp.md`](../plans/2026-08-04-audit-denaro-xp.md): 32 righe matrice chiuse (0
`NEEDS_TEST`), 1 difetto confermato e corretto (D1 `/daily`, fix `6e40524`), valori economici
invariati, gate finali verdi su PostgreSQL (2314 passed, coverage 99.41%). Report:
[`../audits/2026-08-04-denaro-xp.md`](../audits/2026-08-04-denaro-xp.md).

---

## 1. Obiettivo e sequenza

Il lavoro procede in due fasi sequenziali:

1. completare A.1 convertendo le 13 famiglie handler restanti e tutti i loro producer a
   `CallbackData`;
2. subito dopo, svolgere un audit dell'intero repository su ogni percorso che legge, decide o muta
   denaro e XP.

La separazione impedisce di confondere regressioni di trasporto delle callback con difetti
transazionali. L'audit non è limitato ai flussi toccati da A.1.

## 2. Vincoli di comportamento

L'audit può modificare codice e test di denaro o XP quando esiste un vantaggio dimostrato su
correttezza, concorrenza, ledger, idempotenza, rollback o manutenibilità. Ogni correzione parte da
una riproduzione RED mirata; i percorsi già sicuri vengono documentati senza essere riscritti per
uniformità.

Per ora restano invariati:

- importi, premi e costi;
- formule di payout e distribuzione;
- cap XP giornalieri e sorgenti capped/uncapped;
- ranghi, soglie e regole di bilanciamento;
- risultati di gameplay visibili all'utente.

Il ribilanciamento dell'economia è un lavoro futuro separato.

## 3. Perimetro dell'inventario

L'inventario comprende ogni lettura decisionale e ogni scrittura di:

- `Wallet.coins`;
- `User.xp` e `User.xp_today`;
- ledger e audit amministrativo;
- accrediti, addebiti, set saldo, trasferimenti e airdrop;
- premio giornaliero;
- acquisti di cosmetici e consumabili;
- apertura, puntata, chiusura, liquidazione e rimborso delle scommesse;
- premi di quiz, Guess The Game e Sound Quest;
- trofei, progressione e side effect collegati;
- transizioni di stato che autorizzano, impediscono o rendono idempotente un pagamento.

Per ciascun percorso l'audit registra:

1. proprietario della transazione e punto di `commit`;
2. predicato e aritmetica eseguiti in SQL;
3. eventuali lock e loro ordine;
4. rischio di stato obsoleto nella identity map;
5. chiave di idempotenza o claim dello stato;
6. parità tra saldo, XP, ledger e audit;
7. comportamento in caso di eccezione e rollback;
8. copertura SQLite e PostgreSQL esistente o mancante.

## 4. Confini architetturali

Restano normativi gli invarianti di `STEERING.md`:

- gli handler possiedono il `commit`; i service non committano;
- decisioni e aritmetica concorrenti avvengono in SQL, con condizioni nella `WHERE` e aggiornamenti
  relativi nella `SET`;
- `rowcount` determina chi ha vinto una gara, senza read-check-write in Python;
- gli update usano `synchronize_session=False` e un refresh mirato quando il chiamante deve rileggere;
- le letture decisive preferiscono colonne, non entità ORM potenzialmente stale;
- i lock si usano solo quando SQL atomico non basta, nell'ordine canonico
  Event → User → Wallet e, fra wallet, per `tg_id` crescente;
- `User.xp` viene mutato esclusivamente tramite `xp_service`;
- ogni sorgente XP mantiene la propria classificazione capped o uncapped.

Un difetto scoperto può richiedere una modifica a questi moduli, ma non una deroga silenziosa a tali
contratti. Se l'evidenza mostra che un contratto è sbagliato, la spec deve essere aggiornata e
riapprovata prima di cambiarlo.

## 5. Flusso di analisi e correzione

Per ogni area:

1. si completa l'inventario statico dei percorsi e dei test;
2. si assegna un rischio motivato: concorrenza, doppio pagamento, perdita di aggiornamento, ledger
   divergente, rollback incompleto, identity map stale o assenza di idempotenza;
3. un percorso senza rischio concreto viene segnato come verificato e non modificato;
4. un rischio plausibile viene riprodotto con il test più piccolo capace di fallire;
5. una correzione minima porta il test a GREEN senza cambiare il bilanciamento;
6. si rieseguono test focalizzati, suite PostgreSQL quando pertinente e gate globali;
7. test, correzione ed evidenza vengono chiusi in un commit isolato.

I casi di concorrenza e lost update devono usare connessioni PostgreSQL indipendenti. I difetti di
identity map possono essere riprodotti anche in una singola sessione con mutazioni SQL deterministiche,
se il test dimostra precisamente il problema.

## 6. PostgreSQL locale e sicurezza

I test marcati `pg` usano un container PostgreSQL 16 usa-e-getta, senza volume persistente. Il
database è obbligatoriamente `gamingbot_test`; la fixture `tests/conftest.py::pg_engine` rifiuta un
nome che non termina in `_test` e chiama `drop_all`/`create_all` per isolare i test.

Prima di creare il container si verificano nome e porta. La configurazione prevista è:

- immagine `postgres:16-alpine`;
- nome container dedicato `gaming-community-bot-pg-test`;
- bind soltanto su `127.0.0.1`, porta host `5433` se libera;
- nessun volume;
- database `gamingbot_test`;
- DSN di test
  `postgresql+asyncpg://postgres:postgres@localhost:5433/gamingbot_test`.

`TEST_PG_URL` viene passato soltanto all'ambiente dei comandi di test. `DB_URL` non viene puntato al
container e non si usa mai il database Compose `gamingbot` o un database reale. Il container viene
avviato solo dopo l'approvazione della spec e del piano esecutivo.

## 7. Gestione degli errori

Un fallimento PostgreSQL viene prima classificato come problema infrastrutturale o applicativo. Non
si cambia codice per compensare un container non pronto, una porta occupata o una DSN errata.

Una correzione economica deve preservare l'atomicità: un'eccezione non può lasciare saldo, XP,
ledger o stato di pagamento in disaccordo. I service continuano a propagare gli errori al proprietario
della transazione; il chiamante decide rollback e messaggio utente. Nessun test rosso viene accettato
come regressione prevista.

## 8. Strategia di verifica

Prima dell'audit si misurano due baseline:

1. suite predefinita SQLite, coverage, Ruff, mypy configurato e import smoke;
2. suite reale `pytest -m pg` con `TEST_PG_URL`.

Durante A.1 ogni task segue RED/GREEN sui callback, esegue le guardie strutturali e chiude con i gate
globali. La baseline verificata prima del follow-up è **2125 passed, 30 skipped, coverage 99,41%**;
i 30 skip sono test PostgreSQL non abilitati.

Durante l'audit ogni problema richiede:

- test RED focalizzato;
- GREEN dopo la correzione minima;
- test dell'intera area economica coinvolta;
- `pytest -m pg` per concorrenza, lock o semantica specifica PostgreSQL;
- suite completa con coverage almeno 99%;
- Ruff, mypy configurato e import smoke verdi.

La conclusione produce una matrice finale con percorsi modificati, percorsi verificati sicuri,
evidenze di test e rischi residui esplicitamente accettati. Nessuna voce resta implicita.

## 9. Criteri di completamento

Il lavoro è completo quando:

- tutte le aree del §3 sono inventariate;
- ogni rischio confermato ha test RED, correzione minima e verifica GREEN;
- i percorsi non modificati hanno una motivazione verificabile;
- non sono cambiati valori o regole di bilanciamento;
- suite SQLite e PostgreSQL, coverage, Ruff, mypy e import smoke sono verdi;
- `STEERING.md` riflette ogni nuovo invariante realmente introdotto;
- la matrice finale consente a una sessione futura di ricostruire decisioni e prove.
