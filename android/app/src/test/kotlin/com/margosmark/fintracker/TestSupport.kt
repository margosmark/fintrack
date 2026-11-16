package com.margosmark.fintracker

import com.margosmark.fintracker.auth.AuthApi
import com.margosmark.fintracker.http.ApiClient
import com.margosmark.fintracker.http.PayloadCipher
import com.margosmark.fintracker.http.PersistentCookieJar
import com.margosmark.fintracker.session.AuthState
import com.margosmark.fintracker.session.AuthSessionManager
import com.margosmark.fintracker.session.PurgeRegistry
import java.io.File
import java.util.concurrent.ExecutorService
import java.util.concurrent.TimeUnit
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.rules.TemporaryFolder

/**
 * Shared MockWebServer scaffolding. All tests run against the REAL OkHttp
 * stack (cookie jar, interceptors, single-flight executor) with only the
 * server faked — the wire is where the docs/52 contracts live.
 */

/** Reversible toy cipher: proves persistence bytes ≠ plaintext without needing a Keystore on the JVM. */
class TestCipher : PayloadCipher {
    override fun encrypt(plaintext: ByteArray): ByteArray =
        ByteArray(plaintext.size) { i -> (plaintext[i].toInt() xor 0x5A).toByte() }

    override fun decrypt(ciphertext: ByteArray): ByteArray = encrypt(ciphertext) // symmetric XOR

    override fun destroy() { /* no Keystore on the JVM — destruction is a runtime concern */ }
}

/** The backend's exact cookie shape (docs/52 §Exact cookie attributes), minus Secure for the plaintext test server. */
const val REFRESH_COOKIE_VALUE = "refresh_token=rt-cookie-1; Path=/api/v1/auth; HttpOnly"

fun tokenResponseBody(accessToken: String = "access-1", userId: Int = 1, email: String = "a@example.com"): String = """
    {"user":{"id":$userId,"email":"$email","name":null,"privacy_mode":false,
     "currency_tracker_enabled":true,"llm_provider":"none","llm_base_url":null,"llm_model":null,
     "encryption_required":false,"dashboard_preset":"overview","saved_report_views":[],
     "recurring_intent_tag_ids":[]},
     "access_token":"$accessToken","token_type":"bearer"}
""".trimIndent()

fun challengeResponseBody(challengeToken: String = "challenge-1", expiresIn: Int = 300): String =
    """{"challenge":"totp","challenge_token":"$challengeToken","expires_in":$expiresIn}"""

fun errorResponseBody(status: Int, detail: String, code: String): MockResponse =
    MockResponse().setResponseCode(status).setBody("""{"detail":"$detail","code":"$code"}""")

/** A 200 that issues a session: access token in the body + refresh cookie (docs/52 §1). */
fun sessionResponse(
    accessToken: String = "access-1",
    userId: Int = 1,
    email: String = "a@example.com",
    setCookie: String = REFRESH_COOKIE_VALUE,
): MockResponse = MockResponse()
    .setResponseCode(200)
    .setHeader("Set-Cookie", setCookie)
    .setBody(tokenResponseBody(accessToken, userId, email))

class TestStack(
    val server: MockWebServer,
    val storeFile: File,
    val jar: PersistentCookieJar,
    val client: ApiClient,
    val authApi: AuthApi,
    val purgeRegistry: PurgeRegistry,
    val session: AuthSessionManager,
) {
    fun close() {
        client.shutdown()
        runCatching { server.shutdown() }
    }
}

/**
 * @param executor overrides ApiClient's refresh pool. Tests that pin the
 *   pool-thread contract (an exception may never escape it — t_6781e4c5)
 *   pass a recording executor so an escaping throwable is captured instead
 *   of dying with the JVM's default handler.
 */
fun startStack(
    folder: TemporaryFolder,
    executor: ExecutorService? = null,
    listener: (AuthState) -> Unit = {},
): TestStack {
    val server = MockWebServer()
    server.start()
    val storeFile = File(folder.newFolder(), "cookies.enc")
    // Mirror AppContainer's production wiring: auth-route origin filter ON (docs/54 S6).
    val jar = PersistentCookieJar(storeFile, TestCipher(), setCookiePathPrefix = "/api/v1/auth")
    val baseUrl = server.url("/api/v1").toString()
    val client = if (executor != null) {
        ApiClient(baseUrl, cookieJar = jar, executor = executor)
    } else {
        ApiClient(baseUrl, cookieJar = jar)
    }
    val authApi = AuthApi(client)
    val purgeRegistry = PurgeRegistry()
    val session = AuthSessionManager(authApi, client, purgeRegistry, listener)
    return TestStack(server, storeFile, jar, client, authApi, purgeRegistry, session)
}

/** Every request the server has received so far, in arrival order. */
fun MockWebServer.recorded(): List<RecordedRequest> {
    val out = mutableListOf<RecordedRequest>()
    while (out.size < requestCount) {
        out += takeRequest(200, TimeUnit.MILLISECONDS) ?: break
    }
    return out
}

fun List<RecordedRequest>.to(suffix: String): List<RecordedRequest> =
    filter { it.path?.substringBefore("?")?.endsWith(suffix) == true }
