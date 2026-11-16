"""M76 YNAB migration behaviour tests (docs/38; M76 acceptance).

Fixture `ynab_budget.json` is a synthetic YNAB budget export that carries
deliberately broken rows (bad date, bad amount, deleted/unknown account,
unmapped account type) so the "never silently dropped" rule is asserted on
real input rather than on a happy path.
"""

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from app.api.deps import get_engine
from app.api.imports import _refuse_when_encrypted as _refuse_statement
from app.api.ynab import _refuse_when_encrypted as _refuse_ynab
from app.core.errors import APIError
from app.models.accounts import Account
from app.models.budgets import Budget
from app.models.categories import Category
from app.models.goals import Goal
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.tags import Tag
from app.models.transaction_tags import TransactionTag
from app.models.transactions import Transaction
from app.models.users import User

FIXTURES = Path(__file__).parent / "fixtures"
EMAIL = "miguel-ynab@example.com"
PASSWORD = "correct-horse-battery"

CONTENT_TYPES = {".json": "application/json", ".csv": "text/csv"}


def _register(client: TestClient) -> str:
    client.post("/api/v1/auth/register", json={"email": EMAIL, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _send(
    client: TestClient,
    token: str,
    route: str,
    fixture: str,
    form: dict | None = None,
    raw: bytes | None = None,
    expect_error: bool = False,
) -> dict:
    body = raw if raw is not None else (FIXTURES / fixture).read_bytes()
    suffix = Path(fixture).suffix
    response = client.post(
        route,
        headers=_headers(token),
        data={key: str(value) for key, value in (form or {}).items()},
        files={"file": (fixture, body, CONTENT_TYPES.get(suffix, "text/plain"))},
    )
    if expect_error:
        assert response.status_code == 422, response.text
        return response.json()
    assert response.status_code in (200, 201), response.text
    return response.json()


def _user_id(session: Session) -> int:
    user = session.exec(select(User).where(User.email == EMAIL)).first()
    assert user is not None
    return user.id


def _rows(session: Session, model) -> list:
    return session.exec(select(model)).all()


def _seed_categories(session: Session, user_id: int) -> dict[str, int]:
    """This user's `category name → id` map, and never the whole table's.

    The suite database is shared for the whole run and the `session` fixture
    only rolls THIS test's transaction back: a module that drives a live server
    or opens a connection of its own COMMITS a second tenant into the same
    database (`tests/test_m149_offloop_routes.py`, `tests/test_m155_price_session.py`).
    An unfiltered read therefore returns whichever row of a given NAME happens
    to sort last in the heap — which is heap noise, i.e. a 2-in-8 full-suite
    flake and 0 solo (t_a451f171).
    """
    return {c.name.lower(): c.id for c in _rows(session, Category) if c.user_id == user_id}


def _live_transactions(session: Session, user_id: int) -> int:
    """Rows a report would see: committed batches or manual (no batch) rows."""
    live = 0
    for txn in session.exec(select(Transaction).where(Transaction.user_id == user_id)).all():
        if txn.import_batch_id is None:
            live += 1
            continue
        batch = session.get(ImportBatch, txn.import_batch_id)
        if batch is not None and batch.status == ImportBatchStatus.committed:
            live += 1
    return live


def test_preview_maps_everything_and_writes_nothing(client: TestClient, session: Session) -> None:
    token = _register(client)
    user_id = _user_id(session)
    before = {
        "accounts": len(_rows(session, Account)),
        "categories": len(_rows(session, Category)),
        "tags": len(_rows(session, Tag)),
        "transactions": len(_rows(session, Transaction)),
        "batches": len(_rows(session, ImportBatch)),
        "budgets": len(_rows(session, Budget)),
        "goals": len(_rows(session, Goal)),
    }

    plan = _send(client, token, "/api/v1/imports/ynab/preview", "ynab_budget.json")

    after = {
        "accounts": len(_rows(session, Account)),
        "categories": len(_rows(session, Category)),
        "tags": len(_rows(session, Tag)),
        "transactions": len(_rows(session, Transaction)),
        "batches": len(_rows(session, ImportBatch)),
        "budgets": len(_rows(session, Budget)),
        "goals": len(_rows(session, Goal)),
    }
    assert before == after, "preview must not write a single row"
    # Scoped to THIS user: an absolute count over the table is order-dependent
    # (the suite database always carries another tenant — conftest `_decoy_tenant`).
    assert not [t for t in _rows(session, Transaction) if t.user_id == user_id]

    assert plan["source_format"] == "json"
    assert plan["budget_name"] == "Household 2026"
    assert plan["currency"] == "CAD"
    counts = plan["counts"]
    assert counts["accounts_create"] == 3  # Chequing, MasterCard, Vacation Fund
    assert counts["accounts_unmapped"] == 2  # deleted Old Loan + unknown type
    assert counts["groups_create"] == 3
    assert counts["categories_create"] == 3  # Rent, Fun Money, Salary
    assert counts["categories_reuse"] == 1  # "Groceries" matches the seed category
    assert counts["tags_create"] == 3
    assert counts["budgets_create"] == 1  # Rent: monthly funding target
    assert counts["goals_create"] == 1  # Groceries: target balance by date
    assert counts["targets_unsupported"] == 1  # NEED goal flagged, not dropped

    txns = plan["transactions"]
    assert txns["total"] == 16
    assert txns["importable"] == 10
    assert txns["duplicates"] == 0
    assert txns["duplicates_pending"] == 10  # every target account is new here
    assert txns["transfer_pairs"] == 1
    assert txns["rows_truncated"] is False
    assert txns["warnings_truncated"] is False
    assert len(txns["rows"]) == 10

    assert sorted(u["code"] for u in txns["unmappable"]) == [
        "account_unmappable",  # row 6: account marked deleted in YNAB
        "account_unmappable",  # row 14: account type with no equivalent
        "invalid_amount",  # row 9
        "invalid_date",  # row 8
        "unknown_account",  # row 7: account id not in the export
    ]
    assert sorted(w["code"] for w in txns["warnings"]) == [
        "cleared_not_recognized",
        "missing_payee",
        "split_detail_dropped",
        "unknown_category",
        "unknown_payee",
    ]
    # Every unmappable row points at its source row and explains itself.
    assert sorted(u["index"] for u in txns["unmappable"]) == [6, 7, 8, 9, 14]
    assert all(u["reason"] for u in txns["unmappable"])
    assert "deleted in YNAB" in _issue(txns["unmappable"], 6)["reason"]

    assert sorted((t["kind"], t["action"]) for t in plan["targets"]) == [
        ("budget", "create"),
        ("goal", "create"),
        ("unsupported", "skipped"),
    ]
    cleared = {row["index"]: row["cleared"] for row in txns["rows"]}
    assert cleared[1] == "reconciled"
    assert cleared[4] == "uncleared"
    assert cleared[13] is None  # unrecognised value → warning, not a silent pass
    assert user_id > 0


def test_commit_creates_accounts_categories_and_staged_rows(
    client: TestClient, session: Session
) -> None:
    token = _register(client)
    user_id = _user_id(session)
    # Scoped to THIS user: a suite run shares the database with every earlier
    # test, and an unscoped name→id snapshot here picked up a foreign tenant's
    # "groceries" row once (its id shadowed our own in the dict), failing the
    # reuse check below with no defect in the import. The tenant in question was
    # `tests/test_m149_offloop_routes.py`'s live-server register, which committed
    # a second user — with the same 10 default category names — and never took
    # it away: that leak is fixed there, and
    # `conftest._guard_no_committed_tenant_survives` now fails any run that
    # leaves a committed tenant behind (t_a451f171). Every other read in this
    # test is user-scoped for the same reason.
    seed_categories = _seed_categories(session, user_id)
    assert len(seed_categories) == 10

    result = _send(client, token, "/api/v1/imports/ynab/commit", "ynab_budget.json")

    assert result["net_new"] == 10
    assert result["duplicates_skipped"] == 0
    assert sorted(b["imported"] for b in result["batches"]) == [1, 2, 7]
    assert all(b["unresolved"] == 0 for b in result["batches"])

    accounts = {a.name: a for a in _rows(session, Account) if a.user_id == user_id}
    assert set(accounts) == {"Chequing", "MasterCard", "Vacation Fund"}
    assert accounts["MasterCard"].type.value == "credit_card"
    assert accounts["Chequing"].type.value == "chequing"
    assert accounts["Vacation Fund"].type.value == "savings"
    # No starting_balance: balances are rebuilt from the imported history.
    assert accounts["Chequing"].starting_balance is None

    categories = {c.name.lower(): c for c in _rows(session, Category) if c.user_id == user_id}
    # 10 seeded + 3 group parent categories + 3 children (Groceries reused)
    assert len(categories) == 16
    assert categories["groceries"].id == seed_categories["groceries"]
    rent = categories["rent"]
    assert rent.parent_id is not None
    assert categories["monthly bills"].id == rent.parent_id
    assert categories["salary"].parent_id == categories["internal master category"].id

    assert {t.name for t in _rows(session, Tag) if t.user_id == user_id} == {
        "Monthly Bills",
        "Everyday",
        "Internal Master Category",
    }

    budgets = [b for b in _rows(session, Budget) if b.user_id == user_id]
    assert len(budgets) == 1
    assert budgets[0].amount == Decimal("1500.00")
    assert budgets[0].period == "monthly"
    assert budgets[0].category_id == rent.id

    goals = [g for g in _rows(session, Goal) if g.user_id == user_id]
    assert len(goals) == 1
    assert goals[0].name == "Groceries target"
    assert goals[0].target_amount == Decimal("400.00")
    assert goals[0].target_date == "2026-12-31"

    staged = [t for t in _rows(session, Transaction) if t.user_id == user_id]
    assert len(staged) == 10
    assert all(t.import_batch_id is not None for t in staged)
    assert _live_transactions(session, user_id) == 0  # still staged (docs/03)

    by_description = {t.raw_description: t for t in staged}
    rent_row = by_description["ACME Landlord — January rent"]
    assert rent_row.amount_native == Decimal("-1500.00")
    assert rent_row.cad_amount == Decimal("-1500.00")
    assert rent_row.cleared == "reconciled"
    assert rent_row.category_id == rent.id
    assert by_description["ACME Payroll — paycheque"].amount_native == Decimal("4200.00")
    assert by_description["see receipt"].normalized_payee == "see receipt"
    assert by_description["(YNAB import)"].cleared == "uncleared"
    # Unrecognised cleared value stays NULL rather than being guessed at.
    null_cleared = [t for t in staged if t.cleared is None]
    assert len(null_cleared) == 1
    assert null_cleared[0].raw_description == "Corner Market"

    # Transfer pair linked across the two accounts.
    pairs = {t.transfer_pair_id for t in staged if t.transfer_pair_id is not None}
    assert len(pairs) == 2
    paired = [t for t in staged if t.transfer_pair_id is not None]
    assert {p.account_id for p in paired} == {accounts["Chequing"].id, accounts["Vacation Fund"].id}
    assert paired[0].transfer_pair_id == paired[1].id

    # Group tags land on the rows of their categories. The junction carries no
    # user_id, so it is scoped through the transactions of THIS user.
    staged_ids = {t.id for t in staged}
    links = [link for link in _rows(session, TransactionTag) if link.transaction_id in staged_ids]
    assert len(links) == 5
    tagged = {link.transaction_id for link in links}
    assert tagged.issubset(staged_ids)

    batches = [b for b in _rows(session, ImportBatch) if b.user_id == user_id]
    assert len(batches) == 3
    assert all(b.status == ImportBatchStatus.pending_review for b in batches)
    # The batch records the file the user actually uploaded (docs/03).
    assert all(b.file_name == "ynab_budget.json" for b in batches)


def test_seed_ids_hold_when_another_tenant_is_committed_into_the_same_database(
    client: TestClient, session: Session
) -> None:
    """The deterministic half of a 2-in-8 full-suite red (t_a451f171).

    `test_commit_...` compares this user's seeded "groceries" against the id it
    read BEFORE the import, so that read has to be scoped: the suite database is
    shared for the whole run and `tests/test_m149_offloop_routes.py` drives a
    live server whose `POST /auth/register` COMMITS a second tenant — the same
    10 default category names — where the `session` fixture's rollback cannot
    reach it. WHICH of the two "groceries" rows an unfiltered read returns is
    heap noise; a scoped read does not depend on it, and that is what this pins.
    """
    token = _register(client)
    user_id = _user_id(session)
    mine = _seed_categories(session, user_id)
    assert len(mine) == 10

    # A foreign tenant committed through a connection of its OWN — the exact
    # shape m149 leaves behind — and deleted again below.
    engine = get_engine()
    with Session(engine, expire_on_commit=False) as setup:
        stranger = User(email="ynab-stranger@example.com", hashed_password="x")
        setup.add(stranger)
        setup.commit()
        setup.refresh(stranger)
        stranger_id = stranger.id
        assert stranger_id is not None
        setup.add(Category(user_id=stranger_id, name="Groceries", is_system=True))
        setup.commit()

    try:
        _send(client, token, "/api/v1/imports/ynab/commit", "ynab_budget.json")
        # 10 seeded + 3 group parents + 3 children (Groceries reused) — and the
        # 10 seeded rows kept THEIR ids, with no foreign id anywhere among them.
        after = _seed_categories(session, user_id)
        assert len(after) == 16
        assert {name: after[name] for name in mine} == mine, (
            "this user's seed ids moved because another tenant was committed"
        )
        categories = {c.name.lower(): c for c in _rows(session, Category) if c.user_id == user_id}
        assert categories["groceries"].id == mine["groceries"]
    finally:
        with Session(engine) as teardown:
            teardown.exec(delete(Category).where(Category.user_id == stranger_id))
            teardown.exec(delete(User).where(User.id == stranger_id))
            teardown.commit()


def test_reimport_is_idempotent(client: TestClient, session: Session) -> None:
    token = _register(client)
    user_id = _user_id(session)

    first = _send(client, token, "/api/v1/imports/ynab/commit", "ynab_budget.json")
    assert first["net_new"] == 10
    for batch in first["batches"]:
        response = client.post(
            f"/api/v1/imports/{batch['batch_id']}/commit", headers=_headers(token)
        )
        assert response.status_code == 200, response.text
    assert _live_transactions(session, user_id) == 10

    second = _send(client, token, "/api/v1/imports/ynab/commit", "ynab_budget.json")
    counts = second["plan"]["counts"]
    assert counts["accounts_create"] == 0 and counts["accounts_reuse"] == 3
    assert counts["categories_create"] == 0 and counts["categories_reuse"] == 4
    assert counts["groups_create"] == 0 and counts["groups_reuse"] == 3
    assert counts["tags_create"] == 0 and counts["tags_reuse"] == 3
    assert counts["budgets_create"] == 0 and counts["budgets_reuse"] == 1
    assert counts["goals_create"] == 0 and counts["goals_reuse"] == 1
    assert second["plan"]["transactions"]["duplicates"] == 10
    assert second["plan"]["transactions"]["duplicates_pending"] == 0
    assert second["net_new"] == 0
    assert second["duplicates_skipped"] == 10

    for batch in second["batches"]:
        response = client.post(
            f"/api/v1/imports/{batch['batch_id']}/commit", headers=_headers(token)
        )
        assert response.status_code == 200, response.text
    assert _live_transactions(session, user_id) == 10, "re-import must add no live row"
    assert len([a for a in _rows(session, Account) if a.user_id == user_id]) == 3
    assert len([c for c in _rows(session, Category) if c.user_id == user_id]) == 16


def test_unmappable_rows_are_reported_never_skipped(client: TestClient, session: Session) -> None:
    token = _register(client)
    user_id = _user_id(session)
    plan = _send(client, token, "/api/v1/imports/ynab/preview", "ynab_budget.json")
    unmappable = plan["transactions"]["unmappable"]

    by_index = {row["index"]: row for row in unmappable}
    assert set(by_index) == {6, 7, 8, 9, 14}
    assert by_index[6]["code"] == "account_unmappable"
    assert "Old Loan" in by_index[6]["reason"]
    assert by_index[7]["code"] == "unknown_account"
    assert by_index[8]["code"] == "invalid_date"
    assert "2026-13-45" in by_index[8]["reason"]
    assert by_index[9]["code"] == "invalid_amount"
    assert "not-a-number" in by_index[9]["reason"]
    assert by_index[14]["code"] == "account_unmappable"
    assert "otherWeird" in by_index[14]["reason"]

    # Committing must not smuggle those rows in as transactions.
    result = _send(client, token, "/api/v1/imports/ynab/commit", "ynab_budget.json")
    assert result["net_new"] == 10
    assert len([t for t in _rows(session, Transaction) if t.user_id == user_id]) == 10
    assert len(result["plan"]["transactions"]["unmappable"]) == 5


def test_csv_register_imports_categories_tags_and_flags_bad_rows(
    client: TestClient, session: Session
) -> None:
    token = _register(client)
    user_id = _user_id(session)

    plan = _send(client, token, "/api/v1/imports/ynab/preview", "ynab_register.csv")
    assert plan["source_format"] == "csv"
    assert plan["date_format"] == "iso"
    counts = plan["counts"]
    assert counts["accounts_create"] == 1
    assert counts["groups_create"] == 1  # Everyday
    assert counts["groups_reuse"] == 1  # Income matches the seeded category
    assert counts["categories_create"] == 2  # Salary, Snacks
    assert counts["categories_reuse"] == 1  # Groceries matches the seed row
    assert counts["tags_create"] == 2
    assert plan["targets"] == []  # a register export carries no targets
    assert plan["accounts"][0]["type_assumed"] is True
    assert plan["accounts"][0]["mapped_type"] == "chequing"
    assert {c["name"] for c in plan["categories"]} == {"Groceries", "Salary", "Snacks"}
    assert plan["transactions"]["importable"] == 3
    assert [row["code"] for row in plan["transactions"]["unmappable"]] == ["invalid_amount"]
    assert plan["transactions"]["unmappable"][0]["index"] == 4

    result = _send(client, token, "/api/v1/imports/ynab/commit", "ynab_register.csv")
    assert result["net_new"] == 3
    assert result["duplicates_skipped"] == 0
    staged = [t for t in _rows(session, Transaction) if t.user_id == user_id]
    assert len(staged) == 3
    assert {t.raw_description for t in staged} == {
        "Corner Market — Weekly shop",
        "ACME Payroll",
        "Corner Market",
    }
    assert {t.cleared for t in staged} == {"cleared", "reconciled", "uncleared"}
    account = next(a for a in _rows(session, Account) if a.user_id == user_id)
    assert account.name == "Chequing"
    assert {t.account_id for t in staged} == {account.id}
    staged_ids = {t.id for t in staged}
    assert len([f for f in _rows(session, TransactionTag) if f.transaction_id in staged_ids]) == 3


def test_csv_without_account_column_uses_the_supplied_account(
    client: TestClient, session: Session
) -> None:
    token = _register(client)
    user_id = _user_id(session)
    created = client.post(
        "/api/v1/accounts",
        headers=_headers(token),
        json={"name": "Joint", "type": "chequing", "currency": "CAD"},
    )
    assert created.status_code == 201, created.text
    account_id = created.json()["id"]

    raw = b"Date,Payee,Outflow,Inflow\n2026-02-01,Corner Market,12.34,\n"
    plan = _send(
        client,
        token,
        "/api/v1/imports/ynab/preview",
        "inline.csv",
        form={"account_id": account_id},
        raw=raw,
    )
    assert plan["transactions"]["importable"] == 1
    assert plan["accounts"][0]["action"] == "reuse"
    assert plan["accounts"][0]["existing_id"] == account_id
    assert plan["transactions"]["unmappable"] == []

    result = _send(
        client,
        token,
        "/api/v1/imports/ynab/commit",
        "inline.csv",
        form={"account_id": account_id},
        raw=raw,
    )
    assert result["net_new"] == 1
    staged = [t for t in _rows(session, Transaction) if t.user_id == user_id]
    assert len(staged) == 1
    assert staged[0].account_id == account_id
    assert staged[0].amount_native == Decimal("-12.34")


def test_ambiguous_csv_dates_are_assumed_with_a_note(client: TestClient, session: Session) -> None:
    token = _register(client)
    plan = _send(client, token, "/api/v1/imports/ynab/preview", "ynab_register_ambiguous.csv")
    codes = [note["code"] for note in plan["notes"]]
    assert "date_format_assumed" in codes
    note = next(n for n in plan["notes"] if n["code"] == "date_format_assumed")
    assert "date_format" in note["reason"]
    dates = sorted(str(row["date"]) for row in plan["transactions"]["rows"])
    assert dates == ["2026-01-02", "2026-01-03"]  # month-first, YNAB's default
    assert plan["transactions"]["importable"] == 2
    assert plan["transactions"]["unmappable"] == []


def test_ambiguous_csv_dates_can_be_pinned_with_date_format(
    client: TestClient, session: Session
) -> None:
    token = _register(client)
    created = client.post(
        "/api/v1/accounts",
        headers=_headers(token),
        json={"name": "Pinned", "type": "chequing", "currency": "CAD"},
    )
    assert created.status_code == 201, created.text
    plan = _send(
        client,
        token,
        "/api/v1/imports/ynab/preview",
        "inline.csv",
        form={"date_format": "%d/%m/%Y", "account_id": created.json()["id"]},
        raw=b"Date,Payee,Outflow,Inflow\n02/01/2026,Corner Market,12.34,\n",
    )
    assert plan["transactions"]["importable"] == 1
    assert str(plan["transactions"]["rows"][0]["date"]) == "2026-01-02"
    assert "date_format_assumed" not in [note["code"] for note in plan["notes"]]


def test_parse_and_validation_errors_use_the_error_envelope(
    client: TestClient, session: Session
) -> None:
    token = _register(client)

    not_ynab = client.post(
        "/api/v1/imports/ynab/preview",
        headers=_headers(token),
        files={"file": ("other.json", b'{"foo": 1}', "application/json")},
    )
    assert not_ynab.status_code == 422
    assert not_ynab.json()["code"] == "ynab_parse_error"

    bad_json = client.post(
        "/api/v1/imports/ynab/preview",
        headers=_headers(token),
        files={"file": ("broken.json", b"{not json", "application/json")},
    )
    assert bad_json.status_code == 422
    assert bad_json.json()["code"] == "ynab_parse_error"

    no_header = client.post(
        "/api/v1/imports/ynab/preview",
        headers=_headers(token),
        files={"file": ("rows.csv", b"just,some,cells\n1,2,3\n", "text/csv")},
    )
    assert no_header.status_code == 422
    assert no_header.json()["code"] == "ynab_parse_error"

    bad_type = _send(
        client,
        token,
        "/api/v1/imports/ynab/preview",
        "ynab_register.csv",
        form={"account_type": "crypto"},
        expect_error=True,
    )
    assert bad_type["code"] == "invalid_account_type"

    bad_format = _send(
        client,
        token,
        "/api/v1/imports/ynab/preview",
        "ynab_register.csv",
        form={"date_format": "someday"},
        expect_error=True,
    )
    assert bad_format["code"] == "invalid_date_format"

    bad_currency = _send(
        client,
        token,
        "/api/v1/imports/ynab/preview",
        "ynab_register.csv",
        form={"currency": "dollars"},
        expect_error=True,
    )
    assert bad_currency["code"] == "invalid_currency"

    oversized = client.post(
        "/api/v1/imports/ynab/preview",
        headers=_headers(token),
        files={"file": ("big.json", b"{" + b"a" * 5_000_010 + b"}", "application/json")},
    )
    assert oversized.status_code == 413
    assert oversized.json()["code"] == "file_too_large"


def test_commit_is_refused_for_encryption_required_tenants(
    client: TestClient, session: Session
) -> None:
    token = _register(client)
    user = session.exec(select(User).where(User.email == EMAIL)).first()
    assert user is not None
    user.encryption_required = True
    session.add(user)
    session.flush()

    body = client.post(
        "/api/v1/imports/ynab/commit",
        headers=_headers(token),
        files={
            "file": (
                "ynab_budget.json",
                (FIXTURES / "ynab_budget.json").read_bytes(),
                "application/json",
            )
        },
    )
    assert body.status_code == 422
    assert body.json()["code"] == "encryption_required"
    assert len([t for t in _rows(session, Transaction) if t.user_id == user.id]) == 0
    # The DETAIL carries the shared server-side refusal shape: the capability
    # phrase `ServerSideBlocked.tsx` pre-renders for `import:ynab`, the
    # endpoint's own words, and no unlock remedy (docs/33 rule 7d).
    detail = body.json()["detail"]
    assert detail.startswith("Importing a YNAB budget file is unavailable while ")
    assert "cannot safely write encrypted data, so nothing was saved" in detail
    assert "unlock" not in detail.lower()


class _Enrolled:
    """Only the guard's own field is read, so a stand-in beats a persisted user."""

    encryption_required = True


def _detail(helper, user, capability: str) -> str:
    """Run one guard on an ENROLLED tenant and return its refusal detail."""
    try:
        helper(user, capability)
    except APIError as error:
        assert error.status_code == 422
        assert error.code == "encryption_required"
        return error.detail
    raise AssertionError(f"{helper.__module__} did not refuse an enrolled tenant")


def test_ynab_refusal_carries_the_shared_server_side_phrase() -> None:
    """The `import:ynab` detail must be the SAME sentence the other guards send.

    `ServerSideBlocked.tsx` tells an enrolled tenant, before any submit, that
    "<capability> is unavailable while client-side encryption is enabled: this
    path runs server-side and cannot safely write encrypted data." — claiming
    to be the endpoint's detail minus only its trailing "…so nothing was saved"
    (docs/09 §Server-side writes, security findings F3/F4). The helper lives in
    two routers (imports, ynab), so pin the equality directly instead of
    trusting two copies to stay in step; a paraphrase on either side goes red.
    """
    capability = "Importing a YNAB budget file"
    enrolled = _Enrolled()
    assert _detail(_refuse_ynab, enrolled, capability) == _detail(
        _refuse_statement, enrolled, capability
    )


def test_commit_enforces_the_shared_row_cap(client: TestClient, session: Session) -> None:
    """The 2000-row cap belongs to the shared pipeline, so a YNAB pull hits it too.

    Before M79's follow-up the YNAB path re-implemented staging inline and had
    no cap at all (only the file-level 50 000-row guard). One account over
    `MAX_STAGED_ROWS` now returns `413 too_many_rows` and the ONE-transaction
    migration rolls back — no accounts, categories, tags, batches or rows.
    """
    from app.services.import_staging import MAX_STAGED_ROWS

    token = _register(client)
    created = client.post(
        "/api/v1/accounts",
        headers=_headers(token),
        json={"name": "Bulk", "type": "chequing", "currency": "CAD"},
    )
    assert created.status_code == 201, created.text
    account_id = created.json()["id"]

    header = "Date,Payee,Outflow,Inflow\n"
    body = header.encode()
    for index in range(MAX_STAGED_ROWS + 1):
        day = date(2020, 1, 1) + timedelta(days=index)
        body += f"{day.isoformat()},Bulk payee {index},{index + 1}.00,\n".encode()

    before = {
        model: len(_rows(session, model))
        for model in (Account, Category, Tag, Transaction, ImportBatch, Budget, Goal)
    }

    response = client.post(
        "/api/v1/imports/ynab/commit",
        headers=_headers(token),
        data={"account_id": str(account_id)},
        files={"file": ("bulk.csv", body, "text/csv")},
    )
    assert response.status_code == 413, response.text
    assert response.json()["code"] == "too_many_rows"
    assert str(MAX_STAGED_ROWS) in response.json()["detail"]

    after = {
        model: len(_rows(session, model))
        for model in (Account, Category, Tag, Transaction, ImportBatch, Budget, Goal)
    }
    assert before == after, "a rejected migration must leave nothing behind"


def _issue(issues: list[dict], index: int) -> dict:
    return next(issue for issue in issues if issue["index"] == index)
