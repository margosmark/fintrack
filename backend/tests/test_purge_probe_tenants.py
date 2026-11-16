"""Behavioural tests for the probe-tenant backstop planner (scripts/purge_probe_tenants.py).

The planner is a pure function (FK graph in -> dependency-ordered DELETE
statements out), so these run without touching a database. FIXTURES: the real
FK graph of the production database as read on 2026-11-09 (card t_498845bf,
the day the 250-tenant purge ran) and the table list of the hand-written purge
that was dress-rehearsed and then APPLIED successfully against that exact
database. A generated plan that stops reproducing the proven plan fails here
before any sweep ever runs.

Covered:
  * the plan covers EXACTLY the 50 tables the proven purge deleted - no more,
    no less (a table added by a future migration must show up here)
  * topological validity: each table is deleted after every table that
    references it, self-FKs excluded from ordering
  * statement shapes: user_id tables delete by user_id ONLY (a data-FK limb
    there could reach another tenant's rows); join tables OR both sides;
    sharing_links covers partner_user_id too; the users DELETE is last and the
    demo exclusion rides in EVERY statement
  * fail-closed: an FK cycle raises instead of yielding a partial plan
  * the predicate is pinned to the one approved on the card
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "scripts"))

import purge_probe_tenants as purge  # noqa: E402

# Production FK graph (child|col|parent|col), read-only pg_constraint dump on
# example.internal prod, 2026-11-09.
PROD_FKS = [
    tuple(row.split("|"))
    for row in (
        "acb_pools|account_id|accounts|id",
        "acb_pools|security_id|securities|id",
        "acb_pools|user_id|users|id",
        "accounts|institution_id|institutions|id",
        "accounts|user_id|users|id",
        "backup_artifacts|user_id|users|id",
        "bill_reminder_match_terms|bill_reminder_id|bill_reminders|id",
        "bill_reminders|category_id|categories|id",
        "bill_reminders|user_id|users|id",
        "budget_carryovers|budget_id|budgets|id",
        "budget_carryovers|user_id|users|id",
        "budgets|category_id|categories|id",
        "budgets|user_id|users|id",
        "categories|parent_id|categories|id",
        "categories|user_id|users|id",
        "debts|account_id|accounts|id",
        "debts|user_id|users|id",
        "dividend_history|security_id|securities|id",
        "dividend_history|user_id|users|id",
        "dividend_history_refresh|security_id|securities|id",
        "dividend_history_refresh|user_id|users|id",
        "etf_holdings|holding_security_id|securities|id",
        "etf_holdings|security_id|securities|id",
        "etf_holdings|user_id|users|id",
        "fundamentals|security_id|securities|id",
        "fundamentals|user_id|users|id",
        "goals|account_id|accounts|id",
        "goals|user_id|users|id",
        "import_batches|account_id|accounts|id",
        "import_batches|user_id|users|id",
        "institutions|user_id|users|id",
        "learned_payees|category_id|categories|id",
        "learned_payees|user_id|users|id",
        "llm_suggestions|proposed_category_id|categories|id",
        "llm_suggestions|transaction_id|transactions|id",
        "llm_suggestions|user_id|users|id",
        "mcp_audit|user_id|users|id",
        "mcp_tokens|user_id|users|id",
        "notification_channels|user_id|users|id",
        "notification_deliveries|user_id|users|id",
        "notification_delivery_attempts|delivery_id|notification_deliveries|id",
        "notification_delivery_attempts|user_id|users|id",
        "notification_inbox_reads|user_id|users|id",
        "notification_settings|user_id|users|id",
        "oidc_links|user_id|users|id",
        "prices|security_id|securities|id",
        "prices|user_id|users|id",
        "reconciliation_items|account_id|accounts|id",
        "reconciliation_items|transaction_id|transactions|id",
        "reconciliation_items|user_id|users|id",
        "recovery_codes|user_id|users|id",
        "recurring_suppressions|user_id|users|id",
        "refresh_tokens|user_id|users|id",
        "rules|category_id|categories|id",
        "rules|user_id|users|id",
        "rule_tags|rule_id|rules|id",
        "rule_tags|tag_id|tags|id",
        "securities|user_id|users|id",
        "sharing_audit|user_id|users|id",
        "sharing_links|partner_user_id|users|id",
        "sharing_links|user_id|users|id",
        "spending_alerts|budget_id|budgets|id",
        "spending_alerts|user_id|users|id",
        "sync_agreements|user_id|users|id",
        "tags|user_id|users|id",
        "tax_projections|user_id|users|id",
        "totp_factors|user_id|users|id",
        "trades|account_id|accounts|id",
        "trades|import_batch_id|import_batches|id",
        "trades|security_id|securities|id",
        "trades|user_id|users|id",
        "transaction_events|user_id|users|id",
        "transaction_receipts|transaction_id|transactions|id",
        "transaction_receipts|user_id|users|id",
        "transactions|account_id|accounts|id",
        "transactions|category_id|categories|id",
        "transactions|import_batch_id|import_batches|id",
        "transactions|parent_split_id|transactions|id",
        "transactions|transfer_pair_id|transactions|id",
        "transactions|user_id|users|id",
        "transaction_tags|tag_id|tags|id",
        "transaction_tags|transaction_id|transactions|id",
        "vault_envelopes|user_id|users|id",
        "watchlist|security_id|securities|id",
        "watchlist|user_id|users|id",
        "webauthn_credentials|user_id|users|id",
    )
]

# Tables deleted by the proven, applied purge of card t_498845bf (workspace
# purge_candidates.sql, dress-rehearsed in BEGIN..ROLLBACK, then committed
# under Mark's GO - 250 tenants / 31,825 rows, verified 0 left).
PROVEN_TABLES = [
    "acb_pools",
    "backup_artifacts",
    "bill_reminder_match_terms",
    "bill_reminders",
    "budget_carryovers",
    "debts",
    "dividend_history",
    "dividend_history_refresh",
    "etf_holdings",
    "fundamentals",
    "goals",
    "learned_payees",
    "llm_suggestions",
    "mcp_audit",
    "mcp_tokens",
    "notification_channels",
    "notification_delivery_attempts",
    "notification_deliveries",
    "notification_inbox_reads",
    "notification_settings",
    "oidc_links",
    "prices",
    "reconciliation_items",
    "recovery_codes",
    "recurring_suppressions",
    "refresh_tokens",
    "rule_tags",
    "rules",
    "sharing_audit",
    "sharing_links",
    "spending_alerts",
    "budgets",
    "sync_agreements",
    "tax_projections",
    "totp_factors",
    "trades",
    "transaction_events",
    "transaction_receipts",
    "transaction_tags",
    "tags",
    "transactions",
    "categories",
    "import_batches",
    "accounts",
    "institutions",
    "vault_envelopes",
    "watchlist",
    "securities",
    "webauthn_credentials",
    "users",
]


def test_predicate_is_the_approved_one() -> None:
    assert purge.PREDICATE == "email ILIKE '%@example.com' AND email <> 'demo@fintracker.local'"


def test_plan_reproduces_the_proven_purge_table_set() -> None:
    plan = purge.plan_purge(PROD_FKS)
    tables = [table for table, _ in plan]
    assert len(tables) == len(set(tables)), "a table planned twice"
    only_generated = sorted(set(tables) - set(PROVEN_TABLES))
    only_proven = sorted(set(PROVEN_TABLES) - set(tables))
    assert not only_generated and not only_proven, (
        "generated plan diverges from the proven 50-statement purge: "
        f"only_generated={only_generated} only_proven={only_proven}"
    )


def test_plan_is_dependency_ordered_children_first() -> None:
    plan = purge.plan_purge(PROD_FKS)
    index = {table: i for i, (table, _) in enumerate(plan)}
    assert plan[-1][0] == "users", "the tenants row must be deleted last"
    for child, _ccol, parent, _pcol in PROD_FKS:
        if child == parent or parent not in index or child not in index:
            continue
        assert index[child] < index[parent], (
            f"{child} is deleted after {parent} it still references"
        )


def test_user_id_tables_delete_by_user_id_only() -> None:
    plan = dict(purge.plan_purge(PROD_FKS))
    # transactions also references accounts/categories/import_batches/itself:
    # those are DATA links; matching on them could reach other tenants' rows.
    assert "user_id IN (SELECT id FROM users WHERE" in plan["transactions"]
    assert "account_id IN" not in plan["transactions"]
    assert "category_id IN" not in plan["transactions"]
    # prices references securities (same shape): same rule.
    assert "user_id IN (SELECT id FROM users WHERE" in plan["prices"]
    assert "security_id IN" not in plan["prices"]


def test_join_tables_or_both_sides() -> None:
    plan = dict(purge.plan_purge(PROD_FKS))

    def limbs(table: str) -> set[str]:
        where = plan[table].split(" WHERE ", 1)[1]
        return {part.split(" IN ")[0] for part in where.split(" OR ")}

    # Both sides of each join must be covered; the OR limb order follows the
    # FK graph's column order, so assert the set, not the sequence.
    assert limbs("transaction_tags") == {"transaction_id", "tag_id"}
    assert limbs("rule_tags") == {"rule_id", "tag_id"}
    assert "SELECT id FROM transactions WHERE user_id IN" in plan["transaction_tags"]
    assert "SELECT id FROM tags WHERE user_id IN" in plan["transaction_tags"]
    assert plan["bill_reminder_match_terms"].startswith(
        "DELETE FROM bill_reminder_match_terms WHERE bill_reminder_id IN "
        "(SELECT id FROM bill_reminders WHERE"
    )


def test_sharing_links_covers_both_users_fks() -> None:
    plan = dict(purge.plan_purge(PROD_FKS))
    stmt = plan["sharing_links"]
    assert "user_id IN (SELECT id FROM users WHERE" in stmt
    assert "partner_user_id IN (SELECT id FROM users WHERE" in stmt, (
        "a link whose partner is a candidate tenant must die with it"
    )


def test_every_statement_excludes_the_demo_tenant() -> None:
    for table, stmt in purge.plan_purge(PROD_FKS):
        assert "email <> 'demo@fintracker.local'" in stmt, f"{table} lacks the demo guard"
        assert stmt.rstrip(";").startswith(f"DELETE FROM {table} WHERE "), table


def test_users_delete_is_the_approved_predicate_verbatim() -> None:
    plan = dict(purge.plan_purge(PROD_FKS))
    assert plan["users"] == f"DELETE FROM users WHERE {purge.PREDICATE}"


def test_a_future_tenanted_table_joins_the_plan() -> None:
    # A migration adding `widget_stubs.user_id` must be swept without editing
    # the script: coverage is derived from the live FK graph.
    extended = [*PROD_FKS, ("widget_stubs", "user_id", "users", "id")]
    plan = purge.plan_purge(extended)
    tables = [table for table, _ in plan]
    assert "widget_stubs" in tables
    assert tables[-1] == "users", "the new table must be deleted before users"
    assert dict(plan)["widget_stubs"] == (
        f"DELETE FROM widget_stubs WHERE user_id IN (SELECT id FROM users WHERE {purge.PREDICATE})"
    )


def test_an_fk_cycle_fails_closed() -> None:
    cyclic = [
        ("widget_a", "x", "users", "id"),
        ("widget_b", "y", "widget_a", "id"),
        ("widget_a", "z", "widget_b", "id"),
    ]
    try:
        purge.plan_purge(cyclic)
    except ValueError as exc:
        assert "FK cycle" in str(exc)
    else:
        raise AssertionError("a cycle must raise, never yield a partial plan")
