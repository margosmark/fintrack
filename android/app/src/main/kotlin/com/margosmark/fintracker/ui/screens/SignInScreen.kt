package com.margosmark.fintracker.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.margosmark.fintracker.session.AuthState

/**
 * What the sign-in screen can ask the auth layer to do. Implemented by the
 * wiring over the parent card's [com.margosmark.fintracker.session.AuthSessionManager];
 * faked in tests. Failure messages arrive through [onFailure] and MUST be
 * generic (docs/48 §1.4: never confirm whether an account exists).
 */
interface SignInActions {
    fun signIn(email: String, password: String, onFailure: (String) -> Unit)
    fun submitTotp(code: String, onFailure: (String) -> Unit)
    fun exploreDemo()
}

/**
 * The calm sign-in screen (docs/53 AC1.3 "session-expired" + docs/48
 * §1.4). It renders from [AuthState] — TOTP_REQUIRED flips it to the code
 * form, Loading disables every button (no double submits), and the
 * intended destination is shown as an internal label only (revalidated
 * after re-auth by the navigation wiring).
 */
@Composable
fun SignInScreen(
    authState: AuthState,
    actions: SignInActions,
    modifier: Modifier = Modifier,
    returnToLabel: String? = null,
    /** Honest offline-logout disclosure from the wiring (docs/52 §2 rec 3). */
    notice: String? = null,
) {
    var email by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var code by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }

    // Busy is LOCAL: AuthState has no Loading variant by design (a sign-in
    // attempt is not a session). The flag records WHICH auth state the
    // in-flight request STARTED from; null = nothing in flight.
    //
    // A round trip ends in exactly two ways, and both must release the form:
    //
    //  1. FAILURE — `onFailure` fires exactly once (`fail` below);
    //  2. SUCCESS — never a callback: the password attempt answers as
    //     [AuthState.TotpRequired] and the second factor as
    //     [AuthState.Authenticated], both delivered through the session
    //     listener on a background thread (see `sessionSignInActions`).
    //
    // So the in-flight state is keyed to the auth state captured at click
    // time: the moment `authState` differs, the request is over and the next
    // step renders ENABLED. A bare boolean cleared only by `onFailure` (what
    // shipped before t_696bc09d) rode through the password → TOTP transition
    // and left the authenticator step permanently disabled behind
    // "Checking…" — the user could never type a code.
    var inFlightFrom by remember { mutableStateOf<AuthState?>(null) }
    val showTotp = authState is AuthState.TotpRequired
    val active = inFlightFrom != null && authState == inFlightFrom

    // Belt and braces: drop the marker the moment the state has moved on, so
    // a state that later CYCLES back to the captured value (an expired or
    // cancelled challenge reverting to SignedOut while the password attempt's
    // marker is still parked) can never re-disable an idle form. A state
    // change is also the ANSWER to the step that was in flight, so any
    // step-local error from before it is stale — the transition itself is
    // the honest report (a wrong-code message must not ride along under a
    // freshly committed session). Failure copy that arrives WITHOUT a state
    // change — wrong code, offline, timeout — is set by `fail` after this
    // effect runs and survives.
    LaunchedEffect(authState) {
        if (inFlightFrom != null && authState != inFlightFrom) inFlightFrom = null
        error = null
    }

    fun fail(message: String) {
        inFlightFrom = null
        error = message
    }

    Column(
        modifier = modifier
            .fillMaxSize()
            .testTag(com.margosmark.fintracker.navigation.ScreenTags.SIGN_IN)
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("Sign in", style = MaterialTheme.typography.headlineSmall)
        if (returnToLabel != null) {
            Text(
                "You'll be returned to $returnToLabel after sign-in.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        if (!showTotp) {
            OutlinedTextField(
                value = email,
                onValueChange = { email = it },
                label = { Text("Email") },
                singleLine = true,
                enabled = !active,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Email),
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag(SectionTags.EMAIL_FIELD),
            )
            OutlinedTextField(
                value = password,
                onValueChange = { password = it },
                label = { Text("Password") },
                singleLine = true,
                enabled = !active,
                visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag(SectionTags.PASSWORD_FIELD),
            )
            Button(
                onClick = {
                    inFlightFrom = authState
                    error = null
                    actions.signIn(email.trim(), password) { fail(it) }
                },
                enabled = !active && email.isNotBlank() && password.isNotBlank(),
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag(SectionTags.SIGN_IN_BUTTON)
                    .semantics { contentDescription = "Sign in" },
            ) {
                Text(if (active) "Signing in…" else "Sign in")
            }
            OutlinedButton(
                onClick = { actions.exploreDemo() },
                enabled = !active,
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag(SectionTags.EXPLORE_DEMO)
                    .semantics { contentDescription = "Explore demo" },
            ) {
                Text("Explore demo")
            }
        } else {
            Text(
                "Enter the 6-digit code from your authenticator app.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            OutlinedTextField(
                value = code,
                onValueChange = { code = it },
                label = { Text("Authentication code") },
                singleLine = true,
                enabled = !active,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag(SectionTags.TOTP_FIELD),
            )
            Button(
                onClick = {
                    inFlightFrom = authState
                    error = null
                    actions.submitTotp(code.trim()) { fail(it) }
                },
                enabled = !active && code.isNotBlank(),
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag(SectionTags.TOTP_SUBMIT)
                    .semantics { contentDescription = "Submit code" },
            ) {
                Text(if (active) "Checking…" else "Submit code")
            }
        }

        notice?.let { message ->
            Text(
                message,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.tertiary,
                modifier = Modifier
                    .testTag(SectionTags.LOGOUT_NOTICE)
                    .fillMaxWidth(),
            )
        }

        error?.let { message ->
            Text(
                message,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.error,
                modifier = Modifier
                    .testTag(SectionTags.SIGN_IN_ERROR)
                    .fillMaxWidth(),
            )
        }
    }
}
