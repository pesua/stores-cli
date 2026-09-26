# Marketplaces

CLI tools a coding agent can use to shop for you: browse the catalogue, filter it, keep only what the
nearby store has, and read and compare products. The agent-facing instructions live in the skill
`.claude/skills/marketplace-shopping/SKILL.md`.

## Epicentr (epicentrk.ua)

```bash
/home/pesua/Projects/assistant/marketplaces/bin/epicentrk <command> [-h] [--json] [--delay [SECONDS]]
```

The wrapper runs `uv run --project <repo root>`, so this absolute path works from any directory
or project. You don't need an account or API key. Every command's `-h` lists all its options.

### Rate limiting (mandatory)

- **When you run several commands in a row, append `--delay` to every one.** It waits before each
  HTTP request, including the first one (1.5 s without a value), so back-to-back invocations are
  spaced out too. `EPICENTRK_DELAY=1.5` in the environment does the same.
- Put `--delay` last, or give it a value (`--delay 1.5`). A bare `--delay` in front of a positional
  argument tries to swallow it.
- Never run commands in parallel, and don't loop over dozens of products one by one. Narrow the list
  with filters first. This is a real shop's private API.

### Workflow for an agent

1. **Store.** `epicentrk store` shows the saved store and makes no requests. If none is saved, ask the
   user where they are, run `stores --address "…"` (or `--city`, `--near`), and after they confirm,
   `store set <id>`. The default is currently store 44, Дніпро, Запорізьке шосе, 62-К.
2. **Category.** Run `categories --find "words"`, which works offline once the cache is built.
   `[preset]` entries are ready-made filtered listings. Or use `search "query"`, which can redirect
   straight to a listing path. Search can't filter by pickup, so always finish with a category
   listing.
3. **Filters.** `filters <category> --today` shows what you can narrow by, with counts.
4. **Products.** `products <category> --today --stock [filters] [--sort asc] [--sort-unit] [--limit 60] [--page N]`.
5. **Compare.** Run `compare <slug> … --diff` on the 2–5 shortlisted items.
6. **Details.** `product <slug> --reviews 5 --stores 5` on the finalists: read the reviews and the kit
   contents, check the modifications (another pack size or colour may be cheaper per unit, or in stock).
7. **Recommend.** Give the price, the stock in the store, the URL, and why it beats the alternatives.
   For bulk goods (paint, cement, screws, flooring), compare the unit price, not the sticker price.

| Command | What it does |
|---|---|
| `stores --address "…" \| --city X \| --near LAT,LNG` | Lists stores, nearest first (`--type all` includes Експрес and pickup points) |
| `store set <id>` / `store` | Saves or shows the default store (`epicentrk/.state.json`, gitignored) |
| `categories --find "words"` | Searches the whole category tree, including preset filtered listings. Uses a local cache in `epicentrk/.cache/`, rebuilt weekly or with `--refresh` |
| `categories [slug] [--depth 2]` | Browses the category tree |
| `search "query" [--stock]` | Runs a full-text search, which may redirect to a category listing |
| `filters <category> [--today] [filters…]` | Lists the available filters with counts |
| `products <category> [--today] [--stock] [--sort-unit] [filters…]` | Lists products with unit prices. `--today` means "Забрати сьогодні" in the saved store |
| `product <slug\|url> [--stores N] [--reviews N]` | Shows specs, description, modifications (size/weight/colour, each with its price and stock), stock nearby, and reviews |
| `compare <slug> <slug> … [--diff]` | Prints a side-by-side markdown table of prices, stock and specs |

`<category>` can be a slug (`shurupoverty`), a site URL, or a listing path (`/shop/kraski/fs/cvet-belyy/`).

Filters: `-f "Тип акумулятора=Li-ion"`, `-f "Ємність=2,0|4,0"`, `-f "762=1.2..1.8"` (range), `--brand Makita`,
`--price 1000-3000`, `--seller epicentrk|other`, `--promo економією`, `--sort asc|desc|rating|aktsii|new|amount`.

Example:

```bash
E=/home/pesua/Projects/assistant/marketplaces/bin/epicentrk
$E stores --city Дніпро --delay && $E store set 44 --delay
$E categories --find "фарба інтер'єрна" --delay
$E products kraski --today --stock --sort-unit --limit 60 --delay
$E compare <slug1> <slug2> <slug3> --diff --delay
$E product <slug> --stores 5 --reviews 5 --delay
```

### Gotchas

- This is the site's private API (`api.epicentrk.ua`). Its paths and parameters can change without notice.
- `--delay` and `--json` go after the subcommand (`epicentrk products … --delay`).
- Stock only works for Epicentr's own goods. Marketplace goods (`mplc-…`) show `н/д`.
- `search` can't filter by pickup. Take the category it suggests and run `products … --today`.
- The unit price is parsed from the pack size in the product name (`14 кг`, `250 шт`), so an odd name can give a wrong or missing figure.
- `--sort-unit` only re-sorts the page you fetched.
- `--address` sends the address you pass to OpenStreetMap Nominatim for geocoding.
