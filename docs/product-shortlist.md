# Shortlist di prodotto

Questo documento conserva **soltanto le direzioni approvate dall'utente**. Le idee ancora
in discussione non diventano requisiti e non vanno implementate finché non vengono aggiunte
qui esplicitamente.

**Ultimo aggiornamento:** 2026-08-12

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
vittoria.

La base tecnica deve essere riutilizzata, in quest'ordine, anche per:

- **Misteri di Alduino**;
- **Raid narrativi**.

Provider scelto: **Gemini**, per structured output, free tier e finestra di
contesto ampia. Il contesto lungo deve servire le meccaniche persistenti; non è
una ragione per aggiungere un chatbot generico.
