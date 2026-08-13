"""Tests for the parts of grocy_lists that can silently ruin someone's day.

These are the gate CI deploys behind, so they are aimed at the invariants in
README.md rather than at coverage. The one that matters most is ownership:
the shopping list is SHARED, and a predicate that drifts by one character
starts deleting hand-added rows on a 15-minute timer.
"""

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


def test_salling_yields_the_original_price_not_the_markdown():
    """The markdown is what a nearly-expired unit costs today. Writing that in
    would make the whole shelf look cheaper than it is."""
    payload = [{
        "store": {"name": "Netto Testby"},
        "clearances": [{
            "offer": {"ean": "5705830017093", "originalPrice": 19.95,
                      "newPrice": 7.00, "currency": "DKK"},
            "product": {"ean": "5705830017093", "description": "Tun i vand"},
        }],
    }]
    assert g.parse_salling(payload) == {"5705830017093": 19.95}


def test_the_same_product_in_several_stores_is_reduced_to_the_median():
    payload = [
        {"clearances": [{"offer": {"ean": "1", "originalPrice": 10.0}}]},
        {"clearances": [{"offer": {"ean": "1", "originalPrice": 12.0}}]},
        {"clearances": [{"offer": {"ean": "1", "originalPrice": 11.0}}]},
    ]
    assert g.parse_salling(payload) == {"1": 11.0}


def test_the_ean_falls_back_to_the_product_when_the_offer_lacks_one():
    payload = [{"clearances": [
        {"offer": {"originalPrice": 5.0}, "product": {"ean": "5705830000001"}},
    ]}]
    assert g.parse_salling(payload) == {"5705830000001": 5.0}


@pytest.mark.parametrize("payload", [
    [],
    {},
    {"stores": []},
    [{"clearances": []}],
    [{"clearances": [{"offer": {"ean": "1"}}]}],                  # no price
    [{"clearances": [{"offer": {"ean": "", "originalPrice": 5}}]}],  # no ean
    [{"clearances": [{"offer": {"ean": "1", "originalPrice": "n/a"}}]}],
    [{"clearances": [{"offer": {"ean": "1", "originalPrice": 0}}]}],
    [{"clearances": ["nonsense"]}],
    ["nonsense"],
    {"unexpected": "shape"},
])
def test_an_unrecognised_salling_shape_yields_nothing_rather_than_guesses(payload):
    """This runs unattended against a loosely documented response. Returning
    nothing is always safe; guessing writes a wrong price into Grocy."""
    assert g.parse_salling(payload) == {}


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

    adds, renames, removes = g.plan("todo.indkob", desired, g.owns_stock_item)

    assert adds == [("Havregryn", "Havregryn — 0/1 Pakke")]
    assert renames == [("Mælk", "Mælk — 1/3 Liter", "Mælk — 2/3 Liter")]
    assert removes == ["Kaffe — 0/1 Pakke"]


def test_hand_added_rows_never_appear_in_a_removal(list_contents):
    """The load-bearing one. Nothing she wrote may ever reach the remove
    list, no matter what the desired set says."""
    list_contents(HOUSEHOLD_ROWS + ["Mælk — 1/3 Liter"])

    adds, renames, removes = g.plan("todo.indkob", {}, g.owns_stock_item)

    assert removes == ["Mælk — 1/3 Liter"]
    assert adds == []
    assert renames == []
    for row in HOUSEHOLD_ROWS:
        assert row not in removes


def test_a_converged_list_is_a_no_op(list_contents):
    """Run 2 and 3 must do nothing. A plan that is non-empty on an unchanged
    list is the churn bug coming back."""
    list_contents(["Mælk — 1/3 Liter", "Blomster til bordet"])

    adds, renames, removes = g.plan(
        "todo.indkob", {"Mælk": "Mælk — 1/3 Liter"}, g.owns_stock_item)

    assert (adds, renames, removes) == ([], [], [])


def test_a_ticked_row_still_counts_as_present(monkeypatch):
    """If she ticks something off before the purchase is booked in Grocy,
    re-adding it would be the automation arguing with her."""
    monkeypatch.setattr(g, "ha_items", lambda entity: [
        {"summary": "Mælk — 1/3 Liter", "status": "completed"},
    ])

    adds, _, _ = g.plan(
        "todo.indkob", {"Mælk": "Mælk — 1/3 Liter"}, g.owns_stock_item)

    assert adds == []
