"""Behavioural tests for the per-commit live-probe evidence cache.

The tooling under test lives OUTSIDE `backend/` — `scripts/probe_cache.py` +
`scripts/run_probes.py` — but this is the repository's only Python test gate
(`pytest` runs here in AGENTS §4 and in CI), the modules are stdlib-only, and
the whole feature is a correctness claim ("a repeat run of the same committed
stack short-circuits with stored evidence, and nothing else ever does"), which
is exactly the kind of claim that must be executed rather than asserted in
prose. Nothing here touches the database; the session fixture owns that.

Covered:
  * store → hit → stale (age) → stale (date rollover) — freshness has two rules
  * a failure and an incomplete stack identity can never be stored
  * what invalidates a hit: probe bytes, shared driver bytes, base, args, build
  * what must NOT invalidate a hit: a moved commit LABEL on the same build —
    that is the churn this feature exists to kill
  * report materialisation: restored when missing, never overwritten when it
    belongs to a different run
  * stack resolution through the deploy host (injected ssh + fetcher), including
    the pull-without-rebuild trust verdict
  * the runner end to end with a fake probe: miss → run → hit → refresh →
    failure never cached
"""

from __future__ import annotations

import json
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import probe_cache as pc  # noqa: E402
import run_probes as rp  # noqa: E402


@pytest.fixture(autouse=True)
def _production_runner_targets_are_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests drive `rp.main` against production-classified bases
    (DEPLOY_BASE / https://stack.test) with fake probes, a stubbed stack
    resolution and --skip-health-check — no tenant, no network. The production
    gate itself is pinned in tests/test_probe_target.py; opting in here keeps
    this file about the evidence cache (card t_498845bf)."""
    monkeypatch.setenv("PROBE_ALLOW_PROD", "1")


DEPLOY_BASE = "https://fintrac.princessandmark.com"
# The runner tests below pin CACHE behaviour, never M148's live health gate:
# `rp.main` reaches production's /health unless told not to, which turns a
# unit test red on somebody ELSE's calendar — the deployed cert ended
# 2026-11-22, so every future-clock run (and every offline box, and any
# renewal hiccup) failed all five with `refusing to run` (exit 2). The gate
# itself is pinned hermetically in test_health_preflight_gate below.


def stack(**overrides: object) -> pc.Stack:
    values: dict = {
        "commit": "a" * 40,
        "commit_trust": "verified",
        "web": "sha256:webimageid+df61d470f883",
        "api": "sha256:apiimageid",
        "notes": ("test",),
        "authoritative": True,
    }
    values.update(overrides)
    return pc.Stack(**values)


def entry(exit_code: int = 0, *, when: datetime | None = None, **overrides: object) -> dict:
    stamp = when or datetime.now(UTC)
    record: dict = {
        "schema": pc.SCHEMA,
        "exit_code": exit_code,
        "recorded_at": stamp.isoformat(),
        "recorded_date": stamp.astimezone().date().isoformat(),
        "summary": "7 passed, 0 failed",
        "passed": 7,
        "failed": 0,
        "duration_s": 12.5,
        "report_path": "",
        "report": None,
        "probe": "fake_probe.py",
        "base": DEPLOY_BASE,
        "args": [],
    }
    record.update(overrides)
    return record


def test_store_then_lookup_roundtrip(tmp_path: Path) -> None:
    cache = pc.EvidenceCache(tmp_path, now=lambda: datetime.now(UTC))
    identity = "id-1"
    stored = cache.store(stack(), identity, entry())
    assert stored is not None and stored.exists()
    assert stored.parent.name == "a" * 40, "filed under its commit"

    hit = cache.lookup(pc.probe_id(stack(), identity), commit="a" * 40)
    assert hit is not None
    found, path, why = hit
    assert found["summary"] == "7 passed, 0 failed"
    assert path == stored
    assert why == "fresh"
    assert found["stack"]["authoritative"] is True


def test_failure_is_never_stored(tmp_path: Path) -> None:
    cache = pc.EvidenceCache(tmp_path)
    assert cache.store(stack(), "id-fail", entry(exit_code=1)) is None
    assert cache.lookup(pc.probe_id(stack(), "id-fail")) is None


def test_incomplete_identity_is_never_stored(tmp_path: Path) -> None:
    cache = pc.EvidenceCache(tmp_path)
    half = stack(web=pc.UNKNOWN)
    assert not half.complete
    assert cache.store(half, "id-unknown", entry()) is None
    # …and an unknown stack can never SERVE either: no id can be computed for it
    # that a complete stack would also produce.
    assert half.missing == ("web",)


def test_age_and_date_rollover_are_both_enforced(tmp_path: Path) -> None:
    # Pinned clock: the date-mismatch branches below need `now - 13h` to land on
    # the PREVIOUS local date, which a wall-clock `datetime.now` only does when
    # the suite runs before ~13:00 local — after that the 13h-old entry shares
    # today's date, lookup serves it "fresh", and this test fails on the clock.
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    cache = pc.EvidenceCache(tmp_path, now=lambda: now)
    identity = "id-age"

    # 13h old → too old even though it is the same local date in some zones.
    old = now - timedelta(hours=13)
    cache.store(stack(), identity, entry(when=old))
    pid = pc.probe_id(stack(), identity)
    found, _path, _why = cache.lookup(pid, max_age_hours=12) or (None, None, None)
    assert found is None, "an entry older than max_age_hours must not be served"

    # 2h old but recorded on YESTERDAY's date → date-relative assertions must
    # be re-measured (day-change windows, seeded dates).
    cache.clear()
    recent = now - timedelta(hours=2)
    yesterday = (recent.astimezone().date() - timedelta(days=1)).isoformat()
    cache.store(stack(), identity, entry(when=recent, recorded_date=yesterday))
    found, _path, _unused_why = cache.lookup(pid, max_age_hours=12) or (None, None, None)
    assert found is None, "evidence from another local date must not be served"
    _fresh, reason = pc.entry_is_fresh(
        entry(when=recent, recorded_date=yesterday), now=now, max_age_hours=12
    )
    assert "today is" in reason and yesterday in reason

    # …and with a raised max age the date rule still holds.
    _fresh, reason = pc.entry_is_fresh(
        entry(when=old, recorded_date=old.astimezone().date().isoformat()),
        now=now,
        max_age_hours=24,
    )
    assert "old" not in reason
    assert "today is" in reason or "recorded on" in reason


def test_probe_and_driver_edits_invalidate(tmp_path: Path) -> None:
    cache = pc.EvidenceCache(tmp_path)
    script = tmp_path / "probe.py"
    script.write_text("print('v1')\n")
    identity_v1 = pc.probe_identity(script, base=DEPLOY_BASE, args=[])
    cache.store(stack(), identity_v1, entry())

    hit = cache.lookup(pc.probe_id(stack(), identity_v1))
    assert hit is not None, "identical probe bytes must hit"

    script.write_text("print('v2')\n")
    identity_v2 = pc.probe_identity(script, base=DEPLOY_BASE, args=[])
    assert identity_v2 != identity_v1
    assert cache.lookup(pc.probe_id(stack(), identity_v2)) is None, (
        "editing the probe must invalidate its stored evidence"
    )

    # The shared driver rides along as a dependency.
    driver = tmp_path / "a11y_driver.py"
    driver.write_text("DRIVER = 1\n")
    with_driver = pc.probe_identity(script, base=DEPLOY_BASE, args=[], deps=[driver])
    assert with_driver != identity_v2
    driver.write_text("DRIVER = 2\n")
    assert pc.probe_identity(script, base=DEPLOY_BASE, args=[], deps=[driver]) != with_driver


def test_base_and_forwarded_args_change_the_key(tmp_path: Path) -> None:
    script = tmp_path / "probe.py"
    script.write_text("print('v1')\n")
    plain = pc.probe_identity(script, base=DEPLOY_BASE, args=[])
    assert pc.probe_identity(script, base="http://127.0.0.1:18080", args=[]) != plain
    assert pc.probe_identity(script, base=DEPLOY_BASE, args=["--width=390"]) != plain
    # …but order does not: the runner canonicalises before hashing.
    assert pc.probe_identity(script, base=DEPLOY_BASE, args=["--width=390"]) == (
        pc.probe_identity(script, base=DEPLOY_BASE, args=["--width=390"])
    )


def test_relabelled_commit_on_the_same_build_still_hits(tmp_path: Path) -> None:
    """The card's whole claim: a rebase / pull-without-rebuild is free."""
    cache = pc.EvidenceCache(tmp_path)
    identity = "id-relabel"
    before = stack(commit="b" * 40, commit_trust="checkout-ahead")
    cache.store(before, identity, entry(commit="b" * 40))

    after = stack(commit="c" * 40, commit_trust="checkout-ahead")
    assert before.key_parts() == after.key_parts(), "authoritative key ignores the label"

    hit = cache.lookup(pc.probe_id(after, identity), commit="c" * 40)
    assert hit is not None, "same build + same probe must short-circuit"
    found, path, why = hit
    assert path.parent.name == "b" * 40, "still filed under the commit that recorded it"
    assert "stack itself is unchanged" in why
    assert found["commit"] == "b" * 40, "the record keeps the label it was filed under"


def test_relabelled_commit_on_a_PREVIEW_stack_misses(tmp_path: Path) -> None:
    """A preview has no host to interrogate, so its commit stays in the key."""
    cache = pc.EvidenceCache(tmp_path)
    identity = "id-preview"
    cache.store(stack(commit="b" * 40, authoritative=False), identity, entry())
    moved = stack(commit="c" * 40, authoritative=False)
    assert moved.key_parts() != stack(commit="b" * 40, authoritative=False).key_parts()
    assert cache.lookup(pc.probe_id(moved, identity), commit="c" * 40) is None


def test_build_markers_change_the_key(tmp_path: Path) -> None:
    cache = pc.EvidenceCache(tmp_path)
    identity = "id-build"
    cache.store(stack(), identity, entry())
    rebuilt = stack(web="sha256:rebuiltimage+000000000000")
    assert cache.lookup(pc.probe_id(rebuilt, identity)) is None, (
        "a rebuilt container must invalidate every stored verdict"
    )
    assert cache.lookup(pc.probe_id(stack(api="sha256:other"), identity)) is None


def test_entries_and_clear_can_be_filtered_by_commit(tmp_path: Path) -> None:
    """`probe_cache.py ls --commit …` — the filtered glob is one level deep."""
    cache = pc.EvidenceCache(tmp_path)
    cache.store(stack(commit="a" * 40), "id-one", entry())
    cache.store(stack(commit="b" * 40), "id-two", entry())

    assert len(cache.entries()) == 2, "no filter sees every commit dir"
    only_a = cache.entries("a" * 40)
    assert [path.parent.name for path, _e in only_a] == ["a" * 40]
    assert len(only_a) == 1

    assert cache.clear("a" * 40) == 1
    assert len(cache.entries()) == 1, "clearing one commit keeps the others"
    assert [path.parent.name for path, _e in cache.entries()] == ["b" * 40]


def test_materialize_restores_and_never_overwrites(tmp_path: Path) -> None:
    report = tmp_path / "evidence.json"
    report.write_text('{"passed": 7}')
    record = entry(report_path=str(report), report='{"passed": 7}')

    assert pc.materialize(record) == ("ok", str(report))

    report.unlink()
    status, path = pc.materialize(record)
    assert (status, path) == ("restored", str(report))
    assert report.read_text() == '{"passed": 7}'

    report.write_text('{"passed": 9, "someone_elses": true}')
    status, path = pc.materialize(record)
    assert status == "divergent", "a different run's evidence must never be overwritten"
    assert report.read_text() == '{"passed": 9, "someone_elses": true}'

    gone = tmp_path / "deleted-tree" / "evidence.json"
    status, _path = pc.materialize(entry(report_path=str(gone), report="{}"))
    assert status == "no-report"
    assert not gone.exists(), "a cache hit must not re-create a deleted worktree"


def _ssh_lines(*, commit: str, commit_epoch: int, web_created: str, api_created: str) -> str:
    return "\n".join(
        [
            f"commit={commit}",
            f"commit_epoch={commit_epoch}",
            "web_image=sha256:webimage",
            "api_image=sha256:apiimage",
            f"web_created={web_created}",
            f"api_created={api_created}",
        ]
    )


def test_deploy_stack_resolution_reads_the_host(tmp_path: Path) -> None:
    commit = "d" * 40
    # Containers built AFTER the commit → the commit really names the build.
    fresh = _ssh_lines(
        commit=commit,
        commit_epoch=1_700_000_000,
        web_created="2023-11-15T00:00:05.000000000Z",
        api_created="2023-11-15T00:00:10.000000000Z",
    )
    calls: list[list[str]] = []

    def runner(cmd, _timeout):
        calls.append(list(cmd))
        return 0, fresh, ""

    resolved = pc.resolve_stack(
        DEPLOY_BASE,
        runner=runner,
        fetcher=lambda _url: b'const BUILD_ID = "abc123456789";',
        deploy_bases=(DEPLOY_BASE,),
        ssh_target="testhost",
        deploy_path="/srv/app",
    )
    assert resolved.complete
    assert resolved.authoritative is True
    assert resolved.commit == commit
    assert resolved.commit_trust == "verified"
    assert (
        resolved.web == "sha256:webimage+" + pc.sha256_hex(b'const BUILD_ID = "abc123456789";')[:12]
    )
    assert resolved.api == "sha256:apiimage"
    assert calls and calls[0][0] == "ssh" and "testhost" in calls[0]
    assert "/srv/app" in calls[0][-1], "the remote script must cd to the deploy path"


def test_pull_without_rebuild_is_labelled_not_trusted() -> None:
    """Commit NEWER than both containers: the box was pulled, never rebuilt."""
    resolved = pc.resolve_stack(
        DEPLOY_BASE,
        runner=lambda _cmd, _t: (
            0,
            _ssh_lines(
                commit="e" * 40,
                commit_epoch=1_800_000_000,
                web_created="2023-11-15T00:00:00.000000000Z",
                api_created="2023-11-15T00:00:01.000000000Z",
            ),
            "",
        ),
        fetcher=lambda _url: b"sw",
        deploy_bases=(DEPLOY_BASE,),
    )
    assert resolved.commit_trust == "checkout-ahead"
    assert resolved.complete, "the build is still identified — by its image ids"
    assert any("NEWER than both containers" in note for note in resolved.notes)


def test_explicit_commit_relabels_without_losing_authority() -> None:
    """`--commit` names the evidence; it never demotes the build's authority."""

    def runner(_cmd, _timeout):
        return (
            0,
            _ssh_lines(
                commit="f" * 40,
                commit_epoch=1_700_000_000,
                web_created="2023-11-15T00:00:05.000000000Z",
                api_created="2023-11-15T00:00:10.000000000Z",
            ),
            "",
        )

    kwargs = dict(
        runner=runner,
        fetcher=lambda _url: b"sw",
        deploy_bases=(DEPLOY_BASE,),
    )
    host_labelled = pc.resolve_stack(DEPLOY_BASE, **kwargs)
    caller_labelled = pc.resolve_stack(DEPLOY_BASE, commit="f" * 40, **kwargs)
    assert caller_labelled.commit == "f" * 40
    assert caller_labelled.commit_trust == "explicit"
    assert caller_labelled.authoritative is True
    assert caller_labelled.key_parts() == host_labelled.key_parts(), (
        "the label must not reach the key"
    )
    assert pc.probe_id(caller_labelled, "id") == pc.probe_id(host_labelled, "id")


def test_unreachable_deploy_host_means_no_identity() -> None:
    resolved = pc.resolve_stack(
        DEPLOY_BASE,
        runner=lambda _cmd, _t: (255, "", "connect to host testhost port 22: No route"),
        fetcher=lambda _url: b"sw",
        deploy_bases=(DEPLOY_BASE,),
    )
    assert not resolved.complete
    assert resolved.missing == ("commit", "web", "api")
    assert any("ssh failed" in note for note in resolved.notes)


def test_preview_stack_resolution_needs_no_deploy_host() -> None:
    resolved = pc.resolve_stack(
        "http://127.0.0.1:4173",
        runner=lambda _cmd, _t: (_ for _ in ()).throw(AssertionError("ssh must not be called")),
        fetcher=lambda _url: b"sw-bytes",
        deploy_bases=(DEPLOY_BASE,),
    )
    assert resolved.complete
    assert resolved.authoritative is False
    assert resolved.api == "local-preview"
    assert resolved.web == "content:" + pc.sha256_hex(b"sw-bytes")[:12]
    assert resolved.commit_trust in {"local", "explicit"}


def test_normalize_base_folds_scheme_defaults_and_paths() -> None:
    assert pc._normalize_base("https://fintrac.princessandmark.com") == (
        "fintrac.princessandmark.com",
        "",
    )
    assert pc._normalize_base("https://fintrac.princessandmark.com:443/dashboard") == (
        "fintrac.princessandmark.com",
        "",
    )
    assert pc._normalize_base("http://127.0.0.1:18080/") == ("127.0.0.1", "18080")
    assert pc._normalize_base(DEPLOY_BASE) == pc._normalize_base(DEPLOY_BASE + "/dashboard")


# --------------------------------------------------------------------------- #
# Runner: suite expansion, target detection, and the end-to-end short-circuit  #
# --------------------------------------------------------------------------- #


FAKE_PROBE = '''\
"""Fake probe: writes a report, prints the verdict, counts its executions."""
import argparse, json, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://127.0.0.1:9")
ap.add_argument("--out", default=str(HERE / "fake-probe-report.json"))
ap.add_argument("--width", type=int, default=1440)
args = ap.parse_args()

count = HERE / "runs.txt"
count.write_text(str((int(count.read_text()) if count.exists() else 0) + 1))

report = {"passed": 3, "failed": 0, "base": args.base, "width": args.width}
pathlib.Path(args.out).write_text(json.dumps(report))
print(f"PASS  fake check — {args.base}")
print(f"\\n3 passed, 0 failed -> {{args.out}}".replace("->", "\\u2192"))
sys.exit(0)
'''


FAKE_FAILING_PROBE = """\
import argparse, pathlib, sys
ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://127.0.0.1:9")
ap.add_argument("--out", default="unused.json")
args = ap.parse_args()
count = pathlib.Path(__file__).with_name("runs.txt")
count.write_text(str((int(count.read_text()) if count.exists() else 0) + 1))
print("1 passed, 1 failed")
sys.exit(1)
"""


@pytest.fixture()
def runner_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Isolated cache dir + a resolved stack with no network/ssh involved."""
    probe_dir = tmp_path / "probes"
    probe_dir.mkdir()
    monkeypatch.setenv("PROBE_EVIDENCE_DIR", str(tmp_path / "cache"))
    fake = stack()
    monkeypatch.setattr(rp.cache_mod, "resolve_stack", lambda *_a, **_k: fake)
    return {"dir": probe_dir, "cache": tmp_path / "cache", "stack": fake}


def _write_probe(path: Path, source: str) -> Path:
    path.write_text(textwrap.dedent(source))
    return path


def _runs(probe: Path) -> int:
    marker = probe.with_name("runs.txt")
    return int(marker.read_text() or "0") if marker.exists() else 0


def test_runner_miss_then_hit_then_refresh(
    runner_env: dict[str, object], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    probe_dir: Path = runner_env["dir"]  # type: ignore[assignment]
    # Deliberately OUTSIDE the probe directory: the runner forwards --out, so
    # it must know where the report landed without scanning the probe's folder.
    report = probe_dir.parent / "reports" / "fake-probe-report.json"
    report.parent.mkdir()
    probe = _write_probe(probe_dir / "fake_probe.py", FAKE_PROBE)
    args = [
        str(probe),
        "--base",
        DEPLOY_BASE,
        "--skip-health-check",
        "--arg",
        f"out={report}",
    ]

    first = rp.main(args)
    out = capsys.readouterr().out
    assert first == 0
    assert _runs(probe) == 1, "cold cache must actually run the probe"
    assert "RAN" in out and "3 passed, 0 failed" in out
    assert report.exists(), "the probe wrote its own evidence file"

    stored = list(Path(runner_env["cache"]).glob("commits/*/*.json"))  # type: ignore[arg-type]
    assert len(stored) == 1
    record = json.loads(stored[0].read_text())
    assert record["exit_code"] == 0 and record["passed"] == 3
    assert record["report_path"] == str(report)
    assert json.loads(record["report"])["passed"] == 3, "report bytes ride along"

    second = rp.main(args)
    out = capsys.readouterr().out
    assert second == 0
    assert _runs(probe) == 1, "repeat run of the same committed stack must NOT re-run"
    assert "CACHED" in out and "recorded" in out
    assert str(report) in out or report.exists()

    # The evidence file is gone (fresh worktree) → the hit restores it.
    report.unlink()
    third = rp.main(args)
    out = capsys.readouterr().out
    assert third == 0
    assert _runs(probe) == 1
    assert report.exists(), "stored evidence must be re-materialised"
    assert "restored" in out

    # --refresh forces a real re-run and re-files the evidence.
    fourth = rp.main([*args, "--refresh"])
    capsys.readouterr()
    assert fourth == 0
    assert _runs(probe) == 2, "--refresh must bypass the cache"


def test_runner_short_circuits_a_relabelled_commit(
    runner_env: dict[str, object], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """New commit label, same deployed build (rebase / pull, no rebuild) → HIT."""
    probe_dir: Path = runner_env["dir"]  # type: ignore[assignment]
    build = runner_env["stack"]
    probe = _write_probe(probe_dir / "relabel_probe.py", FAKE_PROBE)
    report = probe_dir / "relabel-report.json"

    def resolve(_base: str, *, commit: str | None = None, **_kw: object):
        if commit:
            # Exactly what the real resolver does with an explicit --commit:
            # relabel, keep the build markers and their authority.
            assert build is not None and build.authoritative  # type: ignore[union-attr]
            return pc.Stack(
                commit=commit,
                commit_trust="explicit",
                web=build.web,  # type: ignore[union-attr]
                api=build.api,  # type: ignore[union-attr]
                notes=("relabeled",),
                authoritative=True,
            )
        return build

    monkeypatch.setattr(rp.cache_mod, "resolve_stack", resolve)
    base_args = [str(probe), "--base", DEPLOY_BASE, "--skip-health-check", "--arg", f"out={report}"]

    assert rp.main([*base_args, "--commit", "c" * 40]) == 0
    capsys.readouterr()
    assert _runs(probe) == 1

    assert rp.main([*base_args, "--commit", "d" * 40]) == 0
    out = capsys.readouterr().out
    assert _runs(probe) == 1, "a moved commit label on the same build must not re-run"
    assert "CACHED" in out
    assert "stack itself is unchanged" in out, "the hit says WHY the label differs"


def test_list_is_read_only(runner_env: dict[str, object], capsys: pytest.CaptureFixture) -> None:
    """`--list` reports; it neither runs the probe nor restores the report."""
    probe_dir: Path = runner_env["dir"]  # type: ignore[assignment]
    report = probe_dir.parent / "reports" / "list-report.json"
    report.parent.mkdir()
    probe = _write_probe(probe_dir / "list_probe.py", FAKE_PROBE)
    args = [str(probe), "--base", DEPLOY_BASE, "--skip-health-check", "--arg", f"out={report}"]

    assert rp.main(args) == 0
    capsys.readouterr()
    assert report.exists()
    report.unlink()

    assert rp.main([*args, "--list"]) == 0
    out = capsys.readouterr().out
    assert "LIST" in out and "hit" in out
    assert _runs(probe) == 1, "a dry run must not execute the probe"
    assert not report.exists(), "a dry run must not write the report either"


def test_runner_never_caches_a_failure(
    runner_env: dict[str, object], capsys: pytest.CaptureFixture
) -> None:
    probe_dir: Path = runner_env["dir"]  # type: ignore[assignment]
    probe = _write_probe(probe_dir / "failing_probe.py", FAKE_FAILING_PROBE)
    args = [str(probe), "--base", DEPLOY_BASE, "--skip-health-check"]

    assert rp.main(args) == 1
    capsys.readouterr()
    assert rp.main(args) == 1
    out = capsys.readouterr().out
    assert _runs(probe) == 2, "a failure must re-run every time"
    assert "FAIL" in out
    assert list(Path(runner_env["cache"]).glob("commits/*/*.json")) == []  # type: ignore[arg-type]


def test_runner_skips_probes_it_cannot_target(
    runner_env: dict[str, object], capsys: pytest.CaptureFixture
) -> None:
    probe_dir: Path = runner_env["dir"]  # type: ignore[assignment]
    probe = _write_probe(
        probe_dir / "fixed_target.py",
        """
        import pathlib, sys
        # No argparse target at all: it can only ever hit its own address.
        pathlib.Path(__file__).with_name("runs.txt").write_text("ran")
        sys.exit(0)
        """,
    )
    assert rp.main([str(probe), "--base", DEPLOY_BASE, "--skip-health-check"]) == 0
    out = capsys.readouterr().out
    assert _runs(probe) == 0, "an untargetable probe must not be executed"
    assert "SKIP" in out and "cannot target the deployed build" in out


def test_suite_expansion_dedupes_and_orders(tmp_path: Path) -> None:
    probe_dir = tmp_path / "probes"
    probe_dir.mkdir()
    for name in ("m1_a_probe.py", "m2_b_probe.py", "m3_keyprobe.py"):
        (probe_dir / name).write_text("print()\n")
    suites = {"live": ["*_probe.py", "m1_a_probe.py"]}
    resolved = rp.expand_targets(["live", "m2_b_probe.py"], suites=suites, probe_dir=probe_dir)
    assert [p.name for p in resolved] == ["m1_a_probe.py", "m2_b_probe.py"]
    assert "m3_keyprobe.py" not in [p.name for p in resolved], (
        "`m3_keyprobe.py` is not `*_probe.py` — the glob is literal"
    )
    with pytest.raises(SystemExit):
        rp.expand_targets(["nope_*.py"], suites=suites, probe_dir=probe_dir)


def test_target_mode_reads_source_not_help(tmp_path: Path) -> None:
    arg_probe = _write_probe(
        tmp_path / "arg_probe.py",
        """
        import argparse
        ap = argparse.ArgumentParser(description="mentions --base in prose only")
        ap.add_argument("--base", default="x")
        """,
    )
    env_probe = _write_probe(
        tmp_path / "env_probe.py",
        'import os\nBASE = os.environ.get("A11Y_BASE", "http://localhost:4173")\n',
    )
    fixed_probe = _write_probe(tmp_path / "fixed_probe.py", "print('nothing here')\n")

    assert rp.target_mode(arg_probe) == ("arg", "")
    assert rp.target_mode(env_probe) == ("env", "A11Y_BASE")
    assert rp.target_mode(fixed_probe) == ("none", "")

    # The prose `--base` in the description must not be mistaken for a flag…
    assert rp.declares(arg_probe, "base") is True
    # …and a flag the probe does not declare is never forwarded.
    argv = rp.build_argv(arg_probe, DEPLOY_BASE, "arg", ["width=390", "out=/tmp/x.json"])
    assert argv == [sys.executable, str(arg_probe), "--base", DEPLOY_BASE]
    argv = rp.build_argv(
        _write_probe(
            tmp_path / "wide_probe.py",
            'import argparse\nap = argparse.ArgumentParser()\nap.add_argument("--width")\n',
        ),
        DEPLOY_BASE,
        "arg",
        ["width=390"],
    )
    assert argv[-2:] == ["--width", "390"]


def test_forwarded_out_is_not_part_of_the_identity(tmp_path: Path) -> None:
    """Two runs of the same probe to two files are the same evidence."""
    assert rp._normalize_args(["out=/a.json", "width=390"]) == ["--width=390"]
    assert rp._normalize_args(["width=390", "out=/a.json"]) == ["--width=390"]


def test_health_preflight_gate_is_pinned_without_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """M148's refuse-to-start gate, hermetic: 200 healthy; 503 / any transport
    failure unwell; and `run` turns unwell into exit 2 with the refusal.

    The runner tests skip the live preflight (see the DEPLOY_BASE note), so
    the gate keeps its coverage HERE against a stub instead of production's
    reachability and certificate calendar.
    """
    import urllib.error
    import urllib.request

    class _Ok:
        status = 200

        def __enter__(self) -> _Ok:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Ok())
    healthy, detail = rp._health_preflight("https://stack.test")
    assert healthy is True and "-> 200" in detail

    def _http_error(*a: object, **k: object) -> None:
        raise urllib.error.HTTPError("https://stack.test", 503, "unavailable", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", _http_error)
    healthy, detail = rp._health_preflight("https://stack.test")
    assert healthy is False and "-> 503" in detail

    def _transport(*a: object, **k: object) -> None:
        raise ConnectionError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", _transport)
    healthy, detail = rp._health_preflight("https://stack.test")
    assert healthy is False and "ConnectionError" in detail

    # The wiring: an unwell stack refuses to run (exit 2), never a probe run.
    probe = _write_probe(tmp_path / "gate_probe.py", FAKE_PROBE)
    monkeypatch.setattr(rp, "_health_preflight", lambda *a, **k: (False, "stub says unwell"))
    code = rp.main([str(probe), "--base", "https://stack.test"])
    err = capsys.readouterr().err
    assert code == 2 and "refusing to run" in err
    assert _runs(probe) == 0, "a refused battery must not execute any probe"
