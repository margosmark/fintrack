package com.margosmark.fintracker

import com.margosmark.fintracker.http.ApiClient
import com.margosmark.fintracker.session.AuthState
import com.margosmark.fintracker.session.LogoutOutcome
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.RecordedRequest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * docs/54 §2 R4 + §4.2 L1/L2: the logout-generation interlock. A refresh
 * that races a logout (or an identity switch) must never re-populate the jar
 * or the token store — a ghost cookie would resurrect the old session under
 * the new identity. These tests drive the REAL OkHttp stack with a blocking
 * dispatcher so the race is deterministic, not timing-dependent.
 */
class Docs54InterlockTest {

    @get:Rule
    val folder = TemporaryFolder()

    /**
     * L1: a refresh response that lands AFTER logout's generation bump is
     * discarded in full — token not stored, and the Set-Cookie that already
     * reached the jar during execute() is removed by exact value, never
     * touching a successor's rotation.
     */
    @Test
    fun lateRefreshResponseAfterLogoutIsDiscardedAndLeavesNoGhostCookie() {
        val stack = startStack(folder)
        try {
            val refreshStarted = CountDownLatch(1)
            val releaseRefresh = CountDownLatch(1)
            stack.server.dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse = when (request.path) {
                    "/api/v1/auth/login" -> sessionResponse()
                    "/api/v1/auth/refresh" -> {
                        refreshStarted.countDown()
                        releaseRefresh.await(5, TimeUnit.SECONDS)
                        sessionResponse(
                            accessToken = "late-access",
                            setCookie = "refresh_token=rt-late; Path=/api/v1/auth; HttpOnly",
                        )
                    }
                    else -> MockResponse().setResponseCode(404)
                }
            }
            stack.session.login("a@example.com", "pw")

            lateinit var result: ApiClient.RefreshResult
            val caller = thread {
                result = stack.client.refreshTokens()
            }
            assertTrue(refreshStarted.await(5, TimeUnit.SECONDS))

            // The exact interlock steps logoutSequence performs before clearing.
            stack.client.bumpSessionGeneration()
            stack.client.tokenStore.clear()
            stack.jar.clear()
            releaseRefresh.countDown()
            caller.join(5_000)
            assertTrue(!caller.isAlive)

            assertTrue("late refresh must be discarded, got $result", result is ApiClient.RefreshResult.Rejected)
            assertNull("token store must stay empty", stack.client.tokenStore.accessToken)
            assertTrue("jar must stay empty — no ghost cookie", stack.jar.isEmpty())
        } finally {
            stack.close()
        }
    }

    /**
     * L2/R4 end-to-end: logout issued WHILE a refresh is in flight cancels
     * the call, awaits, bumps the generation, revokes server-side, clears and
     * purges — and the final state is clean even though the server still had
     * a blocked refresh response waiting to be released.
     */
    @Test
    fun logoutDuringInFlightRefreshEndsInCleanSignedOutState() {
        val stack = startStack(folder)
        try {
            val refreshStarted = CountDownLatch(1)
            val releaseRefresh = CountDownLatch(1)
            stack.server.dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse = when (request.path) {
                    "/api/v1/auth/login" -> sessionResponse()
                    "/api/v1/auth/refresh" -> {
                        refreshStarted.countDown()
                        releaseRefresh.await(5, TimeUnit.SECONDS)
                        sessionResponse(
                            accessToken = "late-access",
                            setCookie = "refresh_token=rt-late; Path=/api/v1/auth; HttpOnly",
                        )
                    }
                    "/api/v1/auth/logout" -> MockResponse().setResponseCode(204)
                    else -> MockResponse().setResponseCode(404)
                }
            }
            stack.session.login("a@example.com", "pw")

            val caller = thread { stack.client.refreshTokens() }
            assertTrue(refreshStarted.await(5, TimeUnit.SECONDS))

            val outcome = stack.session.logout()
            releaseRefresh.countDown()
            caller.join(5_000)
            assertTrue(!caller.isAlive)

            assertEquals(LogoutOutcome.SERVER_REVOKED, outcome)
            assertTrue(stack.session.state is AuthState.SignedOut)
            assertNull(stack.client.tokenStore.accessToken)
            assertTrue(stack.jar.isEmpty())
        } finally {
            stack.close()
        }
    }
}
