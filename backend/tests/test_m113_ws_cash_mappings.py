"""M113 — the three Wealthsimple mappings Mark's real exports need (docs/03, docs/41).

Two fixes bundled into one card (t_9bba21d1): his TFSA export left **20 rows**
unmapped (every `Tax/NRT` withholding line plus every TF-to-TF cash transfer)
and his RESP export left **1** (`MoneyMovement/TRANSFER`, the money OUT).
Both files now parse to **zero** unmapped rows in BOTH parsers — the legacy
`parse_investment_csv` the import wizard falls back to for a Wealthsimple
header, and the opted-in `wealthsimple` profile — with the semantics docs/02
and docs/05 pin:

* ``(Tax, NRT)`` -> ``fee``: `price_native` is the MAGNITUDE of the file's
  negative amount, because `fee`'s cash effect is ``-price_native`` (docs/05).
  The dividend row keeps its GROSS amount and the withholding becomes its own
  visible row — gross + fee is the auditable form; no net dividend is ever
  fabricated.
* ``(MoneyMovement, TRANSFER_TF)`` / ``(MoneyMovement, TRANSFER)`` -> cash
  transfers: `quantity = 0`, symbol NOT required, direction read from
  `net_cash_amount`'s sign BEFORE any ``abs()`` (negative = out).
* ``SecurityTransfer/*`` keeps the share-transfer rules: symbol required,
  non-zero quantity, per-share cost basis.

Fixtures ``m113_tfsa_activity.csv`` / ``m113_resp_activity.csv`` are
synthetic stand-ins shaped like Wealthsimple exports — same rows, signs and
activity keys as the real files the M113 mappings were derived from — with
the account id anonymized to ``ws-anon`` (figures scrubbed, docs/60).
"""

from datetime import date
from decimal import Decimal
from pathlib import Path
from time import time_ns

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.importers.brokerage_profiles import parse_brokerage_csv
from app.importers.investment_profile import parse_investment_csv
from app.models.accounts import Account, AccountType
from app.models.securities import Security, SecurityKind
from app.models.trades import Trade, TradeAction
from app.models.users import User
from app.services import market_data
from app.services.total_return import _trade_cash_effect
from app.tax.acb import rebuild_user_pools

API = "/api/v1"
D = Decimal
FIXTURES = Path(__file__).parent / "fixtures"
TFSA_CLIP = FIXTURES / "m113_tfsa_activity.csv"
RESP_CLIP = FIXTURES / "m113_resp_activity.csv"

#: A transfer the file signs NEGATIVE — the (theoretical) money-out case the
#: sign rule must catch even though neither real export contains one yet.
NEGATIVE_TRANSFER_TF_CSV = (
    "effective_date,effective_time,settlement_date,account_id,account_type,activity_type,"
    "activity_sub_type,description,direction,symbol,name,currency,quantity,unit_price,"
    "commission,net_cash_amount\n"
    "2026-04-01,00:00:00,,ws-anon,TFSA,MoneyMovement,TRANSFER_TF,"
    # direction/symbol/name are blank, then currency, quantity, unit_price,
    # commission, net_cash_amount — 16 fields, exactly like the real header.
    "Money transfer out of the account (executed at 2026-04-01),,,,CAD,-250.00,,,-250.00\n"
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Security auto-create must never reach yfinance (docs/01 egress rule)."""
    monkeypatch.setattr(market_data, "lookup_symbol", lambda symbol, **kw: None)
    monkeypatch.setattr(market_data, "fetch_latest_close", lambda symbol, today=None: None)


def _legacy(path) -> tuple[list, list]:
    rows, unmapped = parse_investment_csv(path.read_bytes())
    return rows, unmapped


def _profiled(path, account_currency: str = "CAD"):
    outcome = parse_brokerage_csv(
        path.read_bytes(), "wealthsimple", account_currency=account_currency
    )
    assert outcome is not None  # a WS header always resolves to its profile
    return outcome


def _rows(path, parser: str) -> tuple[list, list]:
    """(rows, unmapped) from either parser — both must agree on M113 keys."""
    if parser == "legacy":
        return _legacy(path)
    outcome = _profiled(path)
    return outcome.rows, outcome.unmapped


def _by_action(rows, action: TradeAction) -> list:
    return [row for row in rows if row.action == action]


def _trade_cash(action: TradeAction | str, quantity: str, price: str, commission: str = "0"):
    """A detached Trade with just the fields the cash-effect rule reads."""
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
# 1. Zero unmapped rows, both parsers (the acceptance clause)
# --------------------------------------------------------------------------- #


def test_tfsa_clip_parses_to_zero_unmapped_in_both_parsers() -> None:
    """The TFSA clip: the same mapping keys as Mark's real export (20 unmapped
    rows of 91 before M113), 0 after — in the legacy parser the wizard actually
    runs by default, and in the profile."""
    rows, unmapped = _legacy(TFSA_CLIP)
    assert unmapped == [], [(u.activity_type, u.activity_sub_type, u.reason) for u in unmapped]
    assert len(rows) == 11

    profiled = _profiled(TFSA_CLIP)
    assert profiled.unmapped == []
    assert len(profiled.rows) == 11


def test_resp_clip_parses_to_zero_unmapped_in_both_parsers() -> None:
    """The RESP clip: the generic money-transfer-out was the last unmapped row
    in Mark's real export (1 before M113, 0 after)."""
    rows, unmapped = _legacy(RESP_CLIP)
    assert unmapped == [], [(u.activity_type, u.activity_sub_type, u.reason) for u in unmapped]
    assert len(rows) == 9

    profiled = _profiled(RESP_CLIP)
    assert profiled.unmapped == []
    assert len(profiled.rows) == 9


# --------------------------------------------------------------------------- #
# 2. (Tax, NRT) -> fee, cost stored as a magnitude
# --------------------------------------------------------------------------- #


# Only the TFSA export carries Tax/NRT rows (the RESP clip has none) — the
# withholding is a US-dividend thing.
@pytest.mark.parametrize("parser", ["legacy", "profile"])
def test_nrt_is_a_fee_row_stored_negative_with_money_out_effect(parser) -> None:
    """M131: a fee is money OUT, so it is stored at its NEGATIVE native sign
    (the file already signs NRT negative) and its cash effect is `-abs`, which
    is money out regardless of how a caller stored the row."""
    path = TFSA_CLIP
    rows, unmapped = _rows(path, parser)
    assert unmapped == []
    fees = _by_action(rows, TradeAction.fee)
    # The clip's withholding rows. The TRANSFER_FEE_REIMBURSEMENT credit is a
    # SEPARATE row and no longer lands as a fee at all (M132: a positive credit
    # is money in and becomes a cash transfer_in — docs/02 §trades), so this
    # `net_cash < 0` filter is exactly the NRT withholding.
    nrt = [row for row in fees if row.net_cash < 0]
    assert [row.price_native for row in nrt] == [D("-5.13"), D("-47.62")]
    for row in nrt:
        assert row.quantity == 0
        assert row.symbol == ""
        # The ROW carries the money-out sign now, and `-abs(price_native)` is
        # the cash effect — always money out, for both the repaired negative
        # rows and any legacy row stored positive.
        effect = _trade_cash_effect(
            _trade_cash(row.action, str(row.quantity), str(row.price_native))
        )
        assert effect == -abs(row.price_native)
        assert effect < 0


def test_nrt_cost_is_negative_in_both_parsers() -> None:
    """`abs_magnitudes` no longer rewrites a cash row's amount (M131), so the
    legacy path and the profile path both store the file's native (negative)
    NRT sign — the two parsers cannot drift."""
    legacy = _legacy(TFSA_CLIP)[0]
    profiled = _profiled(TFSA_CLIP).rows
    for collection in (legacy, profiled):
        assert [
            r.price_native for r in _by_action(collection, TradeAction.fee) if r.net_cash < 0
        ] == [
            D("-5.13"),
            D("-47.62"),
        ]


# --------------------------------------------------------------------------- #
# 3. Cash transfers: sign decides direction, quantity 0, no symbol
# --------------------------------------------------------------------------- #


def test_transfer_tf_positive_rows_are_cash_transfers_in() -> None:
    for parser in ("legacy", "profile"):
        rows, unmapped = _rows(TFSA_CLIP, parser)
        assert unmapped == []
        cash_in = [
            r
            for r in _by_action(rows, TradeAction.transfer_in)
            if r.symbol == ""  # the three TRANSFER_TF rows (VIU keeps its symbol)
        ]
        # M113: the three TRANSFER_TF money transfers stay exactly as they were.
        transfers = [r for r in cash_in if "transfer" in r.description.lower()]
        assert [r.price_native for r in transfers] == [
            D("564.27"),
            D("158.42"),
            D("19742.33"),
        ]
        # M132 (docs/02 §trades): the clip's TRANSFER_FEE_REIMBURSEMENT credit
        # ("CAD credited", +145.75) is money IN, so it joins this list instead
        # of masquerading as a fee that money left the account.
        assert [r.price_native for r in cash_in if "transfer" not in r.description.lower()] == [
            D("145.75")
        ]
        for row in cash_in:
            assert row.quantity == 0
            assert row.commission == 0
            assert (
                _trade_cash_effect(
                    _trade_cash(row.action, str(row.quantity), str(row.price_native))
                )
                > 0
            )


def test_negative_transfer_tf_is_a_cash_transfer_out() -> None:
    """Direction comes from net_cash_amount's sign — the file's negative case,
    which no export contains yet but the rule must already honour."""
    raw = NEGATIVE_TRANSFER_TF_CSV.encode()
    rows, unmapped = parse_investment_csv(raw)
    assert unmapped == []
    (row,) = rows
    assert row.action == "transfer_out"
    assert row.quantity == 0
    assert row.symbol == ""
    assert row.price_native == D("250.00")  # magnitude; the action is the sign
    assert _trade_cash_effect(
        _trade_cash(row.action, str(row.quantity), str(row.price_native))
    ) == D("-250.00")

    profiled = parse_brokerage_csv(raw, "wealthsimple", account_currency="CAD")
    assert profiled is not None
    assert profiled.unmapped == []
    assert profiled.rows[0].action == "transfer_out"
    assert profiled.rows[0].quantity == 0


def test_generic_money_transfer_out_needs_no_symbol() -> None:
    """RESP row 5: `Missing symbol for transfer_out` before M113 — the amount
    is cash, not shares, so the share-transfer symbol rule must not fire."""
    rows, unmapped = _legacy(RESP_CLIP)
    assert unmapped == []
    (out,) = _by_action(rows, TradeAction.transfer_out)
    assert out.symbol == ""
    assert out.quantity == 0
    assert out.price_native == D("134.52")
    assert out.net_cash == D("-134.52")
    assert _trade_cash_effect(
        _trade_cash(out.action, str(out.quantity), str(out.price_native))
    ) == D("-134.52")


# --------------------------------------------------------------------------- #
# 4. No regression: the rows that already worked stay byte-identical
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", [TFSA_CLIP, RESP_CLIP], ids=["tfsa", "resp"])
def test_pre_existing_rows_are_unchanged(path) -> None:
    legacy, legacy_unmapped = _rows(path, "legacy")
    profiled, profiled_unmapped = _rows(path, "profile")
    assert legacy_unmapped == [] and profiled_unmapped == []

    def key(row):
        return (
            row.date,
            row.action,
            row.symbol,
            row.quantity,
            row.price_native,
            row.commission,
            row.currency,
        )

    assert [key(r) for r in legacy] == [key(r) for r in profiled]

    # Spot-check the vocabulary that must not move (EFT/Grant deposits, the
    # gross dividends, signed buy/sell quantities).
    contributions = _by_action(legacy, TradeAction.contribution)
    assert contributions and all(r.price_native >= 0 for r in contributions)
    dividends = _by_action(legacy, TradeAction.dividend)
    assert dividends and all(r.price_native > 0 for r in dividends)
    buys = _by_action(legacy, TradeAction.buy)
    sells = _by_action(legacy, TradeAction.sell)
    assert buys[0].quantity > 0 and sells[0].quantity < 0
    assert buys[0].price_native > 0 and sells[0].price_native > 0


def test_share_transfers_keep_symbol_and_per_share_cost_basis() -> None:
    """SecurityTransfer is untouched by the cash-transfer rule: symbol
    required, non-zero quantity, unit_price = net_cash / quantity."""
    tfsa, tfsa_unmapped = _legacy(TFSA_CLIP)
    assert tfsa_unmapped == []
    (viu,) = [r for r in tfsa if r.symbol == "SYNA"]
    assert viu.action == "transfer_in"
    assert viu.quantity == D("320")
    assert viu.price_native == D("42.16")  # 13491.2 / 320
    assert viu.commission == 0

    resp, resp_unmapped = _legacy(RESP_CLIP)
    assert resp_unmapped == []
    (veqt,) = [r for r in resp if r.symbol == "SYNE" and r.action == "transfer_in"]
    assert veqt.quantity == D("400")
    assert veqt.price_native == D("53.85")  # 21540 / 400


def test_a_symbol_less_share_transfer_is_still_refused() -> None:
    """The symbol rule only relaxes for the MoneyMovement keys — a
    `SecurityTransfer` with no symbol must keep answering unmapped."""
    csv_text = (
        "effective_date,effective_time,settlement_date,account_id,account_type,activity_type,"
        "activity_sub_type,description,direction,symbol,name,currency,quantity,unit_price,"
        "commission,net_cash_amount\n"
        "2026-04-01,00:00:00,,ws-anon,TFSA,SecurityTransfer,-,"
        "Tax-free transfer of 5.0000 shares into the account,,,,CAD,5,,,125.00\n"
    )
    rows, unmapped = parse_investment_csv(csv_text.encode())
    assert rows == []
    assert len(unmapped) == 1
    assert "Missing symbol" in unmapped[0].reason


# --------------------------------------------------------------------------- #
# 5. The profile notice pins the fee-mapping decision (docs/41 notices list)
# --------------------------------------------------------------------------- #


def test_wealthsimple_notice_names_the_fee_mapping_and_eft_direction() -> None:
    """The WS profile's notices pin BOTH cash-vocabulary decisions: the NRT
    fee mapping (M113) and the EFT contribution-vs-withdrawal description
    rule (M131)."""
    profiled = _profiled(TFSA_CLIP)
    assert profiled.notices == [
        "Wealthsimple: non-resident tax (NRT) imports as a FEE against the gross "
        "dividend — the dividend row keeps its gross amount and the withholding "
        "shows as its own row, so no net dividend is fabricated.",
        "Wealthsimple: a MoneyMovement/EFT row is a contribution or a "
        'withdrawal according to its description ("Deposit" / "Withdrawal" / '
        '"Money transfer into|out of the account"); with no description the '
        "amount's sign decides, and a negative contribution is refused rather "
        "than imported.",
    ]


# --------------------------------------------------------------------------- #
# 6. ACB: a quantity-0 cash transfer_out must not blow up the replay
# --------------------------------------------------------------------------- #


def test_cash_transfer_out_replay_never_divides_by_zero(session: Session) -> None:
    """docs/06 replay allocates a share transfer_out over the held quantity —
    a CASH transfer row has quantity 0 and no holding behind it, so the old
    `pool.total_acb * 0 / 0` would raise (500) on commit in a taxable
    account. M113 makes that row a no-op on the pool instead."""
    user = User(email=f"m113-acb-{id(session)}@example.com", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    assert user.id is not None

    account = Account(
        user_id=user.id,
        name="Margin",
        type=AccountType.brokerage_non_registered,
        currency="CAD",
        taxable=True,
    )
    session.add(account)
    session.commit()
    session.refresh(account)

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

    session.add(
        Trade(
            user_id=user.id,
            account_id=account.id,
            security_id=cash.id,
            action=TradeAction.transfer_out,
            date=date(2025, 12, 22),
            quantity=D("0"),
            price_native=D("134.52"),
            commission=D("0"),
        )
    )
    session.commit()

    rebuild_user_pools(session, user.id)  # must not raise
    session.commit()


# --------------------------------------------------------------------------- #
# 7. End to end through POST /trades/import
# --------------------------------------------------------------------------- #


def _auth(client: TestClient) -> dict[str, str]:
    email = f"m113-ws-{time_ns()}@example.com"
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    body = client.post(
        f"{API}/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert body.status_code == 200
    return {"Authorization": f"Bearer {body.json()['access_token']}"}


def _account(client: TestClient, headers: dict, type_: str = "tfsa") -> dict:
    body = client.post(
        f"{API}/accounts",
        json={"name": f"M113 {type_}", "type": type_, "currency": "CAD"},
        headers=headers,
    )
    assert body.status_code == 201, body.text
    return body.json()


def _import(client: TestClient, headers: dict, account_id: int, path) -> dict:
    body = client.post(
        f"{API}/trades/import",
        files={"file": (path.name, path.read_bytes(), "text/csv")},
        data={"account_id": account_id},
        headers=headers,
    )
    assert body.status_code == 201, body.text
    return body.json()


def test_tfsa_import_endpoint_reports_zero_unmapped(client: TestClient) -> None:
    """Acceptance: a probe import of the TFSA clip against an account —
    20 unmapped rows before, 0 after."""
    headers = _auth(client)
    account = _account(client, headers, "tfsa")
    body = _import(client, headers, account["id"], TFSA_CLIP)
    assert body["unmapped"] == [], body["unmapped"]
    assert body["imported"] == 11
    assert body["batch_id"] > 0


def test_resp_import_commits_on_a_taxable_account(client: TestClient) -> None:
    """Stage + COMMIT the RESP clip on a NON-REGISTERED (taxable) account so
    the ACB replay really runs: the cash transfer_out lands with quantity 0,
    the in-kind transfers build pools, and the sell is covered by its own
    in-kind transfer. Before M113 the commit was unreachable (row 5 never
    imported); the guard is what keeps it a 200."""
    headers = _auth(client)
    account = _account(client, headers, "brokerage_non_registered")
    body = _import(client, headers, account["id"], RESP_CLIP)
    assert body["unmapped"] == [], body["unmapped"]
    assert body["imported"] == 9

    commit = client.post(f"{API}/trades/import/{body['batch_id']}/commit", headers=headers)
    assert commit.status_code == 200, commit.text

    # The re-import of a committed file must be all duplicates (docs/03).
    again = _import(client, headers, account["id"], RESP_CLIP)
    assert again["imported"] == 0
    assert again["duplicates_skipped"] == 9
