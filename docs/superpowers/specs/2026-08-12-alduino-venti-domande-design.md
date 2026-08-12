# Alduino ha scelto un gioco — design

## Obiettivo

Sostituire il vecchio user picker inline con una vetrina read-only degli eventi
pubblici e introdurre il primo gioco AI persistente della community: Alduino
sceglie segretamente un videogioco e il gruppo ha venti domande sì/no e tre
tentativi per indovinarlo.

Il codice deve essere la base, non un prototipo usa-e-getta, per i futuri
"Misteri di Alduino" e "Raid narrativi".

## Confini

- L'inline mostra soltanto eventi **aperti** o **programmati**, con data locale.
  Non espone più profili, saldi o ricerca utenti e non modifica stato.
- Le tipologie di evento pubblicano le proprie card tramite capability opzionali
  del registry; l'inline non contiene rami `if/elif` per quiz, scommesse o giochi.
- Un evento programmato è mostrato solo se è un vero avvio futuro. Le azioni di
  chiusura/lock automatico non sono eventi "coming soon".
- La prima versione di 20 Domande non distribuisce CoInn o XP: la correttezza del
  motore viene prima dell'economia.

## Architettura

### Discovery pubblica

`PublicEvent` è un value object immutabile. Ogni `EventType` può implementare:

- `discover_open(session)` per gli eventi giocabili adesso;
- `describe_scheduled(session, item_id)` per risolvere titolo e testo di una
  `ScheduledTask` futura.

Il servizio `event_discovery` aggrega le capability del registry, filtra e ordina
i risultati. Il renderer Telegram conosce soltanto `PublicEvent`.

### Runtime dei giochi AI

Il runtime usa tre livelli:

1. `AIGameSession`: aggregate root e macchina a stati
   `ready -> running -> finished/cancelled`;
2. `AIGameTurn`: ledger append-only e numerato degli input/output;
3. `TwentyQuestionsGame`: stato specifico del gioco (segreto, dossier, limiti,
   contatori, vincitore).

I futuri giochi aggiungeranno la propria tabella di stato e strategia, ma
riuseranno lifecycle, claim esclusivo, audit dei turni e provider strutturato.

### Concorrenza e chiamate esterne

Un turno viene prenotato con un `UPDATE ... WHERE status='running' AND
pending_token IS NULL`. Il token viene committato prima di chiamare Gemini, così
nessuna connessione o transazione resta aperta durante la rete. Un claim scaduto
è recuperabile dopo il timeout configurato.

Al successo, un secondo update condizionale sul token consuma la risorsa e salva
il turno. Al fallimento, il token viene rilasciato e la domanda non viene
conteggiata. Due messaggi concorrenti non possono quindi consumare lo stesso
numero di turno.

### Contratto AI

`StructuredAIProvider` è una porta asincrona indipendente dal gioco.
`GeminiStructuredProvider` usa `aiohttp`, timeout esplicito, JSON Schema e una
risposta con il solo enum chiuso (`si`, `no`, `forse`). Il testo pubblicato è
costruito localmente: Gemini non può allungarlo né far trapelare il segreto. La
strategia usa thinking `minimal`, indipendentemente dal default del provider,
per non spendere il budget di output in ragionamento su una classificazione ternaria.

La strategia di 20 Domande passa soltanto:

- dossier canonico e immutabile del gioco;
- ledger delle domande già fatte;
- domanda corrente delimitata come contenuto non attendibile.

Il testo dell'utente non può cambiare le regole e la risposta viene validata di
nuovo localmente. Nessun testo libero del modello decide vittorie o premi. I
tentativi di soluzione vengono verificati localmente contro titolo e alias
normalizzati.

### Catalogo

I giochi selezionabili provengono da `twenty_questions_games.csv` in
`CATALOG_DIR`, con fallback integrato di 24 titoli e caricamento una volta all'avvio. Ogni
record contiene titolo, alias e un dossier di fatti verificati. Il target viene
copiato nella sessione: cambiare il catalogo o riavviare il bot non cambia una
partita già creata. Un ledger append-only separato conta le estrazioni e resta
anche se una partita viene eliminata: si pesca tra i giochi meno usati, evitando
la ripetizione immediata, così ogni titolo appare una volta prima del giro successivo.
La breve transazione di estrazione è serializzata in PostgreSQL, quindi due
creazioni concorrenti non osservano lo stesso stato del ledger.

## Esperienza utente

Un admin crea la partita dall'hub Eventi, scegliendo un titolo pubblico. Alduino
estrae il gioco ma non lo mostra. L'admin può avviare subito o programmare.

All'avvio il bot pubblica un messaggio ancora nel gruppo. I membri giocano
rispondendo a quel messaggio:

- una normale domanda viene classificata da Alduino;
- `RISPOSTA: <titolo>` usa uno dei tre tentativi di soluzione.

La card viene aggiornata dopo ogni turno con contatori e cronologia compatta. A
vittoria o risorse esaurite la risposta viene rivelata e la sessione chiusa. La
partita resta gestibile dall'hub e visibile nell'inline finché è aperta.

## Degrado controllato

- Chiave Gemini assente o provider irraggiungibile: la partita resta attiva, il
  claim viene liberato e la domanda non viene consumata.
- Output non conforme allo schema: stesso comportamento; mai pubblicare il corpo
  grezzo della risposta.
- Messaggio Telegram non più editabile: il turno rimane registrato e il bot invia
  una nuova card, aggiornando l'anchor.
- Gruppo non configurato: l'avvio fallisce senza cambiare lo stato `ready`.

## Verifica

Test unitari per parsing/validazione Gemini, catalogo e renderer; test di
integrazione per lifecycle, limiti, claim, vittoria e discovery; test handler con
bot/query fake per gli adattatori Telegram. La suite completa e i gate statici
restano obbligatori prima del deploy.
