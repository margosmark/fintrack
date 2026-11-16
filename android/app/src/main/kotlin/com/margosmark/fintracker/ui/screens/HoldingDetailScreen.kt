package com.margosmark.fintracker.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.margosmark.fintracker.finance.FinanceRepository
import com.margosmark.fintracker.finance.PositionRow
import com.margosmark.fintracker.http.ApiException
import com.margosmark.fintracker.navigation.ScreenTags
import com.margosmark.fintracker.state.MaskedText
import com.margosmark.fintracker.session.AuthState
import com.margosmark.fintracker.ui.masking.MaskController

/**
 * One holding's drill-in (docs/48 §3.4): the position record for ONE
 * security in ONE account, with the full provenance row. A symbol with no
 * position (removed / not held in this account) fails as 404 → the honest
 * NotFound pane — v1 does not fabricate "not held" record cards.
 */
fun holdingDetailPresenter(
    repository: FinanceRepository,
    accountId: Int,
    securityId: Int,
): SectionPresenter<PositionRow> = SectionPresenter(
    fetch = {
        val page = repository.positions(accountId)
        page.items.firstOrNull { it.securityId == securityId }
            ?: throw ApiException(404, "not_found", "This holding is not in the account.")
    },
    emptyWhen = { false },
)

@Composable
fun HoldingDetailScreen(
    accountId: Int,
    securityId: Int,
    holding: SectionPresenter<PositionRow>,
    mask: MaskController,
    demo: Boolean,
    modifier: Modifier = Modifier,
    authState: AuthState = AuthState.SignedOut,
) {
    LaunchedEffect(accountId, securityId) { holding.load() }
    val masked = mask.masked
    val holdingState = holding.state.value
    val symbol = (holdingState as? SectionState.Ready)?.data?.symbol
    Column(
        modifier = modifier
            .fillMaxSize()
            .testTag(ScreenTags.HOLDING_DETAIL)
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        ScreenHeader(
            title = symbol ?: "Holding",
            mask = mask,
            demo = demo,
            // Security S1: route ids render only under the server's own
            // verdict for the CURRENT identity — an unconfirmed route id is
            // never rendered (it could name another tenant's holding).
            subtitle = if (holdingState.hasServerData()) {
                "Account $accountId · holding $securityId"
            } else {
                null
            },
        )
        if (authState !is AuthState.Authenticated) {
            SectionBody(state = SectionState.Expired, onRetry = {})
            return@Column
        }
        SectionBody(
            state = holding.state.value,
            onRetry = holding::retry,
            emptyHeading = "No holding to show",
            emptyBody = "This holding is not part of your data.",
        ) { row ->
            HoldingFields(row = row, masked = masked)
        }
    }
}

@Composable
private fun HoldingFields(row: PositionRow, masked: Boolean) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(
            "${row.accountName} · ${row.listingCurrency}",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        DetailRow(label = "Quantity", value = row.quantity)
        if (row.price != null) {
            DetailRow(label = "Price", value = row.price, masked = masked)
        }
        if (row.marketValueNative != null) {
            DetailRow(label = "Market value (${row.listingCurrency})", value = row.marketValueNative, masked = masked)
        }
        if (row.marketValueCad != null) {
            DetailRow(label = "Market value (portfolio)", value = row.marketValueCad, masked = masked)
        }
        if (row.bookCostCad != null) {
            DetailRow(label = "Book cost (portfolio)", value = row.bookCostCad, masked = masked)
        }
        if (row.dayChangeNative != null) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    "Day change ",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                MaskedText(text = row.dayChangeNative, masked = masked, testTagValue = "holding_day_change")
                if (row.dayChangePct != null) {
                    Text(
                        " (${row.dayChangePct}%)",
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
        if (row.unrealizedGainCad != null) {
            DetailRow(label = "Unrealized gain (portfolio)", value = row.unrealizedGainCad, masked = masked)
        }

        PositionFreshnessChips(row = row, masked = masked)
    }
}
