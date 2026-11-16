package com.margosmark.fintracker.ui.screens

import java.time.Instant

/**
 * Per-section presentation states for the v1 read screens (docs/53 AC1.3).
 *
 * Why this exists beside the shell's [com.margosmark.fintracker.state.ScreenState]:
 * none of the v1 read endpoints supplies a `server_checked_at` field
 * (docs/52 §3), so the shell's `Ready/NoData/Stale` types — which REQUIRE a
 * real `server_checked_at` at construction — cannot be built for them without
 * fabricating a timestamp, which the shell's own contract forbids. This type
 * carries `receivedAt` instead: the CLIENT-OBSERVED instant the response
 * arrived, which docs/52 §3 explicitly sanctions when labelled as a client
 * observation (rendered as "Received ⟨t⟩ · device time", never "checked",
 * "updated" or "server").
 *
 * Each state has its own test tag so tests can pin DISTINCT renders without
 * depending on colors (StateTags-style, AC1.3).
 */
sealed interface SectionState<out T> {

    /** First load in flight. No figures of any kind are rendered. */
    data object Loading : SectionState<Nothing>

    /** Data received; [receivedAt] = client-observed receipt (device time). */
    data class Ready<T>(val data: T, val receivedAt: Instant) : SectionState<T>

    /** A refresh is in flight OVER last-known figures; figures + original receipt time stay. */
    data class Refreshing<T>(val data: T, val receivedAt: Instant) : SectionState<T>

    /**
     * The refresh FAILED but last-known figures from this session remain.
     * [offline] distinguishes "no connection" from a server error; both copy
     * forms name the ORIGINAL receipt time (AC1.3 "Couldn't update · showing
     * data from ⟨time⟩" + Retry).
     */
    data class RefreshFailed<T>(val data: T, val receivedAt: Instant, val offline: Boolean) : SectionState<T>

    /**
     * Data present with the SERVER's staleness signal. [staleNote] is built
     * from server fields only (price_age_days / price_date), never from
     * clock math (AC4.1).
     */
    data class Stale<T>(val data: T, val receivedAt: Instant, val staleNote: String) : SectionState<T>

    /** No connection on FIRST load. v1 keeps no offline finance figures. */
    data object Offline : SectionState<Nothing>

    /** Server error / unreachable with no data to show. */
    data object Unavailable : SectionState<Nothing>

    /** The record is gone (stale/deleted). Generic — never reveals foreign existence. */
    data object NotFound : SectionState<Nothing>

    /** Server answered 200 with zero rows — honest empty, never a fabricated zero. */
    data class NoData<T>(val receivedAt: Instant) : SectionState<T>

    /** Permission/privacy withholding (e.g. 403 withheld) explained in place. */
    data class Permission(val detail: String?) : SectionState<Nothing>

    /** The session is provably dead; renders the calm sign-in state. */
    data object Expired : SectionState<Nothing>
}

/** Stable tags for distinct-state assertions (AC1.3) — API for tests. */
object SectionTags {
    const val LOADING = "section_state_loading" // (routed to shell StatePane tag)
    const val READY = "section_state_ready"
    const val REFRESHING = "section_state_refreshing"
    const val REFRESH_FAILED = "section_state_refresh_failed"
    const val STALE = "section_state_stale"
    const val OFFLINE = "section_state_offline"
    const val UNAVAILABLE = "section_state_unavailable"
    const val NOT_FOUND = "section_state_not_found"
    const val NO_DATA = "section_state_no_data"
    const val PERMISSION = "section_state_permission"
    const val EXPIRED = "section_state_expired"
    const val RECEIVED_AT = "section_received_at"
    const val RETRY = "section_retry"
    const val DEMO_BADGE = "demo_badge"
    const val MASK_TOGGLE = "mask_toggle"
    const val WITHHELD_NOTE = "section_withheld_note"
    const val SIGN_OUT = "settings_sign_out"
    const val EMAIL_FIELD = "sign_in_email"
    const val PASSWORD_FIELD = "sign_in_password"
    const val SIGN_IN_BUTTON = "sign_in_button"
    const val EXPLORE_DEMO = "explore_demo"
    const val TOTP_FIELD = "totp_field"
    const val TOTP_SUBMIT = "totp_submit"
    const val SIGN_IN_ERROR = "sign_in_error"
    const val LOGOUT_NOTICE = "logout_notice"
}
