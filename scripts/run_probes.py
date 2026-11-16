#!/usr/bin/env python3
"""Run the live-probe battery with per-commit evidence caching (see probe_cache).

    python3 scripts/run_probes.py --list                     # what would run
    python3 scripts/run_probes.py                            # default suite: live
    python3 scripts/run_probes.py --suite live --base https://fintrac.princessandmark.com
    python3 scripts/run_probes.py 'm130_*' --arg width=390   # one probe, forwarded args
    python3 scripts/run_probes.py --refresh                  # ignore + overwrite cache

WHAT IT SOLVES
--------------
`AGENTS.md` §4 step 5 asks every change to be verified against the running
stack, and the probes that do that (`frontend/scripts/a11y/*_probe.py`) cost
minutes each. Rebases re-triggered 3-6 of those suites against a build that had
not changed — the rebase rewrote local history, nothing was redeployed, and the
answer was already stored. This runner looks the evidence up FIRST
(`scripts/probe_cache.py`) and prints the stored verdict instead of launching
Chrome. The stored evidence is a real artifact: the report JSON is restored to
its normal path when the file is missing, and every hit names the commit,
stack and timestamp it came from.

Rules that make the short-circuit safe:
  * only PASSING runs are ever stored — a failure always re-runs;
  * only when the stack identity is COMPLETE (deploy host reachable and the
    served build readable) — an unknown build can never produce a hit;
  * freshness: recorded today (host-local) and younger than --max-age-hours;
  * the probe's own bytes, `a11y_driver.py` and the axe-core version are part
    of the key, so editing a probe invalidates its stored evidence.

HOW A PROBE GETS ITS TARGET (probes disagree, so the runner adapts)
------------------------------------------------------------------
  `--base` argument  → passed as `--base <url>` (the majority of probes)
  `A11Y_BASE` env    → exported (m103/keywalk/session/axe_scan read the env)
  `SITE_BASE` env    → exported (site_axe_scan)
  neither            → SKIPPED with a printed reason: a probe that cannot be
                       pointed at the deployed build must not be run against it
                       (and must not be reported as verified).
Probes run SERIALLY on purpose: the shared CDP driver pins one debug port and
one Chrome profile, so parallel runs race each other and both verdicts die.

Exit status: 0 when every probed check passed (cached or fresh), 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_cache as cache_mod  # noqa: E402 — path set above

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE_DIR = REPO_ROOT / "frontend" / "scripts" / "a11y"
SUITES_FILE = PROBE_DIR / "suites.json"
# Production-target guard (frontend/scripts/a11y/probe_target.py) — the
# runner refuses to launch the battery against a production base unless
# PROBE_ALLOW_PROD=1 (card t_498845bf).
sys.path.insert(0, str(PROBE_DIR))
import probe_target  # noqa: E402 — path set above
DEFAULT_SUITE = "live"
DEFAULT_BASE = "https://fintrac.princessandmark.com"
DEFAULT_TTL_HOURS = 12.0
DEFAULT_PROBE_TIMEOUT = 900.0
# A probe's own evidence path is printed by nearly every probe on its last
# line (`… 12 passed, 0 failed → /path/probe.json`); the mtime scan below is
# the fallback for the ones that print something else.
OUT_RE = re.compile(r"(?:→|wrote)\s+(\S+\.json)")
COUNTS_RE = re.compile(r"(\d+)\s+passed,\s+(\d+)\s+failed")
MAX_REPORT_BYTES = 1_000_000


@dataclass
class Result:
    probe: str
    status: str  # cached | ran | failed | skipped
    detail: str = ""
    evidence: str = ""
    duration_s: float = 0.0
    counts: tuple[int, int] | None = None
    exit_code: int = 0
    notes: list[str] = field(default_factory=list)


def load_suites(path: Path = SUITES_FILE) -> dict[str, list[str]]:
    """Suite registry: name → globs relative to the probe directory."""
    fallback = {"live": ["*_probe.py"]}
    if not path.exists():
        return fallback
    try:
        data = json.loads(path.read_text())
        suites = data.get("suites")
        if not isinstance(suites, dict) or not suites:
            raise ValueError("no `suites` object")
        return {str(k): [str(g) for g in v] for k, v in suites.items()}
    except (OSError, ValueError, TypeError) as exc:
        print(f"WARN  {path} unreadable ({exc}); falling back to suite `live`", file=sys.stderr)
        return fallback


def expand_targets(
    specs: Sequence[str], *, suites: dict[str, list[str]], probe_dir: Path = PROBE_DIR
) -> list[Path]:
    """Positionals: suite name, file path, or glob — deduped, order preserved."""
    patterns: list[str] = []
    for spec in specs:
        if spec in suites and not (probe_dir / spec).exists():
            patterns.extend(suites[spec])
        else:
            patterns.append(spec)
    resolved: list[Path] = []
    for pattern in patterns:
        candidate = Path(pattern)
        if candidate.is_file():
            matches = [candidate.resolve()]
        else:
            matches = sorted(probe_dir.glob(pattern))
        if not matches:
            raise SystemExit(f"no probe matches {pattern!r} under {probe_dir}")
        for match in matches:
            if match not in resolved:
                resolved.append(match)
    return resolved


def _read(script: Path) -> str:
    try:
        return script.read_text()
    except OSError:
        return ""


def declares(script: Path, option: str, source: str | None = None) -> bool:
    """Does this probe's argparse declare `--option`? Read from its SOURCE.

    Static, not `--help`: a probe imports its driver and half the world before
    argparse will answer, so asking 23 probes to print their help costs ~30s of
    every run. Matching the `add_argument(...)` call is exact for the flag —
    prose that merely MENTIONS `--base` in a docstring is not an
    `add_argument(` call, which is the false positive `--help` text allows.
    """
    text = source if source is not None else _read(script)
    pattern = rf'add_argument\([^)]*["\x27]--{re.escape(option)}["\x27]'
    return re.search(pattern, text) is not None


def target_mode(script: Path) -> tuple[str, str]:
    """(mode, env var) — how this probe takes a target URL, if at all."""
    source = _read(script)
    if declares(script, "base", source):
        return "arg", ""
    for var in ("A11Y_BASE", "SITE_BASE"):
        if re.search(rf"\b{var}\b", source):
            return "env", var
    return "none", ""


def build_argv(script: Path, base: str, mode: str, forwarded: Sequence[str]) -> list[str]:
    """Only options the probe declares — a rejected arg is a failed probe."""
    source = _read(script)
    argv = [sys.executable, str(script)]
    if mode == "arg":
        argv += ["--base", base]
    for token in forwarded:
        key, _, value = token.partition("=")
        if declares(script, key.lstrip("-"), source):
            argv.extend([f"--{key.lstrip('-')}", value])
    return argv


def _normalize_args(forwarded: Sequence[str]) -> list[str]:
    """Canonical identity for forwarded args: sorted, `out` dropped.

    `--out` decides where a report is WRITTEN, not what the probe asserts, so
    it must not fragment the key (same probe, same build, different file).
    """
    kept = [token for token in forwarded if token.split("=", 1)[0].lstrip("-") != "out"]
    return sorted(f"--{t.split('=', 1)[0].lstrip('-')}={t.split('=', 1)[1]}" for t in kept)


def _run_probe(
    script: Path, argv: list[str], env: dict[str, str], timeout: float, echo: bool
) -> tuple[int, str, float]:
    """Run one probe, echoing its output live, capturing it for evidence.

    A drain thread keeps stdout flowing while the main thread enforces the
    timeout — a wedged Chrome must fail the probe, not hang the suite.
    """
    started = time.monotonic()
    lines: list[str] = []
    try:
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            argv,
            cwd=str(script.parent),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        return 127, f"cannot start probe: {exc}\n", time.monotonic() - started

    def drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            if echo:
                print(f"    {line}", end="", flush=True)

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait()
        lines.append(f"\nRUNNER TIMEOUT after {timeout:.0f}s — probe killed\n")
        if echo:
            print(f"    RUNNER TIMEOUT after {timeout:.0f}s — probe killed", flush=True)
    thread.join(timeout=10)
    return code, "".join(lines), time.monotonic() - started


def _discover_report(script: Path, stdout: str, started_at: float) -> Path | None:
    """Where the probe wrote its JSON evidence.

    Preferred: the path the probe printed. Fallback: a JSON file next to the
    probe written during this run, preferring a stem that matches the probe's
    own name (`m130_portfolio_header_probe.py` → `m130-portfolio-header-probe.json`).
    """
    matches = OUT_RE.findall(stdout)
    if matches:
        path = Path(matches[-1])
        if path.exists():
            return path
        if not path.is_absolute():
            candidate = script.parent / path
            if candidate.exists():
                return candidate
    candidates = [
        p
        for p in script.parent.glob("*.json")
        if p.stat().st_mtime >= started_at - 1.5 and p.is_file()
    ]
    if not candidates:
        return None
    stem = re.sub(r"[^a-z0-9]", "", script.stem.lower())
    scored = sorted(
        candidates,
        key=lambda p: (
            0 if re.sub(r"[^a-z0-9]", "", p.stem.lower()).startswith(stem[:16]) else 1,
            -p.stat().st_mtime,
        ),
    )
    return scored[0]


def _counts(stdout: str, report: dict | None) -> tuple[int, int] | None:
    if isinstance(report, dict):
        passed, failed = report.get("passed"), report.get("failed")
        if isinstance(passed, int) and isinstance(failed, int):
            return passed, failed
    found = COUNTS_RE.findall(stdout)
    if found:
        return int(found[-1][0]), int(found[-1][1])
    return None


def _summary_line(stdout: str) -> str:
    for line in reversed(stdout.splitlines()):
        if line.strip():
            return line.strip()[:400]
    return ""


def _cache_entry(
    script: Path,
    *,
    base: str,
    identity_args: Sequence[str],
    stdout: str,
    exit_code: int,
    duration_s: float,
    started_at: float,
    out_path: str | None = None,
) -> dict:
    """Build the record for a completed run (report bytes when locatable).

    `out_path` is the `--out` the RUNNER forwarded: when it did, that is the
    report's exact location — stdout parsing and the mtime scan only exist for
    probes running with their own default output path.
    """
    report_path = (
        Path(out_path)
        if out_path and Path(out_path).exists()
        else _discover_report(script, stdout, started_at)
    )
    report_obj: dict | None = None
    report_text: str | None = None
    report_sha = ""
    if report_path is not None:
        try:
            raw = report_path.read_text()
        except OSError:
            raw = ""
        if raw:
            report_text = raw if len(raw) <= MAX_REPORT_BYTES else None
            report_sha = cache_mod.sha256_hex(raw.encode())
            try:
                parsed = json.loads(raw)
                report_obj = parsed if isinstance(parsed, dict) else None
            except ValueError:
                report_obj = None
    counts = _counts(stdout, report_obj)
    return {
        "probe": script.name,
        "base": base,
        "args": list(identity_args),
        "exit_code": exit_code,
        "duration_s": round(duration_s, 2),
        "summary": _summary_line(stdout),
        "passed": counts[0] if counts else None,
        "failed": counts[1] if counts else None,
        "report_path": str(report_path) if report_path else "",
        "report_sha256": report_sha,
        "report": report_text,
    }


def _stack_line(stack: cache_mod.Stack, root: Path) -> str:
    line = f"stack  {stack.describe()}  cache={root}"
    if stack.commit_trust in {"checkout-ahead", "unverified"}:
        line += "\n       ! commit is a LABEL only — the deploy checkout is ahead of the containers"
    if not stack.complete:
        line += f"\n       ! identity incomplete ({', '.join(stack.missing)}) — caching disabled"
    return line


def _health_preflight(base: str, timeout: float = 10.0) -> tuple[bool, str]:
    """(healthy, one-line detail) for `{base}/api/v1/health` — M148.

    Stdlib only: this runs before any probe, so it must not depend on a probe
    driver being importable. The latency is part of the answer, because a
    stack that answers /health slowly is the FIRST symptom of the pool
    exhaustion that took the API down on 2026-10-11.
    """
    import urllib.error
    import urllib.request

    url = f"{base}/api/v1/health"
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            elapsed = time.perf_counter() - started
            return response.status == 200, f"{url} -> {response.status} in {elapsed:.2f}s"
    except urllib.error.HTTPError as exc:  # 503 etc. — answered, but unwell
        elapsed = time.perf_counter() - started
        return False, f"{url} -> {exc.code} in {elapsed:.2f}s"
    except Exception as exc:  # noqa: BLE001 — any transport failure is "unwell"
        elapsed = time.perf_counter() - started
        return False, f"{url} -> {type(exc).__name__} in {elapsed:.2f}s"


def run(args: argparse.Namespace) -> int:
    def say(message: str) -> None:
        """Every human-readable line in this run.

        Suppressed under --json: that mode promises stdout is one JSON
        document, so progress, verdicts and the probe's own output all move to
        stderr-free silence rather than breaking the parser.
        """
        if not args.json:
            print(message, flush=True)

    suites = load_suites()
    specs = list(args.targets)
    if args.suite:
        specs = args.suite + specs
    if not specs:
        specs = [DEFAULT_SUITE if DEFAULT_SUITE in suites else next(iter(suites))]
    probes = expand_targets(specs, suites=suites)

    forwarded = list(args.arg or [])
    identity_args = _normalize_args(forwarded)
    forwarded_out = next(
        (
            token.partition("=")[2]
            for token in forwarded
            if token.partition("=")[0].lstrip("-") == "out"
        ),
        None,
    )
    base = args.base.rstrip("/")
    cache = cache_mod.EvidenceCache()
    using_cache = not args.no_cache

    stack: cache_mod.Stack | None = None
    if using_cache:
        stack = cache_mod.resolve_stack(base, commit=args.commit)
        say(_stack_line(stack, cache.root))
        if not stack.complete:
            using_cache = False
    elif args.commit:
        say(f"stack  commit={args.commit} (cache off)")

    # M148 probe standard: de-risk BEFORE driving the stack. Probes share the
    # owner's production deployment with a live user, and the 2026-10-11
    # outage (docs/01 § "Sessions and the event loop") happened inside a
    # window where probe traffic was running against it. A probe must never
    # be the thing that takes the stack down, so the battery refuses to start
    # against a stack that is already unwell, and every run prints the
    # /health latency it measured on the way in.
    if not args.list and not args.skip_health_check:
        healthy, detail = _health_preflight(base)
        say(f"health {detail}")
        if not healthy:
            print(
                f"refusing to run: {base or 'the stack'} is not healthy ({detail}). "
                "A probe drives a live stack; pointing it at an unwell one turns a "
                "diagnostic into a second incident. --skip-health-check overrides "
                "this on purpose.",
                file=sys.stderr,
            )
            return 2

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    if base:
        env["A11Y_BASE"] = base
        env["SITE_BASE"] = base

    results: list[Result] = []
    for script in probes:
        mode, env_var = target_mode(script)
        if mode == "none":
            results.append(
                Result(
                    probe=script.name,
                    status="skipped",
                    detail=(
                        "no --base and no A11Y_BASE/SITE_BASE — cannot target the deployed build"
                    ),
                )
            )
            continue

        identity = cache_mod.probe_identity(
            script, base=base, args=identity_args, deps=_shared_deps(script)
        )
        pid = cache_mod.probe_id(stack, identity) if stack else ""

        # `--list` FIRST: a dry run reports what the cache would do and writes
        # nothing — not even the report a real hit would restore. Anything else
        # makes `--list` a probe run with extra steps.
        if args.list:
            status, reason = (
                cache.explain(pid, max_age_hours=args.max_age_hours, commit=stack.commit)
                if (using_cache and stack is not None)
                else ("cache-off", "caching disabled")
            )
            results.append(
                Result(
                    probe=script.name,
                    status="cached" if status == "hit" else "pending",
                    detail=reason if status == "hit" else f"would run — {reason}",
                )
            )
            say(f"LIST   {script.name:<38} {status}: {reason}")
            continue

        if using_cache and stack is not None and not args.refresh:
            hit = cache.lookup(pid, max_age_hours=args.max_age_hours, commit=stack.commit)
            if hit:
                entry, path, why = hit
                status, evidence = cache_mod.materialize(entry)
                note = {
                    "ok": "report verified",
                    "restored": "report restored from cache",
                    "divergent": f"on-disk report belongs to another run — cached copy at {path}",
                    "no-report": "verdict only (report file not captured)",
                }[status]
                detail = (
                    f"{entry.get('summary') or 'recorded pass'} · "
                    f"recorded {entry.get('recorded_at')} · commit "
                    f"{str(entry.get('commit'))[:7]} ({entry.get('commit_trust')})"
                )
                recorded_counts = entry.get("passed"), entry.get("failed")
                counts = (
                    (int(recorded_counts[0]), int(recorded_counts[1]))
                    if isinstance(recorded_counts[0], int) and isinstance(recorded_counts[1], int)
                    else None
                )
                results.append(
                    Result(
                        probe=script.name,
                        status="cached",
                        detail=detail,
                        evidence=evidence or entry.get("report_path", ""),
                        duration_s=float(entry.get("duration_s") or 0.0),
                        counts=counts,
                        notes=[note, why] if why != "fresh" else [note],
                    )
                )
                if not args.quiet:
                    say(
                        f"CACHED {script.name:<38} {detail} · {note}"
                        + (f" · {why}" if why != "fresh" else "")
                    )
                continue

        argv = build_argv(script, base, mode, forwarded)
        if args.verbose or not args.quiet:
            target = base if mode == "arg" else f"{base} via {env_var}"
            say(f"RUN    {script.name:<38} → {target}")
        started_at = time.time()
        code, stdout, duration = _run_probe(
            script, argv, env, args.probe_timeout, echo=not args.quiet and not args.json
        )

        if code != 0:
            results.append(
                Result(
                    probe=script.name,
                    status="failed",
                    detail=f"exit {code}",
                    duration_s=duration,
                    exit_code=code,
                    notes=["failures are never cached — the next run re-measures"],
                )
            )
            say(
                f"FAIL   {script.name:<38} exit {code} ({duration:.0f}s) — "
                "not cached; the next run re-measures"
            )
            if args.quiet and not args.json and stdout.strip():
                # --quiet hides live output, never a failure's evidence.
                tail = "".join(stdout.splitlines(keepends=True)[-40:])
                say(f"---- tail of {script.name} ----\n{tail}---- end tail ----")
            continue

        entry = _cache_entry(
            script,
            base=base,
            identity_args=identity_args,
            stdout=stdout,
            exit_code=code,
            duration_s=duration,
            started_at=started_at,
            out_path=forwarded_out,
        )
        stored = None
        if using_cache and stack is not None:
            stored = cache.store(stack, identity, entry)
        counts = (
            (entry["passed"], entry["failed"])
            if isinstance(entry.get("passed"), int) and isinstance(entry.get("failed"), int)
            else None
        )
        verdict = (
            f"{counts[0]} passed, {counts[1]} failed" if counts else (entry["summary"] or "ok")
        )
        results.append(
            Result(
                probe=script.name,
                status="ran",
                detail=verdict,
                evidence=entry["report_path"],
                duration_s=duration,
                counts=counts,
                notes=(
                    [f"stored → {stored}"]
                    if stored
                    else [
                        "not stored: identity incomplete"
                        if stack and not stack.complete
                        else "not stored (cache off/refresh)"
                    ]
                ),
            )
        )
        say(
            f"RAN    {script.name:<38} {verdict} ({duration:.0f}s)"
            + (f" → {entry['report_path']}" if entry["report_path"] else "")
        )

    return _report(results, args)


def _shared_deps(script: Path) -> list[Path]:
    """Shared machinery whose bytes change every probe's answer.

    `a11y_driver.py` drives every probe, and axe-core is the scanner behind the
    "0 critical + 0 serious" lines — a version bump there changes verdicts
    without any probe file changing, so both are part of the key. The axe
    manifest is hashed as-is: its bytes change only with the package version.
    """
    deps: list[Path] = []
    driver = PROBE_DIR / "a11y_driver.py"
    if driver.exists() and driver != script:
        deps.append(driver)
    axe = REPO_ROOT / "frontend" / "node_modules" / "axe-core" / "package.json"
    if axe.exists():
        deps.append(axe)
    return deps


def _report(results: Sequence[Result], args: argparse.Namespace) -> int:
    cached = [r for r in results if r.status == "cached"]
    ran = [r for r in results if r.status == "ran"]
    failed = [r for r in results if r.status == "failed"]
    skipped = [r for r in results if r.status == "skipped"]
    pending = [r for r in results if r.status == "pending"]
    if args.json:
        print(
            json.dumps(
                {
                    "probes": len(results),
                    "cached": len(cached),
                    "ran": len(ran),
                    "failed": len(failed),
                    "skipped": len(skipped),
                    "pending": len(pending),
                    "results": [vars(r) for r in results],
                },
                indent=2,
                default=str,
            )
        )
    else:
        if args.list:
            # LIST lines above already carry each probe's verdict; the summary
            # reports the run's cost instead of repeating them.
            print(
                f"summary: {len(results)} probes — {len(cached)} cached, "
                f"{len(pending)} would run, {len(skipped)} untargetable"
            )
        elif len(results) > 1 or args.verbose:
            print(
                f"summary: {len(results)} probes — {len(cached)} cached, {len(ran)} ran, "
                f"{len(failed)} failed, {len(skipped)} skipped"
            )
        # FAIL lines are printed the moment a probe dies (with its duration and
        # the not-cached rule); repeating them here would double every verdict.
        for result in skipped:
            print(f"SKIP   {result.probe:<38} {result.detail}")
    return 1 if failed else 0


def parse(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_probes.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="probe globs, paths, or suite names (default: suite `live`)",
    )
    parser.add_argument("--suite", action="append", help="named suite (repeatable)")
    parser.add_argument("--base", default=os.environ.get("PROBE_BASE", DEFAULT_BASE))
    parser.add_argument(
        "--commit",
        default=None,
        help="label the evidence with this commit (default: deploy host HEAD)",
    )
    parser.add_argument(
        "--arg",
        action="append",
        metavar="KEY=VALUE",
        help="forwarded to probes that declare --KEY (e.g. --arg width=390)",
    )
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_TTL_HOURS)
    parser.add_argument("--probe-timeout", type=float, default=DEFAULT_PROBE_TIMEOUT)
    parser.add_argument("--refresh", action="store_true", help="ignore cache, overwrite it")
    parser.add_argument("--no-cache", action="store_true", help="no lookup, no store")
    parser.add_argument("--list", action="store_true", help="resolve + report cache status only")
    parser.add_argument(
        "--skip-health-check",
        action="store_true",
        help="run even when GET /api/v1/health is unhealthy (M148 preflight override)",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="hide probe output (kept on failure)")
    parser.add_argument("--json", action="store_true", help="machine-readable summary")
    args = parser.parse_args(argv)
    if args.quiet and args.json:
        args.verbose = False
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse(argv)
    if not args.list:
        refusal = probe_target.check_target(args.base, who="run_probes.py")
        if refusal is not None:
            print(refusal, file=sys.stderr)
            return 2
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
