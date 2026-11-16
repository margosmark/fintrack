package com.margosmark.fintracker.http

/**
 * The access JWT holder — PROCESS MEMORY ONLY, by contract (docs/52 §2,
 * Option A): the 15-minute access token is never written to disk, prefs, a
 * database, logs, or anything else that outlives the process.
 *
 * The 30-day refresh credential is a different animal: it lives ONLY inside
 * [PersistentCookieJar]'s encrypted store and is never readable through this
 * class (no accessor exposes it — `storedCookieNames()` returns names only).
 */
class AuthTokenStore {
    private val lock = Any()
    private var token: String? = null

    val accessToken: String?
        get() = synchronized(lock) { token }

    fun set(value: String) {
        require(value.isNotEmpty()) { "empty access token" }
        synchronized(lock) { token = value }
    }

    fun clear() {
        synchronized(lock) { token = null }
    }
}
