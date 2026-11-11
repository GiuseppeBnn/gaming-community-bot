# OpenRouter-only — 11 novembre 2026

Snapshot della prima migrazione. Per i profili successivi, la chat high e i comandi
leggeri Ling Flash, vedi [profili per compito](ai-workload-profiles-2026-11-11.md).

## Decisione e limiti dello scan

Runtime solo OpenRouter: niente attese/retry Groq o Gemini. GLM 5.3 Flash resta
su chat, comandi comici e gioco segreto. Il giudice Guess/Sound conserva
`openai/gpt-oss-120b`, prompt e schema booleano: cambia trasporto e viene ora
contabilizzato. Non cambiamo contemporaneamente provider, modello e regole del giudice.

Catalogo pubblico letto via GET, senza generazioni o chiavi. Prezzi USD per 1M
token, non EUR, senza maggiorazioni di ricarica/imposte. Sono prezzi minimi di
catalogo, non preventivi garantiti per gli endpoint ZDR/strict compatibili.

| Modello | Input | Output | Contesto | Valutazione per questo bot |
| --- | ---: | ---: | ---: | --- |
| GLM 5.3 Flash | 0,0399 | 0,0784 | 1.048.576 | Manteniamo la baseline; reasoning obbligatorio, `low` |
| Ling 3.1 Flash | 0,045 | 0,135 | 1.000.000 | Candidato per comandi brevi; promo, disponibilità e italiano da verificare |
| MiniMax M3.1 Flash | 0,075 | 0,30 | 1.048.576 | Nuovo candidato di ottobre, non un risparmio automatico; thinking obbligatorio |
| DeepSeek V4.1 Flash 1017 | 0,006 | 0,50 | 1.048.576 | Snapshot interessante per input lunghi; output più caro, promo/endpoint da controllare |
| Qwen 3.8 Flash | 0,15 | 0,47 | 1.000.000 | Alternativa già nota, non migliore sul solo prezzo |
| GPT-OSS 120B | 0,15 | 0,60 | 131.072 | Manteniamo il giudice esistente, non il modello creativo |

Fonti primarie: [catalogo API](https://openrouter.ai/api/v1/models),
[GLM](https://openrouter.ai/z-ai/glm-5.3-flash),
[Ling](https://openrouter.ai/inclusionai/ling-3.1-flash),
[MiniMax](https://openrouter.ai/minimax/minimax-m3.1-flash),
[DeepSeek 1017](https://openrouter.ai/deepseek/deepseek-v4.1-flash-1017),
[Qwen](https://openrouter.ai/qwen/qwen3.8-flash).

Le pagine pubbliche possono divergere dal catalogo: durante lo scan MiniMax
mostrava 0,06/0,24 e un banner con listino 0,15/0,60; il GET catalogo dava
0,075/0,30. DeepSeek rolling dava 0,051/0,567, diverso dallo snapshot 1017.
Non fissiamo un nuovo modello basandoci sulla tariffa più favorevole letta in una pagina.

Conclusione: non emerge un motivo economico sufficiente per sostituire subito GLM.
Ling/MiniMax/DeepSeek meritano un confronto sintetico su italiano, tono dei sette
comandi, NON LO SO, proposte di titolo e contesto; benchmark generali non bastano.
Nessun candidato nuovo viene attivato automaticamente o aggiunto a una catena lunga.

## Ordini di grandezza, non previsione della bolletta

Ai minimi GLM sopra, ipotizzando per ogni chiamata **tutti** i 1024 token di
thinking disponibili, oltre alla risposta visibile:

| 1.000 chiamate sintetiche | Input/chiamata | Risposta/chiamata | Totale USD circa |
| --- | ---: | ---: | ---: |
| Comandi brevi | 1.000 | 150 | 0,132 |
| Chat con contesto | 6.000 | 250 | 0,339 |
| Domande del gioco | 2.000 | 32 | 0,163 |

Il giudice GPT-OSS, con 1.000 input + 32 output + 1024 thinking, avrebbe invece
circa 0,784 USD per 1.000 chiamate ai minimi pubblici. L'allowance è un tetto,
non un consumo imposto; `exclude=true` nasconde il reasoning ma non lo rende gratis.
Prezzo effettivo, cache e reasoning reale si leggono dal ledger, non da questi esempi.

## Sicurezza e ottimizzazioni effettive

- Un solo tentativo applicativo su OpenRouter; failover tra endpoint upstream
  consentito solo con prezzo massimo, parametri e privacy rispettati.
- Chat/fun conservano breaker, deadline e limiti pubblici. Nessun retry locale
  dopo timeout ambiguo, per non duplicare una generazione potenzialmente addebitata.
- Prenotazione atomica prima della rete, inclusi i token di thinking; settlement
  sull'usage reale. Errori contabili conservano la prenotazione, non aprono nuova spesa.
- GPT-OSS e GLM hanno policy reasoning esplicita `low`; niente `none` su modelli
  che lo rifiutano. Lo schema strict rimane validato anche localmente.
- Il giudice riusa lo stesso adapter structured: modello/timeout/lane sono parametri,
  non una nuova copia di HTTP, parser e contabilità.
- Nessun taglio del contesto di Alduino, nessuna sintesi lossy, nessuna cache delle
  risposte chat, nessun nuovo giudice AI. Restano i prefissi stabili favorevoli alla
  cache implicita: hit e risparmio non sono garantiti.
- Le vecchie conversazioni continuano dalla memoria locale anche se originariamente
  generate su Gemini/Groq. Nessun upstream interaction ID viene riutilizzato.
- Adapter gratuiti e relative chiavi restano solo per confronti opt-in degli
  harness: non sono scelti dagli handler o dal factory runtime. Campi `.env` legacy
  accettati e scartati tramite whitelist di compatibilità: nessuna opzione morta
  esposta come configurazione funzionante. Le chiavi sconosciute restano errori.

Riferimenti: [routing e max_price](https://openrouter.ai/docs/guides/routing/provider-selection),
[reasoning](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).

## Produzione: intervento strettamente separato

Abilitato soltanto `TWENTYQ_V2_ENABLED=true` nel `.env` prod, con copia protetta
dei valori precedenti. Ricreato solo il bot, stessa immagine, senza pull/build,
senza modifiche a dati, chiavi o altri valori del file. Verificati flag effettivo,
container running e zero restart. Non avviata automaticamente alcuna partita.
I comandi `/gioco` e `/gioco_alduino` restano admin-only per la policy già esistente;
gli utenti partecipano rispondendo alle card.

Lettura dei soli aggregati in transazione SQL read-only: a novembre, al momento
del controllo, 9.771 micro-USD registrati (0,009771 USD), zero prenotazioni pendenti.
È il ledger applicativo, non il saldo o la fattura completa dell'account OpenRouter.
Cap globale 5 USD; gioco 4 USD e tutte le altre richieste 1 USD. Questi limiti
non sono stati aumentati. Dopo la migrazione anche Guess/Sound usa la lane da 1 USD:
una diversa partizione va decisa consapevolmente, non aumentata per nascondere un blocco.

Questa attivazione del flag non distribuisce il nuovo codice OpenRouter-only:
finché non viene rilasciato, l'immagine prod mantiene il proprio routing precedente.

## Smoke test live sintetici

Eseguiti con la chiave locale e DB SQLite temporaneo dedicato, senza dati del
gruppo, senza polling/Telegram, senza scritture al DB prod:

- Otto prompt reali chat/fun: preflight massimo 0,012563 USD, addebito registrato
  0,000609 USD; 8/8 risposte non vuote entro i rispettivi cap di caratteri.
  Latenze osservate 1,33–11,28 s. Un `/complotto` ha inserito una parola araba:
  schema/limiti riusciti non equivalgono a stile perfetto; non abbiamo cambiato
  modello o aggiunto retry per ripulire il campione a posteriori.
- Giudice GPT-OSS: abbreviazione corretta accettata, titolo estraneo rifiutato.
- Gioco GLM: quattro casi attesi SÌ, NO, NON LO SO, proposta di titolo hanno
  restituito i quattro enum corretti e JSON conforme.
- Secondo gruppo di sei chiamate: preflight massimo 0,006515 USD, addebito
  registrato 0,000255 USD. Totale dei due run: 14 chiamate, 0,000864 USD.

Sono smoke test piccoli, non una stima statistica della precisione dei giudici
o una prova comparativa dei candidati nuovi. Non sono stati consumati tentativi,
creati eventi o accreditati premi a utenti reali.

Verifica finale sul codice fermo: **3.316 test passati**, inclusi i 68 PostgreSQL,
coverage **99,11%**, Ruff e mypy puliti, import bootstrap riuscito. Il primo
collegamento al PostgreSQL temporaneo tramite port forwarding locale andava
in timeout; il run finale usa il bridge del container. Database e script dei
test live erano usa-e-getta, separati dal DB di sviluppo e dalla produzione.
