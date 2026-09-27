"""Normalise AliExpress payloads (search cards, pdp modules, reviews, store items, orders) to plain dicts."""

import datetime
import json
import re
import statistics

from client import SITE

MONEY_RE = re.compile(r"(\d[\d.,\s]*)")
CURRENCY_HINTS = [("грн", "UAH"), ("US $", "USD"), ("$", "USD"), ("€", "EUR"), ("zł", "PLN")]


def parse_money(text):
    """'грн.1,023.43' / '117,07 грн.' / 'US $2.05' / '$1.09|1|09' → (1023.43, 'UAH')."""
    if text is None:
        return None, None
    if isinstance(text, (int, float)):
        return float(text), None
    text = str(text)
    if "|" in text:  # "$1.09|1|09": integer and cents after the pipes
        head, whole, cents = (text.split("|") + ["", ""])[:3]
        cur = next((c for hint, c in CURRENCY_HINTS if hint in head), None)
        try:
            return int(whole.replace(",", "")) + int(cents or 0) / 100, cur
        except ValueError:
            text = head
    cur = next((c for hint, c in CURRENCY_HINTS if hint in text), None)
    m = MONEY_RE.search(text.replace("\xa0", " "))
    if not m:
        return None, cur
    num = m.group(1).replace(" ", "").rstrip(".,")
    if "," in num and "." in num:
        num = num.replace(",", "") if num.rfind(".") > num.rfind(",") else num.replace(".", "").replace(",", ".")
    elif "," in num:
        whole, _, frac = num.rpartition(",")
        num = f"{whole.replace(',', '')}.{frac}" if len(frac) == 2 else num.replace(",", "")
    try:
        return float(num), cur
    except ValueError:
        return None, cur


def parse_sold(text):
    """'1,000+ sold' → 1000, '4 sold' → 4, '10K+ sold' → 10000."""
    if not text:
        return 0
    m = re.search(r"([\d.,]+)\s*([kK万]?)\+?\s*(sold|orders|продано)?", str(text))
    if not m:
        return 0
    n = float(m.group(1).replace(",", ""))
    return int(n * (10000 if m.group(2) == "万" else 1000 if m.group(2) else 1))


def item_url(pid):
    return f"{SITE}/item/{pid}.html"


# --- search cards -------------------------------------------------------------

BULK_RE = re.compile(r"(.+?)\s*each,\s*≥\s*(\d+)")
SAVE_RE = re.compile(r"save\s+(.+)$", re.I)
PACK_RE = re.compile(r"(?<![\w/])(\d{1,3}(?:\s*/\s*\d{1,3})*)\s*(?:-\s*\d{1,3}\s*)?(pcs|pieces|pc|piece|шт|pack|packs|set|sets)\b", re.I)


def card_tags(x):
    out = []
    for sp in x.get("sellingPoints") or []:
        text = (sp.get("tagContent") or {}).get("tagText")
        out.append((sp.get("source") or "", text or ""))
    return out


def search_card(x):
    p = x.get("prices") or {}
    sale, orig = p.get("salePrice") or {}, p.get("originalPrice") or {}
    tags = card_tags(x)
    card = {
        "id": str(x.get("productId")),
        "title": (x.get("title") or {}).get("displayTitle", ""),
        "price": sale.get("minPrice"),
        "orig": orig.get("minPrice"),
        "currency": sale.get("currencyCode"),
        "rating": (x.get("evaluation") or {}).get("starRating"),
        "sold_text": (x.get("trade") or {}).get("tradeDesc") or "",
        "ad": x.get("productType") not in (None, "natural"),
        "tags": tags,
        "url": item_url(x.get("productId")),
        "bulk": None, "new_user_saving": None,
    }
    card["sold"] = parse_sold(card["sold_text"])
    for src, text in tags:
        m = BULK_RE.search(text)
        if "bulk" in src and m:
            card["bulk"] = (parse_money(m.group(1))[0], int(m.group(2)))
        if "welcomedeal" in src or "new_user" in src:
            m = SAVE_RE.search(text)
            card["new_user_saving"] = parse_money(m.group(1))[0] if m else 0
    card["choice"] = any(src.startswith("choice") for src, _ in tags)
    return card


def search_page(fields):
    info = fields.get("pageInfo") or {}
    items = [search_card(x) for x in (fields.get("mods") or {}).get("itemList", {}).get("content") or []
             if x.get("productId")]
    total = info.get("totalResults") or 0
    padded = info.get("page", 1) == 1 and 0 < total < len(items)
    if padded:  # a thin result gets padded with loosely related items
        items = items[:total]
    return {"total": total, "page": info.get("page"), "page_size": info.get("pageSize"),
            "items": items, "padded": padded}


def facets(fields):
    """Refine filters → [{title, param, multi, sep, values: [{text, value, selected}]}]."""
    out = []
    for g in (fields.get("mods") or {}).get("searchRefineFilters", {}).get("content") or []:
        vals = g.get("content")
        if not isinstance(vals, list):
            continue
        values = []
        for v in vals:
            if g.get("type") == "priceRange":
                if v.get("startPrice") is not None:
                    values.append({"text": v.get("priceRanges"), "value": f"{v['startPrice']}-{v.get('endPrice', '')}",
                                   "selected": v.get("selected"), "note": v.get("boughtRate")})
                continue
            if v.get("selectedValue") is None:
                continue
            values.append({"text": (v.get("text") or "").strip(), "value": v["selectedValue"],
                           "selected": bool(v.get("selected"))})
        if values:
            out.append({"title": (g.get("title") or "").strip(), "type": g.get("type"),
                        "param": g.get("paramName"), "multi": bool(g.get("isMulti")),
                        "sep": g.get("multiValSep") or ",", "values": values})
    return out


# --- product (pdp) ------------------------------------------------------------------

def _num(s):
    return parse_money(s)[0]


def product(r):
    """pdp `result` modules → one flat dict."""
    title = (r.get("PRODUCT_TITLE") or {}).get("text") or (r.get("GLOBAL_DATA") or {}).get("globalData", {}).get("subject")
    g = (r.get("GLOBAL_DATA") or {}).get("globalData") or {}
    rating = r.get("PC_RATING") or {}
    price_mod = r.get("PRICE") or {}
    sku_mod = r.get("SKU") or {}
    ladder = (r.get("LADDER_PRICE") or {}).get("ladderPriceViewMap") or {}
    qty = (r.get("QUANTITY_PC") or {}).get("allSkuQuantityView") or {}
    atm = (r.get("PRICE_BANNER") or {}).get("atmosphereCodeMap") or {}
    price_map = price_mod.get("skuIdStrPriceInfoMap") or {}

    names, hot = {}, set()
    for prop in sku_mod.get("skuProperties") or []:
        for v in prop.get("skuPropertyValues") or []:
            key = (str(prop.get("skuPropertyId")), str(v.get("propertyValueIdLong") or v.get("propertyValueId")))
            names[key] = (prop.get("skuPropertyName"),
                          v.get("propertyValueDefinitionName") or v.get("propertyValueDisplayName")
                          or v.get("propertyValueName"))
            if v.get("showHot"):
                hot.add(key)
    skus = []
    for path in sku_mod.get("skuPaths") or []:
        sid = str(path.get("skuIdStr") or path.get("skuId"))
        attrs, is_hot = [], False
        # skuAttr carries the seller's own value names: "14:173#60W Brown;200001036:200007624"
        custom = dict(re.findall(r"(\d+:\d+)#([^;]+)", path.get("skuAttr") or ""))
        for pair in (path.get("path") or "").split(";"):
            pid, _, vid = pair.partition(":")
            if (pid, vid) in names:
                name, value = names[(pid, vid)]
                attrs.append((name, custom.get(pair, value)))
                is_hot |= (pid, vid) in hot
        pi = price_map.get(sid) or {}
        q = qty.get(sid) or {}
        skus.append({
            "id": sid,
            "attrs": attrs,
            "price": _num(pi.get("salePriceLocal") or pi.get("salePriceString")),
            "orig": (pi.get("originalPrice") or {}).get("value"),
            "stock": path.get("skuStock"),
            "salable": bool(path.get("salable", True)) and (path.get("skuStock") or 0) > 0,
            "max_buy": q.get("maxBuyCount"),
            "limit_hint": q.get("hintText") or q.get("maxBuyCountStr"),
            "ladder": [(b.get("bulkQuantity"), _num(b.get("bulkPrice") or b.get("formattedBulkPrice")))
                       for b in ladder.get(sid) or []],
            "promo": atm.get(sid),
            "hot": is_hot,
        })

    shop = r.get("SHOP_CARD_PC") or {}
    seller = shop.get("sellerInfo") or {}
    store_url = seller.get("storeURL") or ""
    benefits = {(b.get("title") or "").strip(): b.get("value") for b in shop.get("benefitInfoList") or []}
    store = {
        "name": shop.get("storeName"),
        "shop_id": seller.get("storeNum"),
        "seller_id": seller.get("adminSeq"),
        "url_num": (re.search(r"/store/(\d+)", store_url) or [None, None])[1],
        "url": ("https:" + store_url) if store_url.startswith("//") else store_url,
        "country": seller.get("countryCompleteName"),
        "opened": (seller.get("openTime") or "")[:10],
        "opened_text": seller.get("formatOpenTime"),
        "years": seller.get("openedYear"),
        "positive_rate": shop.get("sellerPositiveRate"),
        "feedbacks": shop.get("sellerTotalNum"),
        "sold_180d": benefits.get("# sold in 180 days"),
        "top_rated": seller.get("topRatedSeller"),
    }

    ship = []
    for block in (r.get("SHIPPING") or {}).get("deliveryLayoutInfo") or []:
        b = block.get("bizData") or {}
        if not b:
            continue
        ship.append({
            "to": b.get("shipToCode"), "from": b.get("shipFrom"),
            "days": (b.get("deliveryDayMin"), b.get("deliveryDayMax")),
            "eta": (b.get("displayEtaMinDate"), b.get("displayEtaMaxDate")),
            "fee": 0.0 if b.get("shippingFee") == "free" else b.get("displayAmount"),
            "option": b.get("deliveryOptionCode") or b.get("company"),
            "free_over": b.get("logisticsComposeThreshold") if b.get("thresholdOverZero") == "yes" else None,
        })

    desc = r.get("DESC") or {}
    props = (r.get("PRODUCT_PROP_PC") or {}).get("showedProps") or []
    extend = [v.get("content") for v in (r.get("PRICE_EXTEND") or {}).get("priceExtendViews") or []]
    images = (r.get("HEADER_IMAGE_PC") or {}).get("imagePathList") or []
    return {
        "id": str(g.get("productId") or price_mod.get("productId") or ""),
        "title": title,
        "rating": rating.get("rating"),
        "reviews": rating.get("totalValidNum"),
        "sold_text": rating.get("otherText") or "",
        "sold": parse_sold(rating.get("otherText")),
        "skus": skus,
        "is_lot": bool(price_mod.get("isLot")),
        "store": store,
        "shipping": ship,
        "props": [(p.get("attrName"), p.get("attrValue")) for p in props],
        "desc_url": desc.get("pcDescUrl") or desc.get("msiteDescUrl"),
        "manual": (desc.get("instructionInfo") or {}).get("mediaUrl"),
        "price_notes": [e for e in extend if e],
        "category_path": g.get("categoryPath"),
        "category": g.get("category3") or g.get("category2"),
        "images": images,
        "url": item_url(g.get("productId") or price_mod.get("productId")),
    }


def sku_label(sku):
    return "; ".join(f"{n}: {v}" for n, v in sku["attrs"]) or "(єдиний варіант)"


def price_stats(skus):
    live = [s["price"] for s in skus if s["salable"] and s["price"]]
    if not live:
        live = [s["price"] for s in skus if s["price"]]
    if not live:
        return None
    return {"min": min(live), "median": statistics.median(live), "max": max(live), "n": len(live)}


# --- reviews ---------------------------------------------------------------------

def reviews(data):
    stat = data.get("productEvaluationStatistic") or {}
    items = []
    for e in data.get("evaViewList") or []:
        items.append({
            "stars": round((e.get("buyerEval") or 0) / 20),
            "date": e.get("evalDate"),
            "country": e.get("buyerCountry"),
            "sku": e.get("skuInfo"),
            "text": e.get("buyerTranslationFeedback") or e.get("buyerFeedback") or "",
            "original": e.get("buyerFeedback") or "",
            "images": len(e.get("images") or []),
            "additional": e.get("buyerAddFbDays") is not None and e.get("buyerProductFeedBack"),
        })
    filters = {f["filterCode"]: f["filterCount"] for f in (data.get("filterInfo") or {}).get("filterStatistic") or []}
    return {
        "total": data.get("totalNum") or stat.get("totalNum"),
        "pages": data.get("totalPage"),
        "avg": stat.get("evarageStar"),
        "stars": {i: stat.get(f"{w}StarNum") for i, w in
                  [(5, "five"), (4, "four"), (3, "three"), (2, "two"), (1, "one")]},
        "negative_rate": stat.get("negativeRate"),
        "tags": [(t.get("content"), t.get("num")) for t in data.get("impressionDTOList") or []],
        "filters": filters,
        "items": items,
    }


# --- store items ----------------------------------------------------------------------

def store_item(x):
    prices = x.get("prices") or {}
    regular = (prices.get("piecePrice") or {}).get("amount")
    promo = (prices.get("promotionPiecePrice") or {}).get("amount")
    codes = [a.get("atmosphereCode") for pos in x.get("atmosphere") or [] for a in pos.get("atmosphereDTOList") or []]
    new_user = any("welcomedeal" in (c or "") or "new_user" in (c or "") for c in codes)
    return {
        "id": str(x.get("id")),
        "title": x.get("subject") or x.get("seoTitle") or "",
        "price": promo or regular,
        "regular": regular,
        "orig": regular,
        "new_user": new_user,
        "orders": x.get("orders") or 0,
        "sold": x.get("orders") or 0,
        "sold_text": f"{x.get('orders') or 0} orders",
        "rating": x.get("averageStar"),
        "reviews": x.get("feedbacks") or 0,
        "lot": x.get("piecesPerLot") if x.get("lot") else None,
        "url": item_url(x.get("id")),
        "codes": codes,
    }


# --- orders --------------------------------------------------------------------------

FINAL_STATUSES = {"completed", "closed", "cancelled", "canceled", "expired", "finished", "deleted"}


def order_date(text):
    for fmt in ("%b %d, %Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text.strip(), fmt).date().isoformat()
        except (ValueError, AttributeError):
            continue
    return text


def orders_page(data):
    """One order-list response → (orders, has_more)."""
    comps = data.get("data") or {}
    out = []
    for key, comp in comps.items():
        if not key.startswith("pc_om_list_order_"):
            continue
        f = comp.get("fields") or {}
        total, cur = parse_money(f.get("totalPriceText"))
        lines = []
        for ln in f.get("orderLines") or []:
            price, lcur = parse_money(ln.get("itemPriceText"))
            lines.append({
                "line_id": str(ln.get("orderLineId") or ""),
                "product_id": str(ln.get("productId") or ""),
                "sku_id": str(ln.get("skuId") or ""),
                "title": ln.get("itemTitle") or "",
                "attrs": [(a.get("name"), a.get("text")) for a in ln.get("skuAttrs") or []],
                "price": price, "currency": lcur or cur,
                "qty": ln.get("quantity") or 1,
                "image": ln.get("itemImgUrl"),
            })
        out.append({
            "order_id": str(f.get("orderId")),
            "date": order_date(f.get("orderDateText") or ""),
            "status": f.get("statusText") or "",
            "store": f.get("storeName") or "",
            "store_url": f.get("storePageUrl") or "",
            "total": total, "currency": cur,
            "lines": lines,
        })
    body = next((v.get("fields") or {} for k, v in comps.items() if k.startswith("pc_om_list_body")), {})
    return out, bool(body.get("hasMore"))


def order_detail(data):
    """Order detail → status, price breakdown, delivery estimate, tracking link (no address/payment data)."""
    out = {"status": None, "note": None, "prices": [], "total": None, "eta": None, "tracking": None}
    for key, comp in (data.get("data") or {}).items():
        f = comp.get("fields") or {}
        if key.startswith("detail_order_status_block"):
            out["status"], out["note"] = f.get("title"), f.get("reminder")
            out["tracking"] = next((b.get("href") for b in f.get("buttonVOList") or [] if b.get("type") == "TRACKING"), None)
        elif key.startswith("detail_order_price_block"):
            out["prices"] = [(d.get("title"), d.get("value")) for d in f.get("priceDetails") or []]
            out["total"] = (f.get("totalPrice") or {}).get("value")
        text = json.dumps(f, ensure_ascii=False)
        m = re.search(r'"expectDeliveryDateTitle":\s*"([^"]+)"', text)
        if m:
            out["eta"] = m.group(1)
    return out
