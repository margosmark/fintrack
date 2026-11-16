"""Row-level security table policies and grants

Four classes, every public table in exactly one of them (the coverage test
`tests/test_rls_coverage.py` is what proves that stays true as tables are
added):

* ``SECURED_TABLES`` — tenant-owned domain tables. One ``{table}_access``
  policy keyed to ``current_tenant_id()``; ``WITH CHECK`` is the same
  predicate, so the app role can only ever write rows stamped with the
  request's own tenant (the partner seat of a sharing link is read-only by
  construction).
* ``CHILD_TABLES`` — join tables with no ``user_id`` column of their own,
  scoped through an EXISTS against their tenant parent (the parent's own
  policy is what makes the EXISTS tenant-correct; no recursion, because no
  parent policy reads back a child).
* ``AUTH_TABLES`` — identity/credential tables queried BEFORE a request
  identity exists (login by email, token lookups by hash, the OIDC link
  match), so they stay out of RLS exactly as Lumina's do; every such query
  is keyed by an exact id or hash at the application layer.
* ``GLOBAL_READ_TABLES`` — shared reference caches every tenant reads
  (AGENTS §2's documented exception to per-tenant rows): readable by the
  app role with no policy. ``fx_rates`` also carries INSERT/UPDATE because
  manual rates are a request-path write (docs/07 manual-wins).

``sharing_links`` and ``sharing_audit`` are the two secured tables whose
policies are NOT the plain tenant predicate: the household-sharing accept
flow (docs/43) is the one place a second authenticated user legitimately
reads AND writes a row attributed to someone else, so their policies encode
the consent rules directly — with a column-level UPDATE grant on
``sharing_links`` so no update can re-point ``user_id`` and forge consent.

The policies reference the helper names from the functions module, so a
renamed helper flows through here without a separate edit. Function calls
are schema-qualified so a changed search_path cannot route them to a
different function.

Grants are conditional on the app role existing: a database that has not
run ``scripts/provision_rls_role.py`` still replays every migration (the
policies themselves are harmless without the role), and the startup
verification in ``app.core.rls`` is what refuses to serve requests from an
unprovisioned database. Because those grants ride this revision, a database
that reached head while the role was absent arms only by replaying it
(``alembic downgrade -1`` then ``upgrade head``, as the schema owner, with no
concurrent readers — docs/55 §Rollout). The ``REVOKE … FROM PUBLIC`` on the
helper functions is NOT conditional — it exists to keep every *other* cluster
role from calling the ``SECURITY DEFINER`` helpers, which is exactly the
access the app role's own grant is meant to be (docs/55 §Residual risks).
"""

import logging

from sqlalchemy import Connection, text

from app.db.rls.functions import (
    CURRENT_TENANT_EMAIL,
    CURRENT_TENANT_ID,
    HELPER_FUNCTION_SIGNATURES,
    PARTNER_VISIBLE_TENANTS,
    SHARING_LINK_IDENTITY_MATCHES,
)

logger = logging.getLogger(__name__)

# The unprivileged login role the request path connects as. The schema owner
# (``fintrack``, i.e. DATABASE_URL) keeps migration/background/script
# access and is deliberately NOT constrained by these policies — Postgres
# never applies RLS to a table's owner unless FORCE ROW LEVEL SECURITY is
# set, and it never applies it to superusers at all (production's ``fintrack``
# is both, verified on the deploy host).
APP_ROLE = "fintrack_app"

# Table privileges the app role holds on the data it serves
APP_TABLE_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE"

# The write predicate for the plain secured tables: WITH CHECK is OWN-tenant
# only, which is what keeps the partner seat of a sharing link read-only —
# the partner's reads widen through the helper below, never their writes.
_TENANT_WRITE = f"user_id = {CURRENT_TENANT_ID}()"

# The read predicate: own rows, plus the owners who seated the caller as
# their partner (docs/43 household sharing — the partner projections read the
# owner's rows through the request session). The helper is set-returning and
# uncorrelated, so a policy hoists it into one init-plan per statement.
_TENANT_READ = f"user_id = {CURRENT_TENANT_ID}() OR user_id IN (SELECT {PARTNER_VISIBLE_TENANTS}())"

# (table, USING, WITH CHECK) for tables secured by a single policy. The two
# sharing tables carry None/None because their policies are hand-written
# consent rules — see `_secure_sharing_links` / `_secure_sharing_audit`.
SECURED_TABLES: tuple[tuple[str, str | None, str | None], ...] = (
    ("acb_pools", _TENANT_READ, _TENANT_WRITE),
    ("accounts", _TENANT_READ, _TENANT_WRITE),
    # docs/57 — the assistant's proposal + append-only audit rows. The audit
    # entry is registered for cascade BEFORE the proposal it points at.
    ("assistant_audit", _TENANT_READ, _TENANT_WRITE),
    ("assistant_proposals", _TENANT_READ, _TENANT_WRITE),
    ("backup_artifacts", _TENANT_READ, _TENANT_WRITE),
    ("bill_reminders", _TENANT_READ, _TENANT_WRITE),
    ("budget_carryovers", _TENANT_READ, _TENANT_WRITE),
    ("budgets", _TENANT_READ, _TENANT_WRITE),
    ("categories", _TENANT_READ, _TENANT_WRITE),
    ("debts", _TENANT_READ, _TENANT_WRITE),
    ("dividend_history", _TENANT_READ, _TENANT_WRITE),
    ("dividend_history_refresh", _TENANT_READ, _TENANT_WRITE),
    ("etf_holdings", _TENANT_READ, _TENANT_WRITE),
    ("fundamentals", _TENANT_READ, _TENANT_WRITE),
    ("goals", _TENANT_READ, _TENANT_WRITE),
    ("import_batches", _TENANT_READ, _TENANT_WRITE),
    ("institutions", _TENANT_READ, _TENANT_WRITE),
    ("learned_payees", _TENANT_READ, _TENANT_WRITE),
    ("llm_suggestions", _TENANT_READ, _TENANT_WRITE),
    ("mcp_audit", _TENANT_READ, _TENANT_WRITE),
    ("notification_channels", _TENANT_READ, _TENANT_WRITE),
    ("notification_deliveries", _TENANT_READ, _TENANT_WRITE),
    ("notification_delivery_attempts", _TENANT_READ, _TENANT_WRITE),
    ("notification_inbox_reads", _TENANT_READ, _TENANT_WRITE),
    ("notification_settings", _TENANT_READ, _TENANT_WRITE),
    ("prices", _TENANT_READ, _TENANT_WRITE),
    ("reconciliation_items", _TENANT_READ, _TENANT_WRITE),
    ("recurring_suppressions", _TENANT_READ, _TENANT_WRITE),
    ("rules", _TENANT_READ, _TENANT_WRITE),
    ("securities", _TENANT_READ, _TENANT_WRITE),
    ("sharing_audit", None, None),  # partner-aware — see _secure_sharing_audit
    ("sharing_links", None, None),  # consent rules — see _secure_sharing_links
    ("spending_alerts", _TENANT_READ, _TENANT_WRITE),
    ("sync_agreements", _TENANT_READ, _TENANT_WRITE),
    ("tags", _TENANT_READ, _TENANT_WRITE),
    ("tax_projections", _TENANT_READ, _TENANT_WRITE),
    ("trades", _TENANT_READ, _TENANT_WRITE),
    ("transaction_events", _TENANT_READ, _TENANT_WRITE),
    ("transaction_receipts", _TENANT_READ, _TENANT_WRITE),
    ("transactions", _TENANT_READ, _TENANT_WRITE),
    ("vault_envelopes", _TENANT_READ, _TENANT_WRITE),
    ("watchlist", _TENANT_READ, _TENANT_WRITE),
)

# Join tables without their own user_id, scoped through their tenant parent.
# (table, parent) — the EXISTS reads the parent under the parent's policy, so
# "visible" already means "the current tenant's (or partner-visible) row".
CHILD_TABLES: tuple[tuple[str, str], ...] = (
    ("bill_reminder_match_terms", "bill_reminders"),
    ("rule_tags", "rules"),
    ("transaction_tags", "transactions"),
)

# Auth tables stay out of RLS because the login and token flows query them
# before a request identity exists, always by exact id or hash (AGENTS §2
# keeps the application filter on every one of them anyway).
AUTH_TABLES: tuple[str, ...] = (
    "mcp_tokens",
    "oidc_links",
    "recovery_codes",
    "refresh_tokens",
    "totp_factors",
    "users",
    "webauthn_credentials",
)

# Shared reference caches every tenant reads (docs/02): read policy for the
# app role, no row scoping. `fx_rates` additionally takes the request-path
# manual-rate write (docs/07 §Manual wins).
GLOBAL_READ_TABLES: tuple[str, ...] = (
    "benchmark_prices",
    "fx_rates",
    "provider_fund_registry",
)

# Privileges beyond SELECT on individual global-read tables
_GLOBAL_TABLE_PRIVILEGES: dict[str, str] = {"fx_rates": "SELECT, INSERT, UPDATE"}


def apply_policies(connection: Connection) -> None:
    """Enable the policies and grant the app role the access it serves"""
    role_exists = _app_role_exists(connection)
    if not role_exists:
        # Report, never execute. This string used to reach Postgres as
        # `connection.execute(text("NOTICE: …"))` — i.e. as SQL — so any
        # database upgraded before the role was provisioned died with
        # `syntax error at or near "NOTICE"` instead of taking the
        # "policies without grants" path the message promises (the exact
        # failure that crash-looped the api on the 2026-10-31 production
        # deploy of b6cde5a2; docs/55 §Rollout). Every statement below is
        # already guarded on `role_exists`, so skipping the grants is the
        # whole of the degraded behaviour.
        # The remediation names the REPLAY as well as provisioning: the
        # grants ride this revision, so a database that has since reached
        # head gets them only by re-running it (docs/55 §Rollout step 3).
        logger.warning(
            "role %r does not exist on this cluster — applying the row-level"
            " security policies WITHOUT grants. Run"
            " backend/scripts/provision_rls_role.py and then `alembic upgrade"
            " head` before serving requests (docs/55) — and if this database"
            " is already at head, that upgrade re-applies nothing, so replay"
            " this revision as the schema owner: `alembic downgrade -1` then"
            " `upgrade head`, with no concurrent readers (its downgrade takes"
            " ACCESS-EXCLUSIVE locks).",
            APP_ROLE,
        )

    for table, using_expr, check_expr in SECURED_TABLES:
        # A table added after this migration is created and secured by its own
        # later migration, so a from-scratch upgrade reaches this loop before
        # that table exists, while the test harness builds the full schema
        # first and secures it here. The coverage guard test is what proves
        # every registered table ends up secured.
        if not _table_exists(connection, table):
            continue
        if using_expr is None or check_expr is None:
            continue  # sharing tables have their own policies, below
        _secure_table(connection, table, using_expr, check_expr, role_exists)

    if _table_exists(connection, "sharing_links"):
        _secure_sharing_links(connection, role_exists)
    if _table_exists(connection, "sharing_audit"):
        _secure_sharing_audit(connection, role_exists)

    for table, parent in CHILD_TABLES:
        if _table_exists(connection, table):
            _secure_child_table(connection, table, parent, role_exists)

    for table in GLOBAL_READ_TABLES:
        if not _table_exists(connection, table):
            continue
        privileges = _GLOBAL_TABLE_PRIVILEGES.get(table, "SELECT")
        # REVOKE first: this grant is exact, and the globals are the only
        # registry entries narrower than APP_TABLE_PRIVILEGES. A table
        # re-created while the defaults were armed arrives ALREADY holding
        # the app role's CRUD, which a plain GRANT never takes back — the
        # coverage test asserts the registry exactly (docs/55 §Policies).
        _revoke(connection, f"REVOKE ALL ON public.{table} FROM", role_exists)
        _grant(connection, f"GRANT {privileges} ON public.{table} TO", role_exists)

    for table in AUTH_TABLES:
        if not _table_exists(connection, table):
            continue
        _grant(connection, f"GRANT {APP_TABLE_PRIVILEGES} ON public.{table} TO", role_exists)

    # Schema + sequence access: every table carries a serial/bigserial id, so
    # INSERT needs USAGE on the sequence as well as the table grant.
    _grant(connection, "GRANT USAGE ON SCHEMA public TO", role_exists)
    _grant(connection, "GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO", role_exists)

    # Tables and sequences created by FUTURE migrations inherit these, so a
    # new table is born granted (and unclassified until its migration secures
    # it — the coverage test names it either way).
    if role_exists:
        migrator = connection.execute(text("SELECT current_user")).scalar_one()
        connection.execute(
            text(
                f'ALTER DEFAULT PRIVILEGES FOR ROLE "{migrator}" IN SCHEMA public'
                f' GRANT {APP_TABLE_PRIVILEGES} ON TABLES TO "{APP_ROLE}"'
            )
        )
        connection.execute(
            text(
                f'ALTER DEFAULT PRIVILEGES FOR ROLE "{migrator}" IN SCHEMA public'
                f' GRANT USAGE ON SEQUENCES TO "{APP_ROLE}"'
            )
        )

    # The app role evaluates the policies, so it must execute the helpers;
    # their SECURITY DEFINER owner is what reaches the underlying rows. Each
    # helper is keyed to the executing request's own GUC (or is a
    # pure comparison), so execute access leaks nothing beyond the caller's
    # *stamped* tenant — and the stamp is the executing role's own to set
    # (docs/55 §Residual risks), which is why EXECUTE below is granted to the
    # app role alone.
    for helper in (
        CURRENT_TENANT_ID,
        CURRENT_TENANT_EMAIL,
        PARTNER_VISIBLE_TENANTS,
        SHARING_LINK_IDENTITY_MATCHES,
    ):
        _grant(connection, f"GRANT EXECUTE ON FUNCTION {helper} TO", role_exists)

    # EXECUTE on a function defaults to PUBLIC, and since PG16 CONNECT does
    # too — so without this revoke any cluster role that can reach the
    # database could call the SECURITY DEFINER helpers against a tenant it
    # stamps for itself (docs/55 §Residual risks). The app role keeps its
    # explicit grant above; PUBLIC gets nothing.
    for signature in HELPER_FUNCTION_SIGNATURES:
        _revoke_public_execute(connection, signature)

    # CONNECT is per-database: without it the app role cannot open the
    # database its URL names at all (the provisioning script only creates
    # the cluster-wide role).
    db_name = connection.execute(text("SELECT current_database()")).scalar_one()
    _grant(connection, f'GRANT CONNECT ON DATABASE "{db_name}" TO', role_exists)


def secure_registered_table(connection: Connection, table: str) -> None:
    """Enable row-level security on one already-created table using its registered policy

    Incremental migrations that add a secured table call this so the single
    SECURED_TABLES / CHILD_TABLES definitions stay the only source of a
    table's access rule.
    """
    for name, using_expr, check_expr in SECURED_TABLES:
        if name == table:
            if using_expr is None or check_expr is None:
                if table == "sharing_links":
                    _secure_sharing_links(connection, _app_role_exists(connection))
                elif table == "sharing_audit":
                    _secure_sharing_audit(connection, _app_role_exists(connection))
                return
            _secure_table(connection, name, using_expr, check_expr, _app_role_exists(connection))
            return
    for name, parent in CHILD_TABLES:
        if name == table:
            _secure_child_table(connection, name, parent, _app_role_exists(connection))
            return
    raise KeyError(f"No registered row-level security policy for table {table!r}")


def grant_auth_table(connection: Connection, table: str) -> None:
    """Grant the app role on one auth table added after this migration

    Auth tables carry no row-level policy, so an incremental migration that
    adds one calls this to give the app role the same access this migration
    grants the original auth tables.
    """
    if table not in AUTH_TABLES:
        raise KeyError(f"{table!r} is not a registered auth table")
    _grant(
        connection,
        f"GRANT {APP_TABLE_PRIVILEGES} ON public.{table} TO",
        _app_role_exists(connection),
    )


def grant_global_read_table(connection: Connection, table: str) -> None:
    """Grant the app role on one global-read table added after this migration

    Global-read tables carry no row-level policy, so an incremental migration
    that adds one calls this to give the app role the read access this
    migration grants the original ones.
    """
    if table not in GLOBAL_READ_TABLES:
        raise KeyError(f"{table!r} is not a registered global-read table")
    privileges = _GLOBAL_TABLE_PRIVILEGES.get(table, "SELECT")
    _grant(connection, f"GRANT {privileges} ON public.{table} TO", _app_role_exists(connection))


def drop_policies(connection: Connection) -> None:
    """Drop every policy and disable row-level security on every public table

    Both statements take ACCESS EXCLUSIVE on the target table, so a sweep
    that meets an open transaction used to abort the whole migration at the
    database's `lock_timeout` — that is what QA t_592a6e1a F1 reproduced
    (an idle-in-transaction reader on `securities` and the suite's 500ms
    timeout). Each statement now retries with a growing pause instead of
    failing on first contact, which rides out a transient reader. It does
    NOT skip a table that stays busy: a downgrade that silently leaves
    policies behind would report a clean rollback it did not perform, so
    after the last attempt this raises, naming the statement and the last
    PostgreSQL error.
    """
    connection.execute(text(_DROP_WITH_RETRY))


# Per-statement retry for the exclusive-lock sweep in `drop_policies`. The
# exception block makes each attempt its own subtransaction, so a failed
# statement leaves nothing behind; the pause grows so the sweep yields to a
# short reader instead of colliding with it on every retry.
_RETRY_UNTIL = 8
_DROP_WITH_RETRY = (
    # This DDL is fixed literals plus names read from pg_policies/pg_tables
    # (format()'d with %I), never caller input -- S608 has nothing to inject.
    "DO $$ DECLARE r record; stmt text; attempt int; last_error text; BEGIN "  # noqa: S608
    "FOR r IN SELECT tablename, policyname FROM pg_policies"
    " WHERE schemaname = 'public' ORDER BY tablename, policyname LOOP "
    "stmt := format('DROP POLICY %I ON public.%I', r.policyname, r.tablename); "
    f"attempt := 1; "
    "LOOP BEGIN EXECUTE stmt; EXIT; "
    "EXCEPTION WHEN lock_not_available OR deadlock_detected THEN "
    "last_error := SQLERRM; "
    f"IF attempt >= {_RETRY_UNTIL} THEN RAISE EXCEPTION "
    "'drop_policies: % still blocked after {_RETRY_UNTIL} attempts (%)',"
    " stmt, last_error; END IF; "
    "PERFORM pg_sleep(0.25 * attempt); attempt := attempt + 1; END; END LOOP; "
    "END LOOP; "
    "FOR r IN SELECT tablename FROM pg_tables"
    " WHERE schemaname = 'public' ORDER BY tablename LOOP "
    "stmt := format('ALTER TABLE public.%I DISABLE ROW LEVEL SECURITY', r.tablename); "
    "attempt := 1; "
    "LOOP BEGIN EXECUTE stmt; EXIT; "
    "EXCEPTION WHEN lock_not_available OR deadlock_detected THEN "
    "last_error := SQLERRM; "
    f"IF attempt >= {_RETRY_UNTIL} THEN RAISE EXCEPTION "
    "'drop_policies: % still blocked after {_RETRY_UNTIL} attempts (%)',"
    " stmt, last_error; END IF; "
    "PERFORM pg_sleep(0.25 * attempt); attempt := attempt + 1; END; END LOOP; "
    "END LOOP; END $$"
)


def drop_default_privileges(connection: Connection) -> None:
    """Disarm the default privileges `apply_policies` armed for the app role

    `ALTER DEFAULT PRIVILEGES … GRANT` outlives `drop_policies`, which only
    turns RLS off. Left armed, the NEXT table the migrator creates is born
    with the app role's full CRUD — and a downgrade→upgrade range that
    re-creates an existing one hands it that CRUD retroactively: m106's
    downgrade to 0038 replays 0050/0052, so `benchmark_prices` and
    `provider_fund_registry` came back carrying `DELETE, INSERT, UPDATE`
    beside the `SELECT` the registry declares (docs/55 §Policies, and the
    exact-registry claim the coverage test asserts).

    Symmetric with the two `ALTER DEFAULT PRIVILEGES` statements in
    `apply_policies`, which is what makes 0058's downgrade genuinely
    reversible instead of reverting only the objects and not their
    birth-right grants.
    """
    if not _app_role_exists(connection):
        return
    migrator = connection.execute(text("SELECT current_user")).scalar_one()
    connection.execute(
        text(
            f'ALTER DEFAULT PRIVILEGES FOR ROLE "{migrator}" IN SCHEMA public'
            f' REVOKE {APP_TABLE_PRIVILEGES} ON TABLES FROM "{APP_ROLE}"'
        )
    )
    connection.execute(
        text(
            f'ALTER DEFAULT PRIVILEGES FOR ROLE "{migrator}" IN SCHEMA public'
            f' REVOKE USAGE ON SEQUENCES FROM "{APP_ROLE}"'
        )
    )


def _secure_table(
    connection: Connection, table: str, using_expr: str, check_expr: str, role_exists: bool
) -> None:
    """Enable RLS on a table, add its single access policy, and grant the app role"""
    connection.execute(text(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY"))
    # DROP first so re-applying (the test harness secures the full schema
    # after migrations, incremental migrations re-run this) is idempotent.
    connection.execute(text(f"DROP POLICY IF EXISTS {table}_access ON public.{table}"))
    connection.execute(
        text(
            f"CREATE POLICY {table}_access ON public.{table}"
            f" USING ({using_expr}) WITH CHECK ({check_expr})"
        )
    )
    _grant(connection, f"GRANT {APP_TABLE_PRIVILEGES} ON public.{table} TO", role_exists)


def _secure_child_table(connection: Connection, table: str, parent: str, role_exists: bool) -> None:
    """Scope a join table through an EXISTS against its tenant parent"""
    # The predicate names the parent FK column per table; every registered
    # child joins its parent on the parent's id.
    fk_column = {
        "bill_reminder_match_terms": "bill_reminder_id",
        "rule_tags": "rule_id",
        "transaction_tags": "transaction_id",
    }[table]
    predicate = f"EXISTS (SELECT 1 FROM public.{parent} p WHERE p.id = public.{table}.{fk_column})"  # noqa: S608 -- parent/table come from the fixed _CHILD_FK map above
    _secure_table(connection, table, predicate, predicate, role_exists)


def _secure_sharing_links(connection: Connection, role_exists: bool) -> None:
    """Consent rules for `sharing_links` (docs/43)

    The accept flow is the one cross-tenant WRITE in the product: the
    invitee (partner seat) consumes an invite the owner created, so the
    policies encode who may see / create / transition a link:

    * SELECT — the owner, the seated partner, and any signed-in user whose
      OWN email matches the invite (the token lookup happens before a
      partner id exists, and before ``partner_user_id`` is set).
    * INSERT — owners only, keyed to their own tenant: an authenticated
      caller can never mint a link pointing at somebody else's data.
    * UPDATE — owners fully; the seated partner only while staying seated
      (the WITH CHECK re-reads ``partner_user_id = tenant``, so a partner
      cannot re-point the seat at a third party); plus the TTL arm that
      materializes ``expired`` on an unaccepted invite addressed to the
      caller (grants no access — the visibility helper needs an ACTIVE,
      seated link). Every arm is ANDed with
      ``sharing_link_identity_matches(id, user_id, email, token_hash)``:
      WITH CHECK sees the NEW row while the table still holds the OLD one,
      so the SECURITY DEFINER helper answers "did this update re-point the
      invite's owner, email or token?" — and the ORM writes every mapped
      column on update, which is why this helper, not a column-level grant,
      is what makes consent unforgeable (a seated partner who could set
      ``user_id`` to a victim would silently manufacture consent).
    * DELETE — the same visibility as SELECT (account-deletion cascade and
      owner cleanup).
    """
    owner = f"user_id = {CURRENT_TENANT_ID}()"
    seated = f"partner_user_id = {CURRENT_TENANT_ID}()"
    addressed = f"lower(email) = {CURRENT_TENANT_EMAIL}()"
    visible = f"{owner} OR {seated} OR {addressed}"
    unchanged = f"{SHARING_LINK_IDENTITY_MATCHES}(id, user_id, email, token_hash)"
    # The TTL arm is the only one that grants nothing: the visibility helper
    # needs an ACTIVE, seated link, and this row is still unseated.
    ttl = f"({addressed} AND partner_user_id IS NULL AND status = 'expired')"
    update_check = f"({owner} OR {seated} OR {ttl}) AND {unchanged}"

    connection.execute(text("ALTER TABLE public.sharing_links ENABLE ROW LEVEL SECURITY"))
    for name, kind, using, check in (
        ("sharing_links_select", "SELECT", visible, None),
        ("sharing_links_insert", "INSERT", None, owner),
        (
            "sharing_links_update",
            "UPDATE",
            visible,
            update_check,
        ),
        ("sharing_links_delete", "DELETE", visible, None),
    ):
        connection.execute(text(f"DROP POLICY IF EXISTS {name} ON public.sharing_links"))
        clauses = []
        if using is not None:
            clauses.append(f"USING ({using})")
        if check is not None:
            clauses.append(f"WITH CHECK ({check})")
        connection.execute(
            text(f"CREATE POLICY {name} ON public.sharing_links FOR {kind} {' '.join(clauses)}")
        )

    _grant(connection, f"GRANT {APP_TABLE_PRIVILEGES} ON public.sharing_links TO", role_exists)


def _secure_sharing_audit(connection: Connection, role_exists: bool) -> None:
    """Receipt ledger for sharing events (docs/43 §3)

    Every receipt is attributed to the OWNER's tenant even when the actor is
    the partner (D7), so the predicate is "my tenant, or the owner of a link
    I can see" — the same visibility the sharing policies grant, evaluated
    through them (the subquery reads `sharing_links` under that table's own
    policies, so a caller cannot widen the set). Un-correlated with respect
    to the audited row, so it plans as one init-plan per statement.
    """
    predicate = (
        f"user_id = {CURRENT_TENANT_ID}() OR user_id IN ("  # noqa: S608 -- fixed policy predicate text
        "SELECT sl.user_id FROM public.sharing_links sl)"
    )
    _secure_table(connection, "sharing_audit", predicate, predicate, role_exists)


def _table_exists(connection: Connection, table: str) -> bool:
    """Whether a public table is already present in the database"""
    return (
        connection.execute(
            text("SELECT to_regclass(:qualified)"), {"qualified": f"public.{table}"}
        ).scalar()
        is not None
    )


def _app_role_exists(connection: Connection) -> bool:
    """Whether the app role has been provisioned on this cluster"""
    return connection.execute(
        text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"), {"role": APP_ROLE}
    ).scalar_one()


def _grant(connection: Connection, statement_prefix: str, role_exists: bool) -> None:
    """Run a GRANT statement addressed at the app role, only if it exists"""
    if not role_exists:
        return
    connection.execute(text(f'{statement_prefix} "{APP_ROLE}"'))


def _revoke(connection: Connection, statement_prefix: str, role_exists: bool) -> None:
    """Run a REVOKE statement addressed at the app role, only if it exists

    Same conditional as `_grant`: a database with no app role must not have a
    REVOKE naming it (PostgreSQL errors on an unknown grantee), and the
    statements stay paired so `apply_policies` can tighten a grant back to
    exactly the registry after a table arrives over-privileged.
    """
    if not role_exists:
        return
    connection.execute(text(f'{statement_prefix} "{APP_ROLE}"'))


def _revoke_public_execute(connection: Connection, signature: str) -> None:
    """Drop the default PUBLIC EXECUTE on one helper, when it exists

    Guarded on `to_regprocedure` so `apply_policies` stays callable on a
    database whose helpers are not present (a policies-only re-apply after
    `revoke_rls`), where the bare REVOKE would fail the migration.
    """
    exists = connection.execute(
        text("SELECT to_regprocedure(:signature) IS NOT NULL"), {"signature": signature}
    ).scalar_one()
    if exists:
        connection.execute(text(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC"))
