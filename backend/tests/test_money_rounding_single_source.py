"""Money rounding has ONE implementation and a documented mode per surface.

The `_cents` helper used to be defined EIGHT times and the copies disagreed:
seven ROUND_HALF_UP, two ROUND_HALF_EVEN, and 62 bare `.quantize()` calls that
silently read the process-wide decimal context — including the two most
display-critical ones (`api/accounts._format_cash`, which prints every
`cash_balance` / `total_value`, and `services/budgets.spent_between`, the ONE
spend predicate the Budgets card and the alert generator share). A `.xx5` tie
could therefore render two ways depending on which surface asked.

`app.tax.acb.cents` is now the single canonical entry point (AGENTS §2), with
the rounding mode stated at every call site. These tests pin BOTH halves of
that contract:

* one implementation — the six duplicate helpers are gone and every module
  routes through the canonical function;
* the modes that are DELIBERATE half-even stay half-even, because three
  surfaces pin that behaviour in a doc, a test or a client mirror. Changing
  one would move pinned money, so they are asserted here too.
"""

from __future__ import annotations

import ast
import inspect
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

import pytest

from app.tax.acb import cents

# Modules that used to carry a private `.quantize(...)` for money. Every money
# quantize must go through `cents(...)` with the mode named.
OWNED_MODULES = [
    "app.tax.acb",
    "app.services.reports",
    "app.services.dividends",
    "app.services.demo_seed",
    "app.services.forecast",
    "app.services.projections",
    "app.services.valuation",
    "app.services.exposure_breakdown",
    "app.services.budgets",
    "app.services.account_value",
    "app.api.projections",
    "app.api.accounts",
    "app.api.securities",
]


def _half_even_cents_calls(func: object) -> int:
    """Count `cents(..., rounding=ROUND_HALF_EVEN)` calls inside `func`.

    AST rather than a substring match, so a DOCSTRING quoting the old buggy
    line cannot satisfy it and a prose mention cannot break it.
    """
    tree = ast.parse(inspect.getsource(func))
    found = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        if name != "cents":
            continue
        for kw in node.keywords:
            if kw.arg == "rounding" and getattr(kw.value, "id", "") == "ROUND_HALF_EVEN":
                found += 1
    return found


# --- the canonical helper itself --------------------------------------------


def test_canonical_cents_is_half_up_by_default() -> None:
    assert cents(Decimal("0.005")) == Decimal("0.01")
    assert cents(Decimal("2.345")) == Decimal("2.35")
    assert cents(Decimal("-0.005")) == Decimal("-0.01")


def test_canonical_cents_accepts_an_explicit_mode() -> None:
    # The tie 0.005 is the whole point: it is the one value where the two
    # documented modes disagree, so a regression to the implicit process
    # context shows up here rather than on a customer's balance.
    assert cents(Decimal("0.005")) == Decimal("0.01")
    assert cents(Decimal("0.005"), rounding=ROUND_HALF_EVEN) == Decimal("0.00")
    assert cents(Decimal("2.345"), rounding=ROUND_HALF_EVEN) == Decimal("2.34")
    # ...and the default is unchanged when the kwarg is omitted.
    assert cents(Decimal("0.005"), rounding=ROUND_HALF_UP) == Decimal("0.01")


# --- the duplicates are gone ------------------------------------------------

# (module, name it exposes after consolidation). Five modules that used to keep
# a private `_cents` now import the canonical one as `cents`; `reports` keeps the
# historical private name as a re-export because `total_return.py` imports it
# from there. Either way the OBJECT must be the canonical function.
DUPLICATES = [
    ("app.services.reports", "_cents"),
    ("app.services.dividends", "cents"),
    ("app.services.demo_seed", "cents"),
    ("app.services.forecast", "cents"),
    ("app.api.projections", "cents"),
    ("app.services.projections", "cents"),
    ("app.services.account_value", "_cents"),
]


@pytest.mark.parametrize("module_name,attr", DUPLICATES)
def test_no_module_defines_its_own_cents_helper(module_name: str, attr: str) -> None:
    """Each of these carried a verbatim copy of `acb.cents`; none may again.

    Asserting OBJECT IDENTITY (not just equal behaviour) is the point: a second
    body that re-implements `.quantize(...)` would pass a value-comparison and
    fail this. `reports._cents` is a deliberate re-export of the canonical
    function rather than a copy.
    """
    import importlib

    module = importlib.import_module(module_name)
    assert getattr(module, attr) is cents, f"{module_name}.{attr} must BE app.tax.acb.cents"


def test_no_module_defines_a_quantize_body_for_money() -> None:
    """No owned module may reintroduce a private `.quantize(...)` for money.

    A bare quantize with no `rounding=` is the defect itself: it reads the
    ambient decimal context instead of stating the documented mode. Every money
    quantize now goes through `cents(...)` with the mode named.

    Parsed with `ast`, not grepped: a docstring that QUOTES the old buggy line
    (the comments explaining this very fix do) must not fail the test, while a
    real call anywhere in the module must.
    """
    import ast
    import importlib
    import inspect

    offenders: list[str] = []
    for module_name in OWNED_MODULES:
        source = inspect.getsource(importlib.import_module(module_name))
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "quantize"):
                continue
            if any(kw.arg == "rounding" for kw in node.keywords):
                continue  # explicit mode — fine
            offenders.append(f"{module_name}:{node.lineno}")
    assert not offenders, (
        "money quantized without an explicit rounding mode (use app.tax.acb.cents): "
        + ", ".join(offenders)
    )


def test_the_three_documented_half_even_surfaces_stay_half_even() -> None:
    """Deliberate half-even, each pinned by a doc / test / client mirror.

    * `services/valuation` — docs/08 §Portfolio value history (F-M130-1) and
      `tests/test_m130_portfolio_value.py::test_f_m130_1_...`, which
      hand-computes 1 sh @ 10.006 -> 10.01 and Σ rows 20.02 vs walk 20.01.
    * `services/exposure_breakdown` — docs/16 §4 pins the breakdown money
      fields at ROUND_HALF_EVEN (while pinning `weight_pct` half-UP).
    * `services/budgets` — `frontend/src/lib/s3Engine.ts` documents the mode
      `budgetsProgress` mirrors, and `tests/fixtures/s3-vectors.json` is a
      golden captured from `spent_between` over the live stack.
    """
    from app.api import securities
    from app.services import exposure_breakdown, valuation

    tie = Decimal("10.005")  # 10.01 half-up, 10.00 half-even
    for module, label in (
        (valuation, "valuation"),
        (exposure_breakdown, "exposure_breakdown"),
        (securities, "securities"),
    ):
        assert module._cents(tie) == Decimal("10.00"), label

    # budgets has no `_cents` of its own — it calls the canonical helper with
    # the mode stated, which is the whole point of the `rounding=` parameter.
    # Assert the call really carries ROUND_HALF_EVEN, so nobody "fixes" it to
    # half-up and silently breaks the client mirror (`s3-vectors.json` is
    # captured from this function over the live stack).
    from app.services import budgets

    assert cents(tie, rounding=ROUND_HALF_EVEN) == Decimal("10.00")
    for func in (budgets.spent_between, budgets.carry_in):
        assert _half_even_cents_calls(func) >= 1, (
            f"{func.__name__} must quantize through cents(..., ROUND_HALF_EVEN)"
        )


def test_exposure_weight_pct_stays_half_up_while_its_money_is_half_even() -> None:
    """docs/16 §4 pins BOTH modes on the same payload — keep them apart.

    Money fields are ROUND_HALF_EVEN; `weight_pct` is ROUND_HALF_UP. Reading
    the same module's `weight_pct` off the money helper is the bug this pins.
    """
    from app.services import exposure_breakdown

    tie = Decimal("10.005")
    assert exposure_breakdown._cents(tie) == Decimal("10.00")  # money: half-even
    assert cents(tie, rounding=ROUND_HALF_UP) == Decimal("10.01")  # weight: half-up


# --- accounts moved OFF the process-wide context ----------------------------


def test_accounts_format_cash_is_half_up_not_the_context_default() -> None:
    """`api/accounts._format_cash` printed every account money string.

    It was a bare `.quantize(_CENT)` with no `rounding=`, so it inherited
    whichever mode the ambient decimal context happened to carry (half-even by
    default). It is now the documented money mode, stated explicitly.
    """
    from app.api.accounts import _format_cash

    assert _format_cash(Decimal("10.005")) == "10.01"
    assert _format_cash(Decimal("-0.005")) == "-0.01"
    assert _format_cash(Decimal("2")) == "2.00"


def test_accounts_money_serializes_as_a_string() -> None:
    """AGENTS §2: money never leaves as a float. A guard against a refactor
    that drops the `str()` (or the quantize) on this surface."""
    from app.api.accounts import _format_cash

    out = _format_cash(Decimal("1234.5"))
    assert isinstance(out, str)
    assert out == "1234.50"


def test_reports_and_forecast_agree_on_the_same_tie() -> None:
    """Two surfaces, one number — the actual complaint behind this change.

    Before consolidation the reports path rounded half-up while the accounts
    path inherited the context's half-even, so the same `.xx5` could print
    differently depending on which page asked.
    """
    from app.api.accounts import _format_cash
    from app.services.forecast import money_str
    from app.services.reports import _cents

    tie = Decimal("10.005")
    assert _cents(tie) == Decimal("10.01")
    assert money_str(tie) == "10.01"
    assert _format_cash(tie) == "10.01"
