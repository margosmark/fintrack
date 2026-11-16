#!/usr/bin/env python3
"""Binary-inclusive PII/identifier scan over every blob at a git rev.

WHY THIS EXISTS
--------------
fintrack publishes as a SQUASHED public repo: one initial commit built from
HEAD, full history staying private (owner ruling, 2026-11-14). So HEAD — and
only HEAD — must carry zero real personal identifiers. This tool reads every
tracked blob AS BYTES (PNG pixel data, compressed JSON, minified bundles —
`git grep -a` semantics, no file is skipped as "binary") and classifies every
email-shaped / PII-shaped match.

VERDICT CLASSES (emails)
------------------------
  fixture  the address is on FIXTURE_ALLOWLIST below: test tenants, reserved
           domains, noreply senders — not a real person
  noise    a byte run inside compressed/encoded data that only LOOKS like an
           address because its domain labels are not lowercase (e.g.
           `Z@E[.]J[K]` inside a PNG's DEFLATE stream). Not a mailbox; each image
           family carrying one is vision-reviewed by hand and the review is
           recorded on the purge card (t_355d54e3)
  REAL     everything else — the scan FAILS (exit 1) while any remains

Sections that are reported but NOT verdict-driving (they belong to the
license/topology scrub lane, see NOTICE and docs/13):
  * RFC1918 private IPv4 addresses (documentation/test ranges + the two
    residuals the topology scrub deliberately retained)
  * `/home/<user>` paths (agent/container usernames, not owner PII)

Usage:
    python3 scripts/pii_scan.py                 # scan HEAD
    python3 scripts/pii_scan.py --rev <sha>     # scan an exact commit
    python3 scripts/pii_scan.py --json out.json # machine-readable report

Exit codes: 0 = zero REAL identifiers, 1 = at least one REAL identifier,
2 = usage/git error.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict

# ---------------------------------------------------------------- allowlist
# Domains that are fixtures by construction. Extend deliberately: every entry
# must be a reserved/test domain or a host this repo's own fixtures own.
FIXTURE_DOMAINS = frozenset(
    {
        "example.com",  # RFC 2606 — the probe-tenant default
        "example.org",
        "example.net",
        "fintracker.local",  # demo + locked fixture tenants
        "synthetic.local",
        "test.local",  # deep-link gate fixtures (android/)
        "hermes.local",
        "x.com",  # fixture "other/new" addresses in tests
    }
)
# Reserved TLDs (RFC 2606 / 6761): never routable mail.
FIXTURE_TLDS = frozenset({"test", "example", "invalid", "localhost", "local"})
# Sender-shaped local parts that name no person.
FIXTURE_LOCALPARTS = frozenset({"noreply", "no-reply", "donotreply"})

# ----------------------------------------------------------------- patterns
# Loose: what a naive binary grep finds (any case).
EMAIL_LOOSE = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# RFC1918 private addresses (reported only).
PRIVATE_IPV4 = re.compile(
    rb"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}"
    rb"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)
HOME_PATH = re.compile(rb"/home/[A-Za-z0-9._-]+")
SSN = re.compile(rb"\b\d{3}-\d{2}-\d{4}\b")
# Payment-card shapes: IIN-gated (Visa/MC/Amex/Discover), optional separators.
# A bare "any13-19 digit run + Luhn" matches timestamps and uv.lock hashes;
# the IIN gate is what keeps lockfiles out of the verdict.
CARD_SHAPE = re.compile(
    rb"(?<!\d)(?:"
    rb"4\d{3}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}"  # Visa 16
    rb"|4\d{3}[ -]?\d{4}[ -]?\d{4}[ -]?\d{3}"  # Visa 13
    rb"|5[1-5]\d{2}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}"  # MasterCard
    rb"|3[47]\d{2}[ -]?\d{6}[ -]?\d{5}"  # Amex
    rb"|6(?:011[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}"  # Discover
    rb"|5[ -]?\d{4}[ -]?\d{4}[ -]?\d{4})"
    rb")(?!\d)"
)
PHONE_SHAPE = re.compile(
    rb"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)"
)


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def classify_email(raw: bytes) -> str:
    text = raw.decode("ascii", "replace")
    local, _, domain = text.rpartition("@")
    if not local or not domain:
        return "REAL"
    if domain.lower() in FIXTURE_DOMAINS:
        return "fixture"
    if domain.lower().rsplit(".", 1)[-1] in FIXTURE_TLDS:
        return "fixture"
    if local.lower() in FIXTURE_LOCALPARTS:
        return "fixture"
    # Plausibility: a routable mailbox has an all-lowercase domain with a
    # letter TLD. An uppercase/mixed-case domain inside compressed bytes is a
    # byte coincidence, not an address (measured on the m130/m167 PNGs:
    # `X%@fV[.]tc`, `Z@E[.]J[K]` sit in DEFLATE streams with no text chunks).
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,63}", domain):
        return "noise"
    if not re.fullmatch(r"[A-Za-z0-9._%+-]+", local):
        return "noise"
    return "REAL"


def iter_blobs(rev: str):
    """Yield (path, bytes) for every tracked blob at `rev`."""
    ls = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "-z", rev],
        capture_output=True,
        check=True,
    )
    paths = [p for p in ls.stdout.decode("utf-8", "replace").split("\0") if p]
    proc = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None
    try:
        for path in paths:
            proc.stdin.write(f"{rev}:{path}\n".encode())
            proc.stdin.flush()
            header = proc.stdout.readline().decode("ascii", "replace").split()
            if len(header) != 3 or header[1] != "blob":
                # "missing" / non-blob: skip the payload line if present
                if header and header[0] == "missing":
                    continue
                raise RuntimeError(f"unexpected cat-file header for {path}: {header}")
            size = int(header[2])
            payload = proc.stdout.read(size)
            proc.stdout.read(1)  # trailing newline
            yield path, payload
    finally:
        proc.stdin.close()
        proc.wait()


def scan(rev: str) -> dict:
    emails: dict[str, dict] = defaultdict(lambda: {"cls": None, "paths": set()})
    private_ips: dict[str, set] = defaultdict(set)
    home_paths: dict[str, set] = defaultdict(set)
    ssn: dict[str, set] = defaultdict(set)
    cards: dict[str, set] = defaultdict(set)
    phones: dict[str, set] = defaultdict(set)
    blob_count = 0

    for path, payload in iter_blobs(rev):
        blob_count += 1
        for m in EMAIL_LOOSE.findall(payload):
            key = m.decode("ascii", "replace")
            entry = emails[key]
            entry["cls"] = classify_email(m)
            entry["paths"].add(path)
        for m in PRIVATE_IPV4.findall(payload):
            private_ips[m.decode()].add(path)
        for m in HOME_PATH.findall(payload):
            home_paths[m.decode()].add(path)
        for m in SSN.findall(payload):
            ssn[m.decode()].add(path)
        for m in CARD_SHAPE.findall(payload):
            digits = re.sub(rb"\D", b"", m).decode()
            if len(digits) in (13, 15, 16, 19) and luhn_ok(digits):
                cards[digits].add(path)
        for m in PHONE_SHAPE.findall(payload):
            phones[m.decode()].add(path)

    real = sorted(k for k, v in emails.items() if v["cls"] == "REAL")
    noise = sorted(k for k, v in emails.items() if v["cls"] == "noise")
    fixture = sorted(k for k, v in emails.items() if v["cls"] == "fixture")
    return {
        "rev": rev,
        "blobs": blob_count,
        "emails": {
            "REAL": {k: sorted(emails[k]["paths"]) for k in real},
            "noise": {k: sorted(emails[k]["paths"]) for k in noise},
            "fixture": {k: sorted(emails[k]["paths"]) for k in fixture},
        },
        "luhn_cards": {k: sorted(v) for k, v in sorted(cards.items())},
        "ssn_shaped": {k: sorted(v) for k, v in sorted(ssn.items())},
        "phone_shaped": {k: sorted(v) for k, v in sorted(phones.items())},
        "private_ipv4_reported_only": {k: sorted(v) for k, v in sorted(private_ips.items())},
        "home_paths_reported_only": {k: sorted(v) for k, v in sorted(home_paths.items())},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rev", default="HEAD", help="git rev to scan (default HEAD)")
    ap.add_argument("--json", help="write the full report to this path")
    args = ap.parse_args()

    try:
        report = scan(args.rev)
    except (subprocess.CalledProcessError, RuntimeError, FileNotFoundError) as exc:
        print(f"pii_scan: {exc}", file=sys.stderr)
        return 2

    real = report["emails"]["REAL"]
    noise = report["emails"]["noise"]
    fixture = report["emails"]["fixture"]

    print(f"pii_scan: {report['blobs']} blobs at {report['rev']}")
    print(f"  emails fixture : {len(fixture)} distinct")
    print(f"  emails noise    : {len(noise)} distinct (compressed-byte shape, not mailboxes)")
    for addr, paths in noise.items():
        print(f"      {addr}  <- {', '.join(paths[:3])}" + (" …" if len(paths) > 3 else ""))
    print(f"  emails REAL     : {len(real)} distinct")
    for addr, paths in real.items():
        print(f"      {addr}  <- {', '.join(paths[:3])}" + (" …" if len(paths) > 3 else ""))
    for label, key in (
        ("luhn-valid card shapes", "luhn_cards"),
        ("ssn-shaped", "ssn_shaped"),
        ("phone-shaped", "phone_shaped"),
    ):
        hits = report[key]
        print(f"  {label:24}: {len(hits)} distinct")
        for value, paths in list(hits.items())[:5]:
            print(f"      {value}  <- {', '.join(paths[:3])}")
    print(
        f"  reported only (topology lane): {len(report['private_ipv4_reported_only'])} "
        f"RFC1918 addresses, {len(report['home_paths_reported_only'])} /home/ paths"
    )

    verdict_fail = bool(real) or bool(report["luhn_cards"]) or bool(report["ssn_shaped"])
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1, sort_keys=True)
    if verdict_fail:
        print("VERDICT: FAIL — real identifiers present at this rev")
        return 1
    print("VERDICT: PASS — zero real identifiers at this rev")
    return 0


if __name__ == "__main__":
    sys.exit(main())
