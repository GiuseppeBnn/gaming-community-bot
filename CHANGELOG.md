# Changelog

Modifiche rilevanti al bot. Formato ispirato a
[Keep a Changelog](https://keepachangelog.com/it/1.1.0/).

## [1.1] - 2026-08-05

### Aggiunto
- **Guess The Game / Sound Quest** — la chiusura automatica del round si può
  impostare anche come **data assoluta** (`AAAA-MM-GG HH:MM`), oltre che in
  secondi dall'avvio. Se la data è già passata, il round non parte finché non la
  si aggiorna.
- **Guess The Game / Sound Quest** — il **tempo per giocatore** si può inserire
  anche in **minuti** (es. `5m`, `5 min`), oltre che in secondi.

### Modificato
- **Guess The Game / Sound Quest** — premi di default allineati al Trivia Nerd:
  **1000 / 500 / 250 / 100**.
- **Riepilogo premi** (tutte le attività) — rimosso il suffisso «→ min X» dalla
  consolazione; il minimo garantito continua a valere nel calcolo, sparisce solo
  dall'etichetta.
- **Trivia Nerd** — limite di lunghezza per ogni risposta ridotto a **30
  caratteri**, allineato a ciò che il bottone di gioco mostra davvero.

### Corretto
- **Sound Quest / Guess The Game** — il pulsante **«Esci dal gioco»** non blocca
  più il bot dopo alcuni tentativi: a fine partita restava un pulsante che
  nessun handler gestiva e lo spinner girava a vuoto.
- **Trivia Nerd** — le **risposte lunghe non vengono più tagliate** durante il
  gioco: la validazione consentiva 100 caratteri ma il bottone ne mostrava solo
  40 (allineata anche la prova admin).
- **Trivia Nerd** — la **descrizione** inserita in creazione ora compare
  nell'annuncio di gruppo e all'avvio della partita in privato; prima non veniva
  mai mostrata.
