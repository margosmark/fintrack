package com.margosmark.fintracker.screens

import androidx.compose.runtime.collectAsState
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextClearance
import androidx.compose.ui.test.performTextInput
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.margosmark.fintracker.FlushesBeforeWaitRule
import com.margosmark.fintracker.TestStack
import com.margosmark.fintracker.challengeResponseBody
import com.margosmark.fintracker.errorResponseBody
import com.margosmark.fintracker.session.AuthState
import com.margosmark.fintracker.sessionResponse
import com.margosmark.fintracker.startStack
import com.margosmark.fintracker.ui.screens.SectionTags
import com.margosmark.fintracker.ui.screens.SignInScreen
import com.margosmark.fintracker.ui.sessionSignInActions
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertNull
import kotlin.test.assertTrue
import kotlinx.coroutines.flow.MutableStateFlow
import okhttp3.mockwebserver.MockResponse
import org.junit.After
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/**
 * The sign-in flow over REAL wiring (t_696bc09d, docs/48 §Password → TOTP):
 * [SignInScreen] + the production [sessionSignInActions] over an
 * [com.margosmark.fintracker.session.AuthSessionManager] hitting a local
 * MockWebServer with a SYNTHETIC account — no real credentials, no
 * production host, nothing written anywhere but this JVM.
 *
 * [SignInTotpTransitionTest] pins the screen's state machine against a
 * hand-held fake; this file proves the same pins hold through the code the
 * app actually runs, including the P0: a successful password answer must
 * hand the authenticator step an ENABLED input — never a form wedged behind
 * "Checking…" — while the challenge stays a challenge (no token, no cookie).
 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class SignInFlowWiringTest {

    @get:Rule
    val folder = TemporaryFolder()

    @get:Rule
    val rule = FlushesBeforeWaitRule(createComposeRule())

    private lateinit var stack: TestStack
    private lateinit var executor: ExecutorService

    /** The production listener seam: session state → UI flow (AppScope shape). */
    private val authFlow = MutableStateFlow<AuthState>(AuthState.SignedOut)

    @Before
    fun setUp() {
        stack = startStack(folder, listener = { authFlow.value = it })
        executor = Executors.newSingleThreadExecutor { runnable ->
            Thread(runnable, "sign-in-flow-test").apply { isDaemon = true }
        }
    }

    @After
    fun tearDown() {
        if (this::executor.isInitialized) executor.shutdownNow()
        if (this::stack.isInitialized) stack.close()
    }

    private fun typeInto(tag: String, text: String) {
        rule.onNodeWithTag(tag).performClick()
        rule.onNodeWithTag(tag).performTextInput(text)
    }

    // ---- the P0 flow: password -> editable TOTP -> invalid -> retry -> in --

    @Test
    fun `password to totp to invalid code to retry - succeeds over real HTTP`() {
        val actions = sessionSignInActions(stack.session, executor)
        rule.setContent {
            SignInScreen(authState = authFlow.collectAsState().value, actions = actions)
        }

        // Blank password form: submit gated on input, nothing in flight.
        rule.onNodeWithTag(SectionTags.SIGN_IN_BUTTON).assertIsNotEnabled()

        // The server answers the password step with a challenge.
        stack.server.enqueue(
            MockResponse().setResponseCode(200).setBody(challengeResponseBody("challenge-1", 300)),
        )
        typeInto(SectionTags.EMAIL_FIELD, "user@synthetic.local")
        typeInto(SectionTags.PASSWORD_FIELD, "pw-synthetic")
        rule.onNodeWithTag(SectionTags.SIGN_IN_BUTTON).performClick()

        // A duplicate click during the round trip must not issue a second
        // request (the button is disabled; a disabled node rejects the tap).
        try {
            rule.onNodeWithTag(SectionTags.SIGN_IN_BUTTON).performClick()
        } catch (_: Throwable) {
            // The disabled node rejecting the gesture is itself the guard.
        }

        // P0: the SUCCESS answer has no onFailure callback — the screen must
        // release itself by observing TotpRequired.
        rule.awaitText("Enter the 6-digit code from your authenticator app.")
        rule.onNodeWithTag(SectionTags.TOTP_FIELD).assertIsEnabled()
        rule.onNodeWithTag(SectionTags.TOTP_SUBMIT).assertTextEquals("Submit code")
        rule.onNodeWithText("Checking…").assertDoesNotExist()
        rule.onNodeWithTag(SectionTags.SIGN_IN_BUTTON).assertDoesNotExist()
        rule.runOnIdle {
            assertEquals(1, stack.server.requestCount, "exactly one POST /auth/login")
            assertEquals(
                AuthState.TotpRequired("challenge-1", 300),
                authFlow.value,
                "a challenge is pending, not a session",
            )
            assertNull(stack.client.tokenStore.accessToken, "a challenge stores no token")
            assertTrue(stack.jar.isEmpty(), "a challenge sets no cookie")
        }

        // The wrong code: 401 invalid_totp_code over the wire.
        stack.server.enqueue(errorResponseBody(401, "Invalid or expired code", "invalid_totp_code"))
        typeInto(SectionTags.TOTP_FIELD, "000000")
        rule.onNodeWithTag(SectionTags.TOTP_SUBMIT).performClick()

        // Honest generic copy, the challenge survives, the form re-enables.
        rule.awaitText("That code didn't work. Check your authenticator app and try again.")
        rule.onNodeWithTag(SectionTags.TOTP_FIELD).assertIsEnabled()
        rule.onNodeWithTag(SectionTags.TOTP_SUBMIT).assertTextEquals("Submit code")
        rule.runOnIdle {
            assertEquals(
                AuthState.TotpRequired("challenge-1", 300),
                authFlow.value,
                "a rejected code must not drop or escalate the challenge",
            )
            assertNull(stack.client.tokenStore.accessToken)
            assertEquals(2, stack.server.requestCount, "one login, one code attempt")
        }

        // Retry with the right code: the session commits.
        stack.server.enqueue(
            sessionResponse(accessToken = "access-9", userId = 7, email = "user@synthetic.local"),
        )
        rule.onNodeWithTag(SectionTags.TOTP_FIELD).performTextClearance()
        typeInto(SectionTags.TOTP_FIELD, "123456")
        rule.onNodeWithTag(SectionTags.TOTP_SUBMIT).performClick()
        rule.waitUntil(10_000) { authFlow.value is AuthState.Authenticated }

        rule.runOnIdle {
            val committed = assertIs<AuthState.Authenticated>(authFlow.value)
            assertEquals("user@synthetic.local", committed.user.email, "the second factor commits the session")
            assertEquals("access-9", stack.client.tokenStore.accessToken, "token only after MFA")
            assertEquals(3, stack.server.requestCount, "login, wrong code, right code")
        }
        // The stale step-local error from the rejected attempt is gone with
        // the transition — success reports itself, not the previous step.
        rule.onNodeWithTag(SectionTags.SIGN_IN_ERROR).assertDoesNotExist()
    }

    // ---- offline ----------------------------------------------------------

    @Test
    fun `offline password attempt - honest message and a re-enabled form`() {
        val actions = sessionSignInActions(stack.session, executor)
        rule.setContent {
            SignInScreen(authState = authFlow.collectAsState().value, actions = actions)
        }

        // The server goes away before the attempt: connection refused.
        stack.server.shutdown()
        typeInto(SectionTags.EMAIL_FIELD, "user@synthetic.local")
        typeInto(SectionTags.PASSWORD_FIELD, "pw-synthetic")
        rule.onNodeWithTag(SectionTags.SIGN_IN_BUTTON).performClick()

        rule.awaitText("Couldn't reach FinTracker. Check your connection and try again.")
        rule.onNodeWithTag(SectionTags.EMAIL_FIELD).assertIsEnabled()
        rule.onNodeWithTag(SectionTags.PASSWORD_FIELD).assertIsEnabled()
        rule.onNodeWithTag(SectionTags.SIGN_IN_BUTTON).assertTextEquals("Sign in")
        rule.onNodeWithTag(SectionTags.SIGN_IN_BUTTON).assertIsEnabled()
        rule.runOnIdle {
            assertEquals(AuthState.SignedOut, authFlow.value, "offline changes no state")
            assertNull(stack.client.tokenStore.accessToken)
            assertTrue(stack.jar.isEmpty(), "offline sets no cookie")
        }
    }
}
