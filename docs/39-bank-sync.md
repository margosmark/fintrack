# 39 — Bank sync (open-banking readiness seam)

Milestone anchor: **M79** (Wave-5 addendum). Spec-first: the interface, the
error codes, the consent/revocation semantics and the regulatory timeline are
pinned here; the implementation in `backend/app/services/sync/` is what an
accredited adapter has to satisfy.

**The whole feature is a seam.** Adoption of a real provider must be a config
change, not a re-architecture: no screen scraping, no aggregator integration,
no third-party credential storage ships with this milestone.

## Context

Canada's Consumer-Driven Banking Act makes personal financial data
shareable on request, with the Bank of Canada as regulator. The full timeline
and what it obliges us to build are in **Verification → Regulatory timeline**
below. The short version for design: Phase 1 is read access, accreditation and
MFA are mandatory, and the realistic live window is late 2026–2027 — so the
thing worth building *now* is the seam, because roughly 9M Canadians are on
screen scraping today and the app must be able to switch to an accredited data
source without touching ingestion.

## Approach

### 1. Provider interface (`backend/app/services/sync/`)

Adapter pattern, same shape as `services/fx.py` / `services/market_data.py`.
Four operations, pinned exactly:

```python
class Provider(Protocol):
    slug: str                       # registry key, e.g. "stub"
    display_name: str               # human label for the picker

    def list_institutions(self) -> Sequence[ProviderInstitution]: ...
    def create_agreement(
        self, user: ProviderUser, accounts: Sequence[ProviderAccount]
    ) -> AgreementGrant: ...
    def pull_delta(self, user: ProviderUser, since: datetime | None) -> PullBatch: ...
    def revoke(self, user: ProviderUser) -> None: ...
```

Files:

| File | Role |
| --- | --- |
| `services/sync/provider.py` | `Provider` protocol, `ProviderError` hierarchy, the slug registry (`register_provider` / `get_provider` / `list_providers`) |
| `services/sync/types.py` | frozen dataclasses that cross the seam (below) |
| `services/sync/stub.py` | `StubProvider` — deterministic, no network, `from_fixture()` loader |
| `services/sync/service.py` | the ONLY caller of a provider; consent, pull, revoke, status writes |
| `services/sync/__init__.py` | registers the stub on import, so `GET /sync/providers` is never empty |

**Where network may happen.** `services/sync/` is destination 6 in the docs/11
egress inventory. `_guard_sync_egress()` runs before every provider call and
reuses the `services/fx.py` request-scoped `privacy_mode` ContextVar — one
flag, one vocabulary (`403 privacy_mode_blocks_external_api`), one place to
audit. Nothing outside `services/sync/service.py` imports a provider
implementation.

### 2. Data that crosses the seam

Pinned by the type signature, not by reviewer discipline — a provider is handed
projections, never ORM rows:

```python
@dataclass(frozen=True)
class ProviderUser:
    id: int                      # opaque tenant id ONLY — never email or name

@dataclass(frozen=True)
class ProviderAccount:
    local_id: int                # our accounts.id
    currency: str
    account_type: str            # AccountType value
    # `name` is deliberately absent: it is PII and may be `accounts.name_cipher`.

@dataclass(frozen=True)
class ProviderInstitution:
    ref: str
    name: str
    country: str = "CA"

@dataclass(frozen=True)
class AgreementGrant:
    consent_id: str              # the provider's own reference — NOT a credential
    scopes: tuple[str, ...]
    account_refs: dict[str, int] # provider account ref -> our accounts.id

@dataclass(frozen=True)
class NormalizedTx:
    account_ref: str
    external_id: str             # provider transaction id (debug/audit aid)
    date: date
    amount_native: Decimal       # signed, account currency, debits negative
    description: str
    currency: str | None = None  # None -> account.currency
    posting_date: date | None = None

@dataclass(frozen=True)
class NormalizedBalance:
    account_ref: str
    amount: Decimal
    currency: str
    as_of: date

@dataclass(frozen=True)
class PullBatch:
    transactions: tuple[NormalizedTx, ...] = ()
    balances: tuple[NormalizedBalance, ...] = ()
    provider_status: ProviderStatus = ProviderStatus.ok
    cursor: datetime | None = None
```

`NormalizedTx` is deliberately field-for-field the `RawTransaction` contract
(docs/03) with a `account_ref`/`external_id` in place of a file — the pull maps
onto it in one comprehension.

**Account scope is enforced twice.** `AgreementGrant.account_refs` is
authoritative for what the consent covers; the service re-checks every
`NormalizedTx.account_ref` against it and drops anything else
(`PullResponse.dropped_rows`). An adapter that leaks a foreign account ref can
never stage into an account the consent never covered. The service also
requires `set(account_refs.values()) == set(requested ids)` at consent time —
an adapter that maps only part of the request fails **422
`sync_account_mapping_incomplete`** instead of silently losing an account.

### 3. Data model

#### sync_agreements (migration `0033_sync_agreements`)

| Column | Type | Notes |
| --- | --- | --- |
| `id` | PK | |
| `user_id` | FK `users.id`, indexed | tenancy (docs/02) |
| `provider` | `VARCHAR(32)` | registry slug |
| `consent_id` | `VARCHAR(128)` | provider's agreement reference — **not** a credential |
| `status` | enum `active \| auth_required \| revoked`, `native_enum=False`, length 32 | consent lifecycle; `revoked` is terminal |
| `account_refs` | `JSON` not null | provider ref -> local `accounts.id`; read/written only by `services/sync` |
| `scopes` | `JSON` not null | e.g. `["accounts", "transactions"]` |
| `last_sync_at` | `TIMESTAMPTZ` null | status-page groundwork |
| `provider_status` | enum `ok \| degraded \| auth_required`, `native_enum=False`, length 32, null | last observed health |
| `last_error` | `VARCHAR(255)` null | last provider failure, truncated |
| `revoked_at` | `TIMESTAMPTZ` null | |
| `created_at` / `updated_at` | `TIMESTAMPTZ` not null | `TimestampMixin` |

UNIQUE `uq_sync_agreement_consent (user_id, provider, consent_id)`.

**There is deliberately no credential column.** No token, no secret, no
refresh material. `account_refs` is JSON rather than a junction table because
it is only ever read and written through `services/sync`, and tenancy + export
scoping ride on `user_id` exactly like every other domain table (so
`GET /export/zip` picks it up with no registry change, and
`_USER_OWNED_MODELS` wipes it with the rest of a tenant's data).

#### accounts (same migration)

| Column | Type | Notes |
| --- | --- | --- |
| `last_sync_at` | `TIMESTAMPTZ` null | when this account last received a pull |
| `provider_status` | enum `ok \| degraded \| auth_required`, null | |

`provider_status IS NULL` means **"not linked to a live sync agreement"** — a
state, not missing data. It stays NULL through a revoke while `last_sync_at`
survives as historical evidence of the last pull.

### 4. Ingestion parity (the important one)

Pulled rows flow through the **same** staging code as OFX/CSV uploads:
`services/import_staging.stage_rows()`, which `POST /imports/upload` now calls
too. One function owns batch creation, the cache-only CAD resolution, the
dedup hash, the duplicate salt, the counters and the 2000-row cap.

```
POST /imports/upload ─┐
                      ├─► stage_rows() ─► import_batches(pending_review)
POST /sync/…/pull ────┘        │
                               └─► transactions(import_batch_id, dedup_hash)
                                        │
                                   commit ─► live (committed_tx_conditions)
```

**Duplicate detection bridges the two sources by construction.** The canonical
hash `(account_id, date, amount_native, whitespace-normalized description)` is
looked up across ALL of the account's rows — committed and pending. So a
transaction imported by CSV and then reported again by sync is:

1. staged as a row with `is_duplicate=true` and a **salted** `dedup_hash`
   (so `UNIQUE(account_id, dedup_hash)` holds),
2. counted in `duplicates_skipped` and flagged in the pull's `preview`,
3. deleted at commit unless the user force-includes it via
   `include_ids` — exactly the CSV/OFX match decision (docs/03).

Never silently merged, never duplicated. A provider that ignores `since` and
re-sends everything is therefore still safe: repeats land as duplicates.

`ImportBatch` is per-account, so a multi-account pull creates **one batch per
account that received rows**. A pull that produces no new rows stages nothing
(`staged_batch_ids: []`) but still advances `last_sync_at`.

`file_name` for a pulled batch is `bank-sync:<slug>` and `institution` is the
account's institution name — the same resolution an upload uses.

### 5. Status page groundwork

Per-account `last_sync_at` + `provider_status`, and per-agreement
`last_sync_at` + `provider_status` + `last_error`, all returned by the API
(`AccountPublic`, `AgreementPublic`). Both feed a future status UI regardless
of which provider is configured:

| Condition | `agreement.status` | `agreement.provider_status` | `accounts.provider_status` |
| --- | --- | --- | --- |
| healthy pull | `active` | `ok` | `ok` |
| some rows unresolved (CAD fx gap) | `active` | `ok` | `ok` |
| provider transport failure | `active` | `degraded` | `degraded` |
| provider demands re-consent/MFA | `auth_required` | `auth_required` | `auth_required` |
| user revoked | `revoked` | `NULL` | `NULL` |

`unresolved` (rows with no CAD fx rate) stays its own count on the batch — it
is a data-completeness signal, not a provider-health signal.

### 6. Endpoints (router `backend/app/api/sync.py`, prefix `/api/v1/sync`)

All handlers depend on `get_current_user` (demo write guard + privacy locks
apply unchanged) and `get_db`.

| Method/Path | Contract |
| --- | --- |
| `GET /sync/providers` | → 200 `[ {slug, display_name} ]`, stable slug order. No provider call, so it works under privacy mode and for the demo tenant. |
| `GET /sync/providers/{slug}/institutions` | → 200 `[ {ref, name, country} ]`. Unknown slug → **404 `sync_provider_unknown`**. May reach the provider → privacy-guarded. |
| `POST /sync/agreements` | `{provider: str, account_ids: [int]}` (`extra="forbid"`) → **201** `AgreementPublic`. `account_ids` order decides which provider ref the adapter mints for each account (deduped, first win). Empty list → **422 `validation_error`**; unknown/foreign account → **404 `not_found`**; another active agreement for `(user, provider)` → **409 `sync_agreement_exists`**; adapter maps only part of the request → **422 `sync_account_mapping_incomplete`**; unknown slug → **404 `sync_provider_unknown`**. Rate **10/min**. |
| `GET /sync/agreements` | → 200 `[AgreementPublic]`, newest first. |
| `GET /sync/agreements/{agreement_id}` | → 200 `AgreementPublic`; unknown or another tenant's → **404 `sync_agreement_missing`**. |
| `POST /sync/agreements/{agreement_id}/pull` | → 200 `PullResponse` (below). Rate **10/min**. |
| `POST /sync/agreements/{agreement_id}/revoke` | → 200 `AgreementPublic` with `status=revoked`. Idempotent: revoking twice is still 200. Rate **10/min**. |

**`AgreementPublic`**

```json
{
  "id": 1,
  "provider": "stub",
  "consent_id": "stub-consent-7-1",
  "status": "active",
  "account_ids": [12, 15],
  "scopes": ["accounts", "transactions"],
  "provider_status": "ok",
  "last_sync_at": "2026-09-30T18:04:11.512004+00:00",
  "last_error": null,
  "created_at": "2026-09-30T18:03:59.118004+00:00",
  "revoked_at": null
}
```

`account_ids` is derived from `account_refs.values()` by a `mode="before"`
model validator on the schema itself, so `AgreementPublic` honestly claims
`from_attributes` like every other Public schema while still producing a field
no ORM row has.

**`PullResponse`**

```json
{
  "agreement": { "...": "AgreementPublic" },
  "staged_batch_ids": [41],
  "preview": [
    {"id": 901, "date": "2025-11-03", "amount_native": "-42.35",
     "currency": "CAD", "raw_description": "CORNER STORE #1234 TORONTO ON",
     "duplicate": false, "cad_amount": "-42.35"}
  ],
  "rows": 3,
  "imported": 3,
  "duplicates_skipped": 0,
  "unresolved": 0,
  "balances": [
    {"account_id": 12, "amount": "1500.00", "currency": "CAD", "as_of": "2025-11-06"}
  ],
  "dropped_rows": 0
}
```

`preview` is the same `PreviewRow` shape `POST /imports/upload` returns, so the
existing review UI can open a pulled batch without a second renderer.
`imported` / `duplicates_skipped` / `unresolved` are summed across the
batches.

### 7. Error codes

Every failure is the `{detail, code}` envelope (docs/08). The complete set:

| Status | Code | Raised when |
| --- | --- | --- |
| 403 | `privacy_mode_blocks_external_api` | `users.privacy_mode` is on and the call would reach a provider (list institutions, create, pull, revoke). Reads never reach it. |
| 403 | `demo_account_restricted` | demo tenant on any mutating route (inherited from `get_current_user`) |
| 404 | `sync_provider_unknown` | slug not in the registry |
| 404 | `sync_agreement_missing` | unknown or another tenant's agreement id |
| 404 | `not_found` | an id in `account_ids` is not an account the caller owns |
| 409 | `sync_agreement_exists` | an agreement for `(user, provider)` is already `active` or `auth_required` |
| 409 | `sync_agreement_revoked` | pull after revoke (local gate, checked before the provider is called) |
| 409 | `sync_auth_required` | agreement is `auth_required`, **or** the provider raised `ProviderAuthRequired` |
| 413 | `too_many_rows` | one pull would stage more than `MAX_STAGED_ROWS` (2000) — checked before any row is written, same cap as an upload |
| 422 | `validation_error` | request schema (empty `account_ids`, wrong types, unknown keys) |
| 422 | `sync_account_mapping_incomplete` | `AgreementGrant.account_refs` does not cover every requested account |
| 429 | `rate_limited` | `SYNC_MUTATE_LIMIT = 10/minute` per client on the three mutating routes |
| 502 | `sync_provider_unavailable` | `ProviderUnavailable` — transport/parse/upstream failure |

`ProviderError.status_code` / `.code` travel on the exception, so an adapter
never invents an error shape.

### 8. Consent and revocation semantics

- **Create** calls the provider (`_guard_sync_egress` first). On success the
  agreement is `active`. On provider failure nothing is persisted — no
  half-open consent.
- **One live agreement per `(user, provider)`.** A second `POST` while one is
  `active`/`auth_required` is `409 sync_agreement_exists`; after a revoke a new
  one may be created (a new `consent_id`, a new row — the old row is the audit
  trail, never reused).
- **Pull gates, in order:** owned-or-404 → `revoked`? → `auth_required`? →
  privacy → provider. The first three never call the provider.
- **Provider failures are recorded, not swallowed:**
  - `ProviderUnavailable` → agreement stays `active`, `provider_status =
    degraded`, `last_error` set, linked accounts `degraded`, **502**. Retryable.
  - `ProviderAuthRequired` → agreement → `auth_required`, `provider_status =
    auth_required`, linked accounts `auth_required`, **409**. Further pulls are
    blocked by the *local* gate until the user re-consents (creating a new
    agreement after a revoke is the re-consent path).
  - `ProviderRevoked` → agreement → `revoked`, `revoked_at` set, linked
    accounts `provider_status = NULL`, **409**.
  - Every recorded failure clears back to `ok`/`last_error = null` on the next
    successful pull.
- **Revoke is authoritative locally.** `POST …/revoke` marks the row `revoked`,
  clears `provider_status` on the agreement and on every linked account, sets
  `revoked_at`, and tells the provider as part of the same request. The local
  revocation is persisted after that call; if the provider call fails, the
  revoke still succeeds (200) and the failure is recorded in
  `last_error` — a provider outage must not be able to refuse a user their
  disconnect. Accepted consequence, stated rather than hidden: an orphaned
  provider-side consent can survive, and the adapter owns cleaning that up.
  `last_sync_at` is retained on the accounts as history.
- **Provider waits never retain a database session/transaction.**
  `create_agreement`, `pull`, `revoke`, and `list_institutions` first snapshot
  only the user ID, provider slug, consent cursor/account-reference mapping,
  and account projections required for dispatch, then close the request session
  before invoking the provider. Returned results are persisted in a new bounded
  session/transaction. Existing uncommitted request-session writes are never
  committed to make room for the network call. Create rechecks account
  ownership and active-consent uniqueness in its write transaction; pull
  rechecks ownership and revocation before staging. Provider failures on pull
  are recorded in a separate short transaction.
  `GET /sync/providers/{slug}/institutions` is a pure read with no write
  session afterwards: it closes the request session it was handed (the one
  `get_current_user` already read the user on) before the dynamic provider
  dispatch, so the route parks no connection while the provider answers.
  `tests/test_sync_provider_session_release.py`
  uses a one-connection pool and delayed provider operations to prove an
  authenticated second request acquires that connection during each wait.
- **No credential is stored**, so revoke has nothing secret to destroy; the
  provider's own revoke is a protocol obligation, not a local cleanup.

### 9. E2EE honesty (docs/33 wording rule)

The claim — the wording to use verbatim, in docs today and in UI copy once the
sync status UI ships (§ 5 records that UI as future groundwork; M79 shipped
backend only, so there is **no** live UI copy yet — see `docs/33` § "OUTSIDE
THE ENVELOPE", M83/F-9):

> **Bank sync is not covered by end-to-end encryption.** Provider credentials
> and consent tokens live server-side, and the agreement metadata is
> server-readable. A pull is refused for an opted-in tenant (`422
> encryption_required`): the provider path cannot produce ciphertext, so it
> stops instead of writing plaintext rows.

**The pull half of that claim changed on 2026-10-12 (M181, t_89fa8b10).** This
section used to say "opting into encryption changes nothing about a sync pull".
That was true when it was written and is false now: the unguarded ingestion
paths fail closed (docs/33 § "OUTSIDE THE ENVELOPE (M79)" carries the decision,
the full wire inventory and the exclusion table). Sync must not be fixed alone —
file upload, the brokerage import, `record-dividend` and manual trade writes
got the same guard in the same change, so file and pull still behave
identically.

Concretely:

- `sync_agreements` has no credential column *today* because no accredited
  adapter exists (storing third-party credentials is explicitly out of scope,
  see below). When one lands, the tokens it needs will be server-side state
  the passphrase never protects — that is the disclosure, made now rather than
  after the fact (same posture as Actual's bank-login storage).
- A pulled row is written to `raw_description` / `normalized_payee` /
  `amount_native` with `payee_cipher = raw_desc_cipher = amount_cipher = NULL`
  — **for a tenant that has not opted in** (unchanged default). The `🔒` lock
  marker therefore never appears for a synced row: it renders as plaintext
  whether the vault is locked or unlocked, exactly like a legacy `v0` row
  (docs/33 A16 mixed-mode). Client-side backfill (docs/33 A17) is the only path
  that would ever give it ciphertext, and it fails closed while locked.
- **An opted-in (`encryption_required`) tenant cannot pull at all** (M181): the
  route answers `422 encryption_required` before `service.pull`, so no provider
  call is made and no `import_batches` / `transactions` / `transaction_events`
  row is created. Pulls are disabled because this path runs server-side and
  cannot write the ciphertext contract — unlocking the vault does not enable it,
  and the refusal says so instead of implying a remedy that does not exist.
  Consent setup and revoke stay available; `discard` / `revert` remain the
  cleanup for rows staged before enrolment.
- The `encryption_required` 422 guard covers `POST /transactions`,
  `PATCH /transactions/{id}`, `POST /accounts` and `PATCH /accounts/{id}` —
  and, contrary to the wording that stood here, **YNAB's file upload too**
  (`api/ynab.py:36-58`'s `_refuse_when_encrypted`, called at `:197`, refuses
  the whole migration with 422 before parsing).
  Since M181 it also covers generic CSV/OFX/QFX upload **and** its commit,
  `POST /trades/import` **and** its commit, `POST /trades/record-dividend`,
  manual `POST`/`PATCH /trades`, and this sync pull — every path that would
  write the mandatory plaintext `transactions` columns or the cipher-less
  `trades` table. Full evidence table: `docs/33` § "`encryption_required` guard
  — full wire inventory". Zero-write previews
  (`POST /imports/preview`, `POST /imports/suggest-profile`,
  `POST /trades/import/preview`) and the removal/cleanup routes stay open, each
  documented with its reason.

### 10. Privacy mode

`_guard_sync_egress()` reuses `services/fx.py`'s request-scoped lock.
`create_agreement`, `pull`, `revoke` and `list_institutions` all run it, so
under `privacy_mode` those four return
`403 privacy_mode_blocks_external_api` and **nothing leaves the machine**.
`GET /sync/providers` is a registry read and stays open. File imports and
manual entry are unaffected — the same trade BoC Valet and yfinance already
make (docs/11).

### 11. Stub provider

`services/sync/stub.py`, registered under slug `stub`:

- `list_institutions()` → `stub-bank`, `stub-credit-union`.
- `create_agreement()` mints `{slug}-acc-{index}` per requested account, where
  `index` is the position in the call, and remembers the grant keyed by
  `user.id`. `consent_id` is `{slug}-consent-{user_id}-{seq}`.
- `pull_delta(user, since)` returns fixture rows whose `account_ref` is in the
  grant and whose `date > since.date()` (`since is None` → full history),
  plus the in-scope balances.
- `revoke(user)` marks the grant revoked; a later `pull_delta` raises
  `ProviderRevoked`. This is the defence-in-depth half of "revoke stops pulls":
  even with a stale local row the provider refuses.
- `StubProvider.from_fixture(path)` loads `tests/fixtures/sync_stub_pull.json`
  — the checked-in `fixtures → NormalizedTx` mapping the accredited adapter has
  to keep. A test pins that the in-code default dataset and the fixture are
  byte-identical in meaning, so they cannot drift.
- Test knobs: `pull_status` (reports `degraded`) and `next_pull_error` (raise a
  `ProviderError` on the next pull); `reset()` clears them and all grants.

### Explicitly out of scope

- Any real aggregator / accredited-provider integration.
- Screen scraping of any kind.
- Credential storage for third parties.
- Writing pulled balances into `accounts.starting_balance` or any derived
  total. A `NormalizedBalance` is **reported** in `PullResponse.balances` and
  never persisted — manual data wins (AGENTS §2), and an observed balance is
  not a user assertion. A future reconciliation surface consumes the report.
- Write access to the user's bank account (Phase 2 of the regulations,
  mid-2027 with the Real-Time Rail).

## Touch points

- `backend/app/services/sync/{__init__,provider,types,stub,service}.py` — new.
- `backend/app/services/import_staging.py` — new; extracted from
  `api/imports.py` so upload and pull share one staging path.
- `backend/app/api/sync.py`, `backend/app/schemas/sync.py` — new.
- `backend/app/api/imports.py` — upload now delegates its staging loop to
  `stage_rows` (behaviour unchanged).
- `backend/app/models/sync_agreements.py` — new; `models/accounts.py` gains
  two columns; both registered in `models/__init__.py`.
- `backend/app/api/users.py` — `SyncAgreement` added to `_USER_OWNED_MODELS`
  (no FK to `accounts`, so placement is free).
- `backend/app/schemas/accounts.py` + `api/accounts.py::_build_public` —
  expose `last_sync_at` / `provider_status`.
- `backend/app/core/ratelimit.py` — `SYNC_MUTATE_LIMIT`.
- `backend/app/main.py` — mount the router.
- `backend/alembic/versions/0033_sync_agreements.py`.
- `backend/tests/test_bank_sync.py`, `tests/fixtures/sync_stub_pull.json`,
  `tests/fixtures/sync_dup.csv`.
- Docs: this file; `docs/00` (non-goal wording), `docs/01` (external-network
  invariant + core flow), `docs/02` (table + columns + index), `docs/03`
  (ingestion parity), `docs/08` (endpoints), `docs/10` (milestone), `docs/11`
  (egress destination 6), `docs/33` (E2EE scope).
- **Pending human edit:** `AGENTS.md` §2's external-network line needs the
  same amendment docs/11 already flags (it still names only `fx.py` /
  `market_data.py` and is missing `notify_delivery.py` too). Amending
  `AGENTS.md` is consent-gated and times out unattended, so it is listed here
  instead of blocked on.

## Verification

### Regulatory timeline (PM-verified 2026-09) — why the seam looks like this

| Date | Event |
| --- | --- |
| Mar 2026 | Consumer-Driven Banking Act receives royal assent; Bank of Canada is the regulator |
| 27 Jun 2026 | Draft regulations published, Gazette I |
| 26 Aug 2026 | Consultation closed |
| ~1 yr after final Part II publication | Framework in force → realistic live window **late 2026 – 2027** |
| Phase 1 | **Read access** (what this seam is for) |
| Mid-2027 (with the Real-Time Rail) | Write access |
| Standing obligations | Accreditation for data holders, MFA, penalties up to $10M |

Context: ~9M Canadians currently bank through screen scraping. The design
consequence is the whole point of this milestone — the app must be able to
replace scraping-grade access with an accredited source by changing which
`Provider` is registered, with **zero** change to ingestion, dedup, staging or
the review flow.

### Behaviour tests (`backend/tests/test_bank_sync.py`, 26 tests)

Run: `cd backend && uv run pytest -q tests/test_bank_sync.py` against an
isolated Postgres.

| # | Input | Expected observable |
| --- | --- | --- |
| 1 | `GET /sync/providers` as a registered user | 200 containing `{"slug": "stub", "display_name": "Stub Bank (test provider)"}`; the same three routes without a token → 401 `invalid_token` |
| 2 | `GET /sync/providers/stub/institutions` | 200, refs `["stub-bank", "stub-credit-union"]`, all `country == "CA"`; `/providers/nope/institutions` → 404 `sync_provider_unknown` |
| 3 | `StubProvider.from_fixture("tests/fixtures/sync_stub_pull.json")` → `create_agreement(1 acct)` → `pull_delta(user, None)` | exactly `TX-1001/1002/1003`; `amount_native == Decimal("-42.35")` (a `Decimal`, not a float), `date == 2025-11-03`, `currency is None` on TX-1003 with `posting_date == 2025-11-06`, balances `[Decimal("1500.00")]` |
| 4 | default stub dataset vs the JSON fixture | `transactions`, `balances`, `institutions` compare equal — the two cannot drift |
| 5 | `POST /sync/agreements {provider:"stub", account_ids:[a]}` | 201, `status=active`, `account_ids == [a]`, `scopes == ["accounts","transactions"]`, `provider_status is None`, `last_sync_at is None`; the response contains no token/secret key; `sync_agreements.account_refs.values() == {a}` |
| 6 | same POST with `account_ids: []` / `[999999]` | 422 `validation_error` / 404 `not_found` |
| 7 | adapter maps only `accounts[:1]` of two requested | 422 `sync_account_mapping_incomplete` (nothing persisted) |
| 8 | second POST while one is active, then revoke, then POST again | 409 `sync_agreement_exists` → 200 revoked → 201 with a **different** id |
| 9 | owner vs other tenant on `GET /agreements`, `GET /agreements/{id}`, `POST /pull` | 200 `[]` / 404 `sync_agreement_missing` / 404 `sync_agreement_missing` |
| 10 | pull a 1-account grant over the stub fixture | one batch, `rows=3`, `imported=3`, `dropped_rows=0`, `provider_status=ok`, `last_sync_at` set; `GET /transactions` total **0** (staged); `GET /imports/{id}` → `pending_review`, `file_name == "bank-sync:stub"`; commit → total **3** |
| 11 | after a pull, `GET /accounts` | the account row carries `last_sync_at` (non-null) and `provider_status == "ok"` |
| 12 | pull twice | second response `staged_batch_ids == []`, `rows == 0`, `preview == []`, `last_sync_at` advanced, `balances` still reported |
| 13 | pull with only `NormalizedBalance` output | `balances == [{account_id, amount "1500.00", currency "CAD", as_of "2025-11-06"}]`, `accounts.starting_balance` still `None`, `GET /transactions` total still 0 |
| 14 | a leaky adapter returns a row for `stub-acc-7` (not in the grant) | `dropped_rows == 1`, that row is absent from `preview`, the in-scope rows still stage (`rows == 4` for a 2-account grant) |
| 15 | CSV `sync_dup.csv` (2 rows, `-42.35 CORNER STORE #1234 TORONTO ON`) committed, then a stub pull | `rows == 3`, `imported == 2`, `duplicates_skipped == 1`; the one `duplicate: true` preview row is `CORNER STORE #1234 TORONTO ON`; after commit `GET /transactions` total **4** and exactly **1** row has that description, `is_duplicate == False`, `import_batch_id` = the CSV batch |
| 16 | same, but commit the sync batch with `include_ids: [dup_id]` | `imported == 3`, `duplicates_skipped == 0`, total **5** — the duplicate is kept only because the user chose it |
| 17 | pull → `POST /revoke` → `POST /pull` | 200 revoked (`revoked_at` set, `provider_status NULL`) → 409 `sync_agreement_revoked`; a second revoke still 200; `accounts.provider_status IS NULL` while `last_sync_at` is retained; `last_error` is `None` |
| 18 | stub revoked at the provider, then `pull_delta` called directly | raises `ProviderRevoked` (defence in depth) |
| 19 | adapter whose `revoke` raises `ProviderUnavailable` | revoke still returns **200** `status=revoked` with `last_error` containing the message; the following pull is still 409 `sync_agreement_revoked` |
| 20 | `next_pull_error = ProviderUnavailable("upstream returned 503")` | 502 `sync_provider_unavailable`; agreement stays `active` with `provider_status=degraded` + `last_error`, account `degraded`; clearing the knob and re-pulling → 200 `provider_status=ok`, `last_error=None` |
| 21 | `next_pull_error = ProviderAuthRequired(...)` | 409 `sync_auth_required`; agreement `auth_required`, account `auth_required`; clearing the knob and re-pulling **still** 409 (the local gate, not the provider) |
| 22 | a stub returning 2001 rows | 413 `too_many_rows` and `GET /imports` total **0** — nothing staged |
| 23 | `PATCH /users/me {privacy_mode: true}` after consent | institutions / pull / revoke each → 403 `privacy_mode_blocks_external_api`; `GET /sync/providers` still 200 |
| 24 | `PATCH /users/me {encryption_required: true}`, then consent + pull (+ commit) | consent **201** (metadata only), then the pull → **422 `encryption_required`** whose detail names the disabled capability ("cannot safely write encrypted data") and offers **no** unlock remedy; **ZERO** new `import_batches` / `transactions` / `transaction_events` rows; a *manual* `POST /transactions` for the same user still → 422 `encryption_required`; `revoke` still 200 (cleanup stays open). Control: the same fixture with `encryption_required` left off pulls `imported == 3` into plaintext (row 10) |
| 25 | `MAX_STAGED_ROWS` | `2000` — the cap upload and pull share |

### Provider-session boundary regression

Run `cd backend && uv run pytest -q tests/test_sync_provider_session_release.py`
against the isolated Postgres test database. The test swaps the app engine for a
one-connection pool, blocks a fake provider during each of create, pull,
revoke, and the institutions read, then issues authenticated
`GET /sync/agreements` before unblocking it.
Each case must return 200 for the second request while the provider is still
blocked, and the original operation must return 201 (create) or 200 (pull,
revoke and institutions) after release. A retained request connection makes the
second request hit the bounded pool timeout and fails the test.

### Running-stack smoke

Against the rebuilt stack at `http://localhost:8080`:

1. `POST /api/v1/auth/demo` → token; `GET /api/v1/sync/providers` → 200 with
   the stub (reads are open to the demo tenant).
2. Create an institution + account (as a non-demo user), then
   `POST /api/v1/sync/agreements` → 201; `POST …/pull` → 200 with
   `staged_batch_ids`; `GET /api/v1/imports/{id}` → `pending_review`;
   `POST /api/v1/imports/{id}/commit` → committed; `GET /api/v1/transactions`
   shows the pulled rows.
3. `POST …/revoke` → `status: revoked`; a repeat `pull` →
   `{"code": "sync_agreement_revoked"}`.
4. `GET /api/v1/accounts` shows `last_sync_at` / `provider_status`.
5. **M181:** with `PATCH /api/v1/users/me {"encryption_required": true}` on a
   throwaway tenant, `POST …/pull` → `422 {"code":
   "encryption_required"}` naming the disabled capability (and a manual
   `POST /api/v1/transactions` still 422s); `POST
   /api/v1/sync/agreements` and `POST …/revoke` stay 200/201, and
   `GET /api/v1/imports` stays empty. Flip the flag back off and the same pull
   → 200 with `imported == 3`: the refusal is opt-in, not a global cutover.
