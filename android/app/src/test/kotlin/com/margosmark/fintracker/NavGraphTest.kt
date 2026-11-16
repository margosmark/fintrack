package com.margosmark.fintracker

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.navigation.compose.ComposeNavigator
import androidx.navigation.testing.TestNavHostController
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.margosmark.fintracker.navigation.FinTrackerApp
import com.margosmark.fintracker.navigation.Routes
import com.margosmark.fintracker.navigation.ScreenTags
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/**
 * Nav-graph contract: every v1 destination route resolves to a screen, and
 * list→detail→back preserves context (system back returns to the list).
 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class NavGraphTest {

    @get:Rule
    val composeRule = FlushesBeforeWaitRule(createComposeRule())

    private lateinit var navController: TestNavHostController

    @Before
    fun setUp() {
        navController = TestNavHostController(ApplicationProvider.getApplicationContext())
        navController.navigatorProvider.addNavigator(ComposeNavigator())
        composeRule.setContent {
            FinTrackerApp(scope = testAppScope(), navController = navController)
        }
    }

    /** navigate(route) → the destination pattern that must be on top. */
    private val routeToPattern: List<Pair<String, String>> = listOf(
        Routes.SIGN_IN to Routes.SIGN_IN,
        Routes.GLANCE to Routes.GLANCE,
        Routes.ACCOUNTS to Routes.ACCOUNTS,
        Routes.accountDetail("1") to Routes.ACCOUNT_DETAIL,
        Routes.holdingDetail("1", "1") to Routes.HOLDING_DETAIL,
        Routes.TRANSACTIONS to Routes.TRANSACTIONS,
        Routes.SETTINGS to Routes.SETTINGS,
        Routes.UNAVAILABLE to Routes.UNAVAILABLE,
    )

    @Test
    fun everyV1DestinationRouteResolves() {
        for ((route, pattern) in routeToPattern) {
            composeRule.runOnUiThread { navController.navigate(route) }
            composeRule.waitForIdle()
            val top = navController.currentBackStackEntry?.destination?.route
            assertNotNull("route $route left no current back-stack entry", top)
            assertEquals("route $route landed on the wrong destination", pattern, top)
        }
    }

    @Test
    fun allDestinationsTableCoversTheGraph() {
        // Routes.allDestinations() is the contract used by tooling; it must
        // cover every route the graph declares, placeholders substituted.
        val declared = routeToPattern.map { it.first }.toSet()
        assertTrue(
            "allDestinations() is missing routes: ${declared - Routes.allDestinations().toSet()}",
            declared == Routes.allDestinations().toSet(),
        )
    }

    @Test
    fun eachDestinationRendersItsOwnScreenTag() {
        val expectations: List<Pair<String, String>> = listOf(
            Routes.SIGN_IN to ScreenTags.SIGN_IN,
            Routes.GLANCE to ScreenTags.GLANCE,
            Routes.ACCOUNTS to ScreenTags.ACCOUNTS,
            Routes.accountDetail("7") to ScreenTags.ACCOUNT_DETAIL,
            Routes.holdingDetail("7", "3") to ScreenTags.HOLDING_DETAIL,
            Routes.TRANSACTIONS to ScreenTags.TRANSACTIONS,
            Routes.SETTINGS to ScreenTags.SETTINGS,
            Routes.UNAVAILABLE to ScreenTags.UNAVAILABLE,
        )
        for ((route, tag) in expectations) {
            composeRule.runOnUiThread { navController.navigate(route) }
            composeRule.waitForIdle()
            composeRule.onNodeWithTag(tag).assertIsDisplayed()
        }
    }

    @Test
    fun backFromAccountDetailReturnsToAccountList() {
        composeRule.runOnUiThread { navController.navigate(Routes.ACCOUNTS) }
        composeRule.waitForIdle()
        composeRule.onNodeWithTag(ScreenTags.ACCOUNTS).assertIsDisplayed()

        composeRule.runOnUiThread { navController.navigate(Routes.accountDetail("7")) }
        composeRule.waitForIdle()
        composeRule.onNodeWithTag(ScreenTags.ACCOUNT_DETAIL).assertIsDisplayed()
        // Security S1 (round 3): the `Account 7` subtitle renders only after
        // the SERVER confirms the row for the current identity. These
        // sections never load, so the route-derived id must NOT render —
        // the positive (confirmed) case is pinned in DestinationRound3Test.
        composeRule.onNodeWithText("Account 7").assertDoesNotExist()

        // System back pops detail → list, preserving list context.
        composeRule.runOnUiThread { navController.popBackStack() }
        composeRule.waitForIdle()
        composeRule.onNodeWithTag(ScreenTags.ACCOUNTS).assertIsDisplayed()
        assertEquals(Routes.ACCOUNTS, navController.currentBackStackEntry?.destination?.route)
    }

    @Test
    fun backFromHoldingDetailReturnsToAccountDetail() {
        composeRule.runOnUiThread {
            navController.navigate(Routes.accountDetail("7"))
            navController.navigate(Routes.holdingDetail("7", "3"))
        }
        composeRule.waitForIdle()
        composeRule.onNodeWithTag(ScreenTags.HOLDING_DETAIL).assertIsDisplayed()

        composeRule.runOnUiThread { navController.popBackStack() }
        composeRule.waitForIdle()
        composeRule.onNodeWithTag(ScreenTags.ACCOUNT_DETAIL).assertIsDisplayed()
    }
}
