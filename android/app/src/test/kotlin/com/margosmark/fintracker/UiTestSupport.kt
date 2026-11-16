package com.margosmark.fintracker

import com.margosmark.fintracker.finance.AccountRow
import com.margosmark.fintracker.finance.AccountsPage
import com.margosmark.fintracker.finance.LedgerPage
import com.margosmark.fintracker.finance.PositionRow
import com.margosmark.fintracker.finance.PositionsPage
import com.margosmark.fintracker.finance.TransactionEventsPage
import com.margosmark.fintracker.finance.TransactionsPage
import com.margosmark.fintracker.session.AuthState
import com.margosmark.fintracker.ui.AppScope
import com.margosmark.fintracker.ui.InMemoryDestinationPreserver
import com.margosmark.fintracker.ui.SectionSources
import com.margosmark.fintracker.ui.masking.MaskController
import com.margosmark.fintracker.ui.screens.PortfolioSection
import com.margosmark.fintracker.ui.screens.SectionPresenter
import com.margosmark.fintracker.ui.screens.SignInActions
import java.io.IOException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * Nav-graph test scope: every section fails instantly with an IOException
 * (Offline state) — NO sockets are opened, so graph tests stay hermetic
 * while the screens render their real chrome (headers, tags, session gate).
 */
private class NeverLoadingSections : SectionSources {
    private fun <T> offline(): SectionPresenter<T> =
        SectionPresenter(fetch = { throw IOException("nav test: no data layer") })

    override fun portfolio(): SectionPresenter<PortfolioSection> = offline()
    override fun accounts(): SectionPresenter<AccountsPage> = offline()
    override fun accountsList(): SectionPresenter<AccountsPage> = offline()
    override fun transactions(): SectionPresenter<TransactionsPage> = offline()
    override fun account(accountId: Int): SectionPresenter<AccountRow> = offline()
    override fun ledger(accountId: Int): SectionPresenter<LedgerPage> = offline()
    override fun holdings(accountId: Int): SectionPresenter<PositionsPage> = offline()
    override fun holding(accountId: Int, securityId: Int): SectionPresenter<PositionRow> = offline()
    override fun events(transactionId: Int): SectionPresenter<TransactionEventsPage> = offline()
    override fun accountTransactions(accountId: Int, limit: Int, offset: Int): SectionPresenter<TransactionsPage> =
        offline()
}

private object RecordingSignInActions : SignInActions {
    override fun signIn(email: String, password: String, onFailure: (String) -> Unit) = Unit
    override fun submitTotp(code: String, onFailure: (String) -> Unit) = Unit
    override fun exploreDemo() = Unit
}

fun testAppScope(
    auth: StateFlow<AuthState> = MutableStateFlow(AuthState.SignedOut),
): AppScope = AppScope(
    sections = NeverLoadingSections(),
    auth = auth,
    mask = MaskController(initiallyMasked = true),
    signInActions = RecordingSignInActions,
    destinations = InMemoryDestinationPreserver(),
    onSignOut = {},
)
