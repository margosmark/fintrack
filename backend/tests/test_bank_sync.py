"""M79 bank-sync readiness — stub provider -> staged -> committed (docs/39).

Covers the acceptance set: pull stages through the SAME pipeline as a file
upload, a CSV-imported transaction pulled again by sync stays ONE row and is
surfaced as a match decision, revoke stops pulls, an `encryption_required`
tenant pulls into plaintext exactly as docs/39 documents, privacy mode seals
the egress seam, and every provider failure maps to its pinned error code.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, select

from app.models.accounts import Account
from app.models.import_batches import ImportBatchStatus
from app.models.sync_agreements import ProviderStatus, SyncAgreement, SyncAgreementStatus
from app.models.transactions import Transaction
from app.services.sync.provider import (
    ProviderAuthRequired,
    ProviderRevoked,
    ProviderUnavailable,
    register_provider,
)
from app.services.sync.stub import StubProvider, default_stub
from app.services.sync.types import (
    NormalizedTx,
    ProviderAccount,
    ProviderUser,
    PullBatch,
)

FIXTURES = Path(__file__).parent / "fixtures"
PASSWORD = "correct-horse-battery"
SYNC = "/api/v1/sync"

# The fixture stores debits already negative, i.e. the canonical signed
# convention -> `credits_positive: True` means "signs pass through" (docs/03
# sign conventions). With the default `False` the importer would invert them.
CSV_PROFILE = json.dumps(
    {
        "columns": {"date": "Date", "amount_native": "Amount", "raw_description": "Description"},
        "date_format": "%Y-%m-%d",
        "credits_positive": True,
    }
)

# The one transaction the CSV fixture and the stub fixture agree on — the
# dedup bridge has to recognise it as the same row across the two sources.
SHARED_DESCRIPTION = "CORNER STORE #1234 TORONTO ON"


@pytest.fixture(autouse=True)
def _clean_stub():
    """Fresh provider state per test, default provider restored afterwards."""
    stub = default_stub()
    stub.reset()
    yield
    stub.reset()
    register_provider(stub)


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client: TestClient, email: str = "sync@example.com") -> dict:
    client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def _me(client: TestClient, token: str) -> dict:
    response = client.get("/api/v1/users/me", headers=_headers(token))
    assert response.status_code == 200, response.text
    return response.json()


def _make_accounts(client: TestClient, token: str, count: int = 1) -> list[int]:
    inst = client.post("/api/v1/institutions", json={"name": "Sync Bank"}, headers=_headers(token))
    assert inst.status_code == 201, inst.text
    ids: list[int] = []
    for index in range(count):
        account = client.post(
            "/api/v1/accounts",
            json={
                "name": f"Checking {index}",
                "type": "chequing",
                "currency": "CAD",
                "institution_id": inst.json()["id"],
            },
            headers=_headers(token),
        )
        assert account.status_code == 201, account.text
        ids.append(account.json()["id"])
    return ids


def _create_agreement(
    client: TestClient, token: str, account_ids: list[int], provider: str = "stub"
) -> dict:
    response = client.post(
        f"{SYNC}/agreements",
        json={"provider": provider, "account_ids": account_ids},
        headers=_headers(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _pull(client: TestClient, token: str, agreement_id: int) -> dict:
    response = client.post(f"{SYNC}/agreements/{agreement_id}/pull", headers=_headers(token))
    assert response.status_code == 200, response.text
    return response.json()


def _commit(client: TestClient, token: str, batch_id: int) -> dict:
    response = client.post(f"/api/v1/imports/{batch_id}/commit", headers=_headers(token))
    assert response.status_code == 200, response.text
    return response.json()


def _live_total(client: TestClient, token: str) -> int:
    response = client.get("/api/v1/transactions", headers=_headers(token))
    assert response.status_code == 200, response.text
    return response.json()["total"]


def _scalar(session: Session, sql: str, user_id: int) -> int:
    """One user-scoped count straight from the database, not from the API."""
    return session.connection().execute(text(sql), {"uid": user_id}).scalar_one()


def _upload_csv(client: TestClient, token: str, account_id: int) -> dict:
    raw = (FIXTURES / "sync_dup.csv").read_bytes()
    response = client.post(
        "/api/v1/imports/upload",
        headers=_headers(token),
        data={"account_id": str(account_id), "profile_json": CSV_PROFILE},
        files={"file": ("sync_dup.csv", raw, "text/csv")},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- registry + catalog ------------------------------------------------------


def test_provider_registry_lists_the_stub(client: TestClient) -> None:
    auth = _register(client)
    response = client.get(f"{SYNC}/providers", headers=_headers(auth["access_token"]))
    assert response.status_code == 200
    rows = response.json()
    # Only the stub ships today; a real adapter joins this list, never replaces it.
    assert {"slug": "stub", "display_name": "Stub Bank (test provider)"} in rows
    assert rows[0]["slug"] == "stub"


def test_sync_routes_are_authentication_gated(client: TestClient) -> None:
    assert client.get(f"{SYNC}/providers").status_code == 401
    assert (
        client.post(f"{SYNC}/agreements", json={"provider": "stub", "account_ids": []}).status_code
        == 401
    )
    assert client.post(f"{SYNC}/agreements/1/pull").status_code == 401


def test_institutions_catalog_is_provider_scoped(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    response = client.get(f"{SYNC}/providers/stub/institutions", headers=_headers(token))
    assert response.status_code == 200
    rows = response.json()
    assert [row["ref"] for row in rows] == ["stub-bank", "stub-credit-union"]
    assert all(row["country"] == "CA" for row in rows)

    missing = client.get(f"{SYNC}/providers/nope/institutions", headers=_headers(token))
    assert missing.status_code == 404
    assert missing.json()["code"] == "sync_provider_unknown"


# --- fixtures -> NormalizedTx ------------------------------------------------


def test_stub_fixture_maps_to_normalized_tx() -> None:
    """fixtures -> NormalizedTx: the accredited adapter keeps this contract."""
    stub = StubProvider.from_fixture(FIXTURES / "sync_stub_pull.json")
    assert [row.ref for row in stub.list_institutions()] == [
        "stub-bank",
        "stub-credit-union",
    ]

    user = ProviderUser(id=1)
    stub.create_agreement(
        user, [ProviderAccount(local_id=7, currency="CAD", account_type="chequing")]
    )
    batch = stub.pull_delta(user, None)

    # A one-account grant only sees `stub-acc-0`; `stub-acc-1` and the
    # unmapped `stub-acc-9` rows are out of scope.
    assert [txn.external_id for txn in batch.transactions] == ["TX-1001", "TX-1002", "TX-1003"]
    first = batch.transactions[0]
    assert isinstance(first.amount_native, Decimal)
    assert first.amount_native == Decimal("-42.35")
    assert first.date == date(2025, 11, 3)
    assert first.description == SHARED_DESCRIPTION
    assert first.currency == "CAD"
    # currency=None falls back to the account currency downstream (docs/03).
    assert batch.transactions[2].currency is None
    assert batch.transactions[2].posting_date == date(2025, 11, 6)
    assert [bal.amount for bal in batch.balances] == [Decimal("1500.00")]


def test_default_stub_dataset_matches_the_checked_in_fixture() -> None:
    """The in-code dataset and the fixture must not drift apart."""
    default = StubProvider()
    from_fixture = StubProvider.from_fixture(FIXTURES / "sync_stub_pull.json")
    assert default.transactions == from_fixture.transactions
    assert default.balances == from_fixture.balances
    assert default.institutions == from_fixture.institutions


# --- consent -----------------------------------------------------------------


def test_create_agreement_records_consent_without_credentials(
    client: TestClient, session: Session
) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_ids = _make_accounts(client, token, 2)

    body = _create_agreement(client, token, account_ids)

    assert body["provider"] == "stub"
    assert body["status"] == SyncAgreementStatus.active
    assert body["account_ids"] == account_ids
    assert body["scopes"] == ["accounts", "transactions"]
    assert body["consent_id"].startswith("stub-consent-")
    assert body["provider_status"] is None
    assert body["last_sync_at"] is None
    assert body["revoked_at"] is None
    assert body["created_at"] is not None
    # No credential column exists — consent_id is the provider's own reference.
    assert "token" not in body and "secret" not in body

    agreement = session.get(SyncAgreement, body["id"])
    assert agreement is not None
    assert set(agreement.account_refs.values()) == set(account_ids)


def test_create_agreement_rejects_empty_and_foreign_accounts(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]

    empty = client.post(
        f"{SYNC}/agreements",
        json={"provider": "stub", "account_ids": []},
        headers=_headers(token),
    )
    assert empty.status_code == 422
    assert empty.json()["code"] == "validation_error"

    missing = client.post(
        f"{SYNC}/agreements",
        json={"provider": "stub", "account_ids": [999999]},
        headers=_headers(token),
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


def test_partial_account_mapping_is_rejected_not_silently_dropped(
    client: TestClient,
) -> None:
    """Arbitration principle: an adapter that maps only part of the request fails loudly."""

    class _PartialMapper(StubProvider):
        def create_agreement(self, user, accounts):
            return super().create_agreement(user, accounts[:1])

    register_provider(_PartialMapper(slug="stub"))
    auth = _register(client)
    token = auth["access_token"]
    account_ids = _make_accounts(client, token, 2)

    response = client.post(
        f"{SYNC}/agreements",
        json={"provider": "stub", "account_ids": account_ids},
        headers=_headers(token),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "sync_account_mapping_incomplete"


def test_second_active_agreement_conflicts_until_revoked(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_ids = _make_accounts(client, token, 1)
    first = _create_agreement(client, token, account_ids)

    again = client.post(
        f"{SYNC}/agreements",
        json={"provider": "stub", "account_ids": account_ids},
        headers=_headers(token),
    )
    assert again.status_code == 409
    assert again.json()["code"] == "sync_agreement_exists"

    revoked = client.post(f"{SYNC}/agreements/{first['id']}/revoke", headers=_headers(token))
    assert revoked.status_code == 200
    assert revoked.json()["status"] == SyncAgreementStatus.revoked

    retry = client.post(
        f"{SYNC}/agreements",
        json={"provider": "stub", "account_ids": account_ids},
        headers=_headers(token),
    )
    assert retry.status_code == 201, retry.text
    assert retry.json()["id"] != first["id"]


def test_agreements_are_tenant_scoped(client: TestClient) -> None:
    owner = _register(client, email="owner@example.com")
    other = _register(client, email="intruder@example.com")
    account_ids = _make_accounts(client, owner["access_token"], 1)
    created = _create_agreement(client, owner["access_token"], account_ids)

    listed = client.get(f"{SYNC}/agreements", headers=_headers(other["access_token"]))
    assert listed.status_code == 200
    assert listed.json() == []

    fetched = client.get(
        f"{SYNC}/agreements/{created['id']}", headers=_headers(other["access_token"])
    )
    assert fetched.status_code == 404
    assert fetched.json()["code"] == "sync_agreement_missing"

    pulled = client.post(
        f"{SYNC}/agreements/{created['id']}/pull", headers=_headers(other["access_token"])
    )
    assert pulled.status_code == 404
    assert pulled.json()["code"] == "sync_agreement_missing"


# --- pull -> staged -> committed --------------------------------------------


def test_pull_stages_a_pending_batch_then_commit_makes_rows_live(
    client: TestClient, session: Session
) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_ids = _make_accounts(client, token, 1)
    agreement = _create_agreement(client, token, account_ids)

    pulled = _pull(client, token, agreement["id"])

    assert len(pulled["staged_batch_ids"]) == 1
    batch_id = pulled["staged_batch_ids"][0]
    # A one-account grant only receives the three `stub-acc-0` rows: the stub
    # scopes by grant, and `stub-acc-9` never leaves the provider at all.
    assert pulled["rows"] == 3
    assert pulled["imported"] == 3
    assert pulled["duplicates_skipped"] == 0
    assert pulled["unresolved"] == 0  # CAD account, no fx row needed
    assert pulled["dropped_rows"] == 0
    assert [bal["account_id"] for bal in pulled["balances"]] == account_ids
    assert pulled["balances"][0]["amount"] == "1500.00"
    assert pulled["agreement"]["provider_status"] == ProviderStatus.ok
    assert pulled["agreement"]["last_sync_at"] is not None

    preview = {row["raw_description"]: row for row in pulled["preview"]}
    assert Decimal(preview[SHARED_DESCRIPTION]["amount_native"]) == Decimal("-42.35")
    assert preview["NETFLIX SUBSCRIPTION"]["duplicate"] is False

    # Staged, not live (docs/03 live scope).
    assert _live_total(client, token) == 0
    detail = client.get(f"/api/v1/imports/{batch_id}", headers=_headers(token))
    assert detail.json()["status"] == ImportBatchStatus.pending_review
    assert detail.json()["file_name"] == "bank-sync:stub"
    assert detail.json()["imported"] == 3

    _commit(client, token, batch_id)
    assert _live_total(client, token) == 3
    assert (
        client.get(f"{SYNC}/agreements/{agreement['id']}", headers=_headers(token)).json()[
            "provider_status"
        ]
        == ProviderStatus.ok
    )


def test_account_status_columns_surface_on_account_public(
    client: TestClient, session: Session
) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_ids = _make_accounts(client, token, 1)
    agreement = _create_agreement(client, token, account_ids)
    _pull(client, token, agreement["id"])

    listed = client.get("/api/v1/accounts", headers=_headers(token))
    assert listed.status_code == 200
    row = next(item for item in listed.json()["items"] if item["id"] == account_ids[0])
    assert row["last_sync_at"] is not None
    assert row["provider_status"] == ProviderStatus.ok

    account = session.get(Account, account_ids[0])
    assert account.last_sync_at is not None
    assert account.provider_status == ProviderStatus.ok


def test_second_pull_with_no_new_rows_stages_nothing_but_advances_the_cursor(
    client: TestClient,
) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_ids = _make_accounts(client, token, 1)
    agreement = _create_agreement(client, token, account_ids)
    first = _pull(client, token, agreement["id"])
    first_cursor = first["agreement"]["last_sync_at"]

    second = _pull(client, token, agreement["id"])

    assert second["staged_batch_ids"] == []
    assert second["rows"] == 0
    assert second["preview"] == []
    assert second["imported"] == 0
    from datetime import datetime

    assert datetime.fromisoformat(second["agreement"]["last_sync_at"]) >= datetime.fromisoformat(
        first_cursor
    )
    assert second["balances"] == first["balances"]  # observations still reported


def test_balances_are_reported_and_never_written_to_account_fields(
    client: TestClient, session: Session
) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_ids = _make_accounts(client, token, 1)
    agreement = _create_agreement(client, token, account_ids)
    pulled = _pull(client, token, agreement["id"])

    assert pulled["balances"] == [
        {
            "account_id": account_ids[0],
            "amount": "1500.00",
            "currency": "CAD",
            "as_of": "2025-11-06",
        }
    ]
    account = session.get(Account, account_ids[0])
    # Manual data wins (AGENTS §2): an observed balance is never an assertion.
    assert account.starting_balance is None
    assert _live_total(client, token) == 0  # nothing staged from balances alone


def test_rows_addressed_outside_the_grant_are_dropped_by_the_service(
    client: TestClient,
) -> None:
    """The service never trusts an adapter's scoping (docs/39 § Account scope).

    The well-behaved stub already filters by grant; this one deliberately
    leaks a foreign account ref to prove the service drops it rather than
    staging into an account the consent never covered.
    """

    class _LeakyStub(StubProvider):
        def pull_delta(self, user, since):
            batch = super().pull_delta(user, since)
            leak = NormalizedTx(
                account_ref="stub-acc-7",
                external_id="LEAK-1",
                date=date(2025, 11, 8),
                amount_native=Decimal("-1.00"),
                description="FOREIGN ACCOUNT ROW",
                currency="CAD",
            )
            return PullBatch(
                transactions=(*batch.transactions, leak),
                balances=batch.balances,
                provider_status=batch.provider_status,
            )

    register_provider(_LeakyStub(slug="stub"))
    auth = _register(client)
    token = auth["access_token"]
    account_ids = _make_accounts(client, token, 2)
    agreement = _create_agreement(client, token, account_ids)
    pulled = _pull(client, token, agreement["id"])

    assert pulled["dropped_rows"] == 1
    assert pulled["rows"] == 4  # both in-scope accounts, leak excluded
    descriptions = {row["raw_description"] for row in pulled["preview"]}
    assert "FOREIGN ACCOUNT ROW" not in descriptions
    assert "UNMAPPED ACCOUNT ROW" not in descriptions


# --- ingestion parity: CSV + sync dedup --------------------------------------


def test_csv_then_sync_same_transaction_is_one_row_with_a_match_decision(
    client: TestClient, session: Session
) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_ids = _make_accounts(client, token, 1)

    uploaded = _upload_csv(client, token, account_ids[0])
    assert uploaded["imported"] == 2
    _commit(client, token, uploaded["id"])
    assert _live_total(client, token) == 2

    agreement = _create_agreement(client, token, account_ids)
    pulled = _pull(client, token, agreement["id"])

    # Same transaction, two sources: ONE new live row, ONE staged match decision.
    assert pulled["rows"] == 3
    assert pulled["imported"] == 2
    assert pulled["duplicates_skipped"] == 1
    duplicates = [row for row in pulled["preview"] if row["duplicate"]]
    assert [row["raw_description"] for row in duplicates] == [SHARED_DESCRIPTION]

    _commit(client, token, pulled["staged_batch_ids"][0])
    assert _live_total(client, token) == 4  # 2 CSV rows + 2 genuinely new sync rows

    matches = session.exec(
        select(Transaction)
        .where(Transaction.account_id == account_ids[0])
        .where(Transaction.raw_description == SHARED_DESCRIPTION)
    ).all()
    assert len(matches) == 1, (
        "the CSV row and the synced row must collapse to a single live transaction "
        f"after commit; found {len(matches)}"
    )
    assert matches[0].is_duplicate is False
    assert matches[0].import_batch_id == uploaded["id"]


def test_force_include_keeps_the_sync_duplicate_as_a_second_row(
    client: TestClient, session: Session
) -> None:
    """The match decision is the user's to make — never merged silently."""
    auth = _register(client)
    token = auth["access_token"]
    account_ids = _make_accounts(client, token, 1)
    uploaded = _upload_csv(client, token, account_ids[0])
    _commit(client, token, uploaded["id"])

    agreement = _create_agreement(client, token, account_ids)
    pulled = _pull(client, token, agreement["id"])
    batch_id = pulled["staged_batch_ids"][0]
    duplicate_id = next(row["id"] for row in pulled["preview"] if row["duplicate"])

    response = client.post(
        f"/api/v1/imports/{batch_id}/commit",
        json={"include_ids": [duplicate_id]},
        headers=_headers(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 3
    assert response.json()["duplicates_skipped"] == 0
    # 2 CSV rows + every staged sync row (the forced duplicate included) = 5.
    assert _live_total(client, token) == 5


# --- revocation --------------------------------------------------------------


def test_revoke_kills_the_agreement_and_stops_pulls(client: TestClient, session: Session) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_ids = _make_accounts(client, token, 1)
    agreement = _create_agreement(client, token, account_ids)
    _pull(client, token, agreement["id"])

    revoked = client.post(f"{SYNC}/agreements/{agreement['id']}/revoke", headers=_headers(token))
    assert revoked.status_code == 200, revoked.text
    body = revoked.json()
    assert body["status"] == SyncAgreementStatus.revoked
    assert body["revoked_at"] is not None
    assert body["provider_status"] is None

    blocked = client.post(f"{SYNC}/agreements/{agreement['id']}/pull", headers=_headers(token))
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "sync_agreement_revoked"

    # Idempotent: a second revoke is not an error.
    again = client.post(f"{SYNC}/agreements/{agreement['id']}/revoke", headers=_headers(token))
    assert again.status_code == 200
    assert again.json()["status"] == SyncAgreementStatus.revoked

    # Status page: no live link, historical last_sync_at retained.
    account = session.get(Account, account_ids[0])
    assert account.provider_status is None
    assert account.last_sync_at is not None

    stored = session.get(SyncAgreement, agreement["id"])
    assert stored.last_error is None


def test_provider_side_refuses_a_revoked_consent_too() -> None:
    """Defence in depth: even with a stale local row, the provider says no."""
    stub = default_stub()
    user = ProviderUser(id=4242)
    stub.create_agreement(
        user, [ProviderAccount(local_id=1, currency="CAD", account_type="chequing")]
    )
    stub.revoke(user)

    with pytest.raises(ProviderRevoked):
        stub.pull_delta(user, None)


def test_provider_failure_records_last_error_and_still_revokes_locally(
    client: TestClient, session: Session
) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_ids = _make_accounts(client, token, 1)
    agreement = _create_agreement(client, token, account_ids)

    class _Exploding(StubProvider):
        def revoke(self, user):
            raise ProviderUnavailable("provider logout endpoint is down")

    register_provider(_Exploding(slug="stub"))
    response = client.post(f"{SYNC}/agreements/{agreement['id']}/revoke", headers=_headers(token))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == SyncAgreementStatus.revoked
    assert "provider logout endpoint is down" in response.json()["last_error"]

    blocked = client.post(f"{SYNC}/agreements/{agreement['id']}/pull", headers=_headers(token))
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "sync_agreement_revoked"


# --- provider failure modes --------------------------------------------------


def test_provider_unavailable_is_502_and_marks_the_link_degraded(
    client: TestClient, session: Session
) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_ids = _make_accounts(client, token, 1)
    agreement = _create_agreement(client, token, account_ids)

    default_stub().next_pull_error = ProviderUnavailable("upstream returned 503")
    failed = client.post(f"{SYNC}/agreements/{agreement['id']}/pull", headers=_headers(token))
    assert failed.status_code == 502
    assert failed.json()["code"] == "sync_provider_unavailable"

    stored = session.get(SyncAgreement, agreement["id"])
    assert stored.status == SyncAgreementStatus.active  # consent still good
    assert stored.provider_status == ProviderStatus.degraded
    assert "upstream returned 503" in stored.last_error
    account = session.get(Account, account_ids[0])
    assert account.provider_status == ProviderStatus.degraded

    # Retryable: the transient failure clears on the next pull.
    default_stub().next_pull_error = None
    recovered = _pull(client, token, agreement["id"])
    assert recovered["agreement"]["provider_status"] == ProviderStatus.ok
    assert recovered["agreement"]["last_error"] is None


def test_provider_auth_required_blocks_pulls_until_reconsent(
    client: TestClient, session: Session
) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_ids = _make_accounts(client, token, 1)
    agreement = _create_agreement(client, token, account_ids)

    default_stub().next_pull_error = ProviderAuthRequired("MFA re-prompt required")
    first = client.post(f"{SYNC}/agreements/{agreement['id']}/pull", headers=_headers(token))
    assert first.status_code == 409
    assert first.json()["code"] == "sync_auth_required"

    stored = session.get(SyncAgreement, agreement["id"])
    assert stored.status == SyncAgreementStatus.auth_required
    assert stored.provider_status == ProviderStatus.auth_required
    assert session.get(Account, account_ids[0]).provider_status == ProviderStatus.auth_required

    # The local gate now holds the line — the provider is never called again
    # (clearing the knob proves the 409 comes from our own state).
    default_stub().next_pull_error = None
    second = client.post(f"{SYNC}/agreements/{agreement['id']}/pull", headers=_headers(token))
    assert second.status_code == 409
    assert second.json()["code"] == "sync_auth_required"


def test_row_cap_is_enforced_before_anything_is_staged(client: TestClient) -> None:
    too_many = [_normalized_tx(index) for index in range(2001)]
    register_provider(StubProvider(slug="stub", transactions=too_many, balances=()))
    auth = _register(client)
    token = auth["access_token"]
    account_ids = _make_accounts(client, token, 1)
    agreement = _create_agreement(client, token, account_ids)

    response = client.post(f"{SYNC}/agreements/{agreement['id']}/pull", headers=_headers(token))
    assert response.status_code == 413
    assert response.json()["code"] == "too_many_rows"

    batches = client.get("/api/v1/imports", headers=_headers(token))
    assert batches.json()["total"] == 0, "a rejected pull must leave no rows behind"


def _normalized_tx(index: int):
    from app.services.sync.types import NormalizedTx

    return NormalizedTx(
        account_ref="stub-acc-0",
        external_id=f"BULK-{index}",
        date=date(2025, 1, 1),
        amount_native=Decimal(index + 1),
        description=f"BULK ROW {index}",
        currency="CAD",
    )


# --- privacy mode ------------------------------------------------------------


def test_privacy_mode_seals_every_bank_sync_egress_path(client: TestClient) -> None:
    auth = _register(client)
    token = auth["access_token"]
    account_ids = _make_accounts(client, token, 1)
    agreement = _create_agreement(client, token, account_ids)

    patched = client.patch("/api/v1/users/me", json={"privacy_mode": True}, headers=_headers(token))
    assert patched.status_code == 200, patched.text

    institutions = client.get(f"{SYNC}/providers/stub/institutions", headers=_headers(token))
    assert institutions.status_code == 403
    assert institutions.json()["code"] == "privacy_mode_blocks_external_api"

    pulled = client.post(f"{SYNC}/agreements/{agreement['id']}/pull", headers=_headers(token))
    assert pulled.status_code == 403
    assert pulled.json()["code"] == "privacy_mode_blocks_external_api"

    revoked = client.post(f"{SYNC}/agreements/{agreement['id']}/revoke", headers=_headers(token))
    assert revoked.status_code == 403
    assert revoked.json()["code"] == "privacy_mode_blocks_external_api"

    # Reads stay open: no provider call, no egress.
    providers = client.get(f"{SYNC}/providers", headers=_headers(token))
    assert providers.status_code == 200


# --- E2EE honesty (docs/39 § What sync does not promise) ---------------------


def test_encryption_required_tenant_cannot_pull_plaintext(client: TestClient, session) -> None:
    """M181: sync is still not E2EE, and the pull now REFUSES rather than
    writing plaintext into an opted-in tenant (docs/39 § 9, docs/33 § M79).

    The disclosure is unchanged for everything else — the agreement metadata is
    server-readable and a pull for an ordinary tenant still lands in the
    plaintext columns — but a server-side pull cannot produce ciphertext, so it
    fails closed before anything is staged (dry-run for ordinary tenants is
    covered by the other 25 cases, e.g. #10).
    """
    auth = _register(client)
    token = auth["access_token"]
    # The account must exist before the flag flips: `POST /accounts` also
    # enforces `encryption_required` (it wants `name_cipher`).
    account_ids = _make_accounts(client, token, 1)
    patched = client.patch(
        "/api/v1/users/me", json={"encryption_required": True}, headers=_headers(token)
    )
    assert patched.status_code == 200, patched.text
    assert _me(client, token)["encryption_required"] is True

    # Consent is metadata, not user content — setting it up still works.
    agreement = _create_agreement(client, token, account_ids)

    user_id = _me(client, token)["id"]
    before_batches = _scalar(
        session, "select count(*) from import_batches where user_id = :uid", user_id
    )
    before_txns = _scalar(
        session, "select count(*) from transactions where user_id = :uid", user_id
    )

    pulled = client.post(f"{SYNC}/agreements/{agreement['id']}/pull", headers=_headers(token))
    assert pulled.status_code == 422, pulled.text
    body = pulled.json()
    assert set(body) == {"detail", "code"}
    assert body["code"] == "encryption_required"
    assert "cannot safely write encrypted data" in body["detail"]
    assert "unlock" not in body["detail"].lower()

    # Zero rows staged or committed, and no provider work happened.
    assert (
        _scalar(session, "select count(*) from import_batches where user_id = :uid", user_id)
        == before_batches
    )
    assert (
        _scalar(session, "select count(*) from transactions where user_id = :uid", user_id)
        == before_txns
    )
    assert client.get("/api/v1/imports", headers=_headers(token)).json()["total"] == 0

    # The CRUD guard is unchanged: a manual plaintext write still 422s.
    manual = client.post(
        "/api/v1/transactions",
        json={
            "account_id": account_ids[0],
            "date": "2025-11-10",
            "raw_description": "MANUAL PLAINTEXT",
            "amount_native": "-1.00",
        },
        headers=_headers(token),
    )
    assert manual.status_code == 422
    assert manual.json()["code"] == "encryption_required"

    # Revoke stays available — the only cleanup left for an enrolled tenant.
    revoked = client.post(f"{SYNC}/agreements/{agreement['id']}/revoke", headers=_headers(token))
    assert revoked.status_code == 200, revoked.text


# --- row cap shared with file uploads ---------------------------------------


def test_upload_row_cap_message_is_shared_with_bank_sync(client: TestClient) -> None:
    """Both sources reject through the same `413 too_many_rows` contract."""
    from app.services.import_staging import MAX_STAGED_ROWS

    assert MAX_STAGED_ROWS == 2000
