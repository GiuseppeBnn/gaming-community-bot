# Cataloghi personalizzabili (CSV)

Questi file definiscono **Trofei**, **Ranghi XP** e **Cosmetici del negozio** senza toccare il
codice. I `.example.csv` qui sono **template**: copiali nella cartella dati montata (`data/`),
rinominandoli **senza** `.example`, ed editali a piacimento.

```bash
cp catalogs/trophies.example.csv        data/trophies.csv
cp catalogs/ranks.example.csv           data/ranks.csv
cp catalogs/shop_cosmetics.example.csv  data/shop_cosmetics.csv
# poi edita data/*.csv e riavvia il bot
```

- I file vengono letti **una sola volta all'avvio** (`CATALOG_DIR`, default `data`). Per applicare le
  modifiche **riavvia** il bot.
- Se un file **manca** o è **malformato**, il bot usa i **default integrati** e continua a funzionare.
  Le righe non valide vengono **saltate** e segnalate nei log.
- La prima riga è sempre l'**header** (nomi colonna): non rimuoverla.

## `trophies.csv`
`slug,name,description,icon_emoji,category,rarity,xp_reward,condition_type,condition_value`

- `slug`: identificatore unico (non cambiarlo dopo l'assegnazione, altrimenti i trofei già sbloccati
  non vengono riconosciuti).
- `rarity`: `bronze` · `silver` · `gold` · `platinum`.
- `condition_type` (quando si sblocca): `onboarding` · `balance` · `daily_streak` · `bets_won` ·
  `transfers_made` · `xp`. `condition_value` è la soglia (intero ≥ 0). Lascia vuoti entrambi per un
  trofeo assegnato solo manualmente.
- `xp_reward`: valore mostrato a schermo (gli XP **non** vengono accreditati automaticamente: gli XP si
  guadagnano dagli eventi, non dai trofei).

## `ranks.csv`
`slug,name,emoji,min_xp`

Titoli cosmetici sbloccati automaticamente quando gli XP del membro superano `min_xp`. Il rango
mostrato è quello con `min_xp` più alto tra quelli raggiunti.

## `shop_cosmetics.csv`
`key,name,tag_text,emoji,price`

Tag/titoli acquistabili nel negozio con gli Aldueuri. `tag_text` è il flair mostrato sul profilo
(solo estetico, nessun permesso reale). `price` in monete (intero ≥ 0).
