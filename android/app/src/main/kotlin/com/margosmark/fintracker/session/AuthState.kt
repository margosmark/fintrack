package com.margosmark.fintracker.session

import com.margosmark.fintracker.http.UserPublic

/**
 * The identity/session state the UI renders from. One sealed type, no
 * boolean soup: [TotpRequired] is explicitly NOT [Authenticated] — a password
 * challenge is a pending sign-in, not a session (docs/52 §1).
 */
sealed class AuthState {
    data object SignedOut : AuthState()

    /** A password verified, second factor outstanding. No token, no cookie. */
    data class TotpRequired(val challengeToken: String, val expiresIn: Int) : AuthState()

    data class Authenticated(val user: UserPublic) : AuthState() {
        /** AC5.1's "Demo · read-only" marker derives from this, not from UI guesswork. */
        val isDemo: Boolean get() = user.email == DEMO_EMAIL
    }
}

/** Shared with the backend's seed (`backend/app/services/demo_seed.py::DEMO_EMAIL`). */
const val DEMO_EMAIL = "demo@fintracker.local"

/** What logout learned, so the UI can be honest (docs/52 §2 rec 3). */
enum class LogoutOutcome {
    /** Server confirmed the refresh token was revoked. */
    SERVER_REVOKED,

    /** Server said the token was already invalid — nothing left to revoke. */
    ALREADY_INVALID,

    /**
     * Server unreachable: local credentials/data were cleared, but server-side
     * revocation COULD NOT be confirmed and the disclosure must say so. The
     * refresh row stays live server-side until expiry/reuse detection.
     */
    REVOCATION_UNCONFIRMED,
}
