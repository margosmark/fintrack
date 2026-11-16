package com.margosmark.fintracker

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithTag
import androidx.navigation.compose.ComposeNavigator
import androidx.navigation.testing.TestNavHostController
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.margosmark.fintracker.navigation.DeepLinkResolution
import com.margosmark.fintracker.navigation.DeepLinkTarget
import com.margosmark.fintracker.navigation.FinTrackerApp
import com.margosmark.fintracker.navigation.Routes
import com.margosmark.fintracker.navigation.ScreenTags
import com.margosmark.fintracker.navigation.SessionSnapshot
import com.margosmark.fintracker.navigation.parseDeepLink
import com.margosmark.fintracker.navigation.resolveDeepLink
import com.margosmark.fintracker.http.UserPublic
import com.margosmark.fintracker.session.AuthState
import kotlinx.coroutines.flow.MutableStateFlow
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/**
 * Deep-link gate: links resolve ONLY to authenticated, extant, same-tenant
 * targets. Unknown/stale/foreign targets render the generic unavailable
 * destination; unauthenticated links land on sign-in.
 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class DeepLinkGateTest {

    @get:Rule
    val composeRule = FlushesBeforeWaitRule(createComposeRule())

    private val authed = SessionSnapshot(isAuthenticated = true, tenantId = "t1")
    private val unauthed = SessionSnapshot.UNAUTHENTICATED

    // ---- pure resolver: parsing -------------------------------------------------

    @Test
    fun parsesKnownShapes() {
        assertEquals(DeepLinkTarget.Glance, parseDeepLink("fintracker://app/glance"))
        assertEquals(DeepLinkTarget.AccountsList, parseDeepLink("fintracker://app/accounts"))
        assertEquals(DeepLinkTarget.Account("42"), parseDeepLink("fintracker://app/accounts/42"))
        assertEquals(
            DeepLinkTarget.Holding("42", "7"),
            parseDeepLink("fintracker://app/accounts/42/holdings/7"),
        )
        assertEquals(DeepLinkTarget.Transactions, parseDeepLink("fintracker://app/transactions"))
        assertEquals(DeepLinkTarget.Settings, parseDeepLink("fintracker://app/settings"))
        assertEquals(DeepLinkTarget.Account("42"), parseDeepLink("https://fintrac.princessandmark.com/app/accounts/42"))
    }

    @Test
    fun rejectsUnknownShapes() {
        assertNull(parseDeepLink(null))
        assertNull(parseDeepLink(""))
        assertNull(parseDeepLink("fintracker://evil/glance"))       // foreign host
        assertNull(parseDeepLink("fintracker://app/nope"))          // unknown path
        assertNull(parseDeepLink("fintracker://app/accounts/42/extra")) // unknown depth
        assertNull(parseDeepLink("mailto:someone@example.com"))     // foreign scheme
        assertNull(parseDeepLink("https://evil.example/app/accounts/1")) // unserved host
        assertNull(parseDeepLink("https://host.example/not-app/accounts/1"))
        // The apex does not serve the app (t_16001696) — foreign, like any
        // other subdomain or look-alike (full matrix in DeepLinkHostGateTest).
        assertNull(parseDeepLink("https://princessandmark.com/app/accounts/1"))
        assertNull(parseDeepLink("https://fintrac-princessandmark.com/app/accounts/1"))
    }

    // ---- pure resolver: gating --------------------------------------------------

    @Test
    fun unauthenticatedRecognizedLinkRequiresSignIn() {
        assertEquals(
            DeepLinkResolution.RequireSignIn,
            resolveDeepLink(parseDeepLink("fintracker://app/glance"), unauthed),
        )
        assertEquals(
            DeepLinkResolution.RequireSignIn,
            resolveDeepLink(parseDeepLink("fintracker://app/accounts/42"), unauthed),
        )
    }

    @Test
    fun authenticatedListDestinationsNavigate() {
        for ((target, route) in listOf(
            DeepLinkTarget.Glance to Routes.GLANCE,
            DeepLinkTarget.AccountsList to Routes.ACCOUNTS,
            DeepLinkTarget.Transactions to Routes.TRANSACTIONS,
            DeepLinkTarget.Settings to Routes.SETTINGS,
        )) {
            assertEquals(DeepLinkResolution.Navigate(route), resolveDeepLink(target, authed))
        }
    }

    @Test
    fun staleTargetResolvesUnavailable() {
        // Authenticated, but the account no longer exists → unavailable.
        assertEquals(
            DeepLinkResolution.Unavailable,
            resolveDeepLink(DeepLinkTarget.Account("42"), authed) { false },
        )
        assertEquals(
            DeepLinkResolution.Unavailable,
            resolveDeepLink(DeepLinkTarget.Holding("42", "7"), authed) { false },
        )
    }

    @Test
    fun foreignTenantTargetResolvesUnavailable() {
        // The data layer scopes existence checks to the session's tenant, so
        // a target owned by another tenant answers "not extant" — the shell
        // never sees a foreign route.
        val tenant2Lookup = { _: DeepLinkTarget -> false }
        assertEquals(
            DeepLinkResolution.Unavailable,
            resolveDeepLink(DeepLinkTarget.Account("42"), authed, tenant2Lookup),
        )
    }

    @Test
    fun extantTargetResolvesToItsRoute() {
        assertEquals(
            DeepLinkResolution.Navigate(Routes.accountDetail("42")),
            resolveDeepLink(DeepLinkTarget.Account("42"), authed) { true },
        )
        assertEquals(
            DeepLinkResolution.Navigate(Routes.holdingDetail("42", "7")),
            resolveDeepLink(DeepLinkTarget.Holding("42", "7"), authed) { true },
        )
    }

    @Test
    fun unknownShapeResolvesUnavailableEvenWhenAuthed() {
        assertEquals(
            DeepLinkResolution.Unavailable,
            resolveDeepLink(null, authed),
        )
    }

    // ---- shell wiring: the rendered destination ---------------------------------

    private lateinit var navController: TestNavHostController

    private fun launchShell(uri: String?, session: SessionSnapshot, exists: (DeepLinkTarget) -> Boolean) {
        navController = TestNavHostController(ApplicationProvider.getApplicationContext())
        navController.navigatorProvider.addNavigator(ComposeNavigator())
        // The graph now derives the gate input from the REAL auth flow; the
        // snapshot is translated into it here so the pure gate semantics
        // (resolveDeepLink above) stay exactly what this file pins.
        val auth = MutableStateFlow(
            if (session.isAuthenticated) {
                AuthState.Authenticated(
                    UserPublic(id = 1, email = "gate@test.local", name = null, privacyMode = false),
                )
            } else {
                AuthState.SignedOut
            },
        )
        composeRule.setContent {
            FinTrackerApp(
                scope = testAppScope(auth),
                targetExists = exists,
                deepLinkUri = uri,
                navController = navController,
            )
        }
    }

    @Test
    fun unauthenticatedDeepLinkLandsOnSignIn() {
        launchShell("fintracker://app/glance", unauthed) { true }
        composeRule.waitForIdle()
        composeRule.onNodeWithTag(ScreenTags.SIGN_IN).assertIsDisplayed()
    }

    @Test
    fun staleDeepLinkLandsOnUnavailableDestination() {
        launchShell("fintracker://app/accounts/42", authed) { false }
        // The existence gate runs in a background LaunchedEffect — wait for it.
        composeRule.waitUntil(5_000) {
            composeRule.onAllNodesWithTag(ScreenTags.UNAVAILABLE, useUnmergedTree = true)
                .fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onNodeWithTag(ScreenTags.UNAVAILABLE).assertIsDisplayed()
    }

    @Test
    fun foreignHostDeepLinkLandsOnUnavailableDestination() {
        launchShell("fintracker://evil/accounts/42", authed) { true }
        composeRule.waitForIdle()
        composeRule.onNodeWithTag(ScreenTags.UNAVAILABLE).assertIsDisplayed()
    }

    @Test
    fun extantDeepLinkLandsOnTheRealDestination() {
        launchShell("fintracker://app/accounts/42", authed) { true }
        // The existence gate runs in a background LaunchedEffect — wait for it.
        composeRule.waitUntil(5_000) {
            composeRule.onAllNodesWithTag(ScreenTags.ACCOUNT_DETAIL, useUnmergedTree = true)
                .fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onNodeWithTag(ScreenTags.ACCOUNT_DETAIL).assertIsDisplayed()
    }

    // ---- manifest ↔ code gate agreement (t_16001696) --------------------------

    @Test
    fun manifestRoutesHttpsLinksOnlyForTheServedHost() {
        // The intent-filter's <data android:host> list (AndroidManifest.xml)
        // must agree with the code gate: the served host and the custom
        // scheme route to the app; the apex, look-alikes and foreign hosts
        // never do.
        assertTrue(viewIntentResolves("https://fintrac.princessandmark.com/app/glance"))
        assertTrue(viewIntentResolves("fintracker://app/glance"))
        assertFalse(viewIntentResolves("https://princessandmark.com/app/glance"))
        assertFalse(viewIntentResolves("https://evilprincessandmark.com/app/glance"))
        assertFalse(viewIntentResolves("https://evil.example/app/glance"))
    }

    private fun viewIntentResolves(uri: String): Boolean {
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse(uri))
            .addCategory(Intent.CATEGORY_BROWSABLE)
        return ApplicationProvider.getApplicationContext<Context>()
            .packageManager.queryIntentActivities(intent, 0)
            .isNotEmpty()
    }
}
