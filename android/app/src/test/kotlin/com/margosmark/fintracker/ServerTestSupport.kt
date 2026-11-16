package com.margosmark.fintracker

import com.margosmark.fintracker.finance.AccountsPage
import com.margosmark.fintracker.finance.FinanceRepository
import com.margosmark.fintracker.finance.PositionsPage
import com.margosmark.fintracker.http.ApiClient
import com.margosmark.fintracker.http.PersistentCookieJar
import com.margosmark.fintracker.session.AuthState
import com.margosmark.fintracker.ui.AppScope
import com.margosmark.fintracker.ui.InMemoryDestinationPreserver
import com.margosmark.fintracker.ui.SectionSources
import com.margosmark.fintracker.ui.masking.MaskController
import com.margosmark.fintracker.ui.screens.SectionPresenter
import com.margosmark.fintracker.ui.screens.SignInActions
import com.margosmark.fintracker.ui.screens.accountDetailPresenter
import com.margosmark.fintracker.ui.screens.accountHoldingsPresenter
import com.margosmark.fintracker.ui.screens.accountLedgerPresenter
import com.margosmark.fintracker.ui.screens.accountTransactionsPresenter
import com.margosmark.fintracker.ui.screens.accountsSectionPresenter
import com.margosmark.fintracker.ui.screens.eventsPresenter
import com.margosmark.fintracker.ui.screens.holdingDetailPresenter
import com.margosmark.fintracker.ui.screens.portfolioSectionPresenter
import com.margosmark.fintracker.ui.screens.transactionsSectionPresenter
import java.io.File
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest

/**
 * A REAL read stack against [MockWebServer]: ApiClient (parent card's) →
 * FinanceRepository → the same presenter factories the nav graph wires.
 * No production host is ever contacted (docs/53 evidence rules).
 */
class FakeServer : AutoCloseable {
    val server = MockWebServer()

    val client: ApiClient = ApiClient(
        baseUrl = server.url("/api/v1").toString(),
        cookieJar = PersistentCookieJar(
            storeFile = File.createTempFile("cookie", ".enc"),
            cipher = TestCipher(),
            persistenceEnabled = false,
        ),
    )

    val repository = FinanceRepository(client)

    /** Queue a 200 JSON body for the next request. */
    fun enqueue(body: String, code: Int = 200) {
        server.enqueue(
            MockResponse()
                .setResponseCode(code)
                .setHeader("Content-Type", "application/json")
                .setBody(body),
        )
    }

    /** Queue a failure of the requested kind. */
    fun enqueueError(code: Int, body: String = """{"detail":"x","code":"y"}""") {
        server.enqueue(MockResponse().setResponseCode(code).setBody(body))
    }

    fun enqueueOffline() {
        // A shut-down server: the next call fails with IOException.
        server.shutdown()
    }

    /**
     * Serve canned JSON per PATH PREFIX - order-independent, so concurrent
     * section fetches (glance fires several at once) each get the right body.
     */
    fun serveByPath(vararg routes: Pair<String, String>) {
        val table = routes.toList()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val path = request.requestUrl?.encodedPath ?: ""
                val body = table.firstOrNull { path.contains(it.first) }?.second
                    ?: return MockResponse().setResponseCode(404)
                        .setBody("{\"detail\":\"no canned route for $path\",\"code\":\"not_found\"}")
                return MockResponse()
                    .setResponseCode(200)
                    .setHeader("Content-Type", "application/json")
                    .setBody(body)
            }
        }
    }

    /** Serve the same status for EVERY request (401/403/500 scenarios). */
    fun serveStatus(code: Int) {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = MockResponse()
                .setResponseCode(code)
                .setHeader("Content-Type", "application/json")
                .setBody("{\"detail\":\"synthetic\",\"code\":\"synthetic\"}")
        }
    }

    /** Every request the fake server has seen, in order. */
    fun requests(): List<RecordedRequest> {
        val seen = mutableListOf<RecordedRequest>()
        while (true) {
            val next = try {
                server.takeRequest(50, java.util.concurrent.TimeUnit.MILLISECONDS)
            } catch (_: Exception) {
                null
            }
            if (next == null) break
            seen += next
        }
        return seen
    }

    override fun close() {
        try {
            server.shutdown()
        } catch (_: Exception) {
        }
    }
}

/** An AppScope whose sections all read from [FakeServer]'s repository. */
fun serverBackedScope(
    server: FakeServer,
    auth: StateFlow<AuthState> = MutableStateFlow(
        AuthState.Authenticated(
            com.margosmark.fintracker.http.UserPublic(
                id = 1,
                email = "t@est.local",
                name = null,
                privacyMode = false,
            ),
        ),
    ),
): AppScope {
    val sections = object : SectionSources {
        override fun portfolio(): SectionPresenter<com.margosmark.fintracker.ui.screens.PortfolioSection> =
            portfolioSectionPresenter(server.repository)
        override fun accounts(): SectionPresenter<AccountsPage> = accountsSectionPresenter(server.repository)
        override fun accountsList(): SectionPresenter<AccountsPage> = accountsSectionPresenter(server.repository)
        override fun transactions() = transactionsSectionPresenter(server.repository)
        override fun account(accountId: Int) = accountDetailPresenter(server.repository, accountId)
        override fun ledger(accountId: Int) = accountLedgerPresenter(server.repository, accountId)
        override fun holdings(accountId: Int) = accountHoldingsPresenter(server.repository, accountId)
        override fun holding(accountId: Int, securityId: Int) =
            holdingDetailPresenter(server.repository, accountId, securityId)
        override fun events(transactionId: Int) = eventsPresenter(server.repository, transactionId)
        override fun accountTransactions(accountId: Int, limit: Int, offset: Int) =
            accountTransactionsPresenter(server.repository, accountId, limit, offset)
    }
    return AppScope(
        sections = sections,
        auth = auth,
        mask = MaskController(initiallyMasked = true),
        signInActions = object : SignInActions {
            override fun signIn(email: String, password: String, onFailure: (String) -> Unit) = Unit
            override fun submitTotp(code: String, onFailure: (String) -> Unit) = Unit
            override fun exploreDemo() = Unit
        },
        destinations = InMemoryDestinationPreserver(),
        onSignOut = {},
    )
}
