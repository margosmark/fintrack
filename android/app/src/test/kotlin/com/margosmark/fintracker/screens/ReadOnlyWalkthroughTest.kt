package com.margosmark.fintracker.screens

import com.margosmark.fintracker.FlushesBeforeWaitRule

import androidx.compose.ui.test.hasScrollAction
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.navigation.compose.ComposeNavigator
import androidx.navigation.testing.TestNavHostController
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.margosmark.fintracker.FakeServer
import com.margosmark.fintracker.navigation.FinTrackerApp
import com.margosmark.fintracker.navigation.Routes
import com.margosmark.fintracker.serverBackedScope
import com.margosmark.fintracker.ui.screens.SectionTags
import okhttp3.mockwebserver.RecordedRequest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/**
 * Read-only walkthrough (docs/53 AC1.1/AC5, docs/52 §3): every screen is
 * visited against a real (fake) server, and the wire log proves the client
 * issued GET requests only — no mutation, no invented transaction-detail
 * route, no write affordance hiding behind the list row.
 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class ReadOnlyWalkthroughTest {

    @get:Rule
    val rule = FlushesBeforeWaitRule(createComposeRule())

    @Test
    fun everyScreenWalksThroughOnGetRequestsOnly() {
        FakeServer().use { server ->
            server.serveByPath(*Fixtures.fullReadRouting())
            val navController = TestNavHostController(ApplicationProvider.getApplicationContext())
            navController.navigatorProvider.addNavigator(ComposeNavigator())
            val scope = serverBackedScope(server)
            scope.mask.reveal()
            rule.setContent {
                FinTrackerApp(scope = scope, navController = navController)
            }

            // Glance: positions resolve; tapping a row drills into the holding.
            // (The position row sits below the small Robolectric viewport, so
            // scroll it into view before tapping its centre.)
            rule.awaitText("VCN")
            rule.onNode(hasScrollAction()).performScrollToNode(hasText("VCN"))
            rule.onNodeWithText("VCN").performClick()
            rule.awaitText("Quantity")

            // Accounts list → account detail (with its ledger section).
            rule.runOnUiThread { navController.navigate(Routes.ACCOUNTS) }
            rule.awaitText("TFSA")
            rule.onNodeWithText("TFSA").performClick()
            rule.awaitText("Investment value")

            // Transactions list → per-row events drill-in (the ONLY drill-in
            // docs/52 §3 allows for a transaction).
            rule.runOnUiThread { navController.navigate(Routes.TRANSACTIONS) }
            rule.awaitText("COFFEE SHOP")
            rule.onNodeWithText("COFFEE SHOP").performClick()
            rule.awaitText("Imported from CSV")

            // Settings reachable, still read-only chrome.
            rule.runOnUiThread { navController.navigate(Routes.SETTINGS) }
            rule.awaitNodeWithTag(SectionTags.SIGN_OUT)

            val requests: List<RecordedRequest> = server.requests()
            assertTrue("walkthrough must hit the wire, saw ${requests.size}", requests.size >= 6)
            val writes = requests.filter { it.method != "GET" }
            assertEquals("non-GET requests on the wire: $writes", emptyList<RecordedRequest>(), writes)
            // No invented transaction-detail route: events ride on the id sub-path only.
            requests.forEach { req ->
                val path = req.requestUrl?.encodedPath ?: ""
                assertTrue("unexpected transaction path $path", !path.matches(Regex(".*/transactions/\\d+$")))
            }
        }
    }
}
