package com.margosmark.fintracker.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.margosmark.fintracker.finance.AccountsPage
import com.margosmark.fintracker.navigation.ScreenTags
import com.margosmark.fintracker.session.AuthState
import com.margosmark.fintracker.ui.masking.MaskController

/**
 * The accounts list (docs/48 §3.2): every account's OWN-currency value, its
 * review/provider status and last-sync fact. Nothing is summed across
 * accounts (mixed currencies are never combined client-side, AC1.1).
 */
@Composable
fun AccountsScreen(
    accounts: SectionPresenter<AccountsPage>,
    mask: MaskController,
    demo: Boolean,
    modifier: Modifier = Modifier,
    authState: AuthState = AuthState.SignedOut,
    onOpenAccount: (Int) -> Unit = {},
) {
    LaunchedEffect(Unit) { accounts.load() }
    val masked = mask.masked
    Column(
        modifier = modifier
            .fillMaxSize()
            .testTag(ScreenTags.ACCOUNTS)
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        ScreenHeader(title = "Accounts", mask = mask, demo = demo)
        if (authState !is AuthState.Authenticated) {
            SectionBody(state = SectionState.Expired, onRetry = {})
            return@Column
        }
        SectionBody(
            state = accounts.state.value,
            onRetry = accounts::retry,
            emptyHeading = "No accounts yet",
            emptyBody = "Add and manage accounts in FinTracker on the web.",
        ) { page ->
            AccountRows(page = page, masked = masked, onOpenAccount = onOpenAccount)
        }
    }
}
