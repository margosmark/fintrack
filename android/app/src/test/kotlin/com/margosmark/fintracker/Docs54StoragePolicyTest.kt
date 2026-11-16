package com.margosmark.fintracker

import com.margosmark.fintracker.http.ApiClient
import com.margosmark.fintracker.http.PersistentCookieJar
import com.margosmark.fintracker.session.AppContainer
import com.margosmark.fintracker.ui.PRODUCTION_API_BASE_URL
import java.io.File
import okhttp3.Cookie
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.mockwebserver.MockResponse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.assertThrows
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * docs/54 §1 storage-policy checks the JVM can prove: base-URL fail-closed
 * (S5/E12), the debuggable in-memory fallback seam (§1.4), auth-route cookie
 * origin (S6) and path scoping (S5). Keystore/AES-GCM (S2) and the
 * debuggable FLAG wiring (AppContainer) are runtime concerns — reviewed, and
 * covered on-device by the acceptance card, not here.
 */
class Docs54StoragePolicyTest {

    @get:Rule
    val folder = TemporaryFolder()

    @Test
    fun nonLoopbackPlaintextBaseUrlFailsClosedAtStartup() {
        val jar = PersistentCookieJar(File(folder.newFolder(), "c.enc"), TestCipher())
        // A real host over plaintext is refused before any request exists.
        assertThrows(IllegalArgumentException::class.java) {
            ApiClient("http://evil.example.com/api/v1", jar)
        }
        // The sanctioned local-stack exception: loopback/emulator aliases.
        ApiClient("http://10.0.2.2:8080/api/v1", jar).shutdown()
        ApiClient("http://127.0.0.1:8080/api/v1", jar).shutdown()
        // HTTPS anywhere is fine.
        ApiClient("https://api.example.com/api/v1", jar).shutdown()
    }

    @Test
    fun inMemoryModeNeverTouchesDiskAndStillHoldsTheSession() {
        val file = File(folder.newFolder(), "c.enc")
        file.writeText("stale-bytes-from-a-prior-release")
        // docs/54 §1.4: the debuggable-build behavior — fail closed on disk.
        val jar = PersistentCookieJar(file, TestCipher(), persistenceEnabled = false)
        assertFalse("leftover persisted credential must be deleted", file.exists())

        val url = "http://127.0.0.1/api/v1/auth/refresh".toHttpUrl()
        jar.saveFromResponse(url, listOf(refreshCookie()))
        assertEquals(1, jar.loadForRequest(url).size)
        assertFalse("in-memory mode must never write the file", file.exists())
    }

    @Test
    fun setCookieFromANonAuthRouteIsIgnoredEntirely() {
        val file = File(folder.newFolder(), "c.enc")
        val jar = PersistentCookieJar(file, TestCipher(), setCookiePathPrefix = "/api/v1/auth")
        // S6: only auth-route responses write the jar — memory AND disk.
        jar.saveFromResponse(
            "http://127.0.0.1/api/v1/accounts".toHttpUrl(),
            listOf(refreshCookie()),
        )
        assertTrue(jar.isEmpty())
        assertFalse(file.exists())
        // ...while the auth route's own Set-Cookie is kept.
        jar.saveFromResponse(
            "http://127.0.0.1/api/v1/auth/login".toHttpUrl(),
            listOf(refreshCookie()),
        )
        assertEquals(1, jar.storedCookieNames().size)
    }

    @Test
    fun refreshCookiePathScopingIsHonored() {
        // S5: the cookie exists only where its Path allows (docs/52 §Exact
        // cookie attributes: Path=/api/v1/auth).
        val jar = PersistentCookieJar(File(folder.newFolder(), "c.enc"), TestCipher())
        jar.saveFromResponse(
            "http://127.0.0.1/api/v1/auth/login".toHttpUrl(),
            listOf(refreshCookie()),
        )
        assertEquals(1, jar.loadForRequest("http://127.0.0.1/api/v1/auth/refresh".toHttpUrl()).size)
        assertTrue(jar.loadForRequest("http://127.0.0.1/api/v1/accounts".toHttpUrl()).isEmpty())
        assertTrue(jar.loadForRequest("http://evil.example.com/api/v1/auth/refresh".toHttpUrl()).isEmpty())
    }

    @Test
    fun refreshCookieNeverRidesAnOrdinaryApiCallOnTheWire() {
        // Same scoping proven through the real OkHttp client, jar-level and
        // wire-level together: after login, a domain GET carries NO Cookie
        // header at all (no refresh cookie in transit, ever).
        val stack = startStack(folder)
        try {
            stack.server.enqueue(sessionResponse())
            stack.server.enqueue(MockResponse().setResponseCode(200).setBody("{}"))
            stack.session.login("a@example.com", "pw")
            stack.client.getJson("/accounts")

            val requests = stack.server.recorded()
            assertEquals("/api/v1/accounts", requests.last().path)
            assertNull("domain call must not carry the refresh cookie", requests.last().getHeader("Cookie"))
            assertNull("login left with an empty jar — no cookie to send", requests.first().getHeader("Cookie"))
        } finally {
            stack.close()
        }
    }

    @Test
    fun productionBaseIsHttpsAndInjectedNotDefaulted() {
        // t_16001696: production wiring injects the deployed HTTPS base
        // EXPLICITLY (ui/AppScope.kt productionScope). It passes the S5/E12
        // fail-closed gate; the same host over plaintext is refused.
        assertEquals("https://fintrac.princessandmark.com/api/v1", PRODUCTION_API_BASE_URL)
        val jar = PersistentCookieJar(File(folder.newFolder(), "c.enc"), TestCipher())
        ApiClient(PRODUCTION_API_BASE_URL, jar).shutdown()
        assertThrows(IllegalArgumentException::class.java) {
            ApiClient("http://fintrac.princessandmark.com/api/v1", jar)
        }
        // Policy pin: the constructor DEFAULT stays the emulator/dev alias —
        // a production host must never become a default (security M4: no
        // build-config base-URL field).
        assertEquals("http://10.0.2.2:8080/api/v1", AppContainer.DEFAULT_API_BASE_URL)
        assertFalse(AppContainer.DEFAULT_API_BASE_URL.contains("fintrac"))
        assertFalse(AppContainer.DEFAULT_API_BASE_URL.startsWith("https://"))
    }

    @Test
    fun refreshCookieAgainstTheProductionBaseIsHttpsAndAuthPathOnly() {
        // docs/54 §1 S5 against the REAL production shape: refresh cookie is
        // `Secure; Path=/api/v1/auth` (backend/app/api/auth.py). Jar-level
        // send/drop is Cookie.matches — the same predicate OkHttp's wire path
        // uses; the Secure-over-plaintext drop at the jar seam is also pinned
        // in PersistentCookieJarTest.
        val jar = PersistentCookieJar(File(folder.newFolder(), "c.enc"), TestCipher())
        jar.saveFromResponse(
            "https://fintrac.princessandmark.com/api/v1/auth/login".toHttpUrl(),
            listOf(
                Cookie.Builder()
                    .name("refresh_token")
                    .value("rt-cookie-1")
                    .hostOnlyDomain("fintrac.princessandmark.com")
                    .path("/api/v1/auth")
                    .secure()
                    .build(),
            ),
        )
        // Sent: HTTPS, auth path, production host.
        assertEquals(
            1,
            jar.loadForRequest("https://fintrac.princessandmark.com/api/v1/auth/refresh".toHttpUrl()).size,
        )
        // Not sent: any other path on the host (path scoping).
        assertTrue(
            jar.loadForRequest("https://fintrac.princessandmark.com/api/v1/accounts".toHttpUrl()).isEmpty(),
        )
        // Not sent: plaintext to the same host — Secure never rides http (the
        // docs/52 L48 MCP trap).
        assertTrue(
            jar.loadForRequest("http://fintrac.princessandmark.com/api/v1/auth/refresh".toHttpUrl()).isEmpty(),
        )
        // Not sent: any other host.
        assertTrue(
            jar.loadForRequest("https://evil.example/api/v1/auth/refresh".toHttpUrl()).isEmpty(),
        )
    }

    private fun refreshCookie(): Cookie = Cookie.Builder()
        .name("refresh_token")
        .value("rt-cookie-1")
        .domain("127.0.0.1")
        .path("/api/v1/auth")
        .build()
}
