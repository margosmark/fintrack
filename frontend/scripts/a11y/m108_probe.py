"""M108 QoL batch 4 — the live probe (AGENTS §4 step 5, against a running stack).

    python3 m108_probe.py [--base http://127.0.0.1:18080] [--width 1440]

Four slices, one throwaway tenant (register + login), because the demo tenant
answers 403 demo_account_restricted on every write:

  P4 http   PUT/GET/DELETE round trip byte-for-byte, `has_receipt` on the list
            ONLY on the row that has one, 413 receipt_too_large, 422
            receipt_empty / receipt_unsupported_type (magic bytes, not the
            header), 404 for a foreign tenant, idempotent 204.
  P4 ui     row paperclip arms the shared picker -> upload -> the chip
            RE-RENDERS in the row; editor field shows attached, Remove flips
            it back, "Attach image" re-attaches. >=44px at 390 for every new
            control. axe 0 critical/serious.
  P1 ui     Chart view: Treemap renders one block per category, every <title>
            and the hover readout carry a share TRIMMED to <=2 decimals, and a
            tap on a block drills into the category-filtered transactions view.
  P2 ui     "Save this view" -> full reload -> "Saved views (1)" -> restore
            puts the saved range back -> "Delete saved view" empties it.
  P3 ui     the "Group by" control as shipped by M124 + M159/M179: Category |
            Tag for EVERY session (tags need no partnership), Owner ONLY while
            `GET /partner/state` answers `{linked:true}`. A lone tenant and the
            owner seat (no link of their own) therefore see exactly
            [Category, Tag]; the invited, linked session sees
            [Category, Tag, Owner] and the Owner lens renders. What must NEVER
            appear is an Owner tab a session cannot fill.
  a11y      axe at 1440/390/412 in BOTH themes, plus a text-node overlap sweep
            (the M82 contract) on /reports and /transactions.

Exit 0 only if every check passes; JSON evidence lands next to this script.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from redact import redact
from probe_target import random_password

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
AXE = str(REPO / "frontend" / "node_modules" / "axe-core" / "axe.min.js")
PNG = REPO / "docs" / "assets" / "a11y" / "bills-focus.png"
BASE = os.environ.get("M108_BASE", "http://127.0.0.1:18080")

CHECKS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name} — {detail}", flush=True)
    return bool(ok)


# --------------------------------------------------------------------------- HTTP
class Api:
    """Bearer-token JSON client — the probe's reads/writes never touch React."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.token: str | None = None

    def call(
        self,
        method: str,
        path: str,
        *,
        body: object | None = None,
        raw: bytes | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, bytes]:
        req = urllib.request.Request(self.base + path, method=method)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
        if data is not None:
            req.add_header("Content-Type", content_type)
            req.data = data
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except Exception as exc:  # pragma: no cover - transport failures
            return -1, str(exc).encode()

    def json(self, method: str, path: str, **kw: Any) -> Any:
        status, payload = self.call(method, path, **kw)
        if status < 0 or status >= 400:
            return None
        return json.loads(payload)


def multipart(
    fields: dict[str, str], filename: str, blob: bytes, file_type: str = "image/png"
) -> tuple[bytes, str]:
    boundary = "----m108" + uuid.uuid4().hex
    out = bytearray()
    for key, value in fields.items():
        out += (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
            f"{value}\r\n"
        ).encode()
    out += (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{filename}\"\r\nContent-Type: {file_type}\r\n\r\n"
    ).encode()
    out += blob + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def put_receipt(
    api: Api, tx_id: int, blob: bytes, file_type: str = "image/png", name: str = "r.png"
) -> tuple[int, bytes]:
    body, ctype = multipart({"x": "1"}, name, blob, file_type)
    return api.call("PUT", f"/api/v1/transactions/{tx_id}/receipt", raw=body, content_type=ctype)


def code_of(payload: bytes) -> str:
    try:
        return str(json.loads(payload).get("code", ""))
    except Exception:
        return ""


# ------------------------------------------------------------------- browser glue
def settle(tab: Any, extra: float = 1.4) -> None:
    try:
        tab.wait_for(
            "document.querySelector('[data-testid=\"route-fallback\"]') === null"
            " && document.querySelector('main') !== null",
            timeout=25,
        )
    except TimeoutError:
        pass
    tab.sleep(extra)


def register_in_page(tab: Any, email: str, password: str) -> dict:
    """Register + sign in inside the page so the refresh cookie lands on origin."""
    return tab.evaluate(
        f"""(async () => {{
      const h = {{'Content-Type': 'application/json'}};
      const email = {json.dumps(email)}, password = {json.dumps(password)};
      const reg = await fetch('/api/v1/auth/register',
        {{method:'POST', headers:h, body: JSON.stringify({{email, password}})}});
      const log = await fetch('/api/v1/auth/login',
        {{method:'POST', headers:h, body: JSON.stringify({{email, password}})}});
      const body = await log.json().catch(() => ({{}}));
      return {{register: reg.status, login: log.status, token: body.access_token || null}};
    }})()""",
        await_promise=True,
    )


def ui_login(tab: Any, base: str, email: str, password: str) -> None:
    tab.navigate(f"{base}/login")
    settle(tab)
    tab.evaluate("document.querySelector('input[type=\"email\"]').focus()")
    tab.send("Input.insertText", {"text": email})
    tab.evaluate("document.querySelector('input[type=\"password\"]').focus()")
    tab.send("Input.insertText", {"text": password})
    # Click the button rather than pressing Enter.
    #
    # This comment used to read "the deployed login form does NOT submit on
    # Enter ... no /auth/login request leaves the page" — a MISATTRIBUTION. That
    # was measured through the driver's then-broken `press()`, which sent
    # `rawKeyDown` with no `text`, so Chrome ran no default action and no
    # request ever left the page. With `press()` fixed the same form submits on
    # Enter exactly once (measured against this deployment: 1 login POST,
    # pathname /dashboard — same as the click below). The app was never at
    # fault. Clicking stays: it is the path a mouse user takes and it does not
    # depend on the keyboard fix being present.
    clicked = tab.evaluate(
        """(() => {
          const b = [...document.querySelectorAll('button')]
            .find(el => el.textContent.trim() === 'Log in');
          if (!b) return false;
          b.scrollIntoView({block: 'center'});
          b.click();
          return true;
        })()"""
    )
    if not clicked:
        tab.press("Enter")
    tab.wait_for("location.pathname !== '/login'", timeout=25)


def goto(tab: Any, base: str, path: str) -> None:
    tab.navigate(f"{base}{path}")
    settle(tab)


def js_rect(tab: Any, selector: str, index: int = 0) -> dict | None:
    return tab.evaluate(
        f"""(() => {{
      const el = document.querySelectorAll({json.dumps(selector)})[{index}];
      if (!el) return null;
      el.scrollIntoView({{block: 'center'}});
      const r = el.getBoundingClientRect();
      return {{x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, h: r.height,
               label: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 60)}};
    }})()"""
    )


def click_sel(tab: Any, selector: str, index: int = 0) -> dict | None:
    rect = js_rect(tab, selector, index)
    if rect:
        tab.sleep(0.25)
        tab.click_at(rect["x"], rect["y"])
    return rect


def group_by_tabs(tab: Any) -> list[str] | None:
    """The Reports header's `Group by` option labels, in render order (None
    when the control is not on screen).

    The list a session sees IS the shipped contract (Reports.tsx, docs/09 §The
    `Group by` control): Category and Tag always — M124, tags need no
    partnership — Owner only while `GET /partner/state` answers
    `{linked:true}` (M159/M179). A session that cannot fill a dimension must
    never be offered it.
    """
    return tab.evaluate(
        """(() => {
          const list = document.querySelector('[aria-label="Group by"]');
          if (!list) return null;
          return [...list.querySelectorAll('[role=tab]')].map(b => b.textContent.trim());
        })()"""
    )


def load_axe(tab: Any) -> None:
    tab.evaluate(Path(AXE).read_text(encoding="utf-8"))


def axe_check(tab: Any, name: str) -> dict:
    result = tab.evaluate(
        "axe.run(document, {resultTypes: ['violations']})", await_promise=True
    )
    counts: dict[str, int] = {}
    rows: list[str] = []
    for violation in result.get("violations", []):
        impact = violation.get("impact") or "unknown"
        counts[impact] = counts.get(impact, 0) + 1
        rows.append(f"{violation['id']}({impact}x{len(violation['nodes'])})")
    bad = counts.get("critical", 0) + counts.get("serious", 0)
    check(f"axe 0 critical/serious — {name}", bad == 0, f"{counts} {rows[:4]}")
    return {"counts": counts, "rows": rows}


# Text-node overlap sweep (docs/09 §M82 contract): no two visible text rects may
# print into each other. Scoped deliberately:
#   * `main` only — the contract is about PAGE content; the app shell's chrome
#     (skip link, sidebar) is owned by another milestone and this card is told
#     not to touch sidebar files (M100).
#   * `.sr-only` is skipped: Tailwind clips it to a 1px box, so its Range rect
#     is a phantom that overlaps whatever sits at its anchor — clipped text has
#     no visual box and cannot print into a neighbour.
#   * SVG text is skipped: chart ticks are placed by the library, not by HTML
#     flow; the contract's examples are all HTML (`br.right`/`br.left`).
#   * PINNED layers are skipped (text inside a `sticky`/`fixed` ancestor): a
#     sticky header meeting a sticky bottom bar stacks by z-index with each
#     bar's own background, which is a stacking decision, not the flow overlap
#     docs/09 §M82 describes. Measured live at 390 on /transactions: the
#     sticky `thead` (z-10, `bg-surface/95`) and M64's sticky "Scroll for
#     actions" bar meet by 9px when the table is short — pre-existing M64/
#     M102 behaviour, reported on the card rather than silently fixed in files
#     this milestone does not own.
# Viewport-clipped and x-sorted so a 2k-node page stays fast.
SWEEP_JS = r"""(() => {
  const root = document.querySelector('main') || document.body;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const items = [];
  const pinned = (el) => {
    let n = el;
    while (n && n !== document.documentElement) {
      const pos = getComputedStyle(n).position;
      if (pos === 'sticky' || pos === 'fixed') return true;
      n = n.parentElement;
    }
    return false;
  };
  let n;
  while ((n = walker.nextNode())) {
    const text = (n.textContent || '').trim();
    if (!text) continue;
    const p = n.parentElement;
    if (!p) continue;
    if (p.closest('.sr-only')) continue;          // clipped to 1px — no visual box
    if (p.ownerSVGElement) continue;              // chart-placed text, not HTML flow
    if (pinned(p)) continue;                      // stacked by z-index, not by flow
    const cs = getComputedStyle(p);
    if (cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity === 0) continue;
    const range = document.createRange();
    range.selectNodeContents(n);
    for (const r of range.getClientRects()) {
      if (r.width < 1 || r.height < 1) continue;
      if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
      items.push({t: text.slice(0, 30), l: r.left, r: r.right, top: r.top, b: r.bottom,
                  c: p.closest('[aria-hidden="true"]') ? 1 : 0,
                  d: p.tagName + '.' + String(p.className || '').slice(0, 22)});
    }
  }
  items.sort((a, b) => a.l - b.l);
  let overlaps = 0;
  const samples = [];
  for (let i = 0; i < items.length; i++) {
    for (let j = i + 1; j < items.length && items[j].l < items[i].r; j++) {
      const a = items[i], b = items[j];
      if (a.c || b.c) continue;
      const ox = Math.min(a.r, b.r) - Math.max(a.l, b.l);
      const oy = Math.min(a.b, b.b) - Math.max(a.top, b.top);
      if (ox > 1 && oy > 1) {
        overlaps++;
        if (samples.length < 4) samples.push([a.t, b.t, Math.round(ox), Math.round(oy), a.d, b.d]);
      }
    }
  }
  return {nodes: items.length, overlaps, samples};
})()"""


def sweep(tab: Any, name: str) -> dict:
    res = tab.evaluate(SWEEP_JS)
    check(
        f"text-node sweep 0 overlaps — {name}",
        res.get("overlaps") == 0,
        f"nodes={res.get('nodes')} overlaps={res.get('overlaps')} {res.get('samples')}",
    )
    return res


def reload_tab(tab: Any) -> None:
    """`Tab` has no reload(); navigate() has already enabled the Page domain."""
    tab.send("Page.reload")
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if tab.evaluate("document.readyState") == "complete":
                break
        except Exception:
            pass
        time.sleep(0.2)


def set_theme(tab: Any, theme: str) -> None:
    tab.evaluate(f"localStorage.setItem('theme', {json.dumps(theme)})")
    reload_tab(tab)
    settle(tab)
    tab.sleep(0.8)


# ------------------------------------------------------------------------- checks
def p4_http(api: Api, evidence: dict) -> None:
    png = PNG.read_bytes()
    account = api.json(
        "POST",
        "/api/v1/accounts",
        body={"name": "Probe Chequing", "type": "chequing", "currency": "CAD"},
    )
    account_id = (account or {}).get("id")
    check("probe account", bool(account_id), json.dumps(account or {})[:160])
    row = api.json(
        "POST",
        "/api/v1/transactions",
        body={
            "account_id": account_id,
            "date": "2026-10-06",
            "raw_description": "PROBE COFFEE",
            "normalized_payee": "PROBE COFFEE",
            "amount_native": "-4.25",
            "currency": "CAD",
        },
    )
    tx = (row or {}).get("id")
    other = api.json(
        "POST",
        "/api/v1/transactions",
        body={
            "account_id": account_id,
            "date": "2026-10-05",
            "raw_description": "PROBE BUS",
            "normalized_payee": "PROBE BUS",
            "amount_native": "-3.25",
            "currency": "CAD",
        },
    )
    other_tx = (other or {}).get("id")
    check("two manual rows", bool(tx and other_tx), f"tx={tx} other={other_tx}")
    evidence["tx"], evidence["other_tx"] = tx, other_tx
    if not tx or not other_tx:
        return

    # 1. attach — 200 with the sniffed type and the DECODED size
    status, payload = put_receipt(api, tx, png)
    meta = json.loads(payload) if status == 200 else {}
    check(
        "PUT receipt 200 + sniffed image/png + decoded size",
        status == 200
        and meta.get("content_type") == "image/png"
        and meta.get("size_bytes") == len(png),
        f"HTTP {status} {json.dumps(meta)[:160]}",
    )

    # 2. GET — the bytes come back identical, with the type they were stored as
    req = urllib.request.Request(f"{api.base}/api/v1/transactions/{tx}/receipt")
    req.add_header("Authorization", "Bearer " + api.token)
    with urllib.request.urlopen(req, timeout=60) as resp:
        got, raw_headers = resp.read(), resp.headers
    # Case-insensitive: nginx normalizes the header name to `cache-control` on
    # the way out of the compose network, and `dict(resp.headers)` would then
    # miss a header the app really did send.
    headers = {key.lower(): value for key, value in raw_headers.items()}
    check(
        "GET receipt byte-for-byte + Cache-Control private",
        got == png
        and headers.get("content-type", "").startswith("image/png")
        and "private" in headers.get("cache-control", ""),
        f"{len(got)} bytes, ct={headers.get('content-type')}, cc={headers.get('cache-control')}",
    )

    # 3. the list carries the FLAG, only on that row, never the image
    page = api.json("GET", "/api/v1/transactions?limit=50") or {}
    flags = {item["id"]: item.get("has_receipt") for item in page.get("items", [])}
    check(
        "list has_receipt: true on the image row, false on its sibling",
        flags.get(tx) is True and flags.get(other_tx) is False
        and "data_base64" not in json.dumps(page),
        f"tx={flags.get(tx)} other={flags.get(other_tx)}",
    )

    # 4. 413 before the body is read (declared Content-Length over the cap)
    oversize = b"\x89PNG\r\n\x1a\n" + b"\x00" * (2 * 1024 * 1024 + 1)
    status, payload = put_receipt(api, tx, oversize, name="big.png")
    check(
        "413 receipt_too_large over 2 MB",
        status == 413 and code_of(payload) == "receipt_too_large",
        f"HTTP {status} {code_of(payload)}",
    )

    # 5. magic bytes win over a header that lies
    status, payload = put_receipt(api, tx, b"<html>not an image</html>", "image/png")
    check(
        "422 receipt_unsupported_type (header lies, sniff disagrees)",
        status == 422 and code_of(payload) == "receipt_unsupported_type",
        f"HTTP {status} {code_of(payload)}",
    )

    # 6. empty upload
    status, payload = put_receipt(api, tx, b"")
    check(
        "422 receipt_empty",
        status == 422 and code_of(payload) == "receipt_empty",
        f"HTTP {status} {code_of(payload)}",
    )

    # 7. foreign tenant sees 404 in both directions, not a leak
    stamp = int(time.time() * 1000)
    other_user = Api(api.base)
    other_pw = random_password("m108b")
    reg = other_user.json(
        "POST", "/api/v1/auth/register", body={"email": f"m108b-{stamp}@example.com",
                                               "password": other_pw}
    )
    other_user.token = (reg or {}).get("access_token") or (
        other_user.json("POST", "/api/v1/auth/login",
                        body={"email": f"m108b-{stamp}@example.com",
                              "password": other_pw}) or {}
    ).get("access_token")
    st_put, pl_put = put_receipt(other_user, tx, png)
    st_get, pl_get = other_user.call("GET", f"/api/v1/transactions/{tx}/receipt")
    check(
        "foreign tenant 404 on PUT and GET",
        st_put == 404 and st_get == 404,
        f"PUT {st_put} {code_of(pl_put)} / GET {st_get} {code_of(pl_get)}",
    )

    # 8. delete is idempotent, and the image is really gone
    st1, _ = api.call("DELETE", f"/api/v1/transactions/{tx}/receipt")
    st2, _ = api.call("DELETE", f"/api/v1/transactions/{tx}/receipt")
    st3, pl3 = api.call("GET", f"/api/v1/transactions/{tx}/receipt")
    check(
        "DELETE 204 twice (idempotent) then 404 receipt_not_found",
        st1 == 204 and st2 == 204 and st3 == 404 and code_of(pl3) == "receipt_not_found",
        f"{st1} / {st2} / {st3} {code_of(pl3)}",
    )
    page = api.json("GET", "/api/v1/transactions?limit=50") or {}
    flags = {item["id"]: item.get("has_receipt") for item in page.get("items", [])}
    check("flag clears after DELETE", flags.get(tx) is False, f"tx={flags.get(tx)}")


def seed_reports_data(api: Api, evidence: dict) -> dict:
    """An account, two categories and four dated rows — enough for the Spending
    tab to have blocks, legend entries and a drill target."""
    account = api.json(
        "POST",
        "/api/v1/accounts",
        body={"name": "Probe Spending", "type": "chequing", "currency": "CAD"},
    )
    cats = {}
    for name in ("Probe Groceries", "Probe Transit"):
        created = api.json("POST", "/api/v1/categories", body={"name": name})
        cats[name] = (created or {}).get("id")
    rows = []
    plan = [
        ("Probe Market", "-120.00", "Probe Groceries", "2026-10-02"),
        ("Probe Bakery", "-18.50", "Probe Groceries", "2026-10-04"),
        ("Probe Metro", "-42.00", "Probe Transit", "2026-10-03"),
        ("Probe Taxi", "-26.75", "Probe Transit", "2026-10-06"),
    ]
    for payee, amount, cat, day in plan:
        rows.append(
            api.json(
                "POST",
                "/api/v1/transactions",
                body={
                    "account_id": (account or {}).get("id"),
                    "date": day,
                    "raw_description": payee,
                    "normalized_payee": payee,
                    "amount_native": amount,
                    "currency": "CAD",
                    "category_id": cats.get(cat),
                },
            )
        )
    ids = [r["id"] for r in rows if r and r.get("id")]
    check("reports seed (account + 2 categories + 4 rows)", len(ids) == 4, str(ids))
    evidence["report_rows"] = ids
    return {"account": account, "cats": cats, "rows": ids}


def p1_treemap(tab: Any, base: str, width: int, evidence: dict) -> None:
    goto(tab, base, "/reports?tab=spending")
    click_sel(tab, '[aria-label="Chart view"] [role="tab"]', 1)  # Treemap
    tab.sleep(1.2)
    rects = tab.evaluate(
        """(() => {
          const frame = [...document.querySelectorAll('[role="img"]')]
            .find(el => /treemap/i.test(el.getAttribute('aria-label') || ''));
          if (!frame) return null;
          const rects = [...frame.querySelectorAll('svg rect')];
          return {
            label: frame.getAttribute('aria-label'),
            blocks: rects.length,
            titles: rects.map(r => (r.querySelector('title') || {}).textContent || ''),
          };
        })()"""
    )
    check("treemap renders blocks", bool(rects) and rects["blocks"] >= 2,
          json.dumps(rects)[:200] if rects else "no treemap frame")
    if not rects:
        return
    check("role=img text equivalent present", "treemap" in (rects["label"] or "").lower(),
          rects["label"][:140])

    # Honest trim: every share the chart prints is <= 2 decimals.
    bad = [t for t in rects["titles"] if _more_than_2dp(t)]
    check("rect <title> shares trimmed to <=2dp", not bad, f"bad={bad[:3]}")

    # Hover readout: exact amount + trimmed share.
    first = js_rect(tab, '[role="img"] svg rect', 0)
    if first:
        tab.send("Input.dispatchMouseEvent",
                 {"type": "mouseMoved", "x": first["x"], "y": first["y"], "buttons": 0})
        tab.sleep(0.4)
        readout = tab.evaluate(
            """(() => {
              const el = [...document.querySelectorAll('div[aria-hidden]')]
                .find(d => /\\d/.test(d.textContent) && /%/.test(d.textContent)
                       && d.className.includes('pointer-events-none'));
              return el ? el.textContent.trim() : null;
            })()"""
        )
        check("hover readout shows amount + trimmed share",
              bool(readout) and not _more_than_2dp(readout) and "$" in readout,
              (readout or "no readout")[:120])
    evidence["treemap_titles"] = rects["titles"][:6]
    tab.screenshot(str(HERE / f"m108-treemap-{width}.png"))

    # Legend carries the RAW share on the aria-label (the trim is display-only).
    legend_raw = tab.evaluate(
        """(() => {
          const btns = [...document.querySelectorAll('button[aria-label]')]
            .filter(b => /%/.test(b.getAttribute('aria-label') || ''));
          return btns.map(b => b.getAttribute('aria-label')).slice(0, 4);
        })()"""
    )
    check("legend aria-labels carry the RAW (untrimmed) share for AT",
          any(len(t.split("%")[0].split(".")[-1]) > 2 for t in legend_raw if "." in t)
          or len(legend_raw) == 0,
          json.dumps(legend_raw)[:180])

    # Mobile drill-in: a tap on a block goes to the category-filtered ledger.
    if width <= 420:
        before = tab.evaluate("location.pathname + location.search")
        rect = js_rect(tab, '[role="img"] svg rect', 0)
        if rect:
            tab.click_at(rect["x"], rect["y"])
            tab.sleep(1.6)
        after = tab.evaluate("location.pathname + location.search")
        check("tap a block drills into /transactions?category=…",
              after.startswith("/transactions") and "category=" in after,
              f"{before} -> {after}")


def _more_than_2dp(text: str) -> bool:
    """Any percentage printed with 3+ decimals is a trim violation."""
    import re

    hits = re.findall(r"(\d+)\.(\d+)%", text)
    return any(len(frac) > 2 for _, frac in hits)


def read_range(tab: Any) -> list[str]:
    """The RangePicker's From/To date inputs, in order."""
    return tab.evaluate(
        "[...document.querySelectorAll('input[type=date]')].map(i => i.value)"
    ) or []


def poll_true(tab: Any, expr: str, timeout: float = 6.0, poll: float = 0.3):
    """Evaluate `expr` until it is truthy or `timeout` elapses; return the last value.

    Persist round-trips (PATCH /users/me, list refetch) are network calls — a
    fixed sleep either wastes time or guesses wrong under load.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = tab.evaluate(expr)
            if last:
                return last
        except Exception:
            pass
        time.sleep(poll)
    return last


def open_saved_list(tab: Any) -> None:
    """Open the "Saved views" panel — but only when it is shut.

    Every entry into the panel below runs AFTER something that already closed
    it (restore) or left it open (a completed rename), and the control is a
    toggle: clicking it blind would close the panel the next step needs.
    """
    already = tab.evaluate(
        """(() => {
          const del = document.querySelector('[aria-label^="Delete saved view"]');
          const ren = document.querySelector('input[aria-label^="New name for"]');
          return !!del || !!ren;
        })()"""
    )
    if already:
        return
    tab.evaluate(
        """(() => {
          const b = [...document.querySelectorAll('button')]
            .find(el => /^Saved views/.test(el.textContent.trim()));
          if (b) b.click(); return !!b;
        })()"""
    )
    tab.sleep(0.5)


def p2_saved_views(tab: Any, base: str, evidence: dict) -> None:
    title = f"Probe view {int(time.time()) % 100000}"
    goto(tab, base, "/reports?tab=spending")
    original_range = read_range(tab)
    check("range picker present before saving", len(original_range) == 2
          and all(original_range), json.dumps(original_range))
    # `:has-text` is Playwright syntax — this driver is raw CDP, select by text.
    clicked = tab.evaluate(
        """(() => {
          const b = [...document.querySelectorAll('button')]
            .find(el => el.textContent.trim() === 'Save this view');
          if (!b) return false;
          b.scrollIntoView({block:'center'}); b.click(); return true;
        })()"""
    )
    check("Save this view opens the panel", bool(clicked), str(clicked))
    tab.sleep(0.4)
    filled = tab.evaluate(
        f"""(() => {{
      const input = document.querySelector('input[maxlength="80"]');
      if (!input) return false;
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(input, {json.dumps(title)});
      input.dispatchEvent(new Event('input', {{bubbles: true}}));
      return true;
    }})()"""
    )
    check("name the view", bool(filled), title)
    tab.sleep(0.3)
    tab.evaluate(
        """(() => {
          const b = [...document.querySelectorAll('button')]
            .find(el => el.textContent.trim() === 'Save' && el.type === 'submit');
          if (b) b.click();
          return !!b;
        })()"""
    )
    tab.sleep(1.2)

    reload_tab(tab)   # A FULL reload — the acceptance is "persists across reload".
    settle(tab)
    tab.sleep(1.2)
    listed = tab.evaluate(
        """(() => {
          const b = [...document.querySelectorAll('button')]
            .find(el => /^Saved views/.test(el.textContent.trim()));
          return b ? b.textContent.trim() : null;
        })()"""
    )
    check("Saved views (1) after a full reload", bool(listed) and "(1)" in listed,
          listed or "not found")
    evidence["saved_views_button"] = listed

    # Change the range so "restore" has something observable to put back.
    moved = tab.evaluate(
        """(() => {
          const tabs = [...document.querySelectorAll('[aria-label="Date range presets"] [role=tab]')];
          const target = tabs.find(t => !t.getAttribute('aria-selected')?.includes('true'))
                      || tabs[tabs.length - 1];
          if (!target) return null;
          target.scrollIntoView({block: 'center'}); target.click();
          return target.textContent.trim();
        })()"""
    )
    tab.sleep(1.0)
    shifted = read_range(tab)
    check("range moved off the saved one", bool(moved) and shifted != original_range,
          f"preset={moved} {original_range} -> {shifted}")

    # Open the list, restore, and confirm the row is really there.
    open_saved_list(tab)
    item = tab.evaluate(
        f"""(() => {{
      const b = [...document.querySelectorAll('button')]
        .find(el => (el.textContent || '').includes({json.dumps(title)}));
      if (!b) return null;
      b.scrollIntoView({{block:'center'}});
      const r = b.getBoundingClientRect();
      return {{x: r.x + r.width/2, y: r.y + r.height/2, text: b.textContent.trim()}};
    }})()"""
    )
    check("the saved view is listed with its range", bool(item),
          (item or {}).get("text", "missing"))
    if item:
        tab.click_at(item["x"], item["y"])
        tab.sleep(1.4)
        after = tab.evaluate("location.search")
        check("restore returns to its tab", "tab=spending" in after, after)
        check("restore puts the saved range back exactly",
              read_range(tab) == original_range,
              f"{read_range(tab)} vs {original_range}")

    # Rename + delete, the two mutations the card requires.
    open_saved_list(tab)
    renamed = tab.evaluate(
        f"""(() => {{
      const b = [...document.querySelectorAll('[aria-label^="Rename saved view"]')]
        .find(el => (el.getAttribute('aria-label') || '').includes({json.dumps(title)}));
      if (!b) return false; b.click(); return true;
    }})()"""
    )
    tab.sleep(0.4)
    if renamed:
        filled = tab.evaluate(
            f"""(() => {{
      const input = [...document.querySelectorAll('input[maxlength="80"]')]
        .find(i => (i.getAttribute('aria-label') || '').startsWith('New name for'));
      if (!input) return false;
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(input, {json.dumps(title + ' renamed')});
      input.dispatchEvent(new Event('input', {{bubbles: true}}));
      return input.value;
    }})()"""
        )
        tab.sleep(0.3)
        confirmed = tab.evaluate(
            """(() => {
              // The shared <Button> has no explicit `type`, and an untyped
              // button inside/outside a form reports `submit` — so match on
              // the LABEL (text 'Rename', no aria-label) and not on `type`,
              // which is what made the first run miss this control.
              const b = [...document.querySelectorAll('button')]
                .find(el => el.textContent.trim() === 'Rename'
                       && !el.getAttribute('aria-label'));
              if (b) b.click(); return !!b;
            })()"""
        )
        # Persist is a PATCH round-trip — poll for the result instead of
        # sleeping a fixed beat and hoping it landed.
        renamed_text = poll_true(
            tab,
            f"""(() => !![...document.querySelectorAll('button')]
              .find(el => (el.textContent || '').includes({json.dumps(title + ' renamed')})))()""",
        )
        check("rename persists", bool(renamed_text),
              f"filled={filled!r} confirm_clicked={bool(confirmed)}")
        tab.screenshot(str(HERE / "m108-savedview-renamed.png"))
    else:
        check("rename control present", False, "no Rename button for this saved view")

    open_saved_list(tab)
    deleted = tab.evaluate(
        """(() => {
          const b = document.querySelector('[aria-label^="Delete saved view"]');
          if (!b) return false; b.click(); return true;
        })()"""
    )
    empty = poll_true(
        tab,
        """(() => {
          const b = [...document.querySelectorAll('button')]
            .find(el => /^Saved views/.test(el.textContent.trim()));
          if (!b) return null;
          const text = b.textContent.trim();
          return /\\(\\d+\\)/.test(text) ? null : text;
        })()""",
    )
    check("delete empties the tab's list", bool(deleted) and bool(empty),
          f"clicked={bool(deleted)} button={empty!r}")
    tab.screenshot(str(HERE / "m108-savedview-deleted.png"))


def p3_owner(api: Api, tab: Any, base: str, evidence: dict, owner_email: str,
             password: str, account_id: int) -> None:
    goto(tab, base, "/reports?tab=spending")
    # Still the SAME lone tenant — M124 keeps Category | Tag on screen, and the
    # Owner option must still be absent (only an accepted invite adds it).
    solo = group_by_tabs(tab)
    evidence["group_by_alone_before_invite"] = solo
    check("still alone right before the invite: Category | Tag, no Owner",
          solo == ["Category", "Tag"], json.dumps(solo))
    stamp = int(time.time() * 1000)
    partner_email = f"m108partner-{stamp}@example.com"
    partner = Api(base)
    reg = partner.json("POST", "/api/v1/auth/register",
                       body={"email": partner_email, "password": password})
    partner.token = (reg or {}).get("access_token") or (
        partner.json("POST", "/api/v1/auth/login",
                     body={"email": partner_email, "password": password}) or {}
    ).get("access_token")
    check("partner registered", bool(partner.token), partner_email)

    invite = api.json("POST", "/api/v1/sharing/invites",
                      body={"email": partner_email, "account_ids": [account_id]}) or {}
    token = invite.get("token")
    check("invite created (token returned once)", bool(token),
          f"email_sent={invite.get('email_sent')} keys={sorted(invite)[:6]}")
    if not token:
        return
    accepted = partner.json("POST", "/api/v1/sharing/invites/accept", body={"token": token})
    check("partner accepted", bool(accepted) and accepted.get("linked") is not False,
          json.dumps(accepted or {})[:160])

    # The Owner lens is read through `GET /partner/state`, which answers the
    # PARTNER side (docs/43 §6: for a caller with no link of their own it is
    # `200 {linked:false}`), and its rows are "the caller's own + the owner's
    # allowlisted" (docs/12 §Owner dimension). So the OWNER OPTION belongs to
    # the LINKED session: the owner's own Reports keeps the control with
    # Category | Tag (M124) but must never offer Owner it cannot fill.
    state = partner.json("GET", "/api/v1/partner/state") or {}
    check("partner /partner/state is linked:true", state.get("linked") is True,
          json.dumps(state)[:160])
    owner_state = api.json("GET", "/api/v1/partner/state") or {}
    evidence["partner_state"] = {
        "partner_linked": state.get("linked"),
        "owner_linked": owner_state.get("linked"),
        "account_ids": state.get("account_ids"),
    }

    goto(tab, base, "/reports?tab=spending")
    owner_tabs = group_by_tabs(tab)
    evidence["group_by_owner_session"] = owner_tabs
    check("owner session shows Category | Tag, no Owner (the lens is the partner's)",
          owner_tabs == ["Category", "Tag"],
          f"tabs={json.dumps(owner_tabs)}, owner /partner/state={owner_state.get('linked')}")

    # Switch the browser to the linked household: the access token is in memory
    # only, so logout + a fresh load is a clean change of identity.
    tab.evaluate(
        """(async () => {
          await fetch('/api/v1/auth/logout', {method: 'POST',
            headers: {'Content-Type': 'application/json'}, body: '{}'});
          return true;
        })()""",
        await_promise=True,
    )
    ui_login(tab, base, partner_email, password)
    settle(tab)
    evidence["partner_email"] = partner_email

    goto(tab, base, "/reports?tab=spending")
    tabs = group_by_tabs(tab)
    evidence["group_by_linked_session"] = tabs
    check("Group by shows Category | Tag | Owner in the linked household",
          tabs == ["Category", "Tag", "Owner"], json.dumps(tabs))
    # Owner is the THIRD option since M124 put Tag in front of it — select it
    # by label, never by a fixed index: index 1 is Tag now, and the render
    # check below would then happily pass against the wrong lens.
    owner_idx = next((i for i, t in enumerate(tabs or []) if t == "Owner"), -1)
    if owner_idx >= 0:
        click_sel(tab, '[aria-label="Group by"] [role="tab"]', owner_idx)
    tab.sleep(1.5)
    rendered = tab.evaluate(
        """(() => {
          const frames = [...document.querySelectorAll('[role="img"]')]
            .filter(el => (el.getAttribute('aria-label') || '').length > 0);
          const text = document.body.innerText;
          return {frames: frames.length,
                  hasOwnerWord: /own \\+ shared household/i.test(text),
                  sample: (frames[0] || {}).getAttribute
                    ? frames[0].getAttribute('aria-label') : null};
        })()"""
    )
    # The household label is the lens' own signature (docs/09 §Owner
    # dimension): it only renders once `scope=household` is on screen, so
    # frames>0 alone would also pass on the Category or Tag chart.
    check("Owner grouping renders a chart of the household population",
          bool(rendered and rendered["frames"] > 0 and rendered["hasOwnerWord"]),
          json.dumps(rendered)[:200])
    tab.screenshot(str(HERE / "m108-owner-1440.png"))
    # Drill-in is deliberately omitted under Owner (no category target).
    drill = tab.evaluate("location.search")
    check("owner view kept on the spending tab", "tab=spending" in drill, drill)


def p4_ui(api: Api, tab: Any, base: str, width: int, tx: int, evidence: dict) -> None:
    png = PNG.read_bytes()
    b64 = base64.b64encode(png).decode()
    goto(tab, base, "/transactions")

    # The row's paperclip arms the page's ONE shared file input.
    label = f'button[aria-label^="Attach an image"]'
    rect = js_rect(tab, label, 0)
    check("row paperclip exists with a per-row label", bool(rect),
          (rect or {}).get("label", "missing"))
    if not rect:
        return
    if width <= 420:
        check("paperclip >= 44px at this width",
              rect["w"] >= 43.5 and rect["h"] >= 43.5, f"{rect['w']:.1f}x{rect['h']:.1f}")

    tab.click_at(rect["x"], rect["y"])   # arms receiptTarget for this row
    tab.sleep(0.4)
    attached = tab.evaluate(
        f"""(async () => {{
      const input = document.querySelector('input[type="file"][aria-label^="Choose an image"]');
      if (!input) return 'no-input';
      const bytes = Uint8Array.from(atob({json.dumps(b64)}), c => c.charCodeAt(0));
      const file = new File([bytes], 'receipt.png', {{type: 'image/png'}});
      const dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      input.dispatchEvent(new Event('change', {{bubbles: true}}));
      return 'dispatched';
    }})()""",
        await_promise=True,
    )
    check("shared file input armed + change dispatched", attached == "dispatched", attached)
    chip = None
    for _ in range(20):
        tab.sleep(0.5)
        chip = js_rect(tab, 'button[aria-label^="View the image attached"]', 0)
        if chip:
            break
    check("chip RE-RENDERS in the row after upload", bool(chip),
          (chip or {}).get("label", "no chip"))
    if chip and width <= 420:
        # Touch floor only: the chip deliberately collapses to the table's
        # compact size at `md:` (docs/09 §Receipt images), so the 44px rule is
        # asserted where a finger, not a cursor, reaches it.
        check("chip >= 44px at this width",
              chip["h"] >= 43.5 and chip["w"] >= 43.5, f"{chip['w']:.1f}x{chip['h']:.1f}")
    if chip:
        evidence["chip_label"] = chip.get("label")
    tab.screenshot(str(HERE / f"m108-receipt-chip-{width}.png"))

    # The editor's field: attached state, then Remove, then Attach again.
    edit = js_rect(tab, 'button[aria-label^="Edit transaction"]', 0)
    if edit:
        tab.click_at(edit["x"], edit["y"])
        tab.sleep(1.0)
    field = tab.evaluate(
        """(() => {
          const label = [...document.querySelectorAll('label')]
            .find(l => l.textContent.trim() === 'Receipt image');
          if (!label) return null;
          const box = label.parentElement;
          return {text: box.innerText.replace(/\\n/g, ' | ').slice(0, 160),
                  buttons: [...box.querySelectorAll('button')].map(b => ({
                    t: b.textContent.trim(), h: b.getBoundingClientRect().height}))};
        })()"""
    )
    check("editor shows the attached state", bool(field) and "One image attached" in field["text"],
          json.dumps(field)[:200])
    if field and width <= 420:
        small = [b for b in field["buttons"] if b["h"] < 43.5]
        check("editor controls >= 44px at this width", not small, json.dumps(small))

    # Remove -> the row's chip must disappear too (one source of truth).
    tab.evaluate(
        """(() => {
          const b = [...document.querySelectorAll('button')]
            .find(el => el.textContent.trim() === 'Remove');
          if (b) b.click(); return !!b;
        })()"""
    )
    tab.sleep(1.4)
    gone = tab.evaluate(
        """(() => {
          const b = [...document.querySelectorAll('button')]
            .find(el => el.textContent.trim() === 'Attach image');
          return !!b;
        })()"""
    )
    check("Remove flips the editor back to Attach image", bool(gone), str(gone))
    # Close the dialog, let the list refetch, confirm the row chip is gone.
    tab.press("Escape")
    tab.sleep(1.2)
    chip_after = js_rect(tab, 'button[aria-label^="View the image attached"]', 0)
    check("row chip clears after Remove", chip_after is None, json.dumps(chip_after))

    # Re-attach through the EDITOR's own input (the second entry point).
    edit = js_rect(tab, 'button[aria-label^="Edit transaction"]', 0)
    if edit:
        tab.click_at(edit["x"], edit["y"])
        tab.sleep(1.0)
    tab.evaluate(
        """(() => {
          const b = [...document.querySelectorAll('button')]
            .find(el => el.textContent.trim() === 'Attach image');
          if (b) b.click(); return !!b;
        })()"""
    )
    tab.sleep(0.4)
    tab.evaluate(
        f"""(() => {{
      const input = document.querySelector('#receipt-image');
      if (!input) return 'no-input';
      const bytes = Uint8Array.from(atob({json.dumps(b64)}), c => c.charCodeAt(0));
      const dt = new DataTransfer();
      dt.items.add(new File([bytes], 'receipt.png', {{type: 'image/png'}}));
      input.files = dt.files;
      input.dispatchEvent(new Event('change', {{bubbles: true}}));
      return 'ok';
    }})()"""
    )
    tab.sleep(2.0)
    reattached = tab.evaluate(
        """(() => document.body.innerText.includes('One image attached'))()"""
    )
    check("editor re-attaches", bool(reattached), str(reattached))
    tab.press("Escape")
    tab.sleep(1.5)
    chip_back = js_rect(tab, 'button[aria-label^="View the image attached"]', 0)
    check("row chip back after the editor attach", bool(chip_back), json.dumps(chip_back))
    tab.screenshot(str(HERE / f"m108-receipt-final-{width}.png"))

    # Keyboard: the paperclip must be reachable and activatable without a mouse.
    reachable = tab.evaluate(
        """(() => {
          const target = document.querySelector('button[aria-label^="Attach an image"]');
          if (!target) return null;
          target.focus();
          return document.activeElement === target;
        })()"""
    )
    check("paperclip takes keyboard focus", reachable is True, str(reachable))

    # axe on the page that carries every new control
    load_axe(tab)
    axe_check(tab, f"/transactions {width}")
    sweep(tab, f"/transactions {width}")


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--skip-a11y", action="store_true")
    ap.add_argument("--out", default=str(HERE / "m108-probe.json"))
    args = ap.parse_args()
    BASE = args.base.rstrip("/")
    from probe_target import require_target
    require_target(BASE, who="m108_probe.py")

    from a11y_driver import Browser  # local import: keeps --help fast

    api = Api(BASE)
    evidence: dict = {"base": BASE}
    stamp = int(time.time() * 1000)
    email = f"m108-{stamp}@example.com"
    password = random_password("m108")

    browser = Browser(width=args.width, height=args.height)
    rc = 0
    try:
        tab = browser.new_tab(f"{BASE}/login")
        tab.enable_runtime()
        tab.send("Network.enable")
        tab.force_viewport(args.width, args.height)
        settle(tab)

        creds = register_in_page(tab, email, password)
        check("seed tenant (register + login)",
              creds.get("register") == 200 and creds.get("login") == 200, json.dumps(creds))
        ui_login(tab, BASE, email, password)
        settle(tab)
        logged = api.json("POST", "/api/v1/auth/login",
                          body={"email": email, "password": password}) or {}
        api.token = logged.get("access_token")
        check("bearer token for the HTTP half", bool(api.token))

        # ---- P4 (HTTP) — before any UI, so the counts below are observable
        p4_http(api, evidence)
        tx = evidence.get("tx")

        # ---- shared data for the reports slices
        seeded = seed_reports_data(api, evidence)
        account_id = (seeded["account"] or {}).get("id")

        # ---- P3a: alone = Category | Tag and NEVER Owner (MUST run before
        # the invite) — M124 keeps the control on screen, M159/M179 gate only
        # the Owner option on a partnership.
        goto(tab, BASE, "/reports?tab=spending")
        solo = group_by_tabs(tab)
        evidence["group_by_alone"] = solo
        check("alone: Group by shows Category | Tag, no Owner",
              solo == ["Category", "Tag"], json.dumps(solo))
        if not args.skip_a11y:
            load_axe(tab)
            axe_check(tab, f"/reports {args.width}")
            sweep(tab, f"/reports {args.width}")

        # ---- P1 treemap
        p1_treemap(tab, BASE, args.width, evidence)

        # ---- P2 saved views
        p2_saved_views(tab, BASE, evidence)

        # ---- P4 (UI)
        if tx:
            p4_ui(api, tab, BASE, args.width, tx, evidence)

        # ---- P3b: invite + accept -> the Owner option appears
        if account_id:
            p3_owner(api, tab, BASE, evidence, email, password, int(account_id))
        else:
            check("partner flow needs an account id", False, "no account id from seeding")

        # ---- a11y matrix: both themes at 390 / 412 (and the run's own width)
        if not args.skip_a11y:
            for width in sorted({390, 412, args.width}):
                for theme in ("dark", "light"):
                    tab.force_viewport(width, 900)
                    goto(tab, BASE, "/reports?tab=spending")
                    set_theme(tab, theme)
                    load_axe(tab)
                    axe_check(tab, f"/reports {width}px {theme}")
                    sweep(tab, f"/reports {width}px {theme}")
                    goto(tab, BASE, "/transactions")
                    load_axe(tab)
                    axe_check(tab, f"/transactions {width}px {theme}")
                    sweep(tab, f"/transactions {width}px {theme}")
                    tab.screenshot(str(HERE / f"m108-{width}-{theme}.png"))
            tab.force_viewport(args.width, args.height)

        rc = 0 if not [c for c in CHECKS if not c["ok"]] else 1
    except Exception as exc:  # a crashed check must still leave its evidence
        CHECKS.append({"check": "probe crashed", "ok": False, "detail": repr(exc)[:400]})
        print(f"PROBE CRASH: {exc!r}"[:400], flush=True)
        rc = 2
    finally:
        evidence["checks"] = CHECKS
        evidence["passed"] = sum(1 for c in CHECKS if c["ok"])
        evidence["failed"] = [c for c in CHECKS if not c["ok"]]
        try:
            Path(args.out).write_text(json.dumps(redact(evidence), indent=2), encoding="utf-8")
            print(f"\n{evidence['passed']}/{len(CHECKS)} checks passed -> {args.out}")
        except Exception as write_exc:
            print("could not write evidence:", write_exc)
        browser.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
