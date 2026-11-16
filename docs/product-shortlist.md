# Shortlist di prodotto

Questo documento conserva **soltanto le direzioni approvate dall'utente**. Le idee ancora
in discussione non diventano requisiti e non vanno implementate finché non vengono aggiunte
qui esplicitamente.

**Ultimo aggiornamento:** 2026-11-12

## Inline mode

### Approvato: eventi disponibili

L'inline mode deve mostrare esclusivamente:

- eventi **aperti e utilizzabili adesso**;
- eventi **coming soon**, con data e ora di programmazione chiaramente visibili.

Non fanno parte della direzione approvata il picker utenti, le card profilo o altre funzioni
inline discusse in precedenza. La vecchia documentazione del picker resta come storico
tecnico, non come direzione di prodotto corrente.

## AI per gli utenti

### Implementato: Il gioco segreto di Alduino

Gioco collaborativo con catalogo qualificato IGDB, rotazione persistente e
quote personali. Le domande ricevono **SÌ** o **NO**; **NON LO SO** non consuma
la domanda. Nessun nuovo FORSE. Il comportamento delle vecchie 20 Domande è
legacy, non il contratto della versione corrente. Dettagli nella spec v2.

Il runtime AI attuale è **solo OpenRouter**, con corsie distinte per chat,
comandi leggeri, giudice e gioco; budget mensile persistente. Le vecchie
indicazioni Gemini/free tier non sono più la direzione approvata.

### Approvato, rollout su test: Telefono senza fili

Staffetta aperta, senza audio: scena → immagine → descrizione dell'ultima
immagine → nuovo disegno. Quattro passaggi privati, quattro persone diverse,
risultato pubblico sfogliabile. Nessuna lista di giocatori da aspettare.

Prenotazione atomica per i click contemporanei, due minuti dalla consegna e
rilascio del passaggio se qualcuno sparisce. Chi può aver visto una tappa non
rientra dopo. L'AI riceve soltanto l'ultima descrizione: niente ricostruzione
del prompt iniziale. Massimo 30 minuti, finale parziale se necessario.
Nessun premio economico inventato e nessuna attivazione automatica in prod.

Spec operativa: [Telefono senza fili](telephone-2026-11-12.md).

### Direzione futura già approvata

**Misteri di Alduino**, dopo il consolidamento dei motori esistenti. Il raid
narrativo è stato rimosso dal ramo di rilascio perché ancora acerbo: non va
reintrodotto come parte della staffetta.
