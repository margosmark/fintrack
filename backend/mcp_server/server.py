"""MCP server for fin_tracker — exposes all API endpoints as typed tools.

Each tool maps 1:1 to a backend REST endpoint. Parameters mirror the
Pydantic schemas exactly. The server adds no business logic; it proxies
every call to the running FastAPI backend at FIN_TRACKER_URL.

M80 (docs/40) adds ONE thing on top of that proxy: a context-tier middleware
that gives the seam a policy (read-only tokens and the demo tenant see only
the read tools) and a receipt (every `tools/call` lands in `mcp_audit`).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent

from mcp_server import policy
from mcp_server.audit import record as record_audit
from mcp_server.audit import response_code as audit_response_code
from mcp_server.auth import (
    McpAuthMiddleware,
    current_is_demo,
    current_mcp_policy,
    current_mcp_token_id,
    current_user_id,
    in_mcp_request,
)
from mcp_server.client import client
from mcp_server.uploads import CSV_EXTENSIONS, STATEMENT_EXTENSIONS, open_upload

mcp = MCPServer("fin_tracker")


class AgentSurfaceMiddleware:
    """Tool policy + write receipts at the MCP seam (M80, docs/40 §2-3).

    Registered on `mcp.middleware`, the SDK's context tier: `(ctx, call_next)`
    sees every inbound request with `ctx.method` and the RAW `ctx.params`, and
    its return value becomes the response. That is the only place both halves
    of the problem are visible at once — `tools/list` knows what to hide but
    not what happened, `tools/call` knows what happened but not which tool the
    client believed in — so one middleware owns both.

    Session facts come from `mcp_server.auth`'s contextvars, set by the ASGI
    middleware from `POST /auth/validate-mcp-token`. Their defaults are the
    unrestricted ones, so stdio (no ASGI layer) keeps today's toolset exactly.
    """

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        if ctx.method == "tools/list":
            return self._filtered_list(await call_next(ctx))
        if ctx.method == "tools/call":
            return await self._tool_call(ctx, call_next)
        return await call_next(ctx)

    # -- tools/list ----------------------------------------------------------

    def _filtered_list(self, result: Any) -> Any:
        """Drop the write tools a restricted session may not have; annotate the rest."""
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            return result  # unexpected shape — never guess at someone else's payload
        marker = policy.restriction_marker(current_mcp_policy.get(), current_is_demo.get())
        tools: list[Any] = []
        for tool in result["tools"]:
            name = tool.get("name") if isinstance(tool, dict) else None
            if not isinstance(name, str):
                tools.append(tool)
                continue
            if (
                marker
                and policy.is_write_tool(name)
                and not (
                    name in policy.PROPOSE_TOOLS
                    and current_mcp_policy.get() == "read_propose"
                    # Belt and braces: demo is query-only regardless of what
                    # a policy value says (docs/59 §1) — the mint guard makes
                    # demo+read_propose unreachable, the seam must not lean
                    # on that guard for its toolset.
                    and not current_is_demo.get()
                )
            ):
                # REMOVED, not annotated — the client must not try it.
                # Exception (docs/59 §2): a `read_propose` connector keeps
                # `proposals_create` — an inert pending card, never a domain
                # write; the refusal envelope exempts it too.
                continue
            if isinstance(tool, dict):
                tool = {**tool, "annotations": policy.tool_annotations(name, marker)}
            tools.append(tool)
        return {**result, "tools": tools}

    # -- tools/call ----------------------------------------------------------

    async def _tool_call(self, ctx: Any, call_next: Any) -> Any:
        params = ctx.params if isinstance(ctx.params, dict) else {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            return await call_next(ctx)  # malformed; the SDK answers it
        started = time.perf_counter()

        if name in policy.AUTH_TOOLS and in_mcp_request.get():
            # M84 (docs/40 §2): session lifecycle has no meaning when the
            # identity is fixed by the MCP token — and acting on the shared
            # httpx jar is how one tenant received another's access token
            # (F-1). Refused before the proxy runs, on the HTTP path only;
            # stdio keeps these tools for local dev (docs/23 mode matrix).
            self._record(name, arguments, 403, started)
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(policy.AUTH_TOOL_ENVELOPE))],
                is_error=True,
            )

        if policy.is_write_tool(name):
            envelope = policy.refusal_envelope(
                name, current_mcp_policy.get(), current_is_demo.get()
            )
            if envelope is not None:
                # Refused BEFORE the proxy runs: no backend call, no JWT
                # issued, nothing on the wire (docs/40 §2).
                self._record(name, arguments, 403, started)
                return CallToolResult(
                    content=[TextContent(type="text", text=json.dumps(envelope))],
                    is_error=True,
                )

        try:
            result = await call_next(ctx)
        except Exception:
            self._record(name, arguments, 500, started)
            raise
        self._record(name, arguments, audit_response_code(result), started)
        return result

    def _record(self, name: str, arguments: Any, status: int, started: float) -> None:
        """Best-effort receipt (docs/40 §3) — never fails the call itself."""
        user_id = current_user_id.get(None)
        if user_id is None:
            return  # stdio / modes 1-2: no per-request identity to attribute
        record_audit(
            user_id=user_id,
            mcp_token_id=current_mcp_token_id.get(None),
            tool=name,
            arguments=arguments if isinstance(arguments, dict) else None,
            status=status,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


mcp.middleware.append(AgentSurfaceMiddleware())


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@mcp.tool()
def health_check() -> dict:
    """Check API health (no auth required)."""
    return client.request("GET", "/api/v1/health")  # type: ignore[return-value]


@mcp.tool()
def auth_register(email: str, password: str) -> dict:
    """Register a new account. Returns tokens.

    stdio only — refused on the MCP HTTP path (docs/40 §2).
    """
    return client.request(  # type: ignore[return-value]
        "POST",
        "/api/v1/auth/register",
        json={"email": email, "password": password},
    )


@mcp.tool()
def auth_login(email: str, password: str) -> dict:
    """Login and get tokens.

    stdio only — refused on the MCP HTTP path (docs/40 §2).
    """
    result = client.request(  # type: ignore[return-value]
        "POST",
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    if isinstance(result, dict) and "access_token" in result:
        # The refresh cookie was absorbed off the login response into this
        # session's own scoped slot by `client.request` (M84) — there is no
        # shared jar to read it back from.
        client.set_token(result["access_token"])
    return result


@mcp.tool()
def auth_demo() -> dict:
    """One-click demo account login.

    stdio only — refused on the MCP HTTP path (docs/40 §2).
    """
    result = client.request("POST", "/api/v1/auth/demo", json={})  # type: ignore[return-value]
    if isinstance(result, dict) and "access_token" in result:
        client.set_token(result["access_token"])
    return result


@mcp.tool()
def auth_refresh(refresh_token: str | None = None) -> dict:
    """Refresh access token (rotates the refresh token).

    `refresh_token` is optional and, when omitted, the session's OWN scoped
    token is used. The API is cookie-only, so the token is delivered as a
    cookie either way — it is never placed in the request body, where it
    would land in request logs. The shared httpx jar is never consulted
    (M84): preferring it over the caller's token was the cross-tenant leak.

    stdio only — refused on the MCP HTTP path (docs/40 §2).
    """
    result = client.request(  # type: ignore[return-value]
        "POST",
        "/api/v1/auth/refresh",
        headers=client._refresh_cookie(refresh_token),
    )
    if isinstance(result, dict) and "access_token" in result:
        client.set_token(result["access_token"])
    return result


@mcp.tool()
def auth_logout(refresh_token: str | None = None) -> dict:
    """Logout (revoke refresh token).

    The token travels as a cookie, not a body field — the API reads it only
    from the httpOnly cookie (see `auth_refresh`).

    stdio only — refused on the MCP HTTP path (docs/40 §2).
    """
    return client.request(  # type: ignore[return-value]
        "POST",
        "/api/v1/auth/logout",
        headers=client._refresh_cookie(refresh_token),
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@mcp.tool()
def users_me() -> dict:
    """Get current user profile."""
    return client.request("GET", "/api/v1/users/me")  # type: ignore[return-value]


@mcp.tool()
def users_update_me(
    privacy_mode: bool | None = None,
    currency_tracker_enabled: bool | None = None,
    llm_provider: str | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_api_key: str | None = None,
) -> dict:
    """Update current user settings (partial)."""
    body: dict[str, Any] = {}
    if privacy_mode is not None:
        body["privacy_mode"] = privacy_mode
    if currency_tracker_enabled is not None:
        body["currency_tracker_enabled"] = currency_tracker_enabled
    if llm_provider is not None:
        body["llm_provider"] = llm_provider
    if llm_base_url is not None:
        body["llm_base_url"] = llm_base_url
    if llm_model is not None:
        body["llm_model"] = llm_model
    if llm_api_key is not None:
        body["llm_api_key"] = llm_api_key
    return client.request("PATCH", "/api/v1/users/me", json=body)  # type: ignore[return-value]


@mcp.tool()
def users_change_password(current_password: str, new_password: str) -> dict:
    """Change password. Returns fresh token pair."""
    result = client.request(  # type: ignore[return-value]
        "POST",
        "/api/v1/users/me/change-password",
        json={
            "current_password": current_password,
            "new_password": new_password,
        },
    )
    if isinstance(result, dict) and "access_token" in result:
        # The rotated pair's refresh cookie was absorbed off this response by
        # `client.request` into the caller's own scoped slot (M84).
        client.set_token(result["access_token"])
    return result


@mcp.tool()
def users_update_profile(
    name: str | None = None,
    email: str | None = None,
    current_password: str | None = None,
) -> dict:
    """Update profile. current_password required for email change."""
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if email is not None:
        body["email"] = email
    if current_password is not None:
        body["current_password"] = current_password
    return client.request("PATCH", "/api/v1/users/me/profile", json=body)  # type: ignore[return-value]


@mcp.tool()
def users_delete_account(current_password: str) -> dict:
    """Delete current account and all data. Destructive!"""
    return client.request(  # type: ignore[return-value]
        "DELETE",
        "/api/v1/users/me",
        json={"current_password": current_password},
    )


@mcp.tool()
def users_test_llm(
    llm_provider: str | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_api_key: str | None = None,
) -> dict:
    """Test LLM connectivity with a probe request."""
    body: dict[str, Any] = {}
    if llm_provider is not None:
        body["llm_provider"] = llm_provider
    if llm_base_url is not None:
        body["llm_base_url"] = llm_base_url
    if llm_model is not None:
        body["llm_model"] = llm_model
    if llm_api_key is not None:
        body["llm_api_key"] = llm_api_key
    return client.request("POST", "/api/v1/users/me/llm-test", json=body)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


@mcp.tool()
def categories_list(limit: int = 50, offset: int = 0) -> dict:
    """List categories, paginated."""
    return client.request("GET", "/api/v1/categories", params={"limit": limit, "offset": offset})  # type: ignore[return-value]


@mcp.tool()
def categories_create(name: str, parent_id: int | None = None) -> dict:
    """Create a category."""
    body: dict[str, Any] = {"name": name}
    if parent_id is not None:
        body["parent_id"] = parent_id
    return client.request("POST", "/api/v1/categories", json=body)  # type: ignore[return-value]


@mcp.tool()
def categories_get(category_id: int) -> dict:
    """Get a category by ID."""
    return client.request("GET", f"/api/v1/categories/{category_id}")  # type: ignore[return-value]


@mcp.tool()
def categories_update(
    category_id: int,
    name: str | None = None,
    parent_id: int | None = None,
) -> dict:
    """Update a category (partial)."""
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if parent_id is not None:
        body["parent_id"] = parent_id
    return client.request("PATCH", f"/api/v1/categories/{category_id}", json=body)  # type: ignore[return-value]


@mcp.tool()
def categories_delete(category_id: int) -> dict:
    """Delete a category."""
    return client.request("DELETE", f"/api/v1/categories/{category_id}")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Institutions
# ---------------------------------------------------------------------------


@mcp.tool()
def institutions_list(limit: int = 20, offset: int = 0) -> dict:
    """List institutions, paginated."""
    return client.request("GET", "/api/v1/institutions", params={"limit": limit, "offset": offset})  # type: ignore[return-value]


@mcp.tool()
def institutions_create(
    name: str,
    importer_profile: dict | None = None,
) -> dict:
    """Create an institution."""
    body: dict[str, Any] = {"name": name}
    if importer_profile is not None:
        body["importer_profile"] = importer_profile
    return client.request("POST", "/api/v1/institutions", json=body)  # type: ignore[return-value]


@mcp.tool()
def institutions_get(institution_id: int) -> dict:
    """Get an institution by ID."""
    return client.request("GET", f"/api/v1/institutions/{institution_id}")  # type: ignore[return-value]


@mcp.tool()
def institutions_update(
    institution_id: int,
    name: str | None = None,
    importer_profile: dict | None = None,
) -> dict:
    """Update an institution (partial)."""
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if importer_profile is not None:
        body["importer_profile"] = importer_profile
    return client.request("PATCH", f"/api/v1/institutions/{institution_id}", json=body)  # type: ignore[return-value]


@mcp.tool()
def institutions_delete(institution_id: int) -> dict:
    """Delete an institution (fails if accounts linked)."""
    return client.request("DELETE", f"/api/v1/institutions/{institution_id}")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


@mcp.tool()
def accounts_list(limit: int = 20, offset: int = 0) -> dict:
    """List accounts with balances, paginated."""
    return client.request("GET", "/api/v1/accounts", params={"limit": limit, "offset": offset})  # type: ignore[return-value]


@mcp.tool()
def accounts_create(
    name: str,
    type: str,
    currency: str,
    institution_id: int | None = None,
    starting_balance: str | None = None,
) -> dict:
    """Create an account.

    type: chequing|savings|credit_card|line_of_credit
    |brokerage_non_registered|rrsp|tfsa|fhsa|resp|lira|rdsp
    currency: 3-char ISO (e.g. CAD, USD)
    """
    body: dict[str, Any] = {
        "name": name,
        "type": type,
        "currency": currency.upper(),
    }
    if institution_id is not None:
        body["institution_id"] = institution_id
    if starting_balance is not None:
        body["starting_balance"] = starting_balance
    return client.request("POST", "/api/v1/accounts", json=body)  # type: ignore[return-value]


@mcp.tool()
def accounts_get(account_id: int) -> dict:
    """Get an account by ID."""
    return client.request("GET", f"/api/v1/accounts/{account_id}")  # type: ignore[return-value]


@mcp.tool()
def accounts_update(
    account_id: int,
    name: str | None = None,
    type: str | None = None,
    currency: str | None = None,
    institution_id: int | None = None,
    llm_autopilot: bool | None = None,
    starting_balance: str | None = None,
) -> dict:
    """Update an account (partial)."""
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if type is not None:
        body["type"] = type
    if currency is not None:
        body["currency"] = currency.upper()
    if institution_id is not None:
        body["institution_id"] = institution_id
    if llm_autopilot is not None:
        body["llm_autopilot"] = llm_autopilot
    if starting_balance is not None:
        body["starting_balance"] = starting_balance
    return client.request("PATCH", f"/api/v1/accounts/{account_id}", json=body)  # type: ignore[return-value]


@mcp.tool()
def accounts_delete(account_id: int) -> dict:
    """Delete an account (fails if transactions exist)."""
    return client.request("DELETE", f"/api/v1/accounts/{account_id}")  # type: ignore[return-value]


@mcp.tool()
def accounts_ledger(account_id: int) -> dict:
    """Get account ledger (running balance view)."""
    return client.request("GET", "/api/v1/accounts/ledger", params={"account_id": account_id})  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


@mcp.tool()
def transactions_list(
    limit: int = 50,
    offset: int = 0,
    account_id: int | None = None,
    category_id: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    payee: str | None = None,
    amount_min: str | None = None,
    amount_max: str | None = None,
    direction: str | None = None,
    q: str | None = None,
) -> dict:
    """List transactions with filters, paginated.

    category_id: integer or "null" for uncategorized.
    from_date/to_date: YYYY-MM-DD strings.
    direction: "paid" or "received".
    """
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if account_id is not None:
        params["account_id"] = account_id
    if category_id is not None:
        params["category_id"] = category_id
    if from_date is not None:
        params["from"] = from_date
    if to_date is not None:
        params["to"] = to_date
    if payee is not None:
        params["payee"] = payee
    if amount_min is not None:
        params["amount_min"] = amount_min
    if amount_max is not None:
        params["amount_max"] = amount_max
    if direction is not None:
        params["direction"] = direction
    if q is not None:
        params["q"] = q
    return client.request("GET", "/api/v1/transactions", params=params)  # type: ignore[return-value]


@mcp.tool()
def transactions_create(
    account_id: int,
    date: str,
    raw_description: str,
    amount_native: str,
    posting_date: str | None = None,
    normalized_payee: str | None = None,
    currency: str | None = None,
    category_id: int | None = None,
) -> dict:
    """Create a transaction."""
    body: dict[str, Any] = {
        "account_id": account_id,
        "date": date,
        "raw_description": raw_description,
        "amount_native": amount_native,
    }
    if posting_date is not None:
        body["posting_date"] = posting_date
    if normalized_payee is not None:
        body["normalized_payee"] = normalized_payee
    if currency is not None:
        body["currency"] = currency.upper()
    if category_id is not None:
        body["category_id"] = category_id
    return client.request("POST", "/api/v1/transactions", json=body)  # type: ignore[return-value]


@mcp.tool()
def transactions_update(
    transaction_id: int,
    account_id: int | None = None,
    date: str | None = None,
    posting_date: str | None = None,
    raw_description: str | None = None,
    normalized_payee: str | None = None,
    amount_native: str | None = None,
    currency: str | None = None,
    category_id: int | None = None,
) -> dict:
    """Update a transaction (partial)."""
    body: dict[str, Any] = {}
    if account_id is not None:
        body["account_id"] = account_id
    if date is not None:
        body["date"] = date
    if posting_date is not None:
        body["posting_date"] = posting_date
    if raw_description is not None:
        body["raw_description"] = raw_description
    if normalized_payee is not None:
        body["normalized_payee"] = normalized_payee
    if amount_native is not None:
        body["amount_native"] = amount_native
    if currency is not None:
        body["currency"] = currency.upper()
    if category_id is not None:
        body["category_id"] = category_id
    return client.request("PATCH", f"/api/v1/transactions/{transaction_id}", json=body)  # type: ignore[return-value]


@mcp.tool()
def transactions_delete(transaction_id: int) -> dict:
    """Delete a transaction."""
    return client.request("DELETE", f"/api/v1/transactions/{transaction_id}")  # type: ignore[return-value]


@mcp.tool()
def transactions_similar(q: str) -> dict:
    """Fuzzy payee search for similar past transactions."""
    return client.request("GET", "/api/v1/transactions/similar", params={"q": q})  # type: ignore[return-value]


@mcp.tool()
def transactions_transfer_suggestions() -> dict:
    """Get potential transfer pairs between accounts."""
    return client.request("GET", "/api/v1/transactions/transfer-suggestions")  # type: ignore[return-value]


@mcp.tool()
def transactions_accept_transfer(source_id: int, candidate_id: int) -> dict:
    """Accept a transfer suggestion, linking two transactions."""
    return client.request(  # type: ignore[return-value]
        "POST",
        "/api/v1/transactions/accept-transfer-suggestion",
        json={"source_id": source_id, "candidate_id": candidate_id},
    )


@mcp.tool()
def transactions_detect_transfer(
    transaction_id: int,
) -> dict:
    """Auto-detect a transfer pair for a transaction."""
    return client.request("POST", f"/api/v1/transactions/{transaction_id}/detect-transfer")  # type: ignore[return-value]


@mcp.tool()
def transactions_link_transfer(transaction_id: int, pair_id: int) -> dict:
    """Manually link a transfer pair."""
    return client.request(  # type: ignore[return-value]
        "POST",
        f"/api/v1/transactions/{transaction_id}/link-transfer",
        json={"pair_id": pair_id},
    )


@mcp.tool()
def transactions_unlink_transfer(
    transaction_id: int,
) -> dict:
    """Unlink a transfer pair."""
    return client.request("POST", f"/api/v1/transactions/{transaction_id}/unlink-transfer")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Securities
# ---------------------------------------------------------------------------


@mcp.tool()
def securities_list(limit: int = 50, offset: int = 0) -> dict:
    """List securities, paginated."""
    return client.request("GET", "/api/v1/securities", params={"limit": limit, "offset": offset})  # type: ignore[return-value]


@mcp.tool()
def securities_create(
    symbol: str,
    name: str,
    listing_currency: str,
    kind: str = "stock",
    exchange: str = "",
) -> dict:
    """Create a security. Queues historical price backfill."""
    return client.request(  # type: ignore[return-value]
        "POST",
        "/api/v1/securities",
        json={
            "symbol": symbol,
            "name": name,
            "listing_currency": listing_currency.upper(),
            "kind": kind,
            "exchange": exchange,
        },
    )


@mcp.tool()
def securities_get(security_id: int) -> dict:
    """Get a security by ID."""
    return client.request("GET", f"/api/v1/securities/{security_id}")  # type: ignore[return-value]


@mcp.tool()
def securities_update(
    security_id: int,
    symbol: str | None = None,
    name: str | None = None,
    kind: str | None = None,
    listing_currency: str | None = None,
    exchange: str | None = None,
) -> dict:
    """Update a security (partial)."""
    body: dict[str, Any] = {}
    if symbol is not None:
        body["symbol"] = symbol
    if name is not None:
        body["name"] = name
    if kind is not None:
        body["kind"] = kind
    if listing_currency is not None:
        body["listing_currency"] = listing_currency.upper()
    if exchange is not None:
        body["exchange"] = exchange
    return client.request("PATCH", f"/api/v1/securities/{security_id}", json=body)  # type: ignore[return-value]


@mcp.tool()
def securities_delete(security_id: int) -> dict:
    """Delete a security."""
    return client.request("DELETE", f"/api/v1/securities/{security_id}")  # type: ignore[return-value]


@mcp.tool()
def securities_lookup(symbol: str) -> dict:
    """Lookup symbol via yfinance for auto-fill data."""
    return client.request("GET", "/api/v1/securities/lookup", params={"symbol": symbol})  # type: ignore[return-value]


@mcp.tool()
def securities_overview(security_id: int) -> dict:
    """Get full security overview (dashboard payload)."""
    return client.request("GET", f"/api/v1/securities/{security_id}/overview")  # type: ignore[return-value]


@mcp.tool()
def securities_fundamentals(
    security_id: int,
    trailing_eps: str | None = None,
    pe_ratio: str | None = None,
    market_cap: str | None = None,
    dividend_yield_pct: str | None = None,
    beta: str | None = None,
    sector: str | None = None,
    industry: str | None = None,
    country: str | None = None,
    fifty_two_week_high: str | None = None,
    fifty_two_week_low: str | None = None,
) -> dict:
    """Update fundamentals manually (manual-wins over yfinance)."""
    body: dict[str, Any] = {"source": "manual"}
    if trailing_eps is not None:
        body["trailing_eps"] = trailing_eps
    if pe_ratio is not None:
        body["pe_ratio"] = pe_ratio
    if market_cap is not None:
        body["market_cap"] = market_cap
    if dividend_yield_pct is not None:
        body["dividend_yield_pct"] = dividend_yield_pct
    if beta is not None:
        body["beta"] = beta
    if sector is not None:
        body["sector"] = sector
    if industry is not None:
        body["industry"] = industry
    if country is not None:
        body["country"] = country
    if fifty_two_week_high is not None:
        body["fifty_two_week_high"] = fifty_two_week_high
    if fifty_two_week_low is not None:
        body["fifty_two_week_low"] = fifty_two_week_low
    return client.request("PUT", f"/api/v1/securities/{security_id}/fundamentals", json=body)  # type: ignore[return-value]


@mcp.tool()
def securities_holdings_get(security_id: int) -> dict:
    """Get ETF holdings/composition."""
    return client.request("GET", f"/api/v1/securities/{security_id}/holdings")  # type: ignore[return-value]


@mcp.tool()
def securities_holdings_update(security_id: int, items: list[dict]) -> dict:
    """Bulk replace ETF holdings.

    Each item: holding_name, weight_pct, asset_class,
    holding_symbol?, region?, country?, sector?.
    """
    return client.request(  # type: ignore[return-value]
        "PUT",
        f"/api/v1/securities/{security_id}/holdings",
        json={"items": items},
    )


@mcp.tool()
def securities_holdings_import(security_id: int) -> dict:
    """Import ETF holdings from yfinance."""
    return client.request("POST", f"/api/v1/securities/{security_id}/holdings/import")  # type: ignore[return-value]


@mcp.tool()
def securities_refresh(security_id: int) -> dict:
    """Force market data refresh for a security."""
    return client.request("POST", f"/api/v1/securities/{security_id}/refresh")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------


@mcp.tool()
def trades_list(
    limit: int = 50,
    offset: int = 0,
    account_id: int | None = None,
    security_id: int | None = None,
    action: str | None = None,
) -> dict:
    """List trades, paginated.

    action: buy|sell|dividend|drip|fee|interest|deposit
    |withdrawal|transfer_in|transfer_out
    |return_of_capital|corporate_action_adjustment
    """
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if account_id is not None:
        params["account_id"] = account_id
    if security_id is not None:
        params["security_id"] = security_id
    if action is not None:
        params["action"] = action
    return client.request("GET", "/api/v1/trades", params=params)  # type: ignore[return-value]


@mcp.tool()
def trades_create(
    account_id: int,
    security_id: int,
    action: str,
    date: str,
    price_native: str,
    quantity: str = "0",
    commission: str = "0",
    fx_rate_used: str | None = None,
) -> dict:
    """Record a trade.

    buy/drip: positive quantity
    sell: negative quantity
    cash-only actions: quantity=0
    """
    body: dict[str, Any] = {
        "account_id": account_id,
        "security_id": security_id,
        "action": action,
        "date": date,
        "price_native": price_native,
        "quantity": quantity,
        "commission": commission,
    }
    if fx_rate_used is not None:
        body["fx_rate_used"] = fx_rate_used
    return client.request("POST", "/api/v1/trades", json=body)  # type: ignore[return-value]


@mcp.tool()
def trades_get(trade_id: int) -> dict:
    """Get a trade by ID."""
    return client.request("GET", f"/api/v1/trades/{trade_id}")  # type: ignore[return-value]


@mcp.tool()
def trades_update(
    trade_id: int,
    account_id: int | None = None,
    security_id: int | None = None,
    action: str | None = None,
    date: str | None = None,
    quantity: str | None = None,
    price_native: str | None = None,
    commission: str | None = None,
    fx_rate_used: str | None = None,
) -> dict:
    """Update a trade (partial)."""
    body: dict[str, Any] = {}
    if account_id is not None:
        body["account_id"] = account_id
    if security_id is not None:
        body["security_id"] = security_id
    if action is not None:
        body["action"] = action
    if date is not None:
        body["date"] = date
    if quantity is not None:
        body["quantity"] = quantity
    if price_native is not None:
        body["price_native"] = price_native
    if commission is not None:
        body["commission"] = commission
    if fx_rate_used is not None:
        body["fx_rate_used"] = fx_rate_used
    return client.request("PATCH", f"/api/v1/trades/{trade_id}", json=body)  # type: ignore[return-value]


@mcp.tool()
def trades_delete(trade_id: int) -> dict:
    """Delete a trade."""
    return client.request("DELETE", f"/api/v1/trades/{trade_id}")  # type: ignore[return-value]


@mcp.tool()
def trades_dividend_suggestions() -> dict:
    """Get pending dividend suggestions from yfinance."""
    return client.request("GET", "/api/v1/trades/dividend-suggestions")  # type: ignore[return-value]


@mcp.tool()
def trades_record_dividend(
    account_id: int,
    security_id: int,
    date: str,
    gross_amount: str,
    action: str = "dividend",
    drip: bool = False,
    drip_price: str | None = None,
    drip_commission: str = "0",
    fractional: bool = True,
    fx_rate_used: str | None = None,
) -> dict:
    """Record a dividend (creates 1-3 records atomically)."""
    body: dict[str, Any] = {
        "account_id": account_id,
        "security_id": security_id,
        "date": date,
        "gross_amount": gross_amount,
        "action": action,
        "drip": drip,
        "drip_commission": drip_commission,
        "fractional": fractional,
    }
    if drip_price is not None:
        body["drip_price"] = drip_price
    if fx_rate_used is not None:
        body["fx_rate_used"] = fx_rate_used
    return client.request("POST", "/api/v1/trades/record-dividend", json=body)  # type: ignore[return-value]


@mcp.tool()
def trades_import(account_id: int, file_path: str) -> dict:
    """Upload a CSV of trades for preview.

    file_path must be an existing .csv file inside MCP_UPLOAD_DIR (default
    /home/appuser/mcp_uploads), at most 5 MB; anything else is rejected with a
    structured tool error before any bytes are read.
    """
    with open_upload(file_path, CSV_EXTENSIONS) as f:
        # account_id is a multipart FORM field, not a query parameter:
        # app/api/trades.py::import_trades declares `account_id: Annotated[int, Form()]`,
        # so a query string leaves the field unbound and FastAPI answers 422
        # field_required. Same shape as imports_upload below.
        return client.request(  # type: ignore[return-value]
            "POST",
            "/api/v1/trades/import",
            files={
                "file": (os.path.basename(f.name), f, "text/csv"),
                "account_id": (None, str(account_id)),
            },
        )


@mcp.tool()
def trades_import_commit(batch_id: int, include_ids: list[int] | None = None) -> dict:
    """Commit a trade import batch."""
    body: dict[str, Any] = {"include_ids": include_ids or []}
    return client.request("POST", f"/api/v1/trades/import/{batch_id}/commit", json=body)  # type: ignore[return-value]


@mcp.tool()
def trades_import_discard(batch_id: int) -> dict:
    """Discard a trade import batch."""
    return client.request("POST", f"/api/v1/trades/import/{batch_id}/discard")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


@mcp.tool()
def prices_list(
    limit: int = 50,
    offset: int = 0,
    security_id: int | None = None,
) -> dict:
    """List prices, paginated."""
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if security_id is not None:
        params["security_id"] = security_id
    return client.request("GET", "/api/v1/prices", params=params)  # type: ignore[return-value]


@mcp.tool()
def prices_upsert(security_id: int, date: str, price: str) -> dict:
    """Upsert a manual price (manual wins over yfinance)."""
    return client.request(  # type: ignore[return-value]
        "POST",
        "/api/v1/prices",
        json={"security_id": security_id, "date": date, "price": price},
    )


@mcp.tool()
def prices_get(price_id: int) -> dict:
    """Get a price by ID."""
    return client.request("GET", f"/api/v1/prices/{price_id}")  # type: ignore[return-value]


@mcp.tool()
def prices_update(price_id: int, price: str) -> dict:
    """Update a price (forced to manual source)."""
    return client.request("PATCH", f"/api/v1/prices/{price_id}", json={"price": price})  # type: ignore[return-value]


@mcp.tool()
def prices_delete(price_id: int) -> dict:
    """Delete a price."""
    return client.request("DELETE", f"/api/v1/prices/{price_id}")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# FX
# ---------------------------------------------------------------------------


@mcp.tool()
def fx_get(
    base: str,
    quote: str,
    date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Get FX rate(s). Single date or date range."""
    params: dict[str, Any] = {
        "base": base.upper(),
        "quote": quote.upper(),
    }
    if date is not None:
        params["date"] = date
    if start_date is not None:
        params["start_date"] = start_date
    if end_date is not None:
        params["end_date"] = end_date
    return client.request("GET", "/api/v1/fx", params=params)  # type: ignore[return-value]


@mcp.tool()
def fx_board(days: int = 90) -> dict:
    """Get multi-currency FX board (CAD base, last N days)."""
    return client.request("GET", "/api/v1/fx/board", params={"days": days})  # type: ignore[return-value]


@mcp.tool()
def fx_create(base: str, quote: str, date: str, rate: str) -> dict:
    """Create a manual FX rate (gap-fill)."""
    return client.request(  # type: ignore[return-value]
        "POST",
        "/api/v1/fx",
        json={
            "base": base.upper(),
            "quote": quote.upper(),
            "date": date,
            "rate": rate,
        },
    )


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------


@mcp.tool()
def portfolio_positions(
    account_id: int | None = None,
) -> dict:
    """Get current portfolio positions with market values."""
    params: dict[str, Any] = {}
    if account_id is not None:
        params["account_id"] = account_id
    return client.request("GET", "/api/v1/portfolio/positions", params=params)  # type: ignore[return-value]


@mcp.tool()
def portfolio_exposure(
    account_id: int | None = None,
) -> dict:
    """Get exposure by listing currency."""
    params: dict[str, Any] = {}
    if account_id is not None:
        params["account_id"] = account_id
    return client.request("GET", "/api/v1/portfolio/exposure", params=params)  # type: ignore[return-value]


@mcp.tool()
def portfolio_breakdown(
    dimension: str,
    account_id: int | None = None,
) -> dict:
    """Look-through exposure breakdown.

    dimension: sector|geography|asset_class|company
    """
    params: dict[str, Any] = {"dimension": dimension}
    if account_id is not None:
        params["account_id"] = account_id
    return client.request("GET", "/api/v1/portfolio/breakdown", params=params)  # type: ignore[return-value]


@mcp.tool()
def portfolio_refresh() -> dict:
    """Queue background price+fundamentals refresh."""
    return client.request("POST", "/api/v1/portfolio/refresh")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@mcp.tool()
def reports_spending(
    from_date: str,
    to_date: str,
    granularity: str = "month",
    format: str = "json",
    group_by: str = "category",
    category_id: str = "",
) -> dict:
    """Spending report. granularity: day|week|month|year.

    group_by: category|tag (M124). category_id: drill filter — a category id
    (that category and its descendants) or "uncategorized"; empty for all.
    """
    params = {
        "from": from_date,
        "to": to_date,
        "granularity": granularity,
        "format": format,
        "group_by": group_by,
    }
    if category_id:
        params["category_id"] = category_id
    return client.request("GET", "/api/v1/reports/spending", params=params)  # type: ignore[return-value]


@mcp.tool()
def reports_spending_categories(
    from_date: str,
    to_date: str,
    format: str = "json",
    group_by: str = "category",
) -> dict:
    """Category breakdown of spending. group_by: category|tag (M124)."""
    return client.request(  # type: ignore[return-value]
        "GET",
        "/api/v1/reports/spending/categories",
        params={
            "from": from_date,
            "to": to_date,
            "format": format,
            "group_by": group_by,
        },
    )


@mcp.tool()
def reports_category_tag_matrix(from_date: str, to_date: str, format: str = "json") -> dict:
    """Category x Tag spend matrix (M124) — which tags each category carries.

    Cells count a transaction once PER TAG, so a transaction with several
    tags appears in each of its tag cells; `total_cad` is the once-counted
    total.
    """
    return client.request(  # type: ignore[return-value]
        "GET",
        "/api/v1/reports/category-tag-matrix",
        params={"from": from_date, "to": to_date, "format": format},
    )


@mcp.tool()
def reports_cashflow(
    from_date: str,
    to_date: str,
    granularity: str = "month",
    format: str = "json",
) -> dict:
    """Cashflow report (income vs expenses)."""
    return client.request(  # type: ignore[return-value]
        "GET",
        "/api/v1/reports/cashflow",
        params={
            "from": from_date,
            "to": to_date,
            "granularity": granularity,
            "format": format,
        },
    )


@mcp.tool()
def reports_net_worth(
    from_date: str,
    to_date: str,
    granularity: str = "month",
    format: str = "json",
) -> dict:
    """Net worth history over time."""
    return client.request(  # type: ignore[return-value]
        "GET",
        "/api/v1/reports/net-worth",
        params={
            "from": from_date,
            "to": to_date,
            "granularity": granularity,
            "format": format,
        },
    )


@mcp.tool()
def reports_performance(
    from_date: str,
    to_date: str,
    format: str = "json",
) -> dict:
    """Investment performance report."""
    return client.request(  # type: ignore[return-value]
        "GET",
        "/api/v1/reports/investment-performance",
        params={
            "from": from_date,
            "to": to_date,
            "format": format,
        },
    )


@mcp.tool()
def reports_capital_gains(year: int, format: str = "json") -> dict:
    """Capital gains report for a tax year."""
    return client.request(  # type: ignore[return-value]
        "GET",
        "/api/v1/reports/capital-gains",
        params={"year": year, "format": format},
    )


@mcp.tool()
def reports_dividends(year: int | None = None, format: str = "json") -> dict:
    """Dividend report (by security, by month)."""
    params: dict[str, Any] = {"format": format}
    if year is not None:
        params["year"] = year
    return client.request("GET", "/api/v1/reports/dividends", params=params)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------


@mcp.tool()
def projections_run(
    start_value_cad: str,
    annual_contribution_cad: str,
    years: int,
    inflation_pct: str,
    scenarios: list[dict],
) -> dict:
    """Run portfolio projections.

    scenarios: list of {label, annual_return_pct} (1-8).
    """
    return client.request(  # type: ignore[return-value]
        "POST",
        "/api/v1/projections/run",
        json={
            "start_value_cad": start_value_cad,
            "annual_contribution_cad": annual_contribution_cad,
            "years": years,
            "inflation_pct": inflation_pct,
            "scenarios": scenarios,
        },
    )


@mcp.tool()
def projections_defaults() -> dict:
    """Get projection defaults (portfolio value for UI prefill)."""
    return client.request("GET", "/api/v1/projections/defaults")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# ACB
# ---------------------------------------------------------------------------


@mcp.tool()
def acb_list(
    limit: int = 50,
    offset: int = 0,
    account_id: int | None = None,
    security_id: int | None = None,
) -> dict:
    """List ACB pools, paginated."""
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if account_id is not None:
        params["account_id"] = account_id
    if security_id is not None:
        params["security_id"] = security_id
    return client.request("GET", "/api/v1/acb", params=params)  # type: ignore[return-value]


@mcp.tool()
def acb_rebuild() -> dict:
    """Rebuild all ACB pools for the current user."""
    return client.request("POST", "/api/v1/acb/rebuild", json={})  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


@mcp.tool()
def imports_suggest_profile(file_path: str) -> dict:
    """Analyze CSV headers to suggest column mappings.

    file_path must be an existing .csv file inside MCP_UPLOAD_DIR (default
    /home/appuser/mcp_uploads), at most 5 MB.
    """
    with open_upload(file_path, CSV_EXTENSIONS) as f:
        return client.request(  # type: ignore[return-value]
            "POST",
            "/api/v1/imports/suggest-profile",
            files={"file": (os.path.basename(f.name), f, "text/csv")},
        )


@mcp.tool()
def imports_upload(
    account_id: int,
    file_path: str,
    profile_json: str | None = None,
) -> dict:
    """Upload a CSV/OFX/QFX file for import preview.

    file_path must be an existing .csv, .ofx or .qfx file inside MCP_UPLOAD_DIR
    (default /home/appuser/mcp_uploads), at most 5 MB.
    """
    with open_upload(file_path, STATEMENT_EXTENSIONS) as f:
        form_data: dict[str, Any] = {
            "file": (
                os.path.basename(f.name),
                f,
                "application/octet-stream",
            ),
            "account_id": (None, str(account_id)),
        }
        if profile_json is not None:
            form_data["profile_json"] = (None, profile_json)
        return client.request("POST", "/api/v1/imports/upload", files=form_data)  # type: ignore[return-value]


@mcp.tool()
def imports_list_batches(limit: int = 50, offset: int = 0) -> dict:
    """List import batches, paginated."""
    return client.request("GET", "/api/v1/imports", params={"limit": limit, "offset": offset})  # type: ignore[return-value]


@mcp.tool()
def imports_get_batch(batch_id: int) -> dict:
    """Get an import batch by ID."""
    return client.request("GET", f"/api/v1/imports/{batch_id}")  # type: ignore[return-value]


@mcp.tool()
def imports_get_batch_rows(batch_id: int) -> dict:
    """Get rows in an import batch."""
    return client.request("GET", f"/api/v1/imports/{batch_id}/rows")  # type: ignore[return-value]


@mcp.tool()
def imports_commit_batch(batch_id: int, include_ids: list[int] | None = None) -> dict:
    """Commit an import batch."""
    body: dict[str, Any] = {"include_ids": include_ids or []}
    return client.request("POST", f"/api/v1/imports/{batch_id}/commit", json=body)  # type: ignore[return-value]


@mcp.tool()
def imports_discard_batch(batch_id: int) -> dict:
    """Discard an import batch."""
    return client.request("POST", f"/api/v1/imports/{batch_id}/discard")  # type: ignore[return-value]


@mcp.tool()
def imports_revert_batch(batch_id: int) -> dict:
    """Revert a committed import batch."""
    return client.request("POST", f"/api/v1/imports/{batch_id}/revert")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@mcp.tool()
def export_full() -> dict:
    """Export full tenant data as JSON snapshot."""
    return client.request("GET", "/api/v1/export/full")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@mcp.tool()
def search_global(q: str, limit: int = 5) -> dict:
    """Fuzzy cross-domain search."""
    return client.request("GET", "/api/v1/search", params={"q": q, "limit": limit})  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------


@mcp.tool()
def watchlist_list() -> dict:
    """List watchlist items with latest prices."""
    return client.request("GET", "/api/v1/watchlist")  # type: ignore[return-value]


@mcp.tool()
def watchlist_add(security_id: int) -> dict:
    """Add a security to the watchlist."""
    return client.request("POST", "/api/v1/watchlist", json={"security_id": security_id})  # type: ignore[return-value]


@mcp.tool()
def watchlist_remove(security_id: int) -> dict:
    """Remove a security from the watchlist."""
    return client.request("DELETE", f"/api/v1/watchlist/{security_id}")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Suggestions (LLM categorization)
# ---------------------------------------------------------------------------


@mcp.tool()
def suggestions_run(
    from_date: str | None = None,
    to_date: str | None = None,
    transaction_ids: list[int] | None = None,
) -> dict:
    """Run categorization pipeline. Optional date range or transaction IDs."""
    body: dict[str, Any] = {}
    if from_date is not None:
        body["from"] = from_date
    if to_date is not None:
        body["to"] = to_date
    if transaction_ids is not None:
        body["transaction_ids"] = transaction_ids
    return client.request("POST", "/api/v1/suggestions/run", json=body)  # type: ignore[return-value]


@mcp.tool()
def suggestions_list(
    limit: int = 50,
    offset: int = 0,
    status: str = "suggested",
) -> dict:
    """List LLM suggestions.

    status: suggested|accepted|rejected
    """
    return client.request(  # type: ignore[return-value]
        "GET",
        "/api/v1/suggestions",
        params={"limit": limit, "offset": offset, "status": status},
    )


@mcp.tool()
def suggestions_accept(suggestion_id: int) -> dict:
    """Accept an LLM suggestion."""
    return client.request("POST", f"/api/v1/suggestions/{suggestion_id}/accept")  # type: ignore[return-value]


@mcp.tool()
def suggestions_reject(suggestion_id: int) -> dict:
    """Reject an LLM suggestion."""
    return client.request("POST", f"/api/v1/suggestions/{suggestion_id}/reject")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Rules (categorization rules)
# ---------------------------------------------------------------------------


@mcp.tool()
def rules_list(limit: int = 50, offset: int = 0) -> dict:
    """List categorization rules, paginated."""
    return client.request("GET", "/api/v1/rules", params={"limit": limit, "offset": offset})  # type: ignore[return-value]


@mcp.tool()
def rules_create(
    match_type: str,
    pattern: str,
    priority: int,
    category_id: int,
    enabled: bool = True,
) -> dict:
    """Create a categorization rule.

    match_type: regex|exact_payee|contains
    """
    return client.request(  # type: ignore[return-value]
        "POST",
        "/api/v1/rules",
        json={
            "match_type": match_type,
            "pattern": pattern,
            "priority": priority,
            "category_id": category_id,
            "enabled": enabled,
        },
    )


@mcp.tool()
def rules_get(rule_id: int) -> dict:
    """Get a rule by ID."""
    return client.request("GET", f"/api/v1/rules/{rule_id}")  # type: ignore[return-value]


@mcp.tool()
def rules_update(
    rule_id: int,
    match_type: str | None = None,
    pattern: str | None = None,
    priority: int | None = None,
    category_id: int | None = None,
    enabled: bool | None = None,
) -> dict:
    """Update a rule (partial)."""
    body: dict[str, Any] = {}
    if match_type is not None:
        body["match_type"] = match_type
    if pattern is not None:
        body["pattern"] = pattern
    if priority is not None:
        body["priority"] = priority
    if category_id is not None:
        body["category_id"] = category_id
    if enabled is not None:
        body["enabled"] = enabled
    return client.request("PATCH", f"/api/v1/rules/{rule_id}", json=body)  # type: ignore[return-value]


@mcp.tool()
def rules_delete(rule_id: int) -> dict:
    """Delete a rule."""
    return client.request("DELETE", f"/api/v1/rules/{rule_id}")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Assistant (BYO-AI connector)
# ---------------------------------------------------------------------------


@mcp.tool()
def proposals_create(
    proposal_kind: str,
    proposal_target: str,
    proposal_summary: str,
    proposal_fields: dict,
    question: str,
    answer: str,
) -> dict:
    """Propose a change to your data. Lands as a pending card; nothing is
    modified until you confirm it in the fintrack web app.

    proposal_kind: account.update | transaction.update | forecast_scenario.update
    proposal_target: "<prefix>:<id>" matching the kind (e.g. "transaction:123")
    proposal_fields: the fields to change and their new values
    question: what the user asked you; answer: your explanation of the change
    """
    return client.request(
        "POST",
        "/api/v1/assistant/proposals",
        json={
            "proposal_kind": proposal_kind,
            "proposal_target": proposal_target,
            "proposal_summary": proposal_summary,
            "proposal_fields": proposal_fields,
            "question": question,
            "answer": answer,
        },
    )  # type: ignore[return-value]


def create_auth_app(host: str = "127.0.0.1"):
    """Create the MCP Starlette app wrapped with auth middleware.

    ``transport_security`` is passed explicitly instead of left to the SDK
    default. Left alone, mcp only enables its DNS-rebinding check when ``host``
    is loopback — and when it does, it allows exactly ``127.0.0.1:*``,
    ``localhost:*`` and ``[::1]:*``. This endpoint is meant to be reached
    through the web proxy at the PUBLIC host (``https://<host>/mcp/mcp``,
    docs/23/40), so that allowlist would 421 every proxied request, and no
    allowlist can cover the domain a deployment happens to use.

    The security boundary here is the bearer token, not the Host header:
    McpAuthMiddleware is default-deny and validate-mcp-token authenticates
    EVERY request including ``initialize``, no cookie or other ambient
    credential exists for a browser to replay cross-origin, and no CORS
    headers are emitted for ``/mcp`` — so a rebinding client holds nothing.
    ``host`` is still forwarded: it is what the transport is told about its
    own listener.
    """
    app = mcp.streamable_http_app(
        host=host,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    app.add_middleware(McpAuthMiddleware)
    return app
