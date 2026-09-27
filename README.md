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

## AliExpress (aliexpress.com, ship-to Ukraine, UAH)

```bash
/home/pesua/Projects/assistant/marketplaces/bin/aliexpress <command> [-h] [--json] [--delay SECONDS]
```

Browsing works without an account. The account is optional and used only for your order history
and for `--as-me` price checks. It reads the `aliexpress_cookie` variable from the repo's `.env`:
copy the whole `Cookie` header of any `www.aliexpress.com` request in DevTools while logged in.

### Rate limiting and captchas

- Every command waits 2 s before each HTTP request (`--delay N` or `ALIEXPRESS_DELAY` to change it).
  Never run commands in parallel.
- AliExpress's anti-bot signs its internal (mtop) requests in page JavaScript, so plain HTTP soon gets
  captchas (`RGV587`, `punish`). The client then switches to a real Chrome: headless, with an
  AliExpress page open, it runs the same calls through the page's own `lib.mtop`. It skips plain HTTP
  for the next 3 hours (`.cache/transport.json`). A command that needs the browser takes about 5 s longer.
- If Chrome also hits a human check, a visible window opens by itself: pass the check (a slider or a
  picture puzzle) and the command carries on. The window closes when the command ends.
  `--no-browser` fails instead, and `unblock [--account]` opens the window on demand.
- The browser profiles live in `.cache/browser-profile` and `.cache/browser-profile-account`.
- The account session is read-only and capped at 80 requests a day (`ALIEXPRESS_ACCOUNT_DAILY_LIMIT`).
  It never touches the cart, coupons, orders or messages. The first full history sync takes one
  request per 10 orders (365 orders = 39 requests); later syncs usually take 2–3.

### Workflow for an agent

1. **Category.** `categories "usb c cable"` shows the categories a query lands in, with their ids.
   `categories --find "cable"` searches the ones seen before, offline.
2. **Filters.** `filters "usb c cable" --cat 100000356` lists brands, attributes, ship-from countries
   and price bands. Results are cached for a day.
3. **Listings.** `products "usb c cable" --cat 100000356 --brand UGREEN -f "Length=1 M" --sort orders [--clean] [--sort-score]`.
   Each row shows the price, rating, "sold" and a score, plus flags explaining the bait.
4. **Details.** Run `product <id> --reviews 10` on the finalists. It shows every variant with its
   price, stock and purchase limit, the store, shipping to UA, specs, and which variants reviewers bought.
5. **Compare.** `compare <id> <id> … --diff`.
6. **Brand store.** `stores --brand UGREEN` finds who sells the brand, official stores first.
   `store-products <shop id|product id> --find "cable"` lists a store's range with duplicates merged.
7. **History.** `orders --find "кабель"` shows what you bought before, with the exact variant.
   `product` and `store-products` mark items you already bought.

| Command | What it does |
|---|---|
| `categories "query"` / `categories --find TEXT` | Shows a query's categories (ids for `--cat`), or searches the learnt category cache |
| `filters "query" [--cat ID]` | Lists facets: brands, attributes, categories, ship-from, price bands |
| `products "query" [--cat] [-f NAME=VAL] [--brand] [--sort best\|orders\|price_asc\|price_desc] [--price MIN-MAX] [--ship-from UA] [--choice] [--free-ship] [--four-stars] [--clean] [--sort-score] [--page N]` | Lists listings (60 a page) with bait flags and a score |
| `product <id\|url> [--reviews N] [--desc [--images]] [--as-me]` | Shows variants (price, stock, limits, bulk tiers), bait flags, store, shipping, specs and reviews |
| `reviews <id> [--filter all\|image\|additional\|local] [--page]` | `local` = buyers from Ukraine. Shows the star breakdown, review tags, reviews and the variants reviewers bought |
| `compare <id> <id> … [--diff]` | Prints a side-by-side markdown table |
| `store <product id\|shop id\|name>` | Shows the seller profile: age, % positive, sales, your purchases there |
| `store-products <store> [--find] [--sort orders\|best\|new\|price_asc\|price_desc] [--filter free_ship\|top\|sale\|new]` | Lists a store's range with duplicates merged. The API ignores sorting, so `orders`/`price_*` re-sort the fetched page (40 items) |
| `stores --brand X [--top 6]` | Finds the stores selling a brand, official first (cached 7 days) |
| `orders [--find] [--since] [--status] [--sync\|--full\|--offline]` | Shows your order history from `.cache/orders.sqlite`, which syncs itself every 12 h |
| `order <id>` | Shows one order's lines, subtotal/shipping/coupons/total, delivery estimate and tracking link (cached once final) |
| `session [--check]` | Shows the cookie, today's account requests and the cache state |
| `unblock [--account]` | Opens the Chrome window on demand and waits until a real request passes |

### Bait flags

| Flag | Meaning |
|---|---|
| `bulk` | The headline price is per piece but only from N pieces ("≥ 3 pieces"), or the product has quantity tiers |
| `new-user price` | A welcome deal for new shoppers (often with "Max. 1 pcs/shopper"). Your real price is higher: check with `--as-me` |
| `cheap-sku` / `decoy variants` | The headline price belongs to a variant far below the median, or to one named "only case", "no battery", "1pcs"… |
| `multi-pack` / `pack of N` | Packs of different sizes; compare the price per piece |
| `sold≫reviews` / `young store` | Popularity that the reviews or the store's age don't back up |

### Gotchas

- These are AliExpress's private endpoints (search page JSON, mtop, feedback). Paths and fields can change without notice.
- Guest prices are often new-user deals. Store listings show both prices, and `store-products` prints the regular one.
- A search card has no review count, so its score uses sales as a stand-in. `product` gives the real one.
- There is no category tree endpoint. `categories` learns categories from the searches you run.
- A search with few results is padded with loosely related items; `products` drops them.
- Shipping is computed for the country the response names. `product` warns when that is not UA.
- Guest prices vary between requests (new-user deals are shown to some guest sessions and not others).
  Use `--as-me` for the final candidates.
- Two stores can share a name ("Ugreen Official Store" exists twice). Tell them apart by shop id and age.

## Rozetka (rozetka.com.ua)

```bash
/home/pesua/Projects/assistant/marketplaces/bin/rozetka <command> [-h] [--json] [--delay [SECONDS]]
```

No account or key. **By default everything is limited to goods sold or handled by Rozetka ("Rozetka +")
that are delivered free to Rozetka stores, and out-of-stock goods are hidden.** `--seller any|other|…`,
`--any-delivery` and `--unavailable` lift those limits.

### Rate limiting (mandatory)

Same rules as Epicentr: append `--delay` (1.5 s) to every command when running several in a row, or set
`ROZETKA_DELAY=1.5`. No parallel runs, no loops over dozens of products.

### Workflow for an agent

1. **City.** `city set "Дніпро"` (or `city find "Обухів"`, then `city set "Обухів" --pick N`). It is used
   for the pickup term. Without it, Kyiv is used.
2. **Category.** `search "query"` jumps into a category when the site would, or prints category ids.
3. **Filters.** `filters <id>` shows the facets with counts.
4. **Products.** `products <id> -f "Facet=Value" --brand X --price 1000-3000 --sort cheap [--sort-unit]`.
5. **Details and compare.** `product <id> --reviews 5` and `compare <id> … --diff --delivery`.

| Command | What it does |
|---|---|
| `city [show \| find NAME \| set NAME [--pick N]]` | City for delivery terms (`rozetka/.state.json`, gitignored) |
| `search "query" [--brand X] [--price A-B] [--sort …]` | Search; may redirect into a category listing |
| `filters <category> [filters…]` | Facets with counts, keys and applied values |
| `products <category> [filters…] [--sort-unit] [--page N]` | Listing with prices, unit prices, seller and rating |
| `product <id\|url> [--reviews N]` | Price, pickup term, variants, same model from other sellers, specs, description |
| `compare <id> <id> … [--diff] [--delivery]` | Side-by-side markdown table |

`<category>` is an id (`152499`), a site URL (its filters are kept) or `/ua/<slug>/c<id>/`.
Filters: `-f "Джерело живлення=Акумулятор"`, `-f "Напруга=18|20"`, `-f "крутний=40..80"` (slider),
`--brand Bosch`, `--price 1000-3000`, `--seller rozetka|other|rozetka_only|rozetka_fulfillment|rozetka_eu|any`,
`--any-delivery`, `--sort rank|cheap|expensive|novelty`.

Example:

```bash
R=/home/pesua/Projects/assistant/marketplaces/bin/rozetka
$R city set "Дніпро" --delay
$R search "шуруповерт" --delay
$R products 152499 -f "Напруга=18" --brand DeWalt --sort cheap --delay
$R compare 417079515 417079524 --diff --delivery --delay
$R product 417079515 --reviews 5 --delay
```

### Gotchas

- These are the site's private APIs (`common-api`, `product-api`, `search.rozetka.com.ua`). Paths can change without notice.
- The listing total includes out-of-stock goods. The header says which pages have goods in stock.
- If a filter value doesn't exist in the category, the site drops it (a warning is printed). If a search's filters match nothing, the site returns unfiltered results, and the CLI reports 0 results instead.
- Search only knows the "Rozetka" / "other" seller values and a few facets. For specs, use a category listing.
- Sponsored tiles, which ignore the filters, are removed from listings.
- The site's "Доставка в магазини ROZETKA" facet also covers some supplier-shipped goods that can't be picked up in a store, and free pickup depends on the order total. `product` / `compare --delivery` show the real term and cost (or the cheapest alternative).
- The unit price comes from the name or the selected variant's size, so an odd name can give a wrong or missing figure.
