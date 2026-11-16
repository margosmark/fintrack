"""M113 — the account page must say an import is waiting (docs/08, docs/09).

Regression named after the bug: Mark imported into account 731, opened
`/accounts/731`, saw no securities and no hint that a batch was parked at
`/imports` awaiting commit, and abandoned the flow twice. The API half of the
fix is `AccountPublic.pending_import_count` — a COUNT, on the account-level
GET the page already fetches.

What is pinned here:
  * pending batches on THIS account are counted (1 and N > 1);
  * committed / discarded / reverted batches are NOT counted, and the
    distinction never leaks — the response carries no batch field at all;
  * tenancy: another user's batch on the same account id counts for nobody;
  * the list endpoint keeps the documented 0 default (docs/08 — the banner's
    only consumer is `GET /accounts/{id}`; docs/34's query budget for
    `GET /accounts` stays untouched).
"""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models.accounts import Account
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.transactions import Transaction
from app.models.users import User

API = "/api/v1"


def _register(client: TestClient, email: str) -> dict:
    client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse-battery"})
    response = client.post(
        f"{API}/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert response.status_code == 200
    return response.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _user_id_for_new_user(session: Session, client: TestClient, email: str) -> int:
    _register(client, email)
    return _user_id(session, email)


def _user_id(session: Session, email: str) -> int:
    user = session.exec(select(User).where(User.email == email)).one()
    return user.id


def _create_account(client: TestClient, token: str, **overrides) -> dict:
    payload = {"name": "TFSA", "type": "tfsa", "currency": "CAD", **overrides}
    response = client.post(f"{API}/accounts", json=payload, headers=_headers(token))
    assert response.status_code == 201, response.text
    return response.json()


def _add_batch(
    session: Session,
    user_id: int,
    account_id: int,
    status: ImportBatchStatus,
    *amounts: str,
) -> ImportBatch:
    batch = ImportBatch(
        user_id=user_id,
        account_id=account_id,
        institution="Wealthsimple",
        file_name=f"{status.value}.csv",
        status=status,
    )
    session.add(batch)
    session.flush()
    for index, amount in enumerate(amounts):
        session.add(
            Transaction(
                user_id=user_id,
                account_id=account_id,
                date=date(2026, 10, 1),
                raw_description=f"tx {index}",
                normalized_payee=f"Payee {index}",
                amount_native=Decimal(amount),
                currency="CAD",
                # Batch id in the hash: the UNIQUE (account_id, dedup_hash)
                # spans every batch, so two batches on one account must not
                # collide (docs/03 staging).
                dedup_hash=f"m113-{batch.id}-{index}",
                import_batch_id=batch.id,
            )
        )
    session.flush()
    return batch


def test_marks_case_account_page_counts_one_pending_import(
    session: Session, client: TestClient
) -> None:
    """Mark's case (t_9bba21d1): a staged batch must be visible from the page
    he actually opens — one pending batch, `pending_import_count == 1`."""
    auth = _register(client, "m113-marks-case@example.com")
    token = auth["access_token"]
    account = _create_account(client, token)
    user_id = _user_id(session, "m113-marks-case@example.com")

    assert (
        client.get(f"{API}/accounts/{account['id']}", headers=_headers(token)).json()[
            "pending_import_count"
        ]
        == 0
    )

    _add_batch(session, user_id, account["id"], ImportBatchStatus.pending_review, "12600.00")
    session.commit()

    body = client.get(f"{API}/accounts/{account['id']}", headers=_headers(token)).json()
    assert body["pending_import_count"] == 1


def test_pending_count_is_n_for_n_batches(session: Session, client: TestClient) -> None:
    auth = _register(client, "m113-count@example.com")
    token = auth["access_token"]
    account = _create_account(client, token)
    user_id = _user_id(session, "m113-count@example.com")

    _add_batch(session, user_id, account["id"], ImportBatchStatus.pending_review, "10.00")
    _add_batch(session, user_id, account["id"], ImportBatchStatus.pending_review, "-5.00")
    session.commit()

    body = client.get(f"{API}/accounts/{account['id']}", headers=_headers(token)).json()
    assert body["pending_import_count"] == 2


def test_only_pending_review_counts(session: Session, client: TestClient) -> None:
    """Committed / discarded / reverted batches are not pending and their
    distinction must never leak from this field."""
    auth = _register(client, "m113-statuses@example.com")
    token = auth["access_token"]
    user_id = _user_id(session, "m113-statuses@example.com")

    for status in (
        ImportBatchStatus.committed,
        ImportBatchStatus.discarded,
        ImportBatchStatus.reverted,
    ):
        account = _create_account(client, token, name=f"Acct {status.value}")
        _add_batch(session, user_id, account["id"], status, "10.00")
        session.commit()
        body = client.get(f"{API}/accounts/{account['id']}", headers=_headers(token)).json()
        assert body["pending_import_count"] == 0, status


def test_pending_count_is_a_count_never_batch_contents(
    session: Session, client: TestClient
) -> None:
    """E2EE row-count rule (docs/09): no file name, status, institution or row
    payload rides along with the number."""
    auth = _register(client, "m113-count-only@example.com")
    token = auth["access_token"]
    account = _create_account(client, token)
    user_id = _user_id(session, "m113-count-only@example.com")
    batch = _add_batch(session, user_id, account["id"], ImportBatchStatus.pending_review, "10.00")
    session.commit()

    body = client.get(f"{API}/accounts/{account['id']}", headers=_headers(token)).json()
    assert body["pending_import_count"] == 1
    for forbidden in ("file_name", "institution", "status", "imported", "batch_id"):
        assert forbidden not in body, forbidden
    # ...and the batch itself is untouched (the count never mutates).
    session.refresh(batch)
    assert batch.status == ImportBatchStatus.pending_review


def test_another_users_batch_on_this_account_is_not_counted(
    session: Session, client: TestClient
) -> None:
    """Tenancy: the count is scoped to the account's owner, so a row planted
    with somebody else's `user_id` is invisible on the owner's page."""
    auth = _register(client, "m113-owner@example.com")
    token = auth["access_token"]
    account = _create_account(client, token)
    owner_id = _user_id(session, "m113-owner@example.com")

    intruder = _user_id_for_new_user(session, client, "m113-intruder@example.com")
    _add_batch(session, intruder, account["id"], ImportBatchStatus.pending_review, "10.00")
    _add_batch(session, owner_id, account["id"], ImportBatchStatus.pending_review, "10.00")
    session.commit()

    body = client.get(f"{API}/accounts/{account['id']}", headers=_headers(token)).json()
    assert body["pending_import_count"] == 1


def test_list_endpoint_keeps_the_documented_zero_default(
    session: Session, client: TestClient
) -> None:
    """docs/08: the count is computed on the single-account responses only —
    `GET /accounts` does not run the query (its docs/34 budget stays 17)."""
    auth = _register(client, "m113-list@example.com")
    token = auth["access_token"]
    account = _create_account(client, token)
    user_id = _user_id(session, "m113-list@example.com")
    _add_batch(session, user_id, account["id"], ImportBatchStatus.pending_review, "10.00")
    session.commit()

    page = client.get(f"{API}/accounts", headers=_headers(token)).json()
    assert page["items"][0]["pending_import_count"] == 0
    single = client.get(f"{API}/accounts/{account['id']}", headers=_headers(token)).json()
    assert single["pending_import_count"] == 1


def test_delete_guard_still_sees_batches_count_or_not(session: Session, client: TestClient) -> None:
    """The count is read-only: an account holding a pending batch is still
    refused with 409 `account_in_use` (docs/29 F3 unchanged by M113)."""
    auth = _register(client, "m113-delete@example.com")
    token = auth["access_token"]
    account = _create_account(client, token)
    user_id = _user_id(session, "m113-delete@example.com")
    _add_batch(session, user_id, account["id"], ImportBatchStatus.pending_review, "10.00")
    session.commit()

    assert (
        client.get(f"{API}/accounts/{account['id']}", headers=_headers(token)).json()[
            "pending_import_count"
        ]
        == 1
    )
    deleted = client.delete(f"{API}/accounts/{account['id']}", headers=_headers(token))
    assert deleted.status_code == 409
    assert deleted.json()["code"] == "account_in_use"

    account_row = session.get(Account, account["id"])
    assert account_row is not None
