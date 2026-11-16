package com.margosmark.fintracker.http

import java.io.File
import okhttp3.Cookie
import okhttp3.CookieJar
import okhttp3.HttpUrl
import org.json.JSONArray
import org.json.JSONObject

/**
 * The persistent, app-scoped cookie jar carrying the server's `refresh_token`
 * HttpOnly cookie flow (docs/52 Option A).
 *
 * What this IS: the app's own trusted cookie store — the native equivalent of
 * the browser jar the SPA relies on. `HttpOnly` is a browser-script property
 * and does not transfer; docs/52 says so explicitly, and this class is the
 * place that fact is handled honestly.
 *
 * Persistence: cookies are serialized to JSON, sealed with [PayloadCipher]
 * (Keystore-backed AES-GCM at runtime — see that class for the exact
 * guarantee), and written to [storeFile], which MUST live in app-private
 * storage (see [com.margosmark.fintracker.session.AppContainer], which uses
 * `filesDir`). Backup exclusion is the manifest's `allowBackup="false"`
 * (owned by the app-shell card; required, not optional — docs/54 §1.2 S3).
 *
 * docs/54 compliance, per requirement:
 *  - §1.2 S5: cookie attributes are honored as OkHttp parsed them; the jar
 *    sends a cookie only where `Cookie.matches` allows (scheme/path/host) —
 *    a `Path=/api/v1/auth` refresh cookie never rides an ordinary API call.
 *  - §1.2 S6: ONLY responses whose path starts with [setCookiePathPrefix]
 *    (the auth routes) may write the jar; a Set-Cookie from any other route
 *    is ignored entirely (memory and disk).
 *  - §1.4: [persistenceEnabled]=false makes the jar in-memory only (Option C
 *    behavior) — debuggable builds never persist the credential; the file is
 *    deleted if one is found.
 *  - §4.2 L3: [clear] deletes the file AND destroys the Keystore key.
 *
 * Exposure rules (docs/52 §2 rec 2, docs/54 §3): cookie VALUES are never
 * logged, never returned to UI, never put in prefs. The only inspection
 * accessor is [storedCookieNames] — names, no values.
 *
 * Secure-over-plaintext: this jar stores whatever the server sets, but OkHttp
 * refuses to SEND a `Secure` cookie over a non-TLS connection (same behavior
 * as httpx's jar — see the MCP client lesson in the fin-tracker skill). A
 * local plaintext dev stack must therefore run with `COOKIE_SECURE=false`, or
 * refresh silently 401s. `SecureCookieIsNotSentOverPlaintext` in the unit
 * suite pins this so nobody debugs it in production traffic later.
 */
class PersistentCookieJar(
    private val storeFile: File,
    private val cipher: PayloadCipher,
    private val persistenceEnabled: Boolean = true,
    private val setCookiePathPrefix: String? = null,
) : CookieJar {

    private val lock = Any()
    private val cookies = mutableListOf<Cookie>()

    init {
        if (persistenceEnabled) {
            loadFromDisk()
        } else {
            // docs/54 §1.4: fail closed — a debuggable build must not keep a
            // persisted credential even if one is found on disk.
            storeFile.delete()
        }
    }

    override fun saveFromResponse(url: HttpUrl, received: List<Cookie>) {
        synchronized(lock) {
            // docs/54 §1.2 S6: only auth-route responses write the jar.
            if (setCookiePathPrefix != null && !url.encodedPath.startsWith(setCookiePathPrefix)) {
                return
            }
            for (cookie in received) {
                // Replacement is keyed on identity (name+domain+path), not name:
                // two cookies may share a name across paths.
                cookies.removeAll { it.name == cookie.name && it.domain == cookie.domain && it.path == cookie.path }
                val expired = cookie.persistent && cookie.expiresAt <= System.currentTimeMillis()
                if (!expired) cookies.add(cookie)
            }
            persistLocked()
        }
    }

    override fun loadForRequest(url: HttpUrl): List<Cookie> = synchronized(lock) {
        cookies.filter { it.matches(url) }
    }

    /** Cookie NAMES only — values must never reach UI, logs or crash reports. */
    fun storedCookieNames(): List<String> = synchronized(lock) { cookies.map { it.name } }

    fun isEmpty(): Boolean = synchronized(lock) { cookies.isEmpty() }

    /**
     * Remove a cookie only if its value matches exactly (docs/54 §4.2 L1):
     * the logout interlock's way of discarding a LATE refresh response's
     * Set-Cookie without ever touching a successor identity's rotation.
     * @return true when a cookie was actually removed.
     */
    fun removeIfValue(name: String, value: String): Boolean = synchronized(lock) {
        val removed = cookies.removeAll { it.name == name && it.value == value }
        if (removed) persistLocked()
        removed
    }

    /**
     * Wipe memory, the sealed file AND the backing key (docs/54 §4.2 L3).
     * Used by logout, expiry and identity change; idempotent.
     */
    fun clear() {
        synchronized(lock) {
            cookies.clear()
            storeFile.delete()
        }
        cipher.destroy()
    }

    // --- persistence -------------------------------------------------------

    private fun persistLocked() {
        if (!persistenceEnabled) return // docs/54 §1.4: in-memory only
        val array = JSONArray()
        for (cookie in cookies) {
            array.put(
                JSONObject()
                    .put("name", cookie.name)
                    .put("value", cookie.value)
                    .put("domain", cookie.domain)
                    .put("hostOnly", cookie.hostOnly)
                    .put("path", cookie.path)
                    .put("secure", cookie.secure)
                    .put("httpOnly", cookie.httpOnly)
                    .put("persistent", cookie.persistent)
                    .put("expiresAt", cookie.expiresAt),
            )
        }
        storeFile.parentFile?.mkdirs()
        val sealed = cipher.encrypt(array.toString().toByteArray(Charsets.UTF_8))
        // Write-to-temp then rename: a torn write must not destroy a live session.
        val tmp = File(storeFile.parentFile, storeFile.name + ".tmp")
        tmp.writeBytes(sealed)
        if (!tmp.renameTo(storeFile)) {
            storeFile.delete()
            tmp.renameTo(storeFile)
        }
    }

    private fun loadFromDisk() {
        if (!storeFile.exists()) return
        val parsed = try {
            val json = String(cipher.decrypt(storeFile.readBytes()), Charsets.UTF_8)
            val array = JSONArray(json)
            (0 until array.length()).mapNotNull { index ->
                val obj = array.getJSONObject(index)
                val builder = Cookie.Builder()
                    .name(obj.getString("name"))
                    .value(obj.getString("value"))
                    .path(obj.getString("path"))
                if (obj.getBoolean("hostOnly")) {
                    builder.hostOnlyDomain(obj.getString("domain"))
                } else {
                    builder.domain(obj.getString("domain"))
                }
                if (obj.getBoolean("secure")) builder.secure()
                if (obj.getBoolean("httpOnly")) builder.httpOnly()
                if (obj.getBoolean("persistent")) {
                    val expiresAt = obj.getLong("expiresAt")
                    if (expiresAt > System.currentTimeMillis()) builder.expiresAt(expiresAt)
                }
                builder.build()
            }
        } catch (_: Exception) {
            // Fail closed (docs/52 §2 rec 2): an unreadable store is no store.
            storeFile.delete()
            return
        }
        synchronized(lock) {
            cookies.clear()
            cookies.addAll(parsed.filter { !it.isExpiredAtNow() })
        }
    }

    private fun Cookie.isExpiredAtNow(): Boolean =
        persistent && expiresAt <= System.currentTimeMillis()
}
