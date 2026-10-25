"""Price reading and risk flags for OLX ads.

OLX is mostly used goods from private sellers, so the flags point at what makes a deal risky:
- no OLX Доставка:   no buyer protection, the seller can only meet or ship against prepayment
- new account:       the seller registered less than 30 days ago
- inactive seller:   not seen on OLX for over 30 days, the ad may be stale
- suspiciously cheap: under 40 % of the median price on the same page (a classic scam bait) for new goods
                     or next to a scam sign (new account, no OLX Доставка); under 25 % for other used
                     goods, which are often cheap just because they're old
- weak rating:       OLX seller score "poor", or "fair" from several ratings
"""

import datetime
import statistics

NEW_ACCOUNT_DAYS = 30
INACTIVE_DAYS = 30
CHEAP_SHARE = 0.4
CHEAP_SHARE_USED = 0.25


def days_since(iso, now=None):
    if not iso:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (now - datetime.datetime.fromisoformat(iso)).days


def price_of(ad):
    """The ad's price param value: {value, currency, type: price|arranged|exchange|free, converted_value, …}."""
    p = next((x for x in ad.get("params") or [] if x.get("key") == "price"), None)
    return (p or {}).get("value") or None


def state_key(ad):
    p = next((x for x in ad.get("params") or [] if x.get("key") == "state"), None)
    return ((p or {}).get("value") or {}).get("key")


def uah(ad):
    """Price in UAH for comparisons, or None for exchange/free/no price."""
    p = price_of(ad)
    if not p or p.get("type") in ("exchange", "free") or not p.get("value"):
        return None
    if p.get("currency") not in (None, "UAH"):
        return p.get("converted_value")
    return p["value"]


def money(x):
    return f"{x:,.0f}".replace(",", " ")


def price_text(ad):
    p = price_of(ad)
    if not p:
        return "ціна не вказана"
    if p.get("type") == "exchange":
        return "обмін"
    if p.get("type") == "free":
        return "безкоштовно"
    cur = p.get("currency") or "UAH"
    text = f"{money(p['value'])} ₴" if cur == "UAH" else f"{money(p['value'])} {cur}"
    if cur != "UAH" and p.get("converted_value"):  # listings convert to UAH, a single ad doesn't
        text += f" (≈{money(p['converted_value'])} ₴)"
    if p.get("previous_value") and p["previous_value"] > p["value"]:
        text += f" (було {money(p['previous_value'])})"
    if p.get("arranged") or p.get("negotiable"):
        text += " торг"
    return text


def page_median(ads):
    prices = [v for v in (uah(a) for a in ads) if v]
    return statistics.median(prices) if len(prices) >= 8 else None


def ad_flags(ad, median=None, score=None, now=None):
    flags, scam_signs = [], 0
    if ad.get("status") and ad["status"] != "active":
        flags.append(f"оголошення неактивне ({ad['status']})")
    if not (ad.get("contact") or {}).get("courier"):
        flags.append("без OLX Доставки")
        scam_signs += 1
    user = ad.get("user") or {}
    age = days_since(user.get("created"), now)
    if age is not None and age < NEW_ACCOUNT_DAYS:
        flags.append(f"новий акаунт ({age} дн.)")
        scam_signs += 1
    seen = days_since(user.get("last_seen"), now)
    if seen is not None and seen > INACTIVE_DAYS:
        flags.append(f"продавець не заходив {seen} дн.")
    price = uah(ad)
    share = CHEAP_SHARE if scam_signs or state_key(ad) != "used" else CHEAP_SHARE_USED
    if median and price and price < median * share:
        flags.append(f"підозріло дешево (медіана сторінки {money(median)} ₴)")
    if score:
        label, n = score.get("label"), score.get("ratings") or 0
        if label == "poor" or (label == "fair" and n >= 3):
            flags.append(f"рейтинг {label} ({score.get('score')}/10, {n} оцінок)")
    return flags


def score_text(score):
    if not score or score.get("label") in (None, "none"):
        return "рейтинг: немає оцінок"
    return f"рейтинг {score['label']} {score.get('score')}/10 ({score.get('ratings')} оцінок)"
