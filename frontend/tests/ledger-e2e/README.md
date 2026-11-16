# Ledger E2E (`frontend/tests/ledger-e2e/**`)

Deep behavioural browser coverage for the M185 Ledger redesign, executed against
the **running candidate stack** — the real production bundle served over HTTP
with a live uvicorn + seeded Postgres behind `/api`. No `TestClient`, no mocked
API, no fixture money: every assertion reads either the live DOM, the browser's
own network log, or the payload the server actually served.

Owner: `tester` (`docs/47` §12 ownership table). Nothing outside this directory
is edited by this suite.

## Prerequisites

```bash
# 1. backend — isolated DB, seeded demo tenant (never the suite DB, never prod)
cd backend
DATABASE_URL='postgresql+psycopg://fintrack:fintrack@localhost:5432/fintrack_e2e_<id>' \
JWT_SECRET='<random>' COOKIE_SECURE=false SEED_DEMO_ACCOUNT=true \
  <venv>/bin/alembic upgrade head
DATABASE_URL=... JWT_SECRET=... COOKIE_SECURE=false SEED_DEMO_ACCOUNT=true \
  <venv>/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

# 2. frontend — the candidate's production build, served with an /api proxy
cd frontend && npm run build
node tests/ledger-e2e/harness/serve.mjs 8100 8000

# 3. the suite
cd frontend && node tests/ledger-e2e/run.mjs --out tests/ledger-e2e/results.json
```

Chrome is launched by the harness itself (headless, ephemeral profile on a
scratch dir; `LEDGER_E2E_CHROME` overrides the binary). App/API origins can be
overridden with `--app` / `--api` or `LEDGER_E2E_APP` / `LEDGER_E2E_API` — the
flags are published into `LEDGER_E2E_APP/API` by the runner, because the
Node-side helpers resolve those variables at CALL time (a const snapshot made
the flags reach only the browser half, and every direct API call kept knocking
on the default `:8000`). `LEDGER_E2E_CDP_PORT` moves the debugging port when a
sibling lane already holds 9334.

The suite spends several `POST /auth/demo` calls per run (browser logins plus
the API cross-checks). The auth route is rate limited, so a re-run inside the
limiter window fails with `429 rate_limited` before any check executes — for a
disposable verification stack start uvicorn with `RATE_LIMIT_ENABLED=false`
(the flag is read live at startup; it is a real guard in production and is
never disabled there).

## The trusted press

`page.click` (harness/cdp.mjs) is a trusted `Input.dispatchMouseEvent` press at
the rect captured one eval before dispatch. That blind capture loses under
post-load layout shift: when a sibling card grows after its own query settles,
the press lands on a `<p>` and `requests=0` — the kit's marker for a trusted
press that never reached the control (not a product failure). The retry presses
use `page.pressValidated(selector)`: same trusted chain, but the rect is
re-captured immediately before dispatch and validated with `elementFromPoint`
(re-arms ~150ms apart inside `rearmMs` while layout still moves); suites pair
it with a ~1.2s settle after the panel mounts and one re-press when the
post-click request count is 0. Never press through an untrusted `el.click()`.

## What it covers (docs/47 §13 acceptance examples → suite)

| Suite | Examples | Notes |
| --- | --- | --- |
| `static-scan` | 16 | mockup-only copy absent from source **and** from the built bundle; no vendor tokens in user-facing string literals |
| `shell` | 2, 6 | demo banner verbatim + `role=status`, truth bar segments, top bar vs phone bar at 1440/390/360, demo boots masked |
| `responsive` | 14 | zero horizontal overflow on 6 routes × 3 widths, phone shell below 768, 44 px touch floor, unclipped CTAs |
| `truth` | 8, 9 | net worth, More-panel balances, portfolio value and basis date all equal the served payload; totals equal server `total`; every rendered money string parses |
| `mask` | 6, 7 | click **and** `Ctrl+Shift+H` flip with **zero** network requests, percentages stay, focus + live region, SPA keeps the choice, boot resets to the tenant default, chart axes repaint **in both directions** on the same mounted `/investments` route (QA blocker B1) with **zero** fetches and **no remount** (expando probe on the chart node), masked aria-labels/svg alternatives carry **no raw amounts** (security F-9) |
| `demo-writes` | 3, 4, 17 | refusal is local/singular (one toast, no error panel, no POST, no navigation), W1 downloads live + `GET /export/zip|full` 200, write siblings locked, W2 disabled-before-dispatch with the exact copy and zero probe requests |
| `states` | 15 | real delayed API → loading state, injected envelope-shaped 500 → `error-panel` with `detail` verbatim + a retry that genuinely recovers, truth-bar as-of line, stale-strip consistency |
| `nav` | G1/G2 | More panel groups + every destination walked for real content, account rows, phone drawer Escape/focus, five-tab bar |
| `keyboard` | 13, K1–K3 | skip-link first stop, accent ring, palette focus/Escape/return, More focus/Escape/return, `aria-modal` dialog focus-in/trap/Escape-return |
| `acceptance` | F-10, F-11, F-RETRY | the release-signature re-runs the preserved suite did not cover in a browser: the trades log prints no raw price/commission while masked **and does print them unmasked** (non-vacuous control) at 1440/390; `/forecast` end-value table, assumptions figures, whole-surface text+aria-labels and the **chart's own aria-label** carry no money while masked, with the same unmasked control (the demo tenant seeds no scenarios, so the scenario event row is noted and stays covered by `frontend/tests/f11ForecastMask.test.tsx`); a transient 500 renders the panel with a retry, **one press is exactly one request** and the control never sits enabled mid-flight, the retry recovers in place, and a deterministic refusal (`validation_error`, `demo_account_restricted`) renders the panel **without** a retry and without a second request |
| `tenant` | 1, 15, E2EE | disposable tenant (registered + torn down via `DELETE /users/me`): empty three-part state, zero locks, W2 live dispatch, real stale-price strip, E2EE `not enrolled → unlocked → locked`, and QA blocker **B2** end to end in one tab — logout drops the key, a fresh login stamps it with the bound identity, a same-user hard reload serves the cache with **zero** rescan requests, and a cross-tenant login + reload never resurrects the previous tenant's rows |

Fixtures follow the QA contract: the demo tenant is **read only** (every
mutation, enrolment and probe runs on a freshly registered tenant that is
deleted at the end of the run).

## Output

The runner also watches the browser console for the WHOLE run and emits
`console.no-uncaught-exceptions` (an exception that escaped a React boundary is
a product defect); `console.error` entries are recorded in `notes` and in the
report as evidence, never as a verdict — the fault pins deliberately inject 500s.

`run.mjs` prints one line per check, writes a JSON report
(`verdict`, `total/passed/failed`, per-check `suite`, `viewport`, `id`,
`detail`), and exits `0` only when every check passes. A failing UX/security
condition is a **finding** for the owning lane — this suite never edits
`frontend/src/**`.

`results.json` is a run artifact; it is not committed.

## Load gate on the one stopwatch

`mask.click-instant.*` is the suite's only wall-clock budget (500 ms from
click to chip flip). Per AGENTS.md §4 it is judged only when the box can
answer: if `load1 >= cpus().length` the measurement is still recorded in the
check detail (and a `note` line) but the verdict is withheld, because sibling
test runs saturate this 4-core box and the number would measure the neighbours
rather than the mask. Every functional assertion — zero network requests,
focus return, live-region announcement, the axis repaint — stays unconditional
under load.
