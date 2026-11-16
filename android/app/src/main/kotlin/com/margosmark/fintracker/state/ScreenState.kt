package com.margosmark.fintracker.state

import java.time.Instant

/**
 * Truthful rendering states, shared by every screen.
 *
 * PROVENANCE RULE: a timestamp rendered anywhere in the UI must be a
 * `server_checked_at` value carried on a real server response. The state
 * types make fabrication impossible — there is no constructor that takes
 * "now", and [Instant.now] must never appear in rendering code. When a
 * state has no server-checked time, the field is null and the UI OMITS the
 * timestamp entirely rather than showing a local clock reading.
 */
sealed interface ScreenState<out T> {

    /**
     * Request in flight. [serverCheckedAt] is the server_checked_at of the
     * last SUCCESSFUL response if one is already known (e.g. a background
     * refresh over previously loaded data); null on first load.
     */
    data class Loading<out T>(val serverCheckedAt: Instant? = null) : ScreenState<T>

    /** Fresh data from a real response; [serverCheckedAt] is its server_checked_at. */
    data class Ready<out T>(val data: T, val serverCheckedAt: Instant) : ScreenState<T>

    /** Server answered successfully with zero rows. */
    data class NoData<out T>(val serverCheckedAt: Instant) : ScreenState<T>

    /**
     * Data present but the last successful server check is older than the
     * freshness window. Renders the data PLUS a stale banner with the age.
     */
    data class Stale<out T>(val data: T, val serverCheckedAt: Instant) : ScreenState<T>

    /**
     * Server unreachable/error and no cached data to show. [lastServerCheckedAt],
     * when present, is the server_checked_at of the last response that DID
     * succeed — shown as provenance for the failure, never invented.
     */
    data class Unavailable<out T>(val lastServerCheckedAt: Instant? = null) : ScreenState<T>

    /** E2EE vault locked; dependent surfaces degrade per the lock contract. */
    data object Locked : ScreenState<Nothing>

    /** Session expired; the user must sign in again. */
    data object SessionExpired : ScreenState<Nothing>

    /** Authenticated, but the target is outside this tenant's scope. */
    data object Permission : ScreenState<Nothing>
}
