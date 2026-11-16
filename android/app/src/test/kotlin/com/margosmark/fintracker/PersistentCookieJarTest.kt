package com.margosmark.fintracker

import com.margosmark.fintracker.http.PersistentCookieJar
import java.io.File
import kotlin.test.assertEquals
import kotlin.test.assertTrue
import okhttp3.Cookie
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * The persistence contract of the cookie store (docs/52 §2 rec 2):
 * sealed at rest, survives process restart, wiped on clear, fails closed on
 * corruption — and the Secure-over-plaintext rule a local dev stack must
 * honour is pinned here so nobody debugs it in production traffic later.
 */
class PersistentCookieJarTest {
    @get:Rule
    val folder = TemporaryFolder()

    private lateinit var server: MockWebServer

    @After
    fun tearDown() {
        if (this::server.isInitialized) runCatching { server.shutdown() }
    }

    private fun startServer(): MockWebServer {
        server = MockWebServer()
        server.start()
        return server
    }

    @Test
    fun `cookie survives a process restart and is served to the auth path`() {
        val srv = startServer()
        val storeFile = File(folder.newFolder(), "cookies.enc")
        val url = srv.url("/api/v1/auth/refresh")
        val cookie = Cookie.parse(url, REFRESH_COOKIE_VALUE)!!

        val firstRun = PersistentCookieJar(storeFile, TestCipher())
        firstRun.saveFromResponse(url, listOf(cookie))
        assertEquals(listOf("refresh_token"), firstRun.loadForRequest(url).map { it.name })

        // "Process restart": a brand-new jar over the same sealed file.
        val secondRun = PersistentCookieJar(storeFile, TestCipher())
        val served = secondRun.loadForRequest(url)
        assertEquals(listOf("refresh_token"), served.map { it.name })
        assertEquals("rt-cookie-1", served.single().value)
    }

    @Test
    fun `sealed at rest - the store file never contains the cookie value in plaintext`() {
        val srv = startServer()
        val storeFile = File(folder.newFolder(), "cookies.enc")
        val url = srv.url("/api/v1/auth/refresh")

        PersistentCookieJar(storeFile, TestCipher()).saveFromResponse(url, listOf(Cookie.parse(url, REFRESH_COOKIE_VALUE)!!))

        val bytes = storeFile.readBytes().toString(Charsets.ISO_8859_1)
        assertTrue("rt-cookie-1" !in bytes, "cookie value must be ciphertext on disk")
    }

    @Test
    fun `clear wipes memory and the sealed file`() {
        val srv = startServer()
        val storeFile = File(folder.newFolder(), "cookies.enc")
        val url = srv.url("/api/v1/auth/refresh")
        val jar = PersistentCookieJar(storeFile, TestCipher())
        jar.saveFromResponse(url, listOf(Cookie.parse(url, REFRESH_COOKIE_VALUE)!!))
        assertTrue(storeFile.exists())

        jar.clear()

        assertTrue(jar.isEmpty())
        assertTrue(!storeFile.exists(), "sealed store deleted")
        assertTrue(PersistentCookieJar(storeFile, TestCipher()).isEmpty(), "a fresh jar finds nothing")
    }

    @Test
    fun `an expired cookie removes the stored one`() {
        val srv = startServer()
        val url = srv.url("/api/v1/auth/refresh")
        val jar = PersistentCookieJar(File(folder.newFolder(), "cookies.enc"), TestCipher())
        jar.saveFromResponse(url, listOf(Cookie.parse(url, REFRESH_COOKIE_VALUE)!!))
        assertEquals(1, jar.storedCookieNames().size)

        jar.saveFromResponse(url, listOf(Cookie.parse(url, "refresh_token=; Path=/api/v1/auth; Max-Age=0")!!))

        assertTrue(jar.isEmpty(), "deletion semantics honored (server clears the cookie on logout)")
    }

    @Test
    fun `Secure cookie is stored but never sent over plaintext http - dev stacks need COOKIE_SECURE=false`() {
        val srv = startServer()
        val url = srv.url("/api/v1/auth/refresh")
        val jar = PersistentCookieJar(File(folder.newFolder(), "cookies.enc"), TestCipher())
        val secure = Cookie.parse(url, "$REFRESH_COOKIE_VALUE; Secure")!!

        jar.saveFromResponse(url, listOf(secure))

        assertEquals(listOf("refresh_token"), jar.storedCookieNames(), "stored faithfully")
        assertTrue(
            jar.loadForRequest(url).isEmpty(),
            "OkHttp refuses to send a Secure cookie over http — same trap as httpx (MCP client lesson)",
        )
    }

    @Test
    fun `corrupt store fails closed - file dropped, jar starts empty`() {
        val srv = startServer()
        val storeFile = File(folder.newFolder(), "cookies.enc")
        storeFile.writeBytes("not-ciphertext-at-all".toByteArray())

        val jar = PersistentCookieJar(storeFile, TestCipher())

        assertTrue(jar.isEmpty(), "no credential is recoverable from a corrupt store")
        assertTrue(!storeFile.exists(), "corrupt store deleted rather than reused")
    }
}
