# Cataloghi personalizzabili (CSV)

Questi file definiscono **Trofei**, **Ranghi XP** e **Cosmetici del negozio** senza toccare il
codice. I `.example.csv` qui sono **template**: copiali nella cartella dati montata (`data/`),
rinominandoli **senza** `.example`, ed editali a piacimento.

```bash
cp catalogs/trophies.example.csv               data/trophies.csv
cp catalogs/ranks.example.csv                  data/ranks.csv
cp catalogs/shop_cosmetics.example.csv         data/shop_cosmetics.csv
cp catalogs/consumable_categories.example.csv  data/consumable_categories.csv
cp catalogs/consumables.example.csv            data/consumables.csv
cp catalogs/twenty_questions_games.example.csv data/twenty_questions_games.csv
# poi edita data/*.csv e riavvia il bot
```

- I file vengono letti **una sola volta all'avvio** (`CATALOG_DIR`, default `data`). Per applicare le
  modifiche **riavvia** il bot.
- Se un file **manca** o è **malformato**, il bot usa i **default integrati** e continua a funzionare.
  Le righe non valide vengono **saltate** e segnalate nei log.
- La prima riga è sempre l'**header** (nomi colonna): non rimuoverla.

## `trophies.csv`
`slug,name,description,icon_emoji,category,rarity,xp_reward,condition_type,condition_value,condition_param,hidden`

- `slug`: identificatore unico (non cambiarlo dopo l'assegnazione, altrimenti i trofei già sbloccati
  non vengono riconosciuti).
- `rarity`: `bronze` · `silver` · `gold` · `platinum`.
- `condition_type` (quando si sblocca) e relativi `condition_value` / `condition_param`:

  | `condition_type` | `condition_value` | `condition_param` |
  |---|---|---|
  | `onboarding` | `1` | — |
  | `balance` | CoInn richiesti | — |
  | `daily_streak` | giorni di fila | — |
  | `bets_won` | scommesse vinte | — |
  | `transfers_made` | trasferimenti | — |
  | `xp` | XP richiesti | — |
  | `level` | livello richiesto | — |
  | `item_purchases` | quante volte | **key del consumabile** (`cons_*`) |
  | `category_purchases` | quante volte | **key della categoria** (es. `bevande`) |
  | `shop_purchases` | acquisti totali nella Locanda | — |
  | `podium_count` | quanti podi | **game key** (`trivia` · `guess` · `sound`) |
  | `first_place_count` | quanti 1° posti | **game key** |
  | `event_count` | quante volte | **metric key** (`trivia_last_place` · `trivia_sub30`) |
  | `collection` | — (lascialo vuoto) | **slug dei trofei prerequisiti separati da `;`** |
  | `catalog_complete` | — (lascialo vuoto) | — (possiedi ogni consumabile **e** ogni cosmetico) |
  | `all_trophies` | — (lascialo vuoto) | — (Platino: tutti gli altri trofei auto-sbloccabili) |

  Lascia vuoto `condition_type` per un trofeo assegnato solo manualmente (es. l'ingresso nel server
  Discord, in attesa dell'integrazione). I tipi *item/category/event/collection* richiedono
  `condition_param` (senza, la condizione viene ignorata). Le `collection` si sbloccano quando possiedi
  tutti i trofei elencati (anche a catena nello stesso istante); `all_trophies` (il Platino) ignora i
  trofei manuali, quindi resta ottenibile.
- `hidden`: `true` per un **trofeo nascosto** (mascherato nel catalogo finché non lo sblocchi);
  qualsiasi altro valore (o colonna assente) = visibile.
- `xp_reward`: valore mostrato a schermo (gli XP **non** vengono accreditati automaticamente: gli XP si
  guadagnano dagli eventi, non dai trofei).

## `ranks.csv`
`slug,name,emoji,min_level`

Titoli (nomi rango) sbloccati automaticamente quando il **livello** del membro raggiunge `min_level`.
Il rango mostrato è quello con `min_level` più alto tra quelli raggiunti. Il livello a sua volta deriva
dagli XP tramite una curva geometrica (vedi `XP_LEVEL_BASE` / `XP_LEVEL_GROWTH`): così puoi spostare i
nomi sulle fasce di livello che preferisci senza ricalcolare le soglie XP.

> Nota: la colonna è cambiata da `min_xp` a `min_level`. Un vecchio file con `min_xp` viene ignorato
> (righe non valide) e il bot riparte con i ranghi di default — basta aggiornare l'intestazione.

## `shop_cosmetics.csv`
`key,name,tag_text,emoji,price`

Tag/titoli (cosmetici) acquistabili nella Locanda con i CoInn — acquisto **una tantum**. `tag_text` è
il flair mostrato sul profilo (solo estetico, nessun permesso reale). `price` in monete (intero ≥ 0).
Usa chiavi `tag_*` per non collidere con i consumabili.

## `consumable_categories.csv`
`key,name,emoji,order`

Le categorie del 🍖 Menù della Locanda (raggruppano i consumabili e alimentano i trofei
`category_purchases`). `order` decide l'ordine di visualizzazione (intero, crescente).

## `consumables.csv`
`key,name,emoji,category,price,description`

Cibi e bevande acquistabili **più volte** nella Locanda: ogni acquisto spende CoInn, finisce nella
🎒 Dispensa del membro (mostrata sul profilo) e conta per i trofei del menù. Nessun effetto di gioco,
nessun permesso. `category` deve essere una `key` di `consumable_categories.csv`. Usa chiavi `cons_*`
per non collidere con i tag cosmetici. `price` in monete (intero ≥ 0).

## `twenty_questions_games.csv`

`key,title,aliases,dossier`

Catalogo dei giochi che Alduino può estrarre per 20 Domande. `aliases` usa `|`
come separatore; `dossier` deve contenere almeno 80 caratteri di fatti verificati
su genere, struttura, ambientazione, protagonista e meccaniche. Gemini risponde
esclusivamente da questo dossier: più è concreto, meno risposte finiscono
correttamente in «irrilevante». Le `key` devono essere uniche.
