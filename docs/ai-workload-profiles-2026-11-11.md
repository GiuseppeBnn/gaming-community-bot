# Profili AI per compito — 11 novembre 2026

Estende la migrazione [OpenRouter-only](openrouter-only-2026-11-11.md).
Tutte le corsie sono OpenRouter; nessuna scelta del modello richiede un'altra
chiamata AI. Il comando decide il tier in modo deterministico.

| Compito | Modello di default | Thinking | Allowance extra |
| --- | --- | --- | ---: |
| `/maestro`, `/drama`, `/insulta` | `inclusionai/ling-3.0-flash` | disabilitato | 0 |
| `/complotto`, `/difendi`, `/accusa`, `/dialetto` | `z-ai/glm-5.3-flash` | low | 1024 |
| `/alduino` e reply conversazionali | `z-ai/glm-5.3-flash` | high | 2048 |
| Gioco segreto di Alduino | `z-ai/glm-5.3-flash` | low | 1024 |
| Giudice Guess/Sound | `openai/gpt-oss-120b` | low | 1024 |
| `/d20`, stati, quote, contatori, premi | codice locale | nessuna AI | 0 |

La variante Flash è più leggera nel numero di parametri attivi rispetto ai grandi
modelli generalisti; non basta però scegliere il più piccolo disponibile.
Ling Tiny è stato provato e scartato per questa corsia: italiano poco sensato,
difesa fuori ruolo e dialetto non rispettato. Non impostiamo un modello peggiore
per risparmiare pochi micro-USD su un campione.

Ling Flash ha prodotto risposte più aderenti ai sette prompt sintetici. Solo tre
comandi semplici migrano: il campione non dimostra equivalenza di qualità su ogni
messaggio. In particolare il dialetto rimane sulla corsia standard, e i giudici
che incidono su quote o premi non passano al modello leggero.

Prezzi minimi verificati: Tiny 0,015/0,045 USD per 1M input/output; Flash
0,021/0,063 USD. GLM era 0,0399/0,0784, con thinking obbligatorio.
Il costo effettivo dipende anche dall'endpoint: non è un preventivo garantito.
Fonti: [Tiny](https://openrouter.ai/inclusionai/ling-3.0-tiny),
[Flash](https://openrouter.ai/inclusionai/ling-3.0-flash),
[catalogo API](https://openrouter.ai/api/v1/models).

## Thinking: aumentarlo dove può aiutare

Solo la chat riceve `OPENROUTER_CHAT_REASONING_EFFORT=high` e
`OPENROUTER_CHAT_REASONING_TOKEN_ALLOWANCE=2048`. Si può tornare a `low` senza
toccare modello o altre attività. GLM non supporta `medium`: il setting accetta
soltanto `low`/`high`. Nessun ritardo artificiale viene introdotto.

La risposta visibile mantiene 280 token come budget di risposta e il taglio
pubblico a 500 caratteri. Il cap API è risposta + allowance, perché reasoning e
testo condividono `max_tokens`. Tutto viene prenotato prima della rete, non soltanto
i token visibili. L'allowance è un tetto, non un consumo obbligatorio o una quota
esatta di reasoning garantita dal provider. `exclude=true` non rende il thinking gratis.

Gioco e giudice rimangono low: spesso il problema è l'informazione mancante,
non la capacità di ragionare. Più thinking non può dimostrare fatti assenti dal
dossier. Nessuna chiamata aggiuntiva di revisione o auto-valutazione.

Fonti: [reasoning e limite totale](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).

## Valutazione live, solo sintetica

DB SQLite temporaneo, nessuna chat reale o Telegram. Tutti i test live hanno
preflight di costo massimo e il normale ledger/cap applicativo.

- Tiny: sette prompt comici più una baseline chat GLM low, costo registrato
  0,000401 USD. Risultati tecnicamente ricevuti ma Tiny scartato per qualità.
- Flash: stessi sette prompt comici più chat GLM high, costo registrato
  0,000516 USD. Campione piccolo, non una prova statistica di qualità equivalente.
- Confronto low/high GLM: due casi, ciascuno con stesso prompt, contesto e
  allowance 2048. Primo: intersezione delle disponibilità di tre persone;
  secondo: orario ufficiale contro prompt injection e falsa assegnazione di CoInn.
  Entrambi i profili hanno risposto correttamente, in 1,41–1,61 s.
  Costo delle quattro chiamate: 0,001423 USD; preflight 0,013320 USD.

Totale: 20 chiamate, 0,002340 USD registrati. Non concludiamo che high sia sempre
più preciso o che debba durare dieci secondi. Il routing può scegliere endpoint
diversi; questo e la telemetria del reasoning impediscono di attribuire una
differenza di costo al solo effort da due esempi casuali:
nel primo confronto persino il conteggio reasoning della risposta high era inferiore.

Lo script `eval_text_ai.py` seleziona le stesse tre corsie del runtime e applica
il profilo chat sia alla chiamata sia al preflight: l'aumento del thinking non
può passare inosservato nella stima del costo massimo.

## Cooldown e interfaccia del gioco

`AI_COOLDOWN_SECONDS=10`: finestra condivisa per comandi AI e chat, per utente.
Admin esenti come prima. Il controllo non consuma il cooldown quando l'input è
invalido. Il rate limiter generale e il cap mensile non cambiano.

Card e guida del gioco dicono esplicitamente SÌ/NO. NON LO SO resta l'astensione
senza domanda consumata o riduzione del premio. Il prompt v5 vieta espressamente
FORSE; schema e parsing continuano a rifiutarlo per nuove generazioni. Vecchi
verdetti `forse` rimangono leggibili come NON LO SO, senza riscrivere i dati storici.

Descrizione abbreviata: rimossi esempio numerico e dettagli duplicati del pool;
preservati quote, penalità, proiezione, XP e condizioni di pagamento. Rimosso il
tempo residuo statico, mantenuta la sola data/ora assoluta. Durata effettiva e
chiusura automatica restano invariate. Anche la card legacy spiega SÌ/NO.

Nel fixture sintetico senza ultimi turni, la card passa da 855 a 745 caratteri
HTML (-13%) e la guida da 1145 a 887 (-23%). Non è una misura dei messaggi reali
di ogni partita: titolo, quote e ultimi turni ne cambiano la lunghezza.

Configurazione locale aggiornata senza leggere o modificare credenziali. Queste
modifiche non sono state distribuite in produzione: il precedente intervento prod
rimane la sola abilitazione del flag del gioco.

Verifica finale: **3.324 test passati**, inclusi i 68 PostgreSQL, coverage
**99,12%**, Ruff e mypy senza errori. Le regressioni verificano profili indipendenti,
prenotazione del thinking, preflight del run, tier effettivi dei comandi, confine
esatto dei 10 secondi, assenza di countdown e gestione di SÌ/NO/NON LO SO.
