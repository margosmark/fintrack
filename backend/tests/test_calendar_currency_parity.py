"""End-to-end regression: a stated amount keeps ITS OWN currency from the ledger
row to the calendar's JSON (M170 follow-up, docs/36 §Currency, card t_c5ee17eb).

The defect this pins: the calendar's money field was `amount_cad` and a confirmed
recurring group's `amount_native` went straight into it, so a USD charge was
stated as a CAD figure nobody had computed (`bbd9b26` split the field into
`amount` + `currency`).

The sibling suites pin what each surface says (`test_cashflow_calendar.py`), and
the older parity case
(`test_m170_the_shared_confirmed_query_answers_both_surfaces`) predates the
`currency` field — it compares `(day, label, amount)` only. What is pinned HERE
is the PATH, over real rows and the real HTTP route:

* both readers of the ONE confirmed query state the SAME `(amount, currency)`
  pair, for every source it can name — a CAD bill, a variable bill, a CAD
  group, a USD group, a mixed group and an unknown-currency group in one month,
  and the series' third source (an M174 tag-marked USD group) in its own;
* the code on the wire is the one the ROWS were RECORDED in — the record is the
  evidence, never a constant — and the figure is the rows' own mean,
  unconverted;
* the pair is all-or-nothing on both surfaces, and the read path never consults
  the rate book (docs/36 decision 6) — being currency-preserving IS not
  converting.

The dividend regression now pins the same pair rule for the third leg: a
USD-account security with NULL trade currency is stated as USD, its native mean
is untouched, and the read path does not consult FX. Separate cases retain the
CAD row-evidence control and withhold the pair for mixed dividend rows.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.accounts import Account, AccountType
from app.models.bill_reminders import BillReminder
from app.models.securities import Security
from app.models.tags import Tag
from app.models.trades import Trade, TradeAction
from app.models.transaction_tags import TransactionTag
from app.models.transactions import Transaction
from app.models.users import User
from app.services.confirmed_subscriptions import (
    SOURCE_BILL,
    SOURCE_TAG,
    confirmed_subscriptions,
)
from app.services.dividend_calendar import compute_dividend_calendar
from app.services.txn_filters import today_et

API = "/api/v1"
CAL = f"{API}/reports/cashflow-calendar"


def _today() -> dt.date:
    """The clock, read at CALL time — never captured at import (AGENTS §6)."""
    return today_et()


def _auth(client: TestClient) -> tuple[dict[str, str], int]:
    from datetime import UTC, datetime

    email = f"calparity-{datetime.now(UTC).timestamp()}-{id(client)}@example.com"
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


def _txn(
    session: Session,
    user_id: int,
    account: Account,
    *,
    payee: str,
    day: dt.date,
    amount: str,
    currency: str,
) -> Transaction:
    """One statement row, with the currency the STATEMENT recorded.

    `currency` is deliberately independent of `account.currency` here: ingestion
    stamps the row's own token (docs/02, docs/21), so a card can carry a charge
    in a currency that is not the account's — and the surfaces must read the
    ROW, not the account.
    """
    txn = Transaction(
        user_id=user_id,
        account_id=account.id,
        date=day,
        raw_description=payee,
        normalized_payee=payee,
        amount_native=Decimal(amount),
        currency=currency,
        dedup_hash=f"calparity-{user_id}-{payee}-{day.isoformat()}-{amount}-{currency}",
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


def _accept(client: TestClient, headers: dict[str, str], txns: list[Transaction]) -> dt.date:
    """Accept a recurring group through the REAL route (the user's own action)."""
    payee = txns[0].normalized_payee or txns[0].raw_description
    response = client.post(
        f"{API}/transactions/recurring/accept",
        headers=headers,
        json={
            "group_key": f"{payee.replace(' ', '').upper()}|monthly",
            "cadence": "monthly",
            "transaction_ids": [txn.id for txn in txns],
        },
    )
    assert response.status_code == 200, response.text
    return dt.date.fromisoformat(response.json()["next_expected_date"])


def _pin_next(session: Session, txns: list[Transaction], day: dt.date) -> None:
    """Put an accepted group's stored anchor on `day` (the accept route's own
    column), so the whole fixture lands in ONE month."""
    for txn in txns:
        txn.recurring_next_date = day
        session.add(txn)
    session.commit()


def _tag(session: Session, user_id: int, name: str) -> Tag:
    tag = Tag(user_id=user_id, name=name)
    session.add(tag)
    session.commit()
    session.refresh(tag)
    return tag


def _nominate(session: Session, user_id: int, tag_ids: list[int]) -> None:
    """Nominate the "recurring" tags the way Settings does (M174)."""
    user = session.get(User, user_id)
    assert user is not None
    user.recurring_intent_tag_ids = list(tag_ids)
    session.add(user)
    session.commit()


def _bill(
    session: Session,
    user_id: int,
    *,
    name: str,
    due: str,
    amount: str | None,
) -> BillReminder:
    bill = BillReminder(
        user_id=user_id,
        name=name,
        amount=None if amount is None else Decimal(amount),
        frequency="monthly",
        next_due_date=due,
        enabled=True,
        match_term=name,
    )
    session.add(bill)
    session.commit()
    session.refresh(bill)
    return bill


def _calendar(client: TestClient, headers: dict[str, str], month: str) -> dict:
    response = client.get(CAL, headers=headers, params={"month": month})
    assert response.status_code == 200, response.text
    return response.json()


def _wire_pairs(body: dict) -> list[tuple[str, str, str, str | None, str | None]]:
    """Every `(day, kind, label, amount, currency)` the wire carries."""
    return [
        (day["date"], event["kind"], event["label"], event["amount"], event["currency"])
        for day in body["days"]
        for event in day["events"]
    ]


def _confirmed_pairs(
    entries: list, month: str
) -> list[tuple[str, str, str, str | None, str | None]]:
    """The same tuple, answered by the confirmed query itself.

    `SOURCE_BILL` entries are the Bills series (`kind == "bill"`, label =
    `bill.name`); the group sources share the `subscription` kind, so the two
    vocabularies line up one-for-one (docs/36 decisions 6/9).
    """
    pairs = []
    for entry in entries:
        if entry.next_expected.strftime("%Y-%m") != month:
            continue
        kind = "bill" if entry.source == SOURCE_BILL else "subscription"
        amount = None if entry.amount is None else str(entry.amount)
        pairs.append((entry.next_expected.isoformat(), kind, entry.payee, amount, entry.currency))
    return pairs


def test_the_two_surfaces_state_the_same_amount_and_currency_for_every_source(
    client: TestClient, session: Session
) -> None:
    """The parity pin: one month, every source, both readers, pair included.

    The fixture is the whole vocabulary in one grid: a CAD bill and a VARIABLE
    bill (the Bills series), a CAD group, a USD group, a mixed group and a group
    whose rows disagree with any code (the `subscription` series). The
    expectation is not a literal — it is the OTHER surface's own answer for the
    same user, so the two can only agree by reading the same query.
    """
    headers, user_id = _auth(client)
    cad = _account(session, user_id, currency="CAD")
    usd = _account(session, user_id, currency="USD")

    # `_today().replace(day=28) + 7d` is always inside the NEXT month (a day
    # that exists in every month, then one week), so the pinned month is
    # unambiguous and forward of the clock whatever today is (AGENTS §6).
    due = _today().replace(day=28) + dt.timedelta(days=7)
    month = due.strftime("%Y-%m")
    assert dt.date.fromisoformat(due.isoformat()) > _today()

    _bill(session, user_id, name="Mapleview Rent", due=due.isoformat(), amount="1400.00")
    _bill(
        session,
        user_id,
        name="Hydro (variable)",
        due=(due + dt.timedelta(days=1)).isoformat(),
        amount=None,
    )

    groups = {
        "NETFLIX.COM": [("CAD", "CAD", "-15.99"), ("CAD", "CAD", "-15.99")],
        "SPOTIFY": [("USD", "USD", "-11.99"), ("USD", "USD", "-11.99")],
        # One payee, two denominations: the mean is a number with no currency.
        "DROPBOX": [("CAD", "CAD", "-11.99"), ("USD", "USD", "-12.99")],
        # One row the query cannot denominate is enough (docs/36 decision 15).
        "NOTION": [("CAD", "CAD", "-9.99"), ("CAD", "", "-9.99")],
    }
    for index, (payee, rows) in enumerate(groups.items()):
        charges = [
            _txn(
                session,
                user_id,
                cad if row[0] == "CAD" else usd,
                payee=payee,
                day=_today() - dt.timedelta(days=30 * (row_index + 1)),
                amount=row[2],
                currency=row[1],
            )
            for row_index, row in enumerate(rows)
        ]
        _accept(client, headers, charges)
        _pin_next(session, charges, due + dt.timedelta(days=2 + index))

    body = _calendar(client, headers, month)
    confirmed = confirmed_subscriptions(session, user_id)

    expected = sorted(_confirmed_pairs(confirmed, month))
    actual = sorted(pair for pair in _wire_pairs(body) if pair[1] in ("bill", "subscription"))
    assert actual == expected
    # …and the fixture really exercised all four subscription shapes, plus the
    # two bill shapes, so the equality above is not a tautology over an empty
    # set (a green run must mean the pairs were compared).
    assert {pair[2] for pair in expected} >= {
        "Mapleview Rent",
        "Hydro (variable)",
        "NETFLIX.COM",
        "SPOTIFY",
        "DROPBOX",
        "NOTION",
    }
    stated = {(pair[2]): (pair[3], pair[4]) for pair in expected}
    assert stated["SPOTIFY"] == ("-11.99", "USD")
    assert stated["NETFLIX.COM"] == ("-15.99", "CAD")
    assert stated["DROPBOX"] == (None, None)
    assert stated["NOTION"] == (None, None)
    assert stated["Hydro (variable)"] == (None, None)

    # The payload's second reader of the same query: the count is the WHOLE
    # confirmed set, this month or not.
    assert body["confirmed_subscription_count"] == len(confirmed)

    # All-or-nothing, every event (docs/36 decision 15): never a figure with no
    # denomination, never a denomination with no figure.
    for _day, _kind, _label, amount, currency in _wire_pairs(body):
        assert (amount is None) == (currency is None), (amount, currency)


def test_the_stated_currency_is_the_one_the_rows_were_recorded_in(
    client: TestClient, session: Session
) -> None:
    """The record is the evidence — not the account, not a constant.

    Two groups whose rows carry a currency that is NOT their account's: a CAD
    charge on a USD card and a USD charge on a CAD account. A surface that
    defaulted to `account.currency`, or to CAD, would state the mean under the
    wrong code; the pair has to come from the rows themselves.
    """
    headers, user_id = _auth(client)
    cad = _account(session, user_id, currency="CAD")
    usd = _account(session, user_id, currency="USD")
    due = _today().replace(day=28) + dt.timedelta(days=7)

    cases = {
        "FIGMA": (usd, "CAD", "-12.00"),
        "GITHUB": (cad, "USD", "-4.00"),
    }
    for index, (payee, (account, code, amount)) in enumerate(cases.items()):
        charges = [
            _txn(
                session,
                user_id,
                account,
                payee=payee,
                day=_today() - dt.timedelta(days=30 * (row + 1)),
                amount=amount,
                currency=code,
            )
            for row in range(2)
        ]
        _accept(client, headers, charges)
        _pin_next(session, charges, due + dt.timedelta(days=2 + index))

    body = _calendar(client, headers, due.strftime("%Y-%m"))
    events = {pair[2]: (pair[3], pair[4]) for pair in _wire_pairs(body)}
    assert events["FIGMA"] == ("-12.00", "CAD")  # a CAD charge on a USD card
    assert events["GITHUB"] == ("-4.00", "USD")  # a USD charge on a CAD account

    by_payee = {entry.payee: entry for entry in confirmed_subscriptions(session, user_id)}
    assert (str(by_payee["FIGMA"].amount), by_payee["FIGMA"].currency) == ("-12.00", "CAD")
    assert (str(by_payee["GITHUB"].amount), by_payee["GITHUB"].currency) == ("-4.00", "USD")


def test_the_tag_marked_source_states_its_own_currency_too(
    client: TestClient, session: Session
) -> None:
    """The series' THIRD source (M174) is denominated by the same rows-agree
    rule — and it is the one source with no accept route to pin a date, so its
    month comes from the query's own projection.

    A group every one of whose rows carries a nominated tag is confirmed
    state; a USD group of that shape must reach the calendar as USD, not as the
    CAD a surface could have hard-coded (the two `_group_currency` branches
    share one helper, and this pins the tag branch's side of it end to end).
    """
    headers, user_id = _auth(client)
    usd = _account(session, user_id, currency="USD")
    intent = _tag(session, user_id, "subscription")
    # Same-day-of-month charges — a perfect monthly fit, so anything refused is
    # refused by the tag source's own rules and never by the cadence bands.
    rows = [
        _txn(
            session,
            user_id,
            usd,
            payee="ICLOUD",
            day=_today() - dt.timedelta(days=offset),
            amount="-2.99",
            currency="USD",
        )
        for offset in (90, 60, 30)
    ]
    for row in rows:
        session.add(TransactionTag(transaction_id=row.id, tag_id=intent.id))
    session.commit()
    assert intent.id is not None
    _nominate(session, user_id, [intent.id])

    tagged = [
        entry for entry in confirmed_subscriptions(session, user_id) if entry.source == SOURCE_TAG
    ]
    assert [(entry.payee, str(entry.amount), entry.currency) for entry in tagged] == [
        ("ICLOUD", "-2.99", "USD")
    ]

    body = _calendar(client, headers, tagged[0].next_expected.strftime("%Y-%m"))
    events = [pair for pair in _wire_pairs(body) if pair[1] == "subscription"]
    assert [(pair[2], pair[3], pair[4]) for pair in events] == [
        (tagged[0].payee, str(tagged[0].amount), tagged[0].currency)
    ]


def test_the_read_path_never_consults_the_rate_book(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Currency-preserving IS not converting (docs/36 decision 6, docs/07).

    The rate book is patched to raise for the WHOLE round trip: a path that
    started converting — or merely asking for a rate — would fail here instead
    of quietly inventing a CAD number for a USD commitment.
    """
    headers, user_id = _auth(client)
    usd = _account(session, user_id, currency="USD")
    due = _today().replace(day=28) + dt.timedelta(days=7)
    charges = [
        _txn(
            session,
            user_id,
            usd,
            payee="SPOTIFY",
            day=_today() - dt.timedelta(days=30 * (row + 1)),
            amount="-11.99",
            currency="USD",
        )
        for row in range(2)
    ]
    _accept(client, headers, charges)
    _pin_next(session, charges, due)

    from app.services import fx as fx_service

    def forbidden(*args, **kwargs):
        raise AssertionError("the calendar read consulted the fx rate book")

    monkeypatch.setattr(fx_service, "cached_cad_amount", forbidden)
    monkeypatch.setattr(fx_service, "latest_rate", forbidden)

    body = _calendar(client, headers, due.strftime("%Y-%m"))
    subscriptions = [
        (pair[2], pair[3], pair[4]) for pair in _wire_pairs(body) if pair[2] == "SPOTIFY"
    ]
    assert subscriptions == [("SPOTIFY", "-11.99", "USD")]


def test_dividend_event_preserves_the_rows_native_currency(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The calendar keeps M29's native mean and labels it from row evidence.

    Demo-shaped NULL trade currency falls back to the USD account. The amount
    remains unchanged; no conversion is performed.
    """
    headers, user_id = _auth(client)
    usd = _account(session, user_id, currency="USD")
    security = Security(
        user_id=user_id,
        symbol="VTI",
        name="Vanguard Total Stock Market ETF",
        kind="etf",
        listing_currency="USD",
        exchange="NYSE",
    )
    session.add(security)
    session.commit()
    session.refresh(security)

    # Two quarterly payments of the same native gross, the way the demo seeds
    # them (the trade carries no currency of its own, exactly as the seed and
    # the pre-M132 rows do — the account is USD and so is the listing).
    for days in (180, 89):
        session.add(
            Trade(
                user_id=user_id,
                account_id=usd.id,
                security_id=security.id,
                action=TradeAction.dividend,
                date=_today() - dt.timedelta(days=days),
                quantity=Decimal("0"),
                price_native=Decimal("85.00"),
                commission=Decimal("0"),
            )
        )
    session.commit()

    entry = next(
        item
        for item in compute_dividend_calendar(session, user_id)["upcoming"]
        if item["security_id"] == security.id
    )
    from app.services import fx as fx_service

    def forbid_fx(*args, **kwargs):
        raise AssertionError("dividend calendar must not consult FX")

    monkeypatch.setattr(fx_service, "cached_cad_amount", forbid_fx)
    monkeypatch.setattr(fx_service, "latest_rate", forbid_fx)
    month = entry["next_expected_date"].strftime("%Y-%m")
    dividends = [
        (pair[2], pair[3], pair[4])
        for pair in _wire_pairs(_calendar(client, headers, month))
        if pair[2] == "VTI dividend"
    ]
    # The figure is M29's native mean, untouched…
    assert str(entry["avg_amount"]) == "85.00"
    assert entry["currency"] == "USD"
    assert dividends == [("VTI dividend", "85.00", "USD")]
    assert security.listing_currency == "USD"


def test_dividend_currency_is_row_based_and_withheld_when_unnameable(
    client: TestClient, session: Session
) -> None:
    """CAD row evidence remains CAD; malformed row evidence withholds the pair."""
    headers, user_id = _auth(client)
    cad_account = _account(session, user_id, currency="CAD")
    cases = (
        ("RY.TO", "USD", ("CAD", "CAD"), "45.00"),
        ("MIXED", "CAD", ("USD", "CAD"), "12.00"),
    )
    securities = {}
    for symbol, listing_currency, row_currencies, amount in cases:
        security = Security(
            user_id=user_id,
            symbol=symbol,
            name=symbol,
            kind="stock",
            listing_currency=listing_currency,
            exchange="NYSE",
        )
        session.add(security)
        session.commit()
        session.refresh(security)
        securities[symbol] = security
        for days, row_currency in zip((180, 89), row_currencies, strict=True):
            session.add(
                Trade(
                    user_id=user_id,
                    account_id=cad_account.id,
                    security_id=security.id,
                    action=TradeAction.dividend,
                    date=_today() - dt.timedelta(days=days),
                    quantity=Decimal("0"),
                    price_native=Decimal(amount),
                    currency=row_currency,
                    commission=Decimal("0"),
                )
            )
    session.commit()

    entries = {
        entry["symbol"]: entry
        for entry in compute_dividend_calendar(session, user_id)["upcoming"]
        if entry["symbol"] in securities
    }
    assert entries["RY.TO"]["currency"] == "CAD"
    assert str(entries["RY.TO"]["avg_amount"]) == "45.00"
    assert entries["MIXED"]["currency"] is None
    assert entries["MIXED"]["avg_amount"] is None
    assert entries["MIXED"]["annual_projection"] is None

    month = next(iter(entries.values()))["next_expected_date"].strftime("%Y-%m")
    events = {
        event["label"]: (event["amount"], event["currency"])
        for day in _calendar(client, headers, month)["days"]
        for event in day["events"]
        if event["kind"] == "dividend"
    }
    assert events["RY.TO dividend"] == ("45.00", "CAD")
    assert events["MIXED dividend"] == (None, None)
