# Task 5 — Structured AI providers

## Stato e riferimenti

- Stato: completo.
- Branch: `test_giu`.
- Base verificata prima delle modifiche: `fc3a351fb6894759e490c2e683641ea478de80c9`.
- Head di consegna: commit contenente questo report, con messaggio `feat: aggiungi provider structured fallback`.
- Nessuna richiesta reale verso Gemini, Groq o OpenRouter è stata eseguita: tutti i trasporti dei test sono mockati e i test verificano una singola chiamata fisica.

## File della consegna

- `.env.example`
- `src/config_data/config.py`
- `src/services/ai_service.py`
- `src/services/structured_ai.py`
- `tests/unit/test_config.py`
- `tests/unit/test_structured_ai.py`
- `tests/unit/test_structured_ai_groq.py` (nuovo)
- `tests/unit/test_structured_ai_openrouter.py` (nuovo)
- `.superpowers/sdd/2026-08-23-gioco-segreto-alduino/task-5-report.md` (nuovo)

I file verify-only `src/services/ai_game_service.py`, `src/handlers/twenty_questions.py`, `tests/unit/test_openrouter_service.py`, `tests/unit/test_ai_judge.py` e `tests/unit/test_fun_ai_hardening.py` non sono stati modificati.

## Evidenza RED

La suite rilevante di base era verde: 95 test superati.

Prima di modificare il codice di produzione è stata aggiunta la matrice completa dei test per contratti, configurazione, payload provider, tentativo singolo, normalizzazione errori e Retry-After, validazione schema/enum, usage/costi, privacy, prenotazione e settlement budget, cancellazione e compatibilità legacy Gemini. Il primo run RED eseguibile ha prodotto 4 failure e 81 errori di setup, tutti dovuti ai contratti, alle impostazioni e agli adapter ancora assenti.

Dopo la prima implementazione, tre regressioni privacy aggiunte contro sentinelle sensibili sono risultate RED sui log storici di `ai_service.py`; l'estrazione/hardening provider-neutral ha rimosso tali esposizioni senza cambiare le firme o il comportamento testuale di OpenRouter.

## Implementazione e invarianti

- Introdotti i contratti provider-neutral esatti `StructuredRequest`, `StructuredProviderResult`, `StructuredAIProvider`, `StructuredAIErrorKind` e `StructuredAIError` con metadati sicuri.
- Gli adapter Gemini, Groq e OpenRouter eseguono una sola richiesta e non implementano retry o fallback interni.
- Configurazione Twenty Questions separata da Alduino: Gemini 3.5 Flash/8 s, Groq GPT-OSS 20B/8 s, OpenRouter DeepSeek v4 Flash datato/12 s, con timeout `ge=1`. Rimossi i tre setting Gemini generici anche dal template.
- Payload JSON Schema e opzioni reasoning/privacy/budget corrispondono al brief. OpenRouter usa un solo `model`, fallback disabilitati e prenota sulla lane `twentyq` prima della rete.
- Ogni prenotazione OpenRouter accettata viene regolata su tutti gli esiti. Un errore di settlement è autorevole e scarta anche una risposta valida sollevando `budget_unavailable`; il percorso testuale esistente continua invece a tollerare il fallimento di settlement come richiesto.
- Errori HTTP, autenticazione/configurazione, quota/rate limit, timeout/rete/server, rifiuto, output vuoto/malformato/non conforme/enum/output limit, budget/deadline/unavailable sono normalizzati senza leggere contenuti nei log.
- Usage e costi rifiutano booleani, valori negativi e non finiti. Cancellazione e accounting conservativo sono preservati.
- `GeminiStructuredProvider` mantiene uno shim stretto e testato per il chiamante legacy di Task 8: il percorso autorevole del protocollo accetta `StructuredRequest` e restituisce `StructuredProviderResult`; le vecchie keyword restituiscono temporaneamente il dizionario legacy, usando comunque i nuovi setting dedicati e una sola POST. Non sono stati modificati servizio di gioco o handler.

## Verifica GREEN

- Suite focused/regressioni esatta del brief:
  - `.venv/bin/pytest tests/unit/test_config.py tests/unit/test_no_dead_config.py tests/unit/test_structured_ai.py tests/unit/test_structured_ai_groq.py tests/unit/test_structured_ai_openrouter.py tests/unit/test_openrouter_service.py tests/unit/test_ai_service.py tests/unit/test_ai_judge.py tests/unit/test_fun_ai_hardening.py -q`
  - risultato: 203 test superati, exit 0.
- Type checking Task 5:
  - `.venv/bin/mypy src/services/structured_ai.py src/services/ai_service.py`
  - risultato: nessun problema, exit 0.
- Ruff sui Python modificati/aggiunti:
  - risultato: `All checks passed!`, exit 0.
- Import applicazione:
  - `PYTHONPATH=src .venv/bin/python -c 'import main'`
  - risultato: exit 0.
- Full Docker-free:
  - `.venv/bin/pytest -m 'not pg' -q`
  - risultato: suite completa al 100%, exit 0.
- Full Docker-free con coverage:
  - `.venv/bin/pytest --cov=src --cov-report=term-missing -m 'not pg'`
  - risultato: 2694 superati, 38 deselect `pg`, coverage totale 99,09%, exit 0.
- `git diff --check`: exit 0.

I 38 test `pg` sono esclusi perché la verifica richiesta è Docker-free; Task 5 non modifica schema o flussi PostgreSQL.

## Privacy e audit log

Scansione eseguita:

```text
rg -n 'body\[:|response\.text|system_prompt|user_prompt|thought' \
  src/services/structured_ai.py src/services/ai_service.py
```

Spiegazione di ogni categoria di match:

- `system_prompt` e `user_prompt`: campi/firme legacy, costruzione richiesta, payload outbound e inoltro al budget; nessun uso in chiamate di log.
- `thought`: commento esplicativo, contatore sicuro `thoughtsTokenCount` e filtro delle parti di pensiero; nessun contenuto di pensiero nei log.
- Nessun match per `body[:` o `response.text`.

Una scansione aggiuntiva delle chiamate al logger non trova body, prompt, content, thought, data, text o header. I body non-200 strutturati e testuali vengono letti e scartati; il percorso judge analizza localmente il body solo per distinguere la nota diagnosi `json_validate_failed`, senza registrarlo. I log contengono soltanto metadati sicuri come provider, modello, status, finish reason e contatori token.

La scansione dei setting rimossi non trova `gemini_model`, `gemini_thinking_level` o `gemini_timeout_seconds` in `src`/`.env.example`. La scansione dei segreti trova soltanto placeholder esplicitamente fittizi in `.env.example`; `.env` non è stato toccato e nessun valore reale/account-specifico è presente.

## Evidenza mutation/reversion

- È stata inserita temporaneamente una seconda POST nel ramo non-200: il test rappresentativo Gemini 503 single-attempt è fallito; la mutazione è stata revertita e il test mirato è tornato verde.
- È stato temporaneamente ignorato l'errore di settlement dopo una risposta OpenRouter valida: il test autorevole response-discard è fallito con `DID NOT RAISE`; la mutazione è stata revertita e i test mirati sono tornati verdi.

## Considerazioni residue

Lo shim Gemini e il default di compatibilità di `StructuredAIError.kind` sono intenzionalmente limitati al periodo intermedio prima della migrazione Task 8. Non risultano altri problemi noti o espansioni di scope.
