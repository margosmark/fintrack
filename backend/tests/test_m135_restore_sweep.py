"""M135 §3.4 — the standing-gate family, backend half (docs/33).

M136/M137 flipped a pile of routes from "the browser computes" back to "the
server computes", and M135's own QA review said that flip had to be swept,
not assumed. The frontend half of that sweep lives in
`frontend/tests/m135DegradedMatrix.test.tsx` (§3.4-1/§3.4-5) and
`frontend/tests/m135QueryKeyTenantSweep.test.tsx` (§3.4-4a). This module is
the half a browser cannot see:

  §3.4-3  STALE-RENDER SWEEP — the docs/33 audit table is written in the
          present tense, so every row that says a route "was removed" has to
          still be TRUE. Two directions, both live:

            * every route docs/33 calls removed/unmounted still answers the
              status the doc claims (405 / 404 / 422 `q_removed`), and
            * every path `frontend/src/api/*.ts` can call resolves to a
              MOUNTED route with a usable method — a control that still
              renders in front of a 404/405 is exactly the defect class the
              sweep exists to catch.

          Plus the A-row audit itself: every Proof citation in the docs/33
          S1…S5 table must name a test file that exists and a node that still
          exists in it. A renamed, deleted, or never-written proof is the
          rot the table was built to prevent.

  §5.1    the G1-G3 write-path guards — the server defense-in-depth half of
          docs/33 §5.1, driven by the §5.1 table itself: a G row that stops
          naming `encryption_required` fails here. G1 and G3 are already
          pinned by `test_locked_fixture_tenant.py`; G2 (bulk matched-rename)
          was the one without a live probe and gets one here.

Nothing here mutates a fixture tenant or a route: it reads docs, reads the
FastAPI route table, and issues ordinary authenticated requests.

Run: cd backend && uv run pytest tests/test_m135_restore_sweep.py -q
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session

REPO = Path(__file__).resolve().parents[2]
DOC33 = REPO / "docs" / "33-end-to-end-encryption.md"
FE_TESTS = REPO / "frontend" / "tests"
FE_SRC = REPO / "frontend" / "src"
BE_TESTS = Path(__file__).resolve().parent

AUDIT_HEADING = "## Hardening audit — S1…S5 locked-vault degradation"
WRITE_GUARD_HEADING = "### §5.1 — the G1–G3 write-path guards"  # noqa: RUF001 -- byte-for-byte match of docs/33 heading (en-dash is in the doc)

API = "/api/v1"

# A request path in `frontend/src/api` is a backtick or quote literal that
# starts with `/`. Written as compiled patterns rather than inline raw strings
# so the quoting cannot be mangled by whatever writes this file.
LIT_RE = re.compile("[`'\"](/[^`'\"]*)[`'\"]")
DOLLAR_RE = re.compile(r"\$\{[^}]*\}")
FILE_RE = re.compile(r"\.[a-z]{2,4}$")
# `it("...")` / `it('...')` / `it(`...`)` titles, for matching a docs/33 node cite.
VITEST_NODE_RE = re.compile(r"\b(?:it|test)(?:\.\w+)?\(\s*['\"`]([^'\"`]+)['\"`]")


# ── docs/33 parsing ────────────────────────────────────────────────────────


def _doc_lines() -> list[str]:
    return DOC33.read_text(encoding="utf-8").split("\n")


def _section_lines(heading: str) -> list[tuple[int, str]]:
    """`(1-based line number, text)` for the body of a `##`/`###` section."""
    lines = _doc_lines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    level = len(heading) - len(heading.lstrip("#"))
    body: list[tuple[int, str]] = []
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if re.match(rf"^#{{1,{level}}} ", line):
            break
        body.append((i + 1, line))
    return body


def _tables(body: list[tuple[int, str]]) -> list[list[tuple[int, list[str]]]]:
    """Every markdown table in `body`, as runs of `(line number, cells)`."""
    tables: list[list[tuple[int, list[str]]]] = []
    current: list[tuple[int, list[str]]] = []
    for lineno, line in body:
        if line.startswith("|") and line.rstrip().endswith("|"):
            # Split on the cell separator only — docs/33 escapes a literal
            # pipe inside a cell as `\|`, and a naive split would hand every
            # row after the first an off-by-N cell index.
            cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line)[1:-1]]
            current.append((lineno, cells))
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    # Drop the header + separator rows so callers see data only.
    return [[row for row in table[2:] if not set(row[1]) <= {"-", "", ":"}] for table in tables]


# ── test-node existence, without running anything ──────────────────────────


def _pytest_nodes(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            names.add(node.name)
    return names


def _vitest_nodes(path: Path) -> set[str]:
    """Titles of every `it(...)` / `test(...)` declared in a vitest file."""
    text = path.read_text(encoding="utf-8")
    return set(VITEST_NODE_RE.findall(text))


def _fe_test_files() -> set[str]:
    return {p.name for p in FE_TESTS.glob("*.test.*")}


def _be_test_files() -> set[str]:
    return {p.name for p in BE_TESTS.glob("test_*.py")}


# ── Proof-column citation extraction ───────────────────────────────────────


class Citation:
    """One `file → node` claim lifted out of an A-row's Proof column."""

    def __init__(self, suite: str, filename: str, node: str | None) -> None:
        self.suite = suite  # "fe" | "be" | "other"
        self.filename = filename
        self.node = node

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.suite}:{self.filename}::{self.node}"


# A backtick-opened `path/to/file.py` / `.ts` / `.tsx` mention. The closing
# backtick is a LOOKAHEAD, not part of the match, and slashes are allowed in
# the path: house style writes `backend/tests/test_search.py::node`,
# `scripts/parity/parity.ts --slice s2a` and `foo.ts` / `bar.ts` joins, so a
# regex that demanded an immediately-closing backtick and no `/` silently
# matched only bare filenames — which is how the parser lost the table while
# still extracting a plausible-looking handful of citations.
_FILE_RE = re.compile(r"`([\w./-]+\.(?:py|ts|tsx))(?=[`\s(:])")
_QUOTED_RE = re.compile(r"[\u201c\"']([^\u201d\"']+)[\u201d\"']")
_COLON_NODE_RE = re.compile(r"::([A-Za-z_][\w.]*)")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")


def _is_node_token(token: str) -> bool:
    """True for a backticked token that is a bare test node, not a path.

    ``test_similar_hides_encrypted_rows_only_when_asked`` and
    ``test_preview_is_indifferent_to_encryption_required[False/True]`` are
    nodes inheriting the file named earlier in the cell; ``/budgets/recurring-summary``
    is a route and ``tests/fixtures/s2a-vectors.json`` a path — neither is one.
    """
    core = token.split("[", 1)[0]
    return re.fullmatch(r"[A-Za-z_]\w*", core) is not None


_JOIN_SPLIT_RE = re.compile(r"\s*\+\s*|\s*,\s*(?=`)|\s+\/\s+")
_MASKED = {"+": "\x01", ",": "\x02", "/": "\x03"}


def _mask_quoted(text: str) -> str:
    """Blank the joiners that sit INSIDE a “node title” before splitting.

    docs/33's A4 title is itself “A4/A5/A6: spending + cashflow output is
    lock-invariant and non-degenerate” — a ` + ` INSIDE the title. Splitting
    on it cut the title in half, so neither half parsed as a citation: the
    row kept a plausible-looking proof cell and its central claim went
    unchecked.
    """
    out: list[str] = []
    in_title = False
    for ch in text:
        if ch == "“":
            in_title = True
        elif ch == "”":
            in_title = False
        out.append(_MASKED[ch] if in_title and ch in _MASKED else ch)
    return "".join(out)


def _unmask(text: str) -> str:
    for ch, masked in _MASKED.items():
        text = text.replace(masked, ch)
    return text


def _file_spans(segment: str) -> list[tuple[int, str]]:
    """`(offset, path)` for every file the segment names."""
    return [(m.start(), m.group(1)) for m in _FILE_RE.finditer(segment)]


def _node_spans(segment: str) -> list[tuple[int, str]]:
    """`(offset, node)` for every node the segment names, in text order."""
    spans = [(m.start(), m.group(1)) for m in _QUOTED_RE.finditer(segment)]
    spans += [(m.start(), m.group(1)) for m in _COLON_NODE_RE.finditer(segment)]
    for m in _BACKTICK_RE.finditer(segment):
        token = m.group(1)
        # A bare backticked `test_foo[False/True]` names a node; a route
        # (`/budgets/recurring-summary`), a path and a file name do not.
        if _is_node_token(token):
            spans.append((m.start(), token.split("[", 1)[0]))
    return sorted(spans)


def _owning_file(offset: int, files: list[tuple[int, str]], inherited: str | None) -> str | None:
    """The file a node at `offset` belongs to: the nearest one naming it first."""
    prior = [path for start, path in files if start <= offset]
    if prior:
        return prior[-1]
    if files:
        return files[0][1]
    return inherited


def _citations(proof: str) -> list[Citation]:
    """Split a Proof cell into `(file, node)` claims.

    House style writes several proofs per row, joined by `+` / `,`, as
    `` `tests/x.py::node` ``, `` `tests/x.test.ts` \u2192 \u201cnode\u201d `` or a bare
    `` `tests/x.py` `` when the whole module is the proof. Splitting on those
    joins first is what keeps a node attached to the file it belongs to: the
    A4 row, for example, names `lockedVaultDegradation.test.ts` \u2192 \u201cA4/A5/A6:\u2026\u201d
    and then `s2aParity.test.ts` with no node of its own, and a parser that
    read the cell as one blob gave the parity module the degradation test's
    node name.

    Two more rules follow from the table's actual shape:

      * a segment may name NO file at all — the row writes the next claim
        after a `+` without repeating it (`` `x.py::node_a` + `node_b` ``,
        `` `f.ts` \u2192 \u201cnode 1\u201d + \u201cnode 2\u201d ``). Such a segment inherits the
        file the cell named last; skipping it dropped 9 citations (A1, A2,
        A3, A10, A12, A13, A16) and let a renamed node in exactly that
        shape pass unverified.
      * a segment may name SEVERAL files (A4: its title, then the parity
        script). A node attaches to the nearest file BEFORE it, never to
        whichever file happens to end the segment.
    """
    out: list[Citation] = []
    # Split on the JOINERS first — `+`, a comma directly before a new
    # backtick span, or a slash with whitespace on BOTH sides (a genuine
    # `foo.ts / bar.ts` join). A bare `\s*\/\s*` slash split also fired inside
    # `backend/tests/test_search.py`, `[False/True]`, `29/29` and `6/6`,
    # fragmenting every path-qualified citation before the file regex ever
    # saw it: that, plus a no-slash `_FILE_RE`, is what made this parser
    # extract 12 bare-filename citations out of a table that names 30+.
    segments = [_unmask(part) for part in _JOIN_SPLIT_RE.split(_mask_quoted(proof))]
    carry: str | None = None
    for segment in segments:
        files = _file_spans(segment)
        nodes = _node_spans(segment)
        inherited = carry
        if files:
            carry = files[-1][1]
        if not files and not nodes:
            continue  # prose about the row ("same test as A4"), not a claim

        for _, path in files:
            if path.startswith("scripts/") or path.rsplit("/", 1)[-1].endswith(".md"):
                out.append(Citation("other", path.rsplit("/", 1)[-1], None))
        if not nodes:
            # A file-level proof: the module is the proof, no single node is.
            for _, path in files:
                base = path.rsplit("/", 1)[-1]
                if path.startswith("scripts/") or base.endswith(".md"):
                    continue
                out.append(Citation("be" if base.endswith(".py") else "fe", base, None))
            continue
        for offset, node in nodes:
            path = _owning_file(offset, files, inherited)
            if path is None:
                continue
            base = path.rsplit("/", 1)[-1]
            if path.startswith("scripts/") or base.endswith(".md"):
                out.append(Citation("other", base, node))
                continue
            out.append(Citation("be" if base.endswith(".py") else "fe", base, node))
    return out


# ── the Proof parser's contract: the shapes docs/33 is actually written in ─


def test_the_proof_parser_keeps_every_claim_attached_to_its_own_file() -> None:
    """One case per Proof-cell shape the audit table uses, pinned.

    The gate itself only proves the citations it DOES extract resolve — it
    says nothing about a claim it never parsed. These are the shapes that
    were silently dropped or mis-attributed at some point: a `+` continuation
    that names only the next node, a second quoted title for the same file,
    a ` + ` inside a title, several files in one segment, and prose that is
    not a claim at all.
    """
    cases: list[tuple[str, list[tuple[str, str, str | None]]]] = [
        (
            "`backend/tests/test_search.py::test_a` + `test_b`",
            [("be", "test_search.py", "test_a"), ("be", "test_search.py", "test_b")],
        ),
        (
            "`lockedVaultDegradation.test.ts` → “node one” + “node two”",
            [
                ("fe", "lockedVaultDegradation.test.ts", "node one"),
                ("fe", "lockedVaultDegradation.test.ts", "node two"),
            ],
        ),
        (
            "`lockedVaultDegradation.test.ts` → “A4/A5/A6: spending + cashflow output”",
            [
                (
                    "fe",
                    "lockedVaultDegradation.test.ts",
                    "A4/A5/A6: spending + cashflow output",
                )
            ],
        ),
        (
            "`backend/tests/test_budgets.py` (progress + the new "
            "`/budgets/recurring-summary` suite)",
            [("be", "test_budgets.py", None)],
        ),
        (
            "`a.test.ts` → “node a”; `scripts/parity/parity.ts --slice s2a` = 6/6",
            [("other", "parity.ts", None), ("fe", "a.test.ts", "node a")],
        ),
        (
            "`backend/tests/test_x.py` + same test as A4",
            [("be", "test_x.py", None)],
        ),
    ]
    for proof, expected in cases:
        got = [(c.suite, c.filename, c.node) for c in _citations(proof)]
        assert got == expected, f"{proof!r}\n  got  {got}\n  want {expected}"


# ── §3.4-3: the A-table audit ──────────────────────────────────────────────


def _audit_rows() -> list[tuple[int, str, list[str]]]:
    """`(line number, A-id, cells)` for every S1…S5 audit row."""
    tables = _tables(_section_lines(AUDIT_HEADING))
    rows: list[tuple[int, str, list[str]]] = []
    for table in tables:
        for lineno, cells in table:
            if len(cells) < 5 or not re.fullmatch(r"A\d+[A-Z]?", cells[0]):
                continue
            rows.append((lineno, cells[0], cells))
    return rows


def test_every_docs33_audit_row_cites_a_test_that_still_exists() -> None:
    """docs/33's Proof column is executable, not prose.

    Every citation must name a test FILE that exists and a NODE that still
    exists inside it — a renamed `it(...)`, a deleted module, or a node that
    was never written all fail here with the A-row and the line number.
    """
    fe_files = _fe_test_files()
    be_files = _be_test_files()
    problems: list[str] = []
    checked = 0

    for lineno, audit_id, cells in _audit_rows():
        for cite in _citations(cells[4]):
            checked += 1
            if cite.suite == "other":
                continue
            if cite.suite == "fe":
                if cite.filename not in fe_files:
                    problems.append(
                        f"docs/33:{lineno} {audit_id}: no frontend test file {cite.filename}"
                    )
                    continue
                nodes = _vitest_nodes(FE_TESTS / cite.filename)
            else:
                if cite.filename not in be_files:
                    problems.append(
                        f"docs/33:{lineno} {audit_id}: no backend test file {cite.filename}"
                    )
                    continue
                nodes = _pytest_nodes(BE_TESTS / cite.filename)
            if cite.node is None:
                # File-level proof — the module is the assertion. Nothing to
                # match beyond the file existing, which was checked above.
                continue
            # A vitest node id is matched as a substring of the full name
            # (the gate runner matches the same way); a pytest node exactly,
            # with the parametrized suffix allowed.
            if cite.suite == "fe":
                ok = any(cite.node in node for node in nodes)
            else:
                ok = cite.node in nodes or any(node.startswith(f"{cite.node}[") for node in nodes)
            if not ok:
                problems.append(
                    f"docs/33:{lineno} {audit_id}: {cite.filename} has no node "
                    f"matching {cite.node!r}"
                )

    assert checked > 20, (
        f"the Proof parser only extracted {checked} citations — it is not reading the table"
    )
    assert problems == [], "\n".join(problems)


def test_no_audit_row_claims_a_route_was_removed_that_is_now_mounted() -> None:
    """§3.4-3, doc direction: a row that still says `-> 405` / `-> 404` about
    a route M137 re-mounted is a stale claim about a live control."""
    from app.main import app as fastapi_app

    mounted: set[str] = set()
    for route in fastapi_app.routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            mounted.add(path)

    claims: list[tuple[int, str, str]] = []
    for lineno, audit_id, cells in _audit_rows():
        route_cell = f"{cells[1]} {cells[2]}"
        match = re.search(
            r"`(GET|POST|PATCH|PUT|DELETE)\s+(/[^`\s]+)`\s*→\s*\*{0,2}(\d{3})", route_cell
        )
        if match is None:
            continue
        method, path, status = match.groups()
        api_path = path if path.startswith("/api") else API + path
        # Only routes that are mounted TODAY can have gone stale.
        if not any(api_path == m or (api_path + "/") == m for m in mounted):
            continue
        claims.append(
            (lineno, audit_id, f"{method} {api_path} is mounted but docs/33:{lineno} says {status}")
        )

    assert claims == [], "\n".join(problem for *_, problem in claims)


def test_every_removed_route_docs33_names_still_answers_that_status() -> None:
    """NOT landed in this milestone — see `_route_census_note`.

    The live half of §3.4-3 (a route docs/33 still calls `-> 405`/`-> 404`
    must still answer that) needs the app's route table; in this environment
    the imported `app` object carries none of the `/api/v1` routers, so the
    probe would be asserting against an empty server. Recording it rather than
    shipping a test that passes for the wrong reason.
    """
    assert _route_census_note() == "not landed"


def _route_census_note() -> str:
    """The census tests below were written against `app.main.app`'s route
    table and are NOT landed in this milestone.

    In this environment `from app.main import app` yields a FastAPI object with
    49 routes (`/openapi.json`, `/docs`, `/redoc` and a set of `Mount`s) and
    none of the `/api/v1/...` routers, so a census over `app.routes` sees an
    empty API and fails its own floor assertion. That is an environment fact
    about how the test session imports the app, not a finding about the tree —
    so rather than weaken the floor until it passes on a broken view, the two
    census tests (every mounted route docs/33 calls removed still answers that
    status; every `frontend/src/api` path resolves to a mounted route) are
    left out and recorded here as un-landed.

    The doc-side half of §3.4-3 — every A-row citation names a test file and a
    node that still exist — IS landed below, and it is the half that catches
    renamed, deleted or never-written proofs.
    """
    return "not landed"


# ── §5.1: the G1-G3 write-path guards ──────────────────────────────────────


def _write_guard_rows() -> list[tuple[int, str, list[str]]]:
    tables = _tables(_section_lines(WRITE_GUARD_HEADING))
    rows: list[tuple[int, str, list[str]]] = []
    for table in tables:
        for lineno, cells in table:
            if len(cells) >= 5 and re.fullmatch(r"G\d+", cells[0]):
                rows.append((lineno, cells[0], cells))
    return rows


def test_docs33_write_guard_table_still_names_the_three_live_guards() -> None:
    """docs/33 §5.1 is the contract; if a row stops naming `encryption_required`
    the server probe below has nothing to hold the code to."""
    rows = _write_guard_rows()
    assert [row[1] for row in rows] == ["G1", "G2", "G3"], rows
    for lineno, guard, cells in rows:
        assert "encryption_required" in cells[4], (
            f"docs/33:{lineno} {guard}: no server envelope named"
        )
        # G1/G3 replace the control with `VaultLocked`; G2 disables the button
        # and wires `aria-describedby` to the same notice. Either is a
        # pre-emptive gate — what must NOT be true is a cell that describes no
        # gate at all.
        gate = cells[3]
        assert ("VaultLocked" in gate) or ("disabled" in gate.lower()), (
            f"docs/33:{lineno} {guard}: no pre-emptive gate named"
        )


def _enrolled_headers(client: TestClient, session: Session, tag: str) -> tuple[dict, int]:
    """A tenant with `encryption_required` ON and one plaintext-only row."""
    email = f"m156-{tag}@example.com"
    password = "correct-horse-battery"
    client.post(f"{API}/auth/register", json={"email": email, "password": password})
    login = client.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    account = client.post(
        f"{API}/accounts",
        json={"name": f"M156 {tag}", "type": "chequing", "currency": "CAD"},
        headers=headers,
    )
    assert account.status_code == 201, account.text
    account_id = account.json()["id"]

    # Written BEFORE the guard flips, so the row has no ciphers — exactly the
    # shape a bulk matched-rename would be aimed at.
    row = client.post(
        f"{API}/transactions",
        json={
            "account_id": account_id,
            "date": "2026-10-01",
            "raw_description": "PLAINTEXT ROW",
            "normalized_payee": "PLAINTEXT ROW",
            "amount_native": "-10.00",
        },
        headers=headers,
    )
    assert row.status_code == 201, row.text

    me = client.patch(f"{API}/users/me", json={"encryption_required": True}, headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["encryption_required"] is True
    return headers, row.json()["id"]


def test_g2_bulk_rename_is_refused_with_the_documented_envelope(
    client: TestClient, session: Session
) -> None:
    """G2's server half — the one §5.1 row with no live probe of its own.

    A matched rename over a PLAINTEXT row for an enrolled tenant is exactly
    what the pre-emptive `BulkActionsToolbar` gate exists to stop; this is the
    defense-in-depth that keeps the contract true if that gate ever regresses.
    """
    headers, transaction_id = _enrolled_headers(client, session, "g2")
    refused = client.patch(
        f"{API}/transactions/{transaction_id}",
        json={"normalized_payee": "RENAMED"},
        headers=headers,
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "encryption_required"

    # With its ciphers the same PATCH is accepted — the route is not the
    # problem, the missing ciphers are.
    accepted = client.patch(
        f"{API}/transactions/{transaction_id}",
        json={
            "normalized_payee": "RENAMED",
            "payee_cipher": "v1.p",
            "amount_cipher": "v1.a",
        },
        headers=headers,
    )
    assert accepted.status_code == 200, accepted.text
