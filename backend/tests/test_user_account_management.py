"""Self-service account management: change-password, profile (name/email), delete.

Exercises the three new /users/me endpoints plus the ordered cascade delete
(backend/app/api/users.py::_delete_user_data). Registration/login/preferences
PATCH are untouched and covered by test_auth.py.

Run: cd backend && uv run pytest tests/test_user_account_management.py -q
"""

from datetime import UTC, date, datetime, timedelta

from sqlmodel import func, select

from app.api.auth import _issue_pair
from app.api.users import _USER_OWNED_MODELS
from app.core.security import hash_password, hash_refresh_token
from app.models.acb_pools import AcbPool
from app.models.accounts import Account, AccountType
from app.models.categories import Category
from app.models.etf_holdings import EtfAssetClass, EtfHolding, EtfHoldingSource
from app.models.fundamentals import Fundamentals, FundamentalsSource
from app.models.fx_rates import FxRate, FxSource
from app.models.import_batches import ImportBatch
from app.models.institutions import Institution
from app.models.learned_payees import LearnedPayee
from app.models.llm_suggestions import LlmSuggestion, SuggestionStatus
from app.models.prices import Price, PriceSource
from app.models.refresh_tokens import RefreshToken
from app.models.rules import Rule, RuleMatchType
from app.models.securities import Security, SecurityKind
from app.models.trades import Trade, TradeAction
from app.models.transactions import Transaction
from app.models.users import User
from app.models.watchlist import WatchlistItem
from app.services.demo_seed import DEMO_EMAIL
from tests.test_auth import EMAIL, PASSWORD, REFRESH, _auth_headers, _cookies_over_http, _register

CHANGE_PW = "/api/v1/users/me/change-password"
PROFILE = "/api/v1/users/me/profile"
DELETE = "/api/v1/users/me"
ME = "/api/v1/users/me"
LOGIN = "/api/v1/auth/login"


def _user_id(session, email: str) -> int:
    return session.exec(select(User).where(User.email == email)).one().id


def _seed_user_data(session, user_id: int) -> None:
    """Create one row in every user-owned table for the given user (dependency
    order: children before the parents they reference)."""
    inst = Institution(user_id=user_id, name="RBC")
    session.add(inst)
    session.flush()

    cat = Category(user_id=user_id, name="Groceries")
    session.add(cat)
    session.flush()

    sec = Security(
        user_id=user_id,
        symbol="XEI",
        name="iShares XEI",
        kind=SecurityKind.etf,
        listing_currency="CAD",
        exchange="TSX",
    )
    session.add(sec)
    session.flush()

    acct = Account(user_id=user_id, name="TFSA", type=AccountType.chequing, currency="CAD")
    session.add(acct)
    session.flush()

    txn = Transaction(
        user_id=user_id,
        account_id=acct.id,
        date=date(2026, 1, 1),
        raw_description="WALMART",
        normalized_payee="walmart",
        amount_native=42.0,
        currency="CAD",
        dedup_hash="abc123",
    )
    session.add(txn)
    session.flush()

    trade = Trade(
        user_id=user_id,
        account_id=acct.id,
        security_id=sec.id,
        action=TradeAction.buy,
        date=date(2026, 1, 2),
        quantity=10,
        price_native=30.0,
        commission=0,
    )
    session.add(trade)
    session.flush()

    sugg = LlmSuggestion(
        user_id=user_id,
        transaction_id=txn.id,
        proposed_category_id=cat.id,
        confidence=0.9,
        status=SuggestionStatus.suggested,
        model_used="ollama",
    )
    session.add(sugg)

    lp = LearnedPayee(user_id=user_id, normalized_payee="walmart", category_id=cat.id, hit_count=2)
    session.add(lp)

    pool = AcbPool(
        user_id=user_id,
        account_id=acct.id,
        security_id=sec.id,
        quantity=10,
        total_acb_cad=300,
    )
    session.add(pool)

    batch = ImportBatch(user_id=user_id, account_id=acct.id, institution="rbc", file_name="q.csv")
    session.add(batch)

    etf = EtfHolding(
        user_id=user_id,
        security_id=sec.id,
        holding_name="Acme",
        asset_class=EtfAssetClass.other,
        weight_pct=10.0,
        source=EtfHoldingSource.manual,
    )
    session.add(etf)

    fund = Fundamentals(
        user_id=user_id,
        security_id=sec.id,
        source=FundamentalsSource.manual,
        fetched_at=date(2026, 1, 3),
    )
    session.add(fund)

    price = Price(
        user_id=user_id,
        security_id=sec.id,
        date=date(2026, 1, 4),
        price=30.5,
        source=PriceSource.manual,
    )
    session.add(price)

    watch = WatchlistItem(user_id=user_id, security_id=sec.id)
    session.add(watch)

    rule = Rule(
        user_id=user_id,
        match_type=RuleMatchType.contains,
        pattern="walmart",
        priority=1,
        category_id=cat.id,
    )
    session.add(rule)

    rt = RefreshToken(
        user_id=user_id,
        token_hash=hash_refresh_token("extra-token"),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    session.add(rt)

    session.commit()


# --- change password -------------------------------------------------------


def test_change_password_success(client, session) -> None:
    body = _register(client)
    # M1: refresh token is cookie-only in response bodies now.
    old_refresh = client.cookies.get("refresh_token")
    assert old_refresh
    headers = _auth_headers(body["access_token"])

    res = client.post(
        CHANGE_PW,
        json={"current_password": PASSWORD, "new_password": "new-correct-horse"},
        headers=headers,
    )
    assert res.status_code == 200
    pair = res.json()
    assert pair["user"]["email"] == EMAIL

    # Refresh is cookie-only, so the token travels as a cookie. change-password
    # deletes every refresh token of the user, so the pre-change one is dead.
    with _cookies_over_http():
        dead = client.post(REFRESH, json={}, cookies={"refresh_token": old_refresh})
        assert dead.status_code == 401
        # The successor change-password issued still works.
        successor = client.cookies.get("refresh_token")
        assert successor and successor != old_refresh
        live = client.post(REFRESH, json={}, cookies={"refresh_token": successor})
        assert live.status_code == 200

    # Login with the new password works; old password does not.
    good = client.post(LOGIN, json={"email": EMAIL, "password": "new-correct-horse"})
    assert good.status_code == 200
    bad = client.post(LOGIN, json={"email": EMAIL, "password": PASSWORD})
    assert bad.status_code == 401


def test_change_password_wrong_current(client) -> None:
    token = _register(client)["access_token"]
    res = client.post(
        CHANGE_PW,
        json={"current_password": "wrong", "new_password": "new-correct-horse"},
        headers=_auth_headers(token),
    )
    assert res.status_code == 401
    assert res.json()["code"] == "invalid_credentials"


def test_change_password_short_new(client) -> None:
    token = _register(client)["access_token"]
    res = client.post(
        CHANGE_PW,
        json={"current_password": PASSWORD, "new_password": "short"},
        headers=_auth_headers(token),
    )
    assert res.status_code == 422


def test_change_password_too_long(client) -> None:
    token = _register(client)["access_token"]
    res = client.post(
        CHANGE_PW,
        json={"current_password": PASSWORD, "new_password": "a" * 73},
        headers=_auth_headers(token),
    )
    assert res.status_code == 422
    assert res.json()["code"] == "password_too_long"


# --- profile (name / email) ------------------------------------------------


def test_update_profile_name(client, session) -> None:
    token = _register(client)["access_token"]
    res = client.patch(PROFILE, json={"name": "Ada"}, headers=_auth_headers(token))
    assert res.status_code == 200
    assert res.json()["name"] == "Ada"

    me = client.get(ME, headers=_auth_headers(token))
    assert me.json()["name"] == "Ada"


def test_update_profile_email(client, session) -> None:
    token = _register(client)["access_token"]
    res = client.patch(
        PROFILE,
        json={"email": "new@x.com", "current_password": PASSWORD},
        headers=_auth_headers(token),
    )
    assert res.status_code == 200
    assert res.json()["email"] == "new@x.com"

    login = client.post(LOGIN, json={"email": "new@x.com", "password": PASSWORD})
    assert login.status_code == 200


def test_update_profile_email_duplicate(client, session) -> None:
    _register(client, email="other@x.com")  # second user owns this email
    token = _register(client, email=EMAIL)["access_token"]
    res = client.patch(
        PROFILE,
        json={"email": "other@x.com", "current_password": PASSWORD},
        headers=_auth_headers(token),
    )
    assert res.status_code == 409
    assert res.json()["code"] == "duplicate_email"


def test_update_profile_email_requires_password(client) -> None:
    token = _register(client)["access_token"]
    res = client.patch(PROFILE, json={"email": "other@x.com"}, headers=_auth_headers(token))
    assert res.status_code == 401
    assert res.json()["code"] == "current_password_required"


# --- delete account (cascade) ----------------------------------------------


def test_delete_account_cascade(client, session) -> None:
    body = _register(client)
    uid = _user_id(session, EMAIL)
    _seed_user_data(session, uid)

    # A shared fx_rates row (no user_id) must survive the delete.
    fx = FxRate(base="USD", quote="CAD", rate=1.35, date=date(2026, 1, 1), source=FxSource.manual)
    session.add(fx)
    session.commit()

    res = client.request(
        "DELETE",
        DELETE,
        json={"current_password": PASSWORD},
        headers=_auth_headers(body["access_token"]),
    )
    assert res.status_code == 204

    # Every user-owned table is empty for that user.
    for model in _USER_OWNED_MODELS:
        count = session.exec(
            select(func.count()).select_from(model).where(model.user_id == uid)
        ).first()
        assert count == 0, f"{model.__name__} still has rows for deleted user"

    # The user row itself is gone.
    assert session.exec(select(User).where(User.id == uid)).first() is None

    # Shared fx_rates untouched.
    assert session.exec(select(func.count()).select_from(FxRate)).first() == 1


def test_delete_account_wrong_password(client) -> None:
    token = _register(client)["access_token"]
    res = client.request(
        "DELETE", DELETE, json={"current_password": "wrong"}, headers=_auth_headers(token)
    )
    assert res.status_code == 401
    assert res.json()["code"] == "invalid_credentials"


# --- demo account is excluded from self-service management -----------------


def test_change_password_blocked_for_demo(client, session) -> None:
    user = User(email=DEMO_EMAIL, hashed_password=hash_password(PASSWORD))
    session.add(user)
    session.commit()
    session.refresh(user)
    token = _issue_pair(session, user).access_token
    res = client.post(
        CHANGE_PW,
        json={"current_password": PASSWORD, "new_password": "new-correct-horse"},
        headers=_auth_headers(token),
    )
    assert res.status_code == 403
    assert res.json()["code"] == "demo_account_restricted"


def test_update_profile_blocked_for_demo(client, session) -> None:
    user = User(email=DEMO_EMAIL, hashed_password=hash_password(PASSWORD))
    session.add(user)
    session.commit()
    session.refresh(user)
    token = _issue_pair(session, user).access_token
    res = client.patch(PROFILE, json={"name": "Nope"}, headers=_auth_headers(token))
    assert res.status_code == 403
    assert res.json()["code"] == "demo_account_restricted"


def test_delete_account_blocked_for_demo(client, session) -> None:
    user = User(email=DEMO_EMAIL, hashed_password=hash_password(PASSWORD))
    session.add(user)
    session.commit()
    session.refresh(user)
    token = _issue_pair(session, user).access_token
    res = client.request(
        "DELETE", DELETE, json={"current_password": PASSWORD}, headers=_auth_headers(token)
    )
    assert res.status_code == 403
    assert res.json()["code"] == "demo_account_restricted"
