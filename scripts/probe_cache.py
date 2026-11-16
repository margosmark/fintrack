"""Per-commit evidence cache for fin_tracker's live-probe battery.

WHY THIS EXISTS
---------------
`AGENTS.md` §4 step 5 verifies a change against the RUNNING stack with browser
probes (`frontend/scripts/a11y/*_probe.py`). Each probe is expensive — it
launches Chrome, seeds a throwaway tenant and drives the real UI — and the
question it answers is about **the build that is deployed**, not about the
branch in the working tree. A rebase rewrites local history and changes nothing
that is running, yet every rebase re-triggered 3-6 full probe suites against
the identical deployed build: pure churn, no new information.

This module makes a passing probe run reusable *per commit*. `run_probes.py`
looks up the evidence first and short-circuits with the stored verdict, report
and evidence path instead of launching Chrome again.

THE KEY (this is the part that has to be right)
-----------------------------------------------
Keying on the working tree would be wrong in both directions: a rebase moves
the working-tree SHA (so the cache would miss exactly when it should hit) and a
`git pull` on the deploy host moves it without rebuilding (so the cache would
claim a build that is not running — fabricated evidence). The key is the
deployed stack, read from where it actually lives:

    commit   deploy host `git rev-parse HEAD`   — the per-commit label
    web      image id of the web container  +  sha256 of the SERVED /sw.js
    api      image id of the api container

+ the probe's own identity (its bytes, `a11y_driver.py`, the axe-core version,
base URL and forwarded arguments). Three consequences, each deliberate:

  * rebase, no deploy        → stack unchanged → HIT (the churn this fixes)
  * `git pull`, no rebuild   → commit label moves, stack unchanged → HIT
                               (lookups match on probe id across commit dirs)
  * any rebuild (web or api) → an image id moves → MISS → re-run

If any member cannot be read (deploy host unreachable, /sw.js unfetchable) the
identity is *incomplete*: nothing is served from the cache and nothing is
stored. An unknown build must never be able to produce a hit.

WHAT IS STORED
--------------
Only PASSING runs (`exit 0`) — a failure is re-run every time, because the
whole point of a failure is to re-check whether it is still there. Each entry
holds the stack identity, the probe identity, the counts, the report JSON
(reported so a deleted evidence file can be restored) and the path it was
written to.

Freshness is deliberately strict, because every probe asserts date-relative
facts ("vs <date> close", day-change windows, seeded dates): an entry is
reused only if it was recorded TODAY (host-local date — the same clock the
probe uses) AND is younger than `max_age_hours` (default 12). A build that did
not change still serves different numbers tomorrow.

Layout:  <root>/commits/<commit>/<probe_id>.json
Default root: ~/.cache/fintrack-probe-evidence  (override: PROBE_EVIDENCE_DIR)

CLI:  python3 scripts/probe_cache.py ls [--commit SHA]
      python3 scripts/probe_cache.py clear [--commit SHA]
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "fintrack-probe-evidence"
SCHEMA = 1
UNKNOWN = "unknown"
# The identity member that means "the build behind this base" is a docker
# image built on the deploy host. A base not listed here is the operator's own
# preview (local vite, a worktree build) and gets commit + served-content
# identity instead.
DEFAULT_DEPLOY_BASES = (
    "https://fintrac.princessandmark.com",
    "http://127.0.0.1:18080",
    "http://localhost:18080",
)

Runner = Callable[[Sequence[str], float], tuple[int, str, str]]
Fetcher = Callable[[str], bytes]


def _run(cmd: Sequence[str], timeout: float) -> tuple[int, str, str]:
    """Default subprocess runner: (exit, stdout, stderr), never raising."""
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            list(cmd), capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout, proc.stderr


def _fetch(url: str) -> bytes:
    """Default HTTP fetcher (no auth: /sw.js is public)."""
    with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310 — caller-built URL
        return resp.read()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


#: Public alias — the runner hashes evidence reports with the same digest.
sha256_hex = _sha256


def _short(digest: str, n: int = 12) -> str:
    return digest[:n] if digest else UNKNOWN


def _normalize_base(base: str) -> tuple[str, str]:
    """`https://host:443/x` → (`host`, `""`) — scheme defaults folded away.

    Only the authority is compared, so a trailing path or a trailing slash
    cannot make a deploy base stop looking like the deploy base.
    """
    scheme, _, rest = base.partition("://")
    if not rest:
        scheme, rest = "", base
    authority = rest.split("/", 1)[0]
    if ":" in authority:
        host, _, port = authority.rpartition(":")
    else:
        host, port = authority, ""
    if port == {"https": "443", "http": "80"}.get(scheme.lower()):
        port = ""
    return host.lower().rstrip("."), port


@dataclass(frozen=True)
class Stack:
    """Identity of the build a probe is pointed at.

    `commit` is a LABEL for humans and for the storage layout; `web` and `api`
    are what actually decides whether the answer could have changed. `notes`
    records how each member was obtained (or why it is unknown) so a cached
    verdict can always say where its identity came from.

    `authoritative` separates the two worlds the runner must not confuse:
    a DEPLOY-backed stack reads its build markers off the host that runs the
    containers, so the commit can move (rebase, `git pull` without a rebuild)
    while the answer stays the same — the key then ignores the commit. A
    preview stack has no host to interrogate, so its commit is part of the key
    and nothing is assumed.
    """

    commit: str
    commit_trust: str
    web: str
    api: str
    notes: tuple[str, ...] = field(default_factory=tuple)
    authoritative: bool = False

    @property
    def complete(self) -> bool:
        return all(m not in ("", UNKNOWN) for m in (self.commit, self.web, self.api))

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, m in (("commit", self.commit), ("web", self.web), ("api", self.api))
            if m in ("", UNKNOWN)
        )

    def key_parts(self) -> dict[str, str]:
        """What the probe id is built from — see `authoritative`."""
        if self.authoritative:
            return {"web": self.web, "api": self.api}
        return {"commit": self.commit, "web": self.web, "api": self.api}

    def describe(self) -> str:
        return (
            f"commit={_short(self.commit, 7)} ({self.commit_trust}) "
            f"web={_short(self.web)} api={_short(self.api)}"
        )


def _ssh_deploy(
    base: str,
    *,
    runner: Runner,
    fetcher: Fetcher,
    ssh_target: str,
    deploy_path: str,
    web_container: str,
    api_container: str,
) -> Stack:
    """Read the deployed stack off the deploy host in ONE ssh round trip."""
    notes: list[str] = [f"deploy host {ssh_target}:{deploy_path}"]

    def _field(key: str, command: str) -> str:
        """`key=value` line; a command that fails prints `unknown`, never nothing."""
        return f'printf "{key}=%s\\n" "$({command} 2>/dev/null || echo unknown)"'

    image = "docker inspect -f '{{.Image}}'"
    created = "docker inspect -f '{{.Created}}'"
    remote = "\n".join(
        [
            f"cd {deploy_path} || exit 9",
            _field("commit", "git rev-parse HEAD"),
            _field("commit_epoch", "git log -1 --format=%ct"),
            _field("web_image", f"{image} {web_container}"),
            _field("api_image", f"{image} {api_container}"),
            _field("web_created", f"{created} {web_container}"),
            _field("api_created", f"{created} {api_container}"),
        ]
    )
    code, out, err = runner(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", ssh_target, remote], 40.0
    )
    fields: dict[str, str] = {}
    for line in out.splitlines():
        key, _, value = line.strip().partition("=")
        if key:
            fields[key] = value.strip()
    if code != 0 or not fields:
        notes.append(f"ssh failed (exit {code}): {(err or out).strip()[:160]}")
        return Stack(UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN, tuple(notes))

    commit = fields.get("commit", UNKNOWN)
    if commit in ("", "unknown"):
        notes.append("deploy host has no readable git HEAD")
        commit = UNKNOWN
    web_image = fields.get("web_image", UNKNOWN)
    api_image = fields.get("api_image", UNKNOWN)

    try:
        served = fetcher(base.rstrip("/") + "/sw.js")
        web = f"{web_image}+{_short(_sha256(served))}"
        notes.append("web = container image + sha256 of served /sw.js")
    except Exception as exc:  # noqa: BLE001 — any fetch failure means "unknown"
        web = UNKNOWN
        notes.append(f"served /sw.js unreadable: {type(exc).__name__}: {exc}")

    # Trust the commit as the build's label ONLY when the containers were
    # created at/after it. A checkout that is newer than its containers is the
    # pull-without-rebuild gap (fin-tracker skill): the box is set to a commit
    # the running stack has never seen, so the label stays marked, not trusted.
    trust = "explicit"
    epoch = fields.get("commit_epoch", "0")
    created = _earliest(fields.get("web_created", ""), fields.get("api_created", ""))
    try:
        epoch_ts = int(epoch)
    except ValueError:
        epoch_ts = 0
    if commit == UNKNOWN or created is None:
        trust = "unverified"
    elif epoch_ts and created >= epoch_ts:
        trust = "verified"
        notes.append("containers created at/after HEAD → commit names the running build")
    else:
        trust = "checkout-ahead"
        notes.append(
            "deploy checkout is NEWER than both containers (pulled, not rebuilt) → "
            "commit is a label only; the image ids below are what is running"
        )
    if UNKNOWN in (web_image, api_image):
        notes.append("a container image id is unreadable — identity incomplete")

    return Stack(
        commit,
        trust,
        web,
        api_image,
        tuple(notes),
        authoritative=True,
    )


def _earliest(*stamps: str) -> int | None:
    """Unix seconds of the EARLIEST parseable docker RFC3339 timestamp."""
    values: list[float] = []
    for stamp in stamps:
        try:
            values.append(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return int(min(values)) if values else None


def _local_repo_commit() -> str:
    code, out, _ = _run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], 15.0)
    return out.strip() if code == 0 and out.strip() else UNKNOWN


def resolve_stack(
    base: str,
    *,
    commit: str | None = None,
    runner: Runner = _run,
    fetcher: Fetcher = _fetch,
    deploy_bases: Sequence[str] | None = None,
    ssh_target: str | None = None,
    deploy_path: str | None = None,
    web_container: str | None = None,
    api_container: str | None = None,
) -> Stack:
    """Identity of the stack behind `base`.

    An explicit `--commit` / `PROBE_COMMIT` always wins as the label, but it
    never replaces the deploy host's build markers: the caller can name the
    commit, only the host can prove which bytes are running.
    """
    env = os.environ
    bases = tuple(deploy_bases if deploy_bases is not None else _deploy_bases(env))
    is_deploy = _normalize_base(base) in {_normalize_base(b) for b in bases}
    explicit = commit or env.get("PROBE_COMMIT") or None

    if is_deploy:
        stack = _ssh_deploy(
            base,
            runner=runner,
            fetcher=fetcher,
            ssh_target=ssh_target or env.get("PROBE_DEPLOY_SSH", "example.internal"),
            deploy_path=deploy_path or env.get("PROBE_DEPLOY_PATH", "/home/developer/fin_tracker"),
            web_container=web_container or env.get("PROBE_DEPLOY_WEB", "fin_tracker-web-1"),
            api_container=api_container or env.get("PROBE_DEPLOY_API", "fin_tracker-api-1"),
        )
        if explicit:
            # Same box, caller-named commit: keep the build markers (and their
            # authority) and relabel ONLY — otherwise the label would become
            # part of the key and a relabelled-but-identical build would pay
            # for a full re-run, which is the churn this exists to stop.
            return Stack(
                explicit,
                "explicit",
                stack.web,
                stack.api,
                stack.notes,
                authoritative=stack.authoritative,
            )
        return stack

    # Not a deploy-backed base: the operator's own preview. Commit comes from
    # the caller or this checkout; `web` is the content actually being served;
    # there is no deploy image to read, so `api` is a deliberately local value.
    notes = [f"non-deploy base {base} → commit + served content only"]
    if explicit:
        resolved, trust = explicit, "explicit"
    else:
        resolved, trust = _local_repo_commit(), "local"
        if resolved == UNKNOWN:
            notes.append("no --commit/PROBE_COMMIT and no readable git HEAD")
    try:
        web = "content:" + _short(_sha256(fetcher(base.rstrip("/") + "/sw.js")))
    except Exception as exc:  # noqa: BLE001 — unknown, never guessed
        web = UNKNOWN
        notes.append(f"served /sw.js unreadable: {type(exc).__name__}: {exc}")
    return Stack(resolved, trust, web, "local-preview", tuple(notes))


def _deploy_bases(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = env.get("PROBE_DEPLOY_BASES")
    if not raw:
        return DEFAULT_DEPLOY_BASES
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def probe_identity(
    script: Path,
    *,
    base: str,
    args: Sequence[str] = (),
    deps: Iterable[Path] = (),
) -> str:
    """Content identity of ONE probe run: what is probed, against what, how.

    `deps` carries the shared machinery a probe imports (the CDP driver, the
    axe-core build) — changing either changes the answer, so it changes the key.
    """
    digest = hashlib.sha256()
    for path in (script, *deps):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    digest.update(b"\0base=")
    digest.update(base.encode())
    for arg in args:
        digest.update(b"\0")
        digest.update(arg.encode())
    return digest.hexdigest()


def probe_id(stack: Stack, identity: str) -> str:
    """Filesystem key: same stack + same probe → same id, anything else → new id."""
    payload = json.dumps({"stack": stack.key_parts(), "probe": identity}, sort_keys=True)
    return _sha256(payload.encode())[:32]


def sanitize(name: str) -> str:
    """Make a commit-ish safe as a single path component (never absolute)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return cleaned or UNKNOWN


def now_utc() -> datetime:
    return datetime.now(UTC)


def entry_is_fresh(
    entry: dict,
    *,
    now: datetime,
    max_age_hours: float,
) -> tuple[bool, str]:
    """(fresh?, reason). Both rules are load-bearing — see the module docstring.

    A passing verdict, recorded today (host-local date, the clock the probe's
    date-relative assertions run on) and younger than `max_age_hours`.
    """
    if entry.get("schema") != SCHEMA:
        return False, f"schema {entry.get('schema')} != {SCHEMA}"
    if entry.get("exit_code") != 0:
        return False, "recorded run did not pass"
    recorded = entry.get("recorded_at")
    try:
        stamp = datetime.fromisoformat(str(recorded))
    except ValueError:
        return False, "unreadable recorded_at"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    age = now - stamp.astimezone(now.tzinfo or UTC)
    if age > timedelta(hours=max_age_hours):
        return False, f"{age.total_seconds() / 3600:.1f}h old (> {max_age_hours}h)"
    if str(entry.get("recorded_date")) != _local_date(now):
        return False, (
            f"recorded on {entry.get('recorded_date')}, today is {_local_date(now)} "
            "(date-relative assertions must be re-measured)"
        )
    return True, "fresh"


def _local_date(when: datetime) -> str:
    return when.astimezone().date().isoformat()


class EvidenceCache:
    """Filesystem store. Thread-irrelevant: probes run serially (one CDP port)."""

    def __init__(self, root: Path | None = None, *, now: Callable[[], datetime] = now_utc):
        env_root = os.environ.get("PROBE_EVIDENCE_DIR")
        self.root = Path(root or env_root or DEFAULT_CACHE_DIR).expanduser()
        self.now = now

    def path_for(self, commit: str, pid: str) -> Path:
        return self.root / "commits" / sanitize(commit) / f"{pid}.json"

    def lookup(
        self, pid: str, *, max_age_hours: float = 12.0, commit: str | None = None
    ) -> tuple[dict, Path, str] | None:
        """Freshest matching entry: (entry, path, reason).

        Matches by probe id across EVERY commit dir — the commit is a label,
        and a label move that did not rebuild (pull, rebase-and-push) must not
        cost a re-run. An entry that matches by id but is stale is returned as
        a `None` with no side effect; callers report the miss by running.
        """
        if not self.root.exists():
            return None
        best: tuple[dict, Path, int] | None = None
        for path in sorted(self.root.glob(f"commits/*/{pid}.json")):
            try:
                entry = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if entry.get("probe_id") != pid:
                continue
            fresh, _reason = entry_is_fresh(entry, now=self.now(), max_age_hours=max_age_hours)
            if not fresh:
                continue
            recorded = str(entry.get("recorded_at"))
            if best is None or recorded > str(best[0].get("recorded_at")):
                best = (entry, path, 0)
        if best is None:
            return None
        # Prefer an entry filed under the commit we are asking about.
        if commit:
            own = self.path_for(commit, pid)
            if own.exists():
                entry = json.loads(own.read_text())
                if entry.get("probe_id") == pid:
                    fresh, _ = entry_is_fresh(entry, now=self.now(), max_age_hours=max_age_hours)
                    if fresh:
                        return entry, own, "fresh"
            notes = (
                f"evidence filed under commit {_short(str(best[0].get('commit')), 7)}; "
                "the stack itself is unchanged"
            )
            return best[0], best[1], notes
        return best[0], best[1], "fresh"

    def store(self, stack: Stack, identity: str, entry: dict) -> Path | None:
        """File a passing run. Returns None (and stores nothing) when refused.

        Refusal reasons are the safety net: an incomplete identity or a
        non-passing run must never become a future "verified" verdict.
        """
        if not stack.complete:
            return None
        if entry.get("exit_code") != 0:
            return None
        pid = probe_id(stack, identity)
        stamp = self.now()
        record = {
            "schema": SCHEMA,
            "probe_id": pid,
            "identity": identity,
            "stack": {
                "commit": stack.commit,
                "commit_trust": stack.commit_trust,
                "web": stack.web,
                "api": stack.api,
                "authoritative": stack.authoritative,
            },
            "commit": stack.commit,
            "commit_trust": stack.commit_trust,
            "stack_notes": list(stack.notes),
            "recorded_at": stamp.isoformat(),
            "recorded_date": _local_date(stamp),
            **entry,
        }
        path = self.path_for(stack.commit, pid)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True))
        tmp.replace(path)
        return path

    def entries(self, commit: str | None = None) -> list[tuple[Path, dict]]:
        if not self.root.exists():
            return []
        found: list[tuple[Path, dict]] = []
        # `commits/<commit>/<probe_id>.json` — one level, so the filtered
        # glob must NOT carry the `*` the unfiltered one needs.
        pattern = f"commits/{sanitize(commit)}/*.json" if commit else "commits/*/*.json"
        for path in sorted(self.root.glob(pattern)):
            try:
                found.append((path, json.loads(path.read_text())))
            except (OSError, ValueError):
                continue
        return found

    def explain(
        self, pid: str, *, max_age_hours: float = 12.0, commit: str | None = None
    ) -> tuple[str, str]:
        """(status, reason) for a dry run — why this probe would or wouldn't hit.

        `hit` / `stale` / `miss` / `no-identity`: the reason is the point of
        `--list`, so an operator can see the cost of a run before paying it.
        """
        if not pid:
            return "no-identity", "stack identity incomplete — nothing can be served or stored"
        paths = sorted(self.root.glob(f"commits/*/{pid}.json")) if self.root.exists() else []
        if not paths:
            return "miss", "no evidence recorded for this probe on this stack"
        best: dict | None = None
        for path in paths:
            try:
                entry = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if entry.get("probe_id") != pid:
                continue
            if best is None or str(entry.get("recorded_at")) > str(best.get("recorded_at")):
                best = entry
        if best is None:
            return "miss", "no evidence recorded for this probe on this stack"
        fresh, reason = entry_is_fresh(best, now=self.now(), max_age_hours=max_age_hours)
        if fresh:
            detail = (
                f"fresh — recorded {best.get('recorded_at')} under commit "
                f"{_short(str(best.get('commit')), 7)}"
            )
            if commit and sanitize(str(best.get("commit"))) != sanitize(commit):
                detail += " (stack unchanged, commit label moved)"
            return "hit", detail
        return "stale", reason

    def clear(self, commit: str | None = None) -> int:
        removed = 0
        for path, _entry in self.entries(commit):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed


def materialize(entry: dict, *, allow_overwrite: bool = False) -> tuple[str, str]:
    """Make the stored report present at the path the probe wrote.

    Returns (status, path):
      ok          on-disk report already matches the cached bytes
      restored    file was missing — the cached report was written back
      divergent   file exists but holds a DIFFERENT run's report (a later run
                  for another stack). Never overwritten: overwriting would
                  silently rewrite someone else's committed evidence. The
                  cached copy stays retrievable from the cache.
      no-report   the run was cached as a verdict only (report not captured)
    """
    raw = entry.get("report")
    target = entry.get("report_path")
    if not isinstance(raw, str) or not isinstance(target, str) or not target:
        return "no-report", str(target or "")
    path = Path(target)
    if not path.exists():
        if not path.parent.exists():
            # Never re-create a deleted worktree/scratch tree from a cache hit:
            # the evidence is still retrievable where it was filed.
            return "no-report", str(path)
        path.write_text(raw)
        return "restored", str(path)
    on_disk = path.read_text()
    if on_disk == raw:
        return "ok", str(path)
    if allow_overwrite:
        path.write_text(raw)
        return "restored", str(path)
    return "divergent", str(path)


def parse_args(argv: Sequence[str] | None = None) -> tuple[str, list[str], bool]:
    """Tiny CLI: `ls` / `clear` with an optional `--commit SHA` filter."""
    args = list(sys.argv[1:] if argv is None else argv)
    command = "ls"
    commit: str | None = None
    if args and not args[0].startswith("-"):
        command = args.pop(0)
    for i, token in enumerate(args):
        if token == "--commit" and i + 1 < len(args):
            commit = args[i + 1]
        elif token.startswith("--commit="):
            commit = token.split("=", 1)[1]
        elif token in ("-h", "--help"):
            print(__doc__)
            raise SystemExit(0)
    if command not in {"ls", "clear"}:
        raise SystemExit(f"unknown command {command!r} (try: ls, clear)")
    return command, ([commit] if commit else []), command == "clear"


def main(argv: Sequence[str] | None = None) -> int:
    command, commits, is_clear = parse_args(argv)
    commit = commits[0] if commits else None
    cache = EvidenceCache()
    if is_clear:
        print(f"removed {cache.clear(commit)} entries from {cache.root}")
        return 0
    entries = cache.entries(commit)
    if not entries:
        print(f"no evidence under {cache.root}" + (f" for {commit}" if commit else ""))
        return 0
    print(f"{cache.root}: {len(entries)} entries")
    for path, entry in entries:
        fresh, reason = entry_is_fresh(
            entry, now=now_utc(), max_age_hours=float(os.environ.get("PROBE_CACHE_TTL_HOURS", "12"))
        )
        counts = (
            f"{entry.get('passed', '?')} passed, {entry.get('failed', '?')} failed"
            if "passed" in entry
            else "verdict only"
        )
        print(
            f"  {'HIT ' if fresh else 'stale'} {entry.get('probe', '?'):<38} "
            f"{counts:<24} commit={_short(str(entry.get('commit')), 7)} "
            f"({entry.get('commit_trust')}) {entry.get('recorded_at')}"
            + ("" if fresh else f" — {reason}")
        )
        print(f"       {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
