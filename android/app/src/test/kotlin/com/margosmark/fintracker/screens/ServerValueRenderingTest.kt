package com.margosmark.fintracker.screens

import com.margosmark.fintracker.FlushesBeforeWaitRule

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.navigation.compose.ComposeNavigator
import androidx.navigation.testing.TestNavHostController
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.margosmark.fintracker.FakeServer
import com.margosmark.fintracker.navigation.FinTrackerApp
import com.margosmark.fintracker.serverBackedScope
import com.margosmark.fintracker.state.MASK_BULLETS
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/**
 * AC1.1 server-value rendering, proven end to end over the wire: whatever
 * strings MockWebServer sends are what the glance screen shows — including a
 * headline total that deliberately does not equal the sum of its rows. If the
 * client ever summed or reformatted, "0.33" would appear and "777.77" would not.
 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class ServerValueRenderingTest {

    @get:Rule
    val rule = FlushesBeforeWaitRule(createComposeRule())

    private fun launch(server: FakeServer, reveal: Boolean = true) {
        val navController = TestNavHostController(ApplicationProvider.getApplicationContext())
        navController.navigatorProvider.addNavigator(ComposeNavigator())
        val scope = serverBackedScope(server)
        if (reveal) scope.mask.reveal()
        rule.setContent {
            FinTrackerApp(scope = scope, navController = navController)
        }
    }

    @Test
    fun serverStringsRenderVerbatimAndTotalsAreNeverClientSummed() {
        FakeServer().use { server ->
            server.serveByPath(*Fixtures.fullReadRouting())
            launch(server)

            // The server's headline (rows sum to 0.33 — the screen must show
            // the server's own string, not a computed figure).
            rule.awaitText("787.77")
            assertTrue(rule.textDisplayed("0.11"))
            assertTrue(rule.textDisplayed("0.22"))
            assertFalse("client-side sum rendered", rule.textDisplayed("0.33"))

            // Verbatim price precision survives the round trip to the screen.
            assertTrue(rule.textDisplayed("0.05"))
        }
    }

    @Test
    fun homeFormatsSyntheticValuesAndMasksThem() {
        FakeServer().use { server ->
            server.serveByPath(*(Fixtures.fullReadRouting().map { (path, body) ->
                path to body.replace("787.77", "12345.67").replace("0.05", "123.456789")
            }).toTypedArray())
            launch(server, reveal = false)
            rule.awaitText("TFSA")
            assertFalse(rule.textDisplayed("12,345.67"))
            assertFalse(rule.textDisplayed("123.4568"))
            rule.onNodeWithTag(com.margosmark.fintracker.ui.screens.SectionTags.MASK_TOGGLE).performClick()
            rule.awaitText("12,345.67")
            assertTrue(rule.textDisplayed("123.4568"))
            assertFalse(rule.textDisplayed("123.456789"))
        }
    }

    @Test
    fun mutatedServerFieldChangesWhatTheScreenShows() {
        // The ONLY difference from the fixture above is one response field,
        // and the screen renders the mutated string — proof the value on
        // screen is the server's, not anything the client computed.
        FakeServer().use { server ->
            server.serveByPath(
                *(Fixtures.fullReadRouting().map { (path, body) ->
                    path to body.replace("787.77", "555.55")
                }).toTypedArray(),
            )
            launch(server)
            rule.awaitText("555.55")
            assertFalse(rule.textDisplayed("787.77"))
        }
    }

    @Test
    fun coldStartBootsMaskedAndShowsNoAbsoluteValue() {
        FakeServer().use { server ->
            server.serveByPath(*Fixtures.fullReadRouting())
            launch(server, reveal = false)

            // Sections resolved to real figures (not stuck loading) ...
            rule.awaitText("TFSA")
            // ... yet every absolute value stays behind bullets.
            assertTrue(rule.textDisplayed(MASK_BULLETS))
            assertFalse(rule.textDisplayed("787.77"))
            assertFalse(rule.textDisplayed("10.00"))
            assertFalse(rule.textDisplayed("0.11"))

            // Tapping the mask toggle reveals — values are the same strings.
            rule.onNodeWithTag(com.margosmark.fintracker.ui.screens.SectionTags.MASK_TOGGLE)
                .assertIsDisplayed()
                .performClick()
            rule.awaitText("787.77")
        }
    }
}
