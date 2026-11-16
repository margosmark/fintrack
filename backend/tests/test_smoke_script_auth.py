"""Pin `scripts/smoke.sh` auth headers to shell variables, never literals (t_aac25d95).

Defect this pins (security gate t_1186a63b, blocking F1 on eba7ed9): the
EXIT-trap `cleanup()` shipped with a literal three-asterisk credential in its
DELETE /users/me header. The request went out unauthorized, the API answered
401, the trailing `|| true` swallowed it, and the throwaway production account
survived any crash between login and the happy-path delete — the exact leak
the trap was written to close (users.id 371).

Display pipelines MASK auth-header values (a correct line renders as three
asterisks — that is how the defect shipped), so nothing here is asserted by
reading a rendered line: the checks parse `Path.read_text()` bytes, and the
behavioural check EXECUTES the extracted trap with a stubbed `curl` and reads
the argv the DELETE really sends.

`test_a_literal_credential_*` re-injects the defect into a temp copy and
proves both checks go red on it, so this pin cannot rot into a tautology.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SMOKE = REPO / "scripts" / "smoke.sh"

# Spelled without a literal credential next to the scheme word: the write
# pipeline that authored the original defect rewrites that shape to `***`.
_SCHEME = "Bear" + "er"
_TOKEN_T2 = "$T" + "2"
_STARS = "*" * 3
_HEADER = "Authorization: " + _SCHEME + " "
_VAR_HEADER = re.compile(_HEADER + r"\$[A-Za-z_][A-Za-z0-9_]*")
_ANY_HEADER = re.compile(_HEADER + r"(\S*)")
_CLEANUP = re.compile(r"^cleanup\(\) \{.*?^\}", re.S | re.M)


def _header_tokens(text: str) -> list[tuple[int, str]]:
    """(line number, credential-as-written) for every auth-header line."""
    hits: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if _HEADER not in line:
            continue
        match = _ANY_HEADER.search(line)
        hits.append((number, match.group(1) if match else ""))
    return hits


def _literal_header_lines(text: str) -> list[int]:
    """Line numbers whose auth header is NOT a `$variable` expansion."""
    return [
        number
        for number, line in enumerate(text.splitlines(), 1)
        if _HEADER in line and not _VAR_HEADER.search(line)
    ]


def _cleanup_source(text: str) -> str:
    match = _CLEANUP.search(text)
    assert match, "cleanup() EXIT trap not found in scripts/smoke.sh"
    return match.group(0)


def _with_literal_credential(text: str) -> str:
    """Re-inject the shipped defect: the trap's header becomes a literal."""
    match = _CLEANUP.search(text)
    assert match, "cleanup() EXIT trap not found in scripts/smoke.sh"
    source = match.group(0)
    assert _TOKEN_T2 in source, "trap no longer expands $T2 — revisit this pin"
    mutated = source.replace(_TOKEN_T2, _STARS, 1)
    return text[: match.start()] + mutated + text[match.end() :]


def _run_cleanup(tmp_path: Path, token: str | None, source: str | None = None) -> list[str]:
    """Run the extracted trap under bash with curl/rm stubbed; return the curl argv.

    `token=None` models a crash before login (no session yet): the trap must
    then send nothing at all. `rm` is stubbed so a unit test never sweeps the
    host's /tmp while another battery runs.
    """
    trap = _cleanup_source(SMOKE.read_text()) if source is None else source
    argv_file = tmp_path / "curl_argv.txt"
    harness = tmp_path / "harness.sh"
    lines = [
        "set -u",
        f"""curl() {{ printf '%s\\n' "$@" > "{argv_file}"; }}""",
        "rm() { :; }",
        f"J={tmp_path / 'jar.txt'}",
        "BASE=http://smoke.invalid",
        "PASS=unit-test-password",
    ]
    if token is not None:
        lines.append(f"T2={token}")
    lines += [trap, "cleanup"]
    harness.write_text("\n".join(lines) + "\n")
    proc = subprocess.run(["bash", str(harness)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"harness failed: {proc.stderr}"
    return argv_file.read_text().splitlines() if argv_file.exists() else []


def test_every_auth_header_in_smoke_sh_expands_a_shell_variable() -> None:
    text = SMOKE.read_text()
    tokens = _header_tokens(text)
    assert len(tokens) >= 6, f"expected the documented auth-header lines, got {tokens}"
    literals = _literal_header_lines(text)
    assert literals == [], f"literal (non-variable) auth credential on line(s) {literals}"


def test_cleanup_trap_delete_carries_the_logged_in_token(tmp_path: Path) -> None:
    token = "unit-run-token-abc123"
    argv = _run_cleanup(tmp_path, token)
    assert _HEADER + token in argv, "trap DELETE must send the expanded $T2 value"
    assert not any(_STARS in arg for arg in argv), "trap must never send a literal mask"


def test_cleanup_without_a_session_sends_no_delete(tmp_path: Path) -> None:
    assert _run_cleanup(tmp_path, token=None) == []


def test_a_literal_credential_is_flagged_by_the_static_check() -> None:
    assert _literal_header_lines(SMOKE.read_text()) == [], "the real script is clean"
    mutated = _with_literal_credential(SMOKE.read_text())
    assert _literal_header_lines(mutated), "static check missed the re-injected defect"


def test_a_literal_credential_breaks_the_behavioral_check(tmp_path: Path) -> None:
    token = "unit-run-token-abc123"
    mutated = _with_literal_credential(SMOKE.read_text())
    argv = _run_cleanup(tmp_path, token, source=_cleanup_source(mutated))
    assert any(_STARS in arg for arg in argv), "harness did not capture the literal header"
    assert _HEADER + token not in argv, "the defect must make the good assertion fail"
