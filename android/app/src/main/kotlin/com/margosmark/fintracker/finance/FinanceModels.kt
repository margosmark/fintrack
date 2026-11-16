package com.margosmark.fintracker.finance

/**
 * Wire shapes for the v1 read-only endpoints (docs/52 §3), pinned against the
 * backend schemas: `backend/app/schemas/accounts.py`, `.../portfolio.py`,
 * `.../transactions.py` and `services/valuation.compute_positions`.
 *
 * INVARIANT (docs/52 §4.1, docs/53 AC1.1): every monetary field is the
 * server's Decimal-as-string, carried VERBATIM. No field here is ever parsed
 * into a number, summed or converted in the data layer. The presentation
 * layer formats decimal strings for readability without changing the stored
 * values — Home via `HomeNumberFormat`, account detail via `AccountValueFormat`
 * (docs/52 §3) — and amounts remain bullet-masked when masking is on. Percentages and counts are server JSON numbers and stay
 * numbers, also rendered verbatim. `null` is preserved as `null`, never
 * coerced to zero (docs/53 AC1.3: unavailable is not $0).
 */

/** `GET /accounts` row (`AccountPublic`). */
data class AccountRow(
    val id: Int,
    val name: String,
    /** `AccountType` StrEnum value, verbatim (e.g. `chequing`, `tfsa`). */
    val type: String,
    val currency: String,
    /** Committed-ledger cash (cash accounts) / value-history cash (investment); null = no honest figure. */
    val cashBalance: String?,
    val investmentValue: String,
    val totalValue: String?,
    val startingBalance: String,
    /** ISO instant or null — provider-sync status, NOT a "data checked at" stamp; omitted when null. */
    val lastSyncAt: String?,
    val providerStatus: String?,
    val pendingImportCount: Int,
)

data class AccountsPage(
    val items: List<AccountRow>,
    val total: Int,
)

/** `GET /portfolio/positions` row (`valuation.compute_positions` dict). */
data class PositionRow(
    val accountId: Int,
    val accountName: String,
    val securityId: Int,
    val symbol: String,
    val listingCurrency: String,
    val quantity: String,
    /** ISO market date of the newest usable cached close; null = no price. */
    val priceDate: String?,
    val price: String?,
    /** `"manual"` | `"yfinance"` — manual precedence is docs/07; rendered as the source. */
    val priceSource: String?,
    val priceAgeDays: Int?,
    /** REAL provider-fetch evidence; null = NO evidence, segment omitted (docs/53 AC4.2). */
    val priceFetchedAt: String?,
    val priceStale: Boolean?,
    val marketValueNative: String?,
    val marketValueCad: String?,
    val bookCostCad: String?,
    val dayChangeNative: String?,
    val dayChangePct: Double?,
    val dayChangeCad: String?,
    val unrealizedGainCad: String?,
)

/**
 * `GET /portfolio/positions` envelope. The `*Cad` headline fields are the
 * server's own totals computed from the same `items`; a null
 * `portfolioValueCad` means the server has no single-currency total and the
 * UI must OMIT it — never sum `items` client-side (docs/53 AC1.1).
 */
data class PositionsPage(
    val items: List<PositionRow>,
    val total: Int,
    val totalUnrealizedCad: String?,
    val totalValueCad: String,
    val cashCad: String?,
    val portfolioValueCad: String?,
    val totalDayChangeCad: String?,
    val totalDayChangePct: Double?,
    val dayChangeAsOf: String?,
    val dayChangeMixedBaseline: Boolean,
)

/** `GET /accounts/ledger` row (`LedgerEntry`). */
data class LedgerEntry(
    val transactionId: Int,
    val date: String,
    val description: String,
    val amount: String,
    val runningBalance: String,
)

data class LedgerPage(
    val items: List<LedgerEntry>,
    val startingBalance: String,
    /** Server-named `current_balance`; rendered verbatim under a neutral label. */
    val currentBalance: String,
    val total: Int,
)

/** `GET /transactions` row (`TransactionPublic` subset). */
/** `GET /fx/status` — only the fields v1 needs: how to DENOMINATE a total. */
data class FxStatusRow(
    val reportingCurrency: String,
    val asOf: String?,
    val ageDays: Int?,
    val stale: Boolean?,
)

data class TransactionRow(
    val id: Int,
    val accountId: Int,
    val date: String,
    val rawDescription: String,
    val normalizedPayee: String,
    val amountNative: String,
    val currency: String,
    val cadAmount: String?,
    val categoryId: Int?,
    /** ISO instant or null; null = unreviewed ("Needs review" chip). */
    val reviewedAt: String?,
    val isRecurring: Boolean,
    val hasReceipt: Boolean,
    val isSplit: Boolean,
    val parentSplitId: Int?,
    val note: String?,
) {
    /** The row's display name: the normalized payee, else the raw description. */
    val payeeOrDescription: String
        get() = normalizedPayee.ifBlank { rawDescription }
}

data class TransactionsPage(
    val items: List<TransactionRow>,
    val total: Int,
    val limit: Int,
    val offset: Int,
    /** Rows withheld by privacy/vault policy — explained IN PLACE, never "no data". */
    val withheld: Int,
)

/** `GET /transactions/{id}/events` row (`TransactionEventPublic` subset). */
data class TransactionEventRow(
    val id: Int,
    val txId: Int,
    val eventKind: String,
    val actorLabel: String,
    /** ISO instant or null — a real server event time, labelled as such when rendered. */
    val at: String?,
    val summary: String,
)

data class TransactionEventsPage(
    val items: List<TransactionEventRow>,
    val total: Int,
)

/**
 * Human label for the server's account-type enum. Presentation mapping of a
 * server fact (never a client-side value): the enum string itself is what
 * the server sent; this only makes it readable. Unknown values pass through
 * rather than being relabelled as something they are not.
 */
val AccountRow.typeLabel: String
    get() = when (type) {
        "rrsp" -> "RRSP"
        "tfsa" -> "TFSA"
        "rrif" -> "RRIF"
        "fhsa" -> "FHSA"
        "resp" -> "RESP"
        "rdsp" -> "RDSP"
        "lira" -> "LIRA"
        "non_registered" -> "Taxable"
        "brokerage_non_registered" -> "Brokerage"
        "cash" -> "Cash"
        "other" -> "Other"
        else -> type
    }
