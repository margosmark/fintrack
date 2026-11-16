package com.margosmark.fintracker

import android.os.Bundle
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.navigation.compose.ComposeNavigator
import androidx.navigation.testing.TestNavHostController
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.margosmark.fintracker.finance.AccountsPage
import com.margosmark.fintracker.finance.FinanceRepository
import com.margosmark.fintracker.finance.LedgerPage
import com.margosmark.fintracker.finance.PositionRow
import com.margosmark.fintracker.finance.PositionsPage
import com.margosmark.fintracker.finance.TransactionEventsPage
import com.margosmark.fintracker.finance.TransactionsPage
import com.margosmark.fintracker.http.UserPublic
import com.margosmark.fintracker.navigation.DeepLinkTarget
import com.margosmark.fintracker.navigation.FinTrackerApp
import com.margosmark.fintracker.navigation.Routes
import com.margosmark.fintracker.navigation.ScreenTags
import com.margosmark.fintracker.navigation.filledRoute
import com.margosmark.fintracker.screens.Fixtures
import com.margosmark.fintracker.screens.awaitNodeWithTag
import com.margosmark.fintracker.screens.awaitText
import com.margosmark.fintracker.screens.tagDisplayed
import com.margosmark.fintracker.session.AuthState
import com.margosmark.fintracker.ui.AppScope
import com.margosmark.fintracker.ui.SectionSources
import com.margosmark.fintracker.ui.SessionDestinationPreserver
import com.margosmark.fintracker.ui.masking.MaskController
import com.margosmark.fintracker.ui.screens.PortfolioSection
import com.margosmark.fintracker.ui.screens.SectionPresenter
import com.margosmark.fintracker.ui.screens.SectionTags
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
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue
import kotlinx.coroutines.flow.MutableStateFlow
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.RecordedRequest
import org.junit.After
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.rules.TemporaryFolder
import org.robolectric.annotation.Config

/**
 * Round-3 destination-preservation regressions (docs/53 AC1.3/AC3.2;
 * security S1). One coherent lifecycle:
 *
 *  - F1'  expiry preserves the FILLED route (`account/5`), never the route
 *    pattern (`account/{accountId}`); same-identity re-auth returns there
 *    and REFETCHES it fresh;
 *  - F2'  a signed-out deep link's destination is consumed THROUGH the
 *    existence/tenant gate — no id destination renders while the gate is
 *    in flight (valid or deleted/foreign target); cold start keeps the
 *    same order;
 *  - S1   an identity change at commit hard-clears the preserved
 *    destination — B never navigates to A's route and no prior-tenant id
 *    appears anywhere in B's UI, header included; and a route id never
 *    renders in a detail header without the server's own verdict for the
 *    CURRENT identity.
 *
 * MockWebServer/local synthetic stacks only — no production contact.
 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class DestinationRound3Test {

    @get:Rule
    val composeRule = FlushesBeforeWaitRule(createComposeRule())

    @get:Rule
    val folder = TemporaryFolder()

    private lateinit var stack: TestStack

    @Before
    fun setUp() {
        drained.clear()
    }

    @After
    fun tearDown() {
        if (this::stack.isInitialized) stack.close()
    }

    // ---- fixtures ----------------------------------------------------------

    private fun accountJson(id: Int, name: String): String =
        """{"id":$id,"name":"$name","type":"tfsa","currency":"CAD","cash_balance":"10.00",""" +
            """"investment_value":"100.00","total_value":"110.00","starting_balance":"5.00","last_sync_at":null,""" +
            """"provider_status":null,"pending_import_count":0}"""

    /** Order matters: `serveByPath` matches the first `contains` — specific before generic. */
    private fun serveAccountFive(server: FakeServer) {
        server.serveByPath(
            "/accounts/ledger" to Fixtures.LEDGER_JSON,
            "/accounts/5" to accountJson(5, "Account Five"),
            "/fx/status" to Fixtures.FX_JSON,
            "/portfolio/positions" to Fixtures.POSITIONS_JSON,
            "/transactions" to Fixtures.TRANSACTIONS_JSON,
            "/accounts" to Fixtures.ACCOUNTS_JSON,
        )
    }

    private fun user(id: Int, email: String): UserPublic =
        UserPublic(id = id, email = email, name = null, privacyMode = false)

    private fun authenticated(u: UserPublic): AuthState = AuthState.Authenticated(u)

    private fun newNavController(): TestNavHostController =
        TestNavHostController(ApplicationProvider.getApplicationContext()).also {
            it.navigatorProvider.addNavigator(ComposeNavigator())
        }

    /** [FakeServer] has close() but is not Closeable — same shape as `use`, no shared-harness edits. */
    private inline fun withFakeServer(block: (FakeServer) -> Unit) {
        val server = FakeServer()
        try {
            block(server)
        } finally {
            server.close()
        }
    }

    private fun setAuth(flow: MutableStateFlow<AuthState>, state: AuthState) {
        composeRule.runOnUiThread { flow.value = state }
    }

    /** Requests drained so far, cumulative across snapshots. */
    private val drained = mutableListOf<RecordedRequest>()

    private fun accountFiveFetches(server: FakeServer): Int {
        drained += server.requests()
        return drained.count { it.requestUrl?.encodedPath == "/api/v1/accounts/5" }
    }

    private fun jsonResponse(body: String): MockResponse = MockResponse()
        .setResponseCode(200)
        .setHeader("Content-Type", "application/json")
        .setBody(body)

    // ---- F1': the preserved value is a FILLED route ------------------------

    @Test
    fun `filledRoute fills placeholders and refuses unfillable or unsafe arguments`() {
        val args = Bundle().apply {
            putString(Routes.ARG_ACCOUNT_ID, "5")
            putString(Routes.ARG_HOLDING_ID, "9")
        }
        assertEquals("account/5", filledRoute(Routes.ACCOUNT_DETAIL, args))
        assertEquals("account/5/holding/9", filledRoute(Routes.HOLDING_DETAIL, args))
        assertEquals("glance", filledRoute(Routes.GLANCE, args))
        assertEquals("settings", filledRoute(Routes.SETTINGS, null))
        assertNull(filledRoute(Routes.ACCOUNT_DETAIL, Bundle()), "unfillable placeholder is never preserved")
        assertNull(filledRoute(Routes.ACCOUNT_DETAIL, null))
        assertNull(filledRoute(null, args))
        val unsafe = Bundle().apply { putString(Routes.ARG_ACCOUNT_ID, "5/../settings") }
        assertNull(
            filledRoute(Routes.ACCOUNT_DETAIL, unsafe),
            "an argument that would compose a DIFFERENT route is never preserved",
        )
        val placeholder = Bundle().apply { putString(Routes.ARG_ACCOUNT_ID, "{accountId}") }
        assertNull(filledRoute(Routes.ACCOUNT_DETAIL, placeholder), "a placeholder value is never preserved as a route")
    }

    @Test
    fun `expiry then same-identity re-auth returns to the concrete account route and refetches it (AC1_3, QA F1')`() {
        withFakeServer { server ->
            serveAccountFive(server)
            val auth = MutableStateFlow<AuthState>(AuthState.SignedOut)
            val navController = newNavController()
            val scope = serverBackedScope(server, auth)
            composeRule.setContent {
                FinTrackerApp(scope = scope, navController = navController)
            }
            composeRule.awaitNodeWithTag(ScreenTags.SIGN_IN)

            // A signs in — this identity for the whole test.
            setAuth(auth, authenticated(user(1, "a@example.com")))
            composeRule.awaitNodeWithTag(ScreenTags.GLANCE)

            // A drills into account 5; the server confirms the row, so the id renders.
            composeRule.runOnUiThread { navController.navigate(Routes.accountDetail("5")) }
            composeRule.awaitText("Account Five")
            composeRule.awaitText("Account 5")
            assertEquals(Routes.ACCOUNT_DETAIL, navController.currentBackStackEntry?.destination?.route)
            val fetchesBefore = accountFiveFetches(server)
            assertTrue(fetchesBefore >= 1, "drill-in fetched /accounts/5 at least once (got $fetchesBefore)")

            // Expiry: calm sign-in screen; the FILLED route is preserved (QA F1').
            setAuth(auth, AuthState.SignedOut)
            composeRule.awaitNodeWithTag(ScreenTags.SIGN_IN)
            composeRule.waitUntil(5_000) { scope.destinations.pending == "account/5" }
            assertEquals(
                "account/5",
                scope.destinations.pending,
                "preservation records the FILLED route, never the pattern (QA F1')",
            )
            composeRule.onNodeWithText("Account Five").assertDoesNotExist()

            // Same-identity re-auth: the concrete destination renders AND refetches (AC1.3).
            setAuth(auth, authenticated(user(1, "a@example.com")))
            composeRule.awaitNodeWithTag(ScreenTags.ACCOUNT_DETAIL)
            composeRule.awaitText("Account Five")
            composeRule.awaitText("Account 5")
            val fetchesAfter = accountFiveFetches(server)
            assertTrue(
                fetchesAfter > fetchesBefore,
                "re-auth must REFETCH /accounts/5 fresh: $fetchesBefore -> $fetchesAfter",
            )
        }
    }

    // ---- F2': re-auth navigation waits for the deep-link gate ---------------

    /**
     * QA F2' in this harness's deterministic form: the existence gate resolves
     * BEFORE any id destination exists on the back stack, and the gate's
     * VERDICT decides what renders. (The round-2 probe held the gate open and
     * inspected the screen; a blocking gate double cannot run under the
     * Robolectric compose rule — the withContext(IO) resumption executes
     * inline on the IO worker and the lifecycle move then fails
     * `setCurrentState must be called on the main thread`. So the order is
     * pinned by recording the back stack at the instant the gate decides.)
     */
    private fun runSignedOutDeepLinkGate(exists: Boolean) {
        withFakeServer { server ->
            serveAccountFive(server)
            val auth = MutableStateFlow<AuthState>(AuthState.SignedOut)
            val navController = newNavController()
            val routesAtGate = java.util.Collections.synchronizedList(mutableListOf<String?>())
            val targetExists: (DeepLinkTarget) -> Boolean = {
                routesAtGate += navController.currentBackStackEntry?.destination?.route
                exists
            }
            composeRule.setContent {
                FinTrackerApp(
                    scope = serverBackedScope(server, auth),
                    targetExists = targetExists,
                    deepLinkUri = "fintracker://app/accounts/5",
                    navController = navController,
                )
            }
            composeRule.awaitNodeWithTag(ScreenTags.SIGN_IN)

            // Re-auth: the gate (keyed on auth) resolves the preserved deep
            // link; the session effect hands the destination to it.
            setAuth(auth, authenticated(user(1, "a@example.com")))
            if (exists) {
                composeRule.awaitNodeWithTag(ScreenTags.ACCOUNT_DETAIL)
                composeRule.awaitText("Account Five")
                composeRule.awaitText("Account 5")
            } else {
                composeRule.awaitNodeWithTag(ScreenTags.UNAVAILABLE)
                composeRule.onNodeWithText("Account Five").assertDoesNotExist()
                composeRule.onNodeWithText("Account 5").assertDoesNotExist()
            }
            assertEquals(1, routesAtGate.size, "the gate resolved exactly once")
            val routeAtGate = routesAtGate[0]
            assertTrue(
                routeAtGate != Routes.ACCOUNT_DETAIL && routeAtGate != Routes.HOLDING_DETAIL,
                "no id destination may exist on the back stack when the gate decides (QA F2'): was $routeAtGate",
            )
        }
    }

    @Test
    fun `signed-out deep link then re-auth waits for the gate before rendering a VALID target (AC3_2, QA F2')`() {
        runSignedOutDeepLinkGate(exists = true)
    }

    @Test
    fun `signed-out deep link then re-auth waits for the gate before rendering a DELETED target (AC3_2, QA F2')`() {
        runSignedOutDeepLinkGate(exists = false)
    }

    @Test
    fun `cold start keeps gate-before-render for an id deep link (AC3_2 order preserved)`() {
        // In-memory sections, exactly like the round-2 green cold-start pins
        // in DeepLinkGateTest: this test pins ORDER (where the gate finds the
        // back stack), not data — real-HTTP presenters keep the first frame
        // application open long enough for the gate's IO resumption to run
        // inline on the IO worker, which Robolectric's lifecycle rejects.
        val auth = MutableStateFlow<AuthState>(authenticated(user(1, "a@example.com")))
        val navController = newNavController()
        val routesAtGate = java.util.Collections.synchronizedList(mutableListOf<String?>())
        val targetExists: (DeepLinkTarget) -> Boolean = {
            routesAtGate += navController.currentBackStackEntry?.destination?.route
            true
        }
        composeRule.setContent {
            FinTrackerApp(
                scope = testAppScope(auth),
                targetExists = targetExists,
                deepLinkUri = "fintracker://app/accounts/5",
                navController = navController,
            )
        }
        composeRule.awaitNodeWithTag(ScreenTags.ACCOUNT_DETAIL)
        assertEquals(1, routesAtGate.size, "the gate resolved exactly once")
        assertEquals(
            Routes.GLANCE,
            routesAtGate[0],
            "an id deep link starts at GLANCE; only the gate navigates to the target (AC3.2)",
        )
    }

    // ---- S1: identity change hard-clears the preserved destination ---------

    @Test
    fun `identity change at commit drops A's route - B's UI shows no prior-tenant id anywhere, header included (S1)`() {
        val authFlow = MutableStateFlow<AuthState>(AuthState.SignedOut)
        stack = startStack(folder) { authFlow.value = it }
        val denyAll = AtomicBoolean(false)
        stack.server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                if (denyAll.get()) {
                    return errorResponseBody(401, "Could not validate credentials", "invalid_token")
                }
                val path = request.requestUrl?.encodedPath ?: return MockResponse().setResponseCode(404)
                return when {
                    path.endsWith("/auth/login") -> {
                        val email = Regex("\"email\":\"([^\"]+)\"")
                            .find(request.body.readUtf8())?.groupValues?.get(1)
                        if (email == "b@example.com") {
                            sessionResponse(accessToken = "access-B", userId = 2, email = "b@example.com")
                        } else {
                            sessionResponse(accessToken = "access-A", userId = 1, email = "a@example.com")
                        }
                    }
                    path.endsWith("/auth/logout") -> MockResponse().setResponseCode(204)
                    path.endsWith("/auth/refresh") ->
                        errorResponseBody(401, "Invalid or revoked refresh token", "invalid_or_revoked_refresh_token")
                    path.endsWith("/accounts/ledger") -> jsonResponse(Fixtures.LEDGER_JSON)
                    path.endsWith("/accounts/5") -> jsonResponse(accountJson(5, "TFSA of A"))
                    path.endsWith("/accounts/9") -> jsonResponse(accountJson(9, "B Savings"))
                    path.endsWith("/accounts") -> jsonResponse("""{"items":[${accountJson(9, "B Savings")}],"total":1}""")
                    path.endsWith("/fx/status") -> jsonResponse(Fixtures.FX_JSON)
                    path.endsWith("/portfolio/positions") -> jsonResponse(Fixtures.POSITIONS_JSON)
                    path.endsWith("/transactions") -> jsonResponse(Fixtures.TRANSACTIONS_JSON)
                    else -> MockResponse().setResponseCode(404)
                        .setBody("""{"detail":"no canned route $path","code":"not_found"}""")
                }
            }
        }

        // Production-shaped scope: the REAL session manager + the REAL
        // SessionDestinationPreserver, sections over the same client.
        val repository = FinanceRepository(stack.client)
        val destinations = SessionDestinationPreserver(stack.session)
        val scope = AppScope(
            sections = object : SectionSources {
                override fun portfolio(): SectionPresenter<PortfolioSection> =
                    portfolioSectionPresenter(repository)
                override fun accounts(): SectionPresenter<AccountsPage> = accountsSectionPresenter(repository)
                override fun accountsList(): SectionPresenter<AccountsPage> = accountsSectionPresenter(repository)
                override fun transactions(): SectionPresenter<TransactionsPage> =
                    transactionsSectionPresenter(repository)
                override fun account(accountId: Int): SectionPresenter<com.margosmark.fintracker.finance.AccountRow> =
                    accountDetailPresenter(repository, accountId)
                override fun ledger(accountId: Int): SectionPresenter<LedgerPage> =
                    accountLedgerPresenter(repository, accountId)
                override fun holdings(accountId: Int): SectionPresenter<PositionsPage> =
                    accountHoldingsPresenter(repository, accountId)
                override fun holding(accountId: Int, securityId: Int): SectionPresenter<PositionRow> =
                    holdingDetailPresenter(repository, accountId, securityId)
                override fun events(transactionId: Int): SectionPresenter<TransactionEventsPage> =
                    eventsPresenter(repository, transactionId)
                override fun accountTransactions(
                    accountId: Int,
                    limit: Int,
                    offset: Int,
                ): SectionPresenter<TransactionsPage> =
                    accountTransactionsPresenter(repository, accountId, limit, offset)
            },
            auth = authFlow,
            mask = MaskController(initiallyMasked = true),
            signInActions = object : SignInActions {
                override fun signIn(email: String, password: String, onFailure: (String) -> Unit) = Unit
                override fun submitTotp(code: String, onFailure: (String) -> Unit) = Unit
                override fun exploreDemo() = Unit
            },
            destinations = destinations,
            onSignOut = {},
        )
        val navController = newNavController()
        composeRule.setContent {
            FinTrackerApp(scope = scope, navController = navController)
        }

        // A signs in and drills into account 5 — server-confirmed, so A's
        // header legitimately carries A's id (the positive pin).
        loginBlocking("a@example.com")
        composeRule.awaitNodeWithTag(ScreenTags.GLANCE)
        composeRule.runOnUiThread { navController.navigate(Routes.accountDetail("5")) }
        composeRule.awaitText("TFSA of A")
        composeRule.awaitText("Account 5")
        assertEquals(Routes.ACCOUNT_DETAIL, navController.currentBackStackEntry?.destination?.route)

        // Expiry: every answer is 401 and the follow-up refresh is rejected.
        denyAll.set(true)
        Thread { runCatching { stack.client.getJson("/accounts") } }.also { it.start(); it.join(10_000) }
        composeRule.awaitNodeWithTag(ScreenTags.SIGN_IN)
        composeRule.waitUntil(5_000) { destinations.pending == "account/5" }
        assertEquals("account/5", destinations.pending, "expiry preserved A's FILLED route")

        // B (different user id) logs in: commit must hard-clear A's route.
        denyAll.set(false)
        loginBlocking("b@example.com")
        composeRule.awaitNodeWithTag(ScreenTags.GLANCE)
        assertNull(stack.session.pendingDestination, "identity commit cleared A's destination (security S1)")
        assertNull(destinations.pending, "the production preserver has nothing left to consume")

        // No navigation to A's route; no prior-tenant id anywhere, header included.
        assertFalse(
            composeRule.tagDisplayed(ScreenTags.ACCOUNT_DETAIL),
            "B was never navigated to A's route",
        )
        assertFalse(
            composeRule.tagDisplayed(ScreenTags.HOLDING_DETAIL),
            "B was never navigated to A's route",
        )
        assertFalse(
            composeRule.onAllNodesWithText("TFSA of A", useUnmergedTree = true)
                .fetchSemanticsNodes().isNotEmpty(),
            "A's account name must not appear anywhere in B's UI",
        )
        assertFalse(
            composeRule.onAllNodesWithText("Account 5", useUnmergedTree = true)
                .fetchSemanticsNodes().isNotEmpty(),
            "A's account id must not appear anywhere in B's UI, header included",
        )
        // Positive control: B's own data is live on B's home screen.
        composeRule.awaitText("B Savings")
    }

    private var lastLoginError: Throwable? = null

    private fun loginBlocking(email: String) {
        lastLoginError = null
        val t = Thread {
            try {
                stack.session.login(email, "pw")
            } catch (e: Throwable) {
                lastLoginError = e
            }
        }
        t.start()
        t.join(TimeUnit.SECONDS.toMillis(10))
        lastLoginError?.let { throw AssertionError("login failed for $email", it) }
        assertFalse(t.isAlive, "login did not finish for $email")
    }

    // ---- S1 header clause: route ids render only under a server verdict ----

    @Test
    fun `unconfirmed account route id never renders in the detail header (S1)`() {
        withFakeServer { server ->
            server.serveStatus(404)
            val navController = newNavController()
            val scope = serverBackedScope(server)
            composeRule.setContent {
                FinTrackerApp(scope = scope, navController = navController)
            }
            composeRule.runOnUiThread { navController.navigate(Routes.accountDetail("5")) }
            composeRule.awaitNodeWithTag(SectionTags.NOT_FOUND)
            composeRule.onNodeWithText("Account 5").assertDoesNotExist()
        }
    }

    @Test
    fun `unconfirmed holding route ids never render in the detail header (S1)`() {
        withFakeServer { server ->
            server.serveStatus(404)
            val navController = newNavController()
            val scope = serverBackedScope(server)
            composeRule.setContent {
                FinTrackerApp(scope = scope, navController = navController)
            }
            composeRule.runOnUiThread { navController.navigate(Routes.holdingDetail("5", "9")) }
            composeRule.awaitNodeWithTag(SectionTags.NOT_FOUND)
            composeRule.onNodeWithText("Account 5 · holding 9").assertDoesNotExist()
        }
    }
}
