"""M138 — the dashboard subscriptions card's contract (docs/09 §Bills you probably have).

The card used to be fed by the client-side M27 detector (`s3Engine.ts`, "2 or
more charges", average-gap bucketing, no amount band at all), which is what put
FILM.CA / LOBLAW / TIM HORTONS / SHELL on Mark's dashboard. It is now fed by
`GET /transactions/recurring/candidates?source=subscriptions` — the SAME strict
engine the M96 prompt uses (`services/recurring.py`: ≥3 occurrences, ±15%
same-sign band, every gap must fit one cadence).

Three things are pinned here:

1. **the gate** — the four M133 junk histories can never surface, the six real
   bills still can (and the loose M27 rule still WOULD have accepted the junk,
   so the fixture is the M133 shape and not a strawman);
2. **the key** — `make_subscriptions_group_key` shares M96's merchant identity,
   is amount-free, and can never equal a prompt group key;
3. **the boundary** — a dashboard dismissal hides the dashboard row and leaves
   the M96 prompt's candidate on screen, and the reverse.

Run: cd backend && uv run pytest tests/test_m138_subscriptions_card.py -q
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

from app.models.recurring_suppressions import RecurringSuppression
from app.models.transactions import RecurringCadence
from app.services.recurring import (
    LedgerRow,
    detect_recurring,
    make_group_key,
    make_subscriptions_group_key,
)
from app.services.subscriptions import detect_subscriptions, normalize_payee_key
from app.services.txn_filters import today_et
from tests.test_deep_common import auth, create_account, register, seed_transaction


def _today() -> date:
    """The clock, read at CALL time — never captured at import (t_435c2d14)."""
    return today_et()


#: M133's evidence: `GET /subscriptions` (the loose M27 rule) listed all four.
#: Every history below is 13 months of the thing the dashboard mistook for a
#: bill — amounts that wander outside ±15% and gaps that fit no cadence.
_JUNK = {
    "LOBLAW": (53, (6, 6, 6, 6, 11, 9, 6, 6, 6, 6), ("-18.40", "-72.55")),
    "TIM HORTONS": (95, (3, 3, 3, 3, 8, 7, 3, 3, 3, 3), ("-2.85", "-14.60")),
    "SHELL": (18, (7, 7, 7, 7, 13, 9, 7, 7, 7, 7), ("-21.40", "-64.90")),
}

#: The M133 fixture's strict six — every one of them clears the gate.
_REAL = (
    ("PAYROLL", "5000.00"),
    ("RENT", "-2200.00"),
    ("ROGERS", "-95.00"),
    ("GOODLIFE", "-52.00"),
    ("NETFLIX", "-15.99"),
    ("SPOTIFY", "-5.99"),
)


def _junk_history(
    payee: str, count: int, gap_cycle: tuple[int, ...], amounts: tuple[str, str]
) -> list[LedgerRow]:
    """`count` rows: two amount bands, gaps that wander.

    The gaps are indexed off the same period as the amounts, so each amount
    band's own gaps are the PAIRS `gap_cycle[i] + gap_cycle[i+1]` — one entry
    of the cycle is deliberately an oddball (11+9, 8+7, 13+9) so neither band
    can fit the 7/14/28/30/31-day patterns or the 26-32-day monthly bucket.
    """
    rows: list[LedgerRow] = []
    day = _today() - timedelta(days=395)
    for index in range(count):
        rows.append(
            LedgerRow(
                id=index + 1,
                date=day,
                payee=payee,
                amount=Decimal(amounts[index % 2]),
            )
        )
        day = day + timedelta(days=gap_cycle[index % len(gap_cycle)])
    return rows


def _monthly_history(payee: str, amount: str, count: int = 4) -> list[LedgerRow]:
    start = _today() - timedelta(days=30 * (count - 1))
    return [
        LedgerRow(
            id=index + 1,
            date=start + timedelta(days=30 * index),
            payee=payee,
            amount=Decimal(amount),
        )
        for index in range(count)
    ]


# --- 1. the gate --------------------------------------------------------------


@pytest.mark.parametrize("payee", sorted(_JUNK))
def test_m133_junk_histories_never_surface(payee: str) -> None:
    """LOBLAW 53x / TIM HORTONS 95x / SHELL 18x clear M27, not the strict gate."""
    count, gaps, amounts = _JUNK[payee]
    rows = _junk_history(payee, count, gaps, amounts)
    # The fixture is wide enough that the OLD rule would have accepted it...
    assert len(rows) >= 2
    # ...and the strict rule returns nothing at all.
    assert detect_recurring(rows) == []


def test_film_ca_two_occurrences_can_never_surface_again() -> None:
    """The M133 verdict row: 2 charges is below MIN_OCCURRENCES, full stop."""
    rows = [
        LedgerRow(
            id=1, date=_today() - timedelta(days=60), payee="FILM.CA", amount=Decimal("-36.35")
        ),
        LedgerRow(
            id=2, date=_today() - timedelta(days=30), payee="FILM.CA", amount=Decimal("-36.35")
        ),
    ]
    # Two same-day-month, same-amount charges is EXACTLY the shape the loose
    # detector called a subscription (s3Engine.ts:1141, "2 or more charges").
    assert len(rows) == 2
    assert detect_recurring(rows) == []
    # One more charge and it would qualify — the gate is the count, nothing else.
    rows.append(LedgerRow(id=3, date=_today(), payee="FILM.CA", amount=Decimal("-36.35")))
    assert len(detect_recurring(rows)) == 1


@pytest.mark.parametrize(("payee", "amount"), _REAL)
def test_the_strict_six_still_surface(payee: str, amount: str) -> None:
    """M133's fixture: PAYROLL/RENT/ROGERS/GOODLIFE/NETFLIX/SPOTIFY keep their row."""
    groups = detect_recurring(_monthly_history(payee, amount))
    assert [group.payee for group in groups] == [payee]
    assert groups[0].cadence is RecurringCadence.monthly
    assert groups[0].occurrence_count == 4


def test_the_loose_m27_rule_would_still_list_what_the_card_now_hides(
    client: TestClient, session: Session
) -> None:
    """End-to-end proof the SUBSTRATE changed, not just the fixture.

    The same two tenants' rows go through both readers: `detect_subscriptions`
    (the M27 rule the dashboard used to be fed from — ≥2 charges, no amount
    band) lists FILM.CA; the subscriptions card's endpoint does not.
    """
    body = register(client)
    headers = auth(body["access_token"])
    user_id = body["user"]["id"]
    account_id = create_account(client, headers)

    film_ca_days = (_today() - timedelta(days=60), _today() - timedelta(days=30))
    for day in film_ca_days:
        seed_transaction(session, user_id, account_id, payee="FILM.CA", amount="-36.35", day=day)
    netflix_ids = [
        seed_transaction(
            session,
            user_id,
            account_id,
            payee="NETFLIX",
            amount="-15.99",
            day=_today() - timedelta(days=30 * offset),
        ).id
        for offset in (3, 2, 1, 0)
    ]
    assert len(netflix_ids) == 4

    loose = {row["payee"] for row in detect_subscriptions(session, user_id)}
    assert "FILM.CA" in loose, "the M27 rule still accepts the junk (that is the bug)"
    assert "NETFLIX" in loose

    card = client.get(
        "/api/v1/transactions/recurring/candidates?source=subscriptions", headers=headers
    )
    assert card.status_code == 200
    assert [group["payee"] for group in card.json()["groups"]] == ["NETFLIX"]


# --- 2. the suppression key ---------------------------------------------------


def test_subscriptions_key_shares_the_m96_merchant_identity() -> None:
    """Same `normalize_payee_key` segment as `make_group_key` — one vocabulary."""
    for payee in ("Netflix", "NETFLIX", "netflix.com #1234", "  Superstore #99 "):
        subscriptions_key = make_subscriptions_group_key(payee)
        prompt_key = make_group_key(payee, RecurringCadence.monthly)
        assert subscriptions_key.split("|")[0] == prompt_key.split("|")[0]
        assert subscriptions_key.split("|")[0] == normalize_payee_key(payee)


def test_subscriptions_key_is_amount_free_and_branch_collapse_safe() -> None:
    """A dismissal is per PAYEE: no cadence, no amount, no branch number."""
    variants = ("ROGERS", "rogers", "Rogers #4409", "ROGERS  88123")
    keys = {make_subscriptions_group_key(payee) for payee in variants}
    assert len(keys) == 1
    # The prompt key, by contrast, is merchant + cadence (four of them).
    prompt_keys = {
        make_group_key("ROGERS", cadence)
        for cadence in RecurringCadence  # type: ignore[union-attr]
    }
    assert len(prompt_keys) == len(RecurringCadence)


@pytest.mark.parametrize("cadence", list(RecurringCadence))
def test_subscriptions_key_can_never_equal_a_prompt_group_key(
    cadence: RecurringCadence,
) -> None:
    """The SOURCE discriminator: `subscriptions` is not a cadence value."""
    payee = "NETFLIX"
    subscriptions_key = make_subscriptions_group_key(payee)
    assert subscriptions_key != make_group_key(payee, cadence)
    assert subscriptions_key.endswith("|subscriptions")


def test_both_keys_fit_the_column() -> None:
    """`recurring_suppressions.group_key` is varchar(120)."""
    long_payee = "MERCHANT " * 40
    assert len(make_subscriptions_group_key(long_payee)) <= 120
    for cadence in RecurringCadence:
        assert len(make_group_key(long_payee, cadence)) <= 120  # type: ignore[union-attr]


# --- 3. the boundary ----------------------------------------------------------


def _seed_netflix(session: Session, user_id: int, account_id: int) -> list[int]:
    return [
        seed_transaction(
            session,
            user_id,
            account_id,
            payee="Netflix",
            amount="-15.99",
            day=_today() - timedelta(days=30 * offset),
        ).id
        for offset in (3, 2, 1, 0)
    ]


def _candidates(client: TestClient, headers: dict, source: str | None = None) -> dict:
    url = "/api/v1/transactions/recurring/candidates"
    if source is not None:
        url = f"{url}?source={source}"
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_dashboard_dismissal_leaves_the_m96_prompt_alone(
    client: TestClient, session: Session
) -> None:
    """The M138 boundary: one merchant, two surfaces, two independent answers."""
    body = register(client)
    headers = auth(body["access_token"])
    user_id = body["user"]["id"]
    account_id = create_account(client, headers)
    _seed_netflix(session, user_id, account_id)

    prompt_page = _candidates(client, headers)
    assert [group["payee"] for group in prompt_page["groups"]] == ["Netflix"]
    prompt_key = prompt_page["groups"][0]["group_key"]
    assert prompt_page["groups"][0]["dismiss_key"] == prompt_key

    card_page = _candidates(client, headers, "subscriptions")
    assert [group["payee"] for group in card_page["groups"]] == ["Netflix"]
    card_key = card_page["groups"][0]["dismiss_key"]
    assert card_key != prompt_key

    # The dashboard card dismisses — one idempotent POST, exactly like M96.
    dismiss = client.post(
        "/api/v1/transactions/recurring/suppress",
        headers=headers,
        json={"group_key": card_key},
    )
    assert dismiss.status_code == 204
    assert (
        client.post(
            "/api/v1/transactions/recurring/suppress",
            headers=headers,
            json={"group_key": card_key},
        ).status_code
        == 204
    )

    # ...the card stops offering it...
    after_card = _candidates(client, headers, "subscriptions")
    assert after_card["groups"] == []
    assert after_card["suppressed_count"] == 1
    # ...and the M96 prompt STILL offers it, because it clears the strict rule.
    after_prompt = _candidates(client, headers)
    assert [group["payee"] for group in after_prompt["groups"]] == ["Netflix"]
    assert after_prompt["suppressed_count"] == 0

    # Reverse: declining at the prompt does not silence the dashboard card.
    decline = client.post(
        "/api/v1/transactions/recurring/suppress",
        headers=headers,
        json={"group_key": prompt_key},
    )
    assert decline.status_code == 204
    assert _candidates(client, headers)["groups"] == []
    assert _candidates(client, headers, "subscriptions")["groups"] == []

    # Two answers, two rows — the source discriminator is in the key itself.
    rows = session.exec(
        select(RecurringSuppression).where(RecurringSuppression.user_id == user_id)
    ).all()
    assert {row.group_key for row in rows} == {prompt_key, card_key}


def test_the_source_parameter_rejects_an_unknown_surface(
    client: TestClient,
) -> None:
    body = register(client)
    headers = auth(body["access_token"])
    assert (
        client.get(
            "/api/v1/transactions/recurring/candidates?source=widget", headers=headers
        ).status_code
        == 422
    )
    # The default is unchanged: M96's prompt card keeps its behaviour.
    assert _candidates(client, headers)["suppressed_count"] == 0
