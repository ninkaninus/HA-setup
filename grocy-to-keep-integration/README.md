# grocy_lists — Grocy → Home Assistant shopping list

Grocy's shopping list does not track min-stock state:
`POST /stock/shoppinglist/add-missing-products` is a one-shot snapshot with no
counterpart that removes rows once stock recovers. This worker ignores Grocy's
shopping list entirely and treats **"products where `amount < min_stock_amount`"**
as desired state, converging the household's Home Assistant list onto it.

## Jobs

| Job | Cadence | Writes to |
|---|---|---|
| `sync` | every ~15 min | `todo.indkob` — the **shared** household shopping list |
| `analyse` | weekly | `todo.grocy_forslag` — suggested `min_stock_amount` per product; and `default_best_before_days` in Grocy |
| `prices` | weekly | `product_barcodes.last_price` in Grocy, where it is empty |

`min_stock_amount` is written to Grocy **only** when a suggestion is ticked.
Deriving a suggestion never changes Grocy by itself.

`default_best_before_days` is the deliberate exception: it is written without
approval, so that scanning a barcode at purchase pre-fills a sensible
best-before date. See [Automatic expiry defaults](#automatic-expiry-defaults).

Google Keep support was removed: the account is in Google's Advanced Protection
Program, which permanently disables the App Passwords `gkeepapi` needs. The
household uses the HA list and the Companion app's to-do widget instead.

**Setup instructions — the phone widget and the unRAID cron — are in [SETUP.md](SETUP.md).**

### What `analyse` considers

Over `LOOKBACK_DAYS` (365) it derives a reorder point per product — mean demand
per shopping cycle plus `SAFETY_K × σ` — for products with at least
`MIN_EVENTS` (4) consume events. It then asks a second question: **does that
quantity actually get eaten before it goes off?**

Shelf life is *observed* per product from consume rows
(`best_before_date - purchased_date`), because `default_best_before_days` is
set on only 5 of 109 products. Grocy stores sentinel year-9999 dates for
non-perishables, so anything over `SHELF_SANE_MAX_DAYS` is treated as
"does not meaningfully expire" rather than as a real number.

The verdict is driven by the per-unit fact `used_date > best_before_date` —
what fraction of units were actually consumed past their date. Median hold time
versus median shelf life can look fine while individual units still go off, so
the medians only ever *explain* a flag, never raise one.

| Outcome | Meaning |
|---|---|
| `· bruges 29d, holder 557d` | fine — used well inside its shelf life |
| `⚠ 75% over dato` | routinely eaten past its date; suggestion capped at what fits one shelf life |
| `⚠ holdbarhed 121d` | the minimum would cover longer than the product keeps |
| `✗ køb v. behov` | a single unit outlives its own date at this rate — suggestion is 0, don't hold stock |

Example from the live data: *Økologisk æblejuice* — 75% of units consumed after
their best-before date, so its minimum is capped rather than raised.

Flags are written at the **front** of the annotation, immediately after the
separator. The HA todo-list card truncates each row to one line on a phone, so
anything appended to the end is precisely what gets cut off — which was the
warnings.

## Automatic expiry defaults

Grocy pre-fills the best-before date at purchase from the product's
`default_best_before_days`. It was set on 5 of 109 products, so scanning a
barcode mostly offered today's date. `analyse` now writes that field from the
same observed shelf life it already computes.

This is the only thing the worker changes in Grocy unattended. The justification
is that the date on the package is read at purchase anyway, so a wrong default
costs a correction rather than a spoiled product — which means the bar has to
live in the data instead of in a human. It writes only when:

| Condition | Default | Why |
|---|---|---|
| at least `MIN_SHELF_OBS_WRITE` observations | 4 | double the bar for a suggestion, which someone eyeballs |
| observations agree — MAD/median ≤ `SHELF_REL_SPREAD` | 0.4 | 3, 5, 300, 400 days describes no single product |
| current value is not `-1` | | `-1` is Grocy's "never expires", said deliberately |
| differs from an existing value by > `EXPIRY_CHANGE_THRESHOLD` | 0.3 | a hand-set 30 against an observed 32 isn't worth a write |

The median is **rounded down** — the opposite of `min_stock_amount`, which
rounds up. Both round towards the safe error: an extra packet on the shelf, and
a warning that comes early rather than late.

**The feedback loop is real and worth knowing about.** Once the field is set,
Grocy offers that date at purchase; accepting it makes the next observation echo
our own value rather than the package. What breaks the loop is someone reading
the physical date — the same habit that makes writing without approval
acceptable. It's also why the median is taken over the whole window rather than
the most recent few.

Set `SET_DEFAULT_EXPIRY=0` to turn it off. `analyse --dry-run` prints every
change it would make.

## Barcode prices

Grocy pre-fills the price at purchase from `product_barcodes.last_price`. All
159 barcodes had it empty, so even prices typed by hand were never coming back
at the till. `prices` fills the empty ones from two **exact-keyed** sources:

| Source | Key | Coverage |
|---|---|---|
| Your own purchase history | product id | 31 barcodes across 17 products |
| Salling Products EAN API | EAN | ~40% of the rest, measured at Bilka Tilst |

`GET /v2/products/{ean}?storeId=<uuid>` — barcode in, shelf price out, so a
price can never be attached to the wrong product. Salling is asked only about
barcodes your own history can't answer. No token, no store id, a 404 (that
store doesn't stock it), or an API failure just skips that barcode.

**Prices are per store.** `SALLING_STORE_ID` takes a comma-separated list and
rotates it by ISO week, so two shops alternate odd and even weeks. That widens
coverage as well as being fair to both: a barcode the small Netto doesn't carry
may be found at the Bilka a fortnight later, and since only empty prices are
filled, the ranges accumulate instead of fighting. Measured on six known-good
barcodes — Netto Bjæverskov 5/6, Bilka Waves 6/6, with real price differences
between them (flormelis 10,50 vs 10,95).

Two things keep the scarce quota useful:

- **Other chains' own brands are skipped.** 26 of the 92 unpriced barcodes are
  REMA or Coop private label and can never be at Salling. `SALLING_SKIP_PREFIXES`
  holds those GS1 company prefixes.
- **Each week starts where the last left off.** The quota covers only part of
  the list, and a miss leaves the barcode empty — so without rotating, the same
  first 90 would be re-asked every week and the rest never looked up at all.

The response nests the price under `instore`, alongside a `unitPrice` that is
the comparison price per litre or kilo: 23,75/l for a tin of coconut milk that
costs 9,50. Only `price` is read — see `extract_price`, which is pinned to the
verified shape by tests.

**The quota shapes the schedule.** 100 requests per day, one per barcode, so
`SALLING_MAX_LOOKUPS` is 90 and the job runs weekly: two passes to cover ~128
unpriced barcodes, then only the misses are re-asked. A 429 aborts the rest of
the run rather than hammering. Access must also be **requested** in the portal
— signing up doesn't grant it.

Its response schema isn't published, so `extract_price` looks for a price under
any of several plausible field names and returns nothing when it recognises
none — a schema change degrades to "skip", never to a wrong number.

It only ever fills **empty** prices, so it cannot overwrite what the household
typed, and it is safe to run repeatedly.

### Why there is no name matching here

REMA publishes a full catalogue — 3850 products, live shelf prices,
unauthenticated — but **no GTIN and no server-side search**, so the only way in
is matching product names. Measured against this Grocy: about 11 of 24 REMA
private-label products matched correctly, and the *wrong* answers scored as
high as the right ones. `Chokolade mysli` matched M&M'S chocolate at 46,95;
`Ferskner i lage` matched a bag of bread.

Constraining on brand and size (from Open Food Facts) fixed the scoring — but
OFF has no record for **82 of the 92** unpriced barcodes, so there is nothing
to constrain with. Two products out of 92 survived end to end.

That is the whole reason this job is exact-key only. A wrong expiry date gets
caught, because the package is read at purchase. A wrong price is invisible —
nobody re-checks a number that is already filled in.

## Approving a suggestion — one click

The `Grocy` dashboard in Home Assistant (`/dashboard-grocy/forslag`) shows the
suggestions as a todo list. **Ticking a row is the approval.** On its next run
the worker reads back the completed rows, writes the suggested
`min_stock_amount` to Grocy, and removes the row. Nothing reaches Grocy until
you tick — up to a 15-minute delay, matching the sync cadence.

The target value is parsed out of the row text, which this file owns and
writes, so what gets applied is exactly the number that was on screen when you
ticked it — not a value re-derived later that may have drifted. Rows carry no
`description`: the todo card renders descriptions under every row, so stashing
a machine-readable payload there put raw JSON on the dashboard.

`apply_approved()` is the only place the worker writes to Grocy, and it runs
**only** against `SUGGEST_LIST` — ticking something on the shopping list means
"bought it", never "change Grocy".

## Run

Locally, against the live Grocy and HA:

```bash
cp .env.example .env      # fill in GROCY_API_KEY and HA_TOKEN
docker build -t grocy-lists .

# always do this first after any config change
docker run --rm --env-file .env grocy-lists sync --dry-run

docker run --rm --env-file .env grocy-lists sync
```

On unRAID nothing is built by hand: CI publishes
`ghcr.io/ninkaninus/ha-setup/grocy-lists:sha-<commit>` and a deploy agent pulls
it. See [`../deploy/UNRAID.md`](../deploy/UNRAID.md).

## Tests

```bash
pip install -r requirements.txt pytest
pytest tests -q
```

They cover the pure parts — formatting, row ownership, the reconcile plan and
the suggestion round-trip — because that is where a one-character slip starts
deleting hand-added rows on a 15-minute timer. They are also the CI gate: if
they fail, no image is published and the server has nothing to deploy.

`--dry-run` prints the full add/rename/remove plan and writes nothing.

The worker is **stateless** — no volume needed. All state lives in Grocy and
Home Assistant.

Exit codes: `0` success · `1` fatal (HTTP, network, unexpected API shape).

## Scheduling — unRAID User Scripts

`sync` every 15 minutes, `analyse` Mondays at 06:00. The scripts in `deploy/`
are thin wrappers around `../deploy/run-unit.sh`, which runs whichever image
version the deploy agent last verified — see
[`../deploy/UNRAID.md`](../deploy/UNRAID.md) and [SETUP.md](SETUP.md) part 2.

Do *not* also drive it from an HA `shell_command` — two schedulers on one
worker is how you get overlapping runs.

## Invariants — do not break these

- **The shopping list is SHARED, not owned.** The worker only ever touches rows
  matching its own shape (`name — have/min unit` for stock rows,
  `... min N → M ...` for suggestions). Everything else is invisible to the
  reconciler: never renamed, never removed. This is what lets the household add
  their own items to the same list.
- **Product name is the join key**, stripped of surrounding whitespace.
  `key_of()` splits on `SEP` (`" — "`); a product name containing that exact
  sequence breaks the key, and the script warns on stderr if one appears.
- **Checked items count as present.** `ha_items()` requests both statuses. If
  she ticks something off before the purchase is booked, re-adding it would be
  the automation arguing with her.
- **Rename in place, never delete-and-re-add.** Re-adding sends the row to the
  bottom of her list on every stock change, and loses its ticked state.
- **Adds land before removes.** `reconcile()` computes the whole plan first,
  then applies adds and renames, and only then removals — so a mid-run failure
  leaves extra items rather than missing ones.
- **Only ticking changes a minimum.** Deriving a suggestion never changes
  `min_stock_amount`; `apply_approved()` is its single write path and runs only
  against the suggestions list.
- **`default_best_before_days` is the one unattended write**, and it must stay
  the only one. Adding a second silent write path is how this stops being
  predictable — every other change to Grocy is a thing the household did.

## Do not

- Do not add quantity prefixes to item text (`2 x Mælk`). Quantity in the
  identity string means every partial consumption churns the list.
- Do not use Grocy's own shopping list as an intermediate.

## Verification status (2026-08-12)

Verified against the live Grocy 4.6.0 and Home Assistant 2026.6.3. Full record
is in the module docstring of `grocy_lists.py`. Headlines:

- All Grocy field names in the draft were **correct**, including that spoilage
  is a `spoiled` 0/1 flag on `consume` rows rather than its own
  `transaction_type`.
- `stock_log` server-side filtering and `limit`/`offset` paging both verified:
  7 pages × 50 returned 322 unique ids, exactly matching the unpaged count.
- **Fixed a churn bug.** 48 of 109 product names carry trailing whitespace. The
  desired-set key used the raw name while `reconcile()` derived its key with
  `.strip()`, so they never matched — every run re-added the item and removed
  the old row, forever — a visible flicker on her list every 15 minutes.
  Now stripped at catalogue time; runs 2 and 3 are verified no-ops.
- Grocy is behind **Cloudflare**, which 403s a `Python-urllib` user agent.
  `requests` is fine, and the script now sends an explicit UA so this cannot
  resurface as a mystery 403.
- `todo.get_items` returns completed items when `status` is omitted, despite
  `services.yaml` declaring a `needs_action` default — those defaults are UI
  hints, not applied to API calls. Passed explicitly anyway.

## Caveat worth stating plainly

All of this is downstream of purchases actually being booked into Grocy. If
groceries enter the house unrecorded, stock stays below minimum, the item never
leaves the list, and the household learns to ignore it. Barcode purchase flow on
a phone is what makes the model hold — the fix for a noisy first few weeks is
process, not code.

Related: with 60 consume rows across 22 products in the last 90 days, only 5
products currently clear `MIN_EVENTS=4`, yielding 3 suggestions. `analyse` gets
more useful as consumption booking becomes habitual.

## Whole numbers only

Suggested minimums are always integers, rounded **up** — you cannot keep 0,3 of
a packet on a shelf, and up is the safe direction for a minimum. This also cut
the suggestion list from 22 rows to 13, because every "↓ 0,4" collapsed to 1,
which usually equals the current minimum and so stops being a suggestion at all.

A minimum that is *already* a decimal is surfaced regardless of
`CHANGE_THRESHOLD`. Without that, a product sitting at 1,7 is only 0,3 away
from 2, would never clear the 25% bar, and would keep its decimal forever.
