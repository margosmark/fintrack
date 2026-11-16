package com.margosmark.fintracker.session

import com.margosmark.fintracker.auth.AuthApi
import com.margosmark.fintracker.auth.LoginResult
import com.margosmark.fintracker.http.ApiClient
import com.margosmark.fintracker.http.ApiException
import java.io.IOException

/**
 * The session lifecycle: login (password / TOTP / demo), logout, identity
 * change, session expiry and the intended-destination preserve/revalidate
 * (docs/52 Option A; docs/53 AC1.3, AC3.1).
 *
 * Every method BLOCKS; call from a background thread. State transitions are
 * reported through [listener]; [state] is safe to read from any thread.
 *
 * Ordering contracts this class exists to guarantee:
 *
 *  1. **Purge-before-render** ([docs/53 AC3.1]): [commitSession] runs
 *     [PurgeRegistry.purgeAll] SYNCHRONOUSLY before the access token is set
 *     and before the state becomes [AuthState.Authenticated]. A purge failure
 *     throws and the session is NOT committed — no half-transition.
 *  2. **No silent cookie overwrite** (docs/52 §2 rec 3): starting a login
 *     while a previous identity exists runs the full logout sequence first —
 *     server revocation attempt, THEN jar/token clear, THEN purge — so the
 *     new identity's login request leaves with an empty jar and the old
 *     refresh cookie already revoked (pinned by `IdentitySwitchPurgeOrderTest`).
 *  3. **Expiry is calm and preserves the destination** (docs/53 AC1.3): a
 *     provably dead session clears credentials + jar + purged stores, lands
 *     on [AuthState.SignedOut], and KEEPS [pendingDestination] so re-auth can
 *     revalidate and return the user there. Only an explicit logout drops it
 *     — or an IDENTITY CHANGE: [commitSession] hard-clears a preserved
 *     destination when a DIFFERENT user id commits (security S1), so one
 *     identity never consumes — or renders — another's route. The clear is
 *     keyed on the user id, not the flow: expiry → same-identity re-auth
 *     keeps the destination, expiry → different-identity login drops it.
 *  4. **Offline logout is honest** (docs/52 §2 rec 3): an unreachable server
 *     still clears local state and returns [LogoutOutcome.REVOCATION_UNCONFIRMED]
 *     so the UI discloses that server-side revocation could not be confirmed.
 */
class AuthSessionManager(
    private val authApi: AuthApi,
    private val client: ApiClient,
    val purgeRegistry: PurgeRegistry,
    private val listener: (AuthState) -> Unit = {},
) {
    private val lock = Any()

    @Volatile
    var state: AuthState = AuthState.SignedOut
        private set

    /** Internal same-app route to return to after re-auth; never an external URL. */
    @Volatile
    var pendingDestination: String? = null
        private set

    /**
     * The user id of the last identity that COMMITTED a session in this
     * process — the key [pendingDestination] preservation is scoped to
     * (security S1). Survives expiry on purpose (that is the same-identity
     * re-auth case); starts null so a cold-start deep-link destination,
     * noted before anyone signed in, survives the FIRST sign-in and is
     * revalidated by the deep-link existence/tenant gate instead (AC3.2).
     */
    @Volatile
    private var lastCommittedUserId: Int? = null

    init {
        client.sessionExpiredListener = ::handleSessionExpired
    }

    // --- sign in ------------------------------------------------------------

    fun login(email: String, password: String): LoginResult {
        ensureNoLiveIdentity()
        return when (val result = authApi.login(email, password)) {
            is LoginResult.TotpChallenge -> {
                setState(AuthState.TotpRequired(result.challengeToken, result.expiresIn))
                result
            }
            is LoginResult.Authenticated -> {
                commitSession(result.user, result.accessToken)
                result
            }
        }
    }

    /**
     * Submit the TOTP code of a pending [AuthState.TotpRequired]. A wrong
     * code throws `ApiException(invalid_totp_code)` and changes NO state —
     * the challenge stays pending, calmly.
     */
    fun loginWithTotp(code: String): LoginResult {
        val pending = state
        require(pending is AuthState.TotpRequired) { "no TOTP challenge pending" }
        val result = authApi.loginWithTotp(pending.challengeToken, code)
        val authenticated = result as? LoginResult.Authenticated
            ?: throw IllegalStateException("second factor returned ${result::class.simpleName}")
        commitSession(authenticated.user, authenticated.accessToken)
        return result
    }

    fun loginDemo(): LoginResult {
        ensureNoLiveIdentity()
        val result = authApi.demoLogin()
        val authenticated = result as? LoginResult.Authenticated
            ?: throw IllegalStateException("demo login returned ${result::class.simpleName}")
        commitSession(authenticated.user, authenticated.accessToken)
        return result
    }

    /**
     * Restore a session from the refresh cookie on app start (the SPA's
     * `restoreSession` equivalent): a single refresh through the same
     * single-flight funnel.
     */
    fun restoreSession(): RestoreOutcome {
        val current = state
        if (current is AuthState.Authenticated) return RestoreOutcome.Restored
        // docs/54 §4.2 L3: the purge RE-RUNS on every boot — process death
        // mid-logout must leave no residue. Idempotent, and nothing renders
        // before this returns.
        purgeRegistry.purgeAll()
        return when (val result = client.refreshTokens()) {
            is ApiClient.RefreshResult.Success -> {
                // Boot restore: the purge is a no-op (nothing local yet), but
                // going through the ONE commit seam keeps the ordering
                // contract single-sourced.
                commitSession(result.user, result.accessToken)
                RestoreOutcome.Restored
            }
            ApiClient.RefreshResult.Rejected -> {
                handleSessionExpired()
                RestoreOutcome.Expired
            }
            ApiClient.RefreshResult.Unreachable -> RestoreOutcome.Unreachable
        }
    }

    enum class RestoreOutcome { Restored, Expired, Unreachable }

    // --- logout / identity change ------------------------------------------

    fun logout(): LogoutOutcome {
        val outcome = logoutSequence()
        // Explicit sign-out drops the destination: there is nothing to return to.
        pendingDestination = null
        return outcome
    }

    private fun logoutSequence(): LogoutOutcome {
        // 1. Stop in-flight requests and wait for them to wind down
        //    (docs/52 §2 rec 3; docs/54 §4.2 L2 made it concrete).
        client.cancelInFlight()
        // 1b. Await a refresh already in flight (docs/54 §2 R4), then bump
        //     the session generation BEFORE clearing: any response landing
        //     after this point is discarded by ApiClient — a refresh racing
        //     logout can never re-populate the jar with a ghost cookie
        //     (docs/54 §4.2 L1). The bump precedes the server call so the
        //     window starts the moment we decide the session is over.
        client.awaitPendingRefresh()
        client.bumpSessionGeneration()
        // 2. Attempt server logout with the CURRENT jar — cookie-only route.
        val outcome = try {
            if (authApi.logout()) LogoutOutcome.SERVER_REVOKED else LogoutOutcome.ALREADY_INVALID
        } catch (_: Exception) {
            // Any failure — network, server error, unexpected — means
            // revocation cannot be confirmed. Local clearing below is
            // unconditional (docs/52 §2 rec 3; security M1).
            LogoutOutcome.REVOCATION_UNCONFIRMED
        }
        // 3. Clear credentials — jar AND in-memory access token.
        clearLocalCredentials()
        // 4. Synchronously purge every account-derived store.
        purgeRegistry.purgeAll()
        // 5. Now, and only now, may a signed-out (or next) identity render.
        setState(AuthState.SignedOut)
        return outcome
    }

    // --- session expiry ------------------------------------------------------

    /** Fired by [ApiClient] when the session is provably dead (refresh rejected). */
    private fun handleSessionExpired() {
        clearLocalCredentials()
        // Best-effort purge: expiry must not leave tenant data behind either.
        // A purge failure here is swallowed only in the sense that the user is
        // already signed out — the exception still surfaces to the caller of
        // whichever request triggered the expiry (getJson's throw path).
        purgeRegistry.purgeAll()
        // pendingDestination deliberately survives: AC1.3 revalidation.
        setState(AuthState.SignedOut)
    }

    // --- intended destination (docs/53 AC1.3) --------------------------------

    /**
     * Record where the user was heading before expiry, so re-auth can
     * revalidate and return them there. INTERNAL routes only — anything with
     * a scheme or authority is rejected outright (a destination is never an
     * open redirect). Preservations are FILLED routes (never route patterns)
     * and identity-scoped: [commitSession] drops the value the moment a
     * different identity commits (docs/53 AC1.3; security S1).
     */
    fun noteDestination(internalRoute: String) {
        require(!internalRoute.startsWith("//") && !internalRoute.contains("://")) {
            "destination must be an internal route: $internalRoute"
        }
        pendingDestination = internalRoute
    }

    /** Take the destination once (post re-auth revalidation); clears it. */
    fun consumePendingDestination(): String? = pendingDestination.also { pendingDestination = null }

    // --- internals ------------------------------------------------------------

    /**
     * Single commit seam for a NEW identity: purge first, then token, then
     * state. Order is the contract — `IdentitySwitchPurgeOrderTest` pins the
     * wire-level ordering (purge completes before the next login request even
     * leaves the device, and before `Authenticated` can render).
     *
     * Security S1: an identity CHANGE hard-clears the preserved destination
     * BEFORE any state of the new identity can render — B must never consume
     * (or even see) A's route. Same user id re-committing (expiry → re-auth)
     * keeps it: that is docs/53 AC1.3's revalidation path.
     */
    private fun commitSession(user: com.margosmark.fintracker.http.UserPublic, accessToken: String) {
        val previousUserId = lastCommittedUserId
        if (previousUserId != null && previousUserId != user.id) {
            pendingDestination = null
        }
        purgeRegistry.purgeAll()
        client.tokenStore.set(accessToken)
        lastCommittedUserId = user.id
        setState(AuthState.Authenticated(user))
    }

    /** docs/52 §2 rec 3, item: do not silently overwrite a live refresh cookie. */
    private fun ensureNoLiveIdentity() {
        if (state is AuthState.Authenticated || !client.cookieJar.isEmpty()) {
            logoutSequence()
        }
    }

    private fun clearLocalCredentials() {
        client.tokenStore.clear()
        client.cookieJar.clear()
    }

    private fun setState(next: AuthState) {
        synchronized(lock) { state = next }
        listener(next)
    }
}
