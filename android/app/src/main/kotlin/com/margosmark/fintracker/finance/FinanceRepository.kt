package com.margosmark.fintracker.finance

import com.margosmark.fintracker.http.ApiClient

/**
 * The v1 read surface: GET-only calls through the parent card's [ApiClient]
 * (docs/52 Option A), which already carries the access token, the cookie-only
 * refresh dance and the transport-level read-only guard.
 *
 * Every method BLOCKS and every method returns the server's own strings —
 * this class performs zero computation over financial values. What is
 * deliberately absent: any POST/PUT/PATCH/DELETE, any transaction DETAIL
 * route (none exists), any offline snapshot, any client-side total.
 */
class FinanceRepository(private val client: ApiClient) {

    fun accounts(): AccountsPage = FinanceJson.accountsPage(client.getJson(ApiPaths.ACCOUNTS))

    fun account(accountId: Int): AccountRow = FinanceJson.account(client.getJson(ApiPaths.account(accountId)))

    fun ledger(accountId: Int): LedgerPage =
        FinanceJson.ledgerPage(client.getJson(ApiPaths.ledger(accountId)))

    fun positions(accountId: Int? = null): PositionsPage {
        val path = if (accountId == null) ApiPaths.POSITIONS else ApiPaths.positionsForAccount(accountId)
        return FinanceJson.positionsPage(client.getJson(path))
    }

    fun transactions(limit: Int = 50, offset: Int = 0, accountId: Int? = null): TransactionsPage {
        val params = buildList {
            add("limit=$limit")
            add("offset=$offset")
            if (accountId != null) add("account_id=$accountId")
        }
        return FinanceJson.transactionsPage(
            client.getJson(ApiPaths.TRANSACTIONS + "?" + params.joinToString("&")),
        )
    }

    /** `GET /fx/status` (docs/52 §3): the server's reporting currency for total denominations. */
    fun fxStatus(): FxStatusRow = FinanceJson.fxStatus(client.getJson(ApiPaths.fxStatus()))

    fun transactionEvents(transactionId: Int): TransactionEventsPage =
        FinanceJson.transactionEventsPage(client.getJson(ApiPaths.transactionEvents(transactionId)))
}
