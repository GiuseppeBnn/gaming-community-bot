# Consumo AI fuori dalla conversazione Alduino

Verifica del 15 settembre 2026 su codice, configurazione locale (solo nomi dei
modelli) e documentazione ufficiale. La conversazione normale di Alduino resta
esclusa da questo intervento.

## Interventi

### Gioco segreto: meno metadati ripetuti

Da quattro turni, la cronologia nel prompt usa `columns: [question, verdict]`
e `rows` di coppie domanda/verdetto. Da zero a tre conserva gli oggetti originali.
Non cambia nessuna domanda, risposta, ordine, dossier, finestra di contesto o
regola di selezione. Il JSON continua a fare escaping dei contenuti utente.
La versione prompt diventa `twentyq-question-v2`; schema dei verdetti e dati
persistiti restano identici.

Successivo aggiornamento: il prompt v3 / schema `twentyq-verdict-v2` sostituisce
il nuovo verdetto `forse` con `non_lo_so`, senza consumare la domanda. Il formato
compatto resta invariato. Le misure live sotto si riferiscono al precedente
prompt v2, non costituiscono una valutazione live del nuovo criterio di astensione.

Sul campione locale con domande «La missione N si gioca in cooperativa?»:

| Turni | Prompt precedente, caratteri | Prompt nuovo | Riduzione |
| --- | ---: | ---: | ---: |
| 0 | 86 | 86 | 0 |
| 4 | 361 | 319 | 42 |
| 12 | 915 | 705 | 210 |
| 24 | 1755 | 1293 | 462 |

Sono dimensioni del testo inviato, **non conteggi token**. La riduzione token e
il risparmio economico effettivi dipendono da tokenizer, dossier e utilizzo.
I test ricostruiscono la cronologia originale dalle righe, inclusi Unicode,
virgolette e marcatori, per verificare che non si perda informazione.

### Cache dei prefissi

Il giudice Guess/Sound colloca tutte le regole prima del titolo canonico, così
anche round diversi condividono lo stesso prefisso. Modello, esempi, criteri,
controlli locali, cache dei verdetti e margine di reasoning non cambiano.

I sette comandi comici collocano le regole comuni prima del personaggio specifico.
Le istruzioni sono conservate, con il riferimento posizionale aggiornato da
«sopra» a «sotto». Questo favorisce il riuso del prefisso tra comandi nei provider
che supportano la cache. Non memorizziamo le battute: ogni invocazione genera una
risposta nuova.

La [cache Groq](https://console.groq.com/docs/prompt-caching) richiede prefissi
identici e una soglia minima dipendente dal modello. Al momento documenta il
supporto per GPT-OSS, non per il Qwen configurato per i comandi comici. Quindi
non attribuiamo un risparmio garantito ai comandi su Groq; il riordino è utile
agli endpoint paid con cache compatibile e al giudice GPT-OSS. Non aggiungiamo
padding per raggiungere le soglie.

### Groq nel gioco: evitare tentativi troncati e fallback inutili

Il gioco consentiva 32 token totali a GPT-OSS: reasoning e JSON condividevano quel
limite. Ora l'adapter riserva 512 token aggiuntivi per il reasoning, mantenendo
`low` e lo schema strict. Il tetto diventa 544 per la domanda del gioco; non è
un consumo obbligatorio di 544 token. Il modello gratuito può consumare più
output rispetto a prima, ma ha spazio per terminare il verdetto anziché fallire
e duplicare la richiesta sul paid. L'effetto reale va misurato in esercizio.

HTTP 400 con codice esatto `json_validate_failed` è classificato come errore di
schema, non configurazione: non disabilita Groq per 15 minuti nelle domande
successive. Gli altri 400 mantengono la gestione precedente. I log espongono
solo la classe di errore controllata, mai corpo, prompt o generazione fallita.
Nessun retry aggiuntivo nella stessa richiesta.

## Modelli verificati

| Uso | Modello configurato | Esito |
| --- | --- | --- |
| Comandi comici, free | `qwen/qwen3.6-27b` | Presente nel catalogo Groq |
| Giudice Guess/Sound | `openai/gpt-oss-120b` | Presente nel catalogo Groq |
| Gioco, free | `openai/gpt-oss-20b` | Presente nel catalogo Groq |
| Gioco, free | `gemini-3.5-flash` | Nessuna data di spegnimento annunciata |
| Fallback paid | `z-ai/glm-5.3-flash` | Catalogo e successo reale già verificati nella diagnosi del 15 settembre |

Fonti: [modelli Groq](https://console.groq.com/docs/models),
[deprecazioni Groq](https://console.groq.com/docs/deprecations),
[deprecazioni Gemini](https://ai.google.dev/gemini-api/docs/deprecations).
I riferimenti a Llama dismessi nella documentazione descrivono migrazioni
passate; non sono route attive. Nessuna sostituzione automatica di modello:
un modello più recente non garantisce qualità equivalente nel nostro caso.

## Prove live autorizzate

18 richieste con soli dati sintetici al modello gratuito `openai/gpt-oss-20b`,
temperatura 0,1, reasoning `low`, JSON schema strict. Nessun messaggio del gruppo
inviato e nessun fallback paid usato in queste prove.

Su quattro casi, il vecchio tetto di 32 token ha prodotto quattro HTTP 400
`json_validate_failed`. Con 544 token, tutte le otto richieste di confronto
hanno restituito JSON valido: sette verdetti corretti su otto; un tentativo
su titolo mascherato ha restituito `no` anziché `usa_risposta`. Il margine risolve
gli errori tecnici osservati, non garantisce la correttezza di ogni giudizio.

Tre confronti ulteriori, con dodici righe sintetiche ripetute di cronologia e
lo stesso tetto 544, misurano il formato compatto usando i token riportati dall'API:

| Caso | Input con oggetti | Input con colonne/righe | Differenza |
| --- | ---: | ---: | ---: |
| Sì | 407 | 369 | −38 |
| No | 397 | 359 | −38 |
| Forse | 423 | 385 | −38 |

Circa 9–10% di input in meno su questi campioni. I primi due verdetti sono
corretti con entrambi i formati; il terzo è errato (`no`) in entrambi. Questa
cronologia ripetitiva serve a isolare il formato e non rappresenta il normale
gioco, che deduplica i turni. Output e reasoning variano tra chiamate (anche in
aumento): non possiamo promettere una riduzione del totale o un risparmio mensile
da questi campioni. Serve un'eval più ampia per misurare la qualità del modello.

## Scelte deliberate e limiti della verifica

- Output e thinking GLM non ridotti: limiti più bassi possono troncare risposte
  e aumentare errori. Il limite massimo non equivale ai token effettivamente usati.
- Contesto non riassunto o tagliato. Nessuna cache semantica che confonda domande
  simili ma logicamente diverse.
- Il giudice mantiene corrispondenze esatte, alias e verdetti già memorizzati
  prima di chiamare l'AI. Il gioco mantiene i propri controlli sui duplicati.
- Non ottimizziamo il whitespace dell'envelope HTTP: normalmente non è il testo
  tokenizzato dal modello e non rappresenta un risparmio di token del prompt.
- I test locali verificano che i dati siano preservati; il piccolo confronto live
  misura errori tecnici e token, non dimostra equivalenza qualitativa generale.
