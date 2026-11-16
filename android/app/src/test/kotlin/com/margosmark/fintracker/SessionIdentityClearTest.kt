package com.margosmark.fintracker

import com.margosmark.fintracker.http.ApiException
import com.margosmark.fintracker.session.AuthState
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNull
import kotlin.test.assertTrue
import okhttp3.mockwebserver.MockResponse
import org.junit.After
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * Security S1 (round 3): a preserved destination is IDENTITY-scoped.
 *
 * Expiry KEEPS the destination — that is docs/53 AC1.3's same-identity
 * revalidation path — but `commitSession` hard-clears it the moment a
 * DIFFERENT user id commits, so B never consumes (or even sees) A's route.
 * The clear is keyed on the committing user id, not on the flow shape:
 *
 *  - expiry → same-identity re-auth: destination SURVIVES (AC1.3);
 *  - expiry → different-identity login: destination CLEARED at commit;
 *  - live identity switch (no expiry): destination CLEARED at commit;
 *  - cold start: a deep-link destination noted before anyone signed in
 *    survives the FIRST sign-in (no prior identity to be foreign to) and is
 *    revalidated by the deep-link existence/tenant gate instead (AC3.2,
 *    pinned in `DestinationRound3Test`).
 */
class SessionIdentityClearTest {
    @get:Rule
    val folder = TemporaryFolder()

    private lateinit var stack: TestStack

    @After
    fun tearDown() {
        if (this::stack.isInitialized) stack.close()
    }

    /** Drive a provably dead session: domain GET 401 → refresh rejected. */
    private fun expireSession() {
        stack.server.enqueue(errorResponseBody(401, "Could not validate credentials", "invalid_token"))
        stack.server.enqueue(
            errorResponseBody(401, "Invalid or revoked refresh token", "invalid_or_revoked_refresh_token"),
        )
        assertFailsWith<ApiException> { stack.client.getJson("/accounts") }
        assertEquals(AuthState.SignedOut, stack.session.state, "expiry lands calmly on SignedOut")
    }

    @Test
    fun `expiry then same-identity re-auth keeps the destination (AC1_3)`() {
        stack = startStack(folder)
        stack.server.enqueue(sessionResponse(accessToken = "access-1", userId = 1, email = "a@example.com"))
        stack.session.login("a@example.com", "pw")
        stack.session.noteDestination("account/5")
        expireSession()
        assertEquals(
            "account/5",
            stack.session.pendingDestination,
            "expiry keeps the destination for same-identity revalidation (AC1.3)",
        )

        // Same user id signs back in: the destination must survive the commit.
        stack.server.enqueue(sessionResponse(accessToken = "access-2", userId = 1, email = "a@example.com"))
        stack.session.login("a@example.com", "pw")
        val user = (stack.session.state as AuthState.Authenticated).user
        assertEquals(1, user.id, "same identity committed")
        assertEquals(
            "account/5",
            stack.session.pendingDestination,
            "same-identity re-auth keeps the destination (AC1.3 revalidation)",
        )
    }

    @Test
    fun `expiry then different-identity login hard-clears the destination at commit (S1)`() {
        stack = startStack(folder)
        stack.server.enqueue(sessionResponse(accessToken = "access-1", userId = 1, email = "a@example.com"))
        stack.session.login("a@example.com", "pw")
        stack.session.noteDestination("account/5")
        expireSession()
        assertEquals("account/5", stack.session.pendingDestination, "expiry keeps A's destination")

        // B (a DIFFERENT user id) logs in: commit must drop A's route.
        stack.server.enqueue(sessionResponse(accessToken = "access-B", userId = 2, email = "b@example.com"))
        stack.session.login("b@example.com", "pw")
        val bUser = (stack.session.state as AuthState.Authenticated).user
        assertEquals(2, bUser.id, "B is the rendered identity")
        assertNull(
            stack.session.pendingDestination,
            "identity change hard-clears A's preserved destination (security S1)",
        )
        assertNull(stack.session.consumePendingDestination(), "nothing for B to consume — no navigation to A's route")
    }

    @Test
    fun `live identity switch without expiry also clears the destination (S1)`() {
        stack = startStack(folder)
        stack.server.enqueue(sessionResponse(accessToken = "access-1", userId = 1, email = "a@example.com"))
        stack.session.login("a@example.com", "pw")
        stack.session.noteDestination("account/5")

        // B logs in while A's session is still live: the full switch sequence
        // (server revocation attempt → jar/token clear → purge) then commits
        // B, which must drop A's destination.
        stack.server.enqueue(MockResponse().setResponseCode(204))
        stack.server.enqueue(sessionResponse(accessToken = "access-B", userId = 2, email = "b@example.com"))
        stack.session.login("b@example.com", "pw")
        assertEquals(2, (stack.session.state as AuthState.Authenticated).user.id)
        assertNull(stack.session.pendingDestination, "identity change clears the destination even without expiry")
    }

    @Test
    fun `cold-start destination survives the FIRST sign-in (no prior identity)`() {
        stack = startStack(folder)
        // Noted before anyone signed in — the signed-out deep-link flow
        // (FinTrackerApp) does exactly this; there is no prior identity for
        // it to be foreign to, so the FIRST commit keeps it and the deep-link
        // existence/tenant gate revalidates it (AC3.2).
        stack.session.noteDestination("account/5")
        stack.server.enqueue(sessionResponse(accessToken = "access-1", userId = 1, email = "a@example.com"))
        stack.session.login("a@example.com", "pw")
        assertEquals(
            "account/5",
            stack.session.pendingDestination,
            "first sign-in of a process keeps the deep-link destination; the AC3.2 gate revalidates it",
        )
        assertTrue(stack.session.pendingDestination!!.startsWith("account/"), "internal route only")
    }

    @Test
    fun `explicit logout still drops the destination regardless of identity`() {
        stack = startStack(folder)
        stack.server.enqueue(sessionResponse(accessToken = "access-1", userId = 1, email = "a@example.com"))
        stack.session.login("a@example.com", "pw")
        stack.session.noteDestination("account/5")
        stack.server.enqueue(MockResponse().setResponseCode(204))
        stack.session.logout()
        assertNull(stack.session.pendingDestination, "explicit sign-out drops the destination (unchanged contract)")
    }
}
