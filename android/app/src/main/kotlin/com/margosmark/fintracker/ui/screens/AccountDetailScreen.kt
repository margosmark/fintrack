package com.margosmark.fintracker.ui.screens

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.LocalTextStyle
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.margosmark.fintracker.finance.AccountRow
import com.margosmark.fintracker.finance.AccountsPage
import com.margosmark.fintracker.finance.FinanceRepository
import com.margosmark.fintracker.finance.LedgerPage
import com.margosmark.fintracker.finance.PositionRow
import com.margosmark.fintracker.finance.PositionsPage
import com.margosmark.fintracker.finance.TransactionsPage
import com.margosmark.fintracker.finance.typeLabel
import com.margosmark.fintracker.navigation.ScreenTags
import com.margosmark.fintracker.session.AuthState
import com.margosmark.fintracker.state.MaskedText
import com.margosmark.fintracker.ui.masking.MaskController

/** Test hooks for the account-detail screen's own sections (stable API). */
object AccountDetailTags {
    /** Wrapper around the account-scoped HOLDINGS section (not the ledger). */
    const val HOLDINGS = "account_detail_holdings"

    /** Whole-row tap target of one holding — taps drill into HOLDING_DETAIL. */
    fun holdingRow(securityId: Int): String = "account_holding_row_$securityId"

    /** Whole-row tap target of one ledger row — taps toggle its clamp in place. */
    fun ledgerRow(transactionId: Int): String = "account_ledger_row_$transactionId"

    /** The clamped/expandable description text inside one ledger row. */
    fun ledgerDescription(transactionId: Int): String = "account_ledger_description_$transactionId"

    /** The in-app "Show N more" control that replaced the dead-end footer. */
    const val SHOW_MORE = "account_ledger_show_more"

    /** Frame A's balance hero — the figure the de-dup rules protect. */
    const val BALANCE_HERO = "account_balance_hero"

    /** The hero's own value node (masked-aware probing). */
    const val HERO_VALUE = "account_balance_hero_value"
}

/**
 * Account types whose custody rules allow securities positions — the client
 * mirror of the backend's `INVESTMENT_ACCOUNT_TYPES`
 * (`backend/app/models/accounts.py`), which is exactly the set the server
 * accepts trades on (anything else answers `422 account_not_investment`).
 *
 * The account detail screen asks for `GET /portfolio/positions?account_id=…`
 * ONLY for these types: that route can queue a market-hours price/fundamentals
 * refresh (`backend/app/api/portfolio.py::get_positions`, docs/52 §3), so
 * firing it for a chequing account would be a pointless provider fan-out for
 * rows the server can never return.
 */
private val INVESTMENT_ACCOUNT_TYPES = setOf(
    "brokerage_non_registered",
    "rrsp",
    "tfsa",
    "fhsa",
    "resp",
    "lira",
    "rdsp",
)

/**
 * How many ledger rows hold before the screen asks for the next window.
 * Twenty rows fits a phone screen of history while every row still carries
 * its running balance; each tap pages ONE more window through the existing
 * `GET /transactions` route — no new API (PM ruling, docs/56 §2.4).
 */
private const val LEDGER_PAGE_SIZE = 20

fun accountDetailPresenter(repository: FinanceRepository, accountId: Int): SectionPresenter<AccountRow> =
    SectionPresenter(
        fetch = { repository.account(accountId) },
        emptyWhen = { false },
    )

fun accountLedgerPresenter(repository: FinanceRepository, accountId: Int): SectionPresenter<LedgerPage> =
    SectionPresenter(
        fetch = { repository.ledger(accountId) },
        emptyWhen = { page -> page.items.isEmpty() },
    )

/**
 * ONE "Show N more" window: `GET /transactions?limit=&offset=&account_id=`
 * — the list route used as a pure pager over the SAME account-scoped,
 * split-excluded, committed-only rows `/accounts/{id}/ledger` serves,
 * ordered `date desc, id desc` (no search term), so the screen counts its
 * offset from the END of the ascending ledger it is showing. Deliberately
 * no `emptyWhen`: an empty page is an answer the caller reads (it stops
 * asking), never a section state.
 */
fun accountTransactionsPresenter(
    repository: FinanceRepository,
    accountId: Int,
    limit: Int,
    offset: Int,
): SectionPresenter<TransactionsPage> =
    SectionPresenter(
        fetch = { repository.transactions(limit = limit, offset = offset, accountId = accountId) },
        emptyWhen = { false },
    )

/**
 * The account's OWN holdings: `GET /portfolio/positions?account_id=<id>` —
 * the server's account-scoped filter (tenant scoping and the `owned_or_404`
 * verdict stay server-side; this client never narrows rows itself). Every
 * figure it carries is the server's own string; the headline fields ride the
 * same page and are never re-summed here.
 */
fun accountHoldingsPresenter(repository: FinanceRepository, accountId: Int): SectionPresenter<PositionsPage> =
    SectionPresenter(
        fetch = { repository.positions(accountId) },
        staleWhen = { page -> positionStaleNote(page.items) },
        emptyWhen = { page -> page.items.isEmpty() },
    )

/**
 * Account detail (docs/48 §3.3, docs/52 §3): the account's own fields, its
 * account-scoped HOLDINGS (investment accounts) and its committed ledger with
 * server running balances — three INDEPENDENT sections, so one failing fetch
 * never collapses the others (docs/53 AC1.3). The subtitle keeps the server
 * id visible (`Account 7`) as the drill-in's identity.
 *
 * Interaction contract (docs/56 §2, PM rulings):
 *  - a holding row is ONE tap target that drills into `Routes.HOLDING_DETAIL`
 *    (`onOpenHolding`, wired like Glance's PositionRow);
 *  - a ledger row taps IN PLACE: its description clamps to two lines and the
 *    tap expands/collapses it — never navigation (no transaction-detail
 *    route exists);
 *  - the ledger shows the first window and pages the rest with "Show N
 *    more" through `ledgerMoreSource` (existing `GET /transactions`);
 *  - the balance hero owns `total_value` — no field row repeats it;
 *  - the privacy toggle lives in the APP BAR (this screen passes
 *    `showMask = false` to [ScreenHeader]), so it never scrolls away.
 */
@Composable
fun AccountDetailScreen(
    accountId: Int,
    account: SectionPresenter<AccountRow>,
    ledger: SectionPresenter<LedgerPage>,
    holdings: SectionPresenter<PositionsPage>,
    mask: MaskController,
    demo: Boolean,
    onOpenHolding: (accountId: Int, securityId: Int) -> Unit,
    ledgerMoreSource: (limit: Int, offset: Int) -> SectionPresenter<TransactionsPage>,
    modifier: Modifier = Modifier,
    authState: AuthState = AuthState.SignedOut,
) {
    LaunchedEffect(accountId) {
        account.load()
        ledger.load()
    }
    val masked = mask.masked
    val accountState = account.state.value
    val accountName = (accountState as? SectionState.Ready)?.data?.name
    // Holdings are fetched only once the SERVER has told us this account can
    // hold securities (INVESTMENT_ACCOUNT_TYPES). While the account row is
    // still loading or has failed, the section is simply not composed — the
    // Details section already owns that failure state, and a cash account
    // never triggers the positions route's refresh fan-out.
    val holdsPositions = (accountState as? SectionState.Ready)?.data?.type
        ?.let { it in INVESTMENT_ACCOUNT_TYPES } == true
    LaunchedEffect(accountId, holdsPositions) {
        if (holdsPositions) holdings.load()
    }
    Column(
        modifier = modifier
            .fillMaxSize()
            .testTag(ScreenTags.ACCOUNT_DETAIL)
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        ScreenHeader(
            title = accountName ?: "Account",
            mask = mask,
            demo = demo,
            // The app bar owns the privacy action on this screen (it must
            // never scroll away), so the header renders no second toggle.
            showMask = false,
            // Security S1: the route id renders only under the server's own
            // verdict for the CURRENT identity — an unconfirmed route id is
            // never rendered (it could name another tenant's account).
            subtitle = if (accountState.hasServerData()) "Account $accountId" else null,
        )
        if (authState !is AuthState.Authenticated) {
            SectionBody(state = SectionState.Expired, onRetry = {})
            return@Column
        }

        SectionBody(state = account.state.value, onRetry = account::retry) { row ->
            Column(
                modifier = Modifier.fillMaxWidth(),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                if (row.totalValue != null) {
                    AccountBalanceHero(row = row, hero = row.totalValue, masked = masked)
                }
                SectionCaption("Details")
                AccountDetailFields(row = row, masked = masked)
            }
        }

        if (holdsPositions) {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag(AccountDetailTags.HOLDINGS),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                SectionCaption("Holdings")
                SectionBody(
                    state = holdings.state.value,
                    onRetry = holdings::retry,
                    emptyHeading = "No holdings yet",
                    emptyBody = "Holdings appear here once trades are recorded for this account.",
                    staleNote = { page -> positionStaleNote(page.items) },
                ) { page ->
                    AccountHoldingsContent(page = page, masked = masked, onOpenHolding = onOpenHolding)
                }
            }
        }

        SectionCaption("Ledger")
        SectionBody(
            state = ledger.state.value,
            onRetry = ledger::retry,
            emptyHeading = "No entries yet",
            emptyBody = "Entries appear once this account has recorded transactions.",
        ) { page ->
            LedgerContent(
                page = page,
                currency = (account.state.value as? SectionState.Ready)?.data?.currency,
                masked = masked,
                moreSource = ledgerMoreSource,
            )
        }
    }
}

/**
 * Frame A's balance hero (mockup `android-companion-concept.html`): the
 * account's ONE total figure at key-balance size, above the field list.
 * This owns `total_value`, which is why [AccountDetailFields] renders no
 * `Balance` row — the PM de-dup ruling keeps that figure in exactly one
 * place (docs/56 §2.5).
 */
@Composable
private fun AccountBalanceHero(row: AccountRow, hero: String, masked: Boolean) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .testTag(AccountDetailTags.BALANCE_HERO),
        verticalArrangement = Arrangement.spacedBy(2.dp),
    ) {
        Text(
            "Balance",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Row(verticalAlignment = Alignment.CenterVertically) {
            // displaySmall = the scale's key-balance style (32sp, tnum).
            CompositionLocalProvider(LocalTextStyle provides MaterialTheme.typography.displaySmall) {
                MaskedText(
                    text = AccountValueFormat.amount(hero),
                    masked = masked,
                    testTagValue = AccountDetailTags.HERO_VALUE,
                )
            }
            Text(
                " ${row.currency}",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

@Composable
private fun AccountDetailFields(row: AccountRow, masked: Boolean) {
    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
        DetailRow(label = "Type", value = row.typeLabel)
        DetailRow(label = "Currency", value = row.currency)
        if (row.type == "brokerage_non_registered") {
            DetailRow(label = "Tax treatment", value = "Taxable (non-registered)")
        }
        DetailRow(label = "Starting balance", value = AccountValueFormat.amount(row.startingBalance), masked = masked)
        // Frame A de-dup (PM ruling): a cash balance equal to the hero's
        // total would print the same number twice — the hero owns it.
        // Starting balance and (non-zero) investment value stay: they are
        // the composition the hero is made of.
        if (row.cashBalance != null && !sameFigure(row.cashBalance, row.totalValue)) {
            DetailRow(label = "Cash balance", value = AccountValueFormat.amount(row.cashBalance), masked = masked)
        }
        // A literal 0.00 investment line on a CASH account is noise beside
        // the hero (PM ruling). Investment accounts keep it even at zero —
        // "investments are worth nothing yet" is honest composition there.
        if (row.type in INVESTMENT_ACCOUNT_TYPES || !isZeroFigure(row.investmentValue)) {
            DetailRow(label = "Investment value", value = AccountValueFormat.amount(row.investmentValue), masked = masked)
        }
        // NO `Balance` DetailRow: the hero above owns total_value — the
        // de-dup ruling drops this row whenever it would equal the hero,
        // and without a hero there is no figure to repeat either.
        DetailRow(
            label = "Last sync",
            value = ScreenFormat.serverInstant(row.lastSyncAt) ?: "Not recorded",
        )
        StatusChip(providerStatusChip(row), Modifier.padding(top = 4.dp))
    }
}

/**
 * Display-level figure equality — comparison ONLY, never arithmetic over
 * money: numerically equal decimals are the same figure regardless of
 * trailing-zero serialization ("10" == "10.00"), and an unparsable server
 * string is equal only to itself verbatim.
 */
private fun sameFigure(a: String?, b: String?): Boolean {
    if (a == null || b == null) return false
    val left = a.toBigDecimalOrNull() ?: return a == b
    val right = b.toBigDecimalOrNull() ?: return a == b
    return left.compareTo(right) == 0
}

/** True only when the server string parses to a literal zero (junk never does). */
private fun isZeroFigure(value: String?): Boolean {
    val figure = value?.toBigDecimalOrNull() ?: return false
    return figure.signum() == 0
}

// ---------------------------------------------------------------------------
// Holdings — the server's account-scoped positions, rendered per row
// ---------------------------------------------------------------------------

/**
 * One holding row: symbol + quantity left, the server's market value right
 * (listing currency, with the portfolio-currency figure beneath it), then the
 * shared freshness/provenance chips. Nothing here is summed, converted or
 * re-priced — every figure is a server string, formatted for display only
 * ([AccountValueFormat]) and masked when masking is on (docs/46 D2, docs/53
 * AC2.1). Quantity is a unit count, not a monetary value, so it renders
 * unmasked — the same treatment [HoldingDetailScreen] gives it.
 *
 * The whole row is ONE tap target into `Routes.HOLDING_DETAIL`
 * (`onClickLabel`, never a short `contentDescription` — a content
 * description would suppress the row's own text for TalkBack).
 */
@Composable
private fun AccountPositionRow(row: PositionRow, masked: Boolean, onClick: () -> Unit) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .testTag(AccountDetailTags.holdingRow(row.securityId))
            .clickable(onClickLabel = "open holding details", onClick = onClick)
            .padding(vertical = 8.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(row.symbol, style = MaterialTheme.typography.titleSmall)
                Text(
                    "qty ${AccountValueFormat.quantity(row.quantity)}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Column(horizontalAlignment = Alignment.End) {
                if (row.marketValueNative != null) {
                    AmountText(
                        amount = AccountValueFormat.amount(row.marketValueNative),
                        currency = row.listingCurrency,
                        masked = masked,
                        testTagAmount = "account_holding_value",
                    )
                } else {
                    Text(
                        "No market value",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                if (row.marketValueCad != null) {
                    Row {
                        Text(
                            "≈ ",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        MaskedText(
                            text = AccountValueFormat.amount(row.marketValueCad),
                            masked = masked,
                            testTagValue = "account_holding_value_cad",
                        )
                        Text(
                            " (portfolio)",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            }
        }
        if (row.unrealizedGainCad != null) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    "Unrealized gain ",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                MaskedText(
                    text = AccountValueFormat.amount(row.unrealizedGainCad),
                    masked = masked,
                    testTagValue = "account_holding_gain",
                )
                Text(
                    " (portfolio)",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        // Price + provenance come from the shared chips; the wire model stays
        // verbatim, so the price is formatted on a display-only copy.
        PositionFreshnessChips(row = row.withFormattedPrice(), masked = masked)
    }
}

/** Display-only copy with the price formatted for reading; never persisted. */
private fun PositionRow.withFormattedPrice(): PositionRow =
    copy(price = price?.let { AccountValueFormat.price(it) })

@Composable
private fun AccountHoldingsContent(
    page: PositionsPage,
    masked: Boolean,
    onOpenHolding: (accountId: Int, securityId: Int) -> Unit,
) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        page.items.forEach { row ->
            AccountPositionRow(
                row = row,
                masked = masked,
                onClick = { onOpenHolding(row.accountId, row.securityId) },
            )
            HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
        }
    }
}

// ---------------------------------------------------------------------------
// Ledger — the account's committed transactions with server running balances
// ---------------------------------------------------------------------------

/**
 * One ledger row's display data. [runningBalance] is always the SERVER's
 * own figure for that row (the ledger line, joined by transaction id) —
 * null only when a paginated row has no ledger counterpart, and then the
 * balance line is simply absent: a running balance is never invented here
 * (docs/53 AC1.3).
 */
private class LedgerRowDisplay(
    val transactionId: Int,
    val date: String,
    val description: String,
    val amount: String,
    val runningBalance: String?,
)

@Composable
private fun LedgerContent(
    page: LedgerPage,
    currency: String?,
    masked: Boolean,
    moreSource: (limit: Int, offset: Int) -> SectionPresenter<TransactionsPage>,
) {
    val initialWindow = minOf(LEDGER_PAGE_SIZE, page.items.size)
    var extraRows by remember(page) { mutableStateOf<List<LedgerRowDisplay>>(emptyList()) }
    var loadingMore by remember(page) { mutableStateOf(false) }
    var moreError by remember(page) { mutableStateOf(false) }
    var noMoreFromServer by remember(page) { mutableStateOf(false) }
    var inFlight by remember(page) { mutableStateOf<SectionPresenter<TransactionsPage>?>(null) }

    val shown = initialWindow + extraRows.size
    val hasMore = shown < page.total && !noMoreFromServer
    val nextLimit = minOf(LEDGER_PAGE_SIZE, page.total - shown)

    // Land ONE in-flight window: append its rows (deduped by id), or arm the
    // honest retry note. Declared before the observer that calls it.
    fun landMoreRows(data: TransactionsPage) {
        val ledgerLines = page.items.associateBy { it.transactionId }
        // The server answers newest-first; reversed, the window appends in
        // the ledger's ascending order (rows 20…39 behind rows 0…19).
        val incoming = data.items.map { tx ->
            LedgerRowDisplay(
                transactionId = tx.id,
                date = tx.date,
                // Same rule the server uses for the ledger line:
                // normalized payee, else the raw description.
                description = tx.payeeOrDescription,
                amount = tx.amountNative,
                runningBalance = ledgerLines[tx.id]?.runningBalance,
            )
        }.reversed()
        if (incoming.isEmpty()) {
            noMoreFromServer = true
        } else {
            extraRows = (extraRows + incoming).distinctBy { it.transactionId }
        }
        loadingMore = false
        moreError = false
        inFlight = null
    }

    val flight = inFlight?.state?.value
    LaunchedEffect(flight) {
        val state = flight ?: return@LaunchedEffect
        when (state) {
            is SectionState.Ready -> landMoreRows(state.data)
            is SectionState.Refreshing -> landMoreRows(state.data)
            is SectionState.Stale -> landMoreRows(state.data)
            is SectionState.NoData -> {
                noMoreFromServer = true
                loadingMore = false
                inFlight = null
            }
            SectionState.Loading -> Unit // this window's first paint — wait
            else -> {
                // Offline / Unavailable / NotFound / Permission / Expired:
                // keep every row already on screen, say so once, leave the
                // button armed — the next tap retries this same window.
                moreError = true
                loadingMore = false
                inFlight = null
            }
        }
    }

    val visibleRows = page.items.take(initialWindow).map { entry ->
        LedgerRowDisplay(
            transactionId = entry.transactionId,
            date = entry.date,
            description = entry.description,
            amount = entry.amount,
            runningBalance = entry.runningBalance,
        )
    } + extraRows

    val loadMore: () -> Unit = {
        if (!loadingMore && hasMore) {
            loadingMore = true
            moreError = false
            // Window arithmetic over DESC pages: the rows after the ones on
            // screen sit at offset (total − shown − limit) from the newest
            // end; the response comes back newest-first and is appended in
            // REVERSED order so the list keeps the ledger's ascending order.
            val offset = page.total - shown - nextLimit
            val presenter = moreSource(nextLimit, offset)
            inFlight = presenter
            presenter.load()
        }
    }

    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("starting ", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                AmountText(
                    amount = AccountValueFormat.amount(page.startingBalance),
                    currency = currency,
                    masked = masked,
                    testTagAmount = "ledger_starting",
                )
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("balance ", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                AmountText(
                    amount = AccountValueFormat.amount(page.currentBalance),
                    currency = currency,
                    masked = masked,
                    testTagAmount = "ledger_current",
                )
            }
        }
        visibleRows.forEach { row ->
            LedgerEntryItem(row = row, currency = currency, masked = masked)
        }
        if (hasMore) {
            if (moreError) {
                Text(
                    "Couldn't load more entries.",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            TextButton(
                onClick = loadMore,
                enabled = !loadingMore,
                modifier = Modifier
                    .fillMaxWidth()
                    // Explicit 48dp floor — the shared small button tokens
                    // would undershoot the 44px tap contract (docs/09).
                    .heightIn(min = 48.dp)
                    .testTag(AccountDetailTags.SHOW_MORE),
            ) {
                Text(
                    "Show $nextLimit more",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.primary,
                )
            }
        }
    }
}

/**
 * date │ description │ money rail, ONE tap target: the description clamps
 * to two lines and the tap expands it in place (second tap collapses) —
 * never navigation; there is no transaction-detail route. The right rail
 * carries amount+currency on top and the server's running balance beneath
 * it at 12sp muted tabular, with the currency printed ONCE per row.
 */
@Composable
private fun LedgerEntryItem(row: LedgerRowDisplay, currency: String?, masked: Boolean) {
    var expanded by remember(row.transactionId) { mutableStateOf(false) }
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(
                onClickLabel = if (expanded) "collapse description" else "show full description",
                onClick = { expanded = !expanded },
            )
            .testTag(AccountDetailTags.ledgerRow(row.transactionId))
            .padding(vertical = 6.dp),
        verticalAlignment = Alignment.Top,
    ) {
        Text(
            row.date,
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(end = 8.dp),
        )
        // Contract (PM ruling #2): clamp to 2 lines + tap-to-expand IN
        // PLACE. The clamp is visual only — the semantics always carry the
        // full description — and expanding flips maxLines to unlimited.
        Text(
            row.description,
            style = MaterialTheme.typography.bodyMedium,
            maxLines = if (expanded) Int.MAX_VALUE else 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier
                .weight(1f)
                .testTag(AccountDetailTags.ledgerDescription(row.transactionId)),
        )
        Column(
            horizontalAlignment = Alignment.End,
            modifier = Modifier.padding(start = 8.dp),
        ) {
            AmountText(
                amount = AccountValueFormat.amount(row.amount),
                currency = currency,
                masked = masked,
                testTagAmount = "ledger_amount",
            )
            if (row.runningBalance != null) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        "balance ",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    // 12sp muted + tabular numerals (docs/56 §2.3): the rail
                    // line scans under the amount with no currency repeat.
                    CompositionLocalProvider(
                        LocalTextStyle provides MaterialTheme.typography.labelSmall.merge(
                            TextStyle(
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                fontFeatureSettings = "tnum",
                            ),
                        ),
                    ) {
                        AmountText(
                            amount = AccountValueFormat.amount(row.runningBalance),
                            currency = null,
                            masked = masked,
                            testTagAmount = "ledger_balance",
                        )
                    }
                }
            }
        }
    }
    HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
}
