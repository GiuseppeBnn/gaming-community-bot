# Shortlist di prodotto

Questo documento conserva **soltanto le direzioni approvate dall'utente**. Le idee ancora
in discussione non diventano requisiti e non vanno implementate finché non vengono aggiunte
qui esplicitamente.

**Ultimo aggiornamento:** 2026-08-13

## Inline mode

### Approvato: eventi disponibili

L'inline mode deve mostrare esclusivamente:

- eventi **aperti e utilizzabili adesso**;
- eventi **coming soon**, con data e ora di programmazione chiaramente visibili.

Non fanno parte della direzione approvata il picker utenti, le card profilo o altre funzioni
inline discusse in precedenza. La vecchia documentazione del picker resta come storico
tecnico, non come direzione di prodotto corrente.

## AI per gli utenti

### In implementazione: Alduino ha scelto un gioco

Gioco collaborativo nel gruppo: Alduino estrae un videogioco da un catalogo
verificato; la community dispone di 20 domande sì/no e 3 tentativi per
indovinarlo. Gemini risponde con output strutturato e non decide localmente una
vittoria. Le risposte pubbliche sono soltanto **SÌ**, **NO** o **FORSE**, senza
spiegazioni. L'estrazione usa una rotazione persistente e bilanciata del catalogo,
non una scelta casuale che favorisca o ripeta pochi titoli famosi.
Il catalogo primario è una cache locale sincronizzata da IGDB: soltanto giochi
principali, pubblicati, documentati e con segnali sufficienti di notorietà. I
titoli manuali integrati restano il fallback e non una dipendenza editoriale.

La base tecnica è ora riutilizzata dai **Raid narrativi**; la prossima direzione
approvata, dopo aver consolidato questo motore, è **Misteri di Alduino**.

Provider scelto: **Gemini**, per structured output, free tier e finestra di
contesto ampia. Il contesto lungo deve servire le meccaniche persistenti; non è
una ragione per aggiungere un chatbot generico.

### Implementato: Raid narrativo

Raid comunitario asincrono in tre fasi, pensato per un gruppo ampio la cui
composizione e presenza cambiano durante la giornata:

- nessun quorum e nessuna fotografia iniziale dei membri;
- ingresso libero nella fase corrente, senza penalità per le fasi perse;
- una tattica per utente e fase, modificabile fino alla risoluzione;
- difficoltà calcolata sulla proporzione delle scelte, mai sul numero assoluto
  dei partecipanti;
- conteggi delle singole tattiche nascosti finché la fase è aperta, per limitare
  effetto gregge e rumore;
- scadenza automatica persistente; una fase totalmente vuota viene prorogata una
  sola volta e poi chiusa come spedizione abbandonata, non come sconfitta;
- avvio immediato e pulsante admin **Risolvi fase ora** per testare tutto senza
  attendere le scadenze.

Gemini crea soltanto la veste narrativa immutabile (boss, scene, indizi e testi)
con JSON Schema e validazione di dominio. Contromosse, danni, HP e risultato sono
locali e deterministici. Se Gemini manca o restituisce dati invalidi viene usato
uno scenario integrato completo; un raid avviato non dipende mai dalla rete.
