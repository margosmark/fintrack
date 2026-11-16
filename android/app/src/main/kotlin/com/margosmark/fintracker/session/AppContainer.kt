package com.margosmark.fintracker.session

import android.content.Context
import android.content.pm.ApplicationInfo
import com.margosmark.fintracker.auth.AuthApi
import com.margosmark.fintracker.http.ApiClient
import com.margosmark.fintracker.http.KeystoreGcmCipher
import com.margosmark.fintracker.http.PersistentCookieJar
import java.io.File
import okhttp3.HttpUrl.Companion.toHttpUrl

/**
 * Wiring for the auth/session client — the pieces this card owns, assembled
 * in one place. The UI card constructs [AppContainer] (e.g. from an
 * `Application` subclass) and renders from [AuthSessionManager.state].
 *
 * Base URL policy (docs/52 card, scope item 6):
 *  - [baseUrl] is INJECTED via constructor parameter — no test or gate build
 *    can accidentally bake in a production host. There is currently no
 *    `BuildConfig.API_BASE_URL`; build.gradle.kts has no `buildConfigField`
 *    (security M4). A production base must be passed explicitly at wiring
 *    time and never becomes a test default.
 *  - [DEFAULT_API_BASE_URL] is the dev/emulator default: the host machine's
 *    loopback as seen from the Android emulator, i.e. the LOCAL docker stack
 *    (`http://10.0.2.2:8080` → nginx `/api/v1`). A production base must be
 *    passed explicitly at build time and never becomes a test default.
 *  - A plaintext local stack must run the backend with `COOKIE_SECURE=false`
 *    (see [PersistentCookieJar]: OkHttp will not send a Secure cookie over
 *    http, same trap as the MCP client's).
 *
 * Storage policy (docs/54 §1): the cookie store file lives in
 * [Context.getFilesDir] — app-private storage, UID-isolated (S1),
 * Keystore-sealed (S2; see [KeystoreGcmCipher] for the exact guarantee).
 * Backup exclusion is `allowBackup="false"` in the manifest (app-shell card
 * owns that file; REQUIRED per S3, no `dataExtractionRules` carve-out that
 * re-includes the jar). Per §1.4 a DEBUGGABLE build never persists the jar at
 * all — checked here at runtime, so a debug install is fail-closed even if a
 * future manifest edit regresses the build flag.
 */
class AppContainer(
    context: Context,
    baseUrl: String = DEFAULT_API_BASE_URL,
) {
    val purgeRegistry = PurgeRegistry()

    private val debuggable =
        (context.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) != 0

    val cookieJar = PersistentCookieJar(
        storeFile = File(File(context.filesDir, AUTH_DIR), COOKIE_STORE_FILE),
        cipher = KeystoreGcmCipher(),
        // docs/54 §1.4: fail closed — debuggable builds are in-memory only.
        persistenceEnabled = !debuggable,
        // docs/54 §1.2 S6: only auth-route responses may write the jar.
        setCookiePathPrefix = baseUrl.toHttpUrl().encodedPath.trimEnd('/') + "/auth",
    )

    val apiClient = ApiClient(baseUrl = baseUrl, cookieJar = cookieJar)

    val authApi = AuthApi(apiClient)

    val session = AuthSessionManager(authApi, apiClient, purgeRegistry)

    companion object {
        const val DEFAULT_API_BASE_URL = "http://10.0.2.2:8080/api/v1"
        private const val AUTH_DIR = "auth"
        private const val COOKIE_STORE_FILE = "cookie_store.enc"
    }
}
