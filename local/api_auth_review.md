# API Layer Security Review — `ApiAuth` Subagent

## Scope
Reviewed: `deps.py`, `auth.py`, `users.py`, `transactions.py`, `accounts.py`, `securities.py`, `imports.py`, `prices.py`, `trades.py`.

---

## 1. Critical Findings

**None.** No SQL injection, no full IDOR bypass, no float leaks in monetary paths, no missing auth on any endpoint. The core security posture is sound.

---

## 2. Medium Findings

### M1 — `delete_security` does not check for prices → unhandled 500
**File:** `backend/app/api/securities.py:716`  
**Description:** `delete_security` checks for `Trade` references before deleting, but NOT for `Price` rows. The `Price` model has `security_id: ForeignKey("securities.id")` with no `ON DELETE CASCADE`. Deleting a security with prices causes a raw PostgreSQL `IntegrityError` → 500 Internal Server Error instead of a clean 409.  
**Fix:** Add a price-existence check before `db.delete(security)`:
```python
has_prices = db.exec(select(Price.id).where(Price.security_id == security.id).limit(1)).first()
if has_prices is not None:
    raise api_error(409, "Security has recorded prices; remove them first", "security_in_use")
```

### M2 — Price upsert does not scope existing-row lookup by `user_id`
**File:** `backend/app/api/prices.py:70`  
**Description:** The upsert query searches for existing prices by `(security_id, date)` without a `user_id` filter. While `_security_or_404` already confirms the security belongs to the current user, this violates defense-in-depth tenancy. If the security is deleted and its auto-increment ID is reused by a different user (extremely unlikely but possible in edge cases), the lookup could return a price row belonging to another user.  
**Fix:** Add `Price.user_id == current_user.id` to the existing-row query:
```python
existing = db.exec(
    select(Price).where(
        Price.security_id == payload.security_id,
        Price.date == payload.date,
        Price.user_id == current_user.id,
    )
).first()
```

### M3 — `delete_account` transaction check missing `user_id` scope
**File:** `backend/app/api/accounts.py:199`  
**Description:** The `has_transactions` check queries `Transaction.account_id == account_id` without `Transaction.user_id == current_user.id`. Since `account_id` is a globally unique PK this is not exploitable in practice, but it violates the project tenancy invariant (`user_id` on every query).  
**Fix:** Add `Transaction.user_id == current_user.id` to the check.

### M4 — `delete_security` trade check missing `user_id` scope
**File:** `backend/app/api/securities.py:714`  
**Description:** Same pattern as M3. `Trade.security_id == security.id` without `Trade.user_id == current_user.id`.  
**Fix:** Add `Trade.user_id == current_user.id`.

### M5 — `get_current_user_with_query_token` accepts JWT in URL query string
**File:** `backend/app/api/deps.py:53-68`  
**Description:** The SSE endpoint (`/stream/prices`) accepts tokens via `?token=...`. JWT tokens in URLs are logged by proxies, CDN access logs, browser history, and server access logs. This is an inherent trade-off for SSE (which cannot set custom headers in EventSource), but the token leakage surface should be documented and the token should be single-use or short-lived if possible.  
**Mitigation:** Already partially mitigated by the 15-minute access token expiry. Consider issuing a dedicated short-lived SSE token (e.g. 5 minutes) instead of reusing the access token.

### M6 — `securities.py` `lookup` endpoint is unthrottled
**File:** `backend/app/api/securities.py:110`  
**Description:** `GET /securities/lookup?symbol=...` calls `market_data.lookup_symbol` (an external HTTP request) with no rate limit. An attacker can use this to exhaust outbound connections or cause upstream rate-limit bans. The `@limiter.limit` decorator is missing on this endpoint (present on `register`/`login`/`demo`).  
**Fix:** Add `@limiter.limit("30/minute")` or similar.

---

## 3. Low Findings

### L1 — `_trgm_score` uses raw SQL functions — verify parameterization
**File:** `backend/app/api/transactions.py` (similar endpoint)  
**Description:** The trigram `similarity()` / `word_similarity()` functions receive `query` as a Python string passed to SQLAlchemy. SQLAlchemy's ORM layer parameterizes this correctly (it becomes a bind parameter, not interpolated SQL). **No injection risk**, but worth confirming if any raw `text()` wrapper appears.

### L2 — `accept_transfer_suggestion` does not verify cross-account difference
**File:** `backend/app/api/transactions.py:282`  
**Description:** The endpoint pairs two transactions but does not verify they belong to different accounts. A user could pair two transactions in the same account as a "transfer," which is semantically incorrect for the `transfers` category. Not a security issue, but a business-logic gap.  
**Fix:** Add `if source.account_id == candidate.account_id: raise api_error(409, "Transfer pairs must be in different accounts", "same_account_transfer")`.

### L3 — No maximum file-size guard on upload before parsing
**File:** `backend/app/api/imports.py:82-83`  
**Description:** The 5 MB limit is checked AFTER `file.read()`, meaning the entire file is read into memory first. For very large uploads this could cause memory pressure before the size check fires. FastAPI's `UploadFile` does not enforce size limits at the framework level.  
**Mitigation:** Consider using `SpooledTemporaryFile` or streaming the read with a byte counter, or setting a middleware-level upload size limit.

### L4 — `list_transfer_suggestions` and `list_similar_transactions` lack pagination
**File:** `backend/app/api/transactions.py:205, 177`  
**Description:** Both endpoints return unbounded result sets. For users with thousands of transactions, this could cause slow responses and high memory usage.  
**Mitigation:** Add `limit`/`offset` parameters, consistent with `list_transactions`.

### L5 — `update_transaction` allows arbitrary field overwrite via `setattr` loop
**File:** `backend/app/api/transactions.py:310-312`  
**Description:** The PATCH handler iterates over `payload.model_dump(exclude_unset=True)` and applies each field with `setattr`. If the `TransactionUpdate` schema ever adds a field that shouldn't be directly settable (e.g., `user_id`, `import_batch_id`, `dedup_hash`), it would be silently overwritten. This is currently safe because the schema restricts allowed fields, but it's a fragile pattern.  
**Alternative:** Use an allow-list of mutable fields, or use a whitelist in the loop.

---

## 4. Positive Observations (No Action Required)

- **Auth flow:** Refresh token rotation is correctly implemented — old token revoked, new pair issued, theft detection revokes all sessions.
- **Password validation:** 72-byte bcrypt limit enforced at register. Cost rounds = 12 (meets spec).
- **JWT secret:** Fails fast on the default dev secret via `model_validator`.
- **Tenancy:** Every `_owned_*` helper checks `entity.user_id != user.id`. List queries consistently filter by `current_user.id`.
- **Decimal handling:** All monetary fields use `Decimal` in models, schemas, and API responses. No float leaks detected.
- **Error envelope:** Consistent `{detail, code}` via `api_error()`.
- **Category ownership:** `update_transaction` validates category ownership before assignment.
- **Refresh token theft detection:** Revokes all sessions when a rotated token is reused.
