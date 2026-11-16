package com.margosmark.fintracker.navigation

/**
 * v1 route table. Route strings are the contract between [AppNavHost], the
 * deep-link resolver, and tests — add a destination here first.
 *
 * list→detail back-stack behavior is Navigation Compose's default (each
 * navigate() pushes; system back pops) — no special-casing needed.
 */
object Routes {
    const val SIGN_IN = "sign_in"
    const val GLANCE = "glance"
    const val ACCOUNTS = "accounts"
    const val ACCOUNT_DETAIL = "account/{accountId}"
    const val HOLDING_DETAIL = "account/{accountId}/holding/{holdingId}"
    const val TRANSACTIONS = "transactions"
    const val SETTINGS = "settings"

    /** Generic fallback for unknown/stale/foreign deep-link targets. NOT advertised in intent filters. */
    const val UNAVAILABLE = "unavailable"

    const val ARG_ACCOUNT_ID = "accountId"
    const val ARG_HOLDING_ID = "holdingId"

    fun accountDetail(accountId: String) = "account/$accountId"
    fun holdingDetail(accountId: String, holdingId: String) =
        "account/$accountId/holding/$holdingId"

    /** Every user-facing destination, with concrete sample args where the route has placeholders. */
    fun allDestinations(): List<String> = listOf(
        SIGN_IN,
        GLANCE,
        ACCOUNTS,
        accountDetail("1"),
        holdingDetail("1", "1"),
        TRANSACTIONS,
        SETTINGS,
        UNAVAILABLE,
    )
}
