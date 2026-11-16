package com.margosmark.fintracker

import com.margosmark.fintracker.http.ApiException
import com.margosmark.fintracker.session.AuthState
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNull
import kotlin.test.assertTrue
import org.junit.After
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** docs/53 AC1.3 / card scope item 2: wrong credentials are a calm, typed failure — no refresh, no state change. */
class WrongCredentialsTest {
    @get:Rule
    val folder = TemporaryFolder()

    private lateinit var stack: TestStack

    @After
    fun tearDown() {
        if (this::stack.isInitialized) stack.close()
    }

    @Test
    fun `401 invalid_credentials - no session, no refresh attempt, jar untouched`() {
        stack = startStack(folder)
        stack.server.enqueue(errorResponseBody(401, "Invalid email or password", "invalid_credentials"))

        val error = assertFailsWith<ApiException> { stack.session.login("a@example.com", "wrong") }

        assertEquals(401, error.status)
        assertEquals("invalid_credentials", error.code)
        assertEquals(AuthState.SignedOut, stack.session.state, "a failed login changes no state")
        assertNull(stack.client.tokenStore.accessToken)
        assertTrue(stack.jar.isEmpty())
        val all = stack.server.recorded()
        assertEquals(1, all.size, "exactly the login attempt: ${all.map { it.path }}")
        assertEquals(emptyList(), all.to("/auth/refresh"), "a login 401 must never fall into the refresh dance (docs/29 F5c)")
    }
}
