#!/usr/bin/env python3
"""Seed the M136 locked-fixture tenant (docs/33 §Server-first computing §0-a).

Run against a DEV database only — the tenant is a throwaway that exists to be
loaded while its vault is locked:

    cd backend
    DATABASE_URL=postgresql://... uv run python scripts/seed_locked_fixture.py

The seed is idempotent (a second run finds the user and returns it) and is
gated by `SEED_DEMO_ACCOUNT` (the same kill switch the demo seed uses) so it
cannot fire in a production-shaped environment.

What it produces, and why each half matters to the wave's gates:

    encryption_required = TRUE   the G1-G3 write guards are live, so a browser
                                 session against this tenant exercises the
                                 PRE-EMPTIVE gates (SplitEditor, bulk rename,
                                 YNAB commit) exactly as a real enrolled
                                 tenant would
    cipher rows, NO envelope     every browser starts LOCKED — there is no
                                 wrapped bundle anywhere to unlock with, so
                                 "load the locked path" needs no per-device
                                 enrollment dance (docs/33 §locked-fixture
                                 tenant)

After seeding, log in as the fixture in a browser (or point the QA harness at
it) and every `Withholds?=YES` surface renders its locked state: the shell
chip, the search popover, the S5 search view's withheld count, and the three
write-path gates.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session

from app.api.deps import get_engine
from app.services.locked_fixture import FIXTURE_EMAIL, ensure_locked_fixture_tenant


def main() -> int:
    engine = get_engine()
    with Session(engine, expire_on_commit=False) as session:
        user = ensure_locked_fixture_tenant(session)
    if user is None:
        print("SEED_DEMO_ACCOUNT is off — nothing seeded (kill switch honored).")
        return 1
    print(f"Locked fixture tenant ready: {FIXTURE_EMAIL} (id={user.id})")
    print("  encryption_required = TRUE")
    print("  envelope            = none (loads locked in every browser)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
