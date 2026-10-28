# Revisione e semplificazione del codice — 28 ottobre 2026

Il bot usa aiogram 3 con sessioni SQLAlchemy async iniettate dai middleware.
Gli handler gestiscono Telegram, FSM e commit; i servizi gestiscono dati e regole.
Il registro dei tipi evento collega hub admin, scheduler e discovery pubblica.
Economia, XP e premi usano transizioni SQL condizionate e un ledger persistente;
AI testuale, giudizi strutturati e contabilità paid hanno confini distinti.

## Semplificazioni

- Router AI strutturato: un costruttore di record e un punto di registrazione per
  tentativo al posto di tre costruttori e sei percorsi ripetuti di audit. Restano
  distinti errori del provider, errori di validazione e difetti di programmazione;
  questi ultimi e le cancellazioni non attivano chiamate paid.
- Intrattenimento: stessa funzione paid per route esplicita e fallback automatico.
- Quiz e Guess/Sound: un solo helper per gli errori di lunghezza, con i nomi
  precedenti ancora disponibili nei moduli condivisi.
- Cooldown: la variante con avviso riusa la decisione della variante silenziosa.
- Negozio: lettura dei tag posseduti con una query aggregata, conservando ordine
  del catalogo, deduplicazione ed esclusione dei consumabili.
- Discovery: riuso dell'orologio UTC dello scheduler.

## Difetti corretti

1. L'annullamento di un task leggeva un'entità ORM che poteva essere obsoleta.
   Ora `UPDATE ... WHERE status = 'pending'` decide l'esito nel database e il
   refresh mantiene aggiornati i riferimenti ORM già presenti. Due annullamenti
   concorrenti hanno esattamente un vincitore.
2. Un payload JSON invalido o non-object nello scheduler interrompeva l'intera
   discovery inline. Ora quel timer viene ignorato e gli eventi validi restano
   disponibili; anche i timer esplicitamente interni restano esclusi.
3. Il chunking contava i caratteri Python: una lista ricca di emoji poteva
   superare il limite previsto in unità UTF-16. Ora anche blocchi e separatori
   sono misurati in UTF-16, conservando l'atomicità dei blocchi HTML.
4. Le risposte AI HTTP 200 contenenti JSON invalido sfuggivano agli errori
   normalizzati, interrompendo il fallback. La gestione copre Groq, OpenRouter,
   Gemini e il giudice; OpenRouter conserva la prenotazione prudente se il costo
   effettivo non è leggibile. Anche error body con `error: null`, array o scalare
   sono gestiti senza far fallire il logging del giudice.

## Verifica

Test di regressione per cache ORM obsoleta, annullamento concorrente su
PostgreSQL, timer corrotti/interni, conteggio query del negozio, messaggi emoji,
JSON AI invalido e propagazione dei difetti degli adapter senza fallback paid.
Le suite AI usano mock HTTP; nessun benchmark paid è necessario.

La verifica completa viene eseguita su copie temporanee senza `.env`, sia per il
commit iniziale sia per il risultato modificato: i test del repo leggono altrimenti
la configurazione locale. Le copie mantengono la struttura relativa del progetto,
necessaria anche per applicare correttamente le esclusioni della coverage.
Il database PostgreSQL usato per le verifiche è un container usa-e-getta dedicato.

### Esito

- Suite completa sul risultato: **3240 passed, 67 skipped**; successivamente
  aggiunta e verificata una regressione per timer di tipi evento non registrati
  (**14 test discovery passati**, coverage aggiornata con `--cov-append`).
- **68 test PostgreSQL passati**, inclusa la gara tra annullamenti concorrenti;
  container temporaneo rimosso dopo la verifica.
- Coverage finale **99,0028%** (14.240 statement, 142 non eseguiti): gate al 99%
  superato senza modificare soglie o esclusioni. La baseline era **98,9967%**
  (14.253 statement, 143 non eseguiti), appena sotto il gate nonostante
  l'arrotondamento del report a «99,00%».
- `ruff check src/ tests/`, `mypy`, import di `main` con token sintetico e
  `git diff --check`: tutti superati.
- **26 test aggiunti**; codice applicativo ridotto di **85 righe nette**.

## Confini preservati

Nessuna dipendenza, migrazione, modifica dei cataloghi o rimozione di funzionalità.
Restano i commit negli handler, i lock e le guardie SQL per denaro/XP, i cap paid,
le policy di privacy, i limiti del contesto AI e i filtri admin/membership.
Il chunking continua a non spezzare un singolo blocco troppo lungo: il chiamante
resta responsabile della dimensione del blocco, come nel contratto precedente.
