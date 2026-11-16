#!/usr/bin/env python3
"""Backstop sweep for throwaway probe tenants in the production database.

Card t_498845bf: ~250 `@example.com` probe tenants (repo-public passwords,
seeded fake financial rows) accumulated in production and were purged under an
explicit human GO. Two durable fixes keep them from coming back: the probes
now refuse production targets unless PROBE_ALLOW_PROD=1
(`frontend/scripts/a11y/probe_target.py`), and THIS script is the backstop for
runs that crashed before their own cleanup — dry-run by default.

The DELETE order is NOT hardcoded: it is derived from the live `pg_constraint`
graph (topological, children before parents, self-FKs ignored), so a table
added by a future migration is covered without editing this file. A cycle or
an unreachable table aborts before any statement runs (fail closed).

Guards before any COMMIT (apply mode): candidates left = 0, total users
changed by exactly the candidate count, demo tenant count unchanged. Any
guard failure rolls the whole transaction back. Prints counts and table names
only — never emails, passwords or tokens.

    cd backend
    uv run python scripts/purge_probe_tenants.py             # dry run (ROLLBACK)
    uv run python scripts/purge_probe_tenants.py --apply     # purge + COMMIT

From outside (the container owns DATABASE_URL and the app package):

    ssh example.internal 'cd /home/developer/fin_tracker && docker exec fin_tracker-api-1 \\
        python scripts/purge_probe_tenants.py                # dry run first'
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# `python scripts/purge_probe_tenants.py` puts scripts/, not backend/, at
# sys.path[0] — same guard the other scripts in this directory use. The app
# import stays inside main() so tests can import the planner with no app/env.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

# The predicate approved for the t_498845bf purge. The demo exclusion is
# belt-and-braces (demo is not @example.com, but never rely on that).
PREDICATE = "email ILIKE '%@example.com' AND email <> 'demo@fintracker.local'"
ROOT_TABLE = "users"

FK_QUERY = """
SELECT c.conrelid::regclass::text || '|' || a.attname || '|' ||
       c.confrelid::regclass::text || '|' || b.attname
FROM pg_constraint c
CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
LEFT JOIN LATERAL unnest(c.confkey) WITH ORDINALITY AS kk(attnum, ord)
       ON kk.ord = k.ord
JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
JOIN pg_attribute b ON b.attrelid = c.confrelid AND b.attnum = kk.attnum
WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace
ORDER BY c.conrelid::regclass::text, a.attname
"""


def load_fks(conn) -> list[tuple[str, str, str, str]]:
    """Foreign keys of the connected database as (child, col, parent, col)."""
    rows = conn.execute(text(FK_QUERY)).fetchall()
    return [tuple(row[0].split("|")) for row in rows]  # type: ignore[misc]


def _closure(fks: list[tuple[str, str, str, str]]) -> set[str]:
    """Tables that reach ROOT_TABLE through FK parents (plus ROOT_TABLE)."""
    by_child: dict[str, list[tuple[str, str, str]]] = {}
    for child, ccol, parent, pcol in fks:
        by_child.setdefault(child, []).append((ccol, parent, pcol))
    closure: set[str] = set()
    changed = True
    while changed:
        changed = False
        for child, conds in by_child.items():
            if child in closure:
                continue
            if any(p == ROOT_TABLE or p in closure for _, p, _ in conds):
                closure.add(child)
                changed = True
    closure.add(ROOT_TABLE)
    return closure


def plan_purge(
    fks: list[tuple[str, str, str, str]],
    *,
    predicate: str = PREDICATE,
    root: str = ROOT_TABLE,
) -> list[tuple[str, str]]:
    """Dependency-ordered (table, DELETE statement) pairs for the predicate.

    * a table with a direct FK to `users` is deleted by that FK alone — its
      other FKs are data links, never ownership (a join-style OR over a data
      FK would reach other tenants' rows);
    * a table without a `users` FK (join tables) is deleted when ANY of its
      FK sides belongs to a candidate, one level at a time, parents intact;
    * self-FKs are ignored for ordering (one DELETE per table is safe);
    * ordering: every referencing table is deleted before the table it
      references, so every subquery still reads live rows.
    """
    by_child: dict[str, list[tuple[str, str, str]]] = {}
    children: dict[str, set[str]] = {}
    for child, ccol, parent, pcol in fks:
        by_child.setdefault(child, []).append((ccol, parent, pcol))
        if child != parent:
            children.setdefault(parent, set()).add(child)

    closure = _closure(fks)
    if root not in closure:  # pragma: no cover — _closure always adds root
        raise ValueError(f"root table {root!r} missing from FK graph")

    def owned_where(table: str) -> str:
        conds = [
            f"{ccol} IN (SELECT id FROM {root} WHERE {predicate})"
            for ccol, parent, _ in by_child.get(table, [])
            if parent == root
        ]
        if not conds:
            raise ValueError(f"{table} has no ownership path to {root}")
        return conds[0] if len(conds) == 1 else "(" + " AND ".join(conds) + ")"

    def statement(table: str) -> str:
        if table == root:
            return f"DELETE FROM {root} WHERE {predicate}"
        conds = by_child.get(table, [])
        direct_root = [ccol for ccol, parent, _ in conds if parent == root]
        if direct_root:
            parts = [f"{ccol} IN (SELECT id FROM {root} WHERE {predicate})" for ccol in direct_root]
            return f"DELETE FROM {table} WHERE " + " OR ".join(parts)
        parts = [
            f"{ccol} IN (SELECT {pcol} FROM {parent} WHERE {owned_where(parent)})"
            for ccol, parent, pcol in conds
            if parent in closure and parent != table
        ]
        if not parts:
            raise ValueError(f"no candidate-owned path for join table {table}")
        return f"DELETE FROM {table} WHERE " + " OR ".join(parts)

    # Kahn, children before parents, alphabetical tie-break for determinism.
    pending = set(closure)
    order: list[str] = []
    while pending:
        ready = sorted(t for t in pending if not (children.get(t, set()) & pending))
        if not ready:
            raise ValueError(f"FK cycle among tables: {sorted(pending)}")
        for table in ready:
            order.append(table)
            pending.remove(table)
    return [(table, statement(table)) for table in order]


def run(engine, apply: bool) -> int:
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("SET lock_timeout = '5s'"))
            plan = plan_purge(load_fks(conn))
            where = f" FROM users WHERE {PREDICATE}"
            candidates_before = conn.execute(text("SELECT count(*)" + where)).scalar_one()
            total_before = conn.execute(text("SELECT count(*) FROM users")).scalar_one()
            demo_before = conn.execute(
                text("SELECT count(*) FROM users WHERE email = 'demo@fintracker.local'")
            ).scalar_one()

            if candidates_before == 0:
                trans.rollback()
                print("nothing to purge: 0 candidate tenants (aggregates only)")
                return 0

            print(
                f"{'APPLY' if apply else 'DRY RUN'}: {candidates_before} candidate tenant(s), "
                f"{total_before} user(s) total, {len(plan)} statement(s)"
            )
            total_rows = 0
            for table, stmt in plan:
                rows = conn.execute(text(stmt)).rowcount
                total_rows += rows
                print(f"  {table:<32} {rows}")

            candidates_left = conn.execute(text("SELECT count(*)" + where)).scalar_one()
            total_left = conn.execute(text("SELECT count(*) FROM users")).scalar_one()
            demo_left = conn.execute(
                text("SELECT count(*) FROM users WHERE email = 'demo@fintracker.local'")
            ).scalar_one()

            guards = {
                "candidates_left == 0": candidates_left == 0,
                "total changed by exactly the candidate count": total_left
                == total_before - candidates_before,
                "demo tenant count unchanged": demo_left == demo_before,
            }
            for name, ok in guards.items():
                print(f"  guard {'OK  ' if ok else 'FAIL'} {name}")

            if not all(guards.values()):
                trans.rollback()
                print("guard failed — ROLLED BACK, nothing persisted")
                return 1
            if apply:
                trans.commit()
                print(f"APPLIED: {total_rows} row(s) deleted across {len(plan)} table(s)")
            else:
                trans.rollback()
                print(f"DRY RUN: {total_rows} row(s) would be deleted — ROLLED BACK")
            return 0
        except BaseException:
            trans.rollback()
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="execute the purge (default: dry run + rollback)"
    )
    args = parser.parse_args(argv)

    # Lazy: the planner tests import this module without app/env loaded.
    from app.api.deps import get_engine

    return run(get_engine(), apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
