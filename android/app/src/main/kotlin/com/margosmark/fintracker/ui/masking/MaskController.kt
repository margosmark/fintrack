package com.margosmark.fintracker.ui.masking

import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.MutableState
import androidx.compose.runtime.State
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner

/**
 * Session-scoped display-masking state (docs/46, docs/53 AC2).
 *
 * Truths encoded here:
 *
 *  - **Restore-on-boot OFF.** The controller is constructed masked and there
 *    is NO persistence layer: no SharedPreferences, no file, no DataStore.
 *    A relaunch therefore always starts at the tenant default (masked) —
 *    AC2.2 holds by construction, not by remembering to clear something.
 *  - **Re-mask immediately** on background/device lock (the lifecycle
 *    observer below), logout, identity change and session expiry — the
 *    on-screen frame is replaced with masked values BEFORE the OS takes the
 *    app-switcher snapshot (AC2.3), because masking is a compose-state flip
 *    that lands while the activity is stopping.
 *  - **Masking is presentation only.** It changes what renders; it never
 *    changes payloads, storage or authorization (docs/52 §4.6). Values
 *    travel the authenticated API in plaintext regardless of this flag.
 *  - Silent same-identity session activity (successful refresh) does NOT
 *    touch this flag — the user's in-session choice stands (AC2.2).
 */
class MaskController(initiallyMasked: Boolean = true) {

    private val maskedState: MutableState<Boolean> = mutableStateOf(initiallyMasked)

    val masked: Boolean get() = maskedState.value

    /** Observable for Compose readers (headers, MaskedText call sites). */
    val state: State<Boolean> get() = maskedState

    /** Why the last re-mask happened — surfaced in tests, not in UI copy. */
    var lastMaskReason: MaskReason = MaskReason.COLD_START
        private set

    fun reveal() {
        maskedState.value = false
        lastMaskReason = MaskReason.REVEALED
    }

    fun mask(reason: MaskReason) {
        maskedState.value = true
        lastMaskReason = reason
    }

    /** Toggle used by the shell's mask control. */
    fun toggle() {
        if (masked) reveal() else mask(MaskReason.COLD_START)
    }
}

/** The re-mask triggers docs/53 AC2.3 names, plus the boot default. */
enum class MaskReason {
    COLD_START,
    BACKGROUNDED,
    DEVICE_LOCKED,
    LOGOUT,
    IDENTITY_CHANGED,
    SESSION_EXPIRED,
    REVEALED,
}

/**
 * Re-masks the moment the hosting screen leaves the foreground — which
 * includes device lock (the activity stops) and precedes the OS's
 * recents/app-switcher snapshot, so the snapshot is never the last
 * on-screen frame (AC2.3).
 *
 * Attached by every v1 screen via [rememberMaskEffects]; idempotent —
 * masking twice is a no-op and multiple screens may attach observers.
 */
class MaskLifecycleObserver(private val mask: MaskController) : DefaultLifecycleObserver {
    override fun onStop(owner: LifecycleOwner) {
        mask.mask(MaskReason.BACKGROUNDED)
    }
}

/**
 * Session-driven re-masks: logout, identity change and session expiry all
 * flip the flag back to masked the instant the authenticated user id becomes
 * null or changes (docs/53 AC2.3). A silent same-identity refresh keeps the
 * id stable, so the user's in-session reveal survives it (AC2.2).
 *
 * Called from composition so it always runs on the main thread, like every
 * other compose-state write. Signed-out at first composition is a no-op in
 * effect — the controller already boots masked.
 */
@Composable
fun MaskAuthEffects(
    mask: MaskController,
    authenticatedUserId: Int?,
    explicitLogout: Boolean = false,
) {
    var lastUserId by remember { mutableStateOf(authenticatedUserId) }
    LaunchedEffect(authenticatedUserId) {
        when {
            authenticatedUserId == null -> {
                if (lastUserId != null) {
                    mask.mask(if (explicitLogout) MaskReason.LOGOUT else MaskReason.SESSION_EXPIRED)
                }
            }
            lastUserId != null && lastUserId != authenticatedUserId ->
                mask.mask(MaskReason.IDENTITY_CHANGED)
        }
        lastUserId = authenticatedUserId
    }
}
