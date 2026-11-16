"""M75 OFX export — round trip through OUR OWN importer (docs/11, docs/03).

The acceptance for the OFX path is interoperability with the app itself:
export an account's committed transactions as OFX 1.02, push that file back
through `POST /imports/upload`, commit it, and end with ZERO net-new rows —
every row must be recognised as a duplicate it already has.

That only holds if the writer preserves exactly what `build_dedup_hash` reads:
the account, the booking date, the amount to two places, and the
whitespace-collapsed `raw_description`. The probe case below deliberately
carries `&`, `<`, `>`, a non-ASCII character and a double space, because
those are the shapes that break an SGML writer (entity escaping, encoding,
whitespace) and silently produce a second copy of every row.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlmodel import select

from app.importers.base import AccountView, RawTransaction, build_dedup_hash
from app.importers.registry import detect_importer
from app.models import (
    Account,
    AccountType,
    ImportBatch,
    ImportBatchStatus,
    Transaction,
    User,
)
from app.services.txn_filters import committed_tx_rows
from tests.test_privacy_export import _register

OFX_URL = "/api/v1/export/ofx"
UPLOAD_URL = "/api/v1/imports/upload"

# Entities + unicode + a double space: the three ways an OFX writer loses the
# exact description the dedup hash was built from.
TRICKY_DESCRIPTION = "TIM & <SONS> CAFÉ  COFFEE"


def _seed_account(
    session: Session,
    user: User,
    *,
    name: str,
    descriptions: list[str],
    batch_status: ImportBatchStatus | None = None,
) -> Account:
    """Account with one committed (or staged) transaction per description."""
    assert user.id is not None
    account = Account(user_id=user.id, name=name, type=AccountType.chequing, currency="CAD")
    session.add(account)
    session.flush()
    assert account.id is not None

    batch = None
    if batch_status is not None:
        batch = ImportBatch(
            user_id=user.id,
            account_id=account.id,
            institution="RBC",
            file_name="seed.csv",
            status=batch_status,
        )
        session.add(batch)
        session.flush()
        assert batch.id is not None

    for index, description in enumerate(descriptions):
        day = date(2026, 9, 10 + index)
        amount = Decimal("-12.34") if index % 2 == 0 else Decimal("100.00")
        session.add(
            Transaction(
                user_id=user.id,
                account_id=account.id,
                date=day,
                raw_description=description,
                normalized_payee=description,
                amount_native=amount,
                currency="CAD",
                cad_amount=amount,
                dedup_hash=build_dedup_hash(
                    account.id,
                    RawTransaction(date=day, amount_native=amount, raw_description=description),
                ),
                import_batch_id=batch.id if batch is not None else None,
            )
        )
    session.commit()
    return account


def _committed_ids(session: Session, user: User, account: Account) -> set[int]:
    assert user.id is not None
    assert account.id is not None
    return {
        row.id
        for row in committed_tx_rows(session, user.id)
        if row.account_id == account.id and row.id is not None
    }


def test_ofx_round_trip_through_our_importer_creates_zero_net_new(
    client: TestClient, session: Session
) -> None:
    headers, email = _register(client)
    user = session.exec(select(User).where(User.email == email)).one()
    account = _seed_account(
        session, user, name="My Chequing *", descriptions=[TRICKY_DESCRIPTION, "PAYROLL DEPOSIT"]
    )
    # A staged row and another account's row must not travel with the file:
    # mixing either one in would re-import as net-new data.
    _seed_account(
        session,
        user,
        name="Other",
        descriptions=["STAGED ROW"],
        batch_status=ImportBatchStatus.pending_review,
    )
    # A second COMMITTED account — its row must not travel with account's file.
    _seed_account(session, user, name="Savings", descriptions=["OTHER ACCOUNT ROW"])

    before = _committed_ids(session, user, account)

    response = client.get(OFX_URL, headers=headers, params={"account_id": account.id})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-ofx")
    disposition = response.headers["content-disposition"]
    assert disposition == 'attachment; filename="fin-tracker-my-chequing.ofx"', disposition
    raw = response.content

    # The wire format itself: UTF-8 SGML, one statement, our identifiers.
    text = raw.decode("utf-8")
    assert text.startswith("OFXHEADER:100")
    assert "ENCODING:UTF-8" in text
    assert f"<ACCTID>fintrack-{account.id}" in text
    assert "<CURDEF>CAD" in text
    assert "STAGED ROW" not in text
    assert "OTHER ACCOUNT ROW" not in text

    # Our own detector hands it to our own parser.
    match = detect_importer(raw)
    assert match is not None and match[0] == "ofx"
    parsed = match[1].parse(raw, AccountView(currency="CAD"))
    assert len(parsed) == 2, parsed
    assert [row.raw_description for row in parsed] == [
        " ".join(TRICKY_DESCRIPTION.split()),
        "PAYROLL DEPOSIT",
    ]
    assert [row.amount_native for row in parsed] == [Decimal("-12.34"), Decimal("100.00")]
    assert [row.date for row in parsed] == [date(2026, 9, 10), date(2026, 9, 11)]
    assert all(row.external_ref.startswith(f"fintrack-{account.id}-") for row in parsed)

    # The acceptance: feed the export back through the real upload pipeline.
    upload = client.post(
        UPLOAD_URL,
        headers=headers,
        files={"file": ("statement.ofx", raw, "application/x-ofx")},
        data={"account_id": str(account.id)},
    )
    assert upload.status_code == 201, upload.text
    batch = upload.json()
    assert batch["imported"] == 0, "every exported row must already be known"
    assert batch["duplicates_skipped"] == 2
    assert all(row["duplicate"] for row in batch["preview"]), batch["preview"]

    commit = client.post(f"/api/v1/imports/{batch['id']}/commit", headers=headers)
    assert commit.status_code == 200, commit.text

    after = _committed_ids(session, user, account)
    assert after == before, f"net-new rows after dedup: {sorted(after - before)}"


def test_ofx_of_another_tenants_account_is_404(client: TestClient, session: Session) -> None:
    headers_a, email_a = _register(client)
    headers_b, email_b = _register(client)
    # Both tenants really exist before the cross-tenant read.
    session.exec(select(User).where(User.email == email_a)).one()
    user_b = session.exec(select(User).where(User.email == email_b)).one()
    account_b = _seed_account(session, user_b, name="Private", descriptions=["THEIR ROW"])

    response = client.get(OFX_URL, headers=headers_a, params={"account_id": account_b.id})
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"
    assert "THEIR ROW" not in response.text

    # The owner still gets it (same id, right tenant).
    assert (
        client.get(OFX_URL, headers=headers_b, params={"account_id": account_b.id}).status_code
        == 200
    )


def test_ofx_requires_a_known_account_and_a_valid_vault_state(
    client: TestClient, session: Session
) -> None:
    headers, email = _register(client)
    user = session.exec(select(User).where(User.email == email)).one()
    account = _seed_account(session, user, name="Empty", descriptions=[])

    missing = client.get(OFX_URL, headers=headers)
    assert missing.status_code == 422, missing.text
    assert missing.json()["code"] == "validation_error"

    unknown_account = client.get(OFX_URL, headers=headers, params={"account_id": 999_999})
    assert unknown_account.status_code == 404

    bad_vault = client.get(
        OFX_URL, headers=headers, params={"account_id": account.id, "vault": "sometimes"}
    )
    assert bad_vault.status_code == 422, bad_vault.text
    assert bad_vault.json()["code"] == "export_invalid_vault"

    # An account with no transactions is still a valid (empty) statement.
    empty = client.get(OFX_URL, headers=headers, params={"account_id": account.id})
    assert empty.status_code == 200, empty.text
    body = empty.content.decode("utf-8")
    assert "<BANKTRANLIST>" in body and "<STMTTRN>" not in body


def test_locked_vault_marks_the_ofx_description_instead_of_faking_plaintext(
    client: TestClient, session: Session
) -> None:
    headers, email = _register(client)
    user = session.exec(select(User).where(User.email == email)).one()
    account = _seed_account(session, user, name="Cipher", descriptions=["SECRET LEDGER ROW"])
    assert user.id is not None
    row = session.exec(select(Transaction).where(Transaction.account_id == account.id)).one()
    row.raw_desc_cipher = "v1.ZGVzYw=="
    session.add(row)
    session.commit()

    locked = client.get(OFX_URL, headers=headers, params={"account_id": account.id})
    assert locked.status_code == 200, locked.text
    body = locked.content.decode("utf-8")
    assert "SECRET LEDGER ROW" not in body, "plaintext shipped while the vault is locked"
    assert "<NAME>E2EE:v1.ZGVzYw==" in body

    unlocked = client.get(
        OFX_URL, headers=headers, params={"account_id": account.id, "vault": "unlocked"}
    )
    assert "SECRET LEDGER ROW" in unlocked.content.decode("utf-8")
    assert "E2EE:" not in unlocked.content.decode("utf-8")


def test_ofx_requires_authentication(client: TestClient) -> None:
    assert client.get(OFX_URL, params={"account_id": 1}).status_code == 401
