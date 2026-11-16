package com.margosmark.fintracker

import com.margosmark.fintracker.auth.LoginResult
import com.margosmark.fintracker.http.ApiException
import com.margosmark.fintracker.session.AuthState
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertIs
import kotlin.test.assertNull
import kotlin.test.assertTrue
import okhttp3.mockwebserver.MockResponse
import org.junit.After
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * docs/52 §1: a 200 from POST /auth/login that carries a TOTP challenge is
 * NOT an authenticated session — no cookie, no token, no state that a caller
 * could mistake for "logged in". Plus the wrong-code calm path.
 */
class TotpChallengeTest {
    @get:Rule
    val folder = TemporaryFolder()

    private lateinit var stack: TestStack

    @After
    fun tearDown() {
        if (this::stack.isInitialized) stack.close()
    }

    @Test
    fun `200 with totp challenge is NOT a session`() {
        stack = startStack(folder)
        stack.server.enqueue(MockResponse().setResponseCode(200).setBody(challengeResponseBody("challenge-1", 300)))

        val result = stack.session.login("a@example.com", "pw")

        assertIs<LoginResult.TotpChallenge>(result)
        assertEquals("challenge-1", result.challengeToken)
        assertEquals(AuthState.TotpRequired("challenge-1", 300), stack.session.state, "pending challenge, not Authenticated")
        assertNull(stack.client.tokenStore.accessToken, "no access token exists")
        assertTrue(stack.jar.isEmpty(), "no refresh cookie was set")
    }

    @Test
    fun `wrong totp code is calm - state and challenge survive`() {
        stack = startStack(folder)
        stack.server.enqueue(MockResponse().setResponseCode(200).setBody(challengeResponseBody("challenge-1", 300)))
        stack.session.login("a@example.com", "pw")
        stack.server.enqueue(errorResponseBody(401, "Invalid or expired code", "invalid_totp_code"))

        val error = assertFailsWith<ApiException> { stack.session.loginWithTotp("000000") }

        assertEquals("invalid_totp_code", error.code, "branch on the envelope code, not the copy")
        assertEquals(AuthState.TotpRequired("challenge-1", 300), stack.session.state, "challenge stays pending")
        assertNull(stack.client.tokenStore.accessToken)
    }

    @Test
    fun `correct totp code commits the session with purge before the authenticated state`() {
        val events = mutableListOf<String>()
        stack = startStack(folder) { events += "state:${it::class.simpleName}" }
        stack.server.enqueue(MockResponse().setResponseCode(200).setBody(challengeResponseBody("challenge-1", 300)))
        stack.session.login("a@example.com", "pw")
        stack.purgeRegistry.register("acct-store") { events += "purged" }
        stack.server.enqueue(sessionResponse(accessToken = "access-2", userId = 7, email = "a@example.com"))

        val result = stack.session.loginWithTotp("123456")

        assertIs<LoginResult.Authenticated>(result)
        assertEquals("access-2", stack.client.tokenStore.accessToken)
        assertEquals(AuthState.Authenticated(result.user), stack.session.state)
        assertEquals(
            listOf("state:TotpRequired", "purged", "state:Authenticated"),
            events.toList(),
            "purge completes inside commitSession BEFORE the new identity can render",
        )
    }
}
