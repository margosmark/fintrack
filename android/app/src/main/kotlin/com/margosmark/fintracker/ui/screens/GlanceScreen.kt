package com.margosmark.fintracker.ui.screens

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
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import com.margosmark.fintracker.finance.AccountRow
import com.margosmark.fintracker.finance.AccountsPage
import com.margosmark.fintracker.finance.FinanceRepository
import com.margosmark.fintracker.finance.PositionRow
import com.margosmark.fintracker.finance.PositionsPage
import com.margosmark.fintracker.finance.typeLabel
import com.margosmark.fintracker.navigation.ScreenTags
import com.margosmark.fintracker.session.AuthState
import com.margosmark.fintracker.state.MaskedText
import com.margosmark.fintracker.ui.masking.MaskController

/**
 * Everything the portfolio headline needs: the server's positions envelope
 * PLUS the server's reporting-currency label (from `GET /fx/status`), so the
 * headline total is denominated by a SERVER fact and never assumed CAD.
 */
data class PortfolioSection(
    val positions: PositionsPage,
    val reportingCurrency: String,
)

/** Factory shared by wiring and tests — same fetch, same stale/empty rules. */
fun portfolioSectionPresenter(repository: FinanceRepository): SectionPresenter<PortfolioSection> =
    SectionPresenter(
        fetch = {
            val positions = repository.positions()
            val fx = repository.fxStatus()
            PortfolioSection(positions, fx.reportingCurrency)
        },
        staleWhen = { section -> positionStaleNote(section.positions.items) },
        emptyWhen = { section -> section.positions.items.isEmpty() },
    )

fun accountsSectionPresenter(repository: FinanceRepository): SectionPresenter<AccountsPage> =
    SectionPresenter(
        fetch = { repository.accounts() },
        emptyWhen = { page -> page.items.isEmpty() },
    )

/**
 * The masked glance home (docs/48 §Home glance, docs/53 AC1.1/AC1.3).
 *
 * Two INDEPENDENT sections — portfolio and accounts — each with its own
 * [SectionState], so one failing section never collapses the other to zero
 * (AC1.3 "other sections keep working data"). Every figure rendered comes from a
 * server string; the headline uses `portfolio_value_cad`/`total_value_cad`
 * exactly as delivered and the accounts list shows each account's own
 * currency — nothing is summed, converted or combined client-side. Home values
 * are formatted for display without changing the server data.
 */
@Composable
fun GlanceScreen(
    portfolio: SectionPresenter<PortfolioSection>,
    accounts: SectionPresenter<AccountsPage>,
    mask: MaskController,
    demo: Boolean,
    modifier: Modifier = Modifier,
    authState: AuthState = AuthState.SignedOut,
    onOpenAccount: (Int) -> Unit = {},
    onOpenHolding: (accountId: Int, securityId: Int) -> Unit = { _, _ -> },
) {
    LaunchedEffect(Unit) {
        portfolio.load()
        accounts.load()
    }
    val masked = mask.masked
    Column(
        modifier = modifier
            .fillMaxSize()
            .testTag(ScreenTags.GLANCE)
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        ScreenHeader(title = "Home", mask = mask, demo = demo)

        if (authState !is AuthState.Authenticated) {
            SectionBody(state = SectionState.Expired, onRetry = {})
            return@Column
        }

        // --- Portfolio -------------------------------------------------------
        SectionCaption("Portfolio")
        SectionBody(
            state = portfolio.state.value,
            onRetry = portfolio::retry,
            emptyHeading = "No holdings to show",
            emptyBody = "Positions appear here once trades are recorded in FinTracker.",
            staleNote = { section -> positionStaleNote(section.positions.items) },
        ) { section ->
            PortfolioContent(
                section = section,
                masked = masked,
                onOpenHolding = onOpenHolding,
            )
        }

        // --- Accounts --------------------------------------------------------
        SectionCaption("Accounts")
        SectionBody(
            state = accounts.state.value,
            onRetry = accounts::retry,
            emptyHeading = "No accounts yet",
            emptyBody = "Add and manage accounts in FinTracker on the web.",
        ) { page ->
            AccountRows(
                page = page,
                masked = masked,
                onOpenAccount = onOpenAccount,
            )
        }
    }
}

@Composable
fun SectionCaption(text: String, modifier: Modifier = Modifier) {
    Text(
        text,
        modifier = modifier,
        style = MaterialTheme.typography.titleMedium,
        color = MaterialTheme.colorScheme.onSurface,
    )
}

// ---------------------------------------------------------------------------
// Portfolio content — server figures only
// ---------------------------------------------------------------------------

@Composable
private fun PortfolioContent(
    section: PortfolioSection,
    masked: Boolean,
    onOpenHolding: (Int, Int) -> Unit,
) {
    val page = section.positions
    val ccy = section.reportingCurrency

    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        // Headline: server totals, denominated by the server's reporting
        // currency. When cash is unknown the headline falls back to the
        // server's holdings total and the missing cash is NAMED, never
        // filled with zero (AC1.3).
        if (page.portfolioValueCad != null) {
            HeadlineRow(label = "Portfolio value", value = page.portfolioValueCad, currency = ccy, masked = masked)
        } else {
            HeadlineRow(label = "Holdings value", value = page.totalValueCad, currency = ccy, masked = masked)
            Text(
                "No combined total available · cash figure not provided",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        if (page.cashCad != null && page.portfolioValueCad != null) {
            HeadlineRow(label = "Cash", value = page.cashCad, currency = ccy, masked = masked, small = true)
        }

        // Day change: server-computed. Amount masked; PERCENT stays visible
        // per docs/46 D2 (percentages are not masked).
        if (page.totalDayChangeCad != null) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Day change ", style = MaterialTheme.typography.bodyMedium)
                MaskedText(
                    text = HomeNumberFormat.amount(page.totalDayChangeCad),
                    masked = masked,
                    testTagValue = "day_change_value",
                )
                if (page.totalDayChangePct != null) {
                    Text(
                        " (${page.totalDayChangePct}%)",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                }
            }
            val asOf = ScreenFormat.serverDate(page.dayChangeAsOf)
            if (asOf != null) {
                Text(
                    if (page.dayChangeMixedBaseline) {
                        "vs $asOf close · some rows closed earlier"
                    } else {
                        "vs $asOf close"
                    },
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        } else {
            Text(
                "Day change unavailable for this portfolio",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        HorizontalDivider(Modifier.padding(top = 4.dp))

        page.items.forEach { row ->
            PositionRowItem(row = row, masked = masked, onClick = { onOpenHolding(row.accountId, row.securityId) })
        }
    }
}

@Composable
private fun HeadlineRow(
    label: String,
    value: String,
    currency: String,
    masked: Boolean,
    small: Boolean = false,
) {
    Column {
        Text(
            label,
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Row(verticalAlignment = Alignment.Bottom) {
            MaskedText(
                text = HomeNumberFormat.amount(value),
                masked = masked,
                testTagValue = "headline_value",
            )
            Text(
                " $currency",
                style = if (small) {
                    MaterialTheme.typography.bodyMedium
                } else {
                    MaterialTheme.typography.titleMedium
                },
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/**
 * One position row: symbol/account left, server market value right, and the
 * freshness chips built ONLY from server fields (`price`, `price_date`,
 * `price_age_days`, `price_source`; `price_fetched_at` only when evidenced).
 */
@Composable
private fun PositionRowItem(row: PositionRow, masked: Boolean, onClick: () -> Unit) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .semantics { contentDescription = "Position ${row.symbol} in ${row.accountName}" }
            .padding(vertical = 8.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(row.symbol, style = MaterialTheme.typography.titleSmall)
                Text(
                    row.accountName,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Column(horizontalAlignment = Alignment.End) {
                if (row.marketValueNative != null) {
                    AmountText(
                        amount = HomeNumberFormat.amount(row.marketValueNative),
                        currency = row.listingCurrency,
                        masked = masked,
                        testTagAmount = "position_value",
                    )
                } else {
                    Text(
                        "No market value",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                if (row.marketValueCad != null) {
                    androidx.compose.foundation.layout.Row {
                        Text(
                            "≈ ",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        MaskedText(
                            text = HomeNumberFormat.amount(row.marketValueCad),
                            masked = masked,
                            testTagValue = "position_value_cad",
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
        PositionFreshnessChips(row = row, masked = masked)
    }
}

/**
 * Freshness/provenance chips (docs/53 AC4). Rules:
 *  - `price_date`/`price_age_days` (market-date age) rendered as such — they
 *    are NOT the fetch time.
 *  - `price_fetched_at` rendered ONLY when present — null means no evidence
 *    and the segment is OMITTED, never "fetched"/"checked"/"updated".
 *  - manual source renders verbatim and takes precedence (AC4.3).
 *  - `price_stale` renders the honest stale banner (never styled fresh).
 */
@Composable
fun PositionFreshnessChips(row: PositionRow, masked: Boolean = false) {
    if (row.priceStale == true) {
        val note = listOfNotNull(
            row.priceAgeDays?.let { "$it days old" },
            ScreenFormat.serverDate(row.priceDate)?.let { "as of $it" },
        ).joinToString(" · ")
        Text(
            "Showing saved prices${if (note.isEmpty()) "" else " · $note"}",
            modifier = Modifier.testTag("stale"),
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.tertiary,
        )
    }
    Column(modifier = Modifier.fillMaxWidth().padding(top = 4.dp)) {
        if (row.price != null) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Price ", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                MaskedText(text = HomeNumberFormat.price(row.price), masked = masked, testTagValue = "position_price")
                Text(" ${row.listingCurrency}", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            val provenance = listOfNotNull(
                ScreenFormat.serverDate(row.priceDate)?.let { "as of $it" },
                row.priceAgeDays?.let { "$it days old" },
                ScreenFormat.serverInstant(row.priceFetchedAt)?.let { "fetched $it" },
                row.priceSource,
            ).joinToString(" · ")
            if (provenance.isNotEmpty()) {
                Text(provenance, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        } else {
            Text("No price${row.priceSource?.let { " · $it" } ?: ""}", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

// ---------------------------------------------------------------------------
// Account rows (shared by glance home and the accounts screen)
// ---------------------------------------------------------------------------

/** Server account row: own-currency value ONLY — never summed across accounts. */
@Composable
fun AccountRows(
    page: AccountsPage,
    masked: Boolean,
    onOpenAccount: (Int) -> Unit,
) {
    Column {
        page.items.forEach { row ->
            AccountRowItem(row = row, masked = masked, onClick = { onOpenAccount(row.id) })
            HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
        }
    }
}

@Composable
fun AccountRowItem(row: AccountRow, masked: Boolean, onClick: () -> Unit) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .semantics { contentDescription = "Account ${row.name}" }
            .padding(vertical = 8.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(row.name, style = MaterialTheme.typography.titleSmall)
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    StatusChip(text = row.typeLabel)
                    if (row.type == "brokerage_non_registered") {
                        StatusChip(text = "Taxable")
                    }
                }
            }
            Column(horizontalAlignment = Alignment.End) {
                if (row.totalValue != null) {
                    AmountText(
                        amount = HomeNumberFormat.amount(row.totalValue),
                        currency = row.currency,
                        masked = masked,
                        testTagAmount = "account_value",
                    )
                } else {
                    Text(
                        "No value",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
        Row(
            modifier = Modifier.fillMaxWidth().padding(top = 2.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            if (row.pendingImportCount > 0) {
                StatusChip(text = "${row.pendingImportCount} to review", emphasize = true)
            }
            StatusChip(text = providerStatusChip(row))
            val sync = ScreenFormat.serverInstant(row.lastSyncAt)
            Text(
                if (sync != null) "last synced $sync" else "last sync not recorded",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/**
 * Provider status — honest copy, no promises:
 *  - not_configured/never_refreshed → "provider not configured" (no
 *    refresh-promise wording; AC4.4 forbids next-refresh claims).
 *  - healthy/error → the server's own words.
 */
fun providerStatusChip(row: AccountRow): String = when (row.providerStatus) {
    "healthy" -> "Provider ok"
    "error" -> "Provider error"
    "not_configured" -> "Provider not configured"
    "never_refreshed" -> "Provider not configured"
    null -> "Provider not configured"
    else -> row.providerStatus ?: "Provider not configured"
}
