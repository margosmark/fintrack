package com.margosmark.fintracker.finance

import org.json.JSONArray
import org.json.JSONObject

/**
 * org.json decoding for the read endpoints. Rules:
 *
 *  1. **Verbatim strings.** A Decimal field arrives as a JSON string and is
 *     copied byte-for-byte; a JSON number (counts, percentages, ages) is
 *     rendered through its own textual form. Nothing is parsed into a
 *     floating point number, ever (docs/52 §4.1).
 *  2. **null stays null.** A missing/JSON-null field becomes Kotlin `null`
 *     (or throws when the contract says non-null) — never `0`, never `""`
 *     pretending to be a figure (docs/53 AC1.3).
 *  3. **Unknown fields are ignored** — the server may add fields freely.
 *  4. A malformed SUCCESS body is a hard parse failure (throws), surfacing
 *     as `Unavailable`, not as a silently "empty" screen.
 */
internal object FinanceJson {

    // --- tolerant readers ---------------------------------------------------

    /** String-or-number-or-null as text; null when absent/JSON-null. */
    private fun JSONObject.textOrNull(key: String): String? = when {
        !has(key) || isNull(key) -> null
        else -> get(key).toString()
    }

    /** Non-null text contract field; throws when the server omitted it. */
    private fun JSONObject.text(key: String): String =
        textOrNull(key) ?: throw IllegalStateException("missing string field '$key'")

    private fun JSONObject.int(key: String): Int = when {
        !has(key) || isNull(key) -> throw IllegalStateException("missing int field '$key'")
        else -> get(key).toString().toInt()
    }

    private fun JSONObject.intOrNull(key: String): Int? =
        if (!has(key) || isNull(key)) null else get(key).toString().toInt()

    private fun JSONObject.doubleOrNull(key: String): Double? =
        if (!has(key) || isNull(key)) null else get(key).toString().toDouble()

    private fun JSONObject.boolOrNull(key: String): Boolean? = when {
        !has(key) || isNull(key) -> null
        else -> get(key) as? Boolean
    }

    private fun JSONArray.obj(i: Int): JSONObject = getJSONObject(i)

    // --- envelopes ----------------------------------------------------------

    fun accountsPage(body: String): AccountsPage {
        val obj = JSONObject(body)
        val items = obj.getJSONArray("items")
        return AccountsPage(
            items = (0 until items.length()).map { i ->
                val a = items.obj(i)
                AccountRow(
                    id = a.int("id"),
                    name = a.text("name"),
                    type = a.text("type"),
                    currency = a.text("currency"),
                    cashBalance = a.textOrNull("cash_balance"),
                    investmentValue = a.textOrNull("investment_value")
                        ?: throw IllegalStateException("missing string field 'investment_value'"),
                    totalValue = a.textOrNull("total_value"),
                    startingBalance = a.textOrNull("starting_balance")
                        ?: throw IllegalStateException("missing string field 'starting_balance'"),
                    lastSyncAt = a.textOrNull("last_sync_at"),
                    providerStatus = a.textOrNull("provider_status"),
                    pendingImportCount = a.intOrNull("pending_import_count") ?: 0,
                )
            },
            total = obj.int("total"),
        )
    }

    fun account(body: String): AccountRow {
        val a = JSONObject(body)
        return AccountRow(
            id = a.int("id"),
            name = a.text("name"),
            type = a.text("type"),
            currency = a.text("currency"),
            cashBalance = a.textOrNull("cash_balance"),
            investmentValue = a.textOrNull("investment_value")
                ?: throw IllegalStateException("missing string field 'investment_value'"),
            totalValue = a.textOrNull("total_value"),
            startingBalance = a.textOrNull("starting_balance")
                ?: throw IllegalStateException("missing string field 'starting_balance'"),
            lastSyncAt = a.textOrNull("last_sync_at"),
            providerStatus = a.textOrNull("provider_status"),
            pendingImportCount = a.intOrNull("pending_import_count") ?: 0,
        )
    }

    fun positionsPage(body: String): PositionsPage {
        val obj = JSONObject(body)
        val items = obj.getJSONArray("items")
        return PositionsPage(
            items = (0 until items.length()).map { i ->
                val p = items.obj(i)
                PositionRow(
                    accountId = p.int("account_id"),
                    accountName = p.text("account_name"),
                    securityId = p.int("security_id"),
                    symbol = p.text("symbol"),
                    listingCurrency = p.text("listing_currency"),
                    quantity = p.text("quantity"),
                    priceDate = p.textOrNull("price_date"),
                    price = p.textOrNull("price"),
                    priceSource = p.textOrNull("price_source"),
                    priceAgeDays = p.intOrNull("price_age_days"),
                    priceFetchedAt = p.textOrNull("price_fetched_at"),
                    priceStale = p.boolOrNull("price_stale"),
                    marketValueNative = p.textOrNull("market_value_native"),
                    marketValueCad = p.textOrNull("market_value_cad"),
                    bookCostCad = p.textOrNull("book_cost_cad"),
                    dayChangeNative = p.textOrNull("day_change_native"),
                    dayChangePct = p.doubleOrNull("day_change_pct"),
                    dayChangeCad = p.textOrNull("day_change_cad"),
                    unrealizedGainCad = p.textOrNull("unrealized_gain_cad"),
                )
            },
            total = obj.int("total"),
            totalUnrealizedCad = obj.textOrNull("total_unrealized_cad"),
            totalValueCad = obj.textOrNull("total_value_cad")
                ?: throw IllegalStateException("missing string field 'total_value_cad'"),
            cashCad = obj.textOrNull("cash_cad"),
            portfolioValueCad = obj.textOrNull("portfolio_value_cad"),
            totalDayChangeCad = obj.textOrNull("total_day_change_cad"),
            totalDayChangePct = obj.doubleOrNull("total_day_change_pct"),
            dayChangeAsOf = obj.textOrNull("day_change_as_of"),
            dayChangeMixedBaseline = obj.boolOrNull("day_change_mixed_baseline") ?: false,
        )
    }

    fun ledgerPage(body: String): LedgerPage {
        val obj = JSONObject(body)
        val items = obj.getJSONArray("items")
        return LedgerPage(
            items = (0 until items.length()).map { i ->
                val e = items.obj(i)
                LedgerEntry(
                    transactionId = e.int("transaction_id"),
                    date = e.text("date"),
                    description = e.text("description"),
                    amount = e.text("amount"),
                    runningBalance = e.text("running_balance"),
                )
            },
            startingBalance = obj.text("starting_balance"),
            currentBalance = obj.text("current_balance"),
            total = obj.int("total"),
        )
    }

    fun transactionsPage(body: String): TransactionsPage {
        val obj = JSONObject(body)
        val items = obj.getJSONArray("items")
        return TransactionsPage(
            items = (0 until items.length()).map { i ->
                val t = items.obj(i)
                TransactionRow(
                    id = t.int("id"),
                    accountId = t.int("account_id"),
                    date = t.text("date"),
                    rawDescription = t.text("raw_description"),
                    normalizedPayee = t.textOrNull("normalized_payee") ?: "",
                    amountNative = t.text("amount_native"),
                    currency = t.text("currency"),
                    cadAmount = t.textOrNull("cad_amount"),
                    categoryId = t.intOrNull("category_id"),
                    reviewedAt = t.textOrNull("reviewed_at"),
                    isRecurring = t.boolOrNull("is_recurring") ?: false,
                    hasReceipt = t.boolOrNull("has_receipt") ?: false,
                    isSplit = t.boolOrNull("is_split") ?: false,
                    parentSplitId = t.intOrNull("parent_split_id"),
                    note = t.textOrNull("note"),
                )
            },
            total = obj.int("total"),
            limit = obj.intOrNull("limit") ?: items.length(),
            offset = obj.intOrNull("offset") ?: 0,
            withheld = obj.intOrNull("withheld") ?: 0,
        )
    }

    fun fxStatus(body: String): FxStatusRow {
        val raw = JSONObject(body)
        return FxStatusRow(
            reportingCurrency = raw.textOrNull("reporting_currency") ?: "CAD",
            asOf = raw.textOrNull("as_of"),
            ageDays = raw.optInt("age_days", -1).takeIf { it >= 0 },
            stale = raw.optBoolean("stale", false),
        )
    }

    fun transactionEventsPage(body: String): TransactionEventsPage {
        val obj = JSONObject(body)
        val items = obj.getJSONArray("items")
        return TransactionEventsPage(
            items = (0 until items.length()).map { i ->
                val e = items.obj(i)
                TransactionEventRow(
                    id = e.int("id"),
                    txId = e.int("tx_id"),
                    eventKind = e.text("event_kind"),
                    actorLabel = e.text("actor_label"),
                    at = e.textOrNull("at"),
                    summary = e.text("summary"),
                )
            },
            total = obj.int("total"),
        )
    }
}
