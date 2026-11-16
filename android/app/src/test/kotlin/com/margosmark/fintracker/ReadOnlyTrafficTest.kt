package com.margosmark.fintracker

import com.margosmark.fintracker.http.ReadOnlyViolationException
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue
import okhttp3.mockwebserver.MockResponse
import org.junit.After
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * docs/53 AC1.2 / docs/52 §2 rec 4: Android v1 is READ-ONLY. Zero domain
 * POST/PUT/PATCH/DELETE — the only non-GET traffic is the auth/session
 * bookkeeping docs/52 documents. Two channels:
 *
 *  1. Recorded-traffic assertion over a full walkthrough (login, reads,
 *     refresh, logout): every request on the wire is GET or an allowlisted
 *     auth POST.
 *  2. Direct violation probes: a domain write throws BEFORE the socket and
 *     the server's request count does not move.
 */
class ReadOnlyTrafficTest {
    @get:Rule
    val folder = TemporaryFolder()

    private lateinit var stack: TestStack

    @After
    fun tearDown() {
        if (this::stack.isInitialized) stack.close()
    }

    @Test
    fun `full walkthrough records zero domain writes`() {
        stack = startStack(folder)
        val okJson = MockResponse().setResponseCode(200).setBody("""{"items":[],"total":0}""")
        stack.server.enqueue(sessionResponse(accessToken = "access-1")) // login
        stack.server.enqueue(okJson.clone().setBody("""{"items":[{"id":1,"name":"Checking"}],"total":1}""")) // GET /accounts
        stack.server.enqueue(okJson.clone()) // GET /portfolio/positions
        stack.server.enqueue(okJson.clone()) // GET /transactions
        stack.server.enqueue(
            sessionResponse(
                accessToken = "access-2",
                setCookie = "refresh_token=rt-cookie-2; Path=/api/v1/auth; HttpOnly",
            ),
        ) // refresh
        stack.server.enqueue(okJson.clone()) // replayed/authed read
        stack.server.enqueue(okhttp3.mockwebserver.MockResponse().setResponseCode(204)) // logout

        stack.session.login("a@example.com", "pw")
        stack.client.getJson("/accounts")
        stack.client.getJson("/portfolio/positions")
        stack.client.getJson("/transactions")
        assertTrue(stack.client.refreshTokens() is com.margosmark.fintracker.http.ApiClient.RefreshResult.Success)
        stack.client.getJson("/users/me")
        stack.session.logout()

        val all = stack.server.recorded()
        assertEquals(7, all.size, "expected exactly the walkthrough's requests: ${all.map { it.method + " " + it.path }}")
        val allowlistedPosts = setOf(
            "/api/v1/auth/login",
            "/api/v1/auth/refresh",
            "/api/v1/auth/logout",
        )
        for (request in all) {
            val ok = request.method == "GET" || (request.method == "POST" && request.path in allowlistedPosts)
            assertTrue(ok, "non-GET traffic outside the auth allowlist on the wire: ${request.method} ${request.path}")
        }
    }

    @Test
    fun `domain writes are blocked before any socket and never reach the wire`() {
        stack = startStack(folder)
        val before = stack.server.requestCount

        assertFailsWith<ReadOnlyViolationException> {
            stack.client.execute(stack.client.request("POST", "/accounts", """{"name":"Evil"}"""))
        }
        assertFailsWith<ReadOnlyViolationException> {
            stack.client.execute(stack.client.request("PUT", "/accounts/1", """{"name":"Renamed"}"""))
        }
        assertFailsWith<ReadOnlyViolationException> {
            stack.client.execute(stack.client.request("PATCH", "/users/me", """{"privacy_mode":false}"""))
        }
        assertFailsWith<ReadOnlyViolationException> {
            stack.client.execute(stack.client.request("DELETE", "/accounts/1"))
        }
        // A suffix-guess cannot smuggle a write under the allowlist either.
        assertFailsWith<ReadOnlyViolationException> {
            stack.client.execute(stack.client.request("POST", "/evil/auth/login", "{}"))
        }

        assertEquals(before, stack.server.requestCount, "not one blocked attempt reached the wire")
    }

    @Test
    fun `allowlist covers exactly the docs-52 auth routes`() {
        stack = startStack(folder)
        val allowed = listOf("/auth/login", "/auth/login/totp", "/auth/demo", "/auth/refresh", "/auth/logout")
        for (path in allowed) {
            // Blocked-vs-allowed is decided by the guard; enqueue anything so
            // the call has somewhere to go if (wrongly) allowed.
            stack.server.enqueue(MockResponse().setResponseCode(401).setBody("{}"))
            stack.client.execute(stack.client.request("POST", path, "{}")).close()
        }
        val blocked = listOf(
            "/auth/register",
            "/auth/totp/setup",
            "/accounts",
            "/trades",
            "/import/batches/1/commit",
            "/acb/rebuild",
        )
        for (path in blocked) {
            assertFailsWith<ReadOnlyViolationException>("POST $path must be blocked") {
                stack.client.execute(stack.client.request("POST", path, "{}"))
            }
        }
        assertEquals(allowed.size, stack.server.requestCount, "only the allowlist reached the wire")
    }
}
