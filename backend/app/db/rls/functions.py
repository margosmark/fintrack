"""Row-level security helper functions

Each helper that reads tables is SECURITY DEFINER so it runs as the function
owner (the schema owner, which RLS does not constrain) — that lets a policy
read real membership without recursing into the very table the policy guards,
and without the app role needing direct read access to the guarded table's
siblings. Every helper is paired with the signature needed to drop it, so
adding one is a single edit and revocation can never leave a function behind
(the pattern from Lumina-Finance/lumina-finance `backend/app/db/rls/`).

A helper whose argument list changes is a new function rather than a
replacement, so the signature it used to have is retired through the
obsolete list below and dropped by both the apply and the revoke path.

The helpers read the per-request GUC `app.tenant_id` that
`app.core.rls` stamps transaction-locally on every request connection; an
unstamped connection reads NULL, and NULL matches no rows — connections
outside a request (background workers, scripts, psql) run as the schema
owner and never evaluate the policies at all.

The stamp is a session GUC the executing role can also set for itself
(`SET app.tenant_id = '<victim>'` as `fintrack_app`), so every helper
answers for the **stamped** tenant, never for a verified one: that
forgery is inherent to the trusted-GUC pattern and is written down as a
residual risk (docs/55 §Residual risks) rather than claimed away.
"""

from typing import NamedTuple

from sqlalchemy import Connection, text

# Schema-qualified helper names, the single source reused by the policies and
# the application call sites so a rename is one edit rather than a search
# across files.
CURRENT_TENANT_ID = "public.current_tenant_id"
CURRENT_TENANT_EMAIL = "public.current_tenant_email"
PARTNER_VISIBLE_TENANTS = "public.partner_visible_tenants"
SHARING_LINK_IDENTITY_MATCHES = "public.sharing_link_identity_matches"

# Signatures the app no longer creates, dropped wherever the helpers are
# applied or revoked. Empty today; a retired helper lands here so an
# already-provisioned database cannot keep serving it.
_OBSOLETE_SIGNATURES: tuple[str, ...] = ()


class _Helper(NamedTuple):
    """A helper's creation statement paired with the signature used to drop it"""

    create_sql: str

    # Schema-qualified name and argument types, e.g. public.current_tenant_email()
    drop_signature: str


_HELPERS: tuple[_Helper, ...] = (
    # The per-request tenant identity the app stamps, or NULL when unset so an
    # unstamped connection simply matches no rows. Plain SQL, not SECURITY
    # DEFINER: it reads only the GUC, and a caller able to read its own GUC
    # learns nothing it did not already hold.
    _Helper(
        f"""
    CREATE OR REPLACE FUNCTION {CURRENT_TENANT_ID}() RETURNS bigint
    LANGUAGE sql STABLE SET search_path = '' AS $$
        SELECT nullif(current_setting('app.tenant_id', true), '')::bigint
    $$
    """,
        f"{CURRENT_TENANT_ID}()",
    ),
    # The stamped tenant's own email, lowercase — the key the sharing invite
    # policies match a PENDING invite against before a partner id exists (the
    # accept flow resolves the invite by token, and the row's partner_user_id
    # is still NULL at that point). SECURITY DEFINER so a policy can read
    # `users` without the `users` table itself needing a policy, and so the
    # app role needs no grant on `users` for policy evaluation. Exposes only
    # the STAMPED tenant's own email: the id comes from `app.tenant_id`,
    # which the executing role sets for itself — so this answers about the
    # stamp, not about a verified caller (docs/55 §Residual risks).
    _Helper(
        f"""
    CREATE OR REPLACE FUNCTION {CURRENT_TENANT_EMAIL}() RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
        SELECT lower(u.email) FROM public.users u
        WHERE u.id = {CURRENT_TENANT_ID}()
    $$
    """,  # noqa: S608 -- identifiers come from this module's fixed registry, not from input
        f"{CURRENT_TENANT_EMAIL}()",
    ),
    # Tenants whose data the stamped tenant may READ through an active
    # household-sharing seat (docs/43): the owners who made the caller their
    # partner. Set-returning and uncorrelated so a policy can hoist it into
    # one init-plan per statement instead of a per-row subquery. SECURITY
    # DEFINER so the read is not itself filtered by the sharing policies, and
    # so a policy on `sharing_links` can never recurse. Keyed to the
    # executing request's own GUC: the app role can execute it, but it only
    # ever answers about the *stamped* seat — and the stamp is the
    # executing role's own to set (docs/55 §Residual risks).
    _Helper(
        f"""
    CREATE OR REPLACE FUNCTION {PARTNER_VISIBLE_TENANTS}() RETURNS SETOF bigint
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
        SELECT sl.user_id FROM public.sharing_links sl
        WHERE sl.partner_user_id = {CURRENT_TENANT_ID}() AND sl.status = 'active'
    $$
    """,  # noqa: S608 -- identifiers come from this module's fixed registry, not from input
        f"{PARTNER_VISIBLE_TENANTS}()",
    ),
    # The immutable identity of a sharing invite, read back during an UPDATE's
    # WITH CHECK: the stored row still holds the old values at that point, so
    # "the new row keeps the stored owner, email and token hash" is exactly
    # this function's answer. SECURITY DEFINER so the check is not filtered by
    # (and does not recurse into) the sharing policies themselves. The ORM
    # writes every mapped column on update, so this — not a column-level
    # grant — is what stops a seated partner from re-pointing a link at
    # another tenant and manufacturing consent.
    _Helper(
        f"""
    CREATE OR REPLACE FUNCTION {SHARING_LINK_IDENTITY_MATCHES}(
        p_link_id bigint, p_user_id bigint, p_email text, p_token_hash text
    ) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
        SELECT EXISTS (
            SELECT 1 FROM public.sharing_links sl
            WHERE sl.id = p_link_id AND sl.user_id = p_user_id
              AND sl.email = p_email AND sl.token_hash = p_token_hash
        )
    $$
    """,  # noqa: S608 -- identifiers come from this module's fixed registry, not from input
        f"{SHARING_LINK_IDENTITY_MATCHES}(bigint, bigint, text, text)",
    ),
)

# The same helpers as full `GRANT` / `REVOKE ... ON FUNCTION` signatures:
# schema-qualified name plus argument types (e.g.
# `public.sharing_link_identity_matches(bigint, bigint, text, text)`). Derived
# from `_HELPERS`, so a new helper cannot be created while the PUBLIC revoke
# that `apply_policies` applies is forgotten.
HELPER_FUNCTION_SIGNATURES: tuple[str, ...] = tuple(helper.drop_signature for helper in _HELPERS)


def create_helper_functions(connection: Connection) -> None:
    """Create every SECURITY DEFINER helper the policies and the app rely on"""
    _drop_obsolete_helper_functions(connection)
    for helper in _HELPERS:
        connection.execute(text(helper.create_sql))


def drop_helper_functions(connection: Connection) -> None:
    """Drop every helper function when row-level security is removed"""
    _drop_obsolete_helper_functions(connection)
    for helper in _HELPERS:
        connection.execute(text(f"DROP FUNCTION IF EXISTS {helper.drop_signature}"))


def _drop_obsolete_helper_functions(connection: Connection) -> None:
    """Drop retired helper signatures the app no longer creates"""
    for signature in _OBSOLETE_SIGNATURES:
        connection.execute(text(f"DROP FUNCTION IF EXISTS {signature}"))
