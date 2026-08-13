# Raid narrativo asincrono — design approvato

**Data:** 2026-08-13
**Stato:** implementato

## Obiettivo

Creare un evento narrativo comunitario che resti divertente quando il gruppo è
grande, le persone non rispondono tutte e i partecipanti cambiano tra una fase e
l'altra. L'esperienza deve essere immediatamente percorribile in test senza
aspettare il timer reale.

## Rischi di prodotto considerati

1. **Quorum irraggiungibile.** Una soglia assoluta lega la riuscita al numero di
   assenti e peggiora con gruppi più grandi. Il raid usa soltanto proporzioni tra
   le scelte ricevute.
2. **Effetto gregge.** Mostrare i conteggi live trasforma un indizio in un voto
   meccanico per la maggioranza. Durante la fase si mostra soltanto la conferma
   personale; i conteggi arrivano col risultato.
3. **Spam e rate limit.** Non si edita la card dopo ogni click. Un callback
   registra/upserta la scelta e risponde con un toast; la card cambia soltanto a
   risoluzione, proroga o conclusione.
4. **AI fragile o arbitraria.** Il modello non decide mai il risultato. Genera
   una sceneggiatura immutabile validata; un fallback locale copre key assente,
   quota, timeout e schema invalido.
5. **Punire il silenzio.** Una fase senza risposte ottiene una proroga e poi si
   chiude come abbandonata, senza sconfitta, premi persi o messaggi colpevolizzanti.

## Loop

- 3 fasi concatenate; boss con 90 HP.
- Ogni fase offre assalto, difesa e astuzia con etichette narrative specifiche.
- Il codice sceglie una permutazione: ciascuna categoria è efficace esattamente
  una volta nelle tre fasi.
- Un utente ha una scelta per fase, modificabile fino alla risoluzione.
- Il primo voto fissa anche un d20 personale `1..20`; cambiare scelta non lo ritira.
- `>= 3/5` efficaci: colpo decisivo, 40 danni.
- `>= 1/3` efficaci: successo, 34 danni.
- sotto `1/3`: contrattempo, ma avanzamento, 22 danni.
- Prova di compagnia su CD 11: maggioranza di tiri riusciti `+3`, parità
  `+1`, minoranza `+0`; non esistono malus da dado.
- Dopo la terza fase: vittoria con HP a zero, altrimenti finale di sconfitta
  narrativa. Due fasi riuscite e un contrattempo bastano (34+34+22=90), quindi
  serve coordinazione ma non perfezione. Il bonus d20 è limitato a 3 per fase:
  tre tattiche errate causano al massimo 75 danni, mentre una spedizione con un
  colpo decisivo e due contrattempi può essere salvata solo da tre prove eccellenti
  (93 danni). La fortuna ha quindi un momento memorabile senza prendere il posto
  della strategia.

La soglia 11 rende ogni dado senza modificatori un evento 50/50. Una maggioranza
stretta vale il bonus pieno; una divisione esatta vale soltanto +1. In questo modo
il valore atteso resta vicino a 1,5 danni con gruppi dispari e tra 1,25 e 1,5 con
gruppi pari, invece di regalare a due partecipanti il 75% di probabilità del bonus
pieno. I naturali 20 e 1 compaiono nella cronaca ma non cambiano il danno: bonus
per conteggio assoluto crescerebbero automaticamente con la dimensione del gruppo.

## Tempo e partecipazione

La finestra predefinita è 6 ore. Non c'è iscrizione: chi arriva apre la card e
vota nella fase corrente. Una fase vuota viene estesa una volta di 2 ore; se resta
vuota il raid termina `abandoned`. Le scadenze sono task DB persistenti e
idempotenti rispetto a fasi già risolte.

Il gruppo può avere un solo raid `running`: due narrazioni simultanee dividerebbero
la conversazione. Gli avvii concorrenti sono serializzati su PostgreSQL.

Da `/eventi` l'admin dispone di:

- `Avvia ora`, oltre alla normale programmazione;
- `Risolvi fase ora`, abilitato logicamente dopo almeno una scelta;
- `Termina raid`, che produce un ritiro senza penalità.

Questo permette il test completo immediato: crea → avvia → vota → risolvi, per
tre volte.

## Confine AI

Input: tema admin fino a 300 caratteri, delimitato come contenuto non attendibile.
Output Gemini: boss, introduzione, finali e tre scene con titoli, indizi, etichette
e testi successo/contrattempo. JSON Schema più validazione locale limitano forma,
lunghezze e unicità delle scelte. Le contromosse sono decise localmente e passate
alla regia come vincolo tecnico fidato, così gli indizi corrispondono davvero alla
meccanica; il modello non le sceglie e non le restituisce. Anche il fallback
seleziona l'indizio coerente con ogni contromossa. È un blueprint completo, non
una modalità degradata.

## Persistenza

- `AIGameSession`: lifecycle, gruppo, anchor e turn number.
- `RaidGame`: blueprint, fase, HP, deadline, proroga e risultato.
- `RaidAction`: voto corrente/storico e d20 immutabile, con unique per utente e fase.
- `AIGameTurn(kind="phase")`: audit append-only del risultato, inclusi conteggi,
  danno e partecipanti.
- `ScheduledTask(task_type="raid", action="phase", internal=true)`: risoluzione
  durevole, volutamente nascosta da `/programmati` per impedirne la cancellazione
  accidentale.

Se l'edit della card e il reinvio falliscono, la transizione di gioco resta
valida e viene creato un task interno `action=refresh` con retry 1/2 minuti; al
terzo errore il scheduler notifica l'admin. La disponibilità di Telegram non è
quindi parte della transazione che decide danno e fase.

## Fonti che hanno influenzato il design

- Telegram Bot API: `callback_data` è limitato a 1–64 byte e le inline keyboard
  sono il controllo nativo adatto alle scelte: <https://core.telegram.org/bots/api>.
- Tutorial ufficiale Telegram: i callback dei pulsanti non inviano un nuovo
  messaggio in chat, utile per una superficie a basso rumore:
  <https://core.telegram.org/bots/tutorial>.
- Evidenza sperimentale sui giochi di contribuzione a soglia: soglie fisse
  non rimborsabili diventano più problematiche con l'aumento del gruppo, ragione
  per cui qui non esiste quorum assoluto:
  <https://www.sciencedirect.com/science/article/pii/S0304406820300288>.
- D&D Basic Rules, «Group Checks»: ogni membro tira e il risultato viene deciso
  dalla frazione di successi. Il raid ne usa una variante più prudente sulla
  parità e senza malus, adatta a presenze variabili:
  <https://www.dndbeyond.com/sources/dnd/basic-rules-2014/using-ability-scores#GroupChecks>.
