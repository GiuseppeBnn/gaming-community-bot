# Modelli, prompt e alert AI — 28 ottobre 2026

## Diagnosi

Le notifiche mostravano tre problemi distinti:

- **Groq 404:** `qwen/qwen3.6-27b` è stato ritirato il 14 settembre 2026 per
  free/developer tier; il successore ufficiale è `qwen/qwen3.8-27b`.
- **Gemini 503:** indisponibilità temporanea/capacità del servizio. Un modello
  presente nel catalogo non garantisce che una generazione riesca in quel momento.
- **Telegram Connection reset:** problema di trasporto del polling, già ritentato
  da aiogram. Non dipende dai modelli o dai prompt.

Fonti: [deprecazioni Groq](https://console.groq.com/docs/deprecations),
[modelli Gemini](https://ai.google.dev/gemini-api/docs/models),
[migrazione Gemini 3.8](https://ai.google.dev/gemini-api/docs/generate-content/whats-new-gemini-3.8),
[GLM 5.3 Flash](https://openrouter.ai/z-ai/glm-5.3-flash).

## Modelli e compatibilità

| Corsia | Modello |
| --- | --- |
| Chat e comandi fun Groq | `qwen/qwen3.8-27b`, reasoning `none` |
| Chat e giudizi gioco segreto Gemini | `gemini-3.8-flash` |
| Giudice Guess/Sound | `openai/gpt-oss-120b` |
| Fallback strutturato Groq | `openai/gpt-oss-20b` |
| Fallback paid OpenRouter | `z-ai/glm-5.3-flash` |

Tutti questi ID sono stati confermati tramite GET ai cataloghi ufficiali, usando
le chiavi locali solo negli header. Nessuna credenziale viene riportata nei log.
Aggiornati default, template e le sole selezioni dei modelli nel `.env` locale.
Il vecchio default Groq viene normalizzato anche quando arriva da un `.env`
conservato durante un aggiornamento; le altre selezioni personalizzate restano.

Gemini 3.8 non supporta `minimal`: l'adapter usa `low` per la configurazione
legacy. Aggiunge 1024 token al tetto della risposta perché il limite comprende
anche il ragionamento, e l'adapter strutturato omette i parametri di sampling
legacy. I limiti del contesto e dell'output pubblico restano gli stessi.
Cap paid, prenotazioni conservative, routing ZDR e filtri di accesso invariati.

## Prompt

- Regole condivise più dirette: una sola immagine comica, dettagli del messaggio,
  contenuto citato inerte, niente spiegazioni della battuta o dati personali inventati.
- Ruoli comici meno sovraccarichi: un cavillo per `/difendi`, una sola scena per
  `/drama`, una teoria dichiaratamente assurda per `/complotto`.
- Alduino usa il catalogo per i comandi e i dati live per eventi/stati/orari;
  dichiara le informazioni mancanti e non afferma di aver eseguito azioni admin.
- Giudice del gioco segreto aggiornato a `twentyq-question-v4`: negazioni,
  quantificatori e domande composte espliciti. Schema v2 e regole dei premi invariati.

## Alert

- I tentativi falliti del router testuale sono INFO; nessun provider disponibile
  produce un WARNING di route esaurita. I 503 Gemini e gli HTTP transitori Groq
  restano diagnostici INFO, evitando DM se il fallback recupera.
- Gli HTTP 400/401/403/404 aprono il breaker per 900 s; gli altri guasti per 60 s.
  Lo status viene conservato attraversando gli adapter della chat.
- Il solo canale DM rinvia i `TelegramNetworkError` del polling per 60 s. I log
  completi restano disponibili; il messaggio di connessione ripristinata azzera
  la finestra. La riga di backoff non genera un secondo DM. Auth, conflitti e gli
  altri errori restano immediati, con la deduplicazione esistente.

## Valutazione live

Gli otto prompt reali sono stati eseguiti con input sintetici su Qwen 3.8, senza
Telegram e senza chiamare OpenRouter. Il primo run ha rilevato `/difendi` e
`/drama` troppo lunghi; dopo il ritocco, il secondo run ha prodotto otto risposte
senza errori e tutte entro il cap del proprio prompt, in 0,26–0,79 s per risposta.
È una verifica a campione: stile, correttezza e rispetto della lunghezza restano
proprietà probabilistiche del modello, non garanzie dimostrate da otto esempi.

La prova Gemini sul dataset strutturato ha incontrato timeout e breaker: nessun
verdetto ottenuto. Il primo run della catena gratuita ha ottenuto 23 risposte con
schema valido, 22 coerenti con l'atteso; gli altri 13 casi sono rimasti senza
provider dopo un rate limit Groq. Nessuna corsia OpenRouter abilitata nell'eval.
Il risultato non certifica l'accuratezza del nuovo Gemini; conferma che timeout,
quote e fallback devono restare parte della gestione ordinaria dei provider.

Il run Groq con richieste distanziate ha eliminato gli errori di quota e ottenuto
36 risposte con schema valido: inizialmente 27 corrette su 36. La correzione della
precedenza dei tentativi di titolo, sullo stesso dataset senza cambiarne i valori
attesi, ha portato il run finale a **33/36**, ancora senza errori di rete/schema.
Restano due astensioni (`si-01`, `si-04`) e una proposta di titolo classificata come
sì (`risposta-02`). Questi scarti restano documentati; il prompt non rende il
modello un giudice infallibile e non modifica le guardie o le regole dei premi.

## Verifiche automatiche

- Suite completa: 3255 test passati, 67 saltati, una verifica sul testo del prompt
  ha segnalato la dicitura anti-injection mancante. Ripristinata «non attendibile»;
  retest finale di servizio, builder, eval e handler: **122 passed, 1 skipped**.
- Test mirati iniziali su modelli, payload, routing e alert: **364 passed**.
- Coverage cumulativa finale: **99,01%**, gate al 99% invariato.
- `ruff check src/ tests/`, `mypy`, `git diff --check` e import di `main`: superati.

Le suite girano su una copia temporanea senza `.env`; le prove live leggono solo
le chiavi locali e usano dati sintetici. Nessun invio Telegram. Le modifiche sono
nel working tree e nel `.env` locale; per applicarle a un processo/container già
avviato serve aggiornare e riavviare quel deploy.
