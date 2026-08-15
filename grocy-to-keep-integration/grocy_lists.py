#!/usr/bin/env python3
"""
grocy_lists.py — two jobs against Grocy + Home Assistant.

  sync     Reconcile "products below min stock" onto a SHARED Home Assistant
           todo list. Item text shows current level vs minimum, e.g.
           "Mælk — 1/3 Liter".

           The list is shared with the household: they add their own items to
           it by hand. The worker therefore owns only the rows it recognises
           as its own (see the ownership section below) and never touches
           anything else on the list.

  analyse  Derive a suggested min_stock_amount per product from actual
           consumption over LOOKBACK_DAYS, sized for SHOPPING_INTERVAL_DAYS
           between trips, and write the deltas to a second HA todo list.
           Ticking a suggestion approves it; the next run applies it to Grocy.
           Suggestions are always whole numbers, rounded up.

GOOGLE KEEP SUPPORT WAS REMOVED. The account is enrolled in Google's Advanced
Protection Program, which permanently disables App Passwords — the only way
gkeepapi can obtain a master token. There is no workaround, so the household
uses the Home Assistant list directly (the Companion app has a to-do list
home-screen widget). The previous gkeepapi implementation is in the original
handover archive if that ever changes.

Run: docker run --env-file .env grocy-lists sync
     docker run --env-file .env grocy-lists sync --dry-run
Schedule sync every ~15 min, analyse weekly.

===========================================================================
VERIFICATION RECORD — checked against the live systems on 2026-08-12.
Everything below was confirmed empirically, not from docs.

Grocy 4.6.0 (PHP 8.5.6), 109 products / 89 stock rows:
  objects/products      min_stock_amount ✓  qu_id_stock ✓  active ✓ (values
                        seen: "1" only)     48 products have min > 0
  stock                 product_id ✓  amount ✓
  objects/quantity_units id ✓  name ✓
  objects/stock_log     query[]=transaction_type=consume  ✓ filters correctly
                        query[]=undone=0                  ✓ filters correctly
                        query[]=row_created_timestamp>TS  ✓ 0 rows violated
                        limit/offset paging               ✓ 7 pages x 50 =
                        322 unique ids, exactly matching the unpaged count.
  SPOILAGE is a *flag* (`spoiled` 0/1) on rows whose transaction_type is
  "consume". It is NOT its own transaction_type. Types actually present:
  purchase, consume, inventory-correction, transfer_from, transfer_to,
  product-opened. The existing spoilage handling was therefore correct.
  row_created_timestamp format: "%Y-%m-%d %H:%M:%S" ✓

  !! Grocy sits behind Cloudflare. A "Python-urllib/3.12" User-Agent gets
     HTTP 403; "python-requests/*", curl and a custom UA all get 200. We
     send an explicit UA below so this cannot regress into a mystery 403.

Home Assistant 2026.6.3:
  todo.get_items REST shape is
    {"changed_states": [...],
     "service_response": {"<entity>": {"items": [{summary,status,uid}]}}}
  so the ha_items() unwrapping is correct. ✓
  todo.update_item accepts item + rename ✓
  !! services.yaml declares status default "needs_action", but that default
     is a UI hint and is NOT applied to API calls — omitting `status`
     returns completed items too (verified with a completed probe item).
     We still pass it explicitly: relying on an unspecified default to
     uphold the "checked items count as present" invariant is fragile.
  todo.indkob_auto and todo.grocy_forslag did not exist; both created as
  local_todo entries. Entity ids match the defaults below. ✓

gkeepapi 0.17.1 (pinned in requirements.txt):
  Keep.authenticate(email, master_token, state=None, sync=True,
                    device_id=None)                        ✓ as called
  Keep.find(query=None, func=None, ..., trashed=False)     ✓ (trashed=False
      is already the default, so the lambda need not re-check it)
  Keep.sync(resync=False) ✓  Keep.dump() -> dict ✓  Keep.restore(state) ✓
  ListItem.add(text, checked=False, sort=None) -> ListItem ✓ (indents under
      self)   .indent(node) ✓  .subitems ✓  .indented ✓  .delete() ✓
  List.add(text, checked=False, sort=None) ✓
  !! BrowserLoginRequiredException is raised by the password->master-token
     path, not by authenticate(). Obtaining GOOGLE_MASTER_TOKEN is a
     one-off out-of-band step and may require an App Password.

FIXED: product names are now stripped at catalogue time. 48 of the 109
products have trailing whitespace ("Spagetti "). The desired-set key used
the raw name while reconcile() derived its key with .strip(), so the two
never matched: every run added a duplicate and removed the old row, in
perpetuity. In Keep that is a delete-and-re-add every 15 minutes, sending
the item to the bottom of her list each time. Simulated over 3 runs:
before the fix add=1/remove=1 every run; after, run 2 onward is a no-op.
===========================================================================
"""

import argparse
import json
import math
import os
import re
import difflib
import statistics
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------- config

GROCY_URL = os.environ["GROCY_URL"].rstrip("/")
GROCY_KEY = os.environ["GROCY_API_KEY"]
HA_URL = os.environ["HA_URL"].rstrip("/")
HA_TOKEN = os.environ["HA_TOKEN"]

# Shared with the household — they add their own rows here. See ownership.
AUTO_LIST = os.environ.get("AUTO_LIST", "todo.indkob")
SUGGEST_LIST = os.environ.get("SUGGEST_LIST", "todo.grocy_forslag")

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", 365))
SHOPPING_INTERVAL_DAYS = int(os.environ.get("SHOPPING_INTERVAL_DAYS", 7))
SAFETY_K = float(os.environ.get("SAFETY_K", 1.0))   # 1.0 ≈ 84% service level
MIN_EVENTS = int(os.environ.get("MIN_EVENTS", 4))   # ignore sparse products
CHANGE_THRESHOLD = float(os.environ.get("CHANGE_THRESHOLD", 0.25))  # 25%
SPOILAGE_FLAG = float(os.environ.get("SPOILAGE_FLAG", 0.15))

# --- shelf-life analysis ---------------------------------------------------
# Shelf life is OBSERVED from consume rows (best_before_date - purchased_date),
# because default_best_before_days is set on only 5 of 109 products. Grocy
# carries sentinel best-before dates (year 9999) for non-perishables, which
# show up as ~355000-day shelf lives — anything above the cap is treated as
# "does not meaningfully expire" rather than as a real number.
SHELF_SANE_MAX_DAYS = int(os.environ.get("SHELF_SANE_MAX_DAYS", 3650))
MIN_SHELF_OBS = int(os.environ.get("MIN_SHELF_OBS", 2))
# Flag when this fraction of units were consumed after their best-before date.
LATE_USE_FLAG = float(os.environ.get("LATE_USE_FLAG", 0.34))

# --- automatic default_best_before_days ------------------------------------
# Grocy pre-fills the best-before date at purchase from this field, so setting
# it is what makes a barcode scan land on a sensible date. It is written
# WITHOUT approval, unlike min_stock_amount — a deliberate exception, on the
# grounds that the physical date on the package is checked at purchase anyway,
# so a wrong default costs a correction rather than a spoiled product.
#
# That exception is only defensible because the bar is in the data instead:
# more observations than a suggestion needs, and they have to agree with each
# other. When they don't, nothing is written and the field is left alone.
SET_DEFAULT_EXPIRY = os.environ.get("SET_DEFAULT_EXPIRY", "1") == "1"
# Higher than MIN_SHELF_OBS: that one gates a number you are about to eyeball
# on a list, this one gates a number that silently pre-fills every future
# purchase of the product.
MIN_SHELF_OBS_WRITE = int(os.environ.get("MIN_SHELF_OBS_WRITE", 4))
# Median absolute deviation over the median. A product observed at 3, 5 and
# 400 days has no meaningful default, and an average of one would be worse
# than leaving the field empty.
SHELF_REL_SPREAD = float(os.environ.get("SHELF_REL_SPREAD", 0.4))
# Leave an existing value alone unless the observations disagree with it by
# more than this. Some defaults were set by hand and are roughly right; only
# the badly wrong ones are worth overwriting.
EXPIRY_CHANGE_THRESHOLD = float(os.environ.get("EXPIRY_CHANGE_THRESHOLD", 0.3))

# --- barcode prices ---------------------------------------------------------
# Grocy pre-fills the price at purchase from product_barcodes.last_price, and
# all 159 barcodes had it empty — so even prices typed by hand were not coming
# back. Two sources, both EXACT; there is no name matching anywhere here.
#
#   1. Own purchase history. The price last typed for that product. Free,
#      exact, and the reason this job is worth running even with no token.
#   2. Salling's Anti Food Waste feed, which carries an EAN and the ORIGINAL
#      shelf price alongside each markdown. Matched on the barcode itself, so
#      it cannot attach a price to the wrong product.
#
# Deliberately NOT here: matching REMA's catalogue by product name. It has
# 3850 live prices but publishes no barcode, and name matching put a bar of
# soap's price on the lasagne sheets. A wrong price is invisible — nobody
# re-checks a number that is already filled in — so only exact keys are used.
SET_BARCODE_PRICES = os.environ.get("SET_BARCODE_PRICES", "1") == "1"
# How far back to look for a price you typed yourself. Long: a price from two
# years ago beats no price at all, and it is only ever a pre-fill.
PRICE_LOOKBACK_DAYS = int(os.environ.get("PRICE_LOOKBACK_DAYS", 1825))
# Optional. Without a token the Salling half is skipped and the job still does
# the purchase-history backfill. Free key from developer.sallinggroup.dev,
# scope "Products EAN" (GET /v2/products/{ean}).
SALLING_TOKEN = os.environ.get("SALLING_TOKEN", "")
# Prices are per physical store, so the lookup needs one. Comma-separated UUIDs
# from the Stores API are rotated by ISO week number, so two ids alternate odd
# and even weeks. Without any, the Salling half is skipped.
SALLING_STORE_ID = os.environ.get("SALLING_STORE_ID", "")
# One request per barcode, and the quota is 100 PER DAY. 90 leaves headroom for
# a manual run on the same day without tripping it. With ~128 unpriced barcodes
# the first pass therefore takes two runs, which is why this is scheduled
# weekly rather than monthly: monthly would take two months to converge for no
# gain. A 429 aborts the rest of the run either way.
SALLING_MAX_LOOKUPS = int(os.environ.get("SALLING_MAX_LOOKUPS", 90))
# Seconds between lookups. There is a burst limit on top of the daily quota —
# firing a dozen requests back to back gets 429s regardless of how much of the
# daily allowance is left. Measured while developing this, not guessed.
SALLING_DELAY = float(os.environ.get("SALLING_DELAY", 1.5))
# GS1 company prefixes belonging to OTHER chains' private label, which Salling
# by definition does not sell: 5705830 is REMA 1000, 5705001 is Coop (ØGO).
# 26 of the 92 unpriced barcodes here are one of those, so without this a
# third of a scarce daily quota is spent on guaranteed misses. Skipping only
# ever costs a lookup, never a wrong price.
SALLING_SKIP_PREFIXES = tuple(
    p.strip() for p in
    os.environ.get("SALLING_SKIP_PREFIXES", "5705830,5705001").split(",")
    if p.strip()
)
# Barcodes Salling has denied this many times are assumed not to be sold there
# and are dropped from the rotation, freeing the quota for ones that might
# still land. Not forever, though: ranges change, so they are retried once a
# year. State lives in a Grocy userfield on the barcode — the worker stays
# stateless, and the record sits next to the thing it describes.
SALLING_MISS_LIMIT = int(os.environ.get("SALLING_MISS_LIMIT", 4))
SALLING_RETRY_DAYS = int(os.environ.get("SALLING_RETRY_DAYS", 365))
PROBE_FIELD = "salling_probe"

# --- price comparison on the shopping list ----------------------------------
# Each row on the shared list carries a sub-line with what the item costs at
# each store, cheapest first, so it is obvious where to buy. The todo card
# truncates the summary to one line on a phone, so the description is the only
# place this fits.
SHOW_PRICES_ON_LIST = os.environ.get("SHOW_PRICES_ON_LIST", "1") == "1"
SHELF_FIELD = "salling_shelf"
# Refreshed when older than this. Shelf prices move slowly and the list is
# reconciled every 15 minutes, so re-asking often would spend the whole daily
# quota on prices that have not changed.
SHELF_MAX_AGE_DAYS = int(os.environ.get("SHELF_MAX_AGE_DAYS", 7))
# How many stores to show. More than three does not fit the sub-line.
SHELF_SHOW = int(os.environ.get("SHELF_SHOW", 3))

# --- weekly offers (tilbud) -------------------------------------------------
# From Tjek (eTilbudsavis), which aggregates most Danish chains: REMA 1000,
# Lidl, Netto, Føtex, Bilka, MENY, SPAR, Løvbjerg, nemlig. Free, no key.
#
# Coop IS included, via Kvickly, SuperBrugsen, Brugsen and 365discount. An
# early 8-query sample missed them and this comment used to say otherwise —
# a reminder that "not in my sample" is not "not there".
#
# Offers carry NO barcode, so they are matched on product name. That is the
# same technique rejected for shelf prices, and it is allowed here for two
# reasons: the threshold is high enough that most offers match nothing and are
# dropped (6 of 372 matched, all 6 correct), and an offer is shown to be read,
# not written into Grocy. A wrong shelf price is invisible; a wrong offer costs
# a glance. They are marked with ⚡ on the row so the distinction stays visible.
SHOW_OFFERS = os.environ.get("SHOW_OFFERS", "1") == "1"
TJEK_URL = "https://squid-api.tjek.com/v2/offers/search"
# Only near-identical names. Precision here comes from being allowed to answer
# "no match" — the earlier attempt always took the best of 3844 products and so
# put a bar of soap's price on the lasagne.
OFFER_MATCH_MIN = float(os.environ.get("OFFER_MATCH_MIN", 0.80))
OFFER_SHOW = int(os.environ.get("OFFER_SHOW", 2))
# Offers run in weekly cycles and the job that fetches them is weekly, so this
# only guards against a same-week second run.
OFFER_MAX_AGE_DAYS = int(os.environ.get("OFFER_MAX_AGE_DAYS", 3))
OFFER_FIELD = "offers"
# Tjek indexes more than supermarkets. Without an allowlist, "Spagetti" matched
# AB Catering at 79,00 — a wholesale catering pack — and the German border
# shops (Fleggaard, Poetzsch, Nielsen Scan-Shop) turn up constantly for someone
# who is never going to drive to Padborg for pasta. An allowlist rather than a
# blocklist: a new wholesaler appearing should be ignored by default, not
# quietly trusted.
OFFER_DEALERS = tuple(d.strip().lower() for d in os.environ.get(
    "OFFER_DEALERS",
    "rema 1000,netto,føtex,foetex,bilka,lidl,aldi,kvickly,superbrugsen,"
    "brugsen,dagli'brugsen,365discount,coop,meny,spar,løvbjerg,nemlig"
).split(",") if d.strip())

USER_AGENT = os.environ.get("USER_AGENT", "grocy-lists/1.0")

SEP = " — "  # separates product name from the level annotation

DRY_RUN = False  # set from --dry-run

# ---------------------------------------------------------------- clients


def _session():
    """Retry transient failures. Grocy hiccups and HA restarts are the
    expected case, not the exception — a bare throw mid-reconcile is what
    leaves the list half-applied."""
    s = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.5,                      # 0s, 1.5s, 3s, 6s
        status_forcelist=(429, 500, 502, 503, 504),
        # PUT included deliberately: the only PUT is a product field write,
        # which is idempotent, so retrying a 5xx cannot double-apply it.
        allowed_methods=frozenset(["GET", "POST", "PUT"]),
        raise_on_status=False,
    )
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent": USER_AGENT})   # Cloudflare — see header
    return s


SESSION = _session()


def grocy(path, **params):
    r = SESSION.get(
        f"{GROCY_URL}/api/{path}",
        headers={"GROCY-API-KEY": GROCY_KEY},
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def ha(service, data, response=False):
    domain, name = service.split(".")
    url = f"{HA_URL}/api/services/{domain}/{name}"
    if response:
        url += "?return_response=true"
    r = SESSION.post(
        url,
        headers={"Authorization": f"Bearer {HA_TOKEN}"},
        json=data,
        timeout=30,
    )
    r.raise_for_status()
    return r.json() if response else None


def ha_items(entity):
    """Every item, both statuses. Completed ones count as present so a
    ticked-but-not-yet-purchased item doesn't get re-added as a duplicate.
    status is passed explicitly — see the verification record above."""
    out = ha(
        "todo.get_items",
        {"entity_id": entity, "status": ["needs_action", "completed"]},
        response=True,
    )
    return out["service_response"][entity]["items"]


# ---------------------------------------------------------------- helpers


def fmt(n):
    n = float(n)
    return str(int(n)) if n == int(n) else f"{n:.1f}".replace(".", ",")


def key_of(text):
    return text.split(SEP)[0].strip()


# ------------------------------------------------------------- ownership
#
# The shopping list is SHARED with the household now, so the worker must be
# able to tell its own rows from hand-added ones. Ownership is decided purely
# from the row text, in the format this file writes:
#
#   stock row       "Mælk — 1/3 Liter"        -> annotation starts "have/min"
#   suggestion row  "Mælk — min 1 ↑ 2 Liter"  -> annotation contains "min N → M"
#
# A hand-added "Mælk" or "Blomster til bordet" matches neither and is left
# strictly alone. The one way to get bitten is writing a manual item that
# happens to look like "Something — 1/2 Liter"; the em-dash makes that
# unlikely by hand, and only that exact shape is claimed.

STOCK_ITEM_RE = re.compile(r"^\d[\d.,]*\s*/\s*\d[\d.,]*(\s|$)")


def owns_stock_item(summary):
    if SEP not in summary:
        return False
    return bool(STOCK_ITEM_RE.match(summary.split(SEP, 1)[1].strip()))


def owns_suggestion(summary):
    if SEP not in summary:
        return False
    return bool(SUGGEST_RE.search(summary.split(SEP, 1)[1]))


def plan(entity, desired, owned, notes=None):
    """Compute the full add/rename/remove plan before touching anything.

    desired: {key: text}. Key is the stable identity (product name); text
    is what's displayed.

    notes: {key: description} — the grey sub-line the todo card renders under
    the row. Optional; absent means every description is left alone.

    owned: callable(summary) -> bool, deciding which rows belong to the
    automation. Rows it rejects are INVISIBLE to the reconciler — never
    renamed, never removed, never counted. This is what lets the household
    add their own items to the same list: the worker no longer owns the
    list, only the rows it recognises as its own."""
    notes = notes or {}
    current, current_note = {}, {}
    for item in ha_items(entity):
        if not owned(item["summary"]):
            continue
        key = key_of(item["summary"])
        current[key] = item["summary"]
        current_note[key] = item.get("description") or ""

    adds = [(key, text) for key, text in desired.items() if key not in current]
    renames = [
        (key, current[key], text)
        for key, text in desired.items()
        if key in current and current[key] != text
    ]
    removes = [s for key, s in current.items() if key not in desired]
    # Rows that stay but whose sub-line has changed — a price moved, or the
    # row had no prices last run. Keyed by the text the row will have AFTER
    # any rename, since descriptions are applied last.
    redescribes = [
        (text, notes.get(key, ""))
        for key, text in desired.items()
        if key in current and notes.get(key, "") != current_note.get(key, "")
    ]
    return adds, renames, removes, redescribes


def reconcile(entity, desired, owned, notes=None):
    """Apply the plan. Adds and renames run before any removal, so a
    failure part-way leaves the list with extra items rather than missing
    ones — and we abort before the removal phase if anything threw.

    Descriptions are the grey sub-line the todo card renders under each row.
    An earlier version of this file refused to use them, but for the wrong
    reason: what looked bad was a machine-readable JSON payload, not the
    mechanism. Short human text is exactly what the field is for — and it is
    the only place detail fits, since the card truncates the summary to one
    line on a phone.

    They are applied AFTER renames, because a rename changes the summary that
    identifies the row."""
    notes = notes or {}
    adds, renames, removes, redescribes = plan(entity, desired, owned, notes)

    if DRY_RUN:
        print(f"  [dry-run] {entity}: +{len(adds)} ~{len(renames)} "
              f"-{len(removes)} ≡{len(redescribes)}")
        for key, t in adds:
            note = notes.get(key)
            print(f"      + {t}" + (f"\n          {note}" if note else ""))
        for _, old, new in renames:
            print(f"      ~ {old!r} -> {new!r}")
        for text, note in redescribes:
            print(f"      ≡ {text}\n          {note or '(cleared)'}")
        for s in removes:
            print(f"      - {s}")
        return

    for key, text in adds:
        payload = {"entity_id": entity, "item": text}
        if notes.get(key):
            payload["description"] = notes[key]
        ha("todo.add_item", payload)
    for _, old, new in renames:
        # rename in place: keeps the row's position and its ticked state
        ha("todo.update_item", {"entity_id": entity, "item": old, "rename": new})
    for text, note in redescribes:
        ha("todo.update_item",
           {"entity_id": entity, "item": text, "description": note})
    # only now, once every addition has landed, remove what is no longer wanted
    for summary in removes:
        ha("todo.remove_item", {"entity_id": entity, "item": summary})

    print(f"  {entity}: +{len(adds)} ~{len(renames)} -{len(removes)} "
          f"≡{len(redescribes)}")


# ------------------------------------------------------- approved changes


def grocy_post(path, body):
    r = SESSION.post(
        f"{GROCY_URL}/api/{path}",
        headers={"GROCY-API-KEY": GROCY_KEY, "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    r.raise_for_status()
    return r.json() if r.content else None


def grocy_put(path, body):
    r = SESSION.put(
        f"{GROCY_URL}/api/{path}",
        headers={"GROCY-API-KEY": GROCY_KEY, "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    r.raise_for_status()
    return r.status_code


# "Navn — min 1 ↑ 1,3 Pakke (0,6/7d) · ..."  ->  the 1,3
SUGGEST_RE = re.compile(r"min\s+[\d.,]+\s*[↑↓→]\s*([\d.,]+)")


def apply_approved(entity):
    """Ticking a suggestion IS the approval. Read the completed items back,
    write their suggested minimum to Grocy, then remove them from the list.

    This is the ONLY place the worker writes to Grocy, and it only ever runs
    against SUGGEST_LIST — ticking something on the shopping list means
    "bought it", never "change Grocy".

    The target value is parsed out of the summary this file wrote, so what
    gets applied is exactly the number that was on screen when it was
    ticked — not a value re-derived later that may have drifted."""
    completed = [i for i in ha_items(entity) if i.get("status") == "completed"]
    if not completed:
        return 0

    # only pay for the catalogue lookup when there is something to apply
    by_name = {p["name"]: pid for pid, p in product_catalogue().items()}

    applied = skipped = 0
    for item in completed:
        summary = item["summary"]
        name = key_of(summary)
        pid = by_name.get(name)
        m = SUGGEST_RE.search(summary)
        if pid is None or not m:
            print(f"  skip (cannot read suggestion): {summary!r}", file=sys.stderr)
            skipped += 1
            continue
        try:
            value = float(m.group(1).replace(",", "."))
        except ValueError:
            print(f"  skip (unparsable value): {summary!r}", file=sys.stderr)
            skipped += 1
            continue

        if DRY_RUN:
            print(f"  [dry-run] would set min_stock_amount={value} on "
                  f"{name!r} (product {pid})")
            applied += 1
            continue

        grocy_put(f"objects/products/{pid}", {"min_stock_amount": value})
        ha("todo.remove_item", {"entity_id": entity, "item": summary})
        print(f"  applied min_stock_amount={value} to {name!r} (product {pid})")
        applied += 1

    if applied or skipped:
        print(f"  approved changes: {applied} applied, {skipped} skipped")
    return applied


def product_catalogue():
    units = {u["id"]: u["name"] for u in grocy("objects/quantity_units")}
    products = {}
    for p in grocy("objects/products"):
        if str(p.get("active", 1)) == "0":
            continue
        # .strip() is load-bearing: 48 of 109 product names carry trailing
        # whitespace, and reconcile()'s key is derived with .strip(). Without
        # this the two keys never match and every run re-adds the item.
        name = (p["name"] or "").strip()
        if SEP in name:
            print(f"WARNING: product name contains {SEP!r}, join key will be "
                  f"wrong: {name!r}", file=sys.stderr)
        products[int(p["id"])] = {
            "name": name,
            "min": float(p.get("min_stock_amount") or 0),
            "qu": units.get(int(p["qu_id_stock"] or 0), ""),
            # Grocy uses -1 for "never expires", 0/empty for "not set".
            "bbd": int(p.get("default_best_before_days") or 0),
        }
    return products


# ---------------------------------------------------------------- job: sync


def offers_by_product():
    """{product_id: [offer]} from the product cache. Read-only, like the shelf
    prices: sync never queries Tjek itself."""
    out = {}
    for p in grocy("objects/products"):
        found, _ = parse_offers((p.get("userfields") or {}).get(OFFER_FIELD))
        if found:
            out[int(p["id"])] = found
    return out


def shelf_prices_by_product():
    """{product_id: {store: price}} from the barcode cache.

    Read-only and free: Grocy returns userfields inline with the barcodes, and
    sync must never call Salling itself — it runs every 15 minutes against a
    quota of 100 a day. The weekly prices job is what fills this in.

    A product with several barcodes keeps the first priced one; they are the
    same product, so a second opinion would only flicker the row."""
    out = {}
    for b in grocy("objects/product_barcodes"):
        pid = int(b["product_id"])
        if pid in out:
            continue
        prices, _ = parse_shelf((b.get("userfields") or {}).get(SHELF_FIELD))
        if prices:
            out[pid] = prices
    return out


def job_sync():
    # Anything she ticked on the suggestions list gets applied first, so the
    # catalogue we read below already reflects it.
    apply_approved(SUGGEST_LIST)

    products = product_catalogue()
    stock = {int(s["product_id"]): float(s["amount"]) for s in grocy("stock")}
    # Prices are read from cache only — never looked up here. sync runs every
    # 15 minutes and the Salling quota is 100 a day; the weekly prices job is
    # what refreshes them.
    shelf = shelf_prices_by_product() if SHOW_PRICES_ON_LIST else {}
    offers = offers_by_product() if SHOW_OFFERS else {}
    now = datetime.now()

    desired, notes = {}, {}
    for pid, p in products.items():
        if p["min"] <= 0:
            continue
        have = stock.get(pid, 0.0)
        if have >= p["min"]:
            continue
        if p["name"] in desired:
            print(f"WARNING: duplicate product name {p['name']!r}; only one "
                  f"row will be tracked", file=sys.stderr)
        desired[p["name"]] = (
            f"{p['name']}{SEP}{fmt(have)}/{fmt(p['min'])} {p['qu']}".strip()
        )
        # Exact shelf prices first, then any name-matched offers behind a ⚡.
        # The card collapses newlines into one wrapped paragraph, so the marker
        # is what separates "measured" from "matched", not a line break.
        parts = [price_line(shelf.get(pid, {})),
                 offer_line(offers.get(pid, []), now)]
        notes[p["name"]] = " ".join(x for x in parts if x)

    priced = sum(1 for n in notes.values() if n)
    print(f"sync: {len(desired)} products below minimum, {priced} with prices")
    reconcile(AUTO_LIST, desired, owns_stock_item, notes)
    return 0


# ------------------------------------------------------------- job: analyse


def stock_log_rows(transaction_type, days):
    """All non-undone rows of one transaction type in the window, paginated.
    Server-side filtering and paging both verified against Grocy 4.6.0."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows, offset, page = [], 0, 500
    while True:
        batch = grocy(
            "objects/stock_log",
            **{
                "query[]": [
                    f"transaction_type={transaction_type}",
                    "undone=0",
                    f"row_created_timestamp>{cutoff}",
                ],
                "limit": page,
                "offset": offset,
            },
        )
        rows += batch
        if len(batch) < page:
            return rows
        offset += page


def consumption_log(days):
    return stock_log_rows("consume", days)


def _date(s):
    """Grocy dates are plain 'YYYY-MM-DD'. Returns None on empty/garbage."""
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None


def shelf_default_for(observations, current):
    """The default_best_before_days to write, or None to leave the field alone.

    Pure, and deliberately conservative — it says None far more often than it
    says a number. Every reason to decline is a case where writing something
    would be worse than writing nothing:

      too few observations   a guess dressed as a default
      -1 already set         someone declared this never expires; not ours
      observations disagree  no single number describes the product
      close to what is set   a hand-set value that is already roughly right

    Rounds DOWN, the opposite of min_stock_amount's rounding up. Both round
    towards the safe error: an extra packet on the shelf, and a best-before
    warning that comes early rather than late."""
    if len(observations) < MIN_SHELF_OBS_WRITE:
        return None
    if current < 0:
        return None

    med = statistics.median(observations)
    if med < 1:
        return None

    # Median absolute deviation, not stdev: one mis-typed date should not be
    # able to veto a product whose other observations agree perfectly.
    mad = statistics.median([abs(x - med) for x in observations])
    if mad / med > SHELF_REL_SPREAD:
        return None

    value = int(math.floor(med))
    if value < 1 or value == current:
        return None
    if current > 0 and abs(value - current) / current <= EXPIRY_CHANGE_THRESHOLD:
        return None
    return value


def apply_shelf_defaults(products, shelf_obs):
    """Write observed shelf life back to Grocy as default_best_before_days.

    The second write path to Grocy, and the only one that runs unattended.
    It exists so that scanning a barcode at purchase pre-fills a sensible
    best-before date instead of today's.

    Note the feedback loop this creates: once the field is set, Grocy offers
    that date at purchase, and accepting it makes the next observation echo
    this value rather than the package. What breaks the loop is the human
    checking the physical date — which is exactly the habit that makes writing
    without approval acceptable in the first place. It is also why the median
    is taken over the whole window rather than the most recent observations."""
    if not SET_DEFAULT_EXPIRY:
        return 0

    written = 0
    for pid, obs in sorted(shelf_obs.items()):
        p = products.get(pid)
        if not p:
            continue
        value = shelf_default_for(obs, p["bbd"])
        if value is None:
            continue
        was = p["bbd"] or "unset"
        if DRY_RUN:
            print(f"  [dry-run] would set default_best_before_days={value} on "
                  f"{p['name']!r} (was {was}, {len(obs)} observations)")
        else:
            grocy_put(f"objects/products/{pid}", {"default_best_before_days": value})
            print(f"  set default_best_before_days={value} on {p['name']!r} "
                  f"(was {was}, {len(obs)} observations)")
        written += 1
    return written


def job_analyse():
    # Apply first, then re-derive — otherwise a change approved since the last
    # run gets re-suggested as though it had never been made.
    apply_approved(SUGGEST_LIST)

    products = product_catalogue()
    rows = consumption_log(LOOKBACK_DAYS)

    n_buckets = max(1, math.ceil(LOOKBACK_DAYS / SHOPPING_INTERVAL_DAYS))
    now = datetime.now()

    used = defaultdict(float)
    spoiled = defaultdict(float)
    events = defaultdict(int)
    buckets = defaultdict(lambda: [0.0] * n_buckets)
    shelf_obs = defaultdict(list)   # observed best_before - purchased, days
    hold_obs = defaultdict(list)    # observed used - purchased, days
    late = defaultdict(int)         # consumed after its best-before date
    dated = defaultdict(int)        # consume rows carrying a usable date pair

    for row in rows:
        pid = int(row["product_id"])
        amount = abs(float(row["amount"]))

        # Shelf-life signals come off the consumed stock entry itself, so no
        # second API call is needed. Collected even for spoiled rows — a unit
        # that rotted is exactly the evidence we want here.
        bb = _date(row.get("best_before_date"))
        pd = _date(row.get("purchased_date"))
        ud = _date(row.get("used_date"))
        if bb and pd:
            days = (bb - pd).days
            if 0 < days <= SHELF_SANE_MAX_DAYS:
                shelf_obs[pid].append(days)
        if bb and ud:
            dated[pid] += 1
            if ud > bb:
                late[pid] += 1
        if pd and ud and ud >= pd:
            hold_obs[pid].append((ud - pd).days)

        if str(row.get("spoiled", 0)) == "1":
            spoiled[pid] += amount
            continue  # spoilage is not demand
        ts = datetime.strptime(row["row_created_timestamp"], "%Y-%m-%d %H:%M:%S")
        idx = min(n_buckets - 1, int((now - ts).days / SHOPPING_INTERVAL_DAYS))
        used[pid] += amount
        events[pid] += 1
        buckets[pid][idx] += amount

    desired = {}
    for pid, total in used.items():
        p = products.get(pid)
        if not p or events[pid] < MIN_EVENTS:
            continue

        per_cycle = total / n_buckets
        sigma = statistics.pstdev(buckets[pid]) if n_buckets > 1 else 0.0
        # classic reorder point: demand over the cycle + safety stock.
        # Always a whole number, rounded UP — you cannot keep 0,3 of a packet
        # on the shelf, and rounding up is the safe direction for a minimum.
        # Useful side effect: every "↓ 0,4" suggestion collapses to 1, which
        # usually equals the current minimum and so stops being a suggestion.
        suggested = max(1, math.ceil(per_cycle + SAFETY_K * sigma))

        # --- does it get used before it goes off? --------------------------
        # Ground truth is the per-unit comparison used_date > best_before_date,
        # counted while walking the rows. Median hold vs median shelf can look
        # fine while individual units still go past date (skewed pairing), so
        # late_frac drives the verdict and the medians only explain it.
        shelf = (statistics.median(shelf_obs[pid])
                 if len(shelf_obs[pid]) >= MIN_SHELF_OBS else None)
        hold = (statistics.median(hold_obs[pid]) if hold_obs[pid] else None)
        per_day = total / LOOKBACK_DAYS if LOOKBACK_DAYS else 0
        cover = (suggested / per_day) if per_day > 0 else None
        late_frac = (late[pid] / dated[pid]) if dated[pid] else 0.0

        verdict = ""
        # how much can realistically be eaten inside one shelf life
        eatable = (shelf * per_day) if (shelf and per_day > 0) else None

        if eatable is not None and eatable < 1 and late_frac > LATE_USE_FLAG:
            # a single unit outlives its own date at this rate of use
            verdict = f"✗ køb v. behov, {round(100 * late_frac)}% over dato"
            suggested = 0
        elif late_frac > LATE_USE_FLAG and eatable is not None:
            suggested = max(1, math.ceil(min(suggested, eatable)))
            verdict = f"⚠ {round(100 * late_frac)}% over dato"
        elif cover and shelf and cover > shelf:
            suggested = max(1, math.ceil(eatable))
            verdict = f"⚠ holdbarhed {round(shelf)}d"
        elif late_frac > LATE_USE_FLAG:
            verdict = f"⚠ {round(100 * late_frac)}% over dato"

        waste = spoiled[pid] / (total + spoiled[pid]) if total else 0
        if not verdict and waste > SPOILAGE_FLAG:
            verdict = f"⚠ {round(waste * 100)}% spild"

        current = p["min"]
        base = max(current, 0.5)
        changed = abs(suggested - current) / base >= CHANGE_THRESHOLD
        # A minimum that is not a whole number always gets surfaced, however
        # small the delta. Otherwise CHANGE_THRESHOLD hides it forever: a
        # product sitting at 1,7 is only 0,3 from 2 and would never clear the
        # 25% bar, so the decimal would be permanent.
        decimal_now = current != int(current)
        if not changed and not verdict and not decimal_now:
            continue

        # Flags go FIRST, right after the separator. The todo-list card
        # truncates each row to one line on a phone, so anything appended to
        # the end is exactly what gets cut off — which was the warnings.
        arrow = "↑" if suggested > current else ("↓" if suggested < current else "→")
        head = f"min {fmt(current)} {arrow} {fmt(suggested)} {p['qu']}"
        if verdict:
            head = f"{verdict} · {head}"
        note = f"{p['name']}{SEP}{head} · {fmt(per_cycle)}/uge"
        if hold is not None and shelf:
            note += f" · bruges {round(hold)}d, holder {round(shelf)}d"

        desired[p["name"]] = note

    qualified = sum(1 for e in events.values() if e >= MIN_EVENTS)
    with_shelf = sum(1 for pid in used if len(shelf_obs[pid]) >= MIN_SHELF_OBS)
    print(f"analyse: {len(rows)} consume rows across {len(used)} products over "
          f"{LOOKBACK_DAYS}d; {qualified} meet MIN_EVENTS={MIN_EVENTS}; "
          f"{with_shelf} have usable shelf-life data; {len(desired)} suggestions")
    reconcile(SUGGEST_LIST, desired, owns_suggestion)

    # Runs on every product with observations, not just the ones that produced
    # a suggestion: a product whose minimum is already right still benefits
    # from a sensible best-before default at the till.
    changed = apply_shelf_defaults(products, shelf_obs)
    if changed:
        print(f"  default_best_before_days: {changed} product(s) updated")
    return 0


# ---------------------------------------------------------------- job: prices


def latest_purchase_price(rows):
    """{product_id: price} from the most recent purchase that carried one.

    Most recent rather than average: a price is a pre-fill for the next
    purchase, so the last thing it actually cost beats a mean dragged down by
    what it cost two years ago."""
    best = {}
    for r in rows:
        try:
            price = float(r.get("price"))
            pid = int(r["product_id"])
        except (TypeError, ValueError, KeyError):
            continue
        if price <= 0:
            continue
        ts = r.get("row_created_timestamp") or ""
        if pid not in best or ts > best[pid][0]:
            best[pid] = (ts, price)
    return {pid: price for pid, (_, price) in best.items()}


def _as_price(value):
    """A positive number, or None. Grocy would happily store a string."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def extract_price(payload):
    """The shelf price from a /v2/products/{ean} response, or None.

    Verified against the live API on 2026-08-13. The shape is:

        {"instore": {"ean": "5710405090951", "name": "KOKOSMÆLK",
                     "description": "ASIA KITCHEN", "price": 9.5,
                     "contents": 400, "contentsUnit": "ml",
                     "unit": "l", "unitPrice": 23.75},
         "webshop": null}

    `unitPrice` is ignored ON PURPOSE. It is the comparison price per litre or
    kilo — 23,75 per litre for a tin of coconut milk that costs 9,50 — so
    reaching for it would overstate every product with a unit smaller than its
    comparison unit. Only `price` is the shelf price.

    instore first: this fills in what a barcode scan at the till should show.
    webshop is the fallback for products the store lists online only.

    Anything else returns None, which skips the barcode. A schema change must
    degrade to "no price" rather than to a wrong one — nobody re-checks a
    number that is already filled in."""
    if not isinstance(payload, dict):
        return None
    for section in ("instore", "webshop"):
        block = payload.get(section)
        if isinstance(block, dict):
            price = _as_price(block.get("price"))
            if price:
                return price
    # A flatter shape, should they ever simplify it.
    return _as_price(payload.get("price"))


def kr(value):
    """Danish money: 9.5 -> "9,50". Always two decimals, unlike fmt(), which
    drops them — a price with no øre reads as an error."""
    return f"{float(value):.2f}".replace(".", ",")


def parse_stores(spec):
    """"Netto=uuid,Bilka=uuid" -> [(name, uuid)].

    A bare uuid keeps working and gets a generic label, so an older one-store
    config is not silently broken by this format."""
    stores = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, uuid = part.partition("=")
        stores.append((name.strip(), uuid.strip()) if sep else ("Salling", part))
    return stores


def parse_shelf(value):
    """"Netto=9.50;Bilka=9.95|2026-08-15" -> ({name: price}, date)."""
    if not isinstance(value, str) or "|" not in value:
        return {}, None
    body, _, stamp = value.rpartition("|")
    prices = {}
    for part in body.split(";"):
        name, sep, number = part.partition("=")
        if not sep:
            continue
        price = _as_price(number)
        if price:
            prices[name.strip()] = price
    return prices, _date(stamp)


def format_shelf(prices, when):
    body = ";".join(f"{name}={price:.2f}" for name, price in sorted(prices.items()))
    return f"{body}|{when:%Y-%m-%d}"


def price_line(prices, limit=None):
    """The sub-line under a shopping-list row: cheapest store first.

    Cheapest first because the question the line answers is "where should I
    buy this", and the answer is the first thing on it."""
    if not prices:
        return ""
    limit = SHELF_SHOW if limit is None else limit
    best = sorted(prices.items(), key=lambda kv: (kv[1], kv[0]))[:limit]
    return " · ".join(f"{name} {kr(price)}" for name, price in best)


def norm_name(text):
    """Fold a product name for comparison: lowercase, Danish letters spelled
    out, punctuation dropped. "Havre drik" and "HAVREDRIK" must land close."""
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = text.replace("æ", "ae").replace("ø", "oe").replace("å", "aa")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", text)


def tjek_offers(query):
    """Danish offers matching a search term, from Tjek.

    Danish only: the same endpoint serves Norway, and a third of the results
    for a plain query come back in NOK. Currency is the filter, not the
    dealer name — "Netto" exists in both countries."""
    try:
        r = SESSION.get(TJEK_URL, params={"query": query, "limit": 50}, timeout=30)
        r.raise_for_status()
        rows = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  tjek lookup failed for {query!r}: {e}", file=sys.stderr)
        return []
    if not isinstance(rows, list):
        return []

    offers = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pricing = row.get("pricing") or {}
        if pricing.get("currency") != "DKK":
            continue
        price = _as_price(pricing.get("price"))
        dealer = ((row.get("dealer") or {}).get("name") or "").strip()
        heading = (row.get("heading") or "").strip()
        until = _date(str(row.get("run_till") or "")[:10])
        if dealer.lower() not in OFFER_DEALERS:
            continue
        if price and dealer and heading:
            offers.append({"dealer": dealer, "heading": heading,
                           "price": price, "until": until})
    return offers


def match_offers(product_name, offers, limit=None, threshold=None):
    """Offers whose heading is near-identical to the product name.

    Returns cheapest first, at most one per dealer — two Lidl offers for the
    same thing is noise on a row that has to fit a phone screen."""
    limit = OFFER_SHOW if limit is None else limit
    threshold = OFFER_MATCH_MIN if threshold is None else threshold
    key = norm_name(product_name)
    if not key:
        return []

    scored = []
    for offer in offers:
        score = difflib.SequenceMatcher(None, key, norm_name(offer["heading"])).ratio()
        if score >= threshold:
            scored.append((offer["price"], offer))

    best, seen = [], set()
    for _, offer in sorted(scored, key=lambda x: x[0]):
        if offer["dealer"] in seen:
            continue
        seen.add(offer["dealer"])
        best.append(offer)
        if len(best) >= limit:
            break
    return best


def offer_line(offers, today):
    """"⚡ REMA 1000 12,00 til 15/8". Expired offers are dropped rather than
    shown, since a tilbud that ended is worse than no tilbud at all."""
    parts = []
    for offer in offers:
        until = offer.get("until")
        if until is not None and until < today:
            continue
        when = f" til {until:%-d/%-m}" if until else ""
        parts.append(f"{offer['dealer']} {kr(offer['price'])}{when}")
    return ("⚡ " + " · ".join(parts)) if parts else ""


def format_offers(offers, when):
    body = ";".join(
        f"{o['dealer']}={o['price']:.2f}@{o['until']:%Y-%m-%d}" if o.get("until")
        else f"{o['dealer']}={o['price']:.2f}@"
        for o in offers
    )
    return f"{body}|{when:%Y-%m-%d}"


def parse_offers(value):
    """Inverse of format_offers -> ([{dealer, price, until}], fetched)."""
    if not isinstance(value, str) or "|" not in value:
        return [], None
    body, _, stamp = value.rpartition("|")
    offers = []
    for part in body.split(";"):
        dealer, sep, rest = part.partition("=")
        if not sep:
            continue
        number, _, until = rest.partition("@")
        price = _as_price(number)
        if price:
            offers.append({"dealer": dealer.strip(), "price": price,
                           "until": _date(until)})
    return offers, _date(stamp)


def pick_store(ids, week):
    """Which store to ask this week — ids rotated by ISO week number.

    With two ids that is a plain odd/even alternation, and it buys more than
    fairness between the shops: a barcode the small Netto does not stock may
    be found at the Bilka a fortnight later. Because only EMPTY prices are
    ever filled, the two stores' ranges accumulate rather than overwrite each
    other, and no product ends up flapping between two shops' prices."""
    if not ids:
        return ""
    return ids[week % len(ids)]


def store_ids():
    return [s.strip() for s in SALLING_STORE_ID.split(",") if s.strip()]


def parse_probe(value):
    """"3:2026-08-13" -> (3, date). Anything unreadable -> (0, None).

    Failing open is the right direction: a corrupted marker costs one extra
    lookup, whereas failing closed would silently retire a barcode for good."""
    misses, last = 0, None
    if not isinstance(value, str):
        return misses, last
    count, _, stamp = value.partition(":")
    try:
        misses = max(0, int(count))
    except ValueError:
        return 0, None
    last = _date(stamp)
    return misses, last


def should_ask(probe_value, today):
    """False for a barcode Salling has repeatedly denied, until its yearly
    retry comes round. Ranges change, so "not sold here" is never permanent."""
    misses, last = parse_probe(probe_value)
    if misses < SALLING_MISS_LIMIT:
        return True
    if last is None:
        return True
    return (today - last).days >= SALLING_RETRY_DAYS


USERFIELDS = [
    ("product_barcodes", PROBE_FIELD, "Salling lookup state (set by grocy_lists)"),
    ("product_barcodes", SHELF_FIELD, "Salling shelf prices (set by grocy_lists)"),
    # Offers match on product NAME, so they belong to the product, not to one
    # of its barcodes.
    ("products", OFFER_FIELD, "Weekly offers (set by grocy_lists)"),
]


def ensure_userfields():
    """Define the userfields this worker stores its state in, if missing.

    Idempotent, and the only schema change it ever makes. Grocy returns
    userfields inline on the bulk barcode fetch once one exists, so reading
    the state afterwards costs nothing.

    The `note` field would have been the easy place for this and is
    deliberately untouched: it belongs to the household, and one barcode
    already says "ÆG!!"."""
    have = {(uf.get("entity"), uf.get("name")) for uf in grocy("objects/userfields")}
    for entity, name, caption in USERFIELDS:
        if (entity, name) in have:
            continue
        grocy_post("objects/userfields", {
            "entity": entity,
            "name": name,
            "caption": caption,
            "type": "text-single-line",
            "show_as_column_in_tables": 0,
        })
        print(f"  created Grocy userfield {name!r} on {entity}")


def worth_asking(barcode):
    """False for barcodes Salling cannot possibly stock — another chain's own
    brand. Purely a quota saving; being wrong costs one skipped lookup."""
    return bool(barcode) and not barcode.startswith(SALLING_SKIP_PREFIXES)


def rotate(items, week, window):
    """Start each week's lookups where the last week's left off.

    Without this the run always starts at the top of the list, and since a
    miss leaves the barcode empty, the same first `window` barcodes would be
    re-asked every week forever — anything past that position would never be
    looked up at all. Rotating means the whole list is covered over a few
    weeks, however small the quota is relative to it."""
    if not items or window <= 0:
        return items
    start = (week * window) % len(items)
    return items[start:] + items[:start]


def salling_price(ean, store_id):
    """Shelf price for one barcode at that store, or None.

    404 is the normal case, not an error: it means that store does not stock
    the product. Anything else unexpected also yields None — an opportunistic
    source must never be able to fail the job."""
    r = SESSION.get(
        f"https://api.sallinggroup.com/v2/products/{ean}",
        headers={"Authorization": f"Bearer {SALLING_TOKEN}"},
        params={"storeId": store_id},
        timeout=30,
    )
    if r.status_code == 404:
        return None
    if r.status_code == 429:
        # Not retried: there is a daily quota behind this, and burning it on
        # retries would cost the next run rather than saving this one.
        raise RuntimeError("rate limited (429) — stopping for this run")
    r.raise_for_status()
    try:
        return extract_price(r.json())
    except ValueError:
        return None


def refresh_shelf_prices(products, barcodes, stock, stores, budget, now):
    """Price the things that are actually on the shopping list, at every store.

    Runs before the last_price backfill and takes priority over it: knowing
    where to buy the six items you need this week is worth more than filling
    in a price for something already in the cupboard.

    Returns the number of lookups spent."""
    if not stores or budget <= 0:
        return 0

    wanted = {pid for pid, p in products.items()
              if p["min"] > 0 and stock.get(pid, 0.0) < p["min"]}
    spent = 0
    for b in barcodes:
        pid = int(b["product_id"])
        code = str(b.get("barcode") or "").strip()
        if pid not in wanted or not worth_asking(code):
            continue
        _, when = parse_shelf((b.get("userfields") or {}).get(SHELF_FIELD))
        if when is not None and (now - when).days < SHELF_MAX_AGE_DAYS:
            continue                      # still fresh, do not spend on it
        if budget - spent < len(stores):
            break                         # not enough left for a full comparison

        found = {}
        for name, uuid in stores:
            if spent:
                time.sleep(SALLING_DELAY)
            try:
                price = salling_price(code, uuid)
            except (requests.RequestException, RuntimeError) as e:
                print(f"  salling price refresh stopped: {e}", file=sys.stderr)
                return spent
            spent += 1
            if price:
                found[name] = price

        name = (products.get(pid) or {}).get("name", code)
        if DRY_RUN:
            print(f"  [dry-run] {name}: {price_line(found) or 'not stocked anywhere'}")
        else:
            # Written even when empty, so a product no store carries is not
            # re-priced every single run.
            grocy_put(f"userfields/product_barcodes/{b['id']}",
                      {SHELF_FIELD: format_shelf(found, now)})
    return spent


def refresh_offers(products, stock, now):
    """Look up this week's offers for whatever is on the shopping list.

    One Tjek query per product, and only for things actually needed — the
    point is "there is a tilbud on something you are about to buy", which is
    only interesting for the handful on the list."""
    if not SHOW_OFFERS:
        return 0

    raw = {int(p["id"]): p for p in grocy("objects/products")}
    checked = 0
    for pid, p in products.items():
        if p["min"] <= 0 or stock.get(pid, 0.0) >= p["min"]:
            continue
        cached = (raw.get(pid, {}).get("userfields") or {}).get(OFFER_FIELD)
        _, when = parse_offers(cached)
        if when is not None and (now - when).days < OFFER_MAX_AGE_DAYS:
            continue

        found = match_offers(p["name"], tjek_offers(p["name"]))
        checked += 1
        if DRY_RUN:
            line = offer_line(found, now)
            print(f"  [dry-run] {p['name']}: {line or 'ingen tilbud'}")
        else:
            # Written even when empty, so a product with no offers this week is
            # not re-queried on every run.
            grocy_put(f"userfields/products/{pid}",
                      {OFFER_FIELD: format_offers(found, now)})
    return checked


def job_prices():
    """Fill product_barcodes.last_price where it is empty, so a barcode scan
    at purchase pre-fills a price.

    Only ever fills EMPTY fields — a price already there was either typed by
    the household or written by an earlier run, and neither is ours to
    second-guess. Both sources are keyed on exact identifiers, so nothing here
    can attach a price to the wrong product."""
    if not SET_BARCODE_PRICES:
        print("prices: disabled (SET_BARCODE_PRICES=0)")
        return 0

    products = product_catalogue()
    barcodes = grocy("objects/product_barcodes")
    own = latest_purchase_price(stock_log_rows("purchase", PRICE_LOOKBACK_DAYS))

    empty = [b for b in barcodes
             if str(b.get("last_price") or "").strip() in ("", "0", "0.0")]
    print(f"prices: {len(empty)} of {len(barcodes)} barcodes have no price; "
          f"{len(own)} products have one in your purchase history")

    now = datetime.now()
    week = now.isocalendar()[1]
    stores = parse_stores(SALLING_STORE_ID)
    picked = pick_store(stores, week)
    store = picked[1] if picked else ""
    # Lookups start where last week's left off, so the whole list gets covered
    # over a few weeks rather than the first 90 being re-asked forever.
    empty = rotate(empty, week, SALLING_MAX_LOOKUPS)

    salling_on = bool(SALLING_TOKEN and store)
    wants_list_work = SHOW_PRICES_ON_LIST or SHOW_OFFERS
    if (salling_on or wants_list_work) and not DRY_RUN:
        ensure_userfields()

    # Both of the next two steps care about what is on the shopping list, so
    # stock is read once here rather than by each of them.
    stock = ({int(s["product_id"]): float(s["amount"]) for s in grocy("stock")}
             if wants_list_work else {})

    # What is on the list gets priced first, at every store. Knowing where to
    # buy the six things you need beats filling in a price for something
    # already in the cupboard.
    spent = 0
    if salling_on and SHOW_PRICES_ON_LIST:
        spent = refresh_shelf_prices(products, barcodes, stock, stores,
                                     SALLING_MAX_LOOKUPS, now)
        if spent:
            print(f"  salling: {spent} lookups pricing the shopping list "
                  f"across {len(stores)} stores")

    if SHOW_OFFERS:
        checked = refresh_offers(products, stock, now)
        if checked:
            print(f"  tilbud: checked {checked} products against Tjek")

    if salling_on:
        askable = sum(
            1 for b in empty
            if worth_asking(str(b.get("barcode") or ""))
            and should_ask((b.get("userfields") or {}).get(PROBE_FIELD), now)
        )
        print(f"  salling: week {week}, backfilling from {picked[0]}, "
              f"{askable} of {len(empty)} barcodes worth asking about")

    from_history = from_salling = 0
    # The shopping-list pricing above came out of the same daily allowance.
    looked_up, misses = spent, 0
    for b in empty:
        pid = int(b["product_id"])
        code = str(b.get("barcode") or "").strip()

        price, source = own.get(pid), "purchase history"

        # Salling is asked only about barcodes your own history cannot answer,
        # and only up to a cap: this is a free API offered for non-commercial
        # use, and a bug that turned it into a tight loop would be abusing it.
        probe = (b.get("userfields") or {}).get(PROBE_FIELD)
        if (price is None and salling_on and worth_asking(code)
                and should_ask(probe, now)
                and looked_up < SALLING_MAX_LOOKUPS):
            if looked_up:
                time.sleep(SALLING_DELAY)   # burst limit, see SALLING_DELAY
            try:
                price = salling_price(code, store)
                looked_up += 1
            except (requests.RequestException, RuntimeError) as e:
                print(f"  salling lookup stopped: {e}", file=sys.stderr)
                break
            source = "salling"

            # Record the outcome so a barcode Salling keeps denying drops out
            # of the rotation. Cleared on a hit, since the barcode now has a
            # price and a stale marker would only mislead a later reader.
            if not DRY_RUN:
                if price is None:
                    seen, _ = parse_probe(probe)
                    grocy_put(f"userfields/product_barcodes/{b['id']}",
                              {PROBE_FIELD: f"{seen + 1}:{now:%Y-%m-%d}"})
                elif probe:
                    grocy_put(f"userfields/product_barcodes/{b['id']}",
                              {PROBE_FIELD: ""})
            if price is None:
                misses += 1

        if price is None:
            continue

        name = (products.get(pid) or {}).get("name", f"product {pid}")
        if DRY_RUN:
            print(f"  [dry-run] would set last_price={price} on {code} "
                  f"({name}) from {source}")
        else:
            grocy_put(f"objects/product_barcodes/{b['id']}",
                      {"last_price": price})
            print(f"  set last_price={price} on {code} ({name}) from {source}")
        if source == "salling":
            from_salling += 1
        else:
            from_history += 1

    if looked_up:
        print(f"  salling: asked about {looked_up} barcodes, "
              f"{looked_up - misses} known, {misses} not sold there")
    print(f"  filled {from_history} from purchase history, "
          f"{from_salling} from salling")
    return 0


# ---------------------------------------------------------------- entry


def main():
    global DRY_RUN
    ap = argparse.ArgumentParser()
    # "both" predates the prices job and is kept so existing schedules and
    # scripts keep meaning what they meant. "all" is everything.
    ap.add_argument("job", choices=["sync", "analyse", "prices", "both", "all"])
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan without writing to HA or Grocy")
    args = ap.parse_args()
    DRY_RUN = args.dry_run
    if DRY_RUN:
        print("*** DRY RUN — nothing will be written ***")

    rc = 0
    if args.job in ("sync", "both", "all"):
        rc |= job_sync()
    if args.job in ("analyse", "both", "all"):
        rc |= job_analyse()
    if args.job in ("prices", "all"):
        rc |= job_prices()
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.HTTPError as e:
        body = e.response.text[:200] if e.response is not None else ""
        print(f"HTTP {e.response.status_code if e.response is not None else '?'}: "
              f"{body}", file=sys.stderr)
        sys.exit(1)
    except requests.RequestException as e:
        print(f"network error after retries: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyError as e:
        print(f"missing expected field {e} — an upstream API shape changed; "
              f"re-run the verification in the module docstring", file=sys.stderr)
        sys.exit(1)
