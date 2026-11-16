# 00 — Overview

## Goals

Build a personal finance tracker that, for a Canadian self-hosting user:

1. Tracks spending across institutions (banks, credit cards) via file import.
2. Tracks investments — Canadian and US stocks, ETFs, mutual funds — including FX exposure across listing currencies.
3. Supports Canadian tax preparation: adjusted cost basis (ACB) in CAD, capital gains on sales, superficial-loss rules, and return-of-capital adjustments.
4. Auto-categorizes transactions using a local LLM (Ollama) with optional cloud providers, improving through a learning loop from user confirmations.

Form factor: self-hosted web app first. Schema and auth are designed from day one for a future multi-tenant SaaS with subscriptions (`user_id` on every domain table; JWT bearer auth), but no SaaS features are built in v1.

## Non-goals v1

- No bill-pay
- No aggregator APIs (no Plaid/SnapTrade integration in v1 — the importer interface leaves room, see docs/03).
  Bank sync ships only as the **open-banking readiness seam** (docs/39): a `Provider` protocol, a stub
  adapter, consent/revocation and shared staging — no screen scraping, no third-party credential storage.
- No automated corporate-action parsing (manual adjustment entries only, docs/06)
- No multi-base-currency reporting (CAD reporting currency; native currency stored per row so this can change later)
- No tax filing integration — reports are informational only

## Personas

- **Single-user self-hoster**: technically comfortable, runs docker-compose locally, owns their data, uses Ollama on local GPU for categorization.
- **Future hosted subscriber** (design target only): expects the same features behind accounts/subscriptions; nothing v1 blocks this.

## Initial feature list → milestones

| Capability | Milestone |
| --- | --- |
| App scaffold, schema, auth | M1 |
| Bank/credit-card import (OFX/QFX + CSV profiles) | M2 |
| Transaction categorization + LLM learning loop | M3 |
| Investment tracking (securities, trades, valuation) | M4 |
| Currency exposure view | M4 / M6 dashboard |
| Canadian ACB / capital-gains engine | M5 |
| Frontend completion, dashboard, report exports | M6 |
| Reporting & graphs (spending, cash flow, net worth history, investment performance) | M8 |
| Account & institution management breadth (LIRA/RDSP types, accounts manager) | M9 |
| Demo experience (one-click seeded tenant) | M10 |
| Securities UX & watchlist | M11 |
| Search & similar-examples | M12 |
| Security dashboard & search integration | M13 |
| Dividend income analytics | M14 |
| Investment projections (custom scenarios) | M15 |
| Security fundamentals (EPS, P/E, …) | M16 |
| ETF/MF holdings composition | M17 |
| Look-through exposure breakdown (sector/geography/asset class) | M18 |
| Recursive look-through & company exposure (depth ≤ 3, cycle-safe) | M19 |
| Account sidebar, detail page & transfer suggestions | M20 |
| SSE cached-price streaming & market-hours optimization | M21 |
| Transaction CRUD (manual and committed-row add/edit/delete) | M22 |
| Currency Tracker board (read-only BoC rates, sparklines) | M23 |
| MCP server with token-based auth | M24 |
| Per-category budget tracking with spending limits | M25 |
| Tax-loss harvesting alerts for unrealized losses | M26 |
| Subscription tracker (recurring transaction detection) | M27 |
| Spending velocity & burn rate insights | M28 |
| Dividend calendar (upcoming income projections) | M29 |
| Spending comparison (period-over-period) | M30 |
| Notification & alert settings | M31 |
| Account reconciliation mode | M32 |
| Goal-based savings tracker | M33 |
| Net worth snapshot drill-down | M34 |
| Mobile-responsive layout | M35 |
| Goals management page (full CRUD) | M36 |
| Budget management page (full CRUD) | M37 |
| Currencies route fix | M38 |
| Account Reconciliation page | M39 |
| Bill Reminders page (full CRUD) | M40 |
| Spending Alerts (budget threshold detection) | M41 |
| Tax Projection page (CRA brackets) | M42 |
| Investment Benchmarks (portfolio vs index) | M43 |
| Debt Tracking page (payoff projections) | M44 |

This initial feature list covers M1–M44. Implemented follow-on milestones are tracked in [`docs/10-roadmap.md`](10-roadmap.md) and [`docs/24-new-features.md`](24-new-features.md); the current architecture and repository layout are summarized in [`docs/01-architecture.md`](01-architecture.md).

Milestone details, acceptance criteria, and smoke tests: docs/10-roadmap.md.
