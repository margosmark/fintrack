package com.margosmark.fintracker.screens

import com.margosmark.fintracker.FlushesBeforeWaitRule

import androidx.compose.ui.test.junit4.createComposeRule
import androidx.navigation.compose.ComposeNavigator
import androidx.navigation.testing.TestNavHostController
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.margosmark.fintracker.TestStack
import com.margosmark.fintracker.http.ApiException
import com.margosmark.fintracker.navigation.FinTrackerApp
import com.margosmark.fintracker.navigation.Routes
import com.margosmark.fintracker.navigation.ScreenTags
import com.margosmark.fintracker.navigation.filledRoute
import com.margosmark.fintracker.session.AuthState
import com.margosmark.fintracker.startStack
import com.margosmark.fintracker.tokenResponseBody
import java.util.concurrent.TimeUnit
import java.util.concurrent.CopyOnWriteArrayList
import kotlinx.coroutines.flow.MutableStateFlow
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.RecordedRequest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/**
 * QA round-3 focus probes — @qa, task t_1c5d218b, candidate 38234fb.
 *
 * Fresh equivalents for the two round-2 residuals (F1' AC1.3, F2' AC3.2),
 * driven through the PRODUCTION call path (real FinTrackerApp wiring →
 * SessionDestinationPreserver → AuthSessionManager; only the server is
 * faked with MockWebServer, per docs/53). Plus the PM's mid-run O1
 * interleaving (gate verdict latched → expiry while ON the link's own id
 * route → same-identity re-auth), its stale-verdict/deleted-target twin,
 * and the S1 identity-change clear in a link-launched process.
 *
 * Every observation prints before any assertion so a red still carries
 * its evidence. QA-R3 prefix on all stdout lines.
 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class QaRound3FocusTest {

    @get:Rule
    val composeRule = FlushesBeforeWaitRule(createComposeRule())

    @get:Rule
    val folder = TemporaryFolder()

    private lateinit var dispatcher: QaRound3Dispatcher
    private lateinit var stack: TestStack
    private val authFlow = MutableStateFlow<AuthState>(AuthState.SignedOut)

    @Before
    fun setUp() {
        dispatcher = QaRound3Dispatcher()
        stack = startStack(folder) { authFlow.value = it }
        stack.server.dispatcher = dispatcher
    }

    private fun newNav(): TestNavHostController {
        val nav = TestNavHostController(ApplicationProvider.getApplicationContext())
        nav.navigatorProvider.addNavigator(ComposeNavigator())
        return nav
    }

    private fun signInThroughProductionActions(scope: com.margosmark.fintracker.ui.AppScope) {
        scope.signInActions.signIn("a@example.com", "pw") {}
        composeRule.waitUntil(TimeUnit.SECONDS.toMillis(15)) {
            stack.session.state is AuthState.Authenticated
        }
    }

    /**
     * Delegates to [boundedObserve] (ComposeWait.kt), which retries past a transient
     * fault thrown *by* the condition and names the cause on failure. Kept as a method
     * so the six existing `observe(...)` call sites and their assertions stay untouched.
     */
    private fun observe(name: String, timeoutMs: Long, cond: () -> Boolean): Boolean =
        composeRule.boundedObserve(name, timeoutMs, cond)

    /**
     * Appends one line per run to `build/qa-round3-attribution.log`.
     *
     * The JUnit XML is overwritten by the next run and Gradle's console prints only
     * `AssertionError at File:line` — so the reported solo12-2 red of t_2e96b89e left
     * nothing behind but the line number, and its corpus/request trace was unrecoverable.
     * This file is append-only and survives every later run.
     */
    private fun recordAttribution(line: String) {
        runCatching {
            val out = java.io.File("build/qa-round3-attribution.log")
            out.parentFile?.mkdirs()
            out.appendText("${java.time.Instant.now()} $line\n")
        }
    }

    private fun expireSessionOnTheWire() {
        dispatcher.expireMode = true
        try {
            stack.client.getJson("/accounts")
            throw AssertionError("qa: expected the provably-dead session to throw")
        } catch (e: ApiException) {
            println("QA-R3 expiry exception = status=${e.status} code=${e.code}")
        } finally {
            dispatcher.expireMode = false
        }
    }

    private fun accountFiveFetches(): Int =
        synchronized(dispatcher.paths) {
            dispatcher.paths.count { it.startsWith("/api/v1/accounts/5") && !it.contains("ledger") }
        }

    // =====================================================================
    // O1 (PM mid-run note, t_824fc5ae) — gate verdict latched; expiry while
    // ON the launch link's own id route; SAME-identity re-auth. AC1.3
    // requires the filled destination to be restored with a fresh fetch.
    // =====================================================================

    @Test
    fun o1_expiryWhileOnTheLinksOwnIdRoute_sameIdentityReauthMustReturnThere() {
        // 1. Link-launched, signed out → sign in → gate verdict (latched) → account/5.
        val scope = qaProductionShapedScope(stack, authFlow)
        val nav = newNav()
        composeRule.setContent {
            FinTrackerApp(scope = scope, deepLinkUri = "fintracker://app/accounts/5", navController = nav)
        }
        composeRule.awaitNodeWithTag(ScreenTags.SIGN_IN)
        signInThroughProductionActions(scope)
        val firstRender = observe("o1_account_detail_after_gate", 15_000) {
            composeRule.tagDisplayed(ScreenTags.ACCOUNT_DETAIL)
        }
        composeRule.awaitText("ACCOUNT-FIVE")
        println("QA-R3 O1 initial render=$firstRender route=${nav.currentBackStackEntry?.destination?.route}")

        // 2. Session dies the real way while the user is ON the link's own route.
        expireSessionOnTheWire()
        observe("o1_sign_in_after_expiry", 10_000) { composeRule.tagDisplayed(ScreenTags.SIGN_IN) }
        val pending = stack.session.pendingDestination
        println("QA-R3 O1 pendingDestination after expiry = $pending (expect filled account/5)")

        // 3. SAME-identity re-auth. The gate already answered once for this
        //    process (deepLinkGateDone/Verdict latched) — AC1.3 still
        //    requires returning to the filled destination with a fresh fetch.
        signInThroughProductionActions(scope)
        val returned = observe("o1_return_to_account_detail", 15_000) {
            composeRule.tagDisplayed(ScreenTags.ACCOUNT_DETAIL)
        }
        composeRule.waitForIdle()
        val finalRoute = nav.currentBackStackEntry?.destination?.route
        val finalArg = nav.currentBackStackEntry?.arguments?.getString(Routes.ARG_ACCOUNT_ID)
        // The destination route re-renders immediately after re-auth (loading placeholder);
        // the fresh fetch lands asynchronously. Wait for the data itself — waitForIdle only
        // covers composition, not the in-flight request (run6: fetches=4 but corpus showed
        // "Fetching your data…" at check time).
        val renderedFive = observe("o1_account_five_render", 15_000) {
            composeRule.qaCorpus().any { it.contains("ACCOUNT-FIVE") }
        }
        val fetches = accountFiveFetches()
        val stillOnSignIn = composeRule.tagDisplayed(ScreenTags.SIGN_IN)
        val glance = composeRule.tagDisplayed(ScreenTags.GLANCE)
        println("QA-R3 O1 post-reauth returned=$returned route=$finalRoute arg=$finalArg " +
            "ACCOUNT-FIVE=$renderedFive stillOnSignIn=$stillOnSignIn glance=$glance fetches=$fetches")
        println("QA-R3 O1 corpus=${composeRule.qaCorpus()}")

        assertEquals(
            "AC1.3 (PM O1 interleaving): pending must be the FILLED route",
            "account/5",
            pending,
        )
        assertFalse(
            "PM O1: an AUTHENTICATED user was left on the sign-in composition after re-auth",
            stillOnSignIn,
        )
        assertTrue(
            "AC1.3 (PM O1 interleaving): same-identity re-auth must return to the " +
                "filled destination (the gate's latched verdict must not swallow it)",
            returned && finalArg == "5" && renderedFive,
        )
        assertTrue(
            "AC1.3: the destination must be revalidated with a FRESH fetch after re-auth",
            fetches >= 3,
        )
        stack.close()
    }

    /**
     * O1's security twin: between the gate's verdict and the expiry, the
     * target is DELETED server-side. The latched verdict must never render
     * the deleted id destination after re-auth (fail-closed), and no
     * id-specific state may appear.
     */
    @Test
    fun o1_staleVerdictAfterTargetDeletion_neverRendersTheDeletedId() {
        val scope = qaProductionShapedScope(stack, authFlow)
        val nav = newNav()
        composeRule.setContent {
            FinTrackerApp(scope = scope, deepLinkUri = "fintracker://app/accounts/5", navController = nav)
        }
        composeRule.awaitNodeWithTag(ScreenTags.SIGN_IN)
        signInThroughProductionActions(scope)
        composeRule.awaitText("ACCOUNT-FIVE")

        // The account is deleted server-side after the verdict was delivered.
        dispatcher.notFoundIds = setOf(5)
        expireSessionOnTheWire()
        observe("o1d_sign_in_after_expiry", 10_000) { composeRule.tagDisplayed(ScreenTags.SIGN_IN) }
        val pending = stack.session.pendingDestination
        println("QA-R3 O1-deleted pending after expiry = $pending")

        signInThroughProductionActions(scope)
        // Sample the screen over a window: the deleted id destination must never render.
        composeRule.waitForIdle()
        val window = observe("o1d_settle", 6_000) {
            composeRule.tagDisplayed(ScreenTags.GLANCE) ||
                composeRule.tagDisplayed(ScreenTags.UNAVAILABLE) ||
                composeRule.tagDisplayed(ScreenTags.SIGN_IN)
        }
        composeRule.waitForIdle()
        val destRendered = composeRule.tagDisplayed(ScreenTags.ACCOUNT_DETAIL)
        val renderedFive = composeRule.qaCorpus().any { it.contains("ACCOUNT-FIVE") }
        println("QA-R3 O1-deleted settled=$window destRendered=$destRendered ACCOUNT-FIVE=$renderedFive " +
            "route=${nav.currentBackStackEntry?.destination?.route}")
        println("QA-R3 O1-deleted corpus=${composeRule.qaCorpus()}")

        assertFalse(
            "AC3.2/AC1.3: the deleted id destination rendered after re-auth " +
                "(a stale gate verdict must never render a now-deleted target)",
            destRendered,
        )
        assertFalse("no prior-identity account name may render", renderedFive)
        stack.close()
    }

    // =====================================================================
    // F1' fresh equivalents (AC1.3) — beyond the round-2 account/5 probe:
    // a TWO-argument filled route, and the gate-owns control that a
    // browsing destination is NOT swallowed by the gate-owns branch.
    // =====================================================================

    @Test
    fun ac1_3_expiryOnAMultiArgRoute_preservesEveryResolvedArgument() {
        val scope = qaProductionShapedScope(stack, authFlow)
        val nav = newNav()
        composeRule.setContent {
            FinTrackerApp(scope = scope, navController = nav)
        }
        signInThroughProductionActions(scope)
        composeRule.awaitNodeWithTag(ScreenTags.GLANCE)

        composeRule.runOnUiThread { nav.navigate(Routes.holdingDetail("1", "9")) }
        composeRule.awaitNodeWithTag(ScreenTags.HOLDING_DETAIL)
        composeRule.awaitText("VCN")
        println("QA-R3 multi-arg pre-expiry route=${nav.currentBackStackEntry?.destination?.route}")

        expireSessionOnTheWire()
        observe("multiarg_sign_in", 10_000) { composeRule.tagDisplayed(ScreenTags.SIGN_IN) }
        val pending = stack.session.pendingDestination
        println("QA-R3 multi-arg pending after expiry = $pending (expect account/1/holding/9)")

        signInThroughProductionActions(scope)
        val tReauth = System.currentTimeMillis()
        val restored = observe("multiarg_holding_detail", 15_000) {
            composeRule.tagDisplayed(ScreenTags.HOLDING_DETAIL)
        }
        val tRestored = System.currentTimeMillis()
        composeRule.waitForIdle()
        val argA = nav.currentBackStackEntry?.arguments?.getString(Routes.ARG_ACCOUNT_ID)
        val argH = nav.currentBackStackEntry?.arguments?.getString(Routes.ARG_HOLDING_ID)
        // Same race as o1: route restored instantly, holding data arrives with the fresh fetch.
        val vcnStart = System.currentTimeMillis()
        val vcn = observe("multiarg_vcn_render", 15_000) {
            composeRule.qaCorpus().any { it.contains("VCN") }
        }
        println("QA-R3 multi-arg post-reauth restored=$restored accountId=$argA holdingId=$argH VCN=$vcn " +
            "restoreTagMs=${tRestored - tReauth} vcnRenderMs=${System.currentTimeMillis() - vcnStart}")
        // Attribution: the whole TalkBack corpus (the presenter's state is visible in it —
        // "Holding" = still Loading, "VCN" = Ready, plus every error pane's copy) and every
        // request the server actually saw, in order. Captured once and ALSO carried by the
        // assertion message below: Gradle's console prints only the assertion line, so a
        // console-only red has to name its own mode (never issued / issued-but-still-Loading /
        // error pane) there instead of in stdout nobody archived.
        val corpus = composeRule.qaCorpus()
        val requests = synchronized(dispatcher.paths) { dispatcher.paths.toList() }
        println("QA-R3 multi-arg corpus=$corpus")
        println("QA-R3 multi-arg paths=$requests")
        recordAttribution(
            "ac1_3 VCN=$vcn restored=$restored accountId=$argA holdingId=$argH " +
                "restoreTagMs=${tRestored - tReauth} corpus=$corpus server=$requests"
        )

        assertEquals("every resolved argument must be preserved (accountId)", "account/1/holding/9", pending)
        assertTrue("the two-argument destination must be restored", restored)
        assertEquals("accountId arg preserved", "1", argA)
        assertEquals("holdingId arg preserved", "9", argH)
        assertTrue(
            "the intended holding must render" +
                (if (vcn) "" else " — corpus=$corpus; server saw=$requests"),
            vcn
        )
        stack.close()
    }

    @Test
    fun ac1_3_browsingDestinationAfterALinkLaunch_isNotSwallowedByTheGateOwnsBranch() {
        // Link launch to account/42 (verdict delivered: not found → generic pane),
        // then the user BROWSES to account/5; expiry there must return them to
        // account/5 — the gate-owns branch may only claim the LINK's own route.
        dispatcher.notFoundIds = setOf(42)
        val scope = qaProductionShapedScope(stack, authFlow)
        val nav = newNav()
        composeRule.setContent {
            FinTrackerApp(scope = scope, deepLinkUri = "fintracker://app/accounts/42", navController = nav)
        }
        composeRule.awaitNodeWithTag(ScreenTags.SIGN_IN)
        signInThroughProductionActions(scope)
        observe("browsing_unavailable", 15_000) { composeRule.tagDisplayed(ScreenTags.UNAVAILABLE) }
        println("QA-R3 browsing link verdict route=${nav.currentBackStackEntry?.destination?.route}")

        composeRule.runOnUiThread { nav.navigate(Routes.accountDetail("5")) }
        composeRule.awaitNodeWithTag(ScreenTags.ACCOUNT_DETAIL)
        composeRule.awaitText("ACCOUNT-FIVE")

        expireSessionOnTheWire()
        observe("browsing_sign_in", 10_000) { composeRule.tagDisplayed(ScreenTags.SIGN_IN) }
        val pending = stack.session.pendingDestination
        println("QA-R3 browsing pending after expiry = $pending (expect account/5, NOT the link's route)")

        signInThroughProductionActions(scope)
        val restored = observe("browsing_account_detail", 15_000) {
            composeRule.tagDisplayed(ScreenTags.ACCOUNT_DETAIL)
        }
        composeRule.waitForIdle()
        val arg = nav.currentBackStackEntry?.arguments?.getString(Routes.ARG_ACCOUNT_ID)
        val five = observe("browsing_account_five_render", 15_000) {
            composeRule.qaCorpus().any { it.contains("ACCOUNT-FIVE") }
        }
        println("QA-R3 browsing post-reauth restored=$restored arg=$arg ACCOUNT-FIVE=$five " +
            "route=${nav.currentBackStackEntry?.destination?.route}")

        assertEquals("the BROWSED destination must be preserved, not the link's", "account/5", pending)
        assertTrue("re-auth must return to the browsed destination", restored && arg == "5" && five)
        stack.close()
    }

    // =====================================================================
    // F1' contract unit probe — the preservation regex, fresh assertions
    // (a value must never be able to compose a different route).
    // =====================================================================

    @Test
    fun ac1_3_filledRoute_refusesAnyValueThatCouldComposeAnotherRoute() {
        val safe = android.os.Bundle().apply { putString("accountId", "5") }
        assertEquals("account/5", filledRoute("account/{accountId}", safe))

        val multi = android.os.Bundle().apply {
            putString("accountId", "1")
            putString("holdingId", "9")
        }
        assertEquals(
            "account/1/holding/9",
            filledRoute("account/{accountId}/holding/{holdingId}", multi),
        )

        // No-placeholder routes pass through untouched (list routes).
        assertEquals("glance", filledRoute("glance", null))

        // Hostile / unfillable values are NEVER preserved.
        for (hostile in listOf("5/6", "5/../settings", "{accountId}", "5}", "", "5 6", "5%2F6", "账户")) {
            val b = android.os.Bundle().apply { putString("accountId", hostile) }
            assertNull("hostile argument must never be preserved: '$hostile'", filledRoute("account/{accountId}", b))
        }
        assertNull("a missing argument must never be preserved", filledRoute("account/{accountId}", android.os.Bundle()))
        assertNull("a null argument bundle must never be preserved", filledRoute("account/{accountId}", null))
        println("QA-R3 filledRoute contract: 4 positive + 10 negative assertions green")
    }

    // =====================================================================
    // S1 identity-change clear in a LINK-LAUNCHED process: A's expiry-
    // preserved link destination must be dropped when B commits, before
    // any of B's data renders (docs/53 lifecycle rule 3).
    // =====================================================================

    @Test
    fun s1_identityChangeInALinkProcess_dropsA_preservedDestinationBeforeBRenders() {
        val scope = qaProductionShapedScope(stack, authFlow)
        val nav = newNav()
        composeRule.setContent {
            FinTrackerApp(scope = scope, deepLinkUri = "fintracker://app/accounts/5", navController = nav)
        }
        composeRule.awaitNodeWithTag(ScreenTags.SIGN_IN)
        // A (user id 1) signs in; gate verdict → account/5 renders.
        signInThroughProductionActions(scope)
        composeRule.awaitText("ACCOUNT-FIVE")
        val fetchesBeforeB = accountFiveFetches()
        println("QA-R3 S1 A rendered; /accounts/5 fetches so far = $fetchesBeforeB")

        // A's session expires while ON the link's route → destination noted.
        expireSessionOnTheWire()
        observe("s1_sign_in", 10_000) { composeRule.tagDisplayed(ScreenTags.SIGN_IN) }
        val pendingForA = stack.session.pendingDestination
        println("QA-R3 S1 pending after A's expiry = $pendingForA")

        // B (user id 2) commits a session — the preserved destination must clear.
        dispatcher.loginUserId = 2
        signInThroughProductionActions(scope)
        val pendingAtBCommit = stack.session.pendingDestination
        val settled = observe("s1_b_settled", 15_000) {
            composeRule.tagDisplayed(ScreenTags.GLANCE) ||
                composeRule.tagDisplayed(ScreenTags.ACCOUNT_DETAIL) ||
                composeRule.tagDisplayed(ScreenTags.UNAVAILABLE)
        }
        composeRule.waitForIdle()
        val bArg = nav.currentBackStackEntry?.arguments?.getString(Routes.ARG_ACCOUNT_ID)
        val bRenderedFive = composeRule.qaCorpus().any { it.contains("ACCOUNT-FIVE") }
        val bFetches = accountFiveFetches()
        println("QA-R3 S1 at B commit: pending=$pendingAtBCommit settled=$settled " +
            "route=${nav.currentBackStackEntry?.destination?.route} arg=$bArg " +
            "ACCOUNT-FIVE=$bRenderedFive /accounts/5 fetches=$bFetches (A's count was $fetchesBeforeB)")
        println("QA-R3 S1 corpus=${composeRule.qaCorpus()}")

        assertEquals("A's preserved destination must still have existed for A", "account/5", pendingForA)
        assertNull(
            "S1: an identity CHANGE at commit must hard-clear the preserved destination",
            pendingAtBCommit,
        )
        assertFalse(
            "S1: A's destination must never render for B",
            composeRule.tagDisplayed(ScreenTags.ACCOUNT_DETAIL) && bArg == "5",
        )
        assertFalse("S1: A's account name must never appear in B's UI", bRenderedFive)
        assertEquals(
            "S1: B must not trigger A's destination fetch — no new /accounts/5 request",
            fetchesBeforeB,
            bFetches,
        )
        stack.close()
    }
}

/**
 * Dispatcher with a switchable login user id (identity-change probes) —
 * same wire shapes as QaRound2Dispatcher, MockWebServer only.
 */
class QaRound3Dispatcher : Dispatcher() {
    @Volatile
    var expireMode = false

    @Volatile
    var notFoundIds: Set<Int> = emptySet()

    @Volatile
    var loginUserId: Int = 1

    val paths = CopyOnWriteArrayList<String>()

    private val accountDetail = Regex("/accounts/([0-9]+)\\z")

    override fun dispatch(request: RecordedRequest): MockResponse {
        val url = request.requestUrl
        val path = url?.encodedPath ?: ""
        paths += (url?.let { it.encodedPath + it.query.orEmpty().let { q -> if (q.isEmpty()) "" else "?$q" } } ?: path) +
            "@" + System.currentTimeMillis()

        if (expireMode) {
            return if (path.contains("/auth/refresh")) {
                qaErr(401, "Invalid or revoked refresh token", "invalid_or_revoked_refresh_token")
            } else {
                qaErr(401, "Could not validate credentials", "invalid_token")
            }
        }
        if (path.endsWith("/auth/login")) {
            return MockResponse()
                .setResponseCode(200)
                .setHeader("Set-Cookie", "refresh_token=rt-qa-3; Path=/api/v1/auth; HttpOnly")
                .setHeader("Content-Type", "application/json")
                .setBody(tokenResponseBody("access-qa-3", loginUserId, "a@example.com"))
        }
        if (path.endsWith("/auth/logout")) return MockResponse().setResponseCode(200)
        if (path.endsWith("/auth/refresh")) {
            return qaErr(401, "Invalid or revoked refresh token", "invalid_or_revoked_refresh_token")
        }
        if (path.endsWith("/accounts/ledger")) {
            val id = url?.queryParameter("account_id") ?: "0"
            return qaOk(qaLedgerJson(id))
        }
        val account = accountDetail.find(path)
        if (account != null) {
            val id = account.groupValues[1].toInt()
            val response = if (id in notFoundIds) {
                qaErr(404, "Not found", "not_found")
            } else {
                val name = when (id) {
                    5 -> "ACCOUNT-FIVE"
                    0 -> "ACCOUNT-ZERO"
                    else -> "ACCOUNT-$id"
                }
                qaOk(qaAccountJson(id, name))
            }
            return response
        }
        val body = when {
            path.contains("/portfolio/positions") -> Fixtures.POSITIONS_JSON
            path.contains("/fx/status") -> Fixtures.FX_JSON
            path.contains("/transactions/7/events") -> Fixtures.EVENTS_JSON
            path.contains("/transactions") -> Fixtures.TRANSACTIONS_JSON
            path.contains("/accounts") -> Fixtures.ACCOUNTS_JSON
            else -> "{}"
        }
        return MockResponse()
            .setResponseCode(200)
            .setHeader("Content-Type", "application/json")
            .setBody(body)
    }
}
