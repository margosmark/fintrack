"""Cross-surface reconciliation gate for the dividend calendar's row-currency
export (t_67b48642, docs/24 §M29 denomination ruling, docs/08
`GET /reports/dividend-calendar`).

The defect class this file refuses: a `format=csv` twin that drops the
denomination column, or a subtotal strip that cannot be rebuilt from the rows
under it, lets a spreadsheet print ONE figure across CAD and USD — the exact
mis-statement docs/24 retires ("retain the summary strip as per-currency
subtotals, never one cross-currency number").

Every expectation here is RE-DERIVED from the exported rows plus the documented
period predicates (docs/24 "This ruling changes denomination, NOT schedule
arithmetic or period membership"), never by calling the service a second time:
two calls to the same function agree by construction and prove nothing about a
reconciliation. The one place the two sides may legitimately differ is the
bucket rounding order the same doc pins — "sum unrounded means within each
bucket, then quantize once" versus the per-entry quantized mean a row exports —
so that comparison carries a stated drift bound of half a cent per contributing
row and a fixture that lands exactly on it.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.accounts import Account, AccountType
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.securities import Security
from app.models.trades import Trade, TradeAction
from app.services.txn_filters import today_et

API = "/api/v1"
CALENDAR = f"{API}/reports/dividend-calendar"
CASHFLOW_CALENDAR = f"{API}/reports/cashflow-calendar"

#: The exported headers, pinned to the field names the route emits, in the
#: docs/24 §M29 wire-contract order.
EXPECTED_FIELDS = [
    "security_id",
    "symbol",
    "frequency",
    "currency",
    "avg_amount",
    "annual_projection",
    "last_payment_date",
    "next_expected_date",
    "num_payments",
]

#: The documented bucket rounding allowance: the subtotal sums UNROUNDED means
#: and quantizes once, while each exported row carries its own quantized mean
#: (docs/24 "Preserve existing Decimal precision and half-even cent
#: quantization"). Two decimal places on N rows can therefore disagree with the
#: bucket by at most half a cent each.
BUCKET_DRIFT_PER_ROW = Decimal("0.005")


def _today() -> dt.date:
    """The clock, read at CALL time — never captured at import (AGENTS §6)."""
    return today_et()


def _auth(client: TestClient) -> tuple[dict[str, str], int]:
    from datetime import UTC, datetime

    email = f"divcalx-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        f"{API}/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200, body.text
    headers = {"Authorization": f"Bearer {body.json()['access_token']}"}
    user_id = client.get(f"{API}/users/me", headers=headers).json()["id"]
    return headers, user_id


def _account(session: Session, user_id: int, *, currency: str) -> Account:
    account = Account(
        user_id=user_id,
        name=f"Acct {user_id} {currency}",
        type=AccountType.brokerage_non_registered,
        currency=currency,
        taxable=True,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _security(session: Session, user_id: int, *, symbol: str, listing: str) -> Security:
    security = Security(
        user_id=user_id,
        symbol=symbol,
        name=symbol,
        kind="etf",
        listing_currency=listing,
        exchange="NYSE",
    )
    session.add(security)
    session.commit()
    session.refresh(security)
    return security


def _dividend(
    session: Session,
    user_id: int,
    *,
    account: Account,
    security: Security,
    day: dt.date,
    amount: str,
    currency: str | None = None,
    batch: ImportBatch | None = None,
) -> Trade:
    """One dividend payment. `currency` is the ROW's own denomination (M132):
    `None` is the legacy shape the demo seed still writes, resolved from the
    account at read time and never from the listing currency."""
    trade = Trade(
        user_id=user_id,
        account_id=account.id,
        security_id=security.id,
        action=TradeAction.dividend,
        date=day,
        quantity=Decimal("0"),
        price_native=Decimal(amount),
        currency=currency,
        commission=Decimal("0"),
        import_batch_id=None if batch is None else batch.id,
    )
    session.add(trade)
    session.commit()
    session.refresh(trade)
    return trade


def _payments(
    session: Session,
    user_id: int,
    *,
    account: Account,
    security: Security,
    amounts: list[str],
    offsets: list[int],
    currency: str | None = None,
    batch: ImportBatch | None = None,
) -> None:
    today = _today()
    assert len(amounts) == len(offsets)
    for amount, offset in zip(amounts, offsets, strict=True):
        _dividend(
            session,
            user_id,
            account=account,
            security=security,
            day=today - dt.timedelta(days=offset),
            amount=amount,
            currency=currency,
            batch=batch,
        )


def _seed(session: Session, client: TestClient) -> dict[str, Any]:
    """One ledger whose every shape the reconciliation has to survive:

    * ``CADQ`` — CAD row evidence on a CAD account, exact two-decimal mean;
    * ``USDM`` — legacy NULL row currency on a USD account (the demo seed's
      shape): resolves USD from the ACCOUNT, never from the listing currency;
    * ``MIXQ`` — CAD + USD rows on one security: the pair is withheld;
    * ``RAT``/``RAT2`` — means that do not terminate at two decimals, so the
      bucket's sum-then-quantize and the rows' per-entry rounding genuinely
      disagree and the drift bound is exercised rather than assumed;
    * ``FARQ`` — a GBP security whose next payment is 240 days back, so its
      currency owns a bucket that is zero-filled in the current month;
    * ``STAGED`` — a dividend inside a pending import batch, invisible to a
      live projection (docs/03) and therefore to the export;
    * a SECOND tenant whose dividend must never reach this tenant's export.
    """
    headers, user_id = _auth(client)
    cad = _account(session, user_id, currency="CAD")
    usd = _account(session, user_id, currency="USD")
    gbp = _account(session, user_id, currency="GBP")

    cadq = _security(session, user_id, symbol="CADQ", listing="CAD")
    usdm = _security(session, user_id, symbol="USDM", listing="CAD")  # listing is a decoy
    mixq = _security(session, user_id, symbol="MIXQ", listing="USD")
    rat = _security(session, user_id, symbol="RAT", listing="CAD")
    rat2 = _security(session, user_id, symbol="RAT2", listing="CAD")
    farq = _security(session, user_id, symbol="FARQ", listing="GBP")

    _payments(
        session,
        user_id,
        account=cad,
        security=cadq,
        amounts=["118.40", "118.40"],
        offsets=[60, 30],
        currency="CAD",
    )
    _payments(
        session,
        user_id,
        account=usd,
        security=usdm,
        amounts=["85.00", "85.00"],
        offsets=[60, 30],
        currency=None,
    )
    _payments(
        session,
        user_id,
        account=cad,
        security=mixq,
        amounts=["12.00", "12.00"],
        offsets=[60, 30],
        currency="CAD",
    )
    _dividend(
        session,
        user_id,
        account=cad,
        security=mixq,
        day=_today() - dt.timedelta(days=30),
        amount="12.00",
        currency="USD",
    )
    _payments(
        session,
        user_id,
        account=cad,
        security=rat,
        amounts=["1.00", "1.00", "2.00"],
        offsets=[90, 60, 30],
        currency="CAD",
    )
    _payments(
        session,
        user_id,
        account=cad,
        security=rat2,
        amounts=["2.00", "2.00", "3.00"],
        offsets=[90, 60, 30],
        currency="CAD",
    )
    _payments(
        session,
        user_id,
        account=gbp,
        security=farq,
        amounts=["7.00", "7.00"],
        offsets=[300, 270],
        currency="GBP",
    )

    staged_account = _account(session, user_id, currency="CAD")
    batch = ImportBatch(
        user_id=user_id,
        account_id=staged_account.id,
        institution="investment_csv",
        file_name="staged.csv",
        status=ImportBatchStatus.pending_review,
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    staged = _security(session, user_id, symbol="STAGED", listing="CAD")
    _payments(
        session,
        user_id,
        account=staged_account,
        security=staged,
        amounts=["999.00", "999.00"],
        offsets=[60, 30],
        currency="CAD",
        batch=batch,
    )

    other_headers, other_user_id = _auth(client)
    other_account = _account(session, other_user_id, currency="CAD")
    other_security = _security(session, other_user_id, symbol="OTHER", listing="CAD")
    _payments(
        session,
        other_user_id,
        account=other_account,
        security=other_security,
        amounts=["500.00", "500.00"],
        offsets=[60, 30],
        currency="CAD",
    )

    return {
        "headers": headers,
        "other_headers": other_headers,
        "symbols": {"CADQ", "USDM", "MIXQ", "RAT", "RAT2", "FARQ"},
        "withheld": {"MIXQ"},
    }


def _json(client: TestClient, headers: dict[str, str]) -> dict:
    response = client.get(CALENDAR, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _csv(client: TestClient, headers: dict[str, str]):
    response = client.get(CALENDAR, headers=headers, params={"format": "csv"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv"), response.headers
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="dividend-calendar-'), disposition
    assert disposition.endswith('.csv"'), disposition
    reader = csv.DictReader(io.StringIO(response.text))
    return list(reader.fieldnames or []), list(reader)


def _eligible(next_expected: str, today: dt.date, period: str) -> bool:
    """The docs/24 period predicates, re-implemented here on purpose.

    `this_quarter` keeps the legacy `(next - today).days <= 90` with NO lower
    bound — a next date in the past is inside it — because that is what the
    shipped schedule arithmetic does and this gate reconciles against the
    contract, not against a tidier idea of it.
    """
    day = dt.date.fromisoformat(next_expected)
    if period == "this_month":
        return day.month == today.month and day.year == today.year
    if period == "this_quarter":
        return (day - today).days <= 90
    return day.year == today.year


PERIODS = ("this_month", "this_quarter", "this_year")


def test_the_export_states_the_row_currency_and_matches_the_json_rows(
    client: TestClient, session: Session
) -> None:
    """The CSV twin is the JSON `upcoming` list, cell for cell — money as the
    same two-decimal strings, `currency` on every row that carries a figure,
    and a withheld projection emptying ALL THREE of its money cells together.

    Staged rows and the other tenant's rows are absent from both surfaces: an
    export is not a second population.
    """
    seeded = _seed(session, client)
    headers = seeded["headers"]
    body = _json(client, headers)
    fields, rows = _csv(client, headers)

    assert fields == EXPECTED_FIELDS
    entries = body["upcoming"]
    assert {entry["symbol"] for entry in entries} == seeded["symbols"]
    assert len(rows) == len(entries)

    by_symbol = {row["symbol"]: row for row in rows}
    for entry in entries:
        row = by_symbol[entry["symbol"]]
        assert int(row["security_id"]) == entry["security_id"]
        assert row["frequency"] == entry["frequency"]
        assert row["last_payment_date"] == entry["last_payment_date"]
        assert row["next_expected_date"] == entry["next_expected_date"]
        assert int(row["num_payments"]) == entry["num_payments"]
        for money_field in ("avg_amount", "annual_projection"):
            expected = entry[money_field]
            if expected is None:
                # Withheld means EMPTY, never a formatted stand-in: the cell
                # must hold no figure at all (`Decimal(None)` is a TypeError,
                # which is exactly why there is nothing to compare here).
                assert row[money_field] == "", (entry["symbol"], row)
            else:
                assert row[money_field] == f"{Decimal(expected):.2f}", (entry["symbol"], row)
                # Two decimal places, always: `85` is not the money string the
                # JSON contract puts on the wire.
                assert len(row[money_field].split(".")[-1]) == 2, row
        # All-or-nothing (docs/24 "Row authority and refusal"): a currency cell
        # is present exactly when the money cells are.
        stated = [bool(row[field]) for field in ("currency", "avg_amount", "annual_projection")]
        assert len(set(stated)) == 1, row
        if entry["currency"] is None:
            assert row["currency"] == "", row
        else:
            assert row["currency"] == entry["currency"], row

    # The withheld security keeps its non-money fields (docs/24: "its
    # dates/count remain").
    withheld_row = by_symbol["MIXQ"]
    assert withheld_row["frequency"] == "monthly"
    assert withheld_row["num_payments"] == "3"
    assert withheld_row["next_expected_date"] == _today().isoformat()

    # The demo-shaped NULL row currency resolves from the ACCOUNT (USD), and the
    # CAD-listed decoy does not override it.
    assert by_symbol["USDM"]["currency"] == "USD"
    assert by_symbol["USDM"]["avg_amount"] == "85.00"

    assert "STAGED" not in by_symbol, "a pending import row reached the export"
    assert "OTHER" not in by_symbol, "another tenant's dividend reached the export"


def test_the_summary_subtotals_reconcile_with_the_exported_rows(
    client: TestClient, session: Session
) -> None:
    """THE GATE: every subtotal in `summary.by_currency` is reproducible from
    the exported rows alone — same currency set, same eligibility, same money
    within the documented rounding allowance — and `unresolved_counts` is
    exactly the withheld rows that were eligible.

    Nothing here reads a total that the rows cannot rebuild: a bucket with no
    eligible row is `0.00`, a currency with no eligible row in the current
    month still owns its zero-filled bucket, and the legacy scalar keys stay
    null so no client can print one blended figure.
    """
    seeded = _seed(session, client)
    headers = seeded["headers"]
    body = _json(client, headers)
    _fields, rows = _csv(client, headers)
    today = _today()

    summary = body["summary"]
    assert summary["this_month"] is None
    assert summary["this_quarter"] is None
    assert summary["this_year"] is None

    codes = {row["currency"] for row in rows if row["currency"]}
    buckets = {bucket["currency"]: bucket for bucket in summary["by_currency"]}
    assert set(buckets) == codes, "a bucket with no exported row, or a row with no bucket"

    for code in sorted(codes):
        for period in PERIODS:
            contributing = [
                Decimal(row["avg_amount"])
                for row in rows
                if row["currency"] == code and _eligible(row["next_expected_date"], today, period)
            ]
            subtotal = Decimal(buckets[code][period])
            expected = sum(contributing, Decimal("0"))
            drift = BUCKET_DRIFT_PER_ROW * len(contributing)
            assert abs(subtotal - expected) <= drift, (code, period, subtotal, expected)
            if not contributing:
                # Zero-filled, never an invented figure (docs/24).
                assert subtotal == Decimal("0.00"), (code, period)
            assert len(str(buckets[code][period]).split(".")[-1]) == 2, buckets[code]

    # The unresolved counts are per-period counts of ELIGIBLE withheld
    # projections — not of historical trades, and not of withheld rows that
    # fall outside the window.
    for period in PERIODS:
        withheld = [
            row
            for row in rows
            if not row["currency"] and _eligible(row["next_expected_date"], today, period)
        ]
        assert summary["unresolved_counts"][period] == len(withheld), period

    # The GBP security's next payment is 240 days back: outside this month, so
    # its bucket must be zero here while still existing.
    assert Decimal(buckets["GBP"]["this_month"]) == Decimal("0.00")

    # The withheld security is counted once per eligible period, and its money
    # never reaches a bucket.
    assert summary["unresolved_counts"] == {"this_month": 1, "this_quarter": 1, "this_year": 1}
    # Exact, hand-derived from the seed: CADQ 118.40 + RAT (1+1+2)/3 +
    # RAT2 (2+2+3)/3 = 122.0666… quantized once (docs/24 sum-then-quantize),
    # with MIXQ's two 12.00 CAD rows withheld and STAGED not history — so a
    # bucket that silently absorbed either one cannot land on this figure.
    assert Decimal(buckets["CAD"]["this_quarter"]) == Decimal("122.07")
    assert Decimal(buckets["USD"]["this_quarter"]) == Decimal("85.00")


def test_the_cashflow_calendar_states_the_same_pair_as_the_export(
    client: TestClient, session: Session
) -> None:
    """The third surface: `GET /reports/cashflow-calendar`'s dividend event is
    the SAME `(amount, currency)` pair the export row carries, including the
    withheld `null`/`null` (docs/36 §Currency, docs/24 verification item 5).

    Tying the exported CELLS to the other surface — not two service calls — is
    what makes this a reconciliation: a future refactor that re-derives either
    side independently fails here instead of disagreeing on a screen.
    """
    seeded = _seed(session, client)
    headers = seeded["headers"]
    _fields, rows = _csv(client, headers)

    months = sorted({row["next_expected_date"][:7] for row in rows})
    events: dict[str, tuple[str | None, str | None]] = {}
    for month in months:
        response = client.get(CASHFLOW_CALENDAR, headers=headers, params={"month": month})
        assert response.status_code == 200, response.text
        for day in response.json()["days"]:
            for event in day["events"]:
                if event["kind"] == "dividend":
                    events[event["label"]] = (event["amount"], event["currency"])

    for row in rows:
        label = f"{row['symbol']} dividend"
        assert label in events, label
        amount, currency = events[label]
        expected_amount = row["avg_amount"] or None
        assert amount == expected_amount, (label, amount, expected_amount)
        assert currency == (row["currency"] or None), (label, currency, row["currency"])


def test_both_formats_refuse_fx_reads_and_unsupported_scope(
    client: TestClient, session: Session, monkeypatch
) -> None:
    """Being currency-preserving is not converting (docs/36 decision 6): the
    export answers with the rate book's readers patched to raise, exactly like
    its JSON twin. `format` is validated like every other report twin, and the
    M179 own-only scope contract holds on the CSV surface too (422 at
    `query.scope`, never a narrower silent population).
    """
    seeded = _seed(session, client)
    headers = seeded["headers"]

    from app.services import fx as fx_service

    def forbid_fx(*args, **kwargs):
        raise AssertionError("the dividend calendar and its export must not consult FX")

    monkeypatch.setattr(fx_service, "cached_cad_amount", forbid_fx)
    monkeypatch.setattr(fx_service, "latest_rate", forbid_fx)

    assert client.get(CALENDAR, headers=headers).status_code == 200
    csv_response = client.get(CALENDAR, headers=headers, params={"format": "csv"})
    assert csv_response.status_code == 200, csv_response.text
    assert "CADQ" in csv_response.text

    bad_format = client.get(CALENDAR, headers=headers, params={"format": "xml"})
    assert bad_format.status_code == 422, bad_format.text
    assert bad_format.json()["code"] == "validation_error"
    # Located at the parameter itself, like every other report twin's refusal.
    assert any(error.get("loc") == ["query", "format"] for error in bad_format.json()["detail"]), (
        bad_format.json()["detail"]
    )

    for surface in (CALENDAR, CASHFLOW_CALENDAR):
        widened = client.get(
            surface,
            headers=headers,
            params={"format": "csv", "scope": "household"} if surface == CALENDAR else {},
        )
        if surface == CALENDAR:
            assert widened.status_code == 422, widened.text
            errors = widened.json()["detail"]
            assert any(error.get("loc") == ["query", "scope"] for error in errors), errors

    unauthenticated = client.get(CALENDAR, params={"format": "csv"})
    assert unauthenticated.status_code == 401
