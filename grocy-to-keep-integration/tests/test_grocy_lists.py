"""Tests for the parts of grocy_lists that can silently ruin someone's day.

These are the gate CI deploys behind, so they are aimed at the invariants in
README.md rather than at coverage. The one that matters most is ownership:
the shopping list is SHARED, and a predicate that drifts by one character
starts deleting hand-added rows on a 15-minute timer.
"""

from datetime import datetime

import pytest

import grocy_lists as g


# --------------------------------------------------------------- formatting


@pytest.mark.parametrize("value,expected", [
    (1, "1"),
    (1.0, "1"),
    (0, "0"),
    (2.0, "2"),
    (0.5, "0,5"),
    (1.5, "1,5"),
    (12.5, "12,5"),
])
def test_fmt_whole_numbers_lose_the_decimal(value, expected):
    """Danish decimal comma, and no trailing ",0" — the row text is the
    identity string, so "1" and "1,0" churning back and forth would rename
    the row on every run."""
    assert g.fmt(value) == expected


def test_key_of_strips_surrounding_whitespace():
    """48 of 109 Grocy product names carry trailing whitespace. When the
    desired-set key kept the raw name and reconcile() derived its key with
    .strip(), the two never matched and every run re-added the item and
    removed the old row — a visible flicker on her list every 15 minutes."""
    assert g.key_of("Mælk — 1/3 Liter") == "Mælk"
    assert g.key_of("Mælk ") == "Mælk"
    assert g.key_of(" Grøn pesto — 1/2 Glas") == "Grøn pesto"


def test_key_of_survives_a_row_with_no_annotation():
    assert g.key_of("Blomster til bordet") == "Blomster til bordet"


# ---------------------------------------------------------------- ownership

WORKER_STOCK_ROWS = [
    "Mælk — 1/3 Liter",
    "Grøn pesto — 1/2 Glas",
    "Havregryn — 0/1 Pakke",
    "Fløde — 0,5/1 Liter",     # decimal stock level
    "Salt — 0/1",              # product with no quantity unit
]

WORKER_SUGGESTION_ROWS = [
    "Mælk — min 1 ↑ 2 Liter · 1,4/uge",
    "Havregryn — min 2 ↓ 1 Pakke · 0,4/uge · bruges 29d, holder 557d",
    "Kaffe — min 1 → 1 Pakke · 0,9/uge",
    "Økologisk æblejuice — ⚠ 75% over dato · min 2 ↓ 1 Stk · 0,3/uge",
    "Kardemomme — ✗ køb v. behov, 80% over dato · min 1 ↓ 0 Glas · 0,1/uge",
]

HOUSEHOLD_ROWS = [
    "Blomster til bordet",
    "Mælk",
    "Ring til tandlægen",
    "2 x rundstykker",
    "Batterier - AA",          # hyphen, not the em-dash separator
]


@pytest.mark.parametrize("row", WORKER_STOCK_ROWS)
def test_worker_claims_its_own_stock_rows(row):
    assert g.owns_stock_item(row)


@pytest.mark.parametrize("row", HOUSEHOLD_ROWS)
def test_hand_added_rows_are_never_claimed(row):
    """The invariant the shared list rests on. If this fails, the reconciler
    has started treating her items as its own, and it will remove them."""
    assert not g.owns_stock_item(row)
    assert not g.owns_suggestion(row)


@pytest.mark.parametrize("row", WORKER_SUGGESTION_ROWS)
def test_worker_claims_its_own_suggestion_rows(row):
    assert g.owns_suggestion(row)


@pytest.mark.parametrize("row", WORKER_SUGGESTION_ROWS)
def test_a_suggestion_row_is_not_mistaken_for_a_stock_row(row):
    """sync reconciles the shopping list and analyse the suggestions list.
    A predicate that claimed both would let one job delete the other's rows
    if the two lists were ever pointed at the same entity."""
    assert not g.owns_stock_item(row)


@pytest.mark.parametrize("row", WORKER_STOCK_ROWS)
def test_a_stock_row_is_not_mistaken_for_a_suggestion(row):
    assert not g.owns_suggestion(row)


def test_a_hand_added_row_in_the_workers_shape_is_the_known_hole():
    """Documented, not fixed: a manual row that happens to look exactly like
    the worker's output is claimed. The em-dash makes it unlikely by hand.
    This test exists so the hole stays a decision rather than a surprise."""
    assert g.owns_stock_item("Noget hun skrev — 1/2 Liter")


# ------------------------------------------------------------ suggestion parse


@pytest.mark.parametrize("row,expected", [
    ("Mælk — min 1 ↑ 2 Liter · 1,4/uge", 2.0),
    ("Havregryn — min 2 ↓ 1 Pakke · 0,4/uge", 1.0),
    ("Kaffe — min 1 → 1 Pakke · 0,9/uge", 1.0),
    ("Økologisk æblejuice — ⚠ 75% over dato · min 2 ↓ 1 Stk · 0,3/uge", 1.0),
    ("Kardemomme — ✗ køb v. behov, 80% over dato · min 1 ↓ 0 Glas", 0.0),
    ("Fløde — min 1,5 ↑ 2 Liter", 2.0),
])
def test_the_approved_value_is_read_back_off_the_row(row, expected):
    """apply_approved parses the target out of the row text, so what reaches
    Grocy is the number that was on screen when it was ticked. The flagged
    rows matter here: the verdict sits BEFORE the "min N → M" and contains
    its own digits and percent signs."""
    m = g.SUGGEST_RE.search(row.split(g.SEP, 1)[1])
    assert m is not None
    assert float(m.group(1).replace(",", ".")) == expected


def test_an_unparsable_row_yields_no_match_rather_than_a_wrong_number():
    """A hand-edited row must fail closed. apply_approved skips on no-match;
    the failure mode to avoid is matching something and writing a wrong
    min_stock_amount into Grocy."""
    assert g.SUGGEST_RE.search("Mælk — min noget til 2 Liter") is None


# ------------------------------------------------ default_best_before_days
#
# This is the one write path that runs unattended, so the tests are about
# when it must REFUSE to write. Writing nothing is always safe; writing a
# number derived from thin or contradictory data silently pre-fills every
# future purchase of that product.


def test_a_clear_shelf_life_is_written_when_the_field_is_unset():
    assert g.shelf_default_for([30, 32, 29, 31], current=0) == 30


def test_the_median_is_rounded_down():
    """Opposite of min_stock_amount's rounding up. Both round towards the
    safe error: a best-before warning that comes early, not late."""
    assert g.shelf_default_for([10, 10, 11, 11], current=0) == 10


def test_too_few_observations_writes_nothing():
    """MIN_SHELF_OBS_WRITE is 4 — higher than the 2 that gates a suggestion,
    because nobody eyeballs this one."""
    assert g.shelf_default_for([30, 30, 30], current=0) is None


def test_observations_that_disagree_write_nothing():
    """Two clusters, 4 days apart and 350 days apart, describe no single
    product. Any number here would be worse than an empty field."""
    assert g.shelf_default_for([3, 5, 300, 400], current=0) is None


def test_a_lone_wild_value_among_a_tight_cluster_still_writes():
    """Documenting the edge of the spread guard rather than pretending it
    isn't there: 3/5/8 agree closely enough that the 400 reads as a mistyped
    sentinel, so this writes 6. That is the intended trade — the alternative
    is one bad date vetoing an otherwise clear product."""
    assert g.shelf_default_for([3, 5, 8, 400], current=0) == 6


def test_a_single_outlier_does_not_veto_a_consistent_product():
    """MAD rather than stdev: one mis-typed date shouldn't block a product
    whose other observations agree."""
    assert g.shelf_default_for([30, 31, 30, 29, 300], current=0) == 30


def test_never_expires_is_left_alone():
    """-1 is Grocy's 'never overdue'. Someone said that deliberately."""
    assert g.shelf_default_for([30, 30, 30, 30], current=-1) is None


def test_an_existing_value_that_is_roughly_right_is_left_alone():
    """A hand-set 30 against an observed 32 is not worth a write."""
    assert g.shelf_default_for([32, 32, 33, 31], current=30) is None


def test_an_existing_value_that_is_badly_wrong_is_corrected():
    assert g.shelf_default_for([300, 305, 295, 302], current=30) == 301


def test_a_sub_day_shelf_life_writes_nothing():
    """Rounding down would give 0, which Grocy reads as 'not set' — so the
    write would be a no-op that looks like a decision."""
    assert g.shelf_default_for([0.5, 0.6, 0.4, 0.5], current=0) is None


def test_writing_is_skipped_entirely_when_disabled(monkeypatch):
    monkeypatch.setattr(g, "SET_DEFAULT_EXPIRY", False)
    calls = []
    monkeypatch.setattr(g, "grocy_put", lambda *a, **k: calls.append(a))

    written = g.apply_shelf_defaults(
        {1: {"name": "Mælk", "bbd": 0}}, {1: [30, 30, 31, 29]})

    assert written == 0
    assert calls == []


def test_a_dry_run_writes_nothing(monkeypatch, capsys):
    monkeypatch.setattr(g, "SET_DEFAULT_EXPIRY", True)
    monkeypatch.setattr(g, "DRY_RUN", True)
    calls = []
    monkeypatch.setattr(g, "grocy_put", lambda *a, **k: calls.append(a))

    written = g.apply_shelf_defaults(
        {1: {"name": "Mælk", "bbd": 0}}, {1: [30, 30, 31, 29]})

    assert written == 1
    assert calls == []
    assert "would set default_best_before_days=30" in capsys.readouterr().out


def test_only_the_expiry_field_is_written(monkeypatch):
    """The PUT must not carry min_stock_amount along with it — that field has
    an approval gate, and this path deliberately has none."""
    monkeypatch.setattr(g, "SET_DEFAULT_EXPIRY", True)
    monkeypatch.setattr(g, "DRY_RUN", False)
    calls = []
    monkeypatch.setattr(g, "grocy_put", lambda path, body: calls.append((path, body)))

    g.apply_shelf_defaults({7: {"name": "Mælk", "bbd": 0}}, {7: [30, 30, 31, 29]})

    assert calls == [("objects/products/7", {"default_best_before_days": 30})]


def test_a_product_missing_from_the_catalogue_is_skipped(monkeypatch):
    """Deactivated products are dropped by product_catalogue but can still
    appear in the consumption log."""
    monkeypatch.setattr(g, "SET_DEFAULT_EXPIRY", True)
    monkeypatch.setattr(g, "DRY_RUN", False)
    monkeypatch.setattr(g, "grocy_put", lambda *a, **k: pytest.fail("wrote"))

    assert g.apply_shelf_defaults({}, {99: [30, 30, 31, 29]}) == 0


# ------------------------------------------------------------ barcode prices


def test_the_most_recent_priced_purchase_wins():
    """A price is a pre-fill for the next purchase, so the last thing it cost
    beats an average dragged down by two-year-old prices."""
    rows = [
        {"product_id": "1", "price": "12.50", "row_created_timestamp": "2026-01-05 10:00:00"},
        {"product_id": "1", "price": "14.95", "row_created_timestamp": "2026-06-05 10:00:00"},
        {"product_id": "2", "price": "8.00", "row_created_timestamp": "2026-03-05 10:00:00"},
    ]
    assert g.latest_purchase_price(rows) == {1: 14.95, 2: 8.00}


@pytest.mark.parametrize("row", [
    {"product_id": "1", "price": None, "row_created_timestamp": "2026-01-05 10:00:00"},
    {"product_id": "1", "price": "", "row_created_timestamp": "2026-01-05 10:00:00"},
    {"product_id": "1", "price": "0", "row_created_timestamp": "2026-01-05 10:00:00"},
    {"product_id": "1", "price": "gratis", "row_created_timestamp": "2026-01-05 10:00:00"},
    {"price": "5.00", "row_created_timestamp": "2026-01-05 10:00:00"},
])
def test_purchases_without_a_usable_price_are_ignored(row):
    """Most purchases are booked without a price — 217 of 279 — so the empty
    cases are the common path, not the edge case."""
    assert g.latest_purchase_price([row]) == {}


# extract_price is pinned to the REAL response shape, captured from the live
# API on 2026-08-13. The first version of this code guessed the schema and
# would have returned None for every product — these tests exist so that can
# never silently happen again.

LIVE_RESPONSE = {
    "instore": {
        "contents": 400, "contentsUnit": "ml", "description": "ASIA KITCHEN",
        "ean": "5710405090951", "name": "KOKOSMÆLK", "price": 9.5,
        "unit": "l", "unitPrice": 23.75,
    },
    "webshop": None,
}


def test_the_live_response_shape_yields_the_shelf_price():
    assert g.extract_price(LIVE_RESPONSE) == 9.5


def test_the_comparison_unit_price_is_never_used():
    """unitPrice is per litre or kilo — 23,75/l for a 9,50 tin. Reaching for
    it would overstate every product sold in less than its comparison unit."""
    assert g.extract_price({"instore": {"unitPrice": 23.75}}) is None


def test_webshop_is_the_fallback_when_there_is_no_instore_price():
    payload = {"instore": None, "webshop": {"ean": "1", "price": 12.0}}
    assert g.extract_price(payload) == 12.0


def test_instore_wins_over_webshop():
    """The point of this field is what the till shows when the barcode is
    scanned, which is the in-store price."""
    payload = {"instore": {"price": 9.5}, "webshop": {"price": 12.0}}
    assert g.extract_price(payload) == 9.5


@pytest.mark.parametrize("payload", [
    {}, [], None, "nope", 42,
    {"instore": None, "webshop": None},          # known at Salling, no price
    {"instore": {"ean": "1", "name": "X"}},      # no price field
    {"instore": {"price": None}},
    {"instore": {"price": 0}},
    {"instore": {"price": -5}},
    {"instore": {"price": "gratis"}},
    {"instore": "not a dict"},
    {"unexpected": {"price": 9.5}},              # a shape we do not recognise
])
def test_an_unrecognised_shape_yields_no_price_rather_than_a_guess(payload):
    """A schema change must degrade to "skip this barcode", never to a wrong
    number in Grocy."""
    assert g.extract_price(payload) is None


def test_a_zero_price_is_not_treated_as_a_price():
    """Grocy reads 0 as "unset", so writing it would be a no-op dressed up as
    a decision — and would stop the barcode being retried next run."""
    assert g.extract_price({"instore": {"price": 0}}) is None


NETTO = "8093199f-aff0-46a1-8c93-324c396ab124"
BILKA = "c6744248-cf95-43c3-aa43-d3daef2aeb4b"


def test_two_stores_alternate_by_odd_and_even_week():
    assert g.pick_store([NETTO, BILKA], week=34) == NETTO
    assert g.pick_store([NETTO, BILKA], week=35) == BILKA
    assert g.pick_store([NETTO, BILKA], week=36) == NETTO


def test_the_alternation_holds_across_the_year_boundary():
    """ISO weeks run to 52 or 53. A 53-week year makes week 53 and week 1 pick
    the same store — harmless, since only empty prices are ever filled, but
    worth knowing it is a skipped turn rather than a bug."""
    assert g.pick_store([NETTO, BILKA], week=53) == BILKA
    assert g.pick_store([NETTO, BILKA], week=1) == BILKA


def test_one_store_is_used_every_week():
    assert g.pick_store([NETTO], week=34) == NETTO
    assert g.pick_store([NETTO], week=35) == NETTO


def test_no_stores_means_the_salling_half_is_skipped():
    assert g.pick_store([], week=34) == ""


@pytest.mark.parametrize("barcode,expected", [
    ("5710405090951", True),      # a brand Salling might carry
    ("8710604750950", True),
    ("5705830017093", False),     # REMA 1000 own brand
    ("5705001412108", False),     # ØGO, Coop own brand
    ("", False),
])
def test_other_chains_own_brands_are_not_worth_a_lookup(barcode, expected):
    """Purely a quota saving — 26 of 92 barcodes here are another chain's
    private label and can never be found at Salling. Being wrong about one
    costs a skipped lookup, never a wrong price."""
    assert g.worth_asking(barcode) is expected


def test_each_week_asks_about_a_different_slice():
    """The bug this prevents: the quota only covers part of the list, and a
    miss leaves the barcode empty, so starting from the top every week would
    re-ask the same first 90 forever and never reach the rest."""
    items = list(range(10))
    assert g.rotate(items, week=0, window=4)[0] == 0
    assert g.rotate(items, week=1, window=4)[0] == 4
    assert g.rotate(items, week=2, window=4)[0] == 8


def test_rotation_wraps_and_keeps_every_item():
    items = list(range(10))
    for week in range(12):
        assert sorted(g.rotate(items, week, window=4)) == items


@pytest.mark.parametrize("items,window", [([], 4), ([1, 2], 0)])
def test_rotation_handles_nothing_to_do(items, window):
    assert g.rotate(items, week=3, window=window) == items


# ------------------------------------------------------- known-miss tracking

TODAY = datetime(2026, 8, 13)


@pytest.mark.parametrize("value,expected", [
    ("3:2026-08-13", (3, datetime(2026, 8, 13))),
    ("0:2026-08-13", (0, datetime(2026, 8, 13))),
    ("2", (2, None)),
])
def test_a_probe_marker_round_trips(value, expected):
    assert g.parse_probe(value) == expected


@pytest.mark.parametrize("value", [None, "", "nonsense", ":", "x:2026-08-13", 42, {}])
def test_an_unreadable_marker_fails_open(value):
    """Fail open on purpose: a corrupt marker costs one extra lookup, while
    failing closed would silently retire a barcode for good."""
    assert g.parse_probe(value) == (0, None)
    assert g.should_ask(value, TODAY) is True


def test_a_barcode_under_the_miss_limit_is_still_asked():
    assert g.should_ask("3:2026-08-10", TODAY) is True


def test_a_barcode_at_the_miss_limit_drops_out_of_the_rotation():
    """Four denials is Salling saying it doesn't stock it. Continuing to ask
    spends a quota of 100/day on a guaranteed miss."""
    assert g.should_ask("4:2026-08-10", TODAY) is False
    assert g.should_ask("9:2026-08-10", TODAY) is False


def test_a_retired_barcode_is_retried_after_a_year():
    """Ranges change. "Not sold here" is a fact about today, not forever."""
    assert g.should_ask("4:2025-08-14", TODAY) is False   # 364 days
    assert g.should_ask("4:2025-08-13", TODAY) is True    # 365 days
    assert g.should_ask("9:2024-01-01", TODAY) is True


def test_a_retired_barcode_with_no_date_is_asked_again():
    """No date means the marker predates this scheme or was hand-edited;
    asking once is cheaper than retiring it on evidence we can't read."""
    assert g.should_ask("7", TODAY) is True


# --------------------------------------------------------------- reconcile


@pytest.fixture
def list_contents(monkeypatch):
    """Point ha_items at a fixed list without touching the network."""
    def _set(items):
        monkeypatch.setattr(
            g, "ha_items",
            lambda entity: [{"summary": s, "status": "needs_action"} for s in items],
        )
    return _set


def test_plan_adds_renames_and_removes(list_contents):
    list_contents([
        "Mælk — 1/3 Liter",        # stock changed -> rename
        "Kaffe — 0/1 Pakke",       # recovered -> remove
        "Blomster til bordet",     # hers -> invisible
    ])
    desired = {
        "Mælk": "Mælk — 2/3 Liter",
        "Havregryn": "Havregryn — 0/1 Pakke",
    }

    adds, renames, removes, _ = g.plan("todo.indkob", desired, g.owns_stock_item)

    assert adds == [("Havregryn", "Havregryn — 0/1 Pakke")]
    assert renames == [("Mælk", "Mælk — 1/3 Liter", "Mælk — 2/3 Liter")]
    assert removes == ["Kaffe — 0/1 Pakke"]


def test_hand_added_rows_never_appear_in_a_removal(list_contents):
    """The load-bearing one. Nothing she wrote may ever reach the remove
    list, no matter what the desired set says."""
    list_contents(HOUSEHOLD_ROWS + ["Mælk — 1/3 Liter"])

    adds, renames, removes, _ = g.plan("todo.indkob", {}, g.owns_stock_item)

    assert removes == ["Mælk — 1/3 Liter"]
    assert adds == []
    assert renames == []
    for row in HOUSEHOLD_ROWS:
        assert row not in removes


def test_a_converged_list_is_a_no_op(list_contents):
    """Run 2 and 3 must do nothing. A plan that is non-empty on an unchanged
    list is the churn bug coming back."""
    list_contents(["Mælk — 1/3 Liter", "Blomster til bordet"])

    adds, renames, removes, _ = g.plan(
        "todo.indkob", {"Mælk": "Mælk — 1/3 Liter"}, g.owns_stock_item)

    assert (adds, renames, removes) == ([], [], [])


def test_a_ticked_row_still_counts_as_present(monkeypatch):
    """If she ticks something off before the purchase is booked in Grocy,
    re-adding it would be the automation arguing with her."""
    monkeypatch.setattr(g, "ha_items", lambda entity: [
        {"summary": "Mælk — 1/3 Liter", "status": "completed"},
    ])

    adds, _, _, _ = g.plan(
        "todo.indkob", {"Mælk": "Mælk — 1/3 Liter"}, g.owns_stock_item)

    assert adds == []


# ------------------------------------------------- price sub-line on the list


def test_money_always_keeps_its_oere():
    """fmt() drops trailing zeros, which is right for quantities and wrong for
    money: "9,5 kr" reads as a typo."""
    assert g.kr(9.5) == "9,50"
    assert g.kr(10) == "10,00"
    assert g.kr(12.345) == "12,35"


def test_stores_are_parsed_with_their_names():
    assert g.parse_stores("Netto=aaa,Bilka=bbb") == [("Netto", "aaa"), ("Bilka", "bbb")]


def test_a_bare_uuid_still_works():
    """An older single-store config must not break on the new format."""
    assert g.parse_stores("aaa") == [("Salling", "aaa")]
    assert g.parse_stores("") == []


def test_shelf_prices_round_trip():
    prices = {"Netto": 9.5, "Bilka": 9.95}
    encoded = g.format_shelf(prices, datetime(2026, 8, 15))
    assert g.parse_shelf(encoded) == (prices, datetime(2026, 8, 15))


def test_an_empty_shelf_record_still_carries_its_date():
    """Written even when no store stocks the product, so it is not re-priced
    on every run — the date is what makes "nobody has it" a cached answer."""
    prices, when = g.parse_shelf(g.format_shelf({}, datetime(2026, 8, 15)))
    assert prices == {}
    assert when == datetime(2026, 8, 15)


@pytest.mark.parametrize("value", [None, "", "garbage", "Netto=9.50", 42])
def test_an_unreadable_shelf_record_is_treated_as_missing(value):
    assert g.parse_shelf(value) == ({}, None)


def test_the_cheapest_store_comes_first():
    """The line answers "where do I buy this", so the answer is first."""
    line = g.price_line({"Bilka": 9.95, "Netto": 9.50, "Føtex": 10.25})
    assert line == "Netto 9,50 · Bilka 9,95 · Føtex 10,25"


def test_only_the_configured_number_of_stores_is_shown():
    line = g.price_line({"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0}, limit=3)
    assert line == "A 1,00 · B 2,00 · C 3,00"


def test_no_prices_means_no_sub_line():
    """An empty description clears the row's sub-line rather than writing
    something like "ingen priser", which would be noise on every row."""
    assert g.price_line({}) == ""


def test_a_changed_price_updates_the_sub_line(monkeypatch):
    monkeypatch.setattr(g, "ha_items", lambda entity: [
        {"summary": "Mælk — 1/3 Liter", "status": "needs_action",
         "description": "Netto 9,50"},
    ])
    _, _, _, redescribes = g.plan(
        "todo.indkob", {"Mælk": "Mælk — 1/3 Liter"}, g.owns_stock_item,
        {"Mælk": "Netto 8,95 · Bilka 9,95"})
    assert redescribes == [("Mælk — 1/3 Liter", "Netto 8,95 · Bilka 9,95")]


def test_an_unchanged_sub_line_is_not_rewritten(monkeypatch):
    """Otherwise every 15-minute run would rewrite every description."""
    monkeypatch.setattr(g, "ha_items", lambda entity: [
        {"summary": "Mælk — 1/3 Liter", "status": "needs_action",
         "description": "Netto 9,50"},
    ])
    _, _, _, redescribes = g.plan(
        "todo.indkob", {"Mælk": "Mælk — 1/3 Liter"}, g.owns_stock_item,
        {"Mælk": "Netto 9,50"})
    assert redescribes == []


def test_hand_added_rows_get_no_sub_line_even_if_named(monkeypatch):
    """A note keyed to something the worker does not own must never reach the
    list — the household's rows stay exactly as they wrote them."""
    monkeypatch.setattr(g, "ha_items", lambda entity: [
        {"summary": "Blomster til bordet", "status": "needs_action",
         "description": ""},
    ])
    adds, renames, removes, redescribes = g.plan(
        "todo.indkob", {}, g.owns_stock_item,
        {"Blomster til bordet": "Netto 9,50"})
    assert (adds, renames, removes, redescribes) == ([], [], [], [])


def test_an_item_with_no_description_key_at_all_is_handled(monkeypatch):
    """Verified against HA 2026.6.3: todo.get_items returns "description": ""
    on some items and omits the key entirely on others, in the same response.
    Treating the missing key as anything but empty would rewrite that row's
    sub-line on every 15-minute run, forever."""
    monkeypatch.setattr(g, "ha_items", lambda entity: [
        {"summary": "Mælk — 1/3 Liter", "status": "needs_action"},   # no key
    ])
    _, _, _, redescribes = g.plan(
        "todo.indkob", {"Mælk": "Mælk — 1/3 Liter"}, g.owns_stock_item,
        {"Mælk": ""})
    assert redescribes == []


# ------------------------------------------------------------------- tilbud

OFFERS = [
    {"dealer": "REMA 1000", "heading": "Havredrik", "price": 12.0,
     "until": datetime(2026, 8, 20)},
    {"dealer": "Lidl", "heading": "Havre drik", "price": 10.0,
     "until": datetime(2026, 8, 20)},
    {"dealer": "Netto", "heading": "Chokolade", "price": 5.0,
     "until": datetime(2026, 8, 20)},
]


def test_only_near_identical_names_match():
    """The whole reason offers are allowed to use name matching: the threshold
    is high enough that most offers match nothing and are dropped. The earlier
    price attempt always took the best of 3844 and so could never say no."""
    found = match_names = g.match_offers("Havre drik", OFFERS, limit=5)
    assert [o["dealer"] for o in found] == ["Lidl", "REMA 1000"]
    assert all(o["heading"] != "Chokolade" for o in found)


def test_a_product_with_no_matching_offer_gets_nothing():
    assert g.match_offers("Tandpasta", OFFERS) == []
    assert g.match_offers("", OFFERS) == []


def test_offers_are_cheapest_first_and_one_per_dealer():
    duplicates = OFFERS + [{"dealer": "Lidl", "heading": "Havredrik",
                            "price": 11.0, "until": datetime(2026, 8, 20)}]
    found = g.match_offers("Havredrik", duplicates, limit=5)
    dealers = [o["dealer"] for o in found]
    assert dealers == ["Lidl", "REMA 1000"]      # 10,00 before 12,00
    assert dealers.count("Lidl") == 1            # not twice


def test_the_offer_line_is_marked_and_dated():
    line = g.offer_line(OFFERS[:2], TODAY)
    assert line.startswith("⚡ ")
    assert "REMA 1000 12,00 til 20/8" in line
    assert "Lidl 10,00 til 20/8" in line


def test_an_expired_offer_is_dropped_not_shown():
    """A tilbud that ended is worse than no tilbud — it sends you to the shop
    for a price that is gone."""
    stale = [{"dealer": "Lidl", "heading": "Havredrik", "price": 10.0,
              "until": datetime(2026, 8, 1)}]
    assert g.offer_line(stale, TODAY) == ""


def test_an_offer_with_no_end_date_is_still_shown():
    open_ended = [{"dealer": "Lidl", "heading": "X", "price": 10.0, "until": None}]
    assert g.offer_line(open_ended, TODAY) == "⚡ Lidl 10,00"


def test_no_offers_means_no_marker():
    assert g.offer_line([], TODAY) == ""


def test_offers_round_trip_through_the_cache():
    encoded = g.format_offers(OFFERS[:2], datetime(2026, 8, 15))
    decoded, when = g.parse_offers(encoded)
    assert when == datetime(2026, 8, 15)
    assert [(o["dealer"], o["price"], o["until"]) for o in decoded] == [
        ("REMA 1000", 12.0, datetime(2026, 8, 20)),
        ("Lidl", 10.0, datetime(2026, 8, 20)),
    ]


@pytest.mark.parametrize("value", [None, "", "junk", 42, "Lidl=10.00"])
def test_an_unreadable_offer_cache_is_treated_as_missing(value):
    assert g.parse_offers(value) == ([], None)


def test_danish_letters_do_not_block_a_match():
    """"Havre drik" vs "HAVREDRIK", "Ærter" vs "aerter" — folding has to make
    those comparable or nothing matches."""
    assert g.norm_name("Havre drik") == g.norm_name("HAVREDRIK")
    assert g.norm_name("Ærter") == g.norm_name("aerter")
    assert g.norm_name("Grød-ris ") == g.norm_name("grødris")


def test_only_supermarkets_are_trusted_as_dealers():
    """Tjek indexes wholesalers and German border shops too. "Spagetti"
    matched AB Catering at 79,00 — a catering pack — and Fleggaard/Poetzsch
    are in Germany. An allowlist, so a new wholesaler is ignored by default
    rather than quietly trusted."""
    assert "rema 1000" in g.OFFER_DEALERS
    assert "kvickly" in g.OFFER_DEALERS        # Coop is included
    assert "superbrugsen" in g.OFFER_DEALERS
    assert "ab catering" not in g.OFFER_DEALERS
    assert "fleggaard" not in g.OFFER_DEALERS
