package com.margosmark.fintracker.screens

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.width
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.SemanticsNodeInteraction
import androidx.compose.ui.test.assertHasClickAction
import androidx.compose.ui.test.getBoundsInRoot
import androidx.compose.ui.test.hasAnyAncestor
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.DpRect
import androidx.compose.ui.unit.dp
import androidx.navigation.compose.ComposeNavigator
import androidx.navigation.testing.TestNavHostController
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.margosmark.fintracker.navigation.AppBarTags
import com.margosmark.fintracker.FakeServer
import com.margosmark.fintracker.FlushesBeforeWaitRule
import com.margosmark.fintracker.navigation.FinTrackerApp
import com.margosmark.fintracker.navigation.Routes
import com.margosmark.fintracker.navigation.ScreenTags
import com.margosmark.fintracker.serverBackedScope
import com.margosmark.fintracker.state.MASK_BULLETS
import com.margosmark.fintracker.ui.screens.AccountDetailTags
import com.margosmark.fintracker.ui.screens.SectionTags
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
 * The Account Detail interaction pass (docs/48 §3.3, PM rulings on
 * t_b236fcb8): holdings whole-row nav, ledger clamp + in-place expand,
 * running balance on the right rail with the currency once per row, the
 * "Show N more" pager over the EXISTING `/transactions` route, Frame A's
 * hero de-dup, and the app-bar back/eye tap floors.
 *
 * Root width is pinned to 390 (the design's reference width; Robolectric
 * runs mdpi, so dp == px there and "≥44px" reads as "≥44.dp"), and the
 * 360 floor probe re-wraps the app in a 360dp box. Every figure below is a
 * SERVER STRING from a local [FakeServer] — no production host is contacted.
 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34], qualifiers = "w390dp-h844dp")
class AccountDetailInteractionTest {

    @get:Rule
    val rule = FlushesBeforeWaitRule(createComposeRule())

    private lateinit var navController: TestNavHostController

    /** Compose the app and push [route]; [widthDp] constrains the whole app. */
    private fun launch(
        server: FakeServer,
        route: String = Routes.accountDetail("1"),
        reveal: Boolean = true,
        widthDp: Int? = null,
    ) {
        navController = TestNavHostController(ApplicationProvider.getApplicationContext())
        navController.navigatorProvider.addNavigator(ComposeNavigator())
        val scope = serverBackedScope(server)
        if (reveal) scope.mask.reveal()
        rule.setContent {
            if (widthDp != null) {
                Box(Modifier.width(widthDp.dp).fillMaxHeight()) {
                    FinTrackerApp(scope = scope, navController = navController)
                }
            } else {
                FinTrackerApp(scope = scope, navController = navController)
            }
        }
        rule.runOnUiThread { navController.navigate(route) }
    }

    /** Substring presence — the dead-end copy lives inside a longer footer string. */
    private fun textContains(needle: String): Boolean =
        rule.onAllNodesWithText(needle, substring = true, useUnmergedTree = true)
            .fetchSemanticsNodes()
            .isNotEmpty()

    // ------------------------------------------------------------------
    // 1. Holdings: whole-row tap → Routes.HOLDING_DETAIL (navigation only)
    // ------------------------------------------------------------------

    @Test
    fun holdingsWholeRowTapNavigatesToHoldingDetail() {
        FakeServer().use { server ->
            server.serveDetailApi(account = tfsaAccountJson(), entries = ledgerFixture(3))
            launch(server)

            rule.awaitNodeWithTag(AccountDetailTags.HOLDINGS)
            rule.awaitText("VCN")
            val rowTag = AccountDetailTags.holdingRow(9)
            rule.awaitNodeWithTag(rowTag)
            rule.onNodeWithTag(rowTag).assertHasClickAction()
            rule.onNodeWithTag(rowTag).performClick()
            rule.waitForIdle()

            assertEquals(
                "the holding row drills into HOLDING_DETAIL",
                Routes.HOLDING_DETAIL,
                navController.currentDestination?.route,
            )
            val entry = navController.currentBackStackEntry
            assertEquals("1", entry?.arguments?.getString(Routes.ARG_ACCOUNT_ID))
            assertEquals("9", entry?.arguments?.getString(Routes.ARG_HOLDING_ID))
        }
    }

    // ------------------------------------------------------------------
    // 2. Ledger: description clamps to 2 lines, tap expands IN PLACE,
    //    second tap collapses — never navigation.
    // ------------------------------------------------------------------

    @Test
    fun ledgerRowClampsTwoLinesAndTogglesInPlace() {
        FakeServer().use { server ->
            server.serveDetailApi(
                account = cashAccountJson(),
                entries = ledgerFixture(3),
                positions = emptyPositionsJson(),
            )
            launch(server)

            val descTag = AccountDetailTags.ledgerDescription(1001)
            val rowTag = AccountDetailTags.ledgerRow(1001)
            rule.awaitNodeWithTag(rowTag)

            // What this environment CAN pin: the interaction contract (tap
            // toggles expanded state in place — never navigation) and the
            // source seam that produces the visual clamp. It cannot pin the
            // 2-line geometry itself: Robolectric's font NEVER wraps (a bare
            // 5,400-char body Text lays out 36dp tall — probe), so clamped
            // and expanded heights are identical here by construction.
            val src = listOf(
                java.io.File("src/main/kotlin/com/margosmark/fintracker/ui/screens/AccountDetailScreen.kt"),
                java.io.File("android/app/src/main/kotlin/com/margosmark/fintracker/ui/screens/AccountDetailScreen.kt"),
            ).firstOrNull { it.exists() }
                ?: error("AccountDetailScreen.kt not found from ${java.io.File(".").absolutePath}")
            assertTrue(
                "ledger description must clamp to 2 lines when collapsed",
                src.readText().contains("maxLines = if (expanded) Int.MAX_VALUE else 2"),
            )

            val fullText = rule.onNodeWithTag(descTag, useUnmergedTree = true).fetchSemanticsNode()
                .config[SemanticsProperties.Text].joinToString("") { it.text }
            assertTrue("fixture description must be long (${fullText.length} chars)", fullText.length > 800)
            rule.onNodeWithTag(rowTag).assertHasClickAction()
            assertEquals(
                "clamped row announces the expand action",
                "show full description",
                rowClickLabel(rowTag),
            )

            rule.onNodeWithTag(rowTag).performClick()
            rule.waitForIdle()
            assertEquals(
                "tap flips the TalkBack label in place",
                "collapse description",
                rowClickLabel(rowTag),
            )
            // The full text survives the toggle — clamp is visual only.
            val expandedText = rule.onNodeWithTag(descTag, useUnmergedTree = true).fetchSemanticsNode()
                .config[SemanticsProperties.Text].joinToString("") { it.text }
            assertEquals("expanding never rewrites the description", fullText, expandedText)
            assertEquals(
                "expand happens IN PLACE — no transaction-detail route exists",
                Routes.ACCOUNT_DETAIL,
                navController.currentDestination?.route,
            )

            rule.onNodeWithTag(rowTag).performClick()
            rule.waitForIdle()
            assertEquals(
                "second tap collapses back",
                "show full description",
                rowClickLabel(rowTag),
            )
            assertEquals(
                "collapse stays on ACCOUNT_DETAIL",
                Routes.ACCOUNT_DETAIL,
                navController.currentDestination?.route,
            )
        }
    }

    private fun rowClickLabel(rowTag: String): String =
        rule.onNodeWithTag(rowTag).fetchSemanticsNode()
            .config[SemanticsActions.OnClick].label ?: "<none>"

    // ------------------------------------------------------------------
    // 3. "Show N more": pages through GET /transactions?limit&offset
    //    &account_id, appends in ledger order, kills the dead-end copy.
    // ------------------------------------------------------------------

    @Test
    fun showMorePagesThroughTransactionsRouteAndDropsDeadEndCopy() {
        FakeServer().use { server ->
            server.serveDetailApi(account = cashAccountJson(), entries = ledgerFixture(45))
            launch(server)

            // First window: the 20 OLDEST rows (ids 1001…1020), nothing else.
            rule.awaitNodeWithTag(AccountDetailTags.ledgerRow(1001))
            rule.awaitNodeWithTag(AccountDetailTags.ledgerRow(1020))
            assertFalse(
                "row 21 waits behind Show N more",
                rule.tagDisplayed(AccountDetailTags.ledgerRow(1021)),
            )
            rule.awaitText("Show 20 more")
            assertFalse("dead-end copy is gone", textContains("full history on the web"))
            assertFalse("old footer count copy is gone", textContains("entries ·"))

            // Tap 1: window = ASC[20,40) = DESC offset 45−20−20 = 5, limit 20.
            rule.onNodeWithTag(AccountDetailTags.SHOW_MORE).performScrollTo()
            rule.onNodeWithTag(AccountDetailTags.SHOW_MORE).performClick()
            rule.awaitNodeWithTag(AccountDetailTags.ledgerRow(1040))
            rule.awaitText("Show 5 more")
            assertFalse("row 41 still behind the next tap", rule.tagDisplayed(AccountDetailTags.ledgerRow(1041)))

            // Tap 2: last window = ASC[40,45) → DESC offset 0, limit 5.
            rule.onNodeWithTag(AccountDetailTags.SHOW_MORE).performScrollTo()
            rule.onNodeWithTag(AccountDetailTags.SHOW_MORE).performClick()
            rule.awaitNodeWithTag(AccountDetailTags.ledgerRow(1045))
            assertFalse("exhausted — no further control", rule.tagDisplayed(AccountDetailTags.SHOW_MORE))
            assertFalse("dead-end copy never returns", textContains("full history on the web"))

            assertFullAscendingLedger()

            // The ONLY rows fetched after the initial ledger: the existing
            // /transactions route, windowed, account-scoped. No new API.
            val txPages = server.requests().filter { it.requestUrl?.encodedPath == "/api/v1/transactions" }
            assertEquals("two Show-N-more fetches", 2, txPages.size)
            assertEquals("20", txPages[0].requestUrl?.queryParameter("limit"))
            assertEquals("5", txPages[0].requestUrl?.queryParameter("offset"))
            assertEquals("1", txPages[0].requestUrl?.queryParameter("account_id"))
            assertEquals("5", txPages[1].requestUrl?.queryParameter("limit"))
            assertEquals("0", txPages[1].requestUrl?.queryParameter("offset"))
            assertEquals("1", txPages[1].requestUrl?.queryParameter("account_id"))
            assertEquals("GET only", "GET", txPages[0].method)
        }
    }

    /** Every row renders once, in strict ledger order (tree order = layout). */
    private fun assertFullAscendingLedger() {
        val present = (1001..1045).filter { rule.tagDisplayed(AccountDetailTags.ledgerRow(it)) }
        assertEquals("all 45 rows render", (1001..1045).toList(), present)
        val rowTagPrefix = "account_ledger_row_"
        val ids = rule.onAllNodes(
            SemanticsMatcher("ledger rows") { node ->
                node.config.contains(SemanticsProperties.TestTag) &&
                    node.config[SemanticsProperties.TestTag].startsWith(rowTagPrefix)
            },
            useUnmergedTree = true,
        ).fetchSemanticsNodes().mapNotNull { node ->
            if (node.config.contains(SemanticsProperties.TestTag)) {
                node.config[SemanticsProperties.TestTag].removePrefix(rowTagPrefix).toIntOrNull()
            } else {
                null
            }
        }
        assertEquals("ledger rows render in ascending order", (1001..1045).toList(), ids)
    }

    // ------------------------------------------------------------------
    // 4. Frame A de-dup: hero owns total_value; no card row repeats it.
    // ------------------------------------------------------------------

    @Test
    fun frameADropsTheRowsThatRepeatTheHero() {
        FakeServer().use { server ->
            server.serveDetailApi(account = cashAccountJson(), entries = ledgerFixture(3))
            launch(server)

            rule.awaitNodeWithTag(AccountDetailTags.BALANCE_HERO)
            rule.awaitText("26,720.07")

            // The figure prints EXACTLY once on the whole screen (the hero).
            assertEquals(
                "hero figure must appear exactly once",
                1,
                rule.onAllNodesWithText("26,720.07").fetchSemanticsNodes().size,
            )
            assertFalse("cash balance equal to the hero is dropped", rule.textDisplayed("Cash balance"))
            assertFalse("0.00 investment value on a cash account is dropped", rule.textDisplayed("Investment value"))
            assertTrue("composition keeps starting balance", rule.textDisplayed("Starting balance"))
            assertTrue("field list keeps its identity rows", rule.textDisplayed("Type"))
            assertTrue("field list keeps its currency row", rule.textDisplayed("Currency"))
        }
    }

    /** Investment accounts keep cash/investment composition beside the hero. */
    @Test
    fun investmentAccountKeepsItsCompositionRows() {
        FakeServer().use { server ->
            server.serveDetailApi(account = tfsaAccountJson(), entries = ledgerFixture(3))
            launch(server)

            rule.awaitText("110.00")
            assertTrue("cash ≠ total → cash row stays", rule.textDisplayed("Cash balance"))
            assertTrue("non-zero investment value stays", rule.textDisplayed("Investment value"))
            // The former Balance row's figure = the hero → exactly one node.
            assertEquals(1, rule.onAllNodesWithText("110.00").fetchSemanticsNodes().size)
        }
    }

    // ------------------------------------------------------------------
    // 5. Running balance: on-list, under the amount, right rail, 12sp
    //    line-box, currency printed ONCE per row, masked-aware.
    // ------------------------------------------------------------------

    @Test
    fun runningBalanceStaysOnTheRailWithOneCurrencyPerRow() {
        FakeServer().use { server ->
            server.serveDetailApi(account = cashAccountJson(), entries = ledgerFixture(3))
            launch(server)

            val rowTag = AccountDetailTags.ledgerRow(1001)
            rule.awaitNodeWithTag(rowTag)
            val row = rule.onNodeWithTag(rowTag)

            // The ledger's currency comes from the ACCOUNT section (it can
            // compose before the account row is Ready) — wait for the currency
            // to be on screen before counting it, or this races the loads.
            rule.awaitText("CAD")
            rule.waitForIdle()
            // Currency exactly ONCE in the row (the amount's; none on the balance line).
            val cadNodes = rule.onAllNodes(
                hasText("CAD", substring = true) and hasAnyAncestor(hasTestTag(rowTag)),
                useUnmergedTree = true,
            ).fetchSemanticsNodes()
            val rowText = rule.onNodeWithTag(rowTag).fetchSemanticsNode()
                .config[SemanticsProperties.Text].joinToString("") { it.text }
            assertEquals("currency once per row; rowText=$rowText", 1, cadNodes.size)

            val amount = rule.onNode(
                hasTestTag("ledger_amount") and hasAnyAncestor(hasTestTag(rowTag)),
                useUnmergedTree = true,
            )
            val balance = rule.onNode(
                hasTestTag("ledger_balance") and hasAnyAncestor(hasTestTag(rowTag)),
                useUnmergedTree = true,
            )
            balance.assertExists()

            val rowBounds = row.bounds()
            val amountBounds = amount.bounds()
            val balanceBounds = balance.bounds()

            // Balance sits UNDER the amount on the right rail (right half of the row).
            assertTrue(
                "balance under the amount (${balanceBounds.top} !>= ${amountBounds.bottom})",
                balanceBounds.top >= amountBounds.bottom - 1.dp,
            )
            assertTrue(
                "balance on the right rail (left ${balanceBounds.left} vs half ${(rowBounds.right - rowBounds.left) / 2})",
                balanceBounds.left >= rowBounds.left + (rowBounds.right - rowBounds.left) / 2,
            )
            // The 12sp labelSmall line box is source-level (Type.kt);
            // Robolectric's font metrics don't reproduce sp→dp line boxes
            // faithfully (it measures the 12sp line at 8dp), so pin what the
            // metrics DO carry: it is a real line, and never larger than the
            // 16sp amount above it.
            val amountH = amountBounds.bottom - amountBounds.top
            val balanceH = balanceBounds.bottom - balanceBounds.top
            assertTrue("balance line box $balanceH empty", balanceH >= 4.dp)
            assertTrue("balance line $balanceH exceeds the amount line $amountH", balanceH <= amountH)
        }
    }

    // ------------------------------------------------------------------
    // App bar: ONE privacy action (it never scrolls away), explicit 48dp
    // back/eye, back pops.
    // ------------------------------------------------------------------

    @Test
    fun appBarEyeMasksOnceAt48dpAndBackPops() {
        FakeServer().use { server ->
            server.serveDetailApi(account = cashAccountJson(), entries = ledgerFixture(3))
            launch(server, reveal = false)

            rule.awaitNodeWithTag(ScreenTags.ACCOUNT_DETAIL)
            // The hero exists only once the account row is Ready — waiting on
            // the screen container alone raced its first paint.
            rule.awaitNodeWithTag(AccountDetailTags.HERO_VALUE)
            assertEquals(
                "exactly one mask toggle exists (the header's copy is suppressed)",
                1,
                rule.onAllNodesWithTag(SectionTags.MASK_TOGGLE).fetchSemanticsNodes().size,
            )
            assertTrue("cold start masks the hero", rule.textDisplayed(MASK_BULLETS))
            assertFloor("app bar eye", rule.onNodeWithTag(SectionTags.MASK_TOGGLE), 48.dp)

            rule.onNodeWithTag(SectionTags.MASK_TOGGLE).performClick()
            rule.awaitText("26,720.07")
            assertFalse("revealed — no bullets left on screen", rule.textDisplayed(MASK_BULLETS))

            assertFloor("app bar back", rule.onNodeWithTag(AppBarTags.BACK), 48.dp)
            rule.onNodeWithTag(AppBarTags.BACK).performClick()
            rule.waitForIdle()
            assertEquals("back pops to the list below", Routes.GLANCE, navController.currentDestination?.route)
        }
    }

    // ------------------------------------------------------------------
    // Tap floors at 390 (class qualifier) and 360 (wrapped app).
    // ------------------------------------------------------------------

    @Test
    fun tapTargetsHoldTheFloorAt390() = assertTapTargets(widthDp = null)

    @Test
    fun tapTargetsHoldTheFloorAt360() = assertTapTargets(widthDp = 360)

    private fun assertTapTargets(widthDp: Int?) {
        FakeServer().use { server ->
            server.serveDetailApi(account = tfsaAccountJson(), entries = ledgerFixture(45))
            launch(server, widthDp = widthDp)

            rule.awaitNodeWithTag(AccountDetailTags.HOLDINGS)
            // The section tag appears while its content is still loading —
            // wait for the row itself before scrolling to it.
            rule.awaitNodeWithTag(AccountDetailTags.holdingRow(9))
            rule.awaitNodeWithTag(AccountDetailTags.ledgerRow(1001))

            // App-bar actions: EXPLICITLY 48dp (docs/09 back/eye contract).
            assertFloor("app bar back", rule.onNodeWithTag(AppBarTags.BACK), 48.dp)
            assertFloor("app bar eye", rule.onNodeWithTag(SectionTags.MASK_TOGGLE), 48.dp)

            // Holdings row: whole-row tap target (spec'd 64–82px, floor 44).
            val holding = rule.onNodeWithTag(AccountDetailTags.holdingRow(9))
            holding.performScrollTo()
            assertFloor("holdings row", holding, 44.dp)

            // Ledger row (spec'd 64–65px, floor 44).
            val ledgerRow = rule.onNodeWithTag(AccountDetailTags.ledgerRow(1001))
            ledgerRow.performScrollTo()
            assertFloor("ledger row", ledgerRow, 44.dp)

            // Show N more (explicit 48dp control, floor 44).
            val showMore = rule.onNodeWithTag(AccountDetailTags.SHOW_MORE)
            showMore.performScrollTo()
            assertFloor("show more", showMore, 44.dp)
            showMore.assertHasClickAction()
        }
    }

    private fun assertFloor(label: String, node: SemanticsNodeInteraction, min: Dp) {
        node.assertExists()
        val bounds = node.bounds()
        assertTrue("$label height ${(bounds.bottom - bounds.top)} < $min", (bounds.bottom - bounds.top) >= min)
        assertTrue("$label width ${(bounds.right - bounds.left)} < 44.dp", (bounds.right - bounds.left) >= 44.dp)
    }

    private fun SemanticsNodeInteraction.bounds(): DpRect = getBoundsInRoot()

    // ------------------------------------------------------------------
    // Fixtures — every value a server string; no figure is derived here.
    // ------------------------------------------------------------------

    private fun cashAccountJson(): String =
        """{"id":1,"name":"Chequing","type":"chequing","currency":"CAD","cash_balance":"26720.07",""" +
            """"investment_value":"0.00","total_value":"26720.07","starting_balance":"1000.00",""" +
            """"last_sync_at":null,"provider_status":null,"pending_import_count":0}"""

    private fun tfsaAccountJson(): String =
        """{"id":1,"name":"TFSA","type":"tfsa","currency":"CAD","cash_balance":"10.00",""" +
            """"investment_value":"100.00","total_value":"110.00","starting_balance":"5.00",""" +
            """"last_sync_at":null,"provider_status":null,"pending_import_count":0}"""

    private fun positionsJson(): String =
        """{"items":[{"account_id":1,"account_name":"TFSA","security_id":9,"symbol":"VCN",""" +
            """"listing_currency":"CAD","quantity":"000010.0000","price_date":"2026-10-28","price":"0.05",""" +
            """"price_source":"manual","price_age_days":1,"price_fetched_at":null,"price_stale":false,""" +
            """"market_value_native":"1234.56","market_value_cad":"1234.56","book_cost_cad":"1222.07",""" +
            """"day_change_native":null,"day_change_pct":null,"day_change_cad":null,"unrealized_gain_cad":"12.5"}],""" +
            """"total":1,"total_unrealized_cad":"12.50","total_value_cad":"1234.56","cash_cad":"10.00",""" +
            """"portfolio_value_cad":"1244.57","total_day_change_cad":null,"total_day_change_pct":null,""" +
            """"day_change_as_of":"2026-10-28","day_change_mixed_baseline":false}"""

    /** One ascending ledger entry (date and id both ascend with i). */
    private data class Entry(val id: Int, val date: String, val description: String, val amount: String, val balance: String)

    /**
     * [count] ascending entries. Descriptions are LONG on purpose — every
     * one wraps past two lines at phone width, so the clamp is observable
     * on the first row (the mockup clips all of them at 390).
     */
    private fun ledgerFixture(count: Int): List<Entry> = (0 until count).map { i ->
        Entry(
            id = 1001 + i,
            date = "2026-%02d-%02d".format(1 + i / 28, 1 + i % 28),
            description = "MERCHANT ENTRY ${1000 + i} " +
                "CARD PURCHASE WITH A DESCRIPTION LONG ENOUGH TO WRAP INTO SEVERAL LINES AT PHONE WIDTH ".repeat(10),
            amount = "-10.00",
            balance = "${990 - i * 10}.00",
        )
    }

    private fun ledgerJson(entries: List<Entry>): String {
        val items = entries.joinToString(",") { e ->
            """{"transaction_id":${e.id},"date":"${e.date}","description":"${e.description}",""" +
                """"amount":"${e.amount}","running_balance":"${e.balance}"}"""
        }
        val current = entries.lastOrNull()?.balance ?: "1000.00"
        return """{"items":[$items],"starting_balance":"1000.00","current_balance":"$current","total":${entries.size}}"""
    }

    /** The server's `date desc, id desc` page over the same entries. */
    private fun transactionsJson(entries: List<Entry>, limit: Int, offset: Int): String {
        val window = entries.reversed().drop(offset).take(limit)
        val items = window.joinToString(",") { e ->
            """{"id":${e.id},"account_id":1,"date":"${e.date}","raw_description":"${e.description}",""" +
                """"normalized_payee":"","amount_native":"${e.amount}","currency":"CAD","cad_amount":null,""" +
                """"category_id":null,"reviewed_at":null,"is_recurring":false,"has_receipt":false,""" +
                """"is_split":false,"parent_split_id":null,"note":null}"""
        }
        return """{"items":[$items],"total":${entries.size},"limit":$limit,"offset":$offset,"withheld":0}"""
    }

    private fun emptyPositionsJson(): String =
        """{"items":[],"total":0,"total_unrealized_cad":"0.00","total_value_cad":"0.00","cash_cad":"0.00",""" +
            """"portfolio_value_cad":"0.00","total_day_change_cad":null,"total_day_change_pct":null,""" +
            """"day_change_as_of":null,"day_change_mixed_baseline":false}"""

    /** Param-aware routing for the whole detail screen. */
    private fun FakeServer.serveDetailApi(
        account: String,
        entries: List<Entry>,
        positions: String = positionsJson(),
    ) {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val url = request.requestUrl
                val path = url?.encodedPath ?: ""
                val body = when {
                    path.endsWith("/ledger") -> ledgerJson(entries)
                    path == "/api/v1/transactions" -> transactionsJson(
                        entries = entries,
                        limit = url?.queryParameter("limit")?.toIntOrNull() ?: 50,
                        offset = url?.queryParameter("offset")?.toIntOrNull() ?: 0,
                    )
                    path.endsWith("/portfolio/positions") -> positions
                    path.startsWith("/api/v1/accounts/") -> account
                    else -> null
                } ?: return MockResponse()
                    .setResponseCode(404)
                    .setHeader("Content-Type", "application/json")
                    .setBody("""{"detail":"no canned route for $path","code":"not_found"}""")
                return MockResponse()
                    .setResponseCode(200)
                    .setHeader("Content-Type", "application/json")
                    .setBody(body)
            }
        }
    }
}
