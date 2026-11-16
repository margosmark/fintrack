package com.margosmark.fintracker.screens

import androidx.compose.ui.test.hasAnyDescendant
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.navigation.compose.ComposeNavigator
import androidx.navigation.testing.TestNavHostController
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.margosmark.fintracker.FakeServer
import com.margosmark.fintracker.FlushesBeforeWaitRule
import com.margosmark.fintracker.navigation.FinTrackerApp
import com.margosmark.fintracker.navigation.Routes
import com.margosmark.fintracker.serverBackedScope
import com.margosmark.fintracker.state.MASK_BULLETS
import com.margosmark.fintracker.ui.screens.AccountDetailTags
import com.margosmark.fintracker.ui.screens.SectionTags
import java.util.concurrent.atomic.AtomicInteger
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.RecordedRequest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/**
 * Account detail: the account's OWN holdings beside its ledger, and the
 * account-detail number formatting (docs/48 §3.3, docs/52 §3, docs/53
 * AC1.1 / AC1.3 / AC2.1).
 *
 * Every figure below is a SERVER STRING from a local [FakeServer] — no
 * production host is contacted, and the only requests on the wire are the
 * account-scoped reads this screen is allowed to make.
 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class AccountHoldingsTest {

    @get:Rule
    val rule = FlushesBeforeWaitRule(createComposeRule())

    private lateinit var navController: TestNavHostController

    /** Compose the app and push the account-detail route. */
    private fun launch(server: FakeServer, reveal: Boolean, route: String = Routes.accountDetail("1")) {
        navController = TestNavHostController(ApplicationProvider.getApplicationContext())
        navController.navigatorProvider.addNavigator(ComposeNavigator())
        val scope = serverBackedScope(server)
        if (reveal) scope.mask.reveal()
        rule.setContent { FinTrackerApp(scope = scope, navController = navController) }
        rule.runOnUiThread { navController.navigate(route) }
    }

    /** Bounded wait: the holdings wrapper reaches [tag] (its section state). */
    private fun awaitHoldingsState(tag: String) {
        rule.waitUntil(10_000) { holdingsShows(tag) }
    }

    /** True when the account-detail HOLDINGS wrapper contains a node with [tag]. */
    private fun holdingsShows(tag: String): Boolean =
        rule.onAllNodes(
            hasTestTag(AccountDetailTags.HOLDINGS) and hasAnyDescendant(hasTestTag(tag)),
            useUnmergedTree = true,
        ).fetchSemanticsNodes().isNotEmpty()

    // ------------------------------------------------------------------
    // Holdings render, and the ledger stays a separate section
    // ------------------------------------------------------------------

    @Test
    fun investmentAccountShowsServerHoldingsBesideItsLedger() {
        FakeServer().use { server ->
            server.serveApi(
                account = { ok(accountJson(id = 1, type = "tfsa", name = "TFSA")) },
                positions = { ok(positionsJson(accountId = 1)) },
            )
            launch(server, reveal = true)

            rule.awaitNodeWithTag(AccountDetailTags.HOLDINGS)
            rule.awaitText("Holdings")
            rule.awaitText("VCN")
            // Server quantity "000010.0000" → grouped "10"; server market
            // value "1234.5678" → grouped "1,234.57"; price "0.05123456" →
            // "0.0512"; gain "12.5" → "12.50". Every one of those is the
            // response's own string, formatted for reading.
            rule.awaitText("qty 10")
            rule.awaitText("1,234.57")
            rule.awaitText("0.0512")
            rule.awaitText("12.50")

            // The cash ledger is untouched and still its own section.
            rule.awaitText("Ledger")
            rule.awaitText("COFFEE")
            rule.awaitText("Investment value")

            // Account scoping: the positions call carries THIS route's id.
            val requests = server.requests()
            val positionsRequest = accountScopedPositions(requests).single()
            assertEquals("1", positionsRequest.requestUrl?.queryParameter("account_id"))
            assertEquals("positions must be a GET", "GET", positionsRequest.method)
        }
    }

    /** docs/53 AC1.1: a mutated server field is what the screen shows. */
    @Test
    fun mutatedServerFieldIsWhatTheScreenShows() {
        FakeServer().use { server ->
            server.serveApi(
                account = { ok(accountJson(id = 1, type = "tfsa", name = "TFSA")) },
                positions = { ok(positionsJson(accountId = 1, marketValue = "5432.10")) },
            )
            launch(server, reveal = true)

            // The response's own number, grouped — not the raw wire string and
            // not any figure derived from another field (the account headline
            // still says 110.00).
            rule.awaitText("5,432.10")
            assertFalse("raw wire string must not be rendered verbatim", rule.textDisplayed("5432.10"))
            assertFalse("no other row's figure may leak in", rule.textDisplayed("1,234.57"))
            assertTrue("the account headline is untouched", rule.textDisplayed("110.00"))
        }
    }

    // ------------------------------------------------------------------
    // Cost guard: cash accounts never hit the refresh-capable route
    // ------------------------------------------------------------------

    @Test
    fun cashAccountNeverRequestsPositions() {
        val positionsCalls = AtomicInteger(0)
        FakeServer().use { server ->
            server.serveApi(
                account = { ok(accountJson(id = 1, type = "chequing", name = "Chequing")) },
                positions = { accountId ->
                    if (accountId != null) positionsCalls.incrementAndGet()
                    ok(positionsJson(accountId = 1))
                },
            )
            launch(server, reveal = true)

            rule.awaitText("Investment value")
            rule.awaitText("COFFEE")
            rule.awaitText("chequing")
            // No holdings section is composed at all…
            assertFalse(
                "cash accounts must not compose a holdings section",
                rule.tagDisplayed(AccountDetailTags.HOLDINGS),
            )
            assertFalse(rule.textDisplayed("VCN"))
            // …and the refresh-capable route was never asked for.
            val requests = server.requests()
            assertTrue(
                "account-scoped positions requested for a cash account: " +
                    requests.map { it.requestUrl?.encodedPath + "?" + (it.requestUrl?.query ?: "") },
                accountScopedPositions(requests).isEmpty(),
            )
            assertEquals(0, positionsCalls.get())
        }
    }

    // ------------------------------------------------------------------
    // Independent failure states (docs/53 AC1.3)
    // ------------------------------------------------------------------

    @Test
    fun positionsFailureLeavesDetailsAndLedgerWorking() {
        FakeServer().use { server ->
            server.serveApi(
                account = { ok(accountJson(id = 1, type = "tfsa", name = "TFSA")) },
                positions = { fail(500) },
            )
            launch(server, reveal = true)

            // The holdings section reports its OWN failure…
            awaitHoldingsState(SectionTags.UNAVAILABLE)
            assertTrue(
                "holdings must show its own unavailable state",
                holdingsShows(SectionTags.UNAVAILABLE),
            )
            // …while Details and Ledger keep every figure they already had.
            rule.awaitText("Investment value")
            rule.awaitText("110.00")
            rule.awaitText("COFFEE")
            rule.awaitText("105.50")
            assertFalse("a failed holdings section must not show a row", rule.textDisplayed("VCN"))
        }
    }

    @Test
    fun emptyHoldingsIsAnHonestNoDataState() {
        FakeServer().use { server ->
            server.serveApi(
                account = { ok(accountJson(id = 1, type = "tfsa", name = "TFSA")) },
                positions = { ok(EMPTY_POSITIONS) },
            )
            launch(server, reveal = true)

            awaitHoldingsState(SectionTags.NO_DATA)
            rule.awaitText("No holdings yet")
            assertTrue("empty holdings must be NO_DATA", holdingsShows(SectionTags.NO_DATA))
            assertFalse("no fabricated holding row", rule.textDisplayed("VCN"))
            // The rest of the screen still shows the server's own numbers.
            rule.awaitText("Investment value")
            rule.awaitText("COFFEE")
        }
    }

    /** docs/46 D2 / docs/53 AC2.1: the server's own staleness signal. */
    @Test
    fun stalePricesCarryTheServersStalenessSignal() {
        FakeServer().use { server ->
            server.serveApi(
                account = { ok(accountJson(id = 1, type = "tfsa", name = "TFSA")) },
                positions = { ok(positionsJson(accountId = 1, stale = true)) },
            )
            launch(server, reveal = true)

            awaitHoldingsState(SectionTags.STALE)
            assertTrue(
                "stale prices must show the server's staleness banner",
                holdingsShows(SectionTags.STALE),
            )
            rule.awaitText("Showing saved prices")
        }
    }

    // ------------------------------------------------------------------
    // Masking (docs/46 D2, docs/53 AC2.1)
    // ------------------------------------------------------------------

    @Test
    fun maskedHoldingsHideTheirFiguresUntilRevealed() {
        FakeServer().use { server ->
            server.serveApi(
                account = { ok(accountJson(id = 1, type = "tfsa", name = "TFSA")) },
                positions = { ok(positionsJson(accountId = 1)) },
            )
            // Cold start: masked by default.
            launch(server, reveal = false)

            rule.awaitNodeWithTag(AccountDetailTags.HOLDINGS)
            rule.awaitText("VCN")
            assertFalse("money must be masked at cold start", rule.textDisplayed("1,234.57"))
            assertFalse(rule.textDisplayed("0.0512"))
            assertTrue("masked values read as bullets", rule.textDisplayed(MASK_BULLETS))

            rule.onNodeWithTag(SectionTags.MASK_TOGGLE).performClick()

            rule.awaitText("1,234.57")
            rule.awaitText("0.0512")
            assertFalse("revealing must not leave bullets behind", rule.textDisplayed(MASK_BULLETS))
        }
    }

    // ------------------------------------------------------------------
    // Account scoping: each route id renders only its own response
    // ------------------------------------------------------------------

    @Test
    fun eachAccountRendersOnlyItsOwnHoldings() {
        FakeServer().use { server ->
            server.serveApi(
                account = { id ->
                    ok(accountJson(id = id.toIntOrNull() ?: 1, type = "tfsa", name = "TFSA-$id"))
                },
                positions = { accountId ->
                    ok(
                        if (accountId == "2") {
                            positionsJson(accountId = 2, symbol = "ZAG")
                        } else {
                            positionsJson(accountId = 1, symbol = "VCN")
                        },
                    )
                },
                ledger = { id -> ok(ledgerJson(id)) },
            )
            launch(server, reveal = true)

            rule.awaitText("VCN")
            assertFalse("another account's holding must not render", rule.textDisplayed("ZAG"))

            rule.runOnUiThread { navController.navigate(Routes.accountDetail("2")) }
            rule.awaitText("ZAG")
            assertFalse("the first account's holding must not follow along", rule.textDisplayed("VCN"))

            val positionRequests = accountScopedPositions(server.requests())
            assertEquals(
                "both routes must request their own scope",
                listOf("1", "2"),
                positionRequests.map { it.requestUrl?.queryParameter("account_id") },
            )
        }
    }

    // ------------------------------------------------------------------
    // Wire fixtures — synthetic, disposable, local only
    // ------------------------------------------------------------------

    /** Positions requests that carry an account scope (Glance's list call does not). */
    private fun accountScopedPositions(requests: List<RecordedRequest>): List<RecordedRequest> =
        requests.filter {
            (it.requestUrl?.encodedPath ?: "").endsWith("/portfolio/positions") &&
                it.requestUrl?.queryParameter("account_id") != null
        }

    private class Api(val code: Int, val body: String)

    private fun ok(body: String) = Api(200, body)

    private fun fail(code: Int) =
        Api(code, """{"detail":"synthetic","code":"synthetic"}""")

    /**
     * Path-routed read backend for this screen: the account row by id, its
     * ledger, and `GET /portfolio/positions?account_id=…`. Unknown paths 404,
     * so an unexpected extra request fails the assertions below.
     */
    private fun FakeServer.serveApi(
        account: (String) -> Api = { ok(accountJson(1, "tfsa", "TFSA")) },
        positions: (String?) -> Api = { ok(positionsJson(1)) },
        ledger: (String) -> Api = { ok(ledgerJson(it)) },
    ) {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val url = request.requestUrl
                val path = url?.encodedPath ?: ""
                val api = when {
                    path.endsWith("/portfolio/positions") -> positions(url?.queryParameter("account_id"))
                    path.endsWith("/ledger") ->
                        ledger(path.removePrefix("/api/v1/accounts/").removeSuffix("/ledger"))
                    path.startsWith("/api/v1/accounts/") ->
                        account(path.removePrefix("/api/v1/accounts/"))
                    else -> null
                } ?: return MockResponse()
                    .setResponseCode(404)
                    .setHeader("Content-Type", "application/json")
                    .setBody("""{"detail":"no canned route for $path","code":"not_found"}""")
                return MockResponse()
                    .setResponseCode(api.code)
                    .setHeader("Content-Type", "application/json")
                    .setBody(api.body)
            }
        }
    }

    private fun accountJson(id: Int, type: String, name: String): String =
        """{"id":$id,"name":"$name","type":"$type","currency":"CAD","cash_balance":"10.00",""" +
            """"investment_value":"100.00","total_value":"110.00","starting_balance":"5.00","last_sync_at":null,""" +
            """"provider_status":null,"pending_import_count":0}"""

    /**
     * One holding whose figures deliberately need formatting (leading zeros in
     * the quantity, four-plus decimals in the price/market value) and a server
     * gain of "12.5" that reads as "12.50".
     */
    private fun positionsJson(
        accountId: Int,
        symbol: String = "VCN",
        marketValue: String = "1234.5678",
        stale: Boolean = false,
    ): String =
        """{"items":[{"account_id":$accountId,"account_name":"TFSA","security_id":9,"symbol":"$symbol",""" +
            """"listing_currency":"CAD","quantity":"000010.0000","price_date":"2026-10-28","price":"0.05123456",""" +
            """"price_source":"manual","price_age_days":3,"price_fetched_at":null,"price_stale":$stale,""" +
            """"market_value_native":"$marketValue","market_value_cad":"$marketValue","book_cost_cad":"1222.07",""" +
            """"day_change_native":null,"day_change_pct":null,"day_change_cad":null,"unrealized_gain_cad":"12.5"}],""" +
            """"total":1,"total_unrealized_cad":"12.50","total_value_cad":"$marketValue","cash_cad":"10.00",""" +
            """"portfolio_value_cad":"1244.57","total_day_change_cad":null,"total_day_change_pct":null,""" +
            """"day_change_as_of":"2026-10-28","day_change_mixed_baseline":false}"""

    private fun ledgerJson(accountId: String): String =
        """{"items":[{"transaction_id":7,"date":"2026-10-01","description":"COFFEE","amount":"-4.50",""" +
            """"running_balance":"105.50"}],"starting_balance":"110.00","current_balance":"105.50","total":1}"""
}

/** An empty positions page in the server's exact envelope shape. */
private val EMPTY_POSITIONS =
    """{"items":[],"total":0,"total_value_cad":"0.00","cash_cad":null,"portfolio_value_cad":null,""" +
        """"total_day_change_cad":null,"total_day_change_pct":null,"day_change_as_of":null,""" +
        """"day_change_mixed_baseline":false}"""
