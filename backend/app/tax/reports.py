"""Annual capital-gains summary — docs/06 Output section.

Per-security Schedule-3-style rows plus superficial-loss denial rows and an
explicit disclaimer, rendered as JSON / CSV / printable HTML. Reuses the ACB
engine's full-history computation; never duplicates gain math.
"""

from __future__ import annotations

import csv
import html
from decimal import Decimal
from io import StringIO
from typing import Any

from sqlmodel import Session

from app.services.reports import csv_safe
from app.tax.acb import TaxState, compute_user_tax_state

DISCLAIMER = "Informational only — not filed automatically."

_ITEM_NUMERIC_FIELDS = (
    "proceeds_cad",
    "acb_cad",
    "gain_loss_cad",
    "outlays_commissions_cad",
)


def capital_gains_report(session: Session, user_id: int, year: int) -> dict[str, Any]:
    """Build the annual report dict for `year` (JSON shape per docs/08 / w4-contract).

    Raw disposal results still contain any superficially-denied losses; adding
    the denied amount back removes it from THIS year's deductible result (a
    deferral resurfaces later via the acquiring pool's higher ACB), so the item
    rows sum to the totals exactly as they would be filed.
    """
    state: TaxState = compute_user_tax_state(session, user_id)

    denials = [d for d in state.denials if d.sale_date.year == year]
    denied_by_security: dict[int, Decimal] = {}
    for denial in denials:
        denied_by_security[denial.security_id] = (
            denied_by_security.get(denial.security_id, Decimal("0")) + denial.denied_loss_cad
        )

    grouped: dict[int, dict[str, Any]] = {}

    def _bucket(security_id: int, symbol: str) -> dict[str, Any]:
        return grouped.setdefault(
            security_id,
            {
                "security": symbol,
                "security_id": security_id,
                "symbol": symbol,
                **dict.fromkeys(_ITEM_NUMERIC_FIELDS, Decimal("0")),
            },
        )

    for disposal in state.disposals:
        if disposal.date.year != year:
            continue
        bucket = _bucket(disposal.security_id, disposal.symbol)
        bucket["proceeds_cad"] += disposal.proceeds_cad
        bucket["acb_cad"] += disposal.acb_cad
        bucket["outlays_commissions_cad"] += disposal.outlays_commissions_cad
        bucket["gain_loss_cad"] += disposal.gain_cad

    for roc in state.roc_gains:
        if roc.date.year != year or roc.excess_gain_cad == 0:
            continue
        _bucket(roc.security_id, roc.symbol)["gain_loss_cad"] += roc.excess_gain_cad

    items = list(grouped.values())
    for security_id, bucket in grouped.items():
        bucket["gain_loss_cad"] += denied_by_security.get(security_id, Decimal("0"))
    items.sort(key=lambda b: b["symbol"])

    totals = {name: sum((i[name] for i in items), Decimal("0")) for name in _ITEM_NUMERIC_FIELDS}

    return {
        "year": year,
        "items": items,
        "totals": totals,
        "denials": [
            {
                "security_id": d.security_id,
                "symbol": d.symbol,
                "sale_date": d.sale_date.isoformat(),
                "denied_loss_cad": d.denied_loss_cad,
                "outcome": d.outcome,
                "acquiring_account_id": d.acquiring_account_id,
            }
            for d in sorted(denials, key=lambda d: (d.sale_date, d.symbol))
        ],
        "disclaimer": DISCLAIMER,
    }


def _fmt(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):f}"


def report_csv(report: dict[str, Any]) -> str:
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["security", "proceeds_cad", "acb_cad", "gain_loss_cad", "outlays_commissions_cad"]
    )
    for item in report["items"]:
        writer.writerow(
            [
                csv_safe(item["security"]),
                _fmt(item["proceeds_cad"]),
                _fmt(item["acb_cad"]),
                _fmt(item["gain_loss_cad"]),
                _fmt(item["outlays_commissions_cad"]),
            ]
        )
    writer.writerow([])
    writer.writerow(["superficial_loss_denials"])
    writer.writerow(["security", "sale_date", "outcome", "detail", "denied_loss_cad"])
    for denial in report["denials"]:
        if denial["outcome"] == "deferral":
            detail = f"added to acquiring pool ACB (account {denial['acquiring_account_id']})"
        else:
            detail = "repurchase in registered account; denied with no add-back"
        writer.writerow(
            [
                csv_safe(denial["symbol"]),
                denial["sale_date"],
                denial["outcome"],
                detail,
                _fmt(denial["denied_loss_cad"]),
            ]
        )
    writer.writerow([])
    writer.writerow(["disclaimer", DISCLAIMER])
    return buf.getvalue()


def report_html(report: dict[str, Any]) -> str:
    e = html.escape

    def money(value: Decimal) -> str:
        return _fmt(value)

    item_rows = "\n".join(
        '<tr><td>{security}</td><td class="num">{p}</td><td class="num">{a}</td>'
        '<td class="num">{g}</td><td class="num">{o}</td></tr>'.format(
            security=e(i["security"]),
            p=money(i["proceeds_cad"]),
            a=money(i["acb_cad"]),
            g=money(i["gain_loss_cad"]),
            o=money(i["outlays_commissions_cad"]),
        )
        for i in report["items"]
    )
    total_row = (
        '<tr class="total"><td>Total</td>'
        + "".join(
            f'<td class="num">{money(report["totals"][field])}</td>'
            for field in _ITEM_NUMERIC_FIELDS
        )
        + "</tr>"
    )
    denial_rows = "\n".join(
        '<tr><td>{s}</td><td>{d}</td><td>{o}</td><td class="num">{a}</td></tr>'.format(
            s=e(d["symbol"]), d=d["sale_date"], o=e(d["outcome"]), a=money(d["denied_loss_cad"])
        )
        for d in report["denials"]
    )
    denials_section = (
        f"""
<h2>Superficial-loss denials</h2>
<table>
  <thead><tr><th>Security</th><th>Sale date</th><th>Outcome</th>
         <th>Denied loss (CAD)</th></tr></thead>
  <tbody>
{denial_rows}
  </tbody>
</table>"""
        if denial_rows
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Capital gains {report["year"]} — fin_tracker</title>
<style>
  body {{ font-family: Georgia, serif; margin: 3rem auto; max-width: 46rem; color: #111; }}
  h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.1rem; margin-top: 2rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border-bottom: 1px solid #ccc; padding: 0.35rem 0.6rem; text-align: left; }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  tr.total td {{ font-weight: bold; border-top: 2px solid #111; }}
  .disclaimer {{ margin-top: 2.5rem; font-style: italic; color: #444; }}
  @media print {{ body {{ margin: 0; }} }}
</style>
</head>
<body>
<h1>Capital Gains Summary {report["year"]}
<span style="font-weight:normal">(Schedule&nbsp;3 style)</span></h1>
<table>
  <thead>
    <tr><th>Security</th><th class="num">Proceeds (CAD)</th><th class="num">ACB (CAD)</th>
        <th class="num">Gain/Loss (CAD)</th><th class="num">Outlays/commissions (CAD)</th></tr>
  </thead>
  <tbody>
{item_rows}
{total_row}
  </tbody>
</table>
{denials_section}
<p class="disclaimer">{e(DISCLAIMER)}</p>
</body>
</html>
"""
