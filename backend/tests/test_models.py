"""Model invariants: tenancy (user_id), TimestampMixin, StrEnum native_enum=False,
and *Public projection validates from ORM attributes (docs/02)."""

import inspect

import sqlalchemy
from sqlmodel import SQLModel

import app.models as M
from app.models.mixins import TimestampMixin


def _all_table_models() -> list[type[SQLModel]]:
    """Every table model the domain package exports — DERIVED, not listed.

    This was a hand-maintained list of 20 names, which meant the tenancy
    invariant this file exists to protect was only ever checked against 20 of
    the 49 tables: adding `Budget`, `Debt`, `Goal`, `BillReminder`, any of the
    five notification tables or either auth-factor table shipped WITHOUT being
    covered, and nothing failed. A hand-list makes every future migration
    opt-in, which is the opposite of what AGENTS.md §2 pins ("`user_id` on
    every domain table ... except the shared `fx_rates` cache").

    Deriving from `app.models` means a NEW table is covered the moment it is
    written, and the only thing a human still has to think about is whether it
    belongs in `NO_USER_ID` — which is a decision, not an omission.
    """
    found: list[type[SQLModel]] = []
    for obj in vars(M).values():
        if (
            inspect.isclass(obj)
            and issubclass(obj, SQLModel)
            and getattr(obj, "__table__", None) is not None
            and not obj.__module__.startswith("sqlmodel")
        ):
            found.append(obj)
    return sorted(found, key=lambda c: c.__name__)


DOMAIN_MODELS = _all_table_models()

# Principal tables that intentionally have NO user_id tenancy column. Every
# entry needs a reason, because "it is not in the list" used to mean "nobody
# ever checked it".
NO_USER_ID = {
    # The shared reference cache — the ORIGINAL documented tenancy exception
    # (docs/02), and the template for every operator-level entry below. It is
    # deliberately cross-tenant: manual fills here price every other user's
    # FX, so it is also why `POST /fx` is rate limited.
    M.FxRate,
    # The second shared cache (M168, docs/07 §Provider registry). Same class
    # as `fx_rates` above: issuer facts about the world, identical for every
    # user, written once by discovery and answered for every tenant. There is
    # no owner to tenant it against, and `etf_holdings` — the per-user side of
    # the same read — stays strictly tenanted.
    M.ProviderFundRegistry,
    # The market-data series the vs-Benchmark panel compares against (M173,
    # docs/28 decision 5). A close for `SPY` on a date is universal — there is
    # no owner to tenant it against — and it is deliberately NOT a
    # `securities` row, so no tenant's picker grows a fund no trade explains.
    M.BenchmarkPrice,
    # The principal itself. It self-references via `id`; there is no user to
    # tenant it against.
    M.User,
    # CHILD/join tables. Tenancy is inherited through the parent FK, which
    # CASCADEs, so a raw `DELETE` of the parent cannot leave an orphan that
    # outlives its tenant. Reached only via `bill_reminders.id`, `rule.id` and
    # `transaction.id` respectively — none of which is readable without its
    # parent, which IS tenanted.
    M.BillReminderMatchTerm,
    M.RuleTag,
    M.TransactionTag,
}


def test_the_derived_list_actually_found_every_table_model():
    """Guard the guard.

    If `app.models` stopped re-exporting a model (or the introspection above
    broke), DOMAIN_MODELS would shrink and every assertion below would quietly
    cover less. This pins the count the review measured, so a silent shrink is
    a red test instead of a quiet loss of coverage.
    """
    assert len(DOMAIN_MODELS) == 55, (
        f"expected 55 table models, found {len(DOMAIN_MODELS)} — "
        f"the tenancy assertions below now cover less than they did"
    )


def test_every_tenanted_model_has_user_id_and_timestamps():
    for model in DOMAIN_MODELS:
        assert issubclass(model, TimestampMixin), f"{model.__name__} missing TimestampMixin"
        if model in NO_USER_ID:
            assert "user_id" not in model.__table__.columns, (
                f"{model.__name__} unexpectedly has a user_id column — drop it from NO_USER_ID"
            )
        else:
            assert "user_id" in model.__table__.columns, (
                f"{model.__name__} missing user_id tenancy column"
            )


def test_enum_columns_use_native_enum_false():
    for model in DOMAIN_MODELS:
        for col in model.__table__.columns:
            if isinstance(col.type, sqlalchemy.Enum):
                assert col.type.native_enum is False, (
                    f"{model.__name__}.{col.name} enum must use native_enum=False"
                )


def test_category_orm_validates_into_public_schema():
    from app.schemas.categories import CategoryPublic

    cat = M.Category(id=1, user_id=1, name="Groceries", parent_id=None, is_system=True)
    public = CategoryPublic.model_validate(cat)
    assert public.id == 1
    assert public.user_id == 1
    assert public.name == "Groceries"
    assert public.is_system is True
    # The *Public contract requires from_attributes to read from ORM rows.
    assert CategoryPublic.model_config.get("from_attributes") is True


def test_public_schemas_use_from_attributes():
    from app.schemas.accounts import AccountPublic
    from app.schemas.imports import ImportBatchPublic
    from app.schemas.institutions import InstitutionPublic
    from app.schemas.rules import RulePublic
    from app.schemas.sync import AgreementPublic
    from app.schemas.transactions import TransactionPublic
    from app.schemas.users import UserPublic

    for schema in (
        AccountPublic,
        ImportBatchPublic,
        InstitutionPublic,
        RulePublic,
        AgreementPublic,
        TransactionPublic,
        UserPublic,
    ):
        assert schema.model_config.get("from_attributes") is True
