package com.margosmark.fintracker.http

import java.io.IOException
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response

/**
 * The one HTTP client the Android app talks to the backend through
 * (docs/52 Option A).
 *
 * Wiring:
 *  - [PersistentCookieJar] carries the server's cookie-only `refresh_token`
 *    flow to `/auth/refresh` and `/auth/logout` (the backend reads ONLY the
 *    cookie — a body/header token is rejected by design, docs/52 §1).
 *  - [AuthTokenStore] holds the 15-minute access JWT in process memory;
 *    [AuthorizationInterceptor] attaches it to every request.
 *  - [ReadOnlyCallGuard] makes the whole client read-only outside auth.
 *
 * Threading: every method BLOCKS. Call from a background thread (the UI card
 * decides how); no coroutines dependency was added on purpose — the client
 * uses plain `java.util.concurrent`.
 *
 * Refresh is SINGLE-FLIGHT ([refreshTokens]): the backend rotates one token
 * per refresh and takes the loser of a concurrent race for REUSE, which
 * revokes the user's entire session chain — the exact defect the SPA's
 * `pendingRefresh` exists to prevent (frontend/src/api/client.ts:76-110).
 * Every refresh caller joins one in-flight [CompletableFuture]; exactly one
 * POST ever reaches the wire. Pinned by `SingleFlightRefreshTest`.
 *
 * Base URL: injected, never hardcoded here. Tests pass a MockWebServer URL;
 * the app constructs [ApiClient] through [com.margosmark.fintracker.session.AppContainer],
 * which takes the base URL as a constructor parameter (emulator loopback is
 * the default; a production base is passed explicitly at wiring time).
 */
class ApiClient(
    baseUrl: String,
    val cookieJar: PersistentCookieJar,
    val tokenStore: AuthTokenStore = AuthTokenStore(),
    private val executor: ExecutorService = Executors.newCachedThreadPool { runnable ->
        Thread(runnable, "fintracker-api-${threadIndex.getAndIncrement()}").apply { isDaemon = true }
    },
) {
    val baseUrl: HttpUrl = baseUrl.toHttpUrl()

    init {
        // docs/54 §1.2 S5 / §3 E12: a non-HTTPS base URL is a misconfiguration
        // and must fail closed at startup. The sanctioned exception is the
        // LOCAL dev stack (card scope item 6: tests/dev default to a local
        // stack; docs/53 probes run against local/dev) — and only a loopback
        // or emulator-host alias may use it. A plaintext base pointing at any
        // real host is refused here, before a single request (or cookie) can
        // be produced. E12's TLS is otherwise platform-default: no custom
        // TrustManager, no pinning hooks, no OkHttp disk cache (E2: auth
        // responses are never cached to disk).
        val plaintext = this.baseUrl.scheme != "https"
        require(!plaintext || isLoopbackHost(this.baseUrl.host)) {
            "refusing non-HTTPS base URL for non-loopback host '${this.baseUrl.host}' (docs/54 S5/E12)"
        }
    }

    /** Path prefix of the API, e.g. `/api/v1` — the read-only guard's yardstick. */
    private val basePath: String = this.baseUrl.encodedPath.trimEnd('/')

    val http: OkHttpClient = OkHttpClient.Builder()
        .cookieJar(cookieJar)
        .addInterceptor(AuthorizationInterceptor(tokenStore))
        .addInterceptor(ReadOnlyCallGuard(basePath))
        .build()

    /**
     * Invoked when the session is PROVABLY dead: the server answered 401 to
     * both the request and the follow-up refresh, or the refresh itself was
     * rejected. NOT invoked on a network failure — an offline box is not
     * evidence the session died (docs/53 AC1.3 wants expiry to be a real
     * expiry; the SPA cannot tell these apart, native can).
     */
    var sessionExpiredListener: (() -> Unit)? = null

    private val refreshLock = Any()
    private var pendingRefresh: CompletableFuture<RefreshResult>? = null

    /**
     * Session generation (docs/54 §2 R4, §4.2 L1). Logout bumps it BEFORE
     * clearing; a refresh response that lands after the bump is DISCARDED —
     * neither its access token is stored nor its Set-Cookie kept — so a
     * refresh racing logout can never re-populate the jar with a ghost
     * session that survives the identity switch.
     */
    private val sessionGeneration = java.util.concurrent.atomic.AtomicLong(0)

    fun bumpSessionGeneration(): Long = sessionGeneration.incrementAndGet()

    /** Relative path (e.g. `/accounts`) → absolute URL. */
    fun url(path: String): HttpUrl {
        val resolved = baseUrl.resolve(path)
        requireNotNull(resolved) { "unresolvable path: $path" }
        return resolved
    }

    fun request(method: String, path: String, jsonBody: String? = null): Request {
        val builder = Request.Builder().url(url(basePath + path))
        when (method) {
            "GET", "HEAD", "OPTIONS" -> builder.method(method, null)
            else -> builder.method(method, (jsonBody ?: "{}").toRequestBody(JSON_MEDIA_TYPE))
        }
        return builder.build()
    }

    /** Blocking execute. Caller handles/closes the response (use `.use {}`). */
    fun execute(request: Request): Response = http.newCall(request).execute()

    // --- single-flight refresh --------------------------------------------

    /** What a refresh attempt learned. */
    sealed class RefreshResult {
        data class Success(val user: UserPublic, val accessToken: String) : RefreshResult()

        /** The server rejected the cookie — the session is dead. */
        data object Rejected : RefreshResult()

        /** No answer (IOException) — NOT evidence the session died. */
        data object Unreachable : RefreshResult()
    }

    /**
     * POST /auth/refresh through the ONE in-flight attempt (cookie-only —
     * no Authorization header, no body token, mirroring the SPA).
     *
     * Every concurrent caller gets the same [RefreshResult]; exactly one
     * request is sent. A loser that would previously have raced the winner's
     * cookie (and been taken for reuse, revoking the whole chain) simply
     * joins the winner's future here.
     */
    fun refreshTokens(): RefreshResult {
        val future: CompletableFuture<RefreshResult>
        synchronized(refreshLock) {
            val existing = pendingRefresh
            if (existing != null) {
                future = existing
            } else {
                future = CompletableFuture()
                pendingRefresh = future
                executor.execute {
                    // The caller is blocked in future.get() — its try/catch
                    // sits on ITS thread, so anything thrown HERE is uncaught,
                    // and Android's uncaught-exception handler kills the
                    // PROCESS. That is exactly how the missing INTERNET
                    // permission's SecurityException took the app down 2.1 s
                    // after launch (t_6781e4c5). A background refresh must
                    // never be able to do that: every failure — IOException
                    // or not — maps to Unreachable ("no answer", not a
                    // verdict on the session), and the single-flight slot is
                    // always freed so the next attempt runs fresh.
                    val result = try {
                        doRefresh()
                    } catch (t: Throwable) {
                        if (t !is IOException) {
                            // Non-IO failures are unexpected: surface them in
                            // logcat (System.err → E/System.err) without a
                            // logging dependency — but never rethrow.
                            System.err.println(
                                "ApiClient: background refresh failed with ${t.javaClass.name}: ${t.message}",
                            )
                            t.printStackTrace(System.err)
                        }
                        RefreshResult.Unreachable
                    }
                    synchronized(refreshLock) {
                        if (pendingRefresh === future) pendingRefresh = null
                    }
                    future.complete(result)
                }
            }
        }
        return future.get()
    }

    private fun doRefresh(): RefreshResult {
        // docs/54 R4/L1: the generation this refresh BELONGS TO. Anything that
        // moves the session on (logout, identity switch) bumps it meanwhile.
        val generation = sessionGeneration.get()
        execute(request("POST", "/auth/refresh")).use { response ->
            val body = response.body?.string()
            if (!response.isSuccessful) {
                return when {
                    response.code == 401 -> {
                        tokenStore.clear()
                        RefreshResult.Rejected
                    }
                    // 5xx / 429 are transient — not verdicts on the session.
                    else -> RefreshResult.Unreachable
                }
            }
            if (!ApiJson.hasAccessToken(body)) {
                tokenStore.clear()
                return RefreshResult.Rejected
            }
            val pair = ApiJson.tokenResponse(requireNotNull(body))
            if (generation != sessionGeneration.get()) {
                // LATE RESPONSE — discard both halves. The Set-Cookie already
                // landed in the jar during execute(); remove exactly the value
                // this response carried, never a successor's rotation.
                tokenStore.clear()
                response.header("Set-Cookie")?.let { header ->
                    val value = header.substringAfter("refresh_token=", "").substringBefore(";")
                    if (value.isNotEmpty()) cookieJar.removeIfValue("refresh_token", value)
                }
                return RefreshResult.Rejected
            }
            tokenStore.set(pair.accessToken)
            return RefreshResult.Success(pair.user, pair.accessToken)
        }
    }

    // --- authenticated GET with the SPA's 401 dance ------------------------

    /**
     * GET [path] with the frontend's 401 → single-flight refresh → replay →
     * session-expired dance (frontend/src/api/client.ts `withAuthRetry`).
     *
     * On a provably dead session this clears the access token, fires
     * [sessionExpiredListener] exactly once per failed dance, and throws
     * `ApiException(401, "invalid_token", "Session expired")` — the UI layer
     * renders the calm sign-in screen and preserves the intended destination
     * (docs/53 AC1.3).
     */
    fun getJson(path: String): String {
        execute(request("GET", path)).use { first ->
            if (first.code != 401) {
                return requireSuccess(first, first.body?.string())
            }
        }
        when (refreshTokens()) {
            is RefreshResult.Success -> Unit
            RefreshResult.Rejected -> {
                expireSession()
                throw sessionExpiredException()
            }
            RefreshResult.Unreachable -> throw IOException("refresh unreachable for GET $path")
        }
        execute(request("GET", path)).use { replayed ->
            if (replayed.code == 401) {
                expireSession()
                throw sessionExpiredException()
            }
            return requireSuccess(replayed, replayed.body?.string())
        }
    }

    /**
     * Stops in-flight calls and WAITS for them to wind down (logout/identity
     * switch step 1 — docs/52 §2 rec 3, made concrete by docs/54 §4.2 L2:
     * a response applied after the purge re-creates the old tenant's cache).
     */
    fun cancelInFlight(timeoutMs: Long = 2_000) {
        http.dispatcher.cancelAll()
        val deadline = System.currentTimeMillis() + timeoutMs
        while (
            (http.dispatcher.runningCallsCount() + http.dispatcher.queuedCallsCount()) > 0 &&
            System.currentTimeMillis() < deadline
        ) {
            Thread.sleep(10)
        }
    }

    /**
     * Bounded wait for a refresh already in flight (docs/54 §2 R4: logout
     * "acquires the refresh lock or awaits the in-flight refresh"). Bounded
     * because the generation check below covers a refresh that outlives the
     * wait — a hung network call must never hang logout.
     */
    fun awaitPendingRefresh(timeoutMs: Long = 500) {
        val future = synchronized(refreshLock) { pendingRefresh } ?: return
        try {
            future.get(timeoutMs, java.util.concurrent.TimeUnit.MILLISECONDS)
        } catch (_: Exception) {
            // Bounded wait only; the generation interlock is the real guarantee.
        }
    }

    /** Release the executor — tests and process teardown. */
    fun shutdown() {
        executor.shutdownNow()
    }

    private fun expireSession() {
        tokenStore.clear()
        sessionExpiredListener?.invoke()
    }

    private fun sessionExpiredException() = ApiException(401, "invalid_token", "Session expired")

    private fun requireSuccess(response: Response, body: String?): String {
        if (!response.isSuccessful) {
            val envelope = ApiJson.envelope(body)
            throw ApiException(response.code, envelope.code, envelope.detail ?: "Request failed (${response.code})")
        }
        return body.orEmpty()
    }

    companion object {
        private val JSON_MEDIA_TYPE = "application/json; charset=utf-8".toMediaType()
        private val threadIndex = AtomicInteger()

        /** Loopback + the Android emulator's host aliases — the only plaintext exception (docs/54 S5). */
        private val LOOPBACK_HOSTS = setOf("localhost", "127.0.0.1", "::1", "10.0.2.2", "10.0.3.2")

        fun isLoopbackHost(host: String): Boolean = host.lowercase() in LOOPBACK_HOSTS
    }
}

/** Adds the in-memory access JWT to every request. The refresh cookie is the jar's job. */
class AuthorizationInterceptor(private val tokenStore: AuthTokenStore) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val token = tokenStore.accessToken
        val request = if (token != null) {
            chain.request().newBuilder().header("Authorization", "Bearer $token").build()
        } else {
            chain.request()
        }
        return chain.proceed(request)
    }
}
