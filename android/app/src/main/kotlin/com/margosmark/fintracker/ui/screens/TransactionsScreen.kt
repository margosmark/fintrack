package com.margosmark.fintracker.ui.screens

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import com.margosmark.fintracker.finance.FinanceRepository
import com.margosmark.fintracker.finance.TransactionEventRow
import com.margosmark.fintracker.finance.TransactionEventsPage
import com.margosmark.fintracker.finance.TransactionRow
import com.margosmark.fintracker.finance.TransactionsPage
import com.margosmark.fintracker.navigation.ScreenTags
import com.margosmark.fintracker.session.AuthState
import com.margosmark.fintracker.state.MaskedText
import com.margosmark.fintracker.ui.masking.MaskController

fun transactionsSectionPresenter(repository: FinanceRepository): SectionPresenter<TransactionsPage> =
    SectionPresenter(
        fetch = { repository.transactions() },
        // Withheld rows are NOT "no data" — they get their own in-place
        // explanation (docs/53 AC1.3 permission/withheld contract).
        emptyWhen = { page -> page.items.isEmpty() && page.withheld == 0 },
    )

/**
 * The transactions list (docs/48 §3.5, docs/53 AC1.2 drill-in).
 *
 * The list ROW is the record — there is no transaction-detail endpoint
 * (docs/52 §3). The row's own drill-in is its audit history via
 * `GET /transactions/{id}/events`, loaded lazily per row.
 *
 * `total` vs `items.size` discloses pagination honestly instead of implying
 * the page is everything.
 */
@Composable
fun TransactionsScreen(
    transactions: SectionPresenter<TransactionsPage>,
    /** Per-row audit history factory (`GET /transactions/{id}/events`); null hides the affordance. */
    eventsSource: ((Int) -> SectionPresenter<TransactionEventsPage>)?,
    mask: MaskController,
    demo: Boolean,
    modifier: Modifier = Modifier,
    authState: AuthState = AuthState.SignedOut,
) {
    LaunchedEffect(Unit) { transactions.load() }
    val masked = mask.masked
    Column(
        modifier = modifier
            .fillMaxSize()
            .testTag(ScreenTags.TRANSACTIONS)
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        ScreenHeader(title = "Transactions", mask = mask, demo = demo)
        if (authState !is AuthState.Authenticated) {
            SectionBody(state = SectionState.Expired, onRetry = {})
            return@Column
        }
        SectionBody(
            state = transactions.state.value,
            onRetry = transactions::retry,
            emptyHeading = "No transactions yet",
            emptyBody = "Transactions appear here once data is imported into FinTracker.",
        ) { page ->
            Column(verticalArrangement = Arrangement.spacedBy(0.dp)) {
                if (page.withheld > 0) {
                    Text(
                        "${page.withheld} rows are withheld by your privacy settings and are not shown.",
                        modifier = Modifier.testTag(SectionTags.WITHHELD_NOTE),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                page.items.forEach { row ->
                    TransactionRowItem(row = row, masked = masked, eventsSource = eventsSource)
                }
                if (page.total > page.items.size) {
                    Text(
                        "Showing ${page.items.size} of ${page.total} · full list on the web",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(top = 8.dp),
                    )
                }
            }
        }
    }
}

@Composable
private fun TransactionRowItem(
    row: TransactionRow,
    masked: Boolean,
    eventsSource: ((Int) -> SectionPresenter<TransactionEventsPage>)?,
) {
    var expanded by remember { mutableStateOf(false) }
    Column {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .clickable { expanded = !expanded }
                .semantics { contentDescription = "Transaction ${row.rawDescription} on ${row.date}" }
                .padding(vertical = 8.dp),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    row.date,
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(end = 8.dp),
                )
                Text(
                    row.rawDescription,
                    style = MaterialTheme.typography.bodyMedium,
                    modifier = Modifier.weight(1f),
                )
                Column(horizontalAlignment = Alignment.End) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        MaskedText(
                            text = row.amountNative,
                            masked = masked,
                            testTagValue = "tx_amount",
                        )
                        Text(
                            " ${row.currency}",
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    if (row.cadAmount != null && row.cadAmount != row.amountNative) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            MaskedText(
                                text = row.cadAmount,
                                masked = masked,
                                testTagValue = "tx_amount_cad",
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
            Row(
                modifier = Modifier.fillMaxWidth().padding(top = 4.dp),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                if (row.isRecurring) StatusChip(text = "Recurring")
                if (row.hasReceipt) StatusChip(text = "Receipt")
                if (row.isSplit) StatusChip(text = "Split")
                StatusChip(
                    text = if (row.reviewedAt != null) "Reviewed" else "Unreviewed",
                    emphasize = row.reviewedAt == null,
                )
                if (eventsSource != null) {
                    StatusChip(text = if (expanded) "Hide history" else "History")
                }
            }
        }

        if (eventsSource != null) {
            AnimatedVisibility(visible = expanded) {
                TransactionEventsCell(txId = row.id, eventsSource = eventsSource, masked = masked)
            }
        }
        HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
    }
}

/**
 * The per-row audit trail (`GET /transactions/{id}/events`): kind, summary,
 * actor and server event time. `at` may be null for legacy actions — the
 * segment is OMITTED then, never given a fabricated time (AC4.1).
 */
@Composable
private fun TransactionEventsCell(
    txId: Int,
    eventsSource: (Int) -> SectionPresenter<TransactionEventsPage>,
    masked: Boolean,
) {
    val presenter = remember(txId) { eventsSource(txId) }
    LaunchedEffect(txId) { presenter.load() }
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(start = 8.dp, bottom = 8.dp)
            .testTag("tx_events_$txId"),
        verticalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        SectionBody(
            state = presenter.state.value,
            onRetry = presenter::retry,
            emptyHeading = "No recorded history",
            emptyBody = "Actions on this transaction appear here as the server records them.",
        ) { page ->
            page.items.forEach { event -> TransactionEventLine(event = event) }
        }
    }
}

fun eventsPresenter(repository: FinanceRepository, txId: Int): SectionPresenter<TransactionEventsPage> =
    SectionPresenter(
        fetch = { repository.transactionEvents(txId) },
        emptyWhen = { page -> page.items.isEmpty() },
    )

@Composable
private fun TransactionEventLine(event: TransactionEventRow) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text(
            event.eventKind,
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.primary,
        )
        Column(Modifier.weight(1f)) {
            Text(event.summary, style = MaterialTheme.typography.bodySmall)
            val at = ScreenFormat.serverInstant(event.at)
            Text(
                listOfNotNull(event.actorLabel, at).joinToString(" · "),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}
