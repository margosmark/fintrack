#!/usr/bin/env python3
"""Production-target guard for probe tenants (card t_498845bf).

WHY THIS EXISTS
---------------
The a11y probes seed a throwaway tenant on whatever stack they are pointed at.
Pointed at production by default, ~250 of them accumulated in the production
database — created with passwords that are public in this repository, each
holding seeded fake financial rows (purged 2026-11-09 under Mark's explicit
GO). The contract now is:

    probe tenants are created against a LOCAL / scratch stack by default;
    a production target is allowed only behind the explicit env flag
    PROBE_ALLOW_PROD=1.

Every probe calls `require_target(BASE)` right after it resolves its target,
and `scripts/run_probes.py` calls `check_target()` before it launches
anything, so the rule holds for direct invocations and for the battery alike.

HOW A TARGET IS CLASSIFIED  (deliberately fail-closed)
-------------------------------------------------------
  * host `fintrac.princessandmark.com`            → production (the public URL)
  * loopback host (`localhost` / `127.0.0.1` / `::1` / `0.0.0.0`)
        → local, EXCEPT port 18080 (the documented example.internal→prod smoke tunnel,
          see the fin-tracker skill) which is production
  * anything else (non-loopback hosts such as example.internal, other domains) → production

Caveats stated honestly:
  * `http://localhost:8080` is classified LOCAL because AGENTS.md §4 step 5
    names it the local compose verify path. If you reach production through a
    loopback forward on a port that is not 18080, this guard cannot tell —
    set the flag deliberately for every production battery run.
  * The flag is inherited by subprocesses, so exporting it once covers the
    whole `run_probes.py` battery.
"""

from __future__ import annotations

import os
import secrets
import sys
from collections.abc import Mapping
from urllib.parse import urlparse

PROD_FLAG = "PROBE_ALLOW_PROD"
_FLAG_TRUTHY = frozenset({"1", "true", "yes", "on"})

PROD_HOSTS = frozenset({"fintrac.princessandmark.com"})
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
# Documented example.internal→prod smoke tunnel (fin-tracker skill). Loopback on any
# other port is a local stack.
PROD_TUNNEL_PORTS = frozenset({18080})

_REFUSAL = f"""\
%(who)s: refusing production target %(base)s.
  Probe tenants in the production database need an explicit opt-in:
      PROBE_ALLOW_PROD=1 %(who)s ...        # deliberate production run
  To target a local / scratch stack instead (no flag needed):
      --base http://127.0.0.1:<local-port>
  Rule from card t_498845bf: ~250 throwaway probe tenants with repo-public
  passwords had accumulated in the production DB; probes default local,
  production only behind %(flag)s."""


def flag_enabled(env: Mapping[str, str] | None = None) -> bool:
    """True when PROBE_ALLOW_PROD carries a truthy value."""
    source = os.environ if env is None else env
    return source.get(PROD_FLAG, "").strip().lower() in _FLAG_TRUTHY


def _host_port(base: str) -> tuple[str, int | None]:
    text = base.strip()
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    return (parsed.hostname or "").lower(), parsed.port


def is_production_target(base: str) -> bool:
    """Classify a probe base URL: True when it points at production."""
    host, port = _host_port(base)
    if host in PROD_HOSTS:
        return True
    if host in LOCAL_HOSTS:
        return port in PROD_TUNNEL_PORTS
    # Unknown host (LAN IP, another domain): assume production.
    return True


def check_target(base: str, *, who: str = "probe", env: Mapping[str, str] | None = None) -> str | None:
    """Return a refusal message when `base` is production without the flag, else None."""
    if not is_production_target(base):
        return None
    if flag_enabled(env):
        return None
    return _REFUSAL % {"who": who, "base": base, "flag": PROD_FLAG}


def require_target(base: str, *, who: str = "probe", env: Mapping[str, str] | None = None) -> None:
    """Exit(1) with the refusal message unless `base` is local or the flag is set."""
    message = check_target(base, who=who, env=env)
    if message is not None:
        sys.exit(message)


def random_password(tag: str) -> str:
    """Per-run unguessable probe password: ``<tag>-<16 url-safe chars>``.

    Replaces the committed fixture passwords (card t_32cd7b8e, step 2): a
    stranded probe tenant must not be reachable from repository content, so no
    password literal may live in a tracked file. Backend policy is min 8 /
    max 128 chars (backend/app/schemas/auth.py). Never write the value into
    evidence JSON — pass the mapping through ``strip_passwords()`` at the
    dump site instead.
    """
    return f"{tag}-{secrets.token_urlsafe(12)}"


def strip_passwords(mapping):
    """Copy of a seed/creds mapping with password-ish keys removed.

    Probe seed results flow into ``check(...)`` details and from there into
    committed evidence JSON; a per-run password written there would be a live
    credential in the repo (the rule redact.py applies to JWTs, t_32cd7b8e).
    Non-mappings pass through unchanged.
    """
    if not isinstance(mapping, Mapping):
        return mapping
    return {k: v for k, v in mapping.items() if k not in ("password", "pw")}


def main(argv: list[str] | None = None) -> int:
    """CLI: `python3 probe_target.py <base>` → 0 allowed, 1 refused (prints why)."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(f"usage: python3 probe_target.py <base-url>   ({PROD_FLAG}=1 opts in)", file=sys.stderr)
        return 2
    message = check_target(args[0], who="probe_target")
    if message is None:
        print(f"allowed: {args[0]} ({'production' if is_production_target(args[0]) else 'local'})")
        return 0
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
