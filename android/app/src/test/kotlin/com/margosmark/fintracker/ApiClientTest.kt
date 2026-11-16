package com.margosmark.fintracker

import com.margosmark.fintracker.http.ApiClient
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue
import okhttp3.mockwebserver.MockResponse
import org.junit.After
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * A background refresh failure must never escape the refresh pool thread
 * (t_6781e4c5). The caller is blocked in `future.get()` — its try/catch
 * sits on ITS thread — so anything thrown inside the executor runnable is
 * uncaught, and Android's uncaught-exception handler kills the PROCESS.
 * That is exactly how the missing INTERNET permission's `SecurityException`
 * took the app down 2.1 s after launch, before any UI rendered. Every
 * failure — `IOException` or not — must map to [ApiClient.RefreshResult.Unreachable]
 * ("no answer", not a verdict on the session), and the single-flight slot
 * must be freed for the next attempt.
 */
class ApiClientTest {
    @get:Rule
    val folder = TemporaryFolder()

    private lateinit var stack: TestStack
    private val escaped = CopyOnWriteArrayList<Throwable>()

    @After
    fun tearDown() {
        if (this::stack.isInitialized) stack.close()
    }

    /** A refresh pool whose threads RECORD an uncaught throwable instead of dying with it. */
    private fun recordingExecutor(): ExecutorService =
        Executors.newSingleThreadExecutor { runnable ->
            Thread(runnable, "fintracker-api-test").apply {
                setUncaughtExceptionHandler { _, t -> escaped += t }
            }
        }

    /**
     * `refreshTokens()` blocks in `future.get()` with no timeout; a refresh
     * attempt whose future is never completed (the pre-fix shape) would hang
     * the test forever, so every call goes through a bounded wait.
     */
    private fun refreshBounded(): ApiClient.RefreshResult {
        val pool = Executors.newSingleThreadExecutor()
        try {
            return pool.submit<ApiClient.RefreshResult> { stack.client.refreshTokens() }
                .get(10, TimeUnit.SECONDS)
        } finally {
            pool.shutdown()
        }
    }

    @Test
    fun `non-IO failure on the pool thread maps to Unreachable and never escapes`() {
        stack = startStack(folder, executor = recordingExecutor())
        stack.server.enqueue(sessionResponse(accessToken = "access-1"))
        stack.authApi.login("a@example.com", "pw")
        // 200 whose body carries an access_token but no `user` object:
        // hasAccessToken() passes, tokenResponse() throws JSONException — a
        // NON-IO failure out of doRefresh(), the same code shape as the boot
        // crash's SecurityException (which no JVM test can produce literally:
        // the JVM has no install-time permission model).
        stack.server.enqueue(MockResponse().setResponseCode(200).setBody("""{"access_token":"x"}"""))

        val result = refreshBounded()

        assertIs<ApiClient.RefreshResult.Unreachable>(
            result,
            "a non-IO failure is 'no answer', never a session verdict: $result",
        )
        assertTrue(escaped.isEmpty(), "nothing may escape the refresh pool thread: $escaped")

        // The single-flight slot must be free again: the NEXT refresh runs a
        // fresh wire call instead of joining a future that never completes.
        stack.server.enqueue(sessionResponse(accessToken = "access-2"))
        val second = refreshBounded()
        assertIs<ApiClient.RefreshResult.Success>(second, "slot released after a failed attempt: $second")
        assertEquals("access-2", stack.client.tokenStore.accessToken)
    }

    @Test
    fun `unreachable API base maps to Unreachable and never escapes`() {
        stack = startStack(folder, executor = recordingExecutor())
        stack.server.enqueue(sessionResponse(accessToken = "access-1"))
        stack.authApi.login("a@example.com", "pw")
        stack.server.shutdown() // connection refused -> IOException

        val result = refreshBounded()

        assertIs<ApiClient.RefreshResult.Unreachable>(result, "offline is not a session verdict: $result")
        assertTrue(escaped.isEmpty(), "nothing may escape the refresh pool thread: $escaped")
    }
}
