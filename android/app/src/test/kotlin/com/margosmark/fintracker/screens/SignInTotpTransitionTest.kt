package com.margosmark.fintracker.screens

import androidx.compose.runtime.collectAsState
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.margosmark.fintracker.FlushesBeforeWaitRule
import com.margosmark.fintracker.session.AuthState
import com.margosmark.fintracker.ui.screens.SectionTags
import com.margosmark.fintracker.ui.screens.SignInActions
import com.margosmark.fintracker.ui.screens.SignInScreen
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlinx.coroutines.flow.MutableStateFlow
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/**
 * Sign-in state-machine pins (t_696bc09d, docs/52 §Sign-in form state).
 *
 * The screen ends a round trip in exactly two ways — `onFailure`, or the
 * auth layer ANSWERING with a state change (a success answer is never a
 * callback). Every pin below drives one of those seams with a hand-held
 * [SignInActions] fake; the wire-level flow lives in [SignInFlowWiringTest].
 *
 * The P0: before this card the in-flight flag was a boolean cleared ONLY by
 * `onFailure`, so the password step's success answer (`TotpRequired` via the
 * session listener) left the form disabled behind "Checking…" forever and
 * the user could never type a code.
 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class SignInTotpTransitionTest {

    @get:Rule
    val rule = FlushesBeforeWaitRule(createComposeRule())

    /** Hands every callback to the test instead of answering it. */
    private class HeldSignInActions : SignInActions {
        val signInCalls = java.util.Collections.synchronizedList(mutableListOf<String>())
        val totpCalls = java.util.Collections.synchronizedList(mutableListOf<String>())
        @Volatile
        var pendingFailure: ((String) -> Unit)? = null

        override fun signIn(email: String, password: String, onFailure: (String) -> Unit) {
            signInCalls += email
            pendingFailure = onFailure
        }

        override fun submitTotp(code: String, onFailure: (String) -> Unit) {
            totpCalls += code
            pendingFailure = onFailure
        }

        override fun exploreDemo() = Unit
    }

    private fun typeInto(tag: String, text: String) {
        rule.onNodeWithTag(tag).performClick()
        rule.onNodeWithTag(tag).performTextInput(text)
    }

    /** The session listener answers from a background thread, as production does. */
    private fun answerFromBackgroundThread(auth: MutableStateFlow<AuthState>, state: AuthState) {
        Thread { auth.value = state }.also { it.start(); it.join() }
    }

    private fun failFromBackgroundThread(actions: HeldSignInActions, message: String) {
        val onFailure = assertNotNull(actions.pendingFailure, "an attempt must be in flight first")
        Thread { onFailure(message) }.also { it.start(); it.join() }
    }

    // ---- P0: password -> TOTP transition ---------------------------------

    @Test
    fun `password answer hands the authenticator step an ENABLED code input - no stuck Checking`() {
        val auth = MutableStateFlow<AuthState>(AuthState.SignedOut)
        val actions = HeldSignInActions()
        rule.setContent {
            SignInScreen(authState = auth.collectAsState().value, actions = actions)
        }

        typeInto(SectionTags.EMAIL_FIELD, "user@synthetic.local")
        typeInto(SectionTags.PASSWORD_FIELD, "pw-synthetic")
        rule.onNodeWithTag(SectionTags.SIGN_IN_BUTTON).performClick()

        // The password round trip IS in flight: form disabled, honest label,
        // exactly one attempt issued.
        rule.awaitText("Signing in…")
        rule.onNodeWithTag(SectionTags.EMAIL_FIELD).assertIsNotEnabled()
        rule.onNodeWithTag(SectionTags.SIGN_IN_BUTTON).assertTextEquals("Signing in…")
        rule.runOnIdle {
            assertEquals(1, actions.signInCalls.size, "exactly one sign-in attempt")
        }

        // The SUCCESS answer: a challenge through the session listener. There
        // is no onFailure callback on this path — the screen must release the
        // form by observing the state change itself.
        answerFromBackgroundThread(auth, AuthState.TotpRequired("challenge-1", 300))

        rule.awaitText("Enter the 6-digit code from your authenticator app.")
        rule.onNodeWithTag(SectionTags.TOTP_FIELD).assertIsEnabled()
        rule.onNodeWithTag(SectionTags.TOTP_SUBMIT).assertTextEquals("Submit code")
        rule.onNodeWithText("Checking…").assertDoesNotExist()

        // ...and the released form is actually typeable and submittable.
        typeInto(SectionTags.TOTP_FIELD, "123456")
        rule.onNodeWithTag(SectionTags.TOTP_SUBMIT).assertIsEnabled()
    }

    // ---- in-flight discipline on the authenticator step -------------------

    @Test
    fun `totp submit disables only while in flight - failure re-enables with the honest message`() {
        val auth = MutableStateFlow<AuthState>(AuthState.TotpRequired("challenge-1", 300))
        val actions = HeldSignInActions()
        rule.setContent {
            SignInScreen(authState = auth.collectAsState().value, actions = actions)
        }

        // Initial pin: before any submit the code input is ENABLED.
        rule.onNodeWithTag(SectionTags.TOTP_FIELD).assertIsEnabled()
        rule.awaitText("Submit code")

        typeInto(SectionTags.TOTP_FIELD, "000000")
        rule.onNodeWithTag(SectionTags.TOTP_SUBMIT).assertIsEnabled()
        rule.onNodeWithTag(SectionTags.TOTP_SUBMIT).performClick()

        // In flight: both controls disabled — a duplicate submit cannot be
        // issued (a disabled Compose button delivers no click).
        rule.awaitText("Checking…")
        rule.onNodeWithTag(SectionTags.TOTP_FIELD).assertIsNotEnabled()
        rule.onNodeWithTag(SectionTags.TOTP_SUBMIT).assertIsNotEnabled()
        try {
            rule.onNodeWithTag(SectionTags.TOTP_SUBMIT).performClick()
        } catch (_: Throwable) {
            // A disabled node rejecting the gesture is itself the guard.
        }
        rule.runOnIdle {
            assertEquals(1, actions.totpCalls.size, "no duplicate submit while in flight")
        }

        // The failure lands from the pool thread (production shape).
        failFromBackgroundThread(
            actions,
            "That code didn't work. Check your authenticator app and try again.",
        )

        rule.awaitText("That code didn't work. Check your authenticator app and try again.")
        rule.onNodeWithTag(SectionTags.TOTP_FIELD).assertIsEnabled()
        rule.onNodeWithTag(SectionTags.TOTP_SUBMIT).assertTextEquals("Submit code")
    }

    // ---- cancellation / state cycle ---------------------------------------

    @Test
    fun `a cancelled challenge never wedges the form - a late failure stays honest`() {
        val auth = MutableStateFlow<AuthState>(AuthState.SignedOut)
        val actions = HeldSignInActions()
        rule.setContent {
            SignInScreen(authState = auth.collectAsState().value, actions = actions)
        }

        typeInto(SectionTags.EMAIL_FIELD, "user@synthetic.local")
        typeInto(SectionTags.PASSWORD_FIELD, "pw-synthetic")
        rule.onNodeWithTag(SectionTags.SIGN_IN_BUTTON).performClick()
        rule.awaitText("Signing in…")

        answerFromBackgroundThread(auth, AuthState.TotpRequired("challenge-1", 300))
        rule.awaitText("Enter the 6-digit code from your authenticator app.")
        rule.onNodeWithTag(SectionTags.TOTP_FIELD).assertIsEnabled()

        // The challenge is cancelled server-side (expired/revoked): the auth
        // layer reverts to SignedOut while the password attempt's in-flight
        // marker is still parked. The password step must come back ENABLED —
        // never disabled behind a stale "Signing in…".
        answerFromBackgroundThread(auth, AuthState.SignedOut)
        rule.awaitText("Email")
        rule.onNodeWithTag(SectionTags.EMAIL_FIELD).assertIsEnabled()
        rule.onNodeWithTag(SectionTags.PASSWORD_FIELD).assertIsEnabled()
        rule.onNodeWithTag(SectionTags.SIGN_IN_BUTTON).assertTextEquals("Sign in")
        rule.onNodeWithTag(SectionTags.SIGN_IN_BUTTON).assertIsEnabled()
        assertFalse(
            rule.textDisplayed("Signing in…"),
            "the cancelled attempt must not leave a busy label behind",
        )

        // A LATE failure from the cancelled attempt surfaces honestly and
        // does not re-disable the recovered form.
        failFromBackgroundThread(
            actions,
            "Couldn't reach FinTracker. Check your connection and try again.",
        )
        rule.awaitText("Couldn't reach FinTracker. Check your connection and try again.")
        rule.onNodeWithTag(SectionTags.EMAIL_FIELD).assertIsEnabled()
        rule.onNodeWithTag(SectionTags.SIGN_IN_BUTTON).assertTextEquals("Sign in")
    }
}
