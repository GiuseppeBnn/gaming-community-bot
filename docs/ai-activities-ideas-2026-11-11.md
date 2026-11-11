# Proposte di attività AI — 11 novembre 2026

Idee da scegliere, **non approvate né implementate**. Non modificano la shortlist
di prodotto già approvata. Obiettivo: piccoli interventi asincroni, un risultato
condiviso memorabile, niente dipendenza dalla presenza di dieci persone insieme.

## 1. Il colpo impossibile — la proposta più promettente

Il gruppo deve rubare un oggetto assurdo: il bancomat della Locanda, il tasto
"salta tutorial", l'ultimo neurone di Alduino. Ogni persona propone un solo
attrezzo o piano in una riga. L'AI prepara una complicazione e tre strategie
votabili; il gruppo sceglie. Tre scene nell'arco di 24 ore, poi un epilogo che
riutilizza davvero gli oggetti proposti e diventa la locandina testuale del colpo.

Perché potrebbe funzionare: contributi brevi, nessuna build o statistica da
studiare, anche chi arriva tardi capisce la scheda corrente. Non è un raid:
non si fanno attacchi, non si aspettano turni individuali e non c'è HP da macinare.

Solidità: stato/chiusure/voti decisi dal codice, non dall'umore dell'AI. Nessuna
ricompensa economica basata sul giudizio estetico del modello. Tre generazioni
più epilogo per partita, non una per ogni click. Soluzione predefinita se pochi
votano e finale locale se il provider è indisponibile. Input offensivi/personali
esclusi; niente recap della chat intera. Prima versione con massimo cinque
oggetti e voti modificabili fino alla scadenza.

Rischio: romanzo infinito o troppo rumore. Vincoli: una card alla volta, scene
brevi, massimo quattro messaggi automatici per partita, nessuna convocazione spam.

## 2. La fucina delle reliquie

Due persone offrono un oggetto immaginario a testa. Alduino li fonde: "mouse
rotto" + "scusa del lag" diventa il **Topo dell'Alibi Eterno**, con una descrizione
e un difetto comico legato alle offerte. Un voto sceglie quale reliquia entra
nel museo del gruppo. I vincitori portano una firma condivisa sul reperto.

Il bello: produce una collezione di meme *del gruppo*, non una risposta usa-e-getta.
Le reliquie possono ricomparire nei futuri colpi o essere esposte dalla Locanda.

Solidità: oggetti fittizi, nessun potere Telegram, bonus economico o vantaggio
competitivo. Tre proposte generate insieme, poi solo DB e voti. Limite settimanale
e provenienza persistita. Non vendiamo NFT e non creiamo un secondo mercato monetario.
Rischio: contenuti ripetitivi; un solo reperto selezionato a settimana, con archivio
dei precedenti per evitare nomi e difetti quasi uguali.

## 3. Caccia all'impostore creativo

Un tema assurdo, per esempio "peggior oggetto leggendario mai droppato". Gli
utenti inviano anonimamente una frase; Alduino ne aggiunge una, provando a passare
per umano. Il gruppo vota quale sia dell'AI. Reveal finale con la frase incriminata
e una replica di Alduino se viene scoperto.

Il twist: il bot non giudica gli umani; sono gli umani a giudicare il bot.
Si può giocare in due finestre asincrone senza che i partecipanti siano gli stessi.

Solidità: usiamo solo contributi inviati volontariamente, mai messaggi estratti
di nascosto dalla chat o imitazioni di una persona reale. Con tre umani parte il
round, altrimenti viene annullato senza chiamate AI. Un voto per utente aggiornabile,
bottoni identici e ordine casuale persistito. Input normalizzati nella presentazione
per non distinguere l'AI dalla sola formattazione. Niente copia di stile personale,
niente DM automatici a chi non ha avviato il bot. Una generazione per round.
Rischio: l'AI riconoscibile dalla verbosità; limite uguale di caratteri per tutti.

## 4. La redazione dei futuri sbagliati

Il gruppo inventa un gioco che non dovrebbe esistere: sceglie insieme genere,
meccanica vietata e ambientazione. Alduino crea una scheda Steam parodistica,
una recensione entusiasta e una recensione furiosa sullo *stesso* dettaglio.
Il giorno dopo si vota se comprarlo, rimborsarlo o finanziare un DLC ancora peggiore.

Il risultato è un catalogo di videogiochi impossibili creati dal gruppo, con
ritorni comici e sequel, non un generatore casuale di battute.

Solidità: tre scelte strutturate e una generazione finale, nomi fittizi, contenuto
chiaramente parodistico, nessuna finta notizia reale. Prime versioni solo testo,
senza servizi immagini/audio, browsing o altri costi nascosti. Rischio: diventa
un sondaggio vestito da AI; il dettaglio originale votato deve influenzare tutti
gli artefatti e i sequel, altrimenti la feature non vale la complessità.

## Scelta proposta e basi comuni

Partirei dal **colpo impossibile** per l'esperienza condivisa, oppure
dall'**impostore creativo** se vogliamo il miglior rapporto divertimento/semplicità.
La fucina è la migliore estensione persistente: può dare continuità agli altri giochi.

Per tutte: una macchina a stati persistente, input/voti idempotenti, scadenze
gestite dallo scheduler, schema AI validato e retry manuale senza duplicazione,
limite di chiamate/costo per attività oltre al cap mensile. AI solo alla transizione
di fase; nessun token per status, bottoni, conteggi o premi. Contesto minimo
esplicito per attività, non tutta la memoria del gruppo. Nessun premio deciso
da testo libero AI; timeout non lascia l'attività bloccata in eterno.

Si implementa una proposta solo dopo la scelta, con un piccolo pilot manuale
e criteri osservabili: contributi, completamento, messaggi prodotti e costo per partita.
