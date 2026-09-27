"""Bait detection ("розводняк") and a transparent ranking score.

Every rule yields a short flag string, so the agent sees *why* a listing looks off:
- bulk:        "X each, ≥ 3 pieces" — the headline price needs several units
- new-user:    welcome-deal / new-shopper price or "Max. 1 pcs/shopper" — not what a regular buyer pays
- cheap-sku:   the headline price belongs to a decoy variant (accessory, "no battery", 1 piece…)
- multi-pack:  the title or the variants are packs ("3/5PCS", "10pcs"), compare per piece
- sold≫reviews / young store: popularity that the reviews don't back up
"""

import datetime
import math
import re

from parse import PACK_RE, price_stats

DECOY_RE = re.compile(
    r"\b(only|just|no\s+battery|without|w/o|not\s+include|excluding|accessor(y|ies)|spare|replacement|"
    r"case|cover|shell|bag|pouch|holder|stand|bracket|strap|sticker|film|protector|cable\s+only|"
    r"adapter\s+only|charger\s+only|tool\s+only|body\s+only|bare\s+tool|bare|parts?|kit\s+only|"
    r"1\s*pcs?|1\s*piece|sample|gift|link|remote|manual|box\s+only|empty)\b"
    r"|без\s|тільки|лише|чохол|кабель", re.I)


RANGE_RE = re.compile(r"(\d{1,3})\s*(?:pcs|pc)?\s*-\s*(\d{1,3})\s*(?:pcs|pieces|pc)\b", re.I)


def pack_size(text):
    """Pieces per listing from a title or variant name: '10pcs' → 10, '3/5PCS' → (3, 5), '1pcs-5pcs' → (1, 5)."""
    m = RANGE_RE.search(text or "")
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = PACK_RE.search(text or "")
    if not m:
        return None
    nums = [int(n) for n in re.findall(r"\d+", m.group(1))]
    return nums[0] if len(nums) == 1 else tuple(nums)


def card_flags(c):
    flags = []
    if c.get("bulk"):
        price, qty = c["bulk"]
        flags.append(f"bulk: {price:g}/шт лише від {qty} шт")
    if c.get("new_user_saving") is not None:
        flags.append("new-user price" + (f" (−{c['new_user_saving']:g})" if c["new_user_saving"] else ""))
    if c.get("new_user"):
        flags.append("new-user price")
    pack = pack_size(c.get("title"))
    if isinstance(pack, tuple):
        flags.append(f"multi-pack variants {'/'.join(map(str, pack))} — ціна за мінімальний")
    elif pack and pack > 1:
        flags.append(f"pack of {pack}")
    if c.get("lot"):
        flags.append(f"lot of {c['lot']}")
    if c.get("ad"):
        flags.append("ad")
    reviews = c.get("reviews")
    if reviews is not None and c.get("sold", 0) >= 500 and reviews < c["sold"] * 0.01:
        flags.append(f"sold≫reviews ({c['sold']} vs {reviews})")
    return flags


def product_flags(p):
    flags = []
    stats = price_stats(p["skus"])
    live = [s for s in p["skus"] if s["salable"] and s["price"]]
    if stats and stats["n"] >= 3:
        cheap = [s for s in live if s["price"] < 0.4 * stats["median"]]
        if cheap:
            flags.append(f"cheap-sku: {len(cheap)} варіант(и) дешевші за 0.4× медіани "
                         f"({stats['min']:g} vs медіана {stats['median']:g})")
    decoys = [s for s in live if any(DECOY_RE.search(str(v)) for _, v in s["attrs"])]
    if decoys and stats and any(s["price"] <= stats["median"] for s in decoys):
        flags.append("decoy variants: " + "; ".join(sorted({v for s in decoys[:4] for _, v in s["attrs"]
                                                             if DECOY_RE.search(str(v))}))[:160])
    if any("new_user" in (s["promo"] or "") or "welcome" in (s["promo"] or "") for s in p["skus"]):
        flags.append("new-user price (перевір з --as-me)")
    limited = [s for s in live if s["max_buy"] == 1]
    if limited:
        flags.append(f"max 1 шт/покупця на {len(limited)} варіант(ах) — промо-ціна")
    if any(s["ladder"] for s in p["skus"]):
        best = min((q, pr) for s in p["skus"] for q, pr in s["ladder"] if pr)
        flags.append(f"bulk: ціна від {best[0]} шт — {best[1]:g}/шт")
    packs = {pack_size(" ".join(str(v) for _, v in s["attrs"])) for s in live} - {None}
    if len(packs) > 1:
        flags.append("multi-pack variants: " + ", ".join(str(x) for x in sorted(packs, key=str)))
    if p.get("is_lot"):
        flags.append("sold by lot")
    for note in p.get("price_notes") or []:
        if "coin" in note.lower():
            flags.append(f"price note: {note}")
    if p.get("reviews") is not None and p.get("sold", 0) >= 500 and p["reviews"] < p["sold"] * 0.01:
        flags.append(f"sold≫reviews ({p['sold_text']} vs {p['reviews']} відгуків)")
    opened = (p.get("store") or {}).get("opened")
    if opened:
        try:
            age = (datetime.date.today() - datetime.date.fromisoformat(opened)).days
            if age < 365:
                flags.append(f"young store ({age} днів)")
        except ValueError:
            pass
    return flags


def adjusted_rating(rating, n, prior=4.5, weight=30):
    """Bayesian average: 4.9 on 12 reviews ranks below 4.8 on 3000."""
    if not rating:
        return None
    n = n or 0
    return (prior * weight + float(rating) * n) / (weight + n)


def score(item, flags):
    """Higher is better. Rating (review-count adjusted) + popularity − penalties for bait flags."""
    n = item.get("reviews")
    if n is None:  # search cards have no review count; use a tenth of sales as a stand-in
        n = (item.get("sold") or 0) / 10
    r = adjusted_rating(item.get("rating"), n) or 4.0
    s = (r - 4.0) * 10 + math.log10((item.get("sold") or 0) + 1) * 2
    heavy = ("cheap-sku", "decoy", "bulk", "sold≫reviews", "young store", "multi-pack")
    s -= sum(3 if f.startswith(heavy) else 1 for f in flags if not f.startswith(("ad", "pack of")))
    return round(s, 1)


def popular_variants(review_items):
    """Which SKUs reviewers actually bought, most common first."""
    counts = {}
    for r in review_items:
        if r.get("sku"):
            counts[r["sku"]] = counts.get(r["sku"], 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])
