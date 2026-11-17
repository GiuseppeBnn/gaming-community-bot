# Telefono senza fili — rollout test

Attività approvata: staffetta **testo → immagine**, senza audio, senza roster fisso
e senza premi monetari/XP. Non abilitarla in produzione durante questo rollout.

## Utilizzo

Per provare **soltanto la generazione**, un admin può usare
`/Immagine un pinguino che vende gelati sulla luna` (anche `/immagine`). Accetta
1–150 caratteri e usa la stessa route, stile fumetto, timeout e budget, senza
creare eventi/turni/premi. Una sola richiesta simultanea per admin, nessun retry
automatico. L'immagine viene inviata nella chat in cui si esegue il comando;
per un test privato, inviarlo in DM al bot. Il ledger distingue `admin_image`
da `telephone_image`, ma la lane mensile resta la stessa.

1. Admin, in privato: `/eventi` → **Telefono senza fili** → crea → prepara.
2. Apri la staffetta pronta e conferma **Avvia ora**, oppure programmala.
3. Nel gruppo compare una sola card, con **Prendo la staffetta** verso il privato.
4. La prima persona inventa una scena di 1–150 caratteri, rispondendo al prompt.
   Le altre vedono solo l'ultima immagine e la descrivono. L'AI ridisegna alla cieca.
5. Ogni autore continua a ricevere in privato il risultato della propria descrizione.
   Dopo quattro immagini, o alla chiusura/scadenza, la card mostra l'ultima immagine
   e la scena di partenza: **Guarda l'inizio/Guarda il risultato** confronta prima e
   dopo, **Ricostruisci la catena** apre tutti i passaggi con descrizione e autore.
   Con un solo passaggio si mostra direttamente quello, senza un confronto fittizio.
   Nessuna nuova generazione o messaggio gruppo per sfogliare/confrontare.

La richiesta di preparazione non genera immagini. Ogni persona può vedere un
solo turno nella catena (immagine ricevuta e proprio risultato); il doppio tap
non rinnova il tempo. La prenotazione
vale due minuti **dalla consegna**. Se scade, chiunque altro può proseguire.
La durata totale è 30 minuti, salvo completamento di una generazione già ammessa
entro la scadenza. Anche una catena incompleta viene svelata; senza immagini si
chiude con un messaggio breve. Una sola notifica finale risponde alla card;
nessun ping ripetuto ai membri. La notifica è best-effort at-most-once: un crash
dopo la sua prenotazione può perderla, ma la galleria resta accessibile.

Un altro FSM privato va terminato prima di prendere la staffetta. Durante il
gioco occorre rispondere al prompt originale nel DM: altri messaggi e comandi
non sono descrizioni. Dopo un riavvio prompt, claim e passaggi restano nel DB.

## Configurazione e spesa

```dotenv
TELEPHONE_ENABLED=true
TELEPHONE_IMAGE_MODEL=black-forest-labs/flux-3-image-klein
TELEPHONE_IMAGE_PROVIDER=black-forest-labs
TELEPHONE_IMAGE_MAX_USD=0.012
TELEPHONE_IMAGE_TIMEOUT_SECONDS=120
```

L'API key è quella OpenRouter esistente: non servono nuove credenziali. Default
del flag **false**, incluso prod. Le altre variabili hanno già i default sopra.
Con `false` la funzione è invisibile anche agli admin: niente hub, menu, aiuto,
suggerimenti, eventi inline o contesto live AI. `/immagine`, vecchi link, callback
e reply non consentono accesso o generazione; non vengono pubblicate card.
I task già programmati sono nascosti e consumati senza esecuzione né notifiche
quando scadono, non rimandati alla futura abilitazione. Dati e immagini già
archiviati restano intatti; i messaggi Telegram già inviati non vengono cancellati.
Cambiare il flag nell'env dell'istanza e avviare un nuovo processo: non è un toggle
hot-reload. Con Docker usare `docker compose up -d --force-recreate bot`: un semplice
restart e Watchtower conservano l'env del container precedente.

Il flag separa il rollout, non i sorgenti: si può promuovere lo stesso codice in
prod lasciandolo spento e continuare a sviluppare su test senza cherry-pick o
esclusioni manuali di file. Non promuovere l'env di test in produzione.

Modello/provider/prezzo sono copiati nella partita pronta: per cambiarli su una
partita già creata, crearne un'altra (nessun cambio silenzioso durante la catena).

### Resa visiva (17 novembre 2026)

Le immagini sono generate da **FLUX 3 Image Klein**, non dal GLM usato per il testo.
Il prompt fisso richiede ora una scena a fumetti ricca di materiali, luce e profondità,
leggibile da telefono, senza aggiungere nuovi protagonisti o correggere la descrizione
del partecipante. Nessun passaggio LLM aggiuntivo: al provider arriva sempre soltanto
l'ultima descrizione e lo stile, mai la storia della catena.

Per la coppia esatta FLUX 3 Image Klein / Black Forest Labs il gateway specifica
`provider.options["black-forest-labs"].steps=16` **solo se** il discovery fresco
pubblicizza `steps` e lo slug atteso. È il valore raccomandato per il modello
distillato, non un aumento arbitrario a 40/50 step (il modello base è diverso).
Su altri modelli/provider o se il parametro non è più disponibile, nessun override.
Il timeout resta 120s: indica quanto attendiamo, non quanto il modello deve elaborare.
Gli step espliciti potrebbero coincidere con il default del servizio: non promettono
un miglioramento rispetto a ogni immagine precedente. La resa va confrontata su test.
Modello, PNG 1K, tetto di prenotazione e numero di chiamate restano invariati.

Listino verificato il 12 novembre 2026: FLUX 3 Image Klein, provider Black Forest
Labs, 1K, **$0,009/immagine**, quindi circa **$0,036 per quattro immagini**. La
prenotazione prudenziale è $0,012 per chiamata, $0,048 per una catena senza errori. Non
abbassare il ceiling sotto il prezzo effettivo e non contare sulle promozioni.

In caso di timeout/rete/429/5xx, la staffetta va **in pausa**, non viene distrutta:
chi aveva il passaggio può confermare una sola ripresa (con avviso del costo massimo)
o lasciarlo a un'altra persona. Dopo due minuti la prenotazione si libera. Massimo
**due chiamate ammesse per passaggio**, anche se cambia partecipante: fino a otto
chiamate/$0,096 prenotati se vengono confermati tutti i retry. Nessun tentativo
automatico e nessun aumento del budget mensile. Le vecchie staffette già concluse
restano concluse: dopo l'aggiornamento crearne una nuova.

Prima di ciascun POST viene letto il listino pubblico dell'endpoint: ammessa
solo tariffa fissa per singola immagine 1K. Endpoint vincolato e fallback spenti.
Nessuna stima immagini tramite token di testo.
Il budget usa **la stessa lane `openrouter_other` e lo stesso cap mensile globale**
di chat/fun/giudice: non c'è un secondo portafoglio né un aumento automatico.
Esaurita la lane, il gioco si interrompe senza inviare il POST pagato.

**Limite verificato nello schema API:** `ImageGenerationProviderPreferences` non
espone `max_price`, anche se il routing testuale documenta `max_price.image`.
Non viene inviato un parametro non garantito. Il ceiling immagini è quindi un
gate di listino prima della richiesta, con un margine rispetto ai $0,009 attuali;
non è una promessa contrattuale del provider sul prezzo del POST. Un cambio
fra GET e POST può superare la prenotazione; il costo effettivo viene comunque
registrato e le successive richieste ricontrollano il listino. Per un limite
mensile esterno invalicabile, mantenere il cap sulla key OpenRouter non superiore
al budget desiderato. Nessun aumento automatico dei limiti della key.

Fonti ufficiali:

- [Image API e formato risposta](https://openrouter.ai/docs/guides/overview/multimodal/image-generation)
- [Listino e capability endpoint](https://openrouter.ai/api/v1/images/models/black-forest-labs/flux-3-image-klein/endpoints)
- [Routing e max_price.image](https://openrouter.ai/docs/guides/routing/provider-selection#max-price)
- [Schema ufficiale: ImageGenerationProviderPreferences](https://openrouter.ai/openapi.json)
- [Black Forest Labs: modello distillato e 16 step](https://github.com/black-forest-labs/flux-3-image-klein)
- [Deep link Telegram](https://core.telegram.org/bots/features#deep-linking)

## Invarianti tecnici e limiti onesti

- Claim, lease, invio descrizione e completamento sono CAS SQL su root/token/stato.
  Unique `(session_id, user_tg_id)` nelle visite; ledger numerato per i passaggi.
  La race reale si testa su PostgreSQL, non con la falsa concorrenza SQLite.
- Nessuna transazione del gioco rimane aperta durante HTTP o Telegram. I budget
  usano le proprie transazioni tecniche brevi, come per i modelli testuali.
- Payload AI: ultima descrizione e stile fisso. Nessun riferimento alle immagini
  precedenti, nessuna chat di gruppo, nessun riassunto che corregga i fraintendimenti.
- Membership verificata fail-closed prima di consegnare e prima di generare;
  `protect_content` sui DM. Non impedisce screenshot o accordi tra partecipanti.
- Massimo un POST per job ammesso. Un transitorio gestito consente una sola
  conferma manuale, con nuovo token e contatore SQL per tappa. Il secondo errore,
  un errore permanente o un upload Telegram fallito chiudono la catena, senza
  rigenerazioni automatiche. Un job orfano scade dopo 240s e una prenotazione
  finanziaria orfana resta conservativamente impegnata nel budget.
- Sono conservati soltanto `file_id` Telegram e descrizioni nella partita, non
  base64 nel DB. La risposta HTTP è limitata a 12 MiB e validata come PNG/JPEG.
- L'edit della card/reveal è ritentabile dal dettaglio admin senza spendere.
  L'invio della **prima** card ha il limite non eliminabile della Bot API: crash
  dopo invio ma prima del salvataggio può lasciare una card duplicata/orfana.
  Se la card viene cancellata dal gruppo, l'admin deve verificare prima di
  ripubblicare: non si inviano automaticamente sostituzioni a ogni errore Telegram.
- Il listino non certifica qualità/resa in italiano né disponibilità per ogni key:
  prima della promozione a prod serve una partita manuale completa su test.

## Debug del 12 novembre

Ledger e log hanno isolato un timeout durante la generazione a 90s, non un errore
nel claim. Una successiva prova reale con scena sintetica sullo stesso server,
modello, provider e budget ha prodotto un PNG valido da 1.520.575 byte in 8,15s.
Questo conferma il funzionamento della route, non garantisce l'assenza di futuri
stalli del provider. La correzione introduce ripresa manuale limitata, timeout
massimo 120s e diagnostica senza contenuti; non cambia modello senza evidenze.
