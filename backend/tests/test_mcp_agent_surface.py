"""M80 — the MCP agent surface: policy, toolset filtering, write receipts (docs/40).

Three layers, all against real code:

1. **Classification** (pure): every one of the 107 tools resolves to read or
   write from `server.py` itself, and the compute-only allowlist cannot go
   stale.
2. **The seam** (streamable-http, in-process): `create_auth_app()` over raw
   JSON-RPC with the token validator faked, so the contextvar plumbing the
   production path uses is exercised end to end. `client._http` is swapped for
   a recording MockTransport — a refusal that reached the backend would be a
   failed assertion, not a silent pass.
3. **The backend** (Postgres): mint → list → validate → ingest → read, plus
   the tenancy and secret checks around the two new routes.

FIN_TRACKER_URL is pinned to `.invalid` so no ambient environment can route a
proxy call at the :8000 tunnel while these run.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("FIN_TRACKER_URL", "http://mcp-tests.invalid")

import mcp_server.server as mcp_server_module
from app.core.config import get_settings
from app.core.ratelimit import limiter
from mcp_server import policy
from mcp_server.audit import args_hash, response_code
from mcp_server.auth import in_mcp_request
from mcp_server.client import new_http_client

REGISTER = "/api/v1/auth/register"
TOKENS = "/api/v1/auth/mcp-tokens"
VALIDATE = "/api/v1/auth/validate-mcp-token"
AUDIT_INGEST = "/api/v1/auth/internal-mcp-audit"
AUDIT_READ = "/api/v1/mcp-audit"

EMAIL = "agent-surface@example.com"
PASSWORD = "correct-horse-battery"

INTERNAL_SECRET = "test-internal-secret-for-m80"

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "m80-tests", "version": "0"},
    },
}


@pytest.fixture(autouse=True)
def internal_secret(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point `get_settings().mcp_internal_secret` at a known value.

    The two new backend routes authorize on Settings (like their internal
    siblings), and Settings is cached — so the env var AND the cache have to
    move together, in both directions (M29's lesson in test_m29_validation).
    """
    monkeypatch.setenv("MCP_INTERNAL_SECRET", INTERNAL_SECRET)
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("MCP_INTERNAL_SECRET", raising=False)
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 1. classification
# --------------------------------------------------------------------------- #


def test_every_tool_is_classified() -> None:
    """docs/40 §2 + docs/59 §3.2: 107 tools = 53 read + 54 write, none unclassifiable."""
    methods = policy.tool_methods()
    read, write = policy.split_tools(list(methods))
    assert len(methods) == 107, "a tool was added or removed without docs/40"
    assert (len(read), len(write)) == (53, 54), (
        "read/write split changed — update docs/40 and the help guides"
    )
    # Unclassifiable would have landed in `write`; assert none did by name:
    # every tool resolves to at least one literal HTTP method.
    assert all(methods[name] for name in methods)


def test_compute_only_allowlist_cannot_go_stale() -> None:
    """Each allowlisted POST still exists; nothing else sneaks into read."""
    methods = policy.tool_methods()
    assert set(policy.COMPUTE_ONLY_TOOLS) <= set(methods)
    for name in policy.COMPUTE_ONLY_TOOLS:
        assert not policy.is_write_tool(name)
        assert methods[name] == frozenset({"POST"}), name
    # A tool that does not exist is a write (fail closed), not a free pass.
    assert policy.is_write_tool("no_such_tool")
    # And the classification is method-driven, not name-driven.
    assert not policy.is_write_tool("accounts_list")
    assert policy.is_write_tool("accounts_create")
    assert policy.is_write_tool("accounts_delete")


def test_refusal_envelopes_use_the_pinned_codes() -> None:
    read_only = policy.refusal_envelope("accounts_create", "read_only", False)
    assert read_only is not None
    assert read_only["error"] == 403
    assert read_only["detail"]["code"] == "mcp_read_only"
    assert "accounts_create" in read_only["detail"]["detail"]

    # The demo branch reuses the backend's wording verbatim (docs/40 §2).
    demo = policy.refusal_envelope("accounts_create", "full", True)
    assert demo == {
        "error": 403,
        "detail": {
            "detail": "The demo account is read-only; changes are not allowed",
            "code": "demo_account_restricted",
        },
    }
    # Unrestricted full token, not demo → nothing to refuse.
    assert policy.refusal_envelope("accounts_create", "full", False) is None

    # BYO-AI connector scope (docs/59 §2): propose is its one allowed write,
    # domain writes get their own code, and `read_only` cannot propose at all.
    assert policy.refusal_envelope("proposals_create", "read_propose", False) is None
    domain = policy.refusal_envelope("accounts_create", "read_propose", False)
    assert domain is not None
    assert domain["error"] == 403
    assert domain["detail"]["code"] == "mcp_read_propose"
    assert "proposals_create" in domain["detail"]["detail"]
    read_only_propose = policy.refusal_envelope("proposals_create", "read_only", False)
    assert read_only_propose is not None
    assert read_only_propose["detail"]["code"] == "mcp_read_only"
    # Demo wins over every tier (docs/59 §1: demo is read-only, full stop).
    demo_propose = policy.refusal_envelope("proposals_create", "read_propose", True)
    assert demo_propose is not None
    assert demo_propose["detail"]["code"] == "demo_account_restricted"


def test_propose_tools_pin() -> None:
    """docs/59 §2: the propose write is a write — never a compute-only one."""
    assert policy.PROPOSE_TOOLS == frozenset({"proposals_create"})
    assert not (set(policy.PROPOSE_TOOLS) & set(policy.COMPUTE_ONLY_TOOLS))
    # Unregistered names are writes (fail closed), so this holds before the
    # tool lands in server.py and after — it proxies a POST.
    assert policy.is_write_tool("proposals_create") is True


def test_marker_and_annotations() -> None:
    assert policy.restriction_marker("full", False) is None
    assert policy.restriction_marker("read_only", False) == "read-only token"
    assert policy.restriction_marker("full", True) == "demo tenant is read-only"
    assert (
        policy.restriction_marker("read_only", True) == "read-only token · demo tenant is read-only"
    )
    # BYO-AI connector tier carries its own marker (docs/59 §1).
    assert policy.restriction_marker("read_propose", False) == "read+propose token"
    assert (
        policy.restriction_marker("read_propose", True)
        == "read+propose token · demo tenant is read-only"
    )
    # Anything that is not exactly `full` is restricted (fail closed).
    assert policy.restriction_marker("something_new", False) is not None

    marked = policy.tool_annotations("accounts_list", "read-only token")
    assert marked["readOnlyHint"] is True
    assert marked["title"] == "accounts_list · read-only token"
    unmarked = policy.tool_annotations("accounts_list", None)
    assert unmarked == {"readOnlyHint": True}
    delete = policy.tool_annotations("accounts_delete", None)
    assert delete["readOnlyHint"] is False and delete["destructiveHint"] is True
    create = policy.tool_annotations("accounts_create", None)
    assert create["readOnlyHint"] is False and create["destructiveHint"] is False


# --------------------------------------------------------------------------- #
# 2. the seam — raw JSON-RPC against create_auth_app()
# --------------------------------------------------------------------------- #


class _Validator:
    """Stands in for `POST /auth/validate-mcp-token`.

    Reads the test's `seam` state at request time, so a test can flip
    policy/identity without re-plumbing the ASGI middleware.
    """

    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state

    async def __aenter__(self) -> _Validator:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def post(self, url: str, **kwargs: Any) -> Any:
        assert url.endswith("/api/v1/auth/validate-mcp-token"), url
        state = self._state
        return httpx.Response(
            200,
            json={
                "user_id": state["user_id"],
                "email": "agent@example.com",
                "policy": state["policy"],
                "token_id": state["token_id"],
                "is_demo": state["is_demo"],
            },
        )


@pytest.fixture(name="seam")
def seam_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """An authenticated MCP session plus spies for audit and backend traffic.

    Identity comes from the faked validator, receipts land in
    `state["receipts"]`, and every proxy attempt is recorded in
    `state["backend_calls"]` — a refusal that reached the backend shows up
    there instead of passing silently.
    """
    receipts: list[dict] = []
    backend_calls: list[tuple[str, str]] = []

    def _backend(request: Any) -> Any:
        backend_calls.append((request.method, request.url.path))
        if request.url.path.endswith("/auth/internal-issue-token"):
            # Mode 3 asks for a per-user JWT before it proxies anything.
            return httpx.Response(200, json={"access_token": "backend-jwt", "token_type": "bearer"})
        return httpx.Response(200, json={"ok": True})

    state: dict[str, Any] = {
        "receipts": receipts,
        "backend_calls": backend_calls,
        "policy": "full",
        "is_demo": False,
        "user_id": 4242,
        "token_id": 9,
    }
    validator = _Validator(state)

    class _AsyncClientStub:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _Validator:
            return validator

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClientStub)
    monkeypatch.setattr(mcp_server_module, "record_audit", lambda **kw: receipts.append(kw))
    # Any proxy attempt from a refusal test lands here instead of silently
    # succeeding against whatever listens on :8000.
    # The singleton read MCP_INTERNAL_SECRET at import (before any test env):
    # give it the secret so mode 3 behaves the way the container does.
    monkeypatch.setattr(mcp_server_module.client, "_internal_secret", INTERNAL_SECRET)
    monkeypatch.setattr(
        mcp_server_module.client,
        "_http",
        # new_http_client: the shared no-store jar (M84) the seam must run on.
        new_http_client(
            base_url="http://mcp-tests.invalid", transport=httpx.MockTransport(_backend)
        ),
    )

    app = mcp_server_module.create_auth_app()
    with TestClient(app, base_url="http://localhost:8001") as client:
        state["client"] = client
        yield state


def _rpc(client: Any, session_id: str | None, body: dict) -> tuple[Any, str | None, dict | None]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer opaque",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    response = client.post("/mcp", json=body, headers=headers)
    sid = session_id or response.headers.get("mcp-session-id")
    payload = None
    for line in response.text.splitlines():
        if line.startswith("data:"):
            payload = json.loads(line[5:].strip())
    return response, sid, payload


def _handshake(seam: dict[str, Any]) -> None:
    """initialize + notifications/initialized; the session is then usable."""
    response, sid, init = _rpc(seam["client"], None, INIT)
    assert response.status_code == 200 and init is not None, response.text
    _rpc(seam["client"], sid, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    seam["session_id"] = sid


def _list_tools(seam: dict[str, Any]) -> list[dict]:
    response, sid, payload = _rpc(
        seam["client"],
        seam["session_id"],
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    seam["session_id"] = sid
    assert response.status_code == 200 and payload is not None, response.text
    return payload["result"]["tools"]


def _call(seam: dict[str, Any], tool: str, arguments: dict) -> dict:
    response, sid, payload = _rpc(
        seam["client"],
        seam["session_id"],
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    )
    seam["session_id"] = sid
    assert response.status_code == 200 and payload is not None, response.text
    assert "error" not in payload, payload
    return payload["result"]


def test_full_session_lists_every_tool_annotated(seam: dict[str, Any]) -> None:
    _handshake(seam)
    tools = _list_tools(seam)
    assert (
        len(tools) == 107
    )  # M124 added reports_category_tag_matrix; docs/59 adds proposals_create
    names = {tool["name"] for tool in tools}
    assert {"accounts_create", "transactions_delete", "proposals_create"} <= names
    # Confirmation gate (docs/59 §3.3): proposals_create is the ONLY
    # proposals_* tool — no external session can apply/dismiss/undo/list,
    # in any policy tier.
    assert {name for name in names if name.startswith("proposals_")} == {"proposals_create"}
    for tool in tools:
        annotations = tool["annotations"]
        assert isinstance(annotations["readOnlyHint"], bool)
        assert annotations["readOnlyHint"] is (not policy.is_write_tool(tool["name"]))
        # Unrestricted sessions carry no marker.
        assert "title" not in annotations
    # The propose tool writes an inert card, not domain data (docs/59 §2).
    propose = next(tool for tool in tools if tool["name"] == "proposals_create")
    assert propose["annotations"]["readOnlyHint"] is False
    assert propose["annotations"]["destructiveHint"] is False
    assert seam["receipts"] == []  # listing is not a write


def test_read_only_session_sees_only_read_tools_with_a_marker(seam: dict[str, Any]) -> None:
    seam["policy"] = "read_only"
    _handshake(seam)
    tools = _list_tools(seam)
    names = [tool["name"] for tool in tools]
    assert len(names) == 53, "a write tool leaked into a read-only toolset"
    assert "transactions_create" not in names
    assert "accounts_create" not in names
    assert "accounts_list" in names and "health_check" in names
    for tool in tools:
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["title"] == f"{tool['name']} \u00b7 read-only token"


def test_demo_session_is_filtered_and_marked(seam: dict[str, Any]) -> None:
    seam["is_demo"] = True
    _handshake(seam)
    tools = _list_tools(seam)
    names = [tool["name"] for tool in tools]
    assert len(names) == 53  # M124 added reports_category_tag_matrix
    assert "trades_create" not in names
    assert "reports_net_worth" in names
    assert all(
        tool["annotations"]["title"].endswith("\u00b7 demo tenant is read-only") for tool in tools
    )


def test_read_only_write_call_is_refused_without_reaching_the_backend(
    seam: dict[str, Any],
) -> None:
    seam["policy"] = "read_only"
    _handshake(seam)
    result = _call(seam, "accounts_create", {"name": "Nope"})

    assert result["isError"] is True
    envelope = json.loads(result["content"][0]["text"])
    assert envelope["error"] == 403
    assert envelope["detail"]["code"] == "mcp_read_only"
    assert "accounts_create" in envelope["detail"]["detail"]

    # Nothing left the process: no proxy call, no JWT issuance.
    assert seam["backend_calls"] == []
    # ...but the refusal is on the record.
    assert len(seam["receipts"]) == 1
    receipt = seam["receipts"][0]
    assert receipt["tool"] == "accounts_create"
    assert receipt["status"] == 403
    assert receipt["user_id"] == seam["user_id"]
    assert receipt["mcp_token_id"] == seam["token_id"]
    assert receipt["arguments"] == {"name": "Nope"}  # hashed later, never stored
    assert receipt["duration_ms"] >= 0


def test_read_only_cannot_propose(seam: dict[str, Any]) -> None:
    """AC (docs/59 §7): a read token cannot write — the propose path included.

    Refused on CLASSIFICATION, not dispatch: this holds before and after
    `proposals_create` exists in server.py, because the seam answers from
    `policy.is_write_tool` before the SDK ever resolves the tool name.
    """
    seam["policy"] = "read_only"
    _handshake(seam)
    result = _call(
        seam,
        "proposals_create",
        {
            "proposal_kind": "transaction.update",
            "proposal_target": "txn:1",
            "proposal_summary": "Reassign Loblaws to groceries",
            "proposal_fields": {"category_id": "7"},
            "question": "Categorize my Loblaws run",
            "answer": "Transaction #123 is groceries.",
        },
    )
    assert result["isError"] is True
    envelope = json.loads(result["content"][0]["text"])
    assert envelope["error"] == 403
    assert envelope["detail"]["code"] == "mcp_read_only"
    assert "proposals_create" in envelope["detail"]["detail"]

    # Nothing left the process: no proxy call, no JWT issuance.
    assert seam["backend_calls"] == []
    assert len(seam["receipts"]) == 1
    receipt = seam["receipts"][0]
    assert receipt["tool"] == "proposals_create"
    assert receipt["status"] == 403
    assert receipt["mcp_token_id"] == seam["token_id"]


def test_read_propose_session_lists_reads_plus_exactly_one_write(
    seam: dict[str, Any],
) -> None:
    """docs/59 §1-2: the BYO-AI connector tier = 53 read + proposals_create."""
    seam["policy"] = "read_propose"
    _handshake(seam)
    tools = _list_tools(seam)
    names = [tool["name"] for tool in tools]
    assert len(names) == 54
    assert "proposals_create" in names
    assert "accounts_list" in names and "health_check" in names
    assert "accounts_create" not in names and "transactions_delete" not in names
    # Exactly one proposals_* tool — the confirmation gate (docs/59 §3.3):
    # an external session can propose but can never apply/dismiss/undo/list.
    assert {name for name in names if name.startswith("proposals_")} == {"proposals_create"}
    for tool in tools:
        assert tool["annotations"]["title"] == f"{tool['name']} · read+propose token"
    # Its one write is a write-class, non-destructive tool (docs/59 §2).
    propose = next(tool for tool in tools if tool["name"] == "proposals_create")
    assert propose["annotations"]["readOnlyHint"] is False
    assert propose["annotations"]["destructiveHint"] is False


def test_read_propose_propose_call_is_proxied_and_audited(seam: dict[str, Any]) -> None:
    """The allowed write on this tier: proxied through mode 3, receipt 200.

    This is the seam half of "audit log entries for every external
    read/write" for the propose path (docs/40 §3, docs/59 §4).
    """
    seam["policy"] = "read_propose"
    _handshake(seam)
    result = _call(
        seam,
        "proposals_create",
        {
            "proposal_kind": "transaction.update",
            "proposal_target": "txn:1",
            "proposal_summary": "Reassign Loblaws to groceries",
            "proposal_fields": {"category_id": "7"},
            "question": "Categorize my Loblaws run",
            "answer": "Transaction #123 is groceries.",
        },
    )
    assert not result.get("isError"), result
    # Mode 3 in full: per-user JWT, then the propose proxy — no apply path.
    assert seam["backend_calls"] == [
        ("POST", "/api/v1/auth/internal-issue-token"),
        ("POST", "/api/v1/assistant/proposals"),
    ]
    receipt = seam["receipts"][0]
    assert receipt["status"] == 200
    assert receipt["tool"] == "proposals_create"
    assert receipt["user_id"] == seam["user_id"]
    assert receipt["mcp_token_id"] == seam["token_id"]


def test_read_propose_domain_write_is_refused_before_the_backend(
    seam: dict[str, Any],
) -> None:
    """docs/59 §2: propose is the tier's ONE write; anything else is 403."""
    seam["policy"] = "read_propose"
    _handshake(seam)
    result = _call(seam, "accounts_create", {"name": "Nope", "type": "tfsa", "currency": "CAD"})

    assert result["isError"] is True
    envelope = json.loads(result["content"][0]["text"])
    assert envelope["error"] == 403
    assert envelope["detail"]["code"] == "mcp_read_propose"
    assert "proposals_create" in envelope["detail"]["detail"]
    assert "would modify data directly" in envelope["detail"]["detail"]

    # Nothing left the process: no proxy call, no JWT issuance — but audited.
    assert seam["backend_calls"] == []
    assert len(seam["receipts"]) == 1
    receipt = seam["receipts"][0]
    assert receipt["tool"] == "accounts_create"
    assert receipt["status"] == 403
    assert receipt["mcp_token_id"] == seam["token_id"]


def test_demo_wins_over_read_propose_in_the_toolset_too(seam: dict[str, Any]) -> None:
    """Demo lists the read toolset even under a read_propose policy value.

    The demo mint guard (422 demo_mcp_read_only) makes demo+read_propose
    unreachable, so the seam must NOT lean on that guard: `_filtered_list`
    drops proposals_create for demo regardless of policy (docs/59 §1) —
    and if a row ever slipped through, `refusal_envelope` refuses the call
    with demo_account_restricted first.
    """
    seam["is_demo"] = True
    seam["policy"] = "read_propose"
    _handshake(seam)
    names = [tool["name"] for tool in _list_tools(seam)]
    assert len(names) == 53
    assert "proposals_create" not in names
    envelope = policy.refusal_envelope("proposals_create", "read_propose", True)
    assert envelope is not None
    assert envelope["detail"]["code"] == "demo_account_restricted"


def test_demo_write_call_is_refused_with_the_backend_code(seam: dict[str, Any]) -> None:
    seam["is_demo"] = True
    _handshake(seam)
    result = _call(seam, "transactions_delete", {"transaction_id": 1})
    envelope = json.loads(result["content"][0]["text"])
    assert envelope == {
        "error": 403,
        "detail": {
            "detail": "The demo account is read-only; changes are not allowed",
            "code": "demo_account_restricted",
        },
    }
    assert seam["backend_calls"] == []
    assert seam["receipts"][0]["status"] == 403


def test_auth_tools_are_refused_on_the_http_path(seam: dict[str, Any]) -> None:
    """M84 (docs/40 §2): session lifecycle is stdio-only.

    The streamable-http path fixes identity in the MCP token, so a
    login/refresh/logout there can only ever act on a session the caller does
    not own — which is what the shared-jar leak (F-1) turned `auth_refresh`
    into. Every auth tool must be refused BEFORE the proxy runs, on a clean
    `{detail, code}` envelope, for every policy.
    """
    _handshake(seam)
    for tool in sorted(policy.AUTH_TOOLS):
        result = _call(seam, tool, {})
        assert result["isError"] is True, tool
        envelope = json.loads(result["content"][0]["text"])
        assert envelope["error"] == 403, tool
        assert envelope["detail"]["code"] == "auth_tools_unavailable_on_mcp", tool

    # Nothing left the process: no proxy call, no JWT issuance.
    assert seam["backend_calls"] == []
    # ...but every refusal is on the record (docs/40 §3 receipt table).
    assert len(seam["receipts"]) == len(policy.AUTH_TOOLS)
    assert {receipt["tool"] for receipt in seam["receipts"]} == set(policy.AUTH_TOOLS)
    assert {receipt["status"] for receipt in seam["receipts"]} == {403}
    assert {receipt["user_id"] for receipt in seam["receipts"]} == {seam["user_id"]}


def test_auth_tool_refusal_is_transport_scoped_not_policy_scoped(
    seam: dict[str, Any],
) -> None:
    """A read-only token's `auth_register` reports the transport refusal.

    The HTTP-path refusal is checked FIRST: the tool is unavailable on this
    transport regardless of what the token may do, so the transport answer
    wins over `mcp_read_only`. Both are 403, and neither reaches the backend.
    """
    seam["policy"] = "read_only"
    _handshake(seam)
    result = _call(seam, "auth_register", {"email": "x@y.z", "password": "p"})

    assert result["isError"] is True
    envelope = json.loads(result["content"][0]["text"])
    assert envelope["error"] == 403
    assert envelope["detail"]["code"] == "auth_tools_unavailable_on_mcp"
    assert seam["backend_calls"] == []


def test_auth_tools_still_work_outside_an_http_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stdio (docs/23 modes 1/2) keeps the auth tools — nothing is filtered.

    `in_mcp_request` is the switch: outside a served /mcp request it defaults
    to False and the tools proxy normally instead of hitting the refusal.
    """
    from mcp_server import server as server_mod

    assert in_mcp_request.get() is False
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={"access_token": "stdio-jwt"},
            headers=[("set-cookie", "refresh_token=stdio-refresh; Path=/api/v1/auth; HttpOnly")],
        )

    monkeypatch.setattr(
        mcp_server_module.client,
        "_http",
        new_http_client(
            base_url="http://mcp-tests.invalid", transport=httpx.MockTransport(handler)
        ),
    )
    monkeypatch.setattr(mcp_server_module.client, "_session_token", None)
    monkeypatch.setattr(mcp_server_module.client, "_session_refresh_token", None)

    result = server_mod.auth_login("a@example.com", "pw")  # type: ignore[arg-type]

    assert result == {"access_token": "stdio-jwt"}
    assert seen == ["/api/v1/auth/login"]
    # The login's Set-Cookie landed in the SESSION slot (no request in
    # flight), so the next stdio tool call can still rotate — off the jar it
    # did not: the shared jar stayed empty.
    assert mcp_server_module.client.refresh_token == "stdio-refresh"
    assert list(mcp_server_module.client._http.cookies.jar) == []


def test_unrestricted_write_call_is_proxied_and_recorded(seam: dict[str, Any]) -> None:
    """The other half of the receipt table: an allowed write -> 200 (docs/40 §3)."""
    _handshake(seam)
    result = _call(
        seam, "accounts_create", {"name": "Brokerage", "type": "tfsa", "currency": "CAD"}
    )
    assert not result.get("isError"), result
    # Mode 3 in full: issue a per-user JWT, then proxy the write.
    assert seam["backend_calls"] == [
        ("POST", "/api/v1/auth/internal-issue-token"),
        ("POST", "/api/v1/accounts"),
    ]
    receipt = seam["receipts"][0]
    assert receipt["status"] == 200
    assert receipt["tool"] == "accounts_create"


def test_read_call_is_proxied_and_recorded(seam: dict[str, Any]) -> None:
    """The read half of the receipt table (docs/40 §3, t_bb0033d2 AC).

    Every external READ must land an audit row too — a BYO-AI connector's
    read scope is exactly this path, so "audit log entries for every
    external read/write" fails without it.
    """
    _handshake(seam)
    result = _call(seam, "accounts_list", {"limit": 5})
    assert not result.get("isError"), result
    # Same mode-3 shape as a write: issue a per-user JWT, then proxy the read.
    assert seam["backend_calls"] == [
        ("POST", "/api/v1/auth/internal-issue-token"),
        ("GET", "/api/v1/accounts"),
    ]
    receipt = seam["receipts"][0]
    assert receipt["status"] == 200
    assert receipt["tool"] == "accounts_list"
    # Attributed to the connector's identity, not left anonymous.
    assert receipt["user_id"] == seam["user_id"]
    assert receipt["mcp_token_id"] == seam["token_id"]
    assert receipt["duration_ms"] >= 0


def test_read_only_read_call_is_proxied_and_recorded(seam: dict[str, Any]) -> None:
    """A read-only (BYO-AI query) token audits reads identically (docs/40 §3)."""
    seam["policy"] = "read_only"
    _handshake(seam)
    result = _call(seam, "accounts_list", {"limit": 5})
    assert not result.get("isError"), result
    receipt = seam["receipts"][0]
    assert receipt["status"] == 200
    assert receipt["tool"] == "accounts_list"
    assert receipt["mcp_token_id"] == seam["token_id"]


def test_seam_accepts_the_public_host_the_proxy_sends(seam: dict[str, Any]) -> None:
    """The public path (docs/40) arrives with `Host: <deployment domain>`.

    The transport's own DNS-rebinding allowlist is loopback-only, so the
    default app answers 421 Misdirected Request to EVERY proxied request —
    which is why `create_auth_app()` passes `transport_security` explicitly
    (docs/23). The boundary that matters is the bearer token, and this
    handshake still carries one.
    """
    response = seam["client"].post(
        "/mcp",
        json=INIT,
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer opaque",
            "Host": "fin.example.com",
        },
    )
    # The client must not quietly rewrite the header back to its base URL.
    assert response.request.headers["host"] == "fin.example.com"
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# 3. the backend — mint / validate / ingest / read
# --------------------------------------------------------------------------- #


def _register(client: TestClient) -> dict:
    client.post(REGISTER, json={"email": EMAIL, "password": PASSWORD})
    response = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_mint_read_only_token_round_trip(client: TestClient) -> None:
    headers = _register(client)

    created = client.post(
        TOKENS, json={"name": "laptop-agent", "policy": "read_only"}, headers=headers
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["policy"] == "read_only"
    # The shape grew by exactly one field (docs/40 §1).
    assert set(body) == {
        "id",
        "name",
        "token",
        "policy",
        "created_at",
        "expires_at",
        "last_used_at",
    }

    # Omitted policy defaults to full, so pre-M80 clients keep working.
    default = client.post(TOKENS, json={"name": "legacy-cli"}, headers=headers)
    assert default.status_code == 201
    assert default.json()["policy"] == "full"

    listing = client.get(TOKENS, headers=headers)
    assert listing.status_code == 200
    policies = {item["name"]: item["policy"] for item in listing.json()["items"]}
    assert policies["laptop-agent"] == "read_only"
    assert policies["legacy-cli"] == "full"

    # validate resolves policy + token id for the MCP middleware.
    validated = client.post(VALIDATE, headers={"Authorization": f"Bearer {body['token']}"})
    assert validated.status_code == 200, validated.text
    payload = validated.json()
    user_id = payload.pop("user_id")
    assert isinstance(user_id, int)
    assert payload == {
        "email": EMAIL,
        "policy": "read_only",
        "token_id": body["id"],
        "is_demo": False,
    }


def test_unknown_policy_is_rejected(client: TestClient) -> None:
    headers = _register(client)
    response = client.post(TOKENS, json={"name": "bad", "policy": "admin"}, headers=headers)
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_audit_ingest_round_trip_and_tenant_isolation(client: TestClient) -> None:
    headers_a = _register(client)
    client.post(REGISTER, json={"email": "other-agent@example.com", "password": PASSWORD})
    other = client.post(
        "/api/v1/auth/login", json={"email": "other-agent@example.com", "password": PASSWORD}
    )
    assert other.status_code == 200, other.text
    headers_b = {"Authorization": f"Bearer {other.json()['access_token']}"}

    receipt = {
        "user_id": other.json()["user"]["id"],
        "mcp_token_id": 3,
        "tool": "accounts_create",
        "args_hash": "a" * 64,
        "response_code": 403,
        "duration_ms": 7,
    }
    secret = {"X-Internal-Secret": INTERNAL_SECRET}

    # Wrong/missing secret is a fail-closed 403 before any write.
    for headers in ({}, {"X-Internal-Secret": "nope"}):
        rejected = client.post(AUDIT_INGEST, json=receipt, headers=headers)
        assert rejected.status_code == 403
        assert rejected.json() == {"detail": "Forbidden", "code": "forbidden"}

    # The MCP server's shape (hash, code range) is validated.
    malformed = client.post(
        AUDIT_INGEST, json={**receipt, "args_hash": "not-a-hash"}, headers=secret
    )
    assert malformed.status_code == 422
    assert malformed.json()["code"] == "validation_error"

    ok = client.post(AUDIT_INGEST, json=receipt, headers=secret)
    assert ok.status_code == 204, ok.text

    # The account that owns the row can read it...
    mine = client.get(AUDIT_READ, headers=headers_b)
    assert mine.status_code == 200, mine.text
    assert mine.json()["total"] == 1
    row = mine.json()["items"][0]
    assert row["tool"] == "accounts_create"
    assert row["response_code"] == 403
    assert row["args_hash"] == "a" * 64
    assert row["duration_ms"] == 7

    # ...and a different tenant cannot see it.
    theirs = client.get(AUDIT_READ, headers=headers_a)
    assert theirs.status_code == 200
    assert theirs.json() == {"items": [], "total": 0}

    # Unknown user fails closed rather than orphaning a row.
    missing = client.post(AUDIT_INGEST, json={**receipt, "user_id": 99_999_999}, headers=secret)
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


def test_audit_ingest_rate_limited_like_its_siblings(client: TestClient) -> None:
    limiter.enabled = True
    try:
        receipt = {
            "user_id": 1,
            "tool": "accounts_list",
            "args_hash": "b" * 64,
            "response_code": 200,
            "duration_ms": 1,
        }
        secret = {"X-Internal-Secret": INTERNAL_SECRET}
        for _ in range(120):
            # user_id 1 may not exist here — 404 still counts against the
            # bucket, which is the point: the limit binds before the lookup.
            assert client.post(AUDIT_INGEST, json=receipt, headers=secret).status_code in (
                204,
                404,
            )
        limited = client.post(AUDIT_INGEST, json=receipt, headers=secret)
        assert limited.status_code == 429
        assert limited.json() == {"detail": "Too many attempts", "code": "rate_limited"}
    finally:
        limiter.enabled = False
        limiter.reset()


# --------------------------------------------------------------------------- #
# 4. receipt derivation (docs/40 §3, pinned table)
# --------------------------------------------------------------------------- #


def _result(text: Any, *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def test_response_code_table() -> None:
    assert response_code(_result(json.dumps({"status": "healthy"}))) == 200
    assert response_code(_result(json.dumps({"error": 422, "detail": {}}))) == 422
    assert response_code(_result(json.dumps({"error": 403, "detail": {}}))) == 403
    assert (
        response_code(
            _result(json.dumps({"detail": "no", "code": "path_not_allowed"}), is_error=True)
        )
        == 400
    )
    assert response_code(_result("Traceback: boom", is_error=True)) == 500
    assert response_code(None) == 200


def test_args_hash_is_canonical_and_hides_values() -> None:
    a = args_hash({"b": 1, "a": 2})
    b = args_hash({"a": 2, "b": 1})
    assert a == b  # key order must not change the receipt
    assert len(a) == 64 and set(a) <= set("0123456789abcdef")
    assert a != args_hash({"a": 2, "b": 2})
    # The password never appears anywhere near the stored digest.
    digest = args_hash({"email": "me@example.com", "password": "hunter2"})
    assert "hunter2" not in digest
    assert digest == args_hash({"password": "hunter2", "email": "me@example.com"})
    assert args_hash(None) == args_hash({})
