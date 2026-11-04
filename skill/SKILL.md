---
name: marketplace-shopping
description: Find and compare goods on marketplaces — Epicentr (epicentrk.ua; pickup today in the user's nearby store), Rozetka (rozetka.com.ua; by default only goods sold or handled by Rozetka with free delivery to Rozetka stores), Prom.ua (prom.ua; many small sellers, each with a minimum order sum: search one seller's store and top up a cart to reach it), OLX (olx.ua; classified ads, mostly used goods from private sellers, all of Ukraine or around a city, with risk flags and seller rating) and AliExpress (ship-to Ukraine, UAH; filters out "buy 3"/new-user/decoy-variant bait, reads variants, reviews, stores and the user's own order history). Browse categories, filter listings, read descriptions/specs/reviews, compare candidates and recommend what to buy. Use when the user asks to find, pick, compare or check a product to buy ("знайди в Епіцентрі", "що є в наявності поруч", "підбери шуруповерт", "знайди на розетці", "з безкоштовною доставкою в магазин Rozetka", "знайди на промі", "що ще взяти в цього продавця до мінімальної суми", "знайди на OLX", "б/у", "з рук", "вживаний", "знайди на аліекспрес", "що я купував на алі", "офіційний магазин бренду").
---

# Marketplace shopping

One CLI per marketplace: Epicentr (below), then AliExpress, Rozetka, Prom.ua and OLX in their own sections.

**Where the CLIs are.** This skill lives in `skill/` of the marketplaces repo, and the CLIs are in its
`bin/`: run them as `<this skill's base directory>/../bin/<cli>` (this resolves correctly when the skill
folder is a symlink). `bin/<cli>` below means that path. The wrappers work from any directory.

## Search order

When the user names a marketplace, search only there. Otherwise go step by step, and move to the next
step only when the current one gives no suitable candidate:

1. **Epicentr and Rozetka, free delivery.** Epicentr with `--today` (free pickup in the saved store)
   and Rozetka in its default scope (Rozetka-sold or Rozetka+ goods with free delivery to Rozetka stores).
   Search both and compare them together.
2. **Paid delivery.** Epicentr without `--today` and Rozetka with `--any-delivery` / `--seller any`.
3. **Prom.ua.** Count the seller's minimum order sum and the delivery cost as part of the price.
4. **AliExpress.**

**OLX is a separate branch, not a step.** Search OLX only when the user names it or wants used goods,
second-hand or "з рук" (cheaper than new). Then search only OLX, unless the user also asks to compare
with new goods from the chain above.

Tell the user which step the recommendation comes from and why the earlier steps gave nothing suitable
(not in stock, no such model, much more expensive).

**Always link products.** Every time you mention a specific product to the user (a recommendation, a
finalist, an alternative, a row in a comparison table, an item from the order history), give its full
product URL next to it, not just an id or a slug. **In a table, the link goes into the row itself**
(the product name as a markdown link `[Canon LiDE 110](https://…)`), never into a list under the
table. The CLIs print the URL (`URL:` in `product`, the
numbered list under `compare`). For a listing row that shows only an id, build the URL:
`https://www.aliexpress.com/item/<id>.html`, `https://rozetka.com.ua/ua/<id>/p<id>/`,
`https://epicentrk.ua/ua/shop/<slug>.html`. Prom.ua and OLX rows already include the URL.

## Epicentr

CLI: `bin/epicentrk` (works from any directory; uses the
repo's uv env). Every command takes `-h`, and `--json` for the raw API response.

### Rate limiting — mandatory

**Whenever you run more than one command in a row (which is practically always), pass `--delay` to
every command** (1.5 s before each HTTP request, including the first one), e.g.
`epicentrk products shurupoverty --today --delay`. Put `--delay` last, or give it a value
(`--delay 1.5`), so it doesn't swallow a positional argument. Alternatively, prefix each call with
`EPICENTRK_DELAY=1.5`. Don't run epicentrk commands in parallel, and don't loop over dozens of
products — narrow the list with filters first. This is a private API of a real shop, so don't
hammer it.

### Workflow

1. **Store.** Run `epicentrk store` (no requests). If none is saved, ask the user where they are, then
   run `epicentrk stores --address "<address>"` (or `--city <name>` / `--near LAT,LNG`), and after they
   confirm, run `epicentrk store set <id>`. The store is saved and used by every later command.
   `--type 1` (the default) lists hypermarkets only; `--type all` includes Експрес shops and pickup points.
2. **Category.** Find the listing:
   - `epicentrk categories --find "<words>"` searches the whole tree (~3.3k categories + ~19k presets)
     from a local cache. It is built on the first use (~550 requests); later lookups are free.
     `[preset]` entries are ready-made filtered listings, e.g. "Плитка для ванної".
     Refresh the cache with `--refresh` if it looks stale (it auto-refreshes weekly).
   - `epicentrk search "<query>"` can **redirect** straight to a listing path, or print
     `Category: … → /shop/<slug>/` suggestions.
   - `epicentrk categories [slug]` browses the tree step by step.
   Search can't filter by pickup, so always move on to a category listing.
3. **Filters.** `epicentrk filters <slug-or-path> --today` shows what can be filtered, with counts
   for goods available today in the store.
4. **Products.** `epicentrk products <slug-or-path> --today [--stock] [filters] [--sort asc]`
   - `-f "<filter name or prop id>=<value>"`: several values are joined with `|`
     (`-f "Ємність=2,0|4,0"`). A range takes `-f "762=1.2..1.8"`. Names match case-insensitively,
     and a unique substring is enough.
   - `--brand X` (repeatable), `--price 1000-3000`, `--seller epicentrk|other`, `--promo економією`.
   - `--today` gives the site's "Забрати сьогодні" (pickup today in the selected store).
     `--stock` adds the shelf quantity in that store.
   - `--sort`: `rating` (default), `asc`, `desc`, `aktsii`, `new`, `amount`. Also `--page N` and
     `--limit N` (up to 60).
   - Rows show the **unit price** (`≈56 ₴/кг`, `≈0.18 ₴/шт`, `510 ₴/кв. м (упак. 2.467 кв. м = 1257 ₴)`).
     It comes from the measure field or from the pack size in the name. `--sort-unit` re-sorts the
     fetched page by it. The API can't do that itself, so use `--limit 60` to sort a bigger slice.
   - `модифікацій: N` means the product has sibling sizes, weights or colours.
5. **Details.** `epicentrk product <slug-or-url> [--reviews 5] [--stores 5] [--max-desc 1500]` prints
   the price, unit price, stock in the store, **all modifications** (each with its price, unit price,
   store stock and slug), the full spec table, the description and documents. `--stores N` adds the
   N nearest hypermarkets that have the item in stock.
6. **Compare.** `epicentrk compare <slug1> <slug2> [...] [--diff]` prints a markdown table:
   price, unit price, rating, stock in the store, then every spec aligned by row.
   `--diff` keeps only the rows that differ. 2–5 products stay readable.

### Picking

- Shortlist from the listing, then `compare --diff` the candidates and open the finalists with
  `product` to read the reviews and the kit contents before recommending.
- For bulk goods (paint, cement, screws, flooring), compare the unit price, and check the product's
  modifications: a bigger pack is often much cheaper per kg or litre, but make sure the store has it.
- Stock can only be checked for Epicentr's own goods (`seller: epicentrk`). Marketplace goods
  (`seller: mplc`, slugs starting with `mplc-`) show `н/д` and are never "pickup today".
- Give the recommendation with the price, stock in the store, the product URL and a line on why it
  beats the alternatives.

## AliExpress

CLI: `bin/aliexpress` (works from any directory). Every command
takes `-h` and `--json`. Prices are in UAH and shipping is to Ukraine.

### Etiquette — mandatory

- Commands already wait 2 s before every request. Run them **one at a time**, never in parallel, and
  don't loop `product` over dozens of ids: shortlist from `products` first (2–6 finalists).
- On captchas the tool switches by itself to a real Chrome in the background (about 5 s more per command).
  If AliExpress then wants a human check, a Chrome window opens: tell the user to pass it (a slider or
  a picture puzzle). The command continues on its own. If a command fails with a captcha error anyway,
  stop and tell the user. Don't retry in a loop.
- The account (the user's cookies in `.env`) is only for `orders`, `order` and `--as-me`, with a daily cap
  of 80 requests. Use `--as-me` only for the 1–3 final candidates. The tool is read-only: it can't add to
  cart or buy. Give the user the link.

### Workflow

1. **Category.** `aliexpress categories "<english query>"` gives the category ids for the query
   (AliExpress search works best in English). `categories --find "<words>"` searches the categories seen before.
2. **Filters.** `aliexpress filters "<query>" --cat <id>` lists attribute names and values for `-f`,
   brands, "Shipping from" (`--ship-from UA` means a local warehouse and fast delivery) and price bands.
3. **Listings.** `aliexpress products "<query>" --cat <id> [--brand X] [-f "Name=Val|Val2"] [--sort orders]
   [--price MIN-MAX] [--choice] [--four-stars] [--clean] [--sort-score] [--page N]`.
   - Rows show `⚠` bait flags and `✓ вже купував …` for items in the user's history.
   - `--clean` hides bulk/decoy/multi-pack listings. `--sort-score` re-sorts the page.
4. **Details.** `aliexpress product <id> --reviews 10` on each finalist:
   - Read the **variant table**. The headline price is the cheapest variant, so find the one the user
     actually needs (length, power, "with battery"…) and use *its* price.
   - "що купують" shows which variants reviewers bought.
   - Check the store line (age, % positive), shipping to UA, and flags like `cheap-sku`,
     `decoy variants`, `max 1 шт/покупця`.
   - `aliexpress reviews <id> --filter local` shows reviews from Ukrainian buyers (delivery experience); `--filter image` those with photos.
   - `--desc` fetches the seller description (mostly images; `--images` lists them).
5. **Compare.** `aliexpress compare <id1> <id2> … --diff`.
6. **Brand store.** For a branded product, `aliexpress stores --brand <Brand>` shows who sells it,
   official stores first. `aliexpress store-products <shop id> --find "<model>"` finds the same item in the
   official store, so compare its price and reviews with the reseller's. Several stores can share a name
   (two "Ugreen Official Store"s): prefer the older one with more sales, and quote its shop id.
7. **History.** `aliexpress orders --find "<words>"` (cached, syncs itself every 12 h) answers "what did I
   buy last time / which size". `aliexpress order <id>` shows the price breakdown.
8. **Recommend.** Give the variant to pick (its exact attributes), its real price (a regular price,
   not a new-user deal, unless the user is a new shopper), the store and why it's trustworthy, the delivery
   estimate to UA, and the URL.

### Bait flags to explain to the user

- `bulk: X/шт лише від N шт`: the per-piece price only applies when buying N or more.
- `new-user price`: a welcome deal that regular buyers don't get; `--as-me` shows the user's price.
- `cheap-sku` / `decoy variants`: the listing price belongs to an accessory, "no battery", "1pcs" or similar variant.
- `multi-pack variants` / `pack of N`: compare the price per piece.
- `sold≫reviews`, `young store`: be sceptical of the popularity.

## Rozetka

CLI: `bin/rozetka` (works from any directory). Every command
takes `-h` and `--json`. No account needed.

**Default scope:** every listing and search shows only goods sold by Rozetka or handled by Rozetka
("Rozetka +") **and** delivered free to Rozetka stores ("Доставка в магазини ROZETKA"). Out-of-stock
goods are hidden (`--unavailable` shows them). `--seller any|other|rozetka_only|rozetka_fulfillment`
and `--any-delivery` widen the scope. Only do that when the user asks, or when the default gives
nothing, and say so.

### Rate limiting — mandatory

Pass `--delay` to every command when you run several in a row (1.5 s before each request; put it
last or give it a value). `ROZETKA_DELAY=1.5` does the same. Never run commands in parallel, and don't
loop `product` over dozens of ids: narrow with filters, then open 2–6 finalists.

### Workflow

1. **City.** `rozetka city` shows the city used for delivery terms (Київ if none is saved). Set the
   user's city with `rozetka city set "<name>"` (`city find` / `--pick N` for villages).
2. **Category.** `rozetka search "<query>"` either jumps straight into a category listing
   ("ноутбук asus" → Ноутбуки, brand ASUS) or prints `Категорія [id] (count)` suggestions plus
   matching goods. Search has few filters, so move on to a category.
3. **Filters.** `rozetka filters <id>` lists facets with counts within the default scope. Keys are
   in `[brackets]`, and `*` marks what's applied.
4. **Products.** `rozetka products <id|url> [-f "Facet=Value|Value2"] [-f "Facet=MIN..MAX"]
   [--brand X] [--price 1000-3000] [--sort cheap|expensive|novelty|rank] [--sort-unit] [--page N]`.
   - Facet names and values match case/accent-insensitively, and a unique substring is enough.
   - A pasted site URL keeps its filters, and the default scope is added.
   - The header says which pages hold in-stock goods (the site sorts them first; later pages are
     usually all unavailable).
   - `--sort-unit` re-sorts the page by ₴/кг, л, м² or шт, taken from the name or the selected
     variant (`[2.4 кг]`).
5. **Details.** `rozetka product <id> [--reviews 5] [--review-sort positive_vote_count] [--max-desc 1500]`
   prints the price, the seller, the **Rozetka-store pickup cost and date in the saved city**, variants
   (each with its price and seller), **the same model from other sellers** (often a cheaper Rozetka offer),
   specs, the description and reviews.
6. **Compare.** `rozetka compare <id1> <id2> … [--diff] [--delivery]` prints a markdown table.
   `--delivery` adds the pickup term (1 request per product).

### Picking

- Check "Ця ж модель в інших продавців" in `product`: the same item is often cheaper from Rozetka
  itself or another seller. That list ignores the delivery filter, so check the pickup line of the
  offer you recommend (`product <its id>`).
- The pickup line reads "Отримати сьогодні з 13:00" or a date for goods in a Rozetka warehouse, and
  "Середній термін доставки N днів" for goods shipped by the seller.
- **The site's "Доставка в магазини ROZETKA" facet is not a guarantee.** It also includes some goods
  that Rozetka sells but a supplier ships, and for those the store pickup is unavailable (for example
  Flonal lids: only Nova Poshta, 189 ₴). Free pickup also depends on the order total: a 350 ₴ item
  was charged 49 ₴. Always confirm the finalists with `product` or `compare --delivery` before
  promising free pickup.
- If no Rozetka-sold finalist can be picked up, check the "same model from other sellers" ids with
  `compare <ids> --delivery`. Some third-party sellers ship to Rozetka stores for free (4–7 days).
- If the default scope returns nothing (for example paint: Rozetka mostly doesn't stock it), say so
  before widening with `--seller any` / `--any-delivery`. Widening means paid delivery or a third-party
  seller.
- Give the price, the seller, the pickup term, the URL and why it beats the alternatives.

## Prom.ua

CLI: `bin/prom` (works from any directory). Every command
takes `-h` and `--json`. No account needed.

Prom.ua is a marketplace of thousands of small sellers. **Many of them have a minimum order sum
(usually 50–700 ₴) and won't ship a smaller order.** Every listing row shows the seller, its rating,
its id (`[продавець id …]`) and `мін. замовлення N ₴`. Listings show only goods in stock unless you
pass `--any-stock`.

### Rate limiting — mandatory

Pass `--delay` to every command when you run several in a row (1.5 s before each request; put it last
or give it a value). `PROM_DELAY=1.5` does the same. Never run commands in parallel, and don't loop
`product` over dozens of ids.

### Workflow

1. **Search.** `prom search "<query>"` prints matching categories (`--category ID`) and goods.
   Narrow it down with `prom filters "<query>" --category ID`, then pass
   `-f "<facet>=<value>|<value2>"` (numeric facets take `MIN..MAX`), `--brand X`, `--price 100-500`,
   `--delivery Rozetka|"Нова Пошта"|Самовивіз` and `--sort cheap|expensive|new`.
   - A sorted search keeps only closer matches, so the total drops (the site does the same).
   - "модель у N продавців: A–B ₴" means other sellers offer the same item.
2. **Details.** `prom product <id|url> --offers --reviews 5` prints the price, seller, minimum order,
   delivery and payment options (including "Магазини Rozetka" and Smart free delivery where offered),
   variants, specs and reviews. `--offers` lists every seller of the same model with its price and
   minimum order.
3. **Compare.** `prom compare <id> <id> … --diff`.
4. **One seller's store.** `prom shop <seller>` takes a company id, a company page URL, `cs<id>.prom.ua`
   or a product URL. It prints the seller's card (rating, age, orders, minimum order) and its catalogue.
   - `--find "<query>"` searches inside the store, and `--group <title>` picks a store section (see
     `prom filters --shop <seller>`). All listing filters work there too.
5. **Reaching the minimum order.** When the chosen item costs less than the seller's minimum, run
   `prom shop --cart <id>[xQTY] [--cart <id2>] --fill`. It prints the cart total and the missing sum,
   then lists the seller's in-stock goods priced at least that much, cheapest first. Combine it with
   `--find` to look for something the user actually needs, such as a second lid or a spare part.
   `--price -<sum>` finds cheaper items to combine.

### Picking

- The minimum order sum is part of the real price. Compare "item + cheapest useful top-up" from one
  seller against a pricier seller with no minimum (`product --offers` shows both), and say which you chose.
- Prefer sellers with a high positive share over many reviews, recent activity and "Готово до
  відправки". Read the delivery comments: some sellers ship cash-on-delivery only after a prepayment.
- Rows marked `реклама` are paid placements. They are relevant, but don't treat them as a ranking.
- Give the price, the seller, its minimum order (and what to add to reach it), the delivery options
  and the URL.

## OLX

CLI: `bin/olx` (works from any directory). Every command
takes `-h` and `--json`. No account needed. The tool only reads ads: no phone numbers, no messages,
no login.

OLX is classified ads: mostly used goods, many private sellers, no cart. **Search all of Ukraine by
default.** Pass `--city` (and `--radius KM`) or `--region` only when the user asks for a place
("в Дніпрі", "поруч"). Rows then show the distance in km. `olx geo "<назва>"` gives the id when a
village name is common (`--city "Обухівка, Дніпровський"` also works).

### Rate limiting — mandatory

Pass `--delay` to every command when you run several in a row (1.5 s before each request; put it last
or give it a value). `OLX_DELAY=1.5` does the same. Never run commands in parallel, and don't loop
`product` over dozens of ads: narrow with filters, then open 2–6 finalists. If a command fails with
"HTTP 403 from OLX's WAF", stop and tell the user. Don't retry in a loop.

### Workflow

1. **Category.** `olx search "<запит>"` prints `Категорії (--category ID)` for the query, then ads.
   OLX categories are coarse ("Електроінструмент" holds drills and screwdrivers alike), so keep the
   words in the query and add the category. `olx categories --find "<слова>"` searches the tree offline.
2. **Filters.** `olx filters "<запит>" --category ID` lists the category's filters with counts (brand,
   model, size…), plus state, seller type and regions.
3. **Listing.** `olx search "<запит>" --category ID [-f "Назва=Знач|Знач2"] [-f "Назва=MIN..MAX"]
   [--state used|new] [--price MIN-MAX] [--delivery] [--private|--business] [--sort cheap|new|expensive]
   [--no-ads] [--page N]`.
   - A search returns at most 1000 ads. The header gives the real total, so narrow it with filters
     instead of paging far.
   - `--sort cheap` already drops "обмін", "безкоштовно" and 0 ₴ ads.
   - **Looking for a specific model** ("Epson V37", "LiDE 220"): OLX search is fuzzy, and sellers put the
     same goods into neighbouring categories. Use `olx search "<model>" --category A --category B --all --title`:
     every page of both categories, only ads with all query words in the title. Don't rely on one page.
   - A pasted olx.ua listing URL works with `--url "<url>"` (its filters and sorting are used).
4. **Details.** `olx product <id|url>` prints the price, params, description, OLX Доставка (parcel
   weight), the seller card (on OLX since, last visit, **rating**, number of active ads) and flags.
   `olx seller --ad <id>` lists the seller's other ads (`--find` searches them).
5. **Compare.** `olx compare <id1> <id2> … --diff`.

### Picking

- Read the `⚠` flags and explain them to the user:
  - `без OLX Доставки`: no buyer protection. The deal means meeting in person or prepaying a stranger.
  - `новий акаунт`: the seller registered less than 30 days ago.
  - `продавець не заходив N дн.`: the seller hasn't visited for a while, so the ad may be stale.
  - `підозріло дешево`: far below the page median (under 40 % for new goods or next to a new account /
    no OLX Доставка, under 25 % for other used goods), a common scam bait. It compares against the
    page, so on a mixed page (accessories next to devices) check whether it's really the same thing.
  - `рейтинг poor/fair`: the OLX seller score.
- Prefer ads with OLX Доставка, sellers with a long history and a good rating, and recent activity.
  Recommend paying through OLX Доставка, never prepaying by card or through a link from chat.
- A very cheap "new" branded tool from a business seller (a Makita kit for 900 ₴) is almost always a
  replica. Say so.
- "торг" means the price is negotiable. A price in USD shows its UAH equivalent in listings only
  (`product` and `compare` show just the USD figure).
- `--sort new` sorts by the last bump, so an old ad can come first: rows show `додано …, піднято …`.
- `реклама` rows are paid TOP placements, not a ranking. Use `--no-ads` to hide them.
- Give the price, the state, the place (and the distance when a city was given), the seller (private or
  business, on OLX since, rating), the delivery option, the flags, the URL, and why it beats the others.
