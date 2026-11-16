package com.margosmark.fintracker

import com.margosmark.fintracker.session.AuthState
import com.margosmark.fintracker.session.LogoutOutcome
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue
import org.junit.After
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * docs/52 §2 rec 3 / docs/53 AC3.1: logout attempts server revocation with
 * the current jar, then clears jar + access token and purges every
 * account-derived store — and an OFFLINE logout still clears local state,
 * disclosing that server revocation could not be confirmed.
 */
class LogoutPurgeTest {
    @get:Rule
    val folder = TemporaryFolder()

    private lateinit var stack: TestStack

    @After
    fun tearDown() {
        if (this::stack.isInitialized) stack.close()
    }

    @Test
    fun `logout - revokes server-side with the cookie, clears jar and token, purges all stores`() {
        stack = startStack(folder)
        stack.server.enqueue(sessionResponse(accessToken = "access-1"))
        stack.session.login("a@example.com", "pw")
        assertTrue(stack.jar.storedCookieNames().contains("refresh_token"), "jar carries the cookie after login")
        assertTrue(stack.storeFile.exists(), "sealed cookie store was persisted")
        val purged = mutableListOf<String>()
        stack.purgeRegistry.register("query-cache") { purged += "query-cache" }
        stack.purgeRegistry.register("widget-state") { purged += "widget-state" }
        stack.server.enqueue(okhttp3.mockwebserver.MockResponse().setResponseCode(204))

        val outcome = stack.session.logout()

        assertEquals(LogoutOutcome.SERVER_REVOKED, outcome)
        // The logout request itself carried the cookie — the revocation is real, not local theatre.
        val logoutRequest = stack.server.recorded().to("/auth/logout").single()
        assertTrue("refresh_token=rt-cookie-1" in (logoutRequest.getHeader("Cookie") ?: ""), "cookie-only logout route")
        assertEquals(listOf("query-cache", "widget-state"), purged, "every registered store purged")
        assertTrue(stack.jar.isEmpty(), "jar cleared")
        assertNull(stack.client.tokenStore.accessToken, "in-memory access token cleared")
        assertTrue(!stack.storeFile.exists(), "sealed store file deleted")
        assertEquals(AuthState.SignedOut, stack.session.state)
    }

    @Test
    fun `offline logout - local state cleared, revocation reported unconfirmed`() {
        stack = startStack(folder)
        stack.server.enqueue(sessionResponse(accessToken = "access-1"))
        stack.session.login("a@example.com", "pw")
        val purged = mutableListOf<String>()
        stack.purgeRegistry.register("query-cache") { purged += "query-cache" }
        // The server goes away entirely: the logout call cannot even connect.
        stack.server.shutdown()

        val outcome = stack.session.logout()

        assertEquals(LogoutOutcome.REVOCATION_UNCONFIRMED, outcome, "the UI must disclose this (docs/52 §2 rec 3)")
        assertEquals(listOf("query-cache"), purged, "local purge happens regardless of connectivity")
        assertTrue(stack.jar.isEmpty())
        assertNull(stack.client.tokenStore.accessToken)
        assertEquals(AuthState.SignedOut, stack.session.state)
    }

    @Test
    fun `logout when the server already invalidated the token is not an error`() {
        stack = startStack(folder)
        stack.server.enqueue(sessionResponse(accessToken = "access-1"))
        stack.session.login("a@example.com", "pw")
        stack.server.enqueue(
            errorResponseBody(401, "Invalid or revoked refresh token", "invalid_or_revoked_refresh_token"),
        )

        val outcome = stack.session.logout()

        assertEquals(LogoutOutcome.ALREADY_INVALID, outcome, "nothing left to revoke is not a failure the user sees")
        assertTrue(stack.jar.isEmpty() && !stack.storeFile.exists())
        assertEquals(AuthState.SignedOut, stack.session.state)
    }

    @Test
    fun `explicit logout drops the pending destination`() {
        stack = startStack(folder)
        stack.session.noteDestination("account/5")
        stack.server.enqueue(sessionResponse(accessToken = "access-1"))
        stack.session.login("a@example.com", "pw")
        stack.server.enqueue(okhttp3.mockwebserver.MockResponse().setResponseCode(204))

        stack.session.logout()

        assertNull(stack.session.consumePendingDestination(), "explicit sign-out has nothing to return to")
    }

    @Test
    fun `logout against a 500 still ends SignedOut with empty jar and null token`() {
        // Security M1: a server error must not escape before local clearing.
        stack = startStack(folder)
        stack.server.enqueue(sessionResponse(accessToken = "access-1"))
        stack.session.login("a@example.com", "pw")
        stack.server.enqueue(errorResponseBody(500, "Internal server error", "internal_error"))

        val outcome = stack.session.logout()

        assertEquals(LogoutOutcome.REVOCATION_UNCONFIRMED, outcome)
        assertEquals(AuthState.SignedOut, stack.session.state, "server error must not leave the user signed in")
        assertTrue(stack.jar.isEmpty(), "jar cleared even on server error")
        assertNull(stack.client.tokenStore.accessToken, "token cleared even on server error")
    }

    @Test
    fun `logout against a 429 still ends SignedOut with empty jar and null token`() {
        // Security M1: rate limiting must not escape before local clearing.
        stack = startStack(folder)
        stack.server.enqueue(sessionResponse(accessToken = "access-1"))
        stack.session.login("a@example.com", "pw")
        stack.server.enqueue(errorResponseBody(429, "Too many attempts", "rate_limited"))

        val outcome = stack.session.logout()

        assertEquals(LogoutOutcome.REVOCATION_UNCONFIRMED, outcome)
        assertEquals(AuthState.SignedOut, stack.session.state, "rate limit must not leave the user signed in")
        assertTrue(stack.jar.isEmpty(), "jar cleared even on rate limit")
        assertNull(stack.client.tokenStore.accessToken, "token cleared even on rate limit")
    }
}
