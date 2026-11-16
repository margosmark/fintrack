"""Redact session tokens out of a11y probe evidence BEFORE it is written.

WHY THIS EXISTS
---------------
`.gitignore` deliberately TRACKS `frontend/scripts/a11y/*.json` — the JSON a
probe writes is run evidence and ships with the repo (only `*.log` is
ignored). A probe mints a throwaway tenant, keeps the bearer token in memory
and echoes the login response back into a check detail, so the token rode
along with the evidence: gitleaks 8.30.1 found two live-format JWTs in
`m108-probe.json` and `m125-select-all-matched-probe.json` (pentest
t_bc155706 finding M1; fix card t_35764994).

CONTRACT
--------
Every probe that writes JSON evidence passes its object through `redact()`
at the write site — one call, at the single point where the bytes leave the
process:

    Path(args.out).write_text(json.dumps(redact(report), indent=2))

`redact()` rewrites ONLY strings that look like a JWT (eyJ<seg>.<seg>.<seg>)
and leaves everything else — statuses, counts, structure — byte-identical, so
the evidence still says exactly what the probe measured. It accepts any JSON
value (dict / list / tuple / str / other) and is safe to call on data that
holds no token at all. The replacement is <redacted-jwt>, which no secret rule matches.
"""

from __future__ import annotations

import re
from typing import Any

JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{8,}){2,}")
REDACTED = "<redacted-jwt>"


def redact_text(text: str) -> str:
    """Replace every JWT-shaped substring with <redacted-jwt>."""
    return JWT_RE.sub(REDACTED, text)


def redact(value: Any) -> Any:
    """Recursively redact JWT-shaped strings inside a JSON-able structure."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v) for v in value)
    return value
