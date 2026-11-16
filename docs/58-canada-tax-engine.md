# docs/58 — Canadian tax engine v1 (federal/provincial, registered accounts, CPP/OAS timing)

> **Status:** shipped (t_52ed8d01, M189). Client-only v1 — no server twin,
> no endpoints, no tables. Companion to `docs/27` (server tax projection) and
> the consumer seam for `docs/57` (retirement simulator). v2 scope (not here):
> corporate tax, dividends, RDTOH.

## Context

Kilter's moat is Canadian tax depth: brackets for every jurisdiction,
registered-account rules, CPP/OAS timing. This milestone adopts that depth as
**pure, table-driven, testable functions** in `frontend/src/lib/`, with every
figure traced to a primary source and the tax-year switch expressed as data
(`TAX_YEARS`), never as code branches.

## Files

| File | Role |
| --- | --- |
| `frontend/src/lib/canadaTaxTables.ts` | All data: `TAX_YEARS` (2025, 2026), `RRIF_FACTORS_BP`, `TFSA_ANNUAL_LIMITS`, `resolveTaxYear()`. Every table carries its source URLs. |
| `frontend/src/lib/canadaTaxEngine.ts` | All math: 17 runtime exports (class + 16 functions), integer-cents throughout. |
| `frontend/tests/canadaTaxEngine.test.ts` | 23 tests — brackets, BPA phase-out, ON/BC/AB/QC quirks, year switch, validation, gross-up. |
| `frontend/tests/canadaTaxRegistered.test.ts` | 37 tests — RRSP/TFSA/FHSA/RESP/CLB/RRIF/LIF/LIRA/spousal. |
| `frontend/tests/canadaTaxCppOas.test.ts` | 16 tests — CPP/OAS start-age factors, recovery tax, year switch. |

Rule 8: `docs/33` §Rule-8 inventory carries rows for all 17 exports (client-
only, lock-vacuous — the engine reads no rows and no `../api/` payloads).

## The model (stated once; tests pin it)

1. **Precision.** Money enters as dollars, converts immediately to **integer
   cents**; every intermediate is an integer. Rates are basis-point
   numerators divided once per line with **HALF_UP** rounding (a float
   `k + 0.5` is exact at these magnitudes, so HALF_UP is exact, not
   approximate). Inputs above $100,000,000 are refused
   (`input_too_large`) rather than risk a 2^53 overflow.
2. **Credits are BASIC PERSONAL AMOUNTS ONLY.** v1 applies no CPP, EI, age,
   spouse, employment, or other non-refundable credits — a documented
   limitation, not an accident: the figures run conservative-high (tax ≤
   reality's), and tests cross-check CRA's worked examples component-by-
   component, pinning the missing-credit delta as a bounded gap instead of
   hiding it.
3. **`taxableIncome` vs `netIncome`.** Credits and clawback thresholds use
   net income (line 23400 basis); brackets and the health premium use
   taxable income (line 26000 basis). `netIncome` defaults to
   `taxableIncome`.
4. **Tax-year resolution is data, not code.** `resolveTaxYear(year)` returns
   published tables for 2025/2026; an unpublished year returns the latest
   tables with `usedFallback: true`.

## Data tables (sources verified reachable 2026-11-10)

| Table | 2026 figure (headline) | Primary source |
| --- | --- | --- |
| Federal brackets | 14% ≤ 58,523 · 20.5% ≤ 117,045 · 26% ≤ 181,440 · 29% ≤ 258,482 · 33% above | [current-year tax rates](https://www.canada.ca/en/revenue-agency/services/tax/individuals/tax-rates-brackets/current-year.html) |
| Federal BPA | 16,452 max → 14,829 floor, linear phase-out 181,440 → 258,482 (2.1%/yr slope) | [Form TD1-WS line 9](https://www.canada.ca/en/revenue-agency/services/forms-publications/td1-personal-tax-credits-returns/td1-forms-pay-received-on-january-1-2025-later/td1-ws.html) |
| 13 provinces/territories | brackets + flat BPA each | per-jurisdiction [T4032 January tables](https://www.canada.ca/en/revenue-agency/services/forms-publications/payroll/t4032-payroll-deductions-tables/t4032on-jan/t4032on-january-general-information.html) (the index page carries every jurisdiction) + [TD1 forms](https://www.canada.ca/en/revenue-agency/services/forms-publications/td1-personal-tax-credits-returns/td1-forms-pay-received-on-january-1-later.html) |
| Ontario quirks | 2 surtax bands (20% over 5,818; 36% over 7,446), 6-band health premium (cap 900), low-income reduction `2 × personal − basic` | T4032-ON (worked example in tests) |
| British Columbia | tax reduction: $690 full ≤ 25,570, −3.56¢/$, nil at 44,952 | T4032-BC |
| Quebec | 14/19/24/25.75% brackets, BPA 18,952, **16.5% federal abatement** | [Revenu Québec rates](https://www.revenuquebec.ca/en/citizens/income-tax-return/completing-your-income-tax-return/income-tax-rates/) + [2026 employer kit BPA](https://www.revenuquebec.ca/en/businesses/source-deductions-and-employer-contributions/employers-kit/principal-changes-for-2026-employers-kit/) |
| Yukon | mirrors federal brackets **and** the BPA partial claim (only YT does) | T4032-YT + TD1YT-26 |
| RRSP | dollar limit 33,810 (2025: 32,490); 18% formula; $2,000 grace + 1%/month | [RRSP/RRIF limits](https://www.canada.ca/en/revenue-agency/services/tax/registered-plans-administrators/pspa/mp-rrsp-dpsp-tfsa-limits-ympe.html) + [deduction limit](https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/contributing-a-rrsp-prpp/contributions-affect-your-rrsp-prpp-deduction-limit.html) |
| TFSA | annual 7,000; cumulative 2009–2026 = 109,000 (2024 = 95,000) | [TFSA room](https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/tax-free-savings-account/contributing/calculate-room.html) |
| FHSA | 8,000/yr, 40,000 lifetime | [FHSA contributions](https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/first-home-savings-account/contributing-your-fhsa.html) |
| RESP/CESG/CLB | basic 20% on 2,500 (5,000 with catch-up room) + additional 20/10/0% on first 500 by income; $7,200 lifetime; CLB 500/100 to 2,000 by family-size thresholds | [CESG](https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/registered-education-savings-plans-resps/canada-education-savings-programs-cesp/canada-education-savings-grant-cesg.html) · [CLB](https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/registered-education-savings-plans-resps/canada-education-savings-programs-cesp/canada-learning-bond.html) |
| RRIF minimums | 1/(90−age) under 71; s.7308(4) factors 5.28% (71) … 18.79% (94); 20% from 95 | [ITR s. 7308](https://laws-lois.justice.gc.ca/eng/regulations/C.R.C.,_c._945/section-7308.html) |
| LIF maximum | annuity-due at 6%, n = 91−age → 7.25513% (65) … 100% (90); ≥ prior-year earnings | [FSRA LIF table](https://www.fsrao.ca/industry/pensions/regulatory-framework/guidance-pensions/life-income-fund-lif-and-locked-retirement-income-fund-lrif-maximum-annual-income-payment-amount-table) (formula reproduced to 5 dp) |
| LIRA unlock | age 55+, Ontario locked-in total < 40% of YMPE (29,840) | [FSRA small balance](https://www.fsrao.ca/media/8321/download) + [Ontario Reg. 909](https://www.ontario.ca/laws/regulation/900909) |
| CPP | YMPE 74,600; age-65 max 1,507.65/mo; −0.6%/mo early (floor 64%), +0.7%/mo late (cap 142%) | [CPP rates](https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/payroll-deductions-contributions/canada-pension-plan-cpp/contribution-rates-maximums-exemptions.html) + [ESDC Q4 2026](https://www.canada.ca/en/employment-social-development/programs/pensions/pension/statistics/2026-quarterly-october-december.html) + [when to start](https://www.canada.ca/en/services/benefits/publicpensions/cpp/when-start.html) |
| OAS | Q4 762.50/mo (65–74); recovery 15% over 95,323 capped at OAS; deferral +0.6%/mo (cap 136%) | [OAS payments](https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/payments.html) · [recovery tax](https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/recovery-tax.html) · [ESDC Q4 2026](https://www.canada.ca/en/employment-social-development/programs/pensions/pension/statistics/2026-quarterly-october-december.html) |

Adding a tax year = one new entry in `TAX_YEARS` (brackets, BPA, quirks,
registered limits, CPP/OAS). No function changes.

## Interfaces (the exported contract)

```ts
// Seam for docs/57's retirement simulator — structurally satisfies its
// TaxEngine (id + label + grossWithdrawalFor) without importing it.
interface CanadaTaxEngine {
  readonly id: "ca-engine";
  readonly label: string;                       // "Canadian tax — ON"
  grossWithdrawalFor(netSpend: number, ctx: CanadaTaxEngineContext): number;
}
interface CanadaTaxEngineContext { year: number; age?: number; province?: ProvinceCode; otherIncome?: number }
function caTaxEngine(options?: { province?: ProvinceCode }): CanadaTaxEngine; // default "ON"

// Income tax
function incomeTax(input: TaxInput): TaxBreakdown;
interface TaxInput  { taxableIncome: number; netIncome?: number; province: ProvinceCode; year?: number }
interface TaxBreakdown { federal: number; provincial: number; total: number; province: ProvinceCode; year: number; usedFallbackYear: boolean }

// Net-to-gross: minimal G ≥ 0 with G − (tax(other+G) − tax(other)) ≥ netSpend,
// exact by integer bisection over the cent grid (not an approximation loop).
function grossWithdrawalFor(netSpend: number, ctx: WithdrawalContext & { province: ProvinceCode }): number;

// CPP/OAS (ages are integers; unpublished years freeze at the latest table)
function cppRetirementMonthly(input: { startAge: 60|…|70; year?: number }): number;
function oasMonthly(input: { startAge: 65|…|70; year?: number }): number;
function oasRecoveryTax(input: { netIncome: number; annualOas: number; year?: number }): number;

// Registered accounts (room functions REFUSE unpublished years — see below)
function rrspRoom(input: { earnedIncome: number; priorUnusedRoom?: number; pensionAdjustment?: number; year?: number }): number;
function rrspOverContribution(input: { contributions: number; deductionLimit: number }): { excess: number; taxableExcess: number; penaltyPerMonth: number };
function tfsaRoom(input: { contributions: number; withdrawals: number; year?: number }): number;
function fhsaRoom(input: { contributions: number; unusedRoom?: number; year?: number }): number;
function respCesg(input: { contribution: number; familyNetIncome: number; priorCesg?: number; unusedCesgRoom?: number; beneficiaryAge?: number; year?: number }): number;
function respClb(input: { familyNetIncome: number; childCount: 1|…|5; priorClb?: number; beneficiaryAge?: number; year?: number }): number;
function rrifMinimum(input: { age: number; value: number }): number;
function lifMaximumWithdrawal(input: { age: number; value: number; priorYearEarnings?: number }): number;
function liraSmallBalanceUnlock(input: { age: number; lockedInTotal: number; year?: number }): { eligible: boolean; limit: number };
function spousalRrspAttribution(input: { lastContributionYear: number; withdrawalYear: number }): boolean;
```

### Error codes (`CanadaTaxInputError`, `code` field)

| code | Raised by |
| --- | --- |
| `invalid_tax_input` | non-finite money input |
| `negative_amount` | any negative money input |
| `input_too_large` | > $100,000,000; a gross-up/LIF product that would exceed 2^53; or a gross-up whose doubling search would pass $100,000,000 — with current tables a net spend above **$25,000,000** (`grossWithdrawalFor`, measured 25,000,000 OK / 25,000,001 throws, projection years 2026–2090; docs/57 §4 clamps /retirement's withdrawal field at $10,000,000) |
| `province_required` / `unknown_province` | `incomeTax` / `grossWithdrawalFor` without a resolvable province |
| `tax_year_not_published` | room functions on an unpublished year; `tfsaRoom` before 2009; a TFSA year with no published limit |
| `cpp_start_age_out_of_range` / `oas_start_age_out_of_range` | non-integer or out-of-range start age |
| `invalid_age` | RRIF/LIF age outside [0, 130] or non-integer |
| `invalid_child_count` | `respClb` childCount outside 1–5 |

## Stated behaviors

- **Fallback vs refuse.** Income tax and CPP/OAS *estimate* on an
  unpublished year (latest tables; income tax flags `usedFallbackYear`,
  CPP/OAS dollar amounts are documented as frozen — they do not auto-index).
  Registered-account **room** REFUSES (`tax_year_not_published`): room cannot
  be estimated from stale limits. LIF/RRIF factors are statutory (not
  year-keyed) and take no year.
- **Quebec.** Federal tax is abated 16.5% *after* the federal credits;
  provincial follows Quebec's own brackets/BPA. No QPIP/QST modelling (v2).
- **Ontario.** Surtax applies to basic provincial tax (brackets − credit),
  the health premium to taxable income, and the low-income reduction to
  `min(basic, 2 × 300 − basic)` — floored at 0 (T4032-ON's rule, worked
  example pinned).
- **Yukon** mirrors the federal brackets and the TD1-WS BPA partial claim;
  every other jurisdiction publishes a flat BPA (verified across all 13 TD1s).
- **LIF** = max(prior-year earnings, C/F) with F = ä(91−age) at the 6% floor
  (5-decimal FSRA match at 65/71/75/80/90; the bond-rate floor above 6% is
  not modelled in v1). Reg. 909(4): if the RRIF-factor minimum exceeds this
  maximum, the minimum still must be paid — callers take `max(min, max)`.
- **Spousal RRSP.** Attributed while `withdrawalYear − lastContributionYear
  < 3` (contribution year + two preceding years).

## Relationship to the server

- **No server twin in v1.** `app/services/tax_projection.py` (docs/27) is
  untouched; no endpoint serves these figures. Parity (if ever wanted) is
  future work and is NOT claimed — see the docs/33 inventory disposition.
- **Wired into the retirement simulator (t_1ab83b11).**
  `frontend/src/pages/Retirement.tsx` constructs `caTaxEngine()` —
  `id: "ca-engine"`, default province **ON** (mirroring docs/27
  `DEFAULT_PROVINCE`) — and passes it as the `TaxEngine` seam of docs/57 §3,
  so every retirement step's withdrawal gross-up runs on this engine's
  tables, in the browser. The assumptions panel badge reads
  `Tax engine: Canadian tax — ON`. `flatRateTaxEngine` (docs/57 §3) stays
  exported as the seam's reference implementation and is still pinned by
  R5/R6, but the page no longer constructs it. The wiring adds no endpoint
  and no network call — the seam consumes `{age, year}` only — so the
  lock-vacuous disposition in the docs/33 inventory is unchanged by it.

## Verification

```bash
cd frontend
npx vitest run tests/canadaTaxEngine.test.ts tests/canadaTaxRegistered.test.ts tests/canadaTaxCppOas.test.ts   # 76 passed (23 + 37 + 16)
npx vitest run tests/lockedVaultDegradation.test.ts                                                             # 15 passed — rule 8 rows for all 17 exports
npx tsc --noEmit && npx eslint . --max-warnings 0 && npm run build                                               # gate sequence
```

What the tests pin (each docstring cites its URL):

- **Federal**: CRA's line-19 $7,000-on-$50,000 example reconstructed exactly;
  bracket-boundary cent quantization ($8,496.005 → $8,496.01 HALF_UP); the
  open 33% band; the BPA phase-out curve and $14,829 floor.
- **Ontario**: T4032-ON's worked example component-by-component (premium
  $600, surtax $0, reduction $0 exact; total delta from the published
  $2,377.20 bounded to (0, 1000) and equal to the missing-credit sum); the
  premium ladder incl. per-band caps; both surtax bands; the low-income
  reduction.
- **BC/AB/QC**: reduction-to-nil and phase-out; all six AB brackets at
  $400,000; QC abatement to the cent.
- **Year switch**: 2025 vs 2026 differ only through `TAX_YEARS`; 2031 flags
  `usedFallbackYear`.
- **Registered**: TFSA cumulative sums (2024 = 95,000 mid-check), RRSP
  formula + negative PSPA limits, FHSA caps, CESG 20/10/0% steps and
  lifetime cap, CLB thresholds (incl. inclusive boundary at exactly 58,523),
  s.7308 RRIF factors, FSRA LIF percentages to 5 dp, LIRA 40% YMPE gate,
  spousal 3-year boundary.
- **CPP/OAS**: 2026 and 2025 age-65 maxima, −36%/+42% and +36% factor
  derivations, recovery-tax 15% and cap, frozen-year behavior.
- **Gross-up**: `G − Δtax ≥ net` holds and `G − 0.01` fails (exact
  minimality), on top of other income, with a province override.

Running-stack smoke (this build host has no docker, so AGENTS §4's
`docker compose up -d --build` could not run — the M181 precedent): uvicorn
`app.main:app` on `127.0.0.1:8000` against an isolated database
(`fintrack_smoke_t52ed8d01`, `alembic upgrade head`), the built `dist`
served behind an `/api` proxy on `:4173`. `GET /api/v1/health` returns 200
`{"status":"healthy","checks":{"database":{"status":"healthy"}}}` through
both the backend directly and the proxy; headless Chrome
(`--headless=old`) renders the login page from the bundle
(`title=fin_tracker`, Email/Password inputs, screenshot `smoke52.png`).
The engine is a client-only module with no UI surface in v1 — it is
consumed by docs/57's simulator — so the smoke pins app boot + bundle
integrity while the engine itself is exercised by the 76 unit tests above.
