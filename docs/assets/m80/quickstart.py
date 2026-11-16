"""Executed M80 quickstart — the literal recipe the help guide publishes.

Runs against a live fin_tracker stack and prints a transcript of every request
and response, so the guide is verifiable rather than asserted:

  1. mint a read-only token
  2. connect a real MCP client (streamable-http) to <origin>/mcp/mcp
  3. tools/list  -> read-only toolset, annotated, markers present
  4. a write call -> 403 mcp_read_only, no backend call
  5. GET /mcp-audit -> one receipt per call, refusal included
  6. revoke the token

Usage: uv run python quickstart.py <origin> [email] [password]
Tokens and passwords are redacted in the transcript (it gets committed).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
import uuid

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from mcp_server.policy import is_write_tool

ORIGIN = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:18080"
EMAIL = sys.argv[2] if len(sys.argv) > 2 else f"m80-quickstart-{uuid.uuid4().hex[:8]}@example.com"
PASSWORD = sys.argv[3] if len(sys.argv) > 3 else "quickstart-m80-pass"

LINES: list[str] = []


def say(line: str = "") -> None:
    LINES.append(line)
    print(line)


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def redact(text: str) -> str:
    """Never let a live credential reach the committed transcript."""
    for value in (PASSWORD, TOKENS.get("mcp", ""), TOKENS.get("access", "")):
        if value:
            text = text.replace(value, "<redacted>")
    return text


SECRET_KEYS = {"token", "access_token", "refresh_token", "password", "current_password"}


def scrub(value: object) -> object:
    """Blank credential fields by key — the transcript gets committed."""
    if isinstance(value, dict):
        return {
            key: ("<redacted>" if key in SECRET_KEYS else scrub(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def show(label: str, payload: object, status: int | None = None) -> None:
    prefix = f"< {status} " if status is not None else "< "
    say(f"{prefix}{label} {redact(json.dumps(scrub(payload), sort_keys=True))}")


TOKENS: dict[str, str] = {}
WRITE_TOOL = "accounts_create"


def rest(client: httpx.Client, method: str, path: str, **kwargs) -> httpx.Response:
    body = kwargs.pop("json", None)
    say(f"$ {method} {path}" + (f"  {redact(json.dumps(body))}" if body else ""))
    response = client.request(method, path, json=body, **kwargs)
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    show("", payload if payload != "" else "(empty)", response.status_code)
    return response


async def mcp_part(token: str) -> list[str]:
    """Connect with the read-only token: list the toolset, then try to write."""
    url = f"{ORIGIN}/mcp/mcp"
    say(f"\n--- MCP client → {url} (streamable-http, Bearer <redacted>) ---")
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                say("initialize -> OK (protocol negotiated, session id issued)")

                tools = (await session.list_tools()).tools
                names = [t.name for t in tools]
                say(f"\n$ tools/list -> {len(tools)} tools")
                marked = [
                    t.name for t in tools
                    if t.annotations and getattr(t.annotations, "title", None)
                ]
                say(f"  every tool carries a title marker: {len(marked)}/{len(tools)}")
                sample = next(t for t in tools if t.annotations)
                say(
                    "  sample annotations: "
                    + json.dumps(
                        {"name": sample.name, **sample.annotations.model_dump(exclude_none=True)},
                        sort_keys=True,
                    )
                )
                leaked = sorted(name for name in names if is_write_tool(name))
                say(f"  mutating tools in the list: {leaked or 'none'}")
                assert len(tools) == 52, f"expected the read-only toolset, got {len(tools)}"
                assert not leaked, leaked

                say("\n$ tools/call health_check  (the read side: this one is allowed)")
                ok = await session.call_tool("health_check", {})
                say(f"< isError={ok.is_error}  {redact(ok.content[0].text)}")
                assert not ok.is_error, ok.content[0].text

                say(f"\n$ tools/call {WRITE_TOOL}  (a read-only token trying to write)")
                refused = await session.call_tool(
                    WRITE_TOOL, {"name": "Brokerage", "type": "tfsa", "currency": "CAD"}
                )
                text = refused.content[0].text
                say(f"< isError={refused.is_error}  {redact(text)}")
                envelope = json.loads(text)
                assert refused.is_error and envelope["error"] == 403
                assert envelope["detail"]["code"] == "mcp_read_only"
                return names


def main() -> int:
    say(f"# M80 executed quickstart — {stamp()}")
    say(f"# origin: {ORIGIN}")
    say(f"# account: {EMAIL} (created for this run, removed at the end)")
    say()

    with httpx.Client(base_url=ORIGIN, timeout=30) as client:
        # 1. register (or log in if this run is re-tried)
        response = rest(client, "POST", "/api/v1/auth/register",
                        json={"email": EMAIL, "password": PASSWORD})
        if response.status_code == 409:
            response = rest(client, "POST", "/api/v1/auth/login",
                            json={"email": EMAIL, "password": PASSWORD})
        assert response.status_code in (200, 201), response.text
        TOKENS["access"] = response.json()["access_token"]
        auth = {"Authorization": f"Bearer {TOKENS['access']}"}

        # 2. mint a read-only token
        say()
        minted = rest(client, "POST", "/api/v1/auth/mcp-tokens",
                      json={"name": "quickstart-readonly", "policy": "read_only"}, headers=auth)
        assert minted.status_code == 201, minted.text
        TOKENS["mcp"] = minted.json()["token"]
        say("# token shown once above -> <redacted>")

        # 3-4. connect and exercise the toolset
        tools = asyncio.run(mcp_part(TOKENS["mcp"]))

        # 5. the receipts
        say("\n--- receipts ---")
        audit = rest(client, "GET", "/api/v1/mcp-audit?limit=50", headers=auth)
        assert audit.status_code == 200, audit.text
        rows = audit.json()["items"]
        say(f"# {audit.json()['total']} rows, newest first")
        for row in rows:
            say(f"#   {row['created_at']}  {row['tool']:<20} {row['response_code']}  {row['duration_ms']}ms  token={row['mcp_token_id']}")
        assert any(row["response_code"] == 403 for row in rows), "refusal must be recorded"
        # One receipt per tools/call: the allowed read AND the refusal.
        assert len(rows) == 2, rows
        assert {row["response_code"] for row in rows} == {200, 403}, rows

        # 6. revoke
        say()
        token_id = minted.json()["id"]
        revoked = rest(client, "DELETE", f"/api/v1/auth/mcp-tokens/{token_id}", headers=auth)
        assert revoked.status_code == 204, revoked.text
        # The dead token no longer opens the PUBLIC path. (validate-mcp-token
        # is an internal endpoint — nginx denies it at the origin, M9.)
        after = rest(client, "POST", "/mcp/mcp",
                     json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                     headers={"Authorization": f"Bearer {TOKENS['mcp']}",
                              "Accept": "application/json, text/event-stream"})
        assert after.status_code == 401, after.text
        say("# revoked token -> /mcp/mcp 401, the door stays shut")

        # cleanup: this run's account leaves nothing behind
        say()
        gone = rest(client, "DELETE", "/api/v1/users/me",
                    json={"current_password": PASSWORD}, headers=auth)
        say(f"# account removed: {gone.status_code} (receipts cascade with it)")
    say(f"\n# done — {stamp()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
