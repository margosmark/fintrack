"""fintrackerctl — the operator CLI (AGENTS §4 maintenance surface).

`fintrackerctl provider-funds …` is the maintenance surface M168 promised in
place of a code change (docs/07 §Provider registry): list, edit, re-verify
and drop the coverage rows in `provider_fund_registry`. It never edits a
literal, because there are none left.

Run it wherever `DATABASE_URL` points at the deployment you mean:

    uv run fintrackerctl provider-funds list
    uv run fintrackerctl provider-funds list --family vanguard_ca --stale-days 180
    uv run fintrackerctl provider-funds set static_sector SAP Technology
    uv run fintrackerctl provider-funds verify vanguard_us VOT
    uv run fintrackerctl provider-funds reverify --older-than-days 180

`reverify` IS the quarterly sweep's entry point: it re-probes every
`verified` row older than the threshold (180 days by default) and rewrites
whatever verdict the issuer gives now — a fund that went dark flips to
`miss` on the spot instead of silently serving a stale endpoint.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from app.models.provider_registry import ProviderFundFamily, ProviderFundStatus
from app.services import market_data, provider_registry


def _family(value: str) -> ProviderFundFamily:
    try:
        return ProviderFundFamily(value)
    except ValueError as exc:
        choices = ", ".join(f.value for f in ProviderFundFamily)
        raise argparse.ArgumentTypeError(f"unknown family {value!r} (choose: {choices})") from exc


def _parse_mix(pairs: Sequence[str]) -> dict[str, str]:
    """`Technology=40.90 Healthcare=9.50` -> the stored decimal TEXT map."""
    mix: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--mix expects SECTOR=WEIGHT, got {pair!r}")
        sector, weight = pair.split("=", 1)
        try:
            from decimal import Decimal

            Decimal(weight)  # reject anything that is not a number before storing it
        except Exception as exc:  # the message is what matters
            raise SystemExit(f"--mix weight {weight!r} is not a decimal") from exc
        mix[sector.strip()] = weight.strip()
    return mix


def _rows_for(args: argparse.Namespace) -> list:
    return provider_registry.list_rows(
        family=args.family,
        status=args.status,
        session=None,
    )


def cmd_list(args: argparse.Namespace) -> int:
    rows = _rows_for(args)
    if args.stale_days is not None:
        cutoff_row_ids = {
            row.id for row in provider_registry.stale(older_than_days=args.stale_days)
        }
        rows = [row for row in rows if row.id in cutoff_row_ids]
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "family": str(row.family),
                        "ticker": row.ticker,
                        "status": str(row.status),
                        "port_id": row.port_id,
                        "venue_suffix": row.venue_suffix,
                        "endpoint": row.endpoint,
                        "sector": row.sector,
                        "sector_mix": row.sector_mix,
                        "verified_at": row.verified_at.isoformat() if row.verified_at else None,
                        "verified_by_run": row.verified_by_run,
                        "miss_count": row.miss_count,
                        "last_probe_at": (
                            row.last_probe_at.isoformat() if row.last_probe_at else None
                        ),
                    }
                    for row in rows
                ],
                indent=2,
            )
        )
        return 0
    if not rows:
        print("(no rows)")
        return 0
    print(f"{'FAMILY':<16} {'TICKER':<10} {'STATUS':<9} {'DETAIL':<46} VERIFIED_AT")
    for row in rows:
        detail = (
            row.port_id
            or row.endpoint
            or row.sector
            or (f"{len(row.sector_mix)} sectors" if row.sector_mix else "")
        )
        if row.status is ProviderFundStatus.miss:
            detail = f"miss x{row.miss_count}"
        verified = row.verified_at.date().isoformat() if row.verified_at else "-"
        print(
            f"{row.family!s:<16} {row.ticker:<10} {row.status!s:<9} "
            f"{str(detail)[:46]:<46} {verified}"
        )
    print(f"\n{len(rows)} row(s)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    row = provider_registry.get(args.family, args.ticker)
    if row is None:
        print(f"no row for {args.family} / {args.ticker}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "family": str(row.family),
                "ticker": row.ticker,
                "status": str(row.status),
                "port_id": row.port_id,
                "venue_suffix": row.venue_suffix,
                "endpoint": row.endpoint,
                "sector": row.sector,
                "sector_mix": row.sector_mix,
                "verified_at": row.verified_at.isoformat() if row.verified_at else None,
                "verified_by_run": row.verified_by_run,
                "miss_count": row.miss_count,
                "last_probe_at": row.last_probe_at.isoformat() if row.last_probe_at else None,
            },
            indent=2,
        )
    )
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    mix = _parse_mix(args.mix or ())
    existing = provider_registry.get(args.family, args.ticker)
    row = provider_registry.upsert(
        args.family,
        args.ticker,
        status=ProviderFundStatus.miss if args.miss else ProviderFundStatus.verified,
        run="cli:set",
        port_id=args.port_id or (existing.port_id if existing else None),
        venue_suffix=args.venue_suffix or (existing.venue_suffix if existing else None),
        endpoint=args.endpoint or (existing.endpoint if existing else None),
        sector=args.sector or (existing.sector if existing else None),
        sector_mix=mix or (existing.sector_mix if existing else None),
    )
    print(f"set {row.family}/{row.ticker} -> {row.status}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    if not provider_registry.delete(args.family, args.ticker):
        print(f"no row for {args.family} / {args.ticker}", file=sys.stderr)
        return 1
    print(f"removed {args.family}/{args.ticker}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Probe ONE ticker against its issuer and book the verdict."""
    ticker = args.ticker.strip().upper()
    if args.family in (ProviderFundFamily.static_sector, ProviderFundFamily.fund_sector_mix):
        print(
            "that family has no endpoint to probe — use `set` to edit it "
            "(hand-curated classification data, docs/07)",
            file=sys.stderr,
        )
        return 2
    # Delete first: `verify` means "ask AGAIN", and a `miss` row would
    # otherwise short-circuit the probe it exists to prevent (docs/07).
    provider_registry.delete(args.family, market_data.registry_key(args.family, ticker))
    verdict = market_data.probe_provider(args.family, ticker, run="cli:verify")
    if verdict is None:
        print(f"{args.family}/{ticker}: no verdict (issuer did not answer)", file=sys.stderr)
        return 1
    print(f"{args.family}/{ticker}: {verdict} via cli:verify")
    return 0 if verdict is ProviderFundStatus.verified else 1


def cmd_reverify(args: argparse.Namespace) -> int:
    """The quarterly sweep: re-probe stale `verified` rows (docs/07)."""
    targets = provider_registry.stale(older_than_days=args.stale_days, family=args.family)
    if not targets:
        print(f"nothing older than {args.stale_days} days")
        return 0
    probeable = [
        row
        for row in targets
        if row.family not in (ProviderFundFamily.static_sector, ProviderFundFamily.fund_sector_mix)
    ]
    if args.dry_run:
        for row in probeable:
            print(f"would re-probe {row.family}/{row.ticker} (verified {row.verified_at})")
        skipped = len(targets) - len(probeable)
        if skipped:
            print(f"{skipped} classification row(s) have no endpoint — nothing to probe")
        return 0
    flipped = 0
    for row in probeable:
        verdict = market_data.reverify_provider_row(row)
        if verdict is None:
            # No verdict is NOT a failure: the row keeps what it had, which
            # is the whole reason the sweep never deletes before probing.
            print(f"{row.family}/{row.ticker}: no answer — row unchanged")
            continue
        print(f"{row.family}/{row.ticker}: {verdict}")
        if verdict is ProviderFundStatus.miss:
            flipped += 1
    return 0 if flipped == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fintrackerctl", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    funds = sub.add_parser("provider-funds", help="the provider_fund_registry maintenance surface")
    actions = funds.add_subparsers(dest="action", required=True)

    p_list = actions.add_parser("list", help="list registry rows")
    p_list.add_argument("--family", type=_family, default=None)
    p_list.add_argument(
        "--status",
        choices=[s.value for s in ProviderFundStatus],
        default=None,
    )
    p_list.add_argument(
        "--stale-days",
        type=int,
        default=None,
        help="only rows verified more than N days ago (the sweep's own filter)",
    )
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_show = actions.add_parser("show", help="one row as JSON")
    p_show.add_argument("family", type=_family)
    p_show.add_argument("ticker")
    p_show.set_defaults(func=cmd_show)

    p_set = actions.add_parser("set", help="insert or edit a row by hand")
    p_set.add_argument("family", type=_family)
    p_set.add_argument("ticker")
    p_set.add_argument("--port-id")
    p_set.add_argument("--venue-suffix")
    p_set.add_argument("--endpoint")
    p_set.add_argument("--sector")
    p_set.add_argument(
        "--mix",
        action="append",
        metavar="SECTOR=WEIGHT",
        help="repeatable; fund_sector_mix rows only",
    )
    p_set.add_argument(
        "--miss",
        action="store_true",
        help="write a negative-cache row instead of a verified one",
    )
    p_set.set_defaults(func=cmd_set)

    p_verify = actions.add_parser("verify", help="probe one ticker now and book the verdict")
    p_verify.add_argument("family", type=_family)
    p_verify.add_argument("ticker")
    p_verify.set_defaults(func=cmd_verify)

    p_reverify = actions.add_parser(
        "reverify",
        help="the quarterly sweep: re-probe rows older than --older-than-days",
    )
    p_reverify.add_argument("--older-than-days", type=int, default=180, dest="stale_days")
    p_reverify.add_argument("--family", type=_family, default=None)
    p_reverify.add_argument("--dry-run", action="store_true")
    p_reverify.set_defaults(func=cmd_reverify)

    p_remove = actions.add_parser("remove", help="delete a row")
    p_remove.add_argument("family", type=_family)
    p_remove.add_argument("ticker")
    p_remove.set_defaults(func=cmd_remove)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover — `python -m app.cli`
    raise SystemExit(main())
