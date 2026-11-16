package com.margosmark.fintracker

import com.margosmark.fintracker.session.AuthState
import java.util.Collections
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.RecordedRequest
import org.junit.After
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * docs/52 §2 rec 3 / docs/53 AC3.1: switching identities must not silently
 * overwrite a still-live refresh cookie, and the previous identity's stores
 * are purged SYNCHRONOUSLY before any of the new identity's data can exist.
 *
 * The ordering is proven at the WIRE level via a server dispatcher that
 * records what the device looked like at the moment each login request was
 * served. The decisive pin: when B's login left the device, the jar and the
 * access-token store were already empty and the purge had already run.
 */
class IdentitySwitchPurgeOrderTest {
    @get:Rule
    val folder = TemporaryFolder()

    private lateinit var stack: TestStack

    @After
    fun tearDown() {
        if (this::stack.isInitialized) stack.close()
    }

    @Test
    fun `identity switch - purge completes and credentials are gone before the new login even leaves`() {
        val events = Collections.synchronizedList(mutableListOf<String>())
        stack = startStack(folder) { events += "state:${it::class.simpleName}" }
        stack.server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val path = request.path.orEmpty()
                return when {
                    path.endsWith("/auth/login") -> {
                        val email = Regex("\"email\":\"([^\"]+)\"").find(request.body.readUtf8())?.groupValues?.get(1)
                        events += "login-served:$email(jar=${stack.jar.storedCookieNames()},token=${stack.client.tokenStore.accessToken})"
                        if (email == "b@example.com") {
                            sessionResponse(
                                accessToken = "access-B",
                                userId = 2,
                                email = "b@example.com",
                                setCookie = "refresh_token=rt-B; Path=/api/v1/auth; HttpOnly",
                            )
                        } else {
                            sessionResponse(accessToken = "access-A", userId = 1, email = "a@example.com")
                        }
                    }
                    path.endsWith("/auth/logout") -> {
                        events += "logout-served"
                        MockResponse().setResponseCode(204)
                    }
                    else -> MockResponse().setResponseCode(404)
                }
            }
        }

        // Identity A: sign in; a store now holds A-derived data.
        stack.session.login("a@example.com", "pw")
        var aData: String? = "rows-of-A"
        stack.purgeRegistry.register("account-store") {
            events += "purge-account-store"
            aData = null
        }

        // Identity B: the plain login call performs the full switch.
        stack.session.login("b@example.com", "pw")

        val bUser = (stack.session.state as AuthState.Authenticated).user
        assertEquals("b@example.com", bUser.email, "B is the rendered identity")
        assertEquals(2, bUser.id)
        assertNull(aData, "A's data is gone")
        assertEquals("access-B", stack.client.tokenStore.accessToken)
        assertEquals(listOf("refresh_token"), stack.jar.storedCookieNames(), "B's cookie is the only one left")

        val log = events.toList()
        val aLogin = log.indexOf("login-served:a@example.com(jar=[],token=null)")
        val logoutServed = log.indexOf("logout-served")
        val purge = log.indexOf("purge-account-store")
        val signedOut = log.indexOf("state:SignedOut")
        val bLogin = log.indexOf("login-served:b@example.com(jar=[],token=null)")
        val authenticatedB = log.lastIndexOf("state:Authenticated")

        assertTrue(aLogin in 0 until logoutServed, "A signed in first: $log")
        assertTrue(logoutServed < purge, "server revocation attempt before purge: $log")
        assertTrue(purge < signedOut, "purge completes before SignedOut renders: $log")
        assertTrue(signedOut < bLogin, "B's login request leaves AFTER the purge: $log")
        assertTrue(bLogin < authenticatedB, "state:Authenticated(B) is last: $log")
        // The decisive pins, by value: at the wire moment BOTH logins left the
        // device — B's left with an empty jar and no token. A's live refresh
        // cookie was revoked and cleared, never silently overwritten.
        assertTrue(bLogin >= 0, "B's login was served: $log")
    }
}
