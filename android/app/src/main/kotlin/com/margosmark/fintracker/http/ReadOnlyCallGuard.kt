package com.margosmark.fintracker.http

import java.io.IOException
import okhttp3.Interceptor
import okhttp3.Response

/** Thrown instead of sending a non-GET, non-auth request. Never reaches the wire. */
class ReadOnlyViolationException(val method: String, val path: String) :
    IOException("read-only Android v1 client blocked $method $path (docs/52 §2 rec 4)")

/**
 * Transport-level read-only enforcement for Android v1 (docs/52 §2 rec 4,
 * docs/53 AC1.2).
 *
 * The server's demo-only write guard does NOT make a regular account's bearer
 * token read-only, and "the UI has no edit buttons" is not a boundary. This
 * interceptor is the boundary: the ONLY non-GET traffic the client can emit
 * is the auth/session bookkeeping docs/52 documents — `POST /auth/login`,
 * `/auth/login/totp`, `/auth/demo`, `/auth/refresh`, `/auth/logout`. Anything
 * else (a domain POST/PUT/PATCH/DELETE) throws before OkHttp opens a socket,
 * so a future UI bug cannot silently mutate the ledger.
 *
 * Allowlist matching is on the path RELATIVE TO the API base, exact — not a
 * suffix guess, so a future `/api/v1/evil/auth/login` cannot slip under it.
 *
 * Pinned by `ReadOnlyTrafficTest`: a recorded-traffic assertion over a full
 * walkthrough plus direct violation probes that assert zero wire requests.
 */
class ReadOnlyCallGuard(private val basePath: String) : Interceptor {

    private val allowedPosts: Set<String> = allowedPostPaths.map { basePath + it }.toSet()

    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val path = request.url.encodedPath
        val allowed = when (request.method) {
            "GET", "HEAD", "OPTIONS" -> true
            "POST" -> path in allowedPosts
            else -> false
        }
        if (!allowed) throw ReadOnlyViolationException(request.method, path)
        return chain.proceed(request)
    }

    companion object {
        val allowedPostPaths = listOf(
            "/auth/login",
            "/auth/login/totp",
            "/auth/demo",
            "/auth/refresh",
            "/auth/logout",
        )
    }
}
