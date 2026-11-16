# Public-readiness: HEAD identifier scan + synthetic a11y evidence

## Context

fintrack publishes as a **squashed** public repo: one initial commit built
from HEAD, full history staying private (owner ruling, 2026-11-14). So the
publish gate is not "no secret in history" — it is **HEAD carries zero real
personal identifiers**, and every committed evidence artifact at HEAD is
synthetic or an explicitly classified fixture. Two tools/artifacts pin that:

1. `scripts/pii_scan.py` — the binary-inclusive identifier scan (this doc's
   §Tool).
2. The M118 probe evidence (`frontend/scripts/a11y/m118-value-*`) —
   regenerated against a synthetic tenant (this doc's §M118 evidence).

Card of record: `t_355d54e3` (purge) — its completion report carries the
scan output for the integrated tip.

## Approach — `scripts/pii_scan.py`

```
python3 scripts/pii_scan.py                 # scan HEAD
python3 scripts/pii_scan.py --rev <sha>     # scan an exact commit
python3 scripts/pii_scan.py --json out.json # machine-readable report
```

Reads **every tracked blob at the rev as bytes** (`git ls-tree` +
`git cat-file --batch` — no file is skipped as "binary", so PNG pixel data
and compressed JSON are searched too) and classifies every email-shaped
match:

| class | meaning | verdict |
| --- | --- | --- |
| `fixture` | address is on the allowlist below | counted, not fatal |
| `noise` | byte run inside compressed/encoded data whose domain labels are not lowercase (e.g. `Z@E[.]J[K]` inside a PNG's DEFLATE stream) — not a mailbox; every image family carrying one is vision-reviewed and the review is recorded on the purge card | counted, not fatal, **always printed** |
| `REAL` | everything else | **fatal — exit 1** |

Fixture allowlist (explicit; extend deliberately, every entry must be a
reserved/test domain or a host this repo's fixtures own):

* domains: `example.com`, `example.org`, `example.net`, `fintracker.local`,
  `synthetic.local`, `test.local`, `hermes.local`, `x.com`
* TLDs: `test`, `example`, `invalid`, `localhost`, `local` (RFC 2606/6761
  reserved — never routable mail)
* local parts: `noreply`, `no-reply`, `donotreply`

Also scanned, and **fatal** when present: IIN-gated payment-card shapes
(Visa/MasterCard/Amex/Discover + Luhn — the IIN gate is what keeps `uv.lock`
hashes and timestamps out of the verdict) and SSN shapes. Phone shapes, RFC1918
addresses and `/home/<user>` paths are **reported but not verdict-driving** —
topology is the license/topology scrub's lane (see `NOTICE`), and `/home/`
names the agent/container users, not the owner.

Exit codes: `0` zero real identifiers, `1` at least one, `2` usage/git error.

## Approach — M118 evidence is synthetic (probe defaults)

`frontend/scripts/a11y/m118_value_chart_probe.py` now defaults to a
**non-production** target and a **synthetic** fixture:

| arg | default | note |
| --- | --- | --- |
| `--base` | `http://localhost:8080` | classified LOCAL by `probe_target.py`; any non-local target exits 1 unless `PROBE_ALLOW_PROD=1` (fail-closed, card t_498845bf) |
| `--account` | `1` | never a production account id |
| `--deposit` / `--withdrawal` | `2026-01-12` / `2026-02-16` | invented marker dates |
| `--deposit-amount` / `--withdrawal-amount` | `12500.00` / `-4320.75` | invented marker amounts (parameterized in this card; the checks, the pin/tooltip matching and the data-table date all read the args) |

A production run must pass `--base`, `--account` and all four marker args
explicitly **and** export `PROBE_ALLOW_PROD=1`. The docstring carries the
synthetic re-run recipe: local stack (backend uvicorn + `npx vite`, whose
`vite.config.ts` proxies `/api`), disposable `@example.com` tenant, one
investment account with a `contribution` cash trade on `--deposit` for
`--deposit-amount` and a `withdrawal` cash trade on `--withdrawal` for
`--withdrawal-amount`, owner token minted with the app's own
`create_access_token(<user id>)` into `--token-file`.

The committed `m118-value-*.png` (8 shots) and `m118-value-chart-probe.json`
are the output of that synthetic run: base `http://localhost:5173`, account
`1`, events `2026-01-12 +12500.00` / `2026-02-16 -4320.75`, 22/22 checks,
6 axe runs at 0 critical/serious, 0 console errors.

## Touch points

* `scripts/pii_scan.py` (new) — the scan and its allowlist.
* `frontend/scripts/a11y/m118_value_chart_probe.py` — non-production
  defaults, parameterized marker amounts, synthetic-fixture recipe.
* `frontend/scripts/a11y/m118-value-*.png` + `m118-value-chart-probe.json` —
  regenerated synthetic evidence.
* NOT changed: `m130-demo-investments-390.png` and
  `m167-exposure-map-1440-dark.png` — their email-shaped byte matches are
  DEFLATE-stream noise (both PNGs carry only IHDR/IDAT/IEND, no text chunks),
  their pixels are demo-tenant fixtures (`demo@fintracker.local`,
  `demo_seed.py`'s account names), vision-reviewed on the purge card.

## Approach — real financial figures scrubbed (O2/N1, card t_29bfe235)

Owner ruling (2026-11-14, "Agree on 1 2 3 do it"): HEAD must carry no real
personal financial figures before the squash-publish. History is untouched (it
stays private) — this is a HEAD-content scrub replacing each figure with a
synthetic stand-in of the same shape, so every test keeps its intent (sign
tests still test signs, row counts/keys/signs unchanged, derived arithmetic in
the same docstring or assertion recomputed).

| category (real, pre-scrub) | synthetic (at HEAD) |
| --- | --- |
| account ids (the two real accounts) | `731` / `459` |
| EFT withdrawal figure | `-24,318.76` |
| value-event marker pair (deposit / withdrawal markers) | `+ $16,500.00` / `− $24,318.76` |
| CAD credit rows and the cash-gap legs | `+145.75`; `-582.73` vs `$0.27` (`583.00`) |
| USD round-trip legs | `+6,124.38` / `-6,124.18` |
| MSFT lot (share count @ price, book value, CAD value) | `40 sh @ 486.35`, `152.08`, ≈`27,430` |
| account-value chart marker (one account) | `$3,486.12` |
| YTD net-worth endpoints | `$914,726.53` / `$985,412.07` |
| TFSA acceptance pin (value + ≈ figure) | `286,114.92` / ≈`287,104.38` |
| deposit marker | `12,600` |
| both `m113_*_activity.csv` clips (real export rows: tickers, dates, amounts) | synthetic rows of identical shape — same 11/9 rows, activity keys, signs, field counts |
| m113 probe's watched account id | `--watch-account-id` (no default; the check records `SKIPPED` loudly when unset) |

The **byte-exact real → synthetic mapping** — every pre-scrub literal and its
replacement — deliberately does NOT ship in this tree: it lives outside the
published squash as evidence on private kanban cards — fix card `t_9bc30557`,
attachments `real_to_synthetic_mapping_partA.md` + `real_to_synthetic_mapping_partB.md`
(their concatenation, sha256
`c51d0f653bd9b295360f11d4011de97e3bcdea8e362077e08f86243385add798`) — and the
evidence already on parent card `t_29bfe235`. No pre-scrub figure is written
anywhere in this repository's HEAD.

**Sweep (acceptance evidence, attached to fix card `t_9bc30557`):** every
distinctive pre-scrub literal — extracted from the scrub diff's removed lines
plus the O2/N1 originals in every phrasing; the pattern list is held on the
card, not in this tree — swept over **every tracked blob at the tip, every
file, this document included**, two ways. With numeric-boundary matching (a
swept amount may not match inside a longer unrelated number) → **0 hits across
all tracked files**. Plain fixed-string `git grep -F` → only collisions that
are byte-identical at base `421ee985`, classified per line in the attachment:
demo-tenant fixture prices and a round-number demo range containing a swept
amount as a substring, an unrelated ACB total ending in one, and lockfile /
vector-JSON digit runs containing a swept bare number. The earlier
`sweep_after.txt` attached to card `t_29bfe235` predates this section and is
superseded: it never had docs/60 in scope, and the claim here describes only
the sweep attached to `t_9bc30557`, which ran against this committed text with
docs/60 inside its scope. Classified bare-number residues of that sweep —
classes, with every hit's reason in the attachment — include SVG/path drawing
coordinates (`worldMapData.ts`, `architecture.html`), a test count ("263
tests" in `NEXT_WAVE_PLAN.md`), keywalk indices, local dev ports, lockfile
hash/URL substrings, colour-hex values, timestamps and card ids, docs
performance references, and demo-tenant fixture row ids (`s2*-vectors.json`;
demo accounts are ids 1-6 in `demo_seed.py`).

## Verification

1. **Sweep at the tip** → boundary-matched 0 hits for every pre-scrub literal
   over all tracked files, docs/60 included; fixed-string pass shows only
   base-identical benign collisions (pattern list + full output + per-line
   classifications attached to fix card `t_9bc30557`). History untouched (this
   is a HEAD-content change only).
2. **Backend suite** at the tip: `cd backend && uv run pytest -q` → `0 failed`
   (the scrub touches `test_m131`, `test_m132`, `test_m113_ws_cash_mappings`,
   `test_m145`, `test_account_value_history`, `test_m144_deep_backfill` and the
   two fixture CSVs they read).
3. **Frontend gates**: `npx tsc --noEmit`, `npx eslint . --max-warnings 0`,
   `npm run build`, `npx vitest run` → all green (the m113/m118/m144 fixtures
   and the probe changed).
4. **Fresh-DB migration**: `alembic upgrade head` on an empty database — the
   `0047_trade_currency` change is docstring-only (no schema/behaviour edit).

## Verification (identifier scan)

1. `python3 scripts/pii_scan.py --rev <tip>` → `VERDICT: PASS` with
   `emails REAL: 0`, `luhn-valid card shapes: 0`, `ssn-shaped: 0`; the
   `noise` section names exactly the two PNGs above.
2. `cd frontend/scripts/a11y && python3 m118_value_chart_probe.py --base
   http://localhost:5173 --token-file <token>` against a synthetic stack →
   `22/22 checks passed`, exit 0 (the committed JSON's `checks` array is that
   run).
3. `python3 m118_value_chart_probe.py --base https://fintrac.princessandmark.com`
   with no `PROBE_ALLOW_PROD` → exits 1 with the `require_target` refusal
   before any tab is opened (and the bare no-arg run targets
   `http://localhost:8080`, never production).
4. Backend gate: `backend/tests/test_probe_target.py` (guard census, no
   fixture passwords in probes/evidence) stays green in `uv run pytest -q`.
