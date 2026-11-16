package com.margosmark.fintracker.http

/**
 * Wire shapes for the docs/52 Option A auth client.
 *
 * Parsing is org.json (bundled in the Android runtime — no serialization
 * framework dependency). JSON decoding reads ONLY the fields this client
 * needs; the server's response may carry more (ignore-unknown by construction).
 *
 * Rule (docs/52 §4.1): values are rendered exactly as the server returned
 * them. Nothing in this package computes, sums or reformats a financial value.
 */

/** docs/08 §Error envelope — the ONE error shape the backend answers with. */
data class ApiErrorEnvelope(val detail: String?, val code: String?)

/**
 * A non-2xx API answer, carrying the envelope's `code`.
 *
 * Branch on [code], NEVER on [message] (`detail` is human copy and is free to
 * be reworded; `code` is the contract field — same rule as the SPA's
 * `ApiError`, frontend/src/api/client.ts).
 */
class ApiException(
    val status: Int,
    val code: String?,
    override val message: String,
) : RuntimeException("[${status} ${code ?: "-"}] $message")

/** The subset of the server's `UserPublic` this client uses. Unknown fields ignored. */
data class UserPublic(
    val id: Int,
    val email: String,
    val name: String?,
    val privacyMode: Boolean,
)

/** `POST /auth` success shape (`TokenResponse`) — the refresh token is NEVER in the body. */
data class TokenResponse(val user: UserPublic, val accessToken: String)

/** A 200 that is NOT a session: `POST /auth/login` answered a TOTP challenge. */
data class LoginChallenge(val challengeToken: String, val expiresIn: Int)

/**
 * Minimal org.json decoding with the envelope fallbacks pinned.
 *
 * A malformed success body is a hard failure (IllegalStateException), never a
 * silent "no session": the server sent 2xx, so pretending it did not is worse
 * than crashing loudly into the caller's error path.
 */
internal object ApiJson {
    fun envelope(body: String?): ApiErrorEnvelope {
        if (body.isNullOrBlank()) return ApiErrorEnvelope(null, null)
        return try {
            val obj = org.json.JSONObject(body)
            ApiErrorEnvelope(
                if (obj.has("detail") && !obj.isNull("detail")) obj.getString("detail") else null,
                if (obj.has("code") && !obj.isNull("code")) obj.getString("code") else null,
            )
        } catch (_: Exception) {
            ApiErrorEnvelope(null, null)
        }
    }

    /** True when a 200 body is the `LoginChallenge` shape (docs/52 §1). */
    fun isTotpChallenge(body: String?): Boolean =
        !body.isNullOrBlank() &&
            try {
                org.json.JSONObject(body).optString("challenge") == "totp"
            } catch (_: Exception) {
                false
            }

    fun hasAccessToken(body: String?): Boolean =
        !body.isNullOrBlank() &&
            try {
                org.json.JSONObject(body).has("access_token")
            } catch (_: Exception) {
                false
            }

    fun tokenResponse(body: String): TokenResponse {
        val obj = org.json.JSONObject(body)
        val user = obj.getJSONObject("user")
        return TokenResponse(
            user = UserPublic(
                id = user.getInt("id"),
                email = user.getString("email"),
                name = if (user.has("name") && !user.isNull("name")) user.getString("name") else null,
                privacyMode = user.optBoolean("privacy_mode", false),
            ),
            accessToken = obj.getString("access_token"),
        )
    }

    fun loginChallenge(body: String): LoginChallenge {
        val obj = org.json.JSONObject(body)
        return LoginChallenge(challengeToken = obj.getString("challenge_token"), expiresIn = obj.getInt("expires_in"))
    }

    /** Request JSON for `POST /auth/login` and `POST /auth/login/totp`. */
    fun requestJson(vararg pairs: Pair<String, String>): String {
        val obj = org.json.JSONObject()
        for ((key, value) in pairs) obj.put(key, value)
        return obj.toString()
    }
}
