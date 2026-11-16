package com.margosmark.fintracker

import com.margosmark.fintracker.http.ApiClient
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue
import org.junit.After
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * docs/52 Option A / frontend/src/api/client.ts:76-110 — refresh must be
 * SINGLE-FLIGHT: the backend rotates one token per refresh, and the loser of
 * a concurrent race is taken for REUSE, which revokes the user's entire
 * session chain. Exactly ONE POST /auth/refresh may ever reach the wire for
 * one in-flight generation; every concurrent caller joins it.
 */
class SingleFlightRefreshTest {
    @get:Rule
    val folder = TemporaryFolder()

    private lateinit var stack: TestStack

    @After
    fun tearDown() {
        if (this::stack.isInitialized) stack.close()
    }

    @Test
    fun `concurrent refresh - exactly one wire refresh, losers join the winner`() {
        stack = startStack(folder)
        stack.server.enqueue(sessionResponse(accessToken = "access-1"))
        stack.authApi.login("a@example.com", "pw")
        // Only ONE refresh answer is enqueued: a second wire refresh would block on takeRequest.
        stack.server.enqueue(
            sessionResponse(
                accessToken = "access-2",
                setCookie = "refresh_token=rt-cookie-2; Path=/api/v1/auth; HttpOnly",
            ),
        )

        val results = runConcurrently(8) { stack.client.refreshTokens() }

        assertTrue(results.all { it is ApiClient.RefreshResult.Success }, "all callers share the winner: $results")
        assertEquals("access-2", stack.client.tokenStore.accessToken, "winner's token stored")

        val all = stack.server.recorded()
        val refreshCalls = all.to("/auth/refresh")
        assertEquals(1, refreshCalls.size, "exactly one wire refresh for 8 concurrent callers: ${all.map { it.path }}")
        // Cookie-only contract (docs/52 §1): the refresh carries the cookie, never a body/header token.
        val cookieHeader = refreshCalls[0].getHeader("Cookie") ?: ""
        assertTrue("refresh_token=rt-cookie-1" in cookieHeader, "refresh sent the jar's cookie: '$cookieHeader'")
        assertEquals(emptyList(), all.to("/auth/logout"), "a winning refresh must never trigger revocation traffic")
    }

    @Test
    fun `rejected refresh - all callers see Rejected, still exactly one wire refresh`() {
        stack = startStack(folder)
        stack.server.enqueue(sessionResponse(accessToken = "access-1"))
        stack.authApi.login("a@example.com", "pw")
        stack.server.enqueue(
            errorResponseBody(401, "Invalid or revoked refresh token", "invalid_or_revoked_refresh_token"),
        )

        val results = runConcurrently(8) { stack.client.refreshTokens() }

        assertTrue(results.all { it is ApiClient.RefreshResult.Rejected }, "losers never fire their own request: $results")
        val all = stack.server.recorded()
        assertEquals(1, all.to("/auth/refresh").size, "the 8 losers joined the single attempt: ${all.map { it.path }}")
        assertEquals(null, stack.client.tokenStore.accessToken, "dead session clears the access token")
    }

    @Test
    fun `rotation serializes across successive bursts`() {
        stack = startStack(folder)
        stack.server.enqueue(sessionResponse(accessToken = "access-1"))
        stack.authApi.login("a@example.com", "pw")
        repeat(3) { burst ->
            stack.server.enqueue(
                sessionResponse(
                    accessToken = "access-$burst",
                    setCookie = "refresh_token=rt-$burst; Path=/api/v1/auth; HttpOnly",
                ),
            )
            val results = runConcurrently(4) { stack.client.refreshTokens() }
            assertTrue(results.all { it is ApiClient.RefreshResult.Success })
        }
        assertEquals(3, stack.server.recorded().to("/auth/refresh").size, "one per burst, serialized")
    }

    private fun <T> runConcurrently(threads: Int, block: () -> T): List<T> {
        val pool = Executors.newFixedThreadPool(threads)
        try {
            val futures = (1..threads).map { pool.submit<T> { block() } }
            return futures.map { it.get(15, TimeUnit.SECONDS) }
        } finally {
            pool.shutdown()
        }
    }
}
