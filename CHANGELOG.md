# Changelog

Modifiche rilevanti al bot. Formato ispirato a
[Keep a Changelog](https://keepachangelog.com/it/1.1.0/).

## [1.7] - 2026-08-25

### Modificato
- **Guess The Game / Sound Quest — XP come il Trivia Nerd** — l'esperienza ora
  segue lo stesso schema del trivia: **20 XP di base a ogni partecipante** +
  **10 XP se si indovina** + il **bonus podio** (50 / 30 / 20 per 1°/2°/3°).
  L'XP viene assegnato **sempre**, anche nei round senza premi. Le ricompense in
  CoInn restano invariate (podio e consolazione a chi indovina; importo fisso a chi
  non indovina, solo se il round ha premi).

## [1.6] - 2026-08-24

### Modificato
- **Sondaggi — premio e chiusura nello stesso messaggio** — le righe
  «🏆 Premio… / 🏁 Si chiude il…» ora compaiono **sotto il titolo, nello stesso
  messaggio del sondaggio**, quando titolo + descrizione + queste righe stanno nel
  limite di **300 caratteri** di Telegram. Se non ci stanno, restano in un
  messaggio separato come prima.
- **Guess The Game / Sound Quest — i comandi non contano come tentativi** — se
  durante una partita si invia `/start` (capita ri-toccando «avvia» quando non ci
  si accorge che l'attività è già partita) o un altro comando, non viene più
  contato come tentativo: viene semplicemente ignorato e i tentativi restano
  intatti.

## [1.5] - 2026-08-24

### Aggiunto
- **Sondaggi — avviso del premio ai votanti** — alla chiusura di un sondaggio con
  premio, ogni votante premiato riceve ora un **avviso in privato** (come quando è
  un admin a mandare i premi a mano).

### Modificato
- **Guess The Game / Sound Quest — ricompense riviste** — chi **indovina** mantiene
  la **classifica identica** (podio 1°/2°/3° e consolazione a scendere tra i soli
  risolutori); chi **non indovina** riceve ora una **ricompensa fissa** di
  **25 🪙 CoInn + 10 ⚡ XP**, ma **solo se il round ha premi assegnati** (un round
  senza premi non dà nulla a chi non ha indovinato).
- **Ricompensa minima garantita a 25 CoInn** — per Trivia Nerd, Guess The Game e
  Sound Quest il minimo garantito dell'ultimo classificato è stato alzato da 1 a
  **25 CoInn**.
- **Sondaggi — descrizione** — ora si inserisce **subito dopo la domanda** e viene
  mostrata **sotto il titolo, nello stesso messaggio del sondaggio** (non più in un
  messaggio separato). Se domanda e descrizione insieme superano i **300 caratteri**
  (limite di Telegram), il bot lo segnala e chiede una descrizione più corta.
- **Comando `/daily`** — descrizione del comando semplificata nella guida.

## [1.4] - 2026-08-18

### Aggiunto
- **Manda premi a più utenti** — nuovo pulsante «🎯 Manda premi» nella dashboard
  `/admin` → 💰 Economia. Flusso guidato: scegli **XP** o **CoInn** → digita
  l'importo → incolla la lista degli **@username** (uno per riga). Gli utenti
  trovati vengono premiati e avvisati in privato; quelli non trovati sono
  segnalati nel riepilogo. Registrato nell'audit log.
- **Guess The Game / Sound Quest — premi a tutti i partecipanti** — anche chi
  **non indovina** ora entra in classifica e riceve CoInn (oltre agli XP di
  partecipazione che già prendeva). I CoInn seguono la stessa scala decrescente
  dei quiz, estesa a tutti: chi indovina resta sopra (podio 1°/2°/3° riservato a
  loro), i non-solver ricevono la consolazione a scendere. La classifica di
  chiusura mostra tutti, con i CoInn ricevuti e un segno «non indovinato».

### Corretto
- **`/quiz`, `/guessTheGame`, `/soundQuest` (admin)** — dal gruppo, il pulsante
  «gestisci in privato» ora porta **direttamente all'elenco** di quell'attività,
  invece di aprire tutta la dashboard `/admin`.
- **Trofeo «Ehi, ti sei svegliato finalmente!»** — ora viene assegnato
  retroattivamente a chi usa il bot ma non l'aveva ancora ricevuto (tipicamente un
  admin che aveva saltato la schermata delle regole). Nessun effetto per chi lo ha
  già.

## [1.3] - 2026-08-18

### Aggiunto
- **Comandi `/guessTheGame` e `/soundQuest`** — funzionano come `/quiz`: scritti
  da un utente mostrano i round attivi di quel tipo con il pulsante per giocarli
  in privato (o un messaggio chiaro quando non ce ne sono); agli admin mostrano
  la lista di gestione in chat privata. (Nel menù «/» compaiono in minuscolo
  perché Telegram lo impone, ma la grafia con le maiuscole funziona lo stesso.)
- **Sondaggi — premi ai votanti** — in creazione si può decidere se assegnare un
  premio a **ogni votante**: CoInn + XP (di default 25 🪙 + 10 ⚡, personalizzabili
  o nessuno). Il premio viene pagato alla **chiusura** del sondaggio.
- **Sondaggi — descrizione** — si può aggiungere una descrizione opzionale,
  mostrata nel gruppo insieme al sondaggio.
- **Sondaggi — chiusura programmata** — si può impostare una **data di chiusura**
  (`AAAA-MM-GG HH:MM`): all'orario scelto il bot chiude il sondaggio e annuncia
  nel gruppo l'**opzione vincente**. Se il sondaggio ha un premio la data è
  obbligatoria (è il momento in cui si paga); senza premio la data è facoltativa.
- **Sondaggi — gestione dagli Eventi** — dall'elenco si possono ora **eliminare**
  (come per Trivia Nerd, Guess The Game e Sound Quest) e ogni sondaggio ha una
  scheda con avvia / chiudi / programma chiusura / elimina.

### Modificato
- **Elenchi eventi (Trivia Nerd, Guess The Game, Sound Quest, Sondaggi)** —
  rimosso il codice `#numero` prima del titolo: nell'elenco si vede solo il
  **titolo** scelto in creazione.
- **Sondaggi senza premio né data** — restano sondaggi normali
  «spara-e-dimentica» come prima: pubblicati nel gruppo, senza chiusura
  automatica né premi.

## [1.2] - 2026-08-07

### Aggiunto
- **Trofei — Guess The Game** — tre nuovi traguardi sul podio: 🥉 *Indovina
  Chi?* (10 podi), 🥈 *Maestro dei Quiz* (50 podi), 🥇 *Aki-Alduino* (100 podi).
- **Trofei — Sound Quest** — tre nuovi traguardi sul podio: 🥉 *Buon Orecchio*
  (10 podi), 🥈 *Orecchio Assoluto* (50 podi), 🥇 *Shazam Umano* (100 podi).
  Si sbloccano, si annunciano e compaiono nella lista trofei come tutti gli altri.
- **Trofei — Guess The Game** — 7 traguardi in più: 🥉 *Veni, Vidi, Vici* (primo
  podio); *sotto i 30 secondi* 🥉 *Occhio di Falco* (×10), 🥈 *Ray Tracing Umano*
  (×50), 🥇 *Memoria Fotografica* (×100); e tre nascosti *arriva ultimo* 🥉 *Miope*
  (×10), 🥈 *Texture Incomplete* (×50), 🥇 *Schermo Spento* (×100).
- **Trofei — Sound Quest** — 7 traguardi in più: 🥉 *A caccia di indizi* (primo
  podio); *sotto i 30 secondi* 🥉 *Orecchie da mercante* (×10), 🥈 *Radar Sonar*
  (×50), 🥇 *Direttore d'Orchestra* (×100); e tre nascosti *arriva ultimo* 🥉
  *Rumore Bianco* (×10), 🥈 *Sordo come una Campana* (×50), 🥇 *Snake? SNAKE?!
  SNAAAAAKE* (×100).
- I trofei **nascosti** restano mascherati come «???» nel catalogo finché non li
  ottieni, poi si rivelano nella tua lista — come i «arriva ultimo» del Trivia Nerd.

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
