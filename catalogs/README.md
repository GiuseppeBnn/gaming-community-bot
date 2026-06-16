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
# poi edita data/*.csv e riavvia il bot
```

- I file vengono letti **una sola volta all'avvio** (`CATALOG_DIR`, default `data`). Per applicare le
  modifiche **riavvia** il bot.
- Se un file **manca** o è **malformato**, il bot usa i **default integrati** e continua a funzionare.
  Le righe non valide vengono **saltate** e segnalate nei log.
- La prima riga è sempre l'**header** (nomi colonna): non rimuoverla.

## `trophies.csv`
`slug,name,description,icon_emoji,category,rarity,xp_reward,condition_type,condition_value,condition_param`

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
  | `item_purchases` | quante volte | **key del consumabile** (`cons_*`) |
  | `category_purchases` | quante volte | **key della categoria** (es. `bevande`) |
  | `shop_purchases` | acquisti totali nella Locanda | — |
  | `podium_count` | quanti podi | **game key** (`trivia` · `guess` · `sound`) |
  | `first_place_count` | quanti 1° posti | **game key** |
  | `collection` | — (lascialo vuoto) | **slug dei trofei prerequisiti separati da `;`** |

  Lascia vuoto `condition_type` per un trofeo assegnato solo manualmente. I tipi *item/category/collection*
  richiedono `condition_param` (senza, la condizione viene ignorata). Le `collection` si sbloccano quando
  possiedi tutti i trofei elencati (anche a catena nello stesso istante).
- `xp_reward`: valore mostrato a schermo (gli XP **non** vengono accreditati automaticamente: gli XP si
  guadagnano dagli eventi, non dai trofei).

## `ranks.csv`
`slug,name,emoji,min_xp`

Titoli cosmetici sbloccati automaticamente quando gli XP del membro superano `min_xp`. Il rango
mostrato è quello con `min_xp` più alto tra quelli raggiunti.

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
