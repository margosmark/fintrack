package com.margosmark.fintracker.auth

import com.margosmark.fintracker.http.ApiClient
import com.margosmark.fintracker.http.ApiException
import com.margosmark.fintracker.http.ApiJson
import com.margosmark.fintracker.http.LoginChallenge
import com.margosmark.fintracker.http.UserPublic
import java.io.IOException

/**
 * The auth/session routes docs/52 documents — and ONLY those (Option A keeps
 * the backend's cookie-only protocol untouched; rec 5 forbids inventing a
 * native token protocol).
 *
 *  - `POST /auth/login` — password login. A 200 is NOT necessarily a session:
 *    an account with TOTP enrolled answers a `LoginChallenge` with no cookie
 *    and no token (docs/52 §1). [login] returns the distinction as a sealed
 *    type so no caller can mistake one for the other.
 *  - `POST /auth/login/totp` — the second factor of a password challenge.
 *  - `POST /auth/demo` — the demo door; same TokenResponse shape.
 *  - `POST /auth/refresh` / `POST /auth/logout` — cookie-only; handled by
 *    [ApiClient] (refresh) and [logout] here.
 *
 * Wrong credentials are a calm 401 `invalid_credentials` envelope: [login]
 * throws [ApiException] with that code and changes NO state (no refresh is
 * attempted, the jar is untouched — pinned by `WrongCredentialsTest`).
 *
 * These methods BLOCK; call from a background thread.
 *
 * Note what is deliberately absent: register, TOTP setup/confirm, recovery,
 * passkeys, OIDC, demo-reset, and every domain route. Android v1 is a
 * read-only companion; the read-only guard enforces that even if a future
 * caller tries.
 */
class AuthApi(private val client: ApiClient) {

    fun login(email: String, password: String): LoginResult = postLogin(
        "/auth/login",
        ApiJson.requestJson("email" to email, "password" to password),
    )

    /** Second factor for a pending [LoginResult.TotpChallenge]. */
    fun loginWithTotp(challengeToken: String, code: String): LoginResult = postLogin(
        "/auth/login/totp",
        ApiJson.requestJson("challenge_token" to challengeToken, "code" to code),
    )

    fun demoLogin(): LoginResult = postLogin("/auth/demo", "{}")

    /**
     * `POST /auth/logout` with the current jar (cookie-only).
     *
     * @return true when the server revoked the presented token; false when the
     *   server called it already-invalid (nothing left to revoke — NOT an
     *   error the user must see).
     * @throws IOException when the server is unreachable — the caller (the
     *   session manager) still clears local state and must disclose that
     *   revocation could not be confirmed (docs/52 §2 rec 3).
     */
    fun logout(): Boolean {
        client.execute(client.request("POST", "/auth/logout")).use { response ->
            return when {
                response.isSuccessful -> true
                response.code == 401 -> false
                else -> {
                    val envelope = ApiJson.envelope(response.body?.string())
                    throw ApiException(response.code, envelope.code, envelope.detail ?: "Logout failed")
                }
            }
        }
    }

    private fun postLogin(path: String, body: String): LoginResult {
        client.execute(client.request("POST", path, body)).use { response ->
            val payload = response.body?.string()
            if (!response.isSuccessful) {
                val envelope = ApiJson.envelope(payload)
                throw ApiException(
                    response.code,
                    envelope.code,
                    envelope.detail ?: "Request failed (${response.code})",
                )
            }
            // 200 parsing, in the order that cannot mislabel a challenge:
            // challenge shape first (docs/52 §1 — a challenge is NOT a session).
            return when {
                ApiJson.isTotpChallenge(payload) -> {
                    val challenge: LoginChallenge = ApiJson.loginChallenge(requireNotNull(payload))
                    LoginResult.TotpChallenge(challenge.challengeToken, challenge.expiresIn)
                }
                ApiJson.hasAccessToken(payload) -> {
                    val pair = ApiJson.tokenResponse(requireNotNull(payload))
                    // Token NOT stored here: the session manager commits it
                    // AFTER the identity-purge seam (see AuthSessionManager).
                    LoginResult.Authenticated(pair.user, pair.accessToken)
                }
                else -> throw ApiException(response.code, null, "Malformed auth response from $path")
            }
        }
    }
}

/** What any sign-in call answered. A [TotpChallenge] is NOT an authenticated session. */
sealed class LoginResult {
    data class Authenticated(val user: UserPublic, val accessToken: String) : LoginResult()

    data class TotpChallenge(val challengeToken: String, val expiresIn: Int) : LoginResult()
}
