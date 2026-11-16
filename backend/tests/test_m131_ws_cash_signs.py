"""M131 — Wealthsimple cash-row SIGNS (docs/02, docs/03, docs/05, docs/08, docs/41).

Mark's real TFSA import exposed three cash-row defects that the
M113 mapping alone could not catch, because they live in the SIGN and the
DESCRIPTION, not the (activity_type, activity_sub_type) key:

1. A ``MoneyMovement/EFT`` row whose description was ``"Withdrawal"`` imported
   as a NEGATIVE ``contribution`` (-24,318.76): the cash total survived (the
   effect is the signed amount) but contribution-room math, the value-history
   marker and every semantic read of the row lied. M131 disambiguates EFT by
   the DESCRIPTION (with the amount's sign as fallback) and refuses a negative
   contribution outright (422 ``negative_contribution``).

2. ``fee`` rows stored the file's amount as an ABSOLUTE value, so the
   transactions surface, exports and the event log rendered an inflationary
   ``+$145.75 fee``. M131 stores every cash OUTFLOW (fee/withdrawal) at its
   NEGATIVE native sign and makes the cash effect ``-abs(price_native)`` —
   always money out, for both the repaired negative rows and any legacy row a
   caller stored positive.

3. The value-history event marker for the January withdrawal now reads it as
   a ``withdrawal`` from the ACTION (not inferred from a negative contribution
   sign).

These tests pin the importer's per-row behaviour in BOTH parsers (the legacy
``parse_investment_csv`` the wizard falls back to for a Wealthsimple header,
and the opted-in ``wealthsimple`` profile), the endpoint 422s, the cash-effect
invariance, and the marker/direction flip. Four more rules that the sign alone
cannot express are pinned separately, because a mutation analysis of this file
showed each of them was silent (a reverted rule kept the whole suite green):

* the DESCRIPTION beats the amount's sign in both directions — a positive
  "Money transfer out of the account" is still money out, and a negative
  "Money transfer into the account" is still a (refused) contribution;
* a ``fee`` row is stored at its NEGATIVE native sign whichever way the file
  spelled the amount, and reads that way on GET /trades;
* ``PATCH /trades/{id}`` — the third write path docs/02 names — refuses a
  negative contribution, and the refused write does not land;
* the marker's kind comes from the ACTION, with the sign fallback still
  covering the pre-M131 negative-contribution rows in the database.
"""

from datetime import date
from decimal import Decimal
from time import time_ns

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.importers.brokerage_profiles import parse_brokerage_csv
from app.importers.investment_profile import parse_investment_csv
from app.models.accounts import Account, AccountType
from app.models.securities import Security, SecurityKind
from app.models.trades import Trade, TradeAction
from app.models.users import User
from app.services import market_data
from app.services.account_value import _flow_kind, account_value_history
from app.services.total_return import _trade_cash_effect

API = "/api/v1"
D = Decimal

_WS_HEADER = (
    "effective_date,effective_time,settlement_date,account_id,account_type,activity_type,"
    "activity_sub_type,description,direction,symbol,name,currency,quantity,unit_price,"
    "commission,net_cash_amount"
)


def _ws_row(
    when: str,
    sub: str,
    description: str,
    currency: str,
    quantity: str,
    net_cash: str,
    *,
    type_: str = "MoneyMovement",
) -> str:
    """One 16-field Wealthsimple activity row (money movement, no symbol).

    ``type_`` switches the ``activity_type`` cell — the default
    ``MoneyMovement`` row is the EFT shape; a plain fee row is ``Fee``.
    """
    return (
        f"{when},00:00:00,,ws-anon,TFSA,{type_},{sub},{description},,,,"
        f"{currency},{quantity},,,{net_cash}"
    )


#: An EFT "Withdrawal" (money OUT) and an EFT "Deposit" (money IN) — the two
#: directions that both arrive as (MoneyMovement, EFT) and that ACTIVITY_MAP
#: alone cannot tell apart. The withdrawal mirrors the real EFT "Withdrawal"
#: row (figures scrubbed to synthetic stand-ins, docs/60).
EFT_DIRECTION_CSV = (
    _WS_HEADER
    + "\n"
    + _ws_row("2026-01-14", "EFT", "Withdrawal", "USD", "-24318.76", "-24318.76")
    + "\n"
    + _ws_row("2026-01-12", "EFT", "Deposit", "USD", "12600", "12600")
    + "\n"
    # No description at all: the amount's SIGN decides (negative = out).
    + _ws_row("2026-02-01", "EFT", "", "CAD", "-250.00", "-250.00")
    + "\n"
    # A CONTRADICTION: description says Deposit but the amount is negative —
    # must be refused (422 negative_contribution), never stored.
    + _ws_row("2026-03-01", "EFT", "Deposit", "CAD", "-99.00", "-99.00")
    + "\n"
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(market_data, "lookup_symbol", lambda symbol, **kw: None)
    monkeypatch.setattr(market_data, "fetch_latest_close", lambda symbol, today=None: None)


def _rows(raw: bytes, parser: str) -> tuple[list, list]:
    if parser == "legacy":
        return parse_investment_csv(raw)
    outcome = parse_brokerage_csv(raw, "wealthsimple", account_currency="CAD")
    assert outcome is not None
    return outcome.rows, outcome.unmapped


def _trade_cash(action: TradeAction | str, quantity: str, price: str, commission: str = "0"):
    return Trade(
        user_id=1,
        account_id=1,
        security_id=1,
        action=TradeAction(action),
        date=date(2026, 1, 1),
        quantity=D(quantity),
        price_native=D(price),
        commission=D(commission),
    )


# --------------------------------------------------------------------------- #
# 1. EFT direction comes from the DESCRIPTION (both parsers)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("parser", ["legacy", "profile"])
def test_eft_withdrawal_description_is_a_negative_withdrawal(parser) -> None:
    """The -24,318.76 EFT "Withdrawal" must land as a `withdrawal` at a
    NEGATIVE price — not a negative `contribution`."""
    rows, unmapped = _rows(EFT_DIRECTION_CSV.encode(), parser)
    # The contradictory "Deposit"/negative row is refused; the other 3 import.
    assert len(unmapped) == 1
    assert unmapped[0].code == "negative_contribution"
    withdrawals = [r for r in rows if r.action == TradeAction.withdrawal]
    (row,) = [r for r in withdrawals if r.price_native == D("-24318.76")]
    assert row.quantity == 0
    assert row.symbol == ""
    # Cash effect is money out via -abs, matching the file's signed amount.
    effect = _trade_cash_effect(_trade_cash(row.action, str(row.quantity), str(row.price_native)))
    assert effect == D("-24318.76")


@pytest.mark.parametrize("parser", ["legacy", "profile"])
def test_eft_deposit_description_is_a_positive_contribution(parser) -> None:
    rows, _unmapped = _rows(EFT_DIRECTION_CSV.encode(), parser)
    (row,) = [r for r in rows if r.action == TradeAction.contribution]
    assert row.price_native == D("12600")
    assert row.price_native > 0
    assert _trade_cash_effect(
        _trade_cash(row.action, str(row.quantity), str(row.price_native))
    ) == D("12600")


@pytest.mark.parametrize("parser", ["legacy", "profile"])
def test_eft_without_description_falls_back_to_the_amount_sign(parser) -> None:
    """No description → the sign decides: -250 is money out (`withdrawal`)."""
    rows, _ = _rows(EFT_DIRECTION_CSV.encode(), parser)
    (row,) = [r for r in rows if r.price_native == D("-250.00")]
    assert row.action == TradeAction.withdrawal
    assert _trade_cash_effect(
        _trade_cash(row.action, str(row.quantity), str(row.price_native))
    ) == D("-250.00")


@pytest.mark.parametrize("parser", ["legacy", "profile"])
def test_negative_contribution_is_refused_not_stored(parser) -> None:
    """A "Deposit" whose amount is negative is a contradiction — refused with
    `negative_contribution`, never imported as a negative contribution."""
    rows, unmapped = _rows(EFT_DIRECTION_CSV.encode(), parser)
    assert not any(r.action == TradeAction.contribution and r.price_native < 0 for r in rows)
    (refused,) = [u for u in unmapped if u.code == "negative_contribution"]
    assert refused.activity_sub_type == "EFT"
    assert "negative" in refused.reason.lower()


# --------------------------------------------------------------------------- #
# 2. Endpoint: the import refuses the file, the manual path refuses the trade
# --------------------------------------------------------------------------- #


def _auth(client: TestClient) -> dict[str, str]:
    email = f"m131-{time_ns()}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        f"{API}/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _account(client: TestClient, headers: dict) -> dict:
    body = client.post(
        f"{API}/accounts",
        json={"name": "M131 TFSA", "type": "tfsa", "currency": "CAD"},
        headers=headers,
    )
    assert body.status_code == 201, body.text
    return body.json()


def test_import_endpoint_422s_on_a_negative_contribution(client: TestClient) -> None:
    """POST /trades/import answers 422 `negative_contribution` (not the generic
    `parse_error`) when a row is a contradiction."""
    headers = _auth(client)
    account = _account(client, headers)
    body = client.post(
        f"{API}/trades/import",
        files={"file": ("eft.csv", EFT_DIRECTION_CSV.encode(), "text/csv")},
        data={"account_id": account["id"]},
        headers=headers,
    )
    assert body.status_code == 422, body.text
    assert body.json()["code"] == "negative_contribution"


def test_manual_create_refuses_a_negative_contribution(client: TestClient) -> None:
    """POST /trades answers 422 `negative_contribution` — no write path can
    store the row class that corrupted the contribution math."""
    headers = _auth(client)
    account = _account(client, headers)
    security = client.post(
        f"{API}/securities",
        json={"symbol": "CASHM131", "name": "Cash", "kind": "stock", "listing_currency": "CAD"},
        headers=headers,
    )
    assert security.status_code == 201, security.text
    body = client.post(
        f"{API}/trades",
        json={
            "account_id": account["id"],
            "security_id": security.json()["id"],
            "action": "contribution",
            "date": "2026-01-01",
            "quantity": "0",
            "price_native": "-100.00",
            "commission": "0",
        },
        headers=headers,
    )
    assert body.status_code == 422, body.text
    assert body.json()["code"] == "negative_contribution"


def test_create_rejects_a_currency_field_instead_of_silently_discarding_it(
    client: TestClient,
) -> None:
    """QA nit (M132 acceptance): POST /trades deliberately records the
    ACCOUNT's currency (docs/02 — the create form carries no currency
    field), but the schema used to ACCEPT a `currency` key and drop it on
    the floor — an API consumer would read the 201 as "my currency was
    stored". `extra="forbid"` turns that into the honest answer: 422
    validation_error naming the unknown field. The clean create below is
    the control — the same payload minus the extra key still stores."""
    headers = _auth(client)
    account = _account(client, headers)
    security = client.post(
        f"{API}/securities",
        json={"symbol": "M132FX", "name": "FX Nit", "kind": "stock", "listing_currency": "CAD"},
        headers=headers,
    )
    assert security.status_code == 201, security.text
    base = {
        "account_id": account["id"],
        "security_id": security.json()["id"],
        "action": "contribution",
        "date": "2026-01-01",
        "quantity": "0",
        "price_native": "100.00",
        "commission": "0",
    }

    refused = client.post(f"{API}/trades", json={**base, "currency": "USD"}, headers=headers)
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "validation_error"
    # The rejected key is NAMED, so the consumer learns which field misled them.
    assert any("currency" in str(err.get("loc", "")) for err in refused.json()["detail"])

    stored = client.post(f"{API}/trades", json=base, headers=headers)
    assert stored.status_code == 201, stored.text
    # And the honest path still records the ACCOUNT's currency, not a guess.
    assert stored.json()["currency"] == account["currency"]


# --------------------------------------------------------------------------- #
# 3. fee rows: stored signed-negative, cash effect -abs regardless
# --------------------------------------------------------------------------- #


def test_fee_cash_effect_is_money_out_for_both_storage_signs() -> None:
    """`-abs(price_native)` is direction-safe: a repaired negative fee row AND
    a legacy positive one both read as money out (no silent credit)."""
    for stored in (D("-145.75"), D("145.75")):
        effect = _trade_cash_effect(_trade_cash(TradeAction.fee, "0", str(stored)))
        assert effect == D("-145.75")
        assert effect < 0
    # A withdrawal behaves the same.
    assert _trade_cash_effect(_trade_cash(TradeAction.withdrawal, "0", "-24318.76")) == D(
        "-24318.76"
    )


def test_importer_stores_a_positive_reimbursement_as_money_in() -> None:
    """A POSITIVE ``TRANSFER_FEE_REIMBURSEMENT`` is money IN, not a fee.

    M131 read the profile's ``fee`` mapping as final and stored the credit at
    the canonical money-out sign, which counted "CAD credited" +145.75 as 145.75
    LEAVING the account. On the account under test that single rule accounted
    for 583.00 of the -582.73-vs-Wealthsimple-0.27 cash gap (two rows), and no
    FX rate anywhere could close it: the row-by-row diff against the source
    file showed those two rows as the ONLY disagreement in the 91-row export.

    M132 therefore classifies a positive credit as a cash ``transfer_in``
    (docs/02 §trades — cash-reimbursement rows) — ``quantity`` 0, ``price_native`` the
    credit's own sign — so the ledger records the money arriving. A NEGATIVE
    reimbursement is still money out and keeps the ``fee`` mapping; a genuine
    fee is covered by the test above and is untouched.
    """
    raw = (
        _WS_HEADER + "\n" + "2026-01-17,00:00:00,,ws-anon,TFSA,AdministrativePayment,"
        "TRANSFER_FEE_REIMBURSEMENT,CAD credited,,,,CAD,145.75,,,145.75\n"
    ).encode()
    for parser in ("legacy", "profile"):
        rows, unmapped = _rows(raw, parser)
        assert unmapped == []
        (credit,) = rows
        assert credit.action == TradeAction.transfer_in
        assert credit.quantity == D("0")  # a CASH transfer, no shares (docs/02)
        assert credit.price_native == D("145.75")
        assert credit.price_native > 0
        assert credit.currency == "CAD"
        assert _trade_cash_effect(
            _trade_cash(credit.action, str(credit.quantity), str(credit.price_native))
        ) == D("145.75")

    # ... and a NEGATIVE reimbursement is still money out, via `fee`.
    negative = (
        _WS_HEADER + "\n" + "2026-01-17,00:00:00,,ws-anon,TFSA,AdministrativePayment,"
        "TRANSFER_FEE_REIMBURSEMENT,Fee charged,,,,CAD,-145.75,,,-145.75\n"
    ).encode()
    rows, unmapped = _rows(negative, "profile")
    assert unmapped == []
    (fee,) = [r for r in rows if r.action == TradeAction.fee]
    assert fee.price_native == D("-145.75")
    assert _trade_cash_effect(
        _trade_cash(fee.action, str(fee.quantity), str(fee.price_native))
    ) == D("-145.75")


# --------------------------------------------------------------------------- #
# 4. In-kind transfer cost basis is untouched (docs/06)
# --------------------------------------------------------------------------- #


def test_in_kind_transfer_keeps_per_share_cost_basis() -> None:
    """A SecurityTransfer buys quantity with the net_cash as COST BASIS
    (cost = net_cash / qty) — M131 must not disturb it."""
    raw = (
        _WS_HEADER + "\n" + "2026-01-15,00:00:00,,ws-anon,TFSA,SecurityTransfer,-,"
        "Tax-free transfer of 320.0000 shares into the account,,SYNA,"
        "Synthetic FTSE-like,CAD,320,,,13491.2\n"
    ).encode()
    for parser in ("legacy", "profile"):
        rows, unmapped = _rows(raw, parser)
        assert unmapped == []
        (row,) = rows
        assert row.action == TradeAction.transfer_in
        assert row.quantity == D("320")
        assert row.price_native == D("42.16")  # 13491.2 / 320
        assert row.quantity * row.price_native == D("13491.20")


# --------------------------------------------------------------------------- #
# 5. Value-history marker: the December event reads as a withdrawal (docs/08)
# --------------------------------------------------------------------------- #


def _cash_security(session: Session, user: User) -> Security:
    cash = Security(
        user_id=user.id,
        symbol="CASH",
        name="Cash / No Security",
        kind=SecurityKind.stock,
        listing_currency="CAD",
        exchange="CASH",
    )
    session.add(cash)
    session.commit()
    session.refresh(cash)
    return cash


def test_marker_annotates_a_withdrawal_action_as_money_leaving(session: Session) -> None:
    """A `withdrawal` trade row is money OUT: the marker's kind is `withdrawal`
    and its direction is -1 (the "-" marker). Before M131 the same event was a
    negative `contribution` whose kind was inferred from the sign."""
    user = User(email=f"m131-marker-{time_ns()}@example.com", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    account = Account(
        user_id=user.id,
        name="TFSA",
        type=AccountType.tfsa,
        currency="CAD",
        taxable=False,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    cash = _cash_security(session, user)

    session.add(
        Trade(
            user_id=user.id,
            account_id=account.id,
            security_id=cash.id,
            action=TradeAction.withdrawal,
            date=date(2026, 1, 14),
            quantity=D("0"),
            price_native=D("-24318.76"),
            commission=D("0"),
        )
    )
    session.commit()

    result = account_value_history(
        session, account, date(2025, 12, 1), date(2026, 1, 31), "day", D("1000")
    )
    events = result["events"]  # type: ignore[assignment]
    (event,) = [e for e in events if e["source"] == "trade"]  # type: ignore[index]
    assert event["kind"] == "withdrawal"
    assert event["direction"] == -1
    assert event["action"] == "withdrawal"
    assert event["amount_native"] == D("-24318.76")
    assert event["amount_cad"] == D("-24318.76")  # CAD account, rate 1


def test_contribution_room_no_longer_counts_a_withdrawal(session: Session) -> None:
    """The -24,318.76 row is a `withdrawal` now — it is NOT in the contribution
    population, so a contribution-room tracker that sums `contribution` trades
    can no longer be poisoned by it."""
    user = User(email=f"m131-room-{time_ns()}@example.com", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    account = Account(
        user_id=user.id, name="TFSA", type=AccountType.tfsa, currency="CAD", taxable=False
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    cash = _cash_security(session, user)

    session.add_all(
        [
            Trade(
                user_id=user.id,
                account_id=account.id,
                security_id=cash.id,
                action=TradeAction.contribution,
                date=date(2026, 1, 14),
                quantity=D("0"),
                price_native=D("12600"),
                commission=D("0"),
            ),
            Trade(
                user_id=user.id,
                account_id=account.id,
                security_id=cash.id,
                action=TradeAction.withdrawal,
                date=date(2026, 1, 14),
                quantity=D("0"),
                price_native=D("-24318.76"),
                commission=D("0"),
            ),
        ]
    )
    session.commit()

    contributions = [
        t
        for t in session.exec(select(Trade).where(Trade.account_id == account.id)).all()
        if t.action == TradeAction.contribution
    ]
    # Only the deposit is a contribution; the withdrawal is not counted.
    assert [c.price_native for c in contributions] == [D("12600")]
    assert all(c.price_native >= 0 for c in contributions)


# --------------------------------------------------------------------------- #
# 6. The DESCRIPTION decides even when the amount's SIGN says otherwise
#    (docs/03 §cash-direction keys — the sign is the FALLBACK, not the rule)
# --------------------------------------------------------------------------- #


EFT_WORD_VS_SIGN_CSV = (
    _WS_HEADER
    + "\n"
    # "Money transfer OUT" written with a POSITIVE amount: the word says money
    # left (docs/03), so this is money OUT — stored at the negative sign, never
    # a contribution just because the number is positive.
    + _ws_row("2026-04-01", "EFT", "Money transfer out of the account", "CAD", "", "400.00")
    + "\n"
    # "Money transfer INTO" written NEGATIVE: the word says money in, so the
    # row IS a contribution — and a NEGATIVE contribution is refused (422
    # `negative_contribution`), not quietly re-read as a withdrawal by the sign.
    + _ws_row("2026-04-02", "EFT", "Money transfer into the account", "CAD", "", "-400.00")
    + "\n"
    # A "Withdrawal" written POSITIVE — direction carried by the word alone.
    + _ws_row("2026-04-03", "EFT", "Withdrawal", "CAD", "", "24318.76")
    + "\n"
)


@pytest.mark.parametrize("parser", ["legacy", "profile"])
def test_eft_description_beats_the_amount_sign(parser) -> None:
    """Every row here has a WORD and a NUMBER that disagree, in both
    directions. Sign-only logic (the fallback promoted to the rule) would
    import all three as-is: a positive "transfer out" as a contribution, a
    negative "transfer into" as a withdrawal, and would never refuse the
    contradiction — the silent corruption M131 exists to prevent."""
    rows, unmapped = _rows(EFT_WORD_VS_SIGN_CSV.encode(), parser)
    (refused,) = [u for u in unmapped if u.code == "negative_contribution"]
    assert refused.activity_sub_type == "EFT"
    assert "Contribution amount -400.00 is negative" in refused.reason
    # Exactly one row refused, the two honest outflows stored NEGATIVE.
    assert len(unmapped) == 1
    assert {(r.action, r.price_native) for r in rows} == {
        (TradeAction.withdrawal, D("-400.00")),
        (TradeAction.withdrawal, D("-24318.76")),
    }
    assert not any(r.action == TradeAction.contribution for r in rows)
    for row in rows:
        assert row.quantity == D("0")
        effect = _trade_cash_effect(
            _trade_cash(row.action, str(row.quantity), str(row.price_native))
        )
        assert effect == row.price_native
        assert effect < 0  # money OUT on the ledger, both rows


# --------------------------------------------------------------------------- #
# 7. Signed fees: stored negative, cash effect -abs, honest on the surface
#    (docs/02 §Signed-amount convention, docs/05 cash-effect table)
# --------------------------------------------------------------------------- #


FEE_SIGN_CSV = (
    _WS_HEADER
    + "\n"
    # The file's own negative fee (the real Wealthsimple shape).
    + _ws_row("2026-05-01", "", "Management fee", "CAD", "", "-145.75", type_="Fee")
    + "\n"
    # An exporter that writes the magnitude POSITIVE still means money out.
    + _ws_row("2026-05-02", "", "Advice fee", "CAD", "", "145.75", type_="Fee")
    + "\n"
)


@pytest.mark.parametrize("parser", ["legacy", "profile"])
def test_fee_rows_are_stored_at_the_negative_native_sign(parser) -> None:
    """A cash OUTFLOW stores its NEGATIVE native sign whichever way the file
    spelled the amount. A stored `+145.75 fee` is exactly the M131 defect: the
    transactions surface, the exports and the event log would render money
    LEAVING the account as an inflationary credit."""
    rows, unmapped = _rows(FEE_SIGN_CSV.encode(), parser)
    assert unmapped == []
    assert len(rows) == 2
    for row in rows:
        assert row.action == TradeAction.fee
        assert row.quantity == D("0")
        assert row.price_native == D("-145.75")
        assert row.price_native < 0
        effect = _trade_cash_effect(
            _trade_cash(row.action, str(row.quantity), str(row.price_native))
        )
        assert effect == D("-145.75")
        assert effect < 0


def test_import_stores_the_fee_negative_on_the_trades_surface(client: TestClient) -> None:
    """End to end: after POST /trades/import + commit, GET /trades renders both
    fees at -145.75 — the surface the user actually reads (docs/02: 'honest on
    every surface that renders it directly')."""
    headers = _auth(client)
    account = _account(client, headers)
    staged = client.post(
        f"{API}/trades/import",
        files={"file": ("fees.csv", FEE_SIGN_CSV.encode(), "text/csv")},
        data={"account_id": account["id"]},
        headers=headers,
    )
    assert staged.status_code == 201, staged.text
    assert staged.json()["unmapped"] == [], staged.json()["unmapped"]
    commit = client.post(f"{API}/trades/import/{staged.json()['batch_id']}/commit", headers=headers)
    assert commit.status_code == 200, commit.text
    listed = client.get(f"{API}/trades", params={"account_id": account["id"]}, headers=headers)
    fees = [t for t in listed.json()["items"] if t["action"] == "fee"]
    assert len(fees) == 2, fees
    assert {D(str(t["price_native"])) for t in fees} == {D("-145.75")}


# --------------------------------------------------------------------------- #
# 8. PATCH /trades/{id} refuses a negative contribution too — the third write
#    path docs/02 and docs/08 name (importer per row, POST, PATCH)
# --------------------------------------------------------------------------- #


def test_update_refuses_a_negative_contribution(client: TestClient) -> None:
    """No partial update can turn a trade into the row class that corrupted
    the contribution math: pushing the PRICE negative and pushing the ACTION to
    `contribution` are both refused 422, and neither write lands."""
    headers = _auth(client)
    account = _account(client, headers)
    security = client.post(
        f"{API}/securities",
        json={
            "symbol": "CASHM131U",
            "name": "Cash",
            "kind": "stock",
            "listing_currency": "CAD",
        },
        headers=headers,
    )
    assert security.status_code == 201, security.text
    base = {
        "account_id": account["id"],
        "security_id": security.json()["id"],
        "date": "2026-01-01",
        "quantity": "0",
        "commission": "0",
    }

    # (a) an existing contribution with its price pushed negative
    created = client.post(
        f"{API}/trades",
        json={**base, "action": "contribution", "price_native": "100.00"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    trade_id = created.json()["id"]
    bad = client.patch(
        f"{API}/trades/{trade_id}", json={"price_native": "-100.00"}, headers=headers
    )
    assert bad.status_code == 422, bad.text
    assert bad.json()["code"] == "negative_contribution"
    after = client.get(f"{API}/trades/{trade_id}", headers=headers).json()
    assert D(str(after["price_native"])) == D("100.00")  # refused, not written
    assert after["action"] == "contribution"

    # (b) an existing negative row with its action pushed to `contribution`
    withdrawal = client.post(
        f"{API}/trades",
        json={**base, "action": "withdrawal", "price_native": "-100.00"},
        headers=headers,
    )
    assert withdrawal.status_code == 201, withdrawal.text
    other_id = withdrawal.json()["id"]
    bad = client.patch(f"{API}/trades/{other_id}", json={"action": "contribution"}, headers=headers)
    assert bad.status_code == 422, bad.text
    assert bad.json()["code"] == "negative_contribution"
    after = client.get(f"{API}/trades/{other_id}", headers=headers).json()
    assert after["action"] == "withdrawal"  # refused, not written


# --------------------------------------------------------------------------- #
# 9. The value-history marker reads the ACTION; the sign only covers the
#    pre-M131 rows still in the database (docs/08)
# --------------------------------------------------------------------------- #


def test_flow_kind_reads_the_action_and_keeps_the_legacy_sign_fallback() -> None:
    """Both halves of `_flow_kind`'s contract, pinned separately because they
    can only be told apart when the value is 0:

    * the ACTION is authoritative for a `withdrawal` — a zero-amount withdrawal
      has value 0, which the sign fallback alone would label `contribution`;
    * the sign fallback still labels a legacy NEGATIVE `contribution` (the row
      class M131 now refuses at import, but pre-M131 rows are still in the
      database) as money leaving.
    """
    empty_withdrawal = _trade_cash(TradeAction.withdrawal, "0", "0")
    assert _trade_cash_effect(empty_withdrawal) == D("0")
    assert _flow_kind(empty_withdrawal, D("0")) == "withdrawal"

    legacy_negative_contribution = _trade_cash(TradeAction.contribution, "0", "-24318.76")
    legacy_value = _trade_cash_effect(legacy_negative_contribution)
    assert legacy_value < 0
    assert _flow_kind(legacy_negative_contribution, legacy_value) == "withdrawal"

    deposit = _trade_cash(TradeAction.contribution, "0", "12600")
    assert _flow_kind(deposit, _trade_cash_effect(deposit)) == "contribution"


def test_marker_still_reads_a_legacy_negative_contribution_as_money_leaving(
    session: Session,
) -> None:
    """The value-history EVENT for a pre-M131 row — action `contribution`,
    price -24,318.76 — is still money LEAVING (kind `withdrawal`, direction
    -1). Making the action the ONLY input would label it a contribution and
    hide the outflow from the chart's own explanation."""
    user = User(email=f"m131-legacy-{time_ns()}@example.com", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    account = Account(
        user_id=user.id,
        name="TFSA",
        type=AccountType.tfsa,
        currency="CAD",
        taxable=False,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    cash = _cash_security(session, user)

    session.add(
        Trade(
            user_id=user.id,
            account_id=account.id,
            security_id=cash.id,
            action=TradeAction.contribution,
            date=date(2026, 1, 14),
            quantity=D("0"),
            price_native=D("-24318.76"),
            commission=D("0"),
        )
    )
    session.commit()

    result = account_value_history(
        session, account, date(2025, 12, 1), date(2026, 1, 31), "day", D("1000")
    )
    events = result["events"]  # type: ignore[assignment]
    (event,) = [e for e in events if e["source"] == "trade"]  # type: ignore[index]
    assert event["kind"] == "withdrawal"
    assert event["direction"] == -1
    assert event["amount_native"] == D("-24318.76")
