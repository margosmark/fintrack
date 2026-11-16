"""Behavioural tests for the production-target guard (frontend/scripts/a11y/probe_target.py).

Why they live here: backend pytest is this repository's only Python test gate
(same reasoning as tests/test_probe_cache.py), and the guard plus the runner
gate are stdlib-only. Nothing here touches the database or the network — the
runner cases use a nonexistent probe with --no-cache --skip-health-check, so
whatever happens after the gate is inert.

The contract under test (card t_498845bf): probe tenants default to local /
scratch stacks; a production target is refused unless PROBE_ALLOW_PROD=1 —
both for direct probe invocation and for `scripts/run_probes.py`.

Card t_32cd7b8e adds the credential half of the same exposure: per-run
random passwords (no fixture literal in tracked source), passwords kept out
of committed evidence, and a census that every base-resolving probe calls
require_target so a new probe cannot ship unguarded.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "frontend" / "scripts" / "a11y"))
sys.path.insert(0, str(REPO / "scripts"))

import probe_target as pt  # noqa: E402
import run_probes as rp  # noqa: E402

PROD_URL = "https://fintrac.princessandmark.com"

# Hermetic runner args: no cache (no ssh stack resolution), no health
# preflight (no network) — everything after the gate is inert.
HERMETIC_ARGS = ["--no-cache", "--skip-health-check"]

FAKE_PROBE = """
import argparse
import pathlib
import sys

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="x")  # declares a target: runner must run it
ap.parse_args()
pathlib.Path(__file__).with_name("runs.txt").write_text("ran")
sys.exit(0)
"""


@pytest.fixture()
def fake_probe(tmp_path: Path) -> Path:
    probe = tmp_path / "fake_probe.py"
    probe.write_text(FAKE_PROBE)
    return probe


@pytest.mark.parametrize(
    "base",
    [
        PROD_URL,
        PROD_URL + "/dashboard",
        "http://192.0.2.109:8080",  # example.internal LAN
        "http://example.internal:8080",
        "https://other.example.org",
        "http://127.0.0.1:18080",  # documented example.internal->prod tunnel
        "http://localhost:18080",
        "https://stack.test",  # fail-closed on unknown hosts
    ],
)
def test_production_classification(base: str) -> None:
    assert pt.is_production_target(base) is True


@pytest.mark.parametrize(
    "base",
    [
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:4899",
        "http://localhost:8080",  # AGENTS §4 step 5 local compose path
        "http://127.0.0.1:8000",
        "localhost:5173",  # bare, no scheme
        "http://[::1]:5173",
    ],
)
def test_local_classification(base: str) -> None:
    assert pt.is_production_target(base) is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        (" 1 ", True),
        ("0", False),
        ("", False),
        ("no", False),
        ("maybe", False),
    ],
)
def test_flag_truthiness(value: str, expected: bool) -> None:
    assert pt.flag_enabled({"PROBE_ALLOW_PROD": value}) is expected
    assert pt.flag_enabled({}) is False


def test_local_target_is_allowed_without_the_flag() -> None:
    assert pt.check_target("http://127.0.0.1:5173", who="unit") is None


def test_production_without_the_flag_refuses_and_says_why() -> None:
    message = pt.check_target(PROD_URL, who="unit", env={})
    assert message is not None
    assert PROD_URL in message
    assert "PROBE_ALLOW_PROD=1" in message
    assert "t_498845bf" in message


def test_production_with_the_flag_is_allowed() -> None:
    assert pt.check_target(PROD_URL, who="unit", env={"PROBE_ALLOW_PROD": "1"}) is None


def test_require_target_exits_on_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROBE_ALLOW_PROD", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        pt.require_target(PROD_URL, who="unit")
    assert "PROBE_ALLOW_PROD" in str(excinfo.value)


def test_require_target_passes_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROBE_ALLOW_PROD", raising=False)
    pt.require_target("http://127.0.0.1:5173", who="unit")  # must not raise


def test_flag_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROBE_ALLOW_PROD", "1")
    assert pt.check_target(PROD_URL, who="unit") is None


def test_cli_exit_codes(capsys: pytest.CaptureFixture) -> None:
    assert pt.main(["http://127.0.0.1:5173"]) == 0
    assert pt.main([PROD_URL]) == 1
    assert "PROBE_ALLOW_PROD" in capsys.readouterr().err
    assert pt.main([]) == 2


def test_runner_refuses_production_without_the_flag(
    fake_probe: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.delenv("PROBE_ALLOW_PROD", raising=False)
    code = rp.main([str(fake_probe), *HERMETIC_ARGS, "--base", PROD_URL])
    err = capsys.readouterr().err
    assert code == 2
    assert "refusing production target" in err
    assert "PROBE_ALLOW_PROD=1" in err
    assert not fake_probe.with_name("runs.txt").exists(), (
        "a refused battery must not execute the probe"
    )


def test_runner_refuses_the_example_internal_tunnel_port_without_the_flag(
    fake_probe: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PROBE_ALLOW_PROD", raising=False)
    assert rp.main([str(fake_probe), *HERMETIC_ARGS, "--base", "http://127.0.0.1:18080"]) == 2
    assert not fake_probe.with_name("runs.txt").exists()


def test_runner_lets_a_local_target_through(
    fake_probe: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.delenv("PROBE_ALLOW_PROD", raising=False)
    code = rp.main([str(fake_probe), *HERMETIC_ARGS, "--base", "http://127.0.0.1:9"])
    capsys.readouterr()
    assert code == 0, "a local target must not hit the production gate"
    assert fake_probe.with_name("runs.txt").exists(), "the probe should have run"


def test_runner_flag_opens_the_production_gate(
    fake_probe: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("PROBE_ALLOW_PROD", "1")
    code = rp.main([str(fake_probe), *HERMETIC_ARGS, "--base", PROD_URL])
    out, err = capsys.readouterr()
    assert code == 0, f"flagged run refused at the gate: {out}{err}"
    assert "refusing production target" not in err
    assert fake_probe.with_name("runs.txt").exists(), (
        "with the flag set the battery must actually run"
    )


# ------------------------------------------------------------------ t_32cd7b8e

A11Y_DIR = REPO / "frontend" / "scripts" / "a11y"
# Probes that resolve no target of their own: the CDP driver, the JWT
# redactor, the guard module, and press_regression (renders a file:// page —
# no server, no tenant). Everything else must call require_target.
GUARD_EXEMPT = frozenset({"a11y_driver.py", "redact.py", "probe_target.py", "press_regression.py"})
FIXTURE_PASSWORDS = ("correct-horse-battery", "Probe-pass-1!")


def test_random_password_is_per_run_and_within_policy() -> None:
    first, second = pt.random_password("m109"), pt.random_password("m109")
    assert first.startswith("m109-") and second.startswith("m109-")
    assert first != second, "two probe runs must never share a password"
    assert len(first) >= 16
    assert 8 <= len(first) <= 128, "backend password policy (schemas/auth.py)"
    assert "correct-horse" not in first


def test_strip_passwords_drops_credentials_and_keeps_the_rest() -> None:
    seeded = {
        "email": "m104-1@example.com",
        "password": "s3cret-value",
        "pw": "also-secret",
        "register": 200,
        "nested": {"password": "not-touched"},
    }
    cleaned = pt.strip_passwords(seeded)
    assert "password" not in cleaned and "pw" not in cleaned
    assert cleaned["register"] == 200 and cleaned["email"] == "m104-1@example.com"
    assert cleaned["nested"] == {"password": "not-touched"}, "top level only"
    assert seeded["password"] == "s3cret-value", "the input must not be mutated"
    assert pt.strip_passwords("not-a-mapping") == "not-a-mapping"


def test_every_base_resolving_probe_calls_require_target() -> None:
    missing = [
        path.name
        for path in sorted(A11Y_DIR.glob("*.py"))
        if path.name not in GUARD_EXEMPT and "require_target" not in path.read_text()
    ]
    assert missing == [], "these probes resolve a base but never gate production: " + ", ".join(
        missing
    )


def test_no_fixture_password_literal_in_tracked_probes() -> None:
    hits = [
        f"{path.name}: {fixture}"
        for path in sorted(A11Y_DIR.glob("*.py"))
        for fixture in FIXTURE_PASSWORDS
        if fixture in path.read_text()
    ]
    assert hits == [], "fixture passwords must not live in tracked probe source: " + ", ".join(hits)


def test_committed_evidence_json_carries_no_fixture_password() -> None:
    hits = [
        path.name
        for path in sorted(A11Y_DIR.glob("*.json"))
        if "correct-horse-battery" in path.read_text()
    ]
    assert hits == [], "committed evidence must not carry fixture passwords: " + ", ".join(hits)
