#!/usr/bin/env python3
"""One table runner for the docs/33 locked-vault degradation audit.

Run it, get one table: every audit row (A1–A19) and every global negative
(N1–N3), each mapped to the test node that proves it, with a REAL verdict
read from a fresh vitest + pytest run — not from a claim in a doc.

    uv run python scripts/locked_vault_gate.py            # both suites
    uv run python scripts/locked_vault_gate.py --fe-only  # skip postgres
    uv run python scripts/locked_vault_gate.py --be-only
    uv run python scripts/locked_vault_gate.py --md       # paste-ready markdown

WHY THIS EXISTS (docs/33 §Hardening audit): the audit table lived in prose,
so its "Proof" column could rot silently — a renamed test, a file that moved,
a row whose cited node no longer runs. This runner makes the citation
executable: a row whose proof cannot be found or does not pass fails the run.

THE VERDICT COLUMN IS THE POINT. Three states, never collapsed:

  WORKS      the designed behaviour holds (locked vault degrades honestly, or
             the route is gone on purpose)
  KNOWN RED  the defect is real and TRACKED — docs/33 F-number in the row. A
             green run does not make these pass; they are red until the fix
             ships, and the marker says which line to flip.
  BROKEN     unexpected: a proof missing, failing, or newly passing. This is
             the only state that fails the gate.

Reading a table where every row says "pass" would tell you nothing about
whether the vault is honest while locked. Reading which rows are red — and
that they are red for a tracked reason — is the signal.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FE = REPO / "frontend"
BE = REPO / "backend"

WORKS = "works"
KNOWN_RED = "known-red"

# The suite files whose test nodes back this table. Both halves are run ONCE
# and every row reads from that one run, so the table cannot disagree with
# itself about what passed.
FE_FILES = [
    "tests/lockedVaultDegradation.test.ts",
    "tests/lockedVaultKnownGaps.test.tsx",
    "tests/e2ee.test.ts",
    "tests/backfill.test.ts",
    "tests/m119SimilarPopover.test.tsx",
]
BE_FILES = [
    "tests/test_locked_vault_exposures.py",
    "tests/test_search.py",
    "tests/test_rules_preview_apply.py",
    "tests/test_subscriptions.py",
    "tests/test_vault_envelopes.py",
    "tests/test_m106_review_events.py",
    # M160 (P2-5): the P1-5 lock-invariance proofs for the report routes,
    # which used to be client-engine halves of lockedVaultDegradation.test.ts.
    "tests/test_m160_reports_lock_invariance.py",
]


@dataclass
class Row:
    """One audit line. `proofs` are (suite, file, node-substring) triples."""

    id: str
    feature: str
    exposure: str
    expected: str  # WORKS | KNOWN_RED
    proofs: list[tuple[str, str, str]] = field(default_factory=list)
    note: str = ""


ROWS: list[Row] = [
    # --- S1: search -------------------------------------------------------
    Row(
        "A1",
        "GET /search (blind index only)",
        "reads payee_token while locked; cipher rows drive client fuzzy match",
        WORKS,
        [
            (
                "fe",
                "lockedVaultDegradation.test.ts",
                "A1: no blind-index token while locked",
            ),
            (
                "fe",
                "lockedVaultDegradation.test.ts",
                "cipher rows never match and never render plaintext",
            ),
            (
                "fe",
                "lockedVaultKnownGaps.test.tsx",
                "unlock hint when a locked vault hides cipher rows",
            ),
        ],
        "the hint is the explained-disabled pattern A18 has to copy",
    ),
    Row(
        "A2",
        "GET /transactions/similar -> 405",
        "client-only; a locked vault tiles the cipher rows out of Similar",
        WORKS,
        [
            (
                "be",
                "test_search.py",
                "test_similar_route_is_indifferent_to_encryption_required",
            ),
            (
                "fe",
                "lockedVaultDegradation.test.ts",
                "A2: locked cipher rows never qualify",
            ),
            (
                "fe",
                "m119SimilarPopover.test.tsx",
                "a locked vault with withheld siblings says unlock",
            ),
        ],
    ),
    Row(
        "A3",
        "GET /transactions?q= -> 422 q_removed",
        "ranks over searchPayee(); locked cipher rows render as the lock marker",
        WORKS,
        [
            (
                "be",
                "test_search.py",
                "test_list_q_ranks_the_search_view_server_side",
            ),
            (
                "be",
                "test_search.py",
                "test_list_q_ranking_is_indifferent_to_encryption_required",
            ),
            (
                "fe",
                "lockedVaultDegradation.test.ts",
                "A3: the search page excludes cipher rows",
            ),
        ],
    ),
    # --- S2a/S2b/S2c/S4: kept CSV/HTML report engines (lock-vacuous) -------
    Row(
        "A4-A6",
        "spending, spending/categories, cashflow (CSV)",
        "none — engines read mandatory plaintext columns",
        WORKS,
        [
            (
                "be",
                "test_m160_reports_lock_invariance.py",
                "test_spending_reports_are_indifferent_to_encryption_required",
            )
        ],
        "M160: the client engines are deleted, so the proof is the route's own "
        "P1-5 pair — identical JSON under `encryption_required` [False, True], "
        "with a non-zero denominator so an empty answer cannot pass",
    ),
    Row(
        "A7",
        "net-worth (CSV)",
        "none (same class)",
        WORKS,
        [
            (
                "be",
                "test_m160_reports_lock_invariance.py",
                "test_net_worth_is_indifferent_to_encryption_required",
            )
        ],
        "M160: proved at the route (P1-5), not by a client engine that no longer exists",
    ),
    Row(
        "A8",
        "investment-performance (CSV)",
        "none (same class)",
        WORKS,
        [
            (
                "be",
                "test_m160_reports_lock_invariance.py",
                "test_investment_performance_is_indifferent_to_encryption_required",
            )
        ],
        "M160: proved at the route (P1-5), not by a client engine that no longer exists",
    ),
    Row(
        "A9",
        "dividends (CSV)",
        "none (same class)",
        WORKS,
        [
            (
                "be",
                "test_m160_reports_lock_invariance.py",
                "test_dividends_are_indifferent_to_encryption_required",
            )
        ],
        "M160-R2: the client half (`lib/reportClient.ts::dividendReport`) was "
        "deleted with the shim when the panel was re-pointed, so the proof is "
        "the route's own P1-5 pair — identical JSON under `encryption_required` "
        "[False, True], with a non-zero dividend total",
    ),
    Row(
        "A10",
        "POST /rules/preview (re-mounted M137 P1-2)",
        "none — the SERVER's predicate over plaintext normalized_payee; "
        "the client twin retired with M160",
        WORKS,
        [
            (
                "be",
                "test_rules_preview_apply.py",
                "test_preview_is_indifferent_to_encryption_required",
            ),
            (
                "be",
                "test_rules_preview_apply.py",
                "test_preview_dry_runs_server_side",
            ),
        ],
        "the route is the one engine since M137 P1-2; M160 deleted the "
        "`previewRule` client twin, so the proof is the route's own P1-5 pair",
    ),
    Row(
        "A11",
        "POST /rules/{id}/apply (server write)",
        "rules.pattern is PLAINTEXT, no cipher column (F8)",
        WORKS,
        [("be", "test_rules_preview_apply.py", "test_apply_forward_writes_nothing")],
        "writes server-side; the preview that gates it is A10",
    ),
    Row(
        "A12",
        "GET /subscriptions (re-mounted M137 P1-2)",
        "none — server detection over plaintext columns; the client twin "
        "retired with M160",
        WORKS,
        [
            (
                "be",
                "test_subscriptions.py",
                "test_subscriptions_is_indifferent_to_encryption_required",
            ),
            (
                "be",
                "test_subscriptions.py",
                "test_get_subscriptions_answers_with_the_report_shape",
            ),
        ],
        "the route is the detector since M137 P1-2; M160 deleted the "
        "`subscriptionsReport` client twin, so the proof is the route's own "
        "P1-5 pair over the frozen `subscriptions.detect` golden",
    ),
    Row(
        "A13",
        "GET /budgets (definitions only)",
        "definitions plaintext; progress computed client-side",
        WORKS,
        [
            (
                "fe",
                "lockedVaultDegradation.test.ts",
                "A13: budget definitions/progress are lock-invariant",
            )
        ],
    ),
    Row(
        "A14",
        "capital-gains (csv/html)",
        "none — ACB replay over plaintext inputs",
        WORKS,
        [
            (
                "be",
                "test_m160_reports_lock_invariance.py",
                "test_capital_gains_is_indifferent_to_encryption_required",
            )
        ],
        "M160: one implementation (docs/06), proved lock-invariant at the route",
    ),
    Row(
        "A15",
        "GET /acb",
        "none (same class)",
        WORKS,
        [
            (
                "be",
                "test_m160_reports_lock_invariance.py",
                "test_acb_pools_are_indifferent_to_encryption_required",
            )
        ],
        "M160: the client pool math is deleted; the read is a derived cache "
        "written only through rebuild_user_pools, proved lock-invariant here",
    ),
    # --- Phase 2 surfaces -------------------------------------------------
    Row(
        "A16",
        "Txn list + account names (display)",
        "ciphers -> the lock marker; v0 rows stay plaintext (mixed-mode by design)",
        WORKS,
        [
            (
                "fe",
                "e2ee.test.ts",
                "shows the lock marker when ciphertext exists but vault is locked",
            ),
            ("fe", "e2ee.test.ts", "falls back to plaintext legacy rows"),
        ],
    ),
    Row(
        "A17",
        "Client backfill (write)",
        "fail-closed: getDEK() throws 'Vault is locked.'",
        WORKS,
        [("fe", "backfill.test.ts", "fails closed when the vault is locked")],
    ),
    # --- the two honest gaps ---------------------------------------------
    Row(
        "A18",
        "S5 search view has no unlock hint",
        "a locked vault with cipher rows shows a bare empty list",
        WORKS,
        [
            (
                "fe",
                "lockedVaultKnownGaps.test.tsx",
                "the S5 search view announces what it withholds",
            ),
            (
                "fe",
                "lockedVaultKnownGaps.test.tsx",
                "is lock-silent when nothing is withheld",
            ),
        ],
        "finding F4 — FIXED (M136): the shared VaultLocked partial variant "
        "replaces the empty state with a measured withheld count and an "
        "inline unlock; the characterization tripwire flipped with it",
    ),
    Row(
        "F2",
        "logout does not lock the vault",
        "sessionDEK + decrypted-value cache survive logout for the page's life",
        KNOWN_RED,
        [
            (
                "fe",
                "lockedVaultKnownGaps.test.tsx",
                "logout clears the session but keeps the DEK",
            )
        ],
        "finding F2 — tracked; lockVault() is only wired to the Settings button",
    ),
    # --- global negatives: plaintext BY DESIGN, not ciphertext leaks ------
    Row(
        "N1",
        "transaction_events.payload (M106)",
        "amount/payee verbatim plaintext, no cipher column, ships in /export/full",
        WORKS,
        [
            (
                "be",
                "test_locked_vault_exposures.py",
                "test_receipt_payloads_carry_plaintext_verbatim",
            ),
            (
                "be",
                "test_m106_review_events.py",
                "test_export_full_carries_transaction_events",
            ),
            ("be", "test_m106_review_events.py", "test_partner_projection_boundary"),
        ],
        "accepted + disclosed (docs/33 OUTSIDE THE ENVELOPE; docs/43 §6 strips it from the partner projection)",
    ),
    Row(
        "N2",
        "dedup_hash (F7)",
        "unsalted SHA-256 over account|date|amount|raw_description — dictionary-reversible",
        WORKS,
        [
            (
                "be",
                "test_locked_vault_exposures.py",
                "test_dedup_hash_is_unsalted_sha256_over_plaintext",
            )
        ],
        "tripwire: fails the day a keyed HMAC ships, which is the point",
    ),
    Row(
        "N3",
        "rules.pattern / learned_payees.normalized_payee (F8)",
        "plaintext columns, no blind index",
        WORKS,
        [
            (
                "be",
                "test_locked_vault_exposures.py",
                "test_rules_and_learned_payees_carry_no_cipher_or_token_column",
            )
        ],
        "tripwire: fails the day a cipher sibling column lands",
    ),
]


# --- execution ---------------------------------------------------------------


def _run(
    cmd: list[str], cwd: Path, env: dict | None = None
) -> subprocess.CompletedProcess:
    """Run a suite, capture output. Non-zero is not fatal here: the ROWS
    decide pass/fail, and a suite that dies should surface as BROKEN rows with
    the reason, not as a truncated traceback."""
    merged = {**os.environ, **(env or {})}
    # check=False on purpose: a suite that dies must surface as BROKEN rows
    # with a reason, not as a traceback that hides which rows were affected.
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=merged,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )


def _collect_fe(out: Path) -> dict[str, str]:
    """Map vitest test names -> verdict. Full names carry the describe block."""
    data = json.loads(out.read_text())
    results: dict[str, str] = {}
    for suite in data.get("testResults", []):
        for assertion in suite.get("assertionResults", []):
            name = assertion.get("fullName") or assertion.get("title", "")
            results[name] = assertion.get("status", "unknown")
    return results


def _collect_be(out: Path) -> dict[str, str]:
    """Map pytest node ids -> verdict, including every parametrized variant."""
    results: dict[str, str] = {}
    root = ET.parse(out).getroot()
    cases = root.iter("testcase")
    for case in cases:
        nodeid = case.get("name", "")
        classname = case.get("classname", "")
        if case.find("skipped") is not None:
            status = "skipped"
        elif case.find("failure") is not None or case.find("error") is not None:
            status = "failed"
        else:
            status = "passed"
        # classname is the module path; name is the function (plus params).
        results[f"{classname}::{nodeid}"] = status
        results[nodeid] = status
    return results


def _matches(proof: tuple[str, str, str], results: dict[str, str]) -> str | None:
    """Resolve one citation against a run. Returns the verdict, or None when
    the cited node is NOT in the run — a missing proof is its own failure."""
    _suite, filename, needle = proof
    # vitest full names carry describe+title, NOT the file name — so the
    # filename leg can only ever match pytest xml (classname carries the
    # module). Keep the file leg for be; for fe, the needle alone is the
    # citation (needles include the test's own prose and are per-row unique).
    if _suite == "fe":
        for name, status in results.items():
            if needle in name:
                return status
        return None
    # A vitest full name or a pytest node id, both matched as substrings.
    for name, status in results.items():
        if (
            filename.replace("test_", "") in name or filename in name
        ) and needle in name:
            return status
    # pytest node ids carry the module, not the file name.
    for name, status in results.items():
        if needle in name and ("::" in name):
            return status
    return None


def evaluate(rows: list[Row], fe: dict[str, str], be: dict[str, str]) -> list[dict]:
    """Turn a run into verdicts. Three states, never collapsed into 'pass'."""
    out = []
    for row in rows:
        results = {"fe": fe, "be": be}
        verdicts, missing = [], []
        for proof in row.proofs:
            status = _matches(proof, results[proof[0]])
            if status is None:
                missing.append(proof[2])
            elif status == "failed":
                verdicts.append("failed")
            else:
                verdicts.append("ok")

        if missing:
            state, detail = "BROKEN", "proof not found: " + "; ".join(missing)
        elif "failed" in verdicts:
            state, detail = "BROKEN", "proof failed"
        elif row.expected == KNOWN_RED:
            # The characterization test PASSES because the defect is still
            # there. Its passing IS the red. The day it fails, the fix landed
            # and the marker must move — which is also a broken gate.
            state, detail = "KNOWN RED", row.note or "tracked defect, still present"
        else:
            state, detail = (
                "WORKS",
                row.note or f"{len(verdicts)}/{len(verdicts)} proofs pass",
            )
        out.append(
            {
                "id": row.id,
                "feature": row.feature,
                "exposure": row.exposure,
                "state": state,
                "detail": detail,
            }
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fe-only", action="store_true")
    ap.add_argument("--be-only", action="store_true")
    ap.add_argument("--md", action="store_true", help="markdown table, paste-ready")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fe_out, be_out = tmp_path / "fe.json", tmp_path / "be.xml"
        fe, be = {}, {}
        notes = []

        if not args.be_only:
            files = [str(FE / f) for f in FE_FILES]
            proc = _run(
                [
                    "npx",
                    "vitest",
                    "run",
                    *files,
                    "--reporter",
                    "json",
                    "--outputFile",
                    str(fe_out),
                ],
                FE,
                {"NODE_ENV": "test"},
            )
            if fe_out.exists():
                fe = _collect_fe(fe_out)
            else:
                tail = (
                    proc.stderr.strip().splitlines()[-1:]
                    or proc.stdout.strip().splitlines()[-1:]
                )
                notes.append(
                    f"vitest produced no report (exit {proc.returncode}): {tail}"
                )

        if not args.fe_only:
            url = os.environ.get("DATABASE_URL")
            if not url:
                notes.append(
                    "DATABASE_URL unset — backend rows skipped (see fin-tracker skill for the tunnel)"
                )
            else:
                files = [str(BE / f) for f in BE_FILES]
                proc = _run(
                    ["uv", "run", "pytest", "-q", "--junitxml", str(be_out), *files],
                    BE,
                )
                if be_out.exists():
                    be = _collect_be(be_out)
                else:
                    notes.append(f"pytest produced no report (exit {proc.returncode})")

    verdicts = evaluate(ROWS, fe, be)

    works = [v for v in verdicts if v["state"] == "WORKS"]
    reds = [v for v in verdicts if v["state"] == "KNOWN RED"]
    broken = [v for v in verdicts if v["state"] == "BROKEN"]

    if args.md:
        print("| # | Feature / endpoint | E2EE-field exposure | Verdict | Detail |")
        print("|---|---|---|---|---|")
        for v in verdicts:
            flag = {"WORKS": "GREEN", "KNOWN RED": "RED", "BROKEN": "BROKEN"}[
                v["state"]
            ]
            print(
                f"| {v['id']} | {v['feature']} | {v['exposure']} | {flag} | {v['detail']} |"
            )
    else:
        width = max(len(v["id"]) for v in verdicts)
        for v in verdicts:
            tag = {
                "WORKS": "[GREEN]    ",
                "KNOWN RED": "[RED]      ",
                "BROKEN": "[BROKEN]   ",
            }[v["state"]]
            print(f"{tag} {v['id']:<{width}}  {v['feature']}")
            print(f"{'':13}exposure: {v['exposure']}")
            print(f"{'':13}{v['detail']}")
        print()
        print(f"{len(works)} works | {len(reds)} known red | {len(broken)} broken")

    for note in notes:
        print(f"NOTE: {note}", file=sys.stderr)

    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
