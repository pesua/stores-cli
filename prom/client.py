"""Thin client for prom.ua's GraphQL API (https://prom.ua/graphql).

The endpoint accepts ad-hoc queries without login (introspection is off; the field names below
come from the site's own compiled queries). `X-Language: uk` makes names and texts Ukrainian.
Listing filters are passed as `params`, the same keys the site puts in its URLs:
`category`, `a<attr id>` (attribute values), `price_local__gte/__lte`, `delivery`,
`binary_filters` (presence_available, evo_pay, delivery_subscription, …), `opinions`.
"""

import re
import threading
import time
import urllib.parse

import requests

SITE = "https://prom.ua"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
PAGE_SIZE = 29  # the site's page size; the API takes up to ~90

PRODUCT_ITEM = """
  product {
    id name urlText price discountedPrice priceCurrency minOrderSum measureUnit
    presence { presence isAvailable isEnding isWait }
    catalogPresence { title }
    productOpinionCounters { rating count }
    wholesalePrices { minimumOrderQuantity price }
    company { id name slug regionName opinionStats { opinionPositivePercent opinionTotal } }
  }
  productModel { model_id min_price max_price company_count model_product_ids }
  advert { source }
"""

FILTERS = """
  filters {
    total
    priceChartFilter { values }
    binaryFilters { name values { selected value count title } }
    attributeFilters { name title type min max measureUnit values { selected value count title } }
    categoryFilter { name title values { selected value count title } }
    productGroupFilter { name title values { selected value count title } }
    deliveryFilter { name values { selected value count title } }
    opinionsFilter { name title values { selected value count title } }
  }
"""

SEARCH_Q = """
query($search_term: String!, $params: Any, $company_id: Int, $sort: String, $limit: Int, $offset: Int) {
  listing: searchListing(search_term: $search_term, params: $params, company_id: $company_id,
                         sort: $sort, limit: $limit, offset: $offset) {
    page { total correctedSearchTerm products { %s } }
    %s
  }
}""" % (PRODUCT_ITEM, FILTERS)

COMPANY_LISTING_Q = """
query($company_id: Int!, $params: Any, $sort: String, $limit: Int, $offset: Int) {
  listing: companyListing(company_id: $company_id, params: $params, sort: $sort, limit: $limit, offset: $offset) {
    page { total products { %s } }
    %s
  }
}""" % (PRODUCT_ITEM, FILTERS)

COMPANY_Q = """
query($id: Int!) {
  company(id: $id) {
    id name slug regionName ageYears webSiteUrl lastActivityTime deliveredOrdersCategoryText phone
    isOperating isStatusBad inTopSegment description warrantyInfo companyOpinionsUrl
    opinionStats { opinionPositivePercent opinionTotal }
    scheduleDays { name dayType workTimeRangeStart workTimeRangeEnd }
  }
}"""

PRODUCT_Q = """
query($id: Long!) {
  product(id: $id) {
    id name urlText price discountedPrice priceCurrency minOrderSum measureUnit sku newModelId
    presence { presence isAvailable isEnding isWait }
    catalogPresence { title }
    descriptionPlain
    attributes { name group values { value } }
    wholesalePrices { minimumOrderQuantity price }
    productOpinionCounters { rating count }
    company { id name slug regionName ageYears lastActivityTime deliveredOrdersCategoryText phone isOperating
              companyOpinionsUrl opinionStats { opinionPositivePercent opinionTotal } }
    buyBox { companyCount minPrice maxPrice modelId }
    deliveryOptions { name raw_type comment is_free_prom_shipping }
    paymentOptions { name raw_type }
  }
  productOpinionOnProductCardListing(productId: $id) {
    opinions { dateCreated rating text pros cons authorName fromBuyerText }
  }
  variationItems(id: $id) {
    productItem { product { id discountedPrice presence { isAvailable } } }
    attributes { name values { value } }
  }
}"""

ITEMS_Q = """
query($ids: [Long!]!) {
  productItemsList(ids: $ids, adv_source: "prom:variations") { %s }
}""" % PRODUCT_ITEM


class ApiError(RuntimeError):
    pass


class Prom:
    def __init__(self, timeout=30, delay=0.0):
        """delay: minimum seconds between HTTP requests, counted from construction too, so
        back-to-back CLI runs are spaced out as well as requests within one run."""
        self.timeout = timeout
        self.delay = delay
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA, "X-Language": "uk", "Origin": SITE, "Referer": SITE + "/ua/",
            "Accept": "application/json",
        })

    def _throttle(self):
        if not self.delay:
            return
        with self._lock:
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def gql(self, query, variables):
        self._throttle()
        r = self.s.post(SITE + "/graphql", json={"query": query, "variables": variables}, timeout=self.timeout)
        try:
            data = r.json()
        except ValueError:
            raise ApiError(f"HTTP {r.status_code}, non-JSON body: {r.text[:200]}")
        if data.get("errors"):
            raise ApiError("; ".join(e.get("message", str(e)) for e in data["errors"]))
        if r.status_code >= 400:
            raise ApiError(f"HTTP {r.status_code}")
        return data["data"]

    def search(self, text, params=None, company_id=None, sort=None, limit=PAGE_SIZE, offset=0):
        return self.gql(SEARCH_Q, {"search_term": text, "params": params or {}, "company_id": company_id,
                                   "sort": sort, "limit": limit, "offset": offset})["listing"]

    def company_listing(self, company_id, params=None, sort=None, limit=PAGE_SIZE, offset=0):
        return self.gql(COMPANY_LISTING_Q, {"company_id": company_id, "params": params or {}, "sort": sort,
                                            "limit": limit, "offset": offset})["listing"]

    def company(self, company_id):
        return self.gql(COMPANY_Q, {"id": company_id})["company"]

    def product(self, product_id):
        return self.gql(PRODUCT_Q, {"id": product_id})

    def items(self, ids, chunk=60):
        """Listing-style records for many product ids (any sellers), batched."""
        out = []
        ids = [int(i) for i in ids]
        for i in range(0, len(ids), chunk):
            out += self.gql(ITEMS_Q, {"ids": ids[i:i + chunk]})["productItemsList"] or []
        return out


# --- references -----------------------------------------------------------

def product_url(p):
    return f"{SITE}/ua/p{p['id']}-{p.get('urlText') or 'p'}.html"


def company_url(c):
    return f"{SITE}/ua/c{c['id']}-{c.get('slug') or 'c'}.html"


def product_id(ref):
    """Product id from an id, 'p123' or a product URL (…/p3190588358-kryshka.html)."""
    ref = ref.strip()
    m = re.search(r"/p(\d+)", ref) or re.fullmatch(r"p?(\d+)", ref)
    if not m:
        raise ValueError(f"not a product reference: {ref}")
    return int(m.group(1))


def seller_ref(ref):
    """('company', id) or ('product', id) from a company id, 'c123', a company page URL
    (prom.ua/ua/c4157633-shopmate.html), a seller site cs4157633.prom.ua, or a product URL."""
    ref = ref.strip()
    if re.fullmatch(r"c?\d+", ref):
        return "company", int(ref.lstrip("c"))
    if ref.startswith("http"):
        parts = urllib.parse.urlsplit(ref)
        m = re.match(r"cs(\d+)\.", parts.netloc)
        if m:
            return "company", int(m.group(1))
        m = re.search(r"/c(\d+)-", parts.path)
        if m:
            return "company", int(m.group(1))
        m = re.search(r"/p(\d+)", parts.path)
        if m:
            return "product", int(m.group(1))
    raise ValueError(f"not a seller reference: {ref} (use the company id shown as [продавець id …], "
                     "a prom.ua company/product URL or cs<id>.prom.ua)")
