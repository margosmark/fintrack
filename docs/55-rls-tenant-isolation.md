# 55 — Row-level security for tenant isolation (defense in depth)

## Context

Tenant isolation was enforced purely in application code: every query carries a
`user_id` filter (AGENTS §2). One forgotten filter in a new query is a
cross-tenant data leak, and nothing in the database would stop it. This doc
pins the second lock: Postgres row-level security (RLS), keyed to a
per-request tenant context, following the pattern Lumina-Finance/lumina-finance
uses in `backend/app/db/rls/` (inspected at
`7d6e740c370cae33f48e9ac4d9bd72617a4bb664`): `SECURITY DEFINER` helper
functions (each paired with its drop signature), table policies generated from
a single registry, and coverage/isolation tests that keep the registry true.

**RLS is a second lock, never the only one.** Every existing application-level
`tenant_id` filter stays exactly where it is: dropping them would make app
behaviour depend on database configuration and would break local dev plus the
test fixtures. The two layers are independent — either one alone still scopes
the data, and a defect in one is caught by the other.

## Approach

### Roles

Postgres never applies RLS to a table's owner, and never to a superuser. The
deployed `fintrack` role is both (verified on the deploy host), so the request
path can never be constrained on that role. Two roles, two jobs:

| Role | Created by | Used by | RLS |
|---|---|---|---|
| `fintrack` (`DATABASE_URL`) | compose / cluster | alembic, background workers, post-response `BackgroundTasks`, scripts, operator psql, the test suite | exempt (owner) |
| `fintrack_app` (`APP_DATABASE_URL`) | `backend/scripts/provision_rls_role.py` | the request path (`get_db`) | enforced |

`APP_DATABASE_URL` is optional and defaults to unset: with it unset, `get_db`
keeps using the maintenance engine and nothing changes anywhere (local dev,
the test suite, any deployment mid-rollout) — the startup log names this state
explicitly. With it set, `app.core.rls.verify_request_role` checks the LIVE
connection at boot and refuses to start unless the role is not superuser, not
`BYPASSRLS`, does not own any `public` table, and can actually read
`public.transactions` (i.e. the migration's grants are present). A
misconfiguration therefore fails loudly instead of enforcing nothing.

The provisioning script creates only the cluster-wide role (password:
`APP_ROLE_PASSWORD`, falling back to the `DATABASE_URL` password). Every
per-database grant — `CONNECT`, schema usage, table privileges, sequence
usage, the helpers, and the default privileges for future tables — is applied
by the migration, so `alembic upgrade head` (which the api container already
runs before uvicorn) is what arms each database. When the role does not
exist, the migration still applies the policies, skips every grant, and logs
a warning naming the provisioning script — it is a report, never a statement
sent to Postgres (a `NOTICE:` string executed as SQL is the SyntaxError that
crash-looped the 2026-10-31 production deploy, `b6cde5a2`) — so
replay-from-scratch never depends on cluster provisioning.

**Provisioning an existing database at head does NOT arm it.** The grants ride
revision `0058`, and `alembic upgrade head` on a database that is already at
head re-applies nothing. A database that reached head while the role was
absent — which is exactly what the first deploy of `0058` to an unprovisioned
cluster produces, and the degraded state the warning above reports — keeps zero
grants after `provision_rls_role.py` creates the role, and arming
`APP_DATABASE_URL` against it trips the fail-closed refusal in
`app/core/rls.py` ("the request role cannot read public.transactions"). The
replay is `0058` itself: as the schema owner, `alembic downgrade
0057_price_fetched_at` followed by `alembic upgrade head`. The target is
spelled out rather than `-1`: bare `-1` only meant "back to 0057" while 0058
was the tip of the chain, and every migration stacked on top of RLS (M188's
`0059`, M189's `0060`, …) moves it. Run it with **no
concurrent readers attached** — `0058`'s downgrade (`revoke_rls`) drops every
policy, helper and default privilege, and each `DROP POLICY` takes an
ACCESS-EXCLUSIVE lock on its table. Pinned by
`tests/test_rls_role_absent.py::test_at_head_database_stays_ungranted_until_0058_is_replayed`.

### Helpers (`backend/app/db/rls/functions.py`)

All schema-qualified, all `STABLE`, all paired with a drop signature; an
argument change is a new function and the old signature is retired through
`_OBSOLETE_SIGNATURES` so revocation can never orphan one.

| Helper | Shape | Purpose |
|---|---|---|
| `current_tenant_id()` | plain SQL | `nullif(current_setting('app.tenant_id', true), '')::bigint` — NULL when unstamped, which matches no rows (fail closed) |
| `current_tenant_email()` | `SECURITY DEFINER` | the stamped tenant's own `lower(email)` from `users` — the key the pending-invite policy matches before a partner id exists |
| `partner_visible_tenants()` | `SECURITY DEFINER`, `SETOF bigint` | the owners who seated the caller as their partner on an ACTIVE link; uncorrelated so policies hoist it into one init-plan per statement |
| `sharing_link_identity_matches(id, user_id, email, token_hash)` | `SECURITY DEFINER` | reads the STORED row during an UPDATE's `WITH CHECK` (which sees only the new row) — the anti-forgery pin for sharing links |

`SECURITY DEFINER` (owner = the schema owner) is what lets a policy read
membership without recursing into the guarded table and without granting the
app role direct read access to the data behind the check. Each helper is
keyed to the executing request's own GUC or is a pure comparison, so execute
access leaks nothing beyond the caller's **stamped** tenant — the stamp is
the executing role's own to set, so §Residual risks says plainly what that
does not buy. EXECUTE on the helpers goes to `fintrack_app` alone:
`apply_policies` revokes the default `PUBLIC` EXECUTE on all four, because on
PG16+ every cluster role also holds `CONNECT` and could otherwise call the
definer helpers against a tenant of its own choosing.

### Policies (`backend/app/db/rls/policies.py`)

Every `public` table is in exactly one class; `tests/test_rls_coverage.py`
fails by name on any table that is not.

- **`SECURED_TABLES` (40 tenant tables)** — one `{table}_access` policy:
  `USING (user_id = current_tenant_id()) WITH CHECK (user_id =
  current_tenant_id())`. The identical `WITH CHECK` is what makes the app
  role's writes self-scoped: a request can only ever insert or update rows
  stamped with its own tenant.
- **`sharing_links` / `sharing_audit`** — hand-written consent rules
  (docs/43), because the invite-accept flow is the product's one sanctioned
  cross-tenant write:
  - `sharing_links`: SELECT/DELETE for the owner, the seated partner, and any
    signed-in user whose OWN email matches the invite (the token lookup runs
    before `partner_user_id` exists); INSERT for owners only; UPDATE for the
    owner, for the seated partner staying seated, and for the addressee
    materializing `expired` on their own unaccepted invite — every arm ANDed
    with `sharing_link_identity_matches(...)`, so no update can re-point the
    link's owner, email or token (a seated partner re-pointing `user_id` at a
    victim would otherwise silently manufacture consent). The ORM writes every
    mapped column on update, which is why this helper — not a column-level
    grant — is the pin.
  - `sharing_audit`: `user_id = tenant OR user_id IN (<owners of links the
    caller can see>)` for both `USING` and `WITH CHECK` — receipts are
    attributed to the owner's tenant even when the partner acted (docs/43 D7),
    and the sub-read of `sharing_links` is itself policy-scoped, so a caller
    cannot widen the set.
- **`CHILD_TABLES` (3 join tables)** — `transaction_tags`, `rule_tags`,
  `bill_reminder_match_terms` carry no `user_id` of their own; both clauses are
  an `EXISTS` against the tenant parent, whose own policy makes the EXISTS
  tenant-correct (no recursion: no parent policy reads a child back).
- **`AUTH_TABLES` (7)** — `users`, `refresh_tokens`, `totp_factors`,
  `webauthn_credentials`, `recovery_codes`, `oidc_links`, `mcp_tokens` stay
  outside RLS because their queries run before a request identity exists
  (login by email, token validation by hash, the OIDC link match, the MCP
  docs guard), always keyed by an exact id or hash at the application layer.
  `users` is the auth anchor: every read of it is id- or email-keyed, and it
  holds no financial rows.
- **`GLOBAL_READ_TABLES` (3)** — `benchmark_prices`, `provider_fund_registry`
  (read-only) and `fx_rates` (read + the request-path manual-rate
  `INSERT/UPDATE`, docs/07 manual-wins): shared reference caches, AGENTS §2's
  documented exception to per-tenant rows.

Grants are exactly the declared privileges per class (the coverage test
compares `information_schema.role_table_grants` against the registry
**exactly** — an extra privilege anywhere fails). The globals are the one
class narrower than the standard set, so their grant is `REVOKE ALL` first
and `GRANT` after: a table re-created while the defaults were armed arrives
already holding the standard set, and a plain GRANT never takes privilege
back. `ALTER DEFAULT PRIVILEGES` gives future tables and sequences the
standard set, and a new table is unclassified until its own migration calls
`secure_registered_table` / `grant_auth_table` / `grant_global_read_table` —
the coverage test names it either way. Those defaults are armed by
`apply_policies` and **disarmed by `revoke_rls`** (`drop_default_privileges`):
an arm the downgrade left set would hand every table the migrator later
re-creates — m106's downgrade-to-0038 replay of 0050/0052 included — a
birth-right grant the registry never declared.

0058's rollback is not lock-free and this document does not pretend it is:
`drop_policies` sweeps `DROP POLICY` and `DISABLE ROW LEVEL SECURITY` over
every public table, each statement taking ACCESS EXCLUSIVE, so a range
downgrade contends with any open transaction on any of them (QA t_592a6e1a
F1 caught an idle-in-transaction reader tripping the suite's 500ms
`lock_timeout`). Each statement now retries with a growing pause — enough to
ride out a transient reader — and then **raises**, naming the statement and
the last PostgreSQL error: skipping a busy table would let the migration
report a rollback it did not perform. Two tests pin the behaviour
(`test_rls_migration.py`): a reader that releases inside the retry window
does not fail the rollback, and a wedged reader ends it in an error with the
schema left exactly where it was. A production range downgrade of 0058
should still be run without concurrent readers.

### Per-request tenant context (`backend/app/core/rls.py`)

- `tenant_ctx` — a `ContextVar` holding the authenticated tenant, written only
  from a verified token (`get_current_user`, the SSE variant) or from the
  pre-identity seed helpers — never from request input.
- An engine-level `begin` listener stamps every new transaction on the request
  engine with `SELECT set_config('app.tenant_id', …, is_local=true)`.
  Transaction-local means the value dies with the transaction: a pooled
  connection can never carry one request's tenant into the next request
  (proved by `test_begin_stamp_is_transaction_local_and_dies_with_the_transaction`).
- `set_tenant_now(session, user_id)` — binds AND stamps the live transaction,
  for the two flows that write tenant rows before any identity exists:
  `POST /auth/register` (the user's 10 default categories) and the demo seed
  (`ensure_demo_account`, reachable pre-identity through `POST /auth/demo`).
- `POST /auth/demo/reset` needs no change: it authenticates as the demo user,
  so the request is already stamped with the demo tenant.
- Every request-path session binds to the request engine — including the sync
  write session (`services/sync/service.py::_write_session`), which opens its
  own connection after the provider call and joins the CALLER's bind
  (a savepoint when that bind is a `Connection`, i.e. the test harness). A
  request-path commit (`pull`, `create_agreement`, `revoke`) can therefore
  never fall back to `get_engine()`, the RLS-exempt schema-owner engine.

### Out of scope (deliberately unchanged)

- Background workers, post-response `BackgroundTasks` (price/fund backfill
  fan-out), scripts, operator psql and alembic keep using the maintenance
  engine (`get_engine()`), which is RLS-exempt by role. They are system paths
  that legitimately cross tenants; their queries keep their explicit
  `user_id` filters.
- The SSE price stream (`_price_stream`) keeps its own maintenance-engine
  session: it outlives the request by design and reads the price cache by
  explicit ids.
- No public API shape change, no MCP surface change, no Android change.
- `docs/02`'s table list is untouched; the class of each table lives in the
  registry and this doc.

## Touch points

- `backend/app/db/rls/` — `functions.py`, `policies.py`, `__init__.py`
  (`apply_rls` / `revoke_rls`)
- `backend/alembic/versions/0058_row_level_security.py` — `upgrade` =
  `apply_rls`, `downgrade` = `revoke_rls`
- `backend/app/core/rls.py` — ContextVar, stamping, startup verification
- `backend/app/api/deps.py` — `get_request_engine()`, `get_db()`, tenant bind
  in `get_current_user` / `get_current_user_with_query_token`
- `backend/app/core/config.py` — `app_database_url`, `app_role_password`
- `backend/app/main.py` — startup verification / not-armed log line
- `backend/app/api/auth.py` (`register`), `backend/app/services/demo_seed.py`
  (`ensure_demo_account`) — `set_tenant_now` on the pre-identity seed paths
- `backend/scripts/provision_rls_role.py` — idempotent role provisioning
- `backend/tests/test_rls_coverage.py`, `test_rls_isolation.py`,
  `test_rls_migration.py`, `test_rls_role_absent.py`, `rls_support.py`
- `.env.example` — `APP_DATABASE_URL` / `APP_ROLE_PASSWORD` documented

## Verification

Run locally (Actions billing is not funded — every gate runs on the box):

```bash
cd backend
uv run ruff format --check && uv run ruff check
uv run pytest tests/test_rls_coverage.py tests/test_rls_isolation.py tests/test_rls_migration.py tests/test_rls_role_absent.py -q
uv run pytest -q   # full suite: no behavioural change without APP_DATABASE_URL
```

What the RLS tests prove:

1. **The second lock fires** (`test_rls_isolation.py`): as the app role, a
   query with NO tenant filter returns only the stamped tenant's rows — never
   another tenant's; unstamped reads return zero rows; cross-tenant
   `INSERT`/`UPDATE`/`DELETE` change nothing (`WITH CHECK` / `USING`).
2. **Sharing stays correct**: a seated partner reads the owner's rows and can
   write none of them; a seated partner cannot re-point a link at a third
   party (identity helper); the addressee sees only their own pending invite;
   partner receipts land in the owner's audit ledger; an unrelated tenant can
   write none of it.
3. **Wiring end-to-end**: `POST /auth/register` through an app-role session
   seeds its 10 default categories (the handler stamps its new tenant), and
   the same request with the stamp removed is refused by the database
   (500 `internal_error`, nothing leaked).
4. **Pooled-connection hygiene**: the begin stamp is transaction-local; a
   second session on the same pooled connection sees no residue.
5. **Registry truth** (`test_rls_coverage.py`): every public table is
   classified; secured/child tables have RLS enabled with a live policy;
   auth/global tables have none; helpers exist with the right signatures and
   `SECURITY DEFINER` shape; the app role holds exactly the declared
   privileges (no superuser, no `BYPASSRLS`, not the owner).
6. **Migration** (`test_rls_migration.py`): a from-scratch `upgrade head`
   replays every migration including 0058; `downgrade 0057_price_fetched_at`
   removes every
   policy, disables RLS, drops the helpers **and disarms the app role's
   `ALTER DEFAULT PRIVILEGES`** (an arm left standing would grant CRUD to
   every table a later replay re-creates); `upgrade head` restores all four.
7. **The sync write path never reaches the owner engine**
   (`test_rls_isolation.py`): with `APP_DATABASE_URL` armed, a
   `POST /sync/agreements/{id}/pull` request path opens its write session on
   the REQUEST engine and connects as `fintrack_app` — the old
   `get_engine()` fallback would have answered `fintrack` there.
8. **The helpers are not executable by `PUBLIC`**
   (`test_rls_coverage.py`): a fresh no-privilege role inherits exactly
   `PUBLIC`'s privileges, and none of the four helpers grants `EXECUTE` to
   `PUBLIC` any more (the app role keeps its own grant).
9. **A database with no app role still replays** (`test_rls_role_absent.py`):
   on a private throwaway cluster where `fintrack_app` was never created,
   `upgrade head` (0057 → 0058 → head) exits 0, lands every policy and helper,
   arms no default privileges, and `upgrade → downgrade → upgrade`
   round-trips; `apply_policies` puts **no statement** on the wire in the
   missing-role branch (every statement is already guarded on the role's
   existence) and reports the condition on the logger instead.
10. **Provisioning an at-head database arms nothing until 0058 is replayed**
   (`test_rls_role_absent.py`): creating the role on that role-less-at-head
   cluster and running `alembic upgrade head` (a no-op at head) leaves the
   request role with **no** `SELECT` on `public.transactions`, so
   `verify_request_role` refuses to boot (`cannot read public.transactions`) —
   the direct test for the guard's ungranted branch. The documented replay
   (`downgrade 0057_price_fetched_at` → `upgrade head`, as the schema owner)
   then re-applies
   every per-database grant and the same guard passes.

The pre-existing suite is unaffected because it overrides `get_db` and never
sets `APP_DATABASE_URL` — the enforcement claims above come from the dedicated
modules, which connect as the app role explicitly.

## Rollout (deploy host)

1. `ssh example.internal 'docker exec -i fin_tracker-db-1 psql -U fintrack -d fintrack'`
   (or any superuser psql) — only needed if the deployment's `fintrack` lacks
   CREATEROLE; otherwise step 2 suffices:
   `CREATE ROLE fintrack_app LOGIN PASSWORD '<random>'`. Whoever CREATEs the
   role holds the ADMIN option on it; a later `GRANT fintrack_app TO <role>`
   WITHOUT `WITH ADMIN OPTION` does not, and without it the provision script
   cannot rotate the password (PostgreSQL 16+ requires CREATEROLE **and** the
   admin option to ALTER a role — the script warns and continues in that
   case, and a wrong password then fails at boot, loudly).
2. `cd backend && uv run python scripts/provision_rls_role.py` (idempotent;
   sets/rotates the login password from `APP_ROLE_PASSWORD` or the
   `DATABASE_URL` password). All three paths are exercised by hand: create
   when missing, refuse with the psql guidance when the caller lacks
   CREATEROLE, warn-and-continue when the password cannot be rotated.
   **Production MUST set `APP_ROLE_PASSWORD` explicitly** — a fresh random
   value (`python3 -c 'import secrets; print(secrets.token_hex(32))'`),
   stored only in the deploy host's repo-root `.env`, `chmod 600` (uid 1000,
   gitignored). The script's silent fallback to the `DATABASE_URL` password
   (`scripts/provision_rls_role.py:30`) would reuse the maintenance role's
   password for the request role, which is the reuse docs/55's role
   separation exists to prevent.
3. Deploy the branch (api rebuild runs `alembic upgrade head`, which applies
   the policies and per-database grants).
   **If the database already reached head without the role** (i.e. `0058`
   ran on a cluster that had no `fintrack_app` — `upgrade head` is then a
   no-op and never re-applies the grants, so step 2 alone does not arm it),
   replay `0058` as the schema owner: `alembic downgrade
   0057_price_fetched_at` then
   `alembic upgrade head`, with **no concurrent readers attached** (its
   downgrade takes ACCESS-EXCLUSIVE locks). Step 4's refusal names this
   exact state when it fires.
4. Add to the deployed `.env`:
   `APP_DATABASE_URL=postgresql+psycopg://fintrack_app:<password>@localhost:5432/fintrack`
   (+ `APP_ROLE_PASSWORD` if rotating independently), then `docker compose up
   -d api`. Boot logs must show `Request role 'fintrack_app' verified:
   constrained by row-level security`; a refusal names the missing step.
5. Read-only verification: log in, load the dashboard, run one report (tenant
   path); `docker logs fin_tracker-api-1` free of `permission denied` /
   `row-level security` lines.

Rollback: unset `APP_DATABASE_URL` and restart the api — the request path
returns to the maintenance role and the policies become inert (they never
constrain the owner), with no schema change required. A full schema rollback
is `alembic downgrade 0057_price_fetched_at`.

## Residual risks (stated, not hidden)

- **RLS here is not a defense against an attacker who can already execute
  SQL as `fintrack_app`.** The tenant stamp is an ordinary session GUC the
  app role may set for itself — `SET app.tenant_id = '<victim>'` — so an
  attacker with SQL injection or a compromised app credential passes every
  `WITH CHECK`, reads and writes another tenant's rows, and gets all three
  SECURITY DEFINER helpers to answer for the forged tenant
  (`current_tenant_email()`, `partner_visible_tenants()`,
  `sharing_link_identity_matches()`). The `AUTH_TABLES` sit outside RLS by
  design (their queries run pre-identity), so `SELECT * FROM users` returns
  every password hash; revoking `PUBLIC`'s EXECUTE on the helpers keeps
  *other cluster roles* out, not `fintrack_app` itself. This blast radius is
  inherent to the trusted-GUC pattern and is accepted for the stated threat
  model: the second lock exists to catch a forgotten `WHERE` clause in
  ordinary application code, which it does stop. Anything that can execute
  SQL as the app role is outside that model and must be treated as a full
  compromise of every tenant — the mitigations for that class are the
  application-layer tenant filters, parameterised statements and credential
  hygiene, not RLS.
- While `APP_DATABASE_URL` is unset, RLS is inert by design (the pre-RLS
  behaviour). The startup warning names this state; making the key mandatory
  is a deliberate follow-up once the rollout has settled.
- System paths (workers, scripts, operator psql) run as the owner and are not
  constrained — they keep their explicit tenant filters at the application
  layer. A forgotten filter in a SYSTEM path is covered by neither lock.
- `users` is outside RLS: every read of it is id- or email-keyed today, and a
  hypothetical forgotten filter there would read profile rows (name, flags) —
  not financial data. Account deletion (`DELETE /users/me`) relies on the
  application's ownership check plus FK cascades.
- Policy evaluation adds a per-row predicate (cheap: one GUC read; the partner
  set is one uncorrelated init-plan per statement). The suite's latency pins
  run without RLS (owner role) and therefore do not measure it; re-measure
  the 10k-row first-paint budget on a staged deployment before widening the
  rollout to a larger tenant.
