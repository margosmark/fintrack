"""initial schema — every docs/02 table

Column and constraint order mirrors the SQLModel metadata DDL exactly so the
migration and models can be verified against each other mechanically.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(*values: str) -> sa.Enum:
    return sa.Enum(*values, native_enum=False, length=32, nullable=False)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    ]


def upgrade() -> None:
    op.create_table(
        "users",
        *_timestamps(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("hashed_password", sa.String(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("privacy_mode", sa.Boolean(), nullable=False),
        sa.Column("llm_provider", _enum("ollama", "openai_compatible"), nullable=False),
        sa.Column("llm_base_url", sa.String(), nullable=True),
        sa.Column("llm_model", sa.String(), nullable=True),
        sa.Column("llm_api_key", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=False)

    op.create_table(
        "refresh_tokens",
        *_timestamps(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index(op.f("ix_refresh_tokens_user_id"), "refresh_tokens", ["user_id"], unique=False)

    op.create_table(
        "institutions",
        *_timestamps(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("importer_profile", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index(op.f("ix_institutions_user_id"), "institutions", ["user_id"], unique=False)

    op.create_table(
        "accounts",
        *_timestamps(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("institution_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "type",
            _enum(
                "chequing",
                "savings",
                "credit_card",
                "line_of_credit",
                "brokerage_non_registered",
                "rrsp",
                "tfsa",
                "fhsa",
                "resp",
            ),
            nullable=False,
        ),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("taxable", sa.Boolean(), nullable=False),
        sa.Column("llm_autopilot", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["institution_id"], ["institutions.id"]),
    )
    op.create_index(op.f("ix_accounts_user_id"), "accounts", ["user_id"], unique=False)

    op.create_table(
        "categories",
        *_timestamps(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["parent_id"], ["categories.id"]),
    )
    op.create_index(op.f("ix_categories_user_id"), "categories", ["user_id"], unique=False)

    op.create_table(
        "import_batches",
        *_timestamps(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("institution", sa.String(), nullable=False),
        sa.Column("file_name", sa.String(), nullable=False),
        sa.Column(
            "status",
            _enum("pending_review", "committed", "discarded"),
            nullable=False,
        ),
        sa.Column("imported", sa.Integer(), nullable=False),
        sa.Column("duplicates_skipped", sa.Integer(), nullable=False),
        sa.Column("unresolved", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
    )
    op.create_index(
        op.f("ix_import_batches_account_id"), "import_batches", ["account_id"], unique=False
    )

    op.create_table(
        "transactions",
        *_timestamps(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=True),
        sa.Column("raw_description", sa.String(), nullable=False),
        sa.Column("normalized_payee", sa.String(), nullable=False),
        sa.Column("amount_native", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("cad_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("import_batch_id", sa.Integer(), nullable=True),
        sa.Column("dedup_hash", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "dedup_hash", name="uq_transactions_account_dedup_hash"),
        sa.ForeignKeyConstraint(
            ["import_batch_id"], ["import_batches.id"], name="fk_transactions_import_batch"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"]),
    )
    op.create_index(
        "ix_transactions_user_id_account_id_date",
        "transactions",
        ["user_id", "account_id", "date"],
        unique=False,
    )
    op.create_index(
        "ix_transactions_user_id_category_id",
        "transactions",
        ["user_id", "category_id"],
        unique=False,
    )
    op.create_index(
        "ix_transactions_user_id_date",
        "transactions",
        ["user_id", "date"],
        unique=False,
    )

    op.create_table(
        "securities",
        *_timestamps(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", _enum("stock", "etf", "mutual_fund"), nullable=False),
        sa.Column("listing_currency", sa.String(), nullable=False),
        sa.Column("exchange", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "symbol", "exchange", name="uq_securities_symbol"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )

    op.create_table(
        "trades",
        *_timestamps(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("security_id", sa.Integer(), nullable=False),
        sa.Column(
            "action",
            _enum(
                "buy",
                "sell",
                "dividend",
                "distribution",
                "return_of_capital",
                "drip",
                "fee",
                "contribution",
                "withdrawal",
                "transfer_in",
                "transfer_out",
                "corporate_action_adjustment",
            ),
            nullable=False,
        ),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("price_native", sa.Numeric(20, 8), nullable=False),
        sa.Column("commission", sa.Numeric(18, 2), nullable=False),
        sa.Column("fx_rate_used", sa.Numeric(20, 8), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"]),
    )
    op.create_index(
        "ix_trades_user_account_security_date",
        "trades",
        ["user_id", "account_id", "security_id", "date"],
        unique=False,
    )

    op.create_table(
        "acb_pools",
        *_timestamps(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("security_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("total_acb_cad", sa.Numeric(18, 2), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "security_id", name="uq_acb_pools_account_security"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"]),
    )

    op.create_table(
        "fx_rates",
        *_timestamps(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("base", sa.String(), nullable=False),
        sa.Column("quote", sa.String(), nullable=False),
        sa.Column("rate", sa.Numeric(20, 8), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("source", _enum("boc_valet", "manual"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("base", "quote", "date", name="uq_fx_rates_base_quote_date"),
    )

    op.create_table(
        "rules",
        *_timestamps(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("match_type", _enum("regex", "exact_payee", "contains"), nullable=False),
        sa.Column("pattern", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"]),
    )

    op.create_table(
        "llm_suggestions",
        *_timestamps(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("proposed_category_id", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", _enum("suggested", "accepted", "rejected"), nullable=False),
        sa.Column("model_used", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"]),
        sa.ForeignKeyConstraint(["proposed_category_id"], ["categories.id"]),
    )
    op.create_index(
        "ix_llm_suggestions_transaction_pending",
        "llm_suggestions",
        ["transaction_id"],
        unique=False,
        postgresql_where=sa.text("status = 'suggested'"),
    )

    op.create_table(
        "learned_payees",
        *_timestamps(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("normalized_payee", sa.String(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "normalized_payee", name="uq_learned_payees_user_payee"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"]),
    )

    op.create_table(
        "prices",
        *_timestamps(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("security_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("price", sa.Numeric(20, 8), nullable=False),
        sa.Column("source", _enum("manual", "yfinance"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("security_id", "date", name="uq_prices_security_date"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"]),
    )


def downgrade() -> None:
    op.drop_table("prices")
    op.drop_table("learned_payees")
    op.drop_index("ix_llm_suggestions_transaction_pending", table_name="llm_suggestions")
    op.drop_table("llm_suggestions")
    op.drop_table("rules")
    op.drop_table("fx_rates")
    op.drop_table("acb_pools")
    op.drop_index("ix_trades_user_account_security_date", table_name="trades")
    op.drop_table("trades")
    op.drop_table("securities")
    op.drop_index("ix_transactions_user_id_date", table_name="transactions")
    op.drop_index("ix_transactions_user_id_category_id", table_name="transactions")
    op.drop_index("ix_transactions_user_id_account_id_date", table_name="transactions")
    op.drop_table("transactions")
    op.drop_index("ix_import_batches_account_id", table_name="import_batches")
    op.drop_table("import_batches")
    op.drop_index("ix_categories_user_id", table_name="categories")
    op.drop_table("categories")
    op.drop_index("ix_accounts_user_id", table_name="accounts")
    op.drop_table("accounts")
    op.drop_index("ix_institutions_user_id", table_name="institutions")
    op.drop_table("institutions")
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
