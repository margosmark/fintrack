package com.margosmark.fintracker

import com.margosmark.fintracker.http.ApiException
import com.margosmark.fintracker.session.AuthState
import com.margosmark.fintracker.ui.SessionDestinationPreserver
import java.io.IOException
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNull
import kotlin.test.assertTrue
import org.junit.After
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * docs/53 AC1.3 "Session expired": a provably dead session lands on a calm
 * SignedOut state while the intended destination is PRESERVED as an internal
 * same-app route for revalidation after re-auth. A network failure is NOT
 * treated as expiry — an offline box proves nothing about the session.
 */
class SessionExpiryTest {
    @get:Rule
    val folder = TemporaryFolder()

    private lateinit var stack: TestStack

    @After
    fun tearDown() {
        if (this::stack.isInitialized) stack.close()
    }

    @Test
    fun `dead session - signed out calmly, destination preserved for revalidation`() {
        stack = startStack(folder)
        stack.server.enqueue(sessionResponse(accessToken = "access-1"))
        stack.session.login("a@example.com", "pw")
        stack.session.noteDestination("account/5")
        // The domain GET is answered 401; the follow-up refresh is rejected too.
        stack.server.enqueue(errorResponseBody(401, "Could not validate credentials", "invalid_token"))
        stack.server.enqueue(
            errorResponseBody(401, "Invalid or revoked refresh token", "invalid_or_revoked_refresh_token"),
        )

        val error = assertFailsWith<ApiException> { stack.client.getJson("/accounts") }

        assertEquals("invalid_token", error.code)
        assertEquals(AuthState.SignedOut, stack.session.state, "calm sign-in state, no crash loop")
        assertNull(stack.client.tokenStore.accessToken)
        assertTrue(stack.jar.isEmpty(), "dead session clears the jar")
        assertEquals(
            "account/5",
            stack.session.pendingDestination,
            "destination survives expiry (AC1.3) for post-re-auth revalidation",
        )
        assertEquals("account/5", stack.session.consumePendingDestination(), "consume returns it exactly once")
        assertNull(stack.session.consumePendingDestination())
    }

    @Test
    fun `network failure during refresh is NOT treated as expiry`() {
        stack = startStack(folder)
        stack.server.enqueue(sessionResponse(accessToken = "access-1"))
        stack.session.login("a@example.com", "pw")
        stack.session.noteDestination("account/5")
        stack.server.enqueue(errorResponseBody(401, "Could not validate credentials", "invalid_token"))
        // The refresh endpoint never answers — the box went offline mid-flight.
        stack.server.shutdown()

        assertFailsWith<IOException> { stack.client.getJson("/accounts") }

        assertTrue(stack.session.state is AuthState.Authenticated, "an offline box is not evidence the session died")
        assertEquals("access-1", stack.client.tokenStore.accessToken)
        assertEquals("account/5", stack.session.pendingDestination)
    }

    @Test
    fun `production call-site contract - SessionDestinationPreserver passes NavHost routes through`() {
        // QA F1: the production wiring (FinTrackerApp → SessionDestinationPreserver
        // → AuthSessionManager) must preserve NavHost route strings. This test
        // pins that contract so a regression in noteDestination's validation
        // fails here, not silently in production.
        stack = startStack(folder)
        val preserver = SessionDestinationPreserver(stack.session)

        // List-level route.
        preserver.note("glance")
        assertEquals("glance", stack.session.pendingDestination, "NavHost route 'glance' preserved")
        assertEquals("glance", preserver.consume())
        assertNull(stack.session.pendingDestination)

        // Id route.
        preserver.note("account/5")
        assertEquals("account/5", stack.session.pendingDestination, "NavHost route 'account/5' preserved")
        assertEquals("account/5", preserver.consume())

        // Security guard still rejects external URLs.
        preserver.note("//evil.com/phish")
        assertNull(stack.session.pendingDestination, "protocol-relative URL rejected")
        preserver.note("https://evil.com")
        assertNull(stack.session.pendingDestination, "absolute URL rejected")
    }
}
