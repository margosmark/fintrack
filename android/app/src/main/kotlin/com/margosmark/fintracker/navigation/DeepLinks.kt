package com.margosmark.fintracker.navigation

import java.net.URI

/**
 * Minimal session view the shell needs to gate deep links. The auth/data
 * card (t_6eddd208) owns the real session client under data/ and net/;
 * it maps its session into this snapshot at the seam documented in
 * android/ARCHITECTURE.md. The shell never reads tokens itself.
 */
data class SessionSnapshot(
    val isAuthenticated: Boolean,
    val tenantId: String?,
) {
    companion object {
        /** The shell's default before auth lands: nothing is authenticated. */
        val UNAUTHENTICATED = SessionSnapshot(isAuthenticated = false, tenantId = null)
    }
}

/** What a recognized deep link points at, before auth/extant checks. */
sealed interface DeepLinkTarget {
    data object Glance : DeepLinkTarget
    data object AccountsList : DeepLinkTarget
    data class Account(val accountId: String) : DeepLinkTarget
    data class Holding(val accountId: String, val holdingId: String) : DeepLinkTarget
    data object Transactions : DeepLinkTarget
    data object Settings : DeepLinkTarget
}

sealed interface DeepLinkResolution {
    /** Authenticated + extant + same-tenant: navigate to [route]. */
    data class Navigate(val route: String) : DeepLinkResolution

    /** Recognized but not signed in: go to sign-in first. */
    data object RequireSignIn : DeepLinkResolution

    /** Unknown, stale, or foreign target: render the generic unavailable destination. */
    data object Unavailable : DeepLinkResolution
}

/**
 * The hosts whose https links this app accepts as deep links (security M3),
 * matched EXACTLY (case-insensitively) — never by wildcard or bare suffix, so
 * every other subdomain of the apex and every look-alike stays foreign:
 * `fintrac-princessandmark.com`, `evilprincessandmark.com`,
 * `fintrac.princessandmark.com.evil.example` … none of them resolve.
 *
 * The apex (`princessandmark.com`) is deliberately NOT served: it does not
 * host the app and its TLS certificate is self-signed, so an https link under
 * it can never be a legitimate handoff. This set must match the manifest's
 * https `<data android:host>` entries one-to-one (AndroidManifest.xml).
 */
val SERVED_HOSTS: Set<String> = setOf("fintrac.princessandmark.com")

/** True iff [host] is exactly one of [SERVED_HOSTS] (case-insensitive, no suffix matching). */
fun isServedHost(host: String?): Boolean =
    host != null && host.lowercase() in SERVED_HOSTS

/**
 * Parses a deep-link URI into a [DeepLinkTarget], or null when the shape is
 * not one this app owns (unknown scheme/host/paths — includes foreign links
 * that merely look similar).
 *
 * Accepted shapes (host segment shown as `app` for the custom scheme):
 *   fintracker://app/glance
 *   fintracker://app/accounts
 *   fintracker://app/accounts/{accountId}
 *   fintracker://app/accounts/{accountId}/holdings/{holdingId}
 *   fintracker://app/transactions
 *   fintracker://app/settings
 *   https://fintrac.princessandmark.com/app/… (same paths, served host only)
 */
fun parseDeepLink(uriString: String?): DeepLinkTarget? {
    if (uriString.isNullOrBlank()) return null
    val uri = try {
        URI(uriString)
    } catch (_: Exception) {
        return null
    }
    val scheme = uri.scheme?.lowercase() ?: return null
    val segments = when (scheme) {
        "fintracker" -> {
            if (!uri.host.equals("app", ignoreCase = true)) return null
            pathSegments(uri.rawPath)
        }
        "http", "https" -> {
            // Web handoff links: /app/... under the served host only (security M3).
            if (!isServedHost(uri.host)) return null
            val segs = pathSegments(uri.rawPath)
            if (segs.firstOrNull() != "app") return null
            segs.drop(1)
        }
        else -> return null
    }
    return when {
        segments == listOf("glance") -> DeepLinkTarget.Glance
        segments == listOf("accounts") -> DeepLinkTarget.AccountsList
        segments.size == 2 && segments[0] == "accounts" ->
            DeepLinkTarget.Account(segments[1])
        segments.size == 4 && segments[0] == "accounts" && segments[2] == "holdings" ->
            DeepLinkTarget.Holding(segments[1], segments[3])
        segments == listOf("transactions") -> DeepLinkTarget.Transactions
        segments == listOf("settings") -> DeepLinkTarget.Settings
        else -> null
    }
}

private fun pathSegments(rawPath: String?): List<String> =
    rawPath.orEmpty().trim('/').split('/').filter { it.isNotEmpty() }

/**
 * Deep-link gate. A target navigates ONLY when all three hold:
 *  1. the session is authenticated,
 *  2. the target exists (not stale),
 *  3. it belongs to the session's tenant.
 *
 * [targetExists] answers (2) AND (3) together: the data layer MUST scope its
 * lookup to the current session's tenant, so "exists in another tenant"
 * answers false exactly like "does not exist" — a foreign id never resolves.
 * Unrecognized shapes resolve to [DeepLinkResolution.Unavailable]; nothing
 * falls through to a crash or a blank screen.
 */
fun resolveDeepLink(
    target: DeepLinkTarget?,
    session: SessionSnapshot,
    targetExists: (DeepLinkTarget) -> Boolean = { false },
): DeepLinkResolution {
    if (target == null) return DeepLinkResolution.Unavailable
    if (!session.isAuthenticated) return DeepLinkResolution.RequireSignIn
    val route = when (target) {
        DeepLinkTarget.Glance -> Routes.GLANCE
        DeepLinkTarget.AccountsList -> Routes.ACCOUNTS
        DeepLinkTarget.Transactions -> Routes.TRANSACTIONS
        DeepLinkTarget.Settings -> Routes.SETTINGS
        is DeepLinkTarget.Account -> Routes.accountDetail(target.accountId)
        is DeepLinkTarget.Holding -> Routes.holdingDetail(target.accountId, target.holdingId)
    }
    // List-level destinations carry no per-id identity to verify.
    val needsExistenceCheck = target is DeepLinkTarget.Account || target is DeepLinkTarget.Holding
    if (needsExistenceCheck && !targetExists(target)) return DeepLinkResolution.Unavailable
    return DeepLinkResolution.Navigate(route)
}

/** Convenience: parse + resolve in one step (what the shell calls at startup). */
fun resolveDeepLinkUri(
    uriString: String?,
    session: SessionSnapshot,
    targetExists: (DeepLinkTarget) -> Boolean = { false },
): DeepLinkResolution = resolveDeepLink(parseDeepLink(uriString), session, targetExists)
