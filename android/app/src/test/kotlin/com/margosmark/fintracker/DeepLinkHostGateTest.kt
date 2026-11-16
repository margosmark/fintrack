package com.margosmark.fintracker

import com.margosmark.fintracker.navigation.DeepLinkResolution
import com.margosmark.fintracker.navigation.DeepLinkTarget
import com.margosmark.fintracker.navigation.Routes
import com.margosmark.fintracker.navigation.SERVED_HOSTS
import com.margosmark.fintracker.navigation.SessionSnapshot
import com.margosmark.fintracker.navigation.isServedHost
import com.margosmark.fintracker.navigation.parseDeepLink
import com.margosmark.fintracker.navigation.resolveDeepLink
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Deep-link HOST gate (security M3, t_16001696): https handoff links are
 * accepted for the served host(s) EXACTLY — case-insensitively, never by
 * wildcard or bare suffix. The apex and every other subdomain/look-alike stay
 * foreign, and foreign shapes still resolve to the generic unavailable
 * destination, never a pass-through. Pure JVM — no Robolectric needed.
 */
class DeepLinkHostGateTest {

    private val authed = SessionSnapshot(isAuthenticated = true, tenantId = "t1")
    private val unauthed = SessionSnapshot.UNAUTHENTICATED

    // ---- accepted: the served host, exactly -----------------------------------

    @Test
    fun servedHostParsesAllOwnedShapes() {
        assertEquals(
            DeepLinkTarget.Glance,
            parseDeepLink("https://fintrac.princessandmark.com/app/glance"),
        )
        assertEquals(
            DeepLinkTarget.Account("42"),
            parseDeepLink("https://fintrac.princessandmark.com/app/accounts/42"),
        )
        assertEquals(
            DeepLinkTarget.Holding("42", "7"),
            parseDeepLink("https://fintrac.princessandmark.com/app/accounts/42/holdings/7"),
        )
    }

    @Test
    fun servedHostMatchingIsCaseInsensitive() {
        // DNS hostnames are case-insensitive; the gate must be too.
        assertTrue(isServedHost("FINTRAC.princessandmark.com"))
        assertTrue(isServedHost("fintrac.PRINCESSANDMARK.COM"))
        assertEquals(
            DeepLinkTarget.Glance,
            parseDeepLink("https://FINTRAC.PrincessAndMark.com/app/glance"),
        )
    }

    // ---- rejected: apex, subdomains, look-alikes ------------------------------

    @Test
    fun apexAndForeignSubdomainsAreRejected() {
        // The apex does not serve the app (self-signed TLS) — no https link
        // under it is a legitimate handoff.
        assertNull(parseDeepLink("https://princessandmark.com/app/glance"))
        // Any other subdomain of the same apex is foreign…
        assertNull(parseDeepLink("https://www.princessandmark.com/app/glance"))
        assertNull(parseDeepLink("https://evil.princessandmark.com/app/glance"))
        assertNull(parseDeepLink("https://fintrac.api.princessandmark.com/app/glance"))
    }

    @Test
    fun lookAlikeHostsAreRejected() {
        // Bare-suffix and hyphen look-alikes: NO suffix matching, ever.
        assertNull(parseDeepLink("https://fintrac-princessandmark.com/app/glance"))
        assertNull(parseDeepLink("https://evilprincessandmark.com/app/glance"))
        assertNull(parseDeepLink("https://princessandmark.com.evil.example/app/glance"))
        assertNull(parseDeepLink("https://fintrac.princessandmark.com.evil.example/app/glance"))
        assertNull(parseDeepLink("https://evil.example/app/glance"))
        // Userinfo trickery: the real host is evil.example, not the userinfo.
        assertNull(parseDeepLink("https://fintrac.princessandmark.com@evil.example/app/glance"))
        assertFalse(isServedHost(null))
        assertFalse(isServedHost(""))
    }

    // ---- resolution stays gated end-to-end ------------------------------------

    @Test
    fun servedHostLinkResolvesThroughTheAuthGate() {
        assertEquals(
            DeepLinkResolution.Navigate(Routes.GLANCE),
            resolveDeepLink(parseDeepLink("https://fintrac.princessandmark.com/app/glance"), authed),
        )
        assertEquals(
            DeepLinkResolution.RequireSignIn,
            resolveDeepLink(parseDeepLink("https://fintrac.princessandmark.com/app/glance"), unauthed),
        )
    }

    @Test
    fun foreignHostLinkResolvesUnavailableEvenWhenAuthed() {
        assertEquals(
            DeepLinkResolution.Unavailable,
            resolveDeepLink(parseDeepLink("https://evil.example/app/glance"), authed),
        )
        assertEquals(
            DeepLinkResolution.Unavailable,
            resolveDeepLink(parseDeepLink("https://princessandmark.com/app/glance"), authed),
        )
    }

    @Test
    fun customSchemeIsUnchangedAndStillGated() {
        assertEquals(
            DeepLinkResolution.Navigate(Routes.GLANCE),
            resolveDeepLink(parseDeepLink("fintracker://app/glance"), authed),
        )
        assertEquals(
            DeepLinkResolution.RequireSignIn,
            resolveDeepLink(parseDeepLink("fintracker://app/glance"), unauthed),
        )
        assertNull(parseDeepLink("fintracker://evil/glance"))
    }

    @Test
    fun servedHostsSetIsExactlyTheDeployedHost() {
        // Pinned to the deployed stack (deploy-smoke.yml BASE); adding a host
        // here is a deliberate security-M3 decision, not an accident.
        assertEquals(setOf("fintrac.princessandmark.com"), SERVED_HOSTS)
    }
}
