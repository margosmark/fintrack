package com.margosmark.fintracker.finance

/**
 * The read-only API map for Android v1 (docs/52 §3). Every path here exists
 * on the backend today; nothing in v1 may call a route that is not listed.
 *
 * There is deliberately NO transaction-detail path: the backend has no
 * `GET /transactions/{id}` handler, so the LIST ROW is the record and
 * `GET /transactions/{id}/events` is the only per-transaction drill-in.
 */
object ApiPaths {
    /** Page 1 of the account list (server default limit is 50, max 200). */
    const val ACCOUNTS = "/accounts"

    /** Whole-portfolio positions + server-computed headline. */
    const val POSITIONS = "/portfolio/positions"

    /** Recent transactions, first page (server-ordered, committed rows only). */
    const val TRANSACTIONS = "/transactions"

    fun account(accountId: Int) = "/accounts/$accountId"

    /** Server-ordered committed ledger with starting/running balances. */
    fun ledger(accountId: Int) = "/accounts/ledger?account_id=$accountId"

    /** Positions narrowed to one account (server-side filter). */
    fun positionsForAccount(accountId: Int) = "/portfolio/positions?account_id=$accountId"

    /** Event history for ONE transaction (owned_or_404 + user-scoped). */
    fun transactionEvents(transactionId: Int) = "/transactions/$transactionId/events"

    /** `GET /fx/status` — freshness context; supplies the reporting-currency label. */
    fun fxStatus() = "/fx/status"
}
