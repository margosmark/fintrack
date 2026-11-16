package com.margosmark.fintracker.screens

import com.margosmark.fintracker.FlushesBeforeWaitRule

import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.navigation.compose.ComposeNavigator
import androidx.navigation.testing.TestNavHostController
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.margosmark.fintracker.FakeServer
import com.margosmark.fintracker.navigation.FinTrackerApp
import com.margosmark.fintracker.navigation.Routes
import com.margosmark.fintracker.serverBackedScope
import com.margosmark.fintracker.ui.screens.SectionTags
import okhttp3.mockwebserver.MockResponse
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/**
 * AC1.3: each state has its OWN render — loading shows no figures, offline is
 * not unavailable, permission is explained in place, empty is honest — and a
 * failure never collapses to zero.
 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class DistinctStatesTest {

    @get:Rule
    val rule = FlushesBeforeWaitRule(createComposeRule())

    private fun launchTransactions(server: FakeServer) {
        val navController = TestNavHostController(ApplicationProvider.getApplicationContext())
        navController.navigatorProvider.addNavigator(ComposeNavigator())
        val scope = serverBackedScope(server)
        scope.mask.reveal()
        rule.setContent {
            FinTrackerApp(scope = scope, navController = navController)
        }
        rule.runOnUiThread { navController.navigate(Routes.TRANSACTIONS) }
    }

    @Test
    fun loadingRendersNoFigures() {
        FakeServer().use { server ->
            server.server.enqueue(
                MockResponse()
                    .setResponseCode(200)
                    .setHeader("Content-Type", "application/json")
                    .setBody(Fixtures.TRANSACTIONS_JSON)
                    .setHeadersDelay(1_500, java.util.concurrent.TimeUnit.MILLISECONDS),
            )
            launchTransactions(server)
            rule.onNodeWithTag(SectionTags.LOADING).assertExists()
            assertFalse("loading must show no figures", rule.textDisplayed("COFFEE SHOP"))
        }
    }

    @Test
    fun readyRendersTheServerRows() {
        FakeServer().use { server ->
            server.serveByPath("/transactions" to Fixtures.TRANSACTIONS_JSON)
            launchTransactions(server)
            rule.awaitNodeWithTag(SectionTags.READY)
            assertTrue(rule.textDisplayed("COFFEE SHOP"))
        }
    }

    @Test
    fun emptyIsAnHonestNoData() {
        FakeServer().use { server ->
            server.serveByPath("/transactions" to Fixtures.TRANSACTIONS_EMPTY_JSON)
            launchTransactions(server)
            rule.awaitNodeWithTag(SectionTags.NO_DATA)
            assertFalse(rule.textDisplayed("COFFEE SHOP"))
        }
    }

    @Test
    fun sessionExpiredRendersTheExpiredState() {
        FakeServer().use { server ->
            server.serveStatus(401)
            launchTransactions(server)
            rule.awaitNodeWithTag(SectionTags.EXPIRED)
        }
    }

    @Test
    fun permissionRendersThePermissionState() {
        FakeServer().use { server ->
            server.serveStatus(403)
            launchTransactions(server)
            rule.awaitNodeWithTag(SectionTags.PERMISSION)
        }
    }

    @Test
    fun notFoundIsItsOwnStateOnDrillIn() {
        FakeServer().use { server ->
            server.serveStatus(404)
            val navController = TestNavHostController(ApplicationProvider.getApplicationContext())
            navController.navigatorProvider.addNavigator(ComposeNavigator())
            val scope = serverBackedScope(server)
            scope.mask.reveal()
            rule.setContent {
                FinTrackerApp(scope = scope, navController = navController)
            }
            rule.runOnUiThread { navController.navigate(Routes.accountDetail("404")) }
            rule.awaitNodeWithTag(SectionTags.NOT_FOUND)
        }
    }

    @Test
    fun serverErrorIsUnavailableNotZero() {
        FakeServer().use { server ->
            server.serveStatus(500)
            launchTransactions(server)
            rule.awaitNodeWithTag(SectionTags.UNAVAILABLE)
        }
    }

    @Test
    fun noConnectionIsOffline() {
        FakeServer().use { server ->
            server.enqueueOffline()
            launchTransactions(server)
            rule.awaitNodeWithTag(SectionTags.OFFLINE)
            assertFalse("offline must never render figures", rule.textDisplayed("COFFEE SHOP"))
        }
    }
}
