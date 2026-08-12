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
