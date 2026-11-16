package com.margosmark.fintracker.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import com.margosmark.fintracker.navigation.ScreenTags
import com.margosmark.fintracker.session.AuthState
import com.margosmark.fintracker.ui.masking.MaskController

/**
 * Settings (docs/53 AC2.3/AC5.1): the masking control at full size, the
 * demo marker, session facts, sign-out, and the honesty notes — no domain
 * figures, therefore nothing to mask beyond what the header already shows.
 */
@Composable
fun SettingsScreen(
    mask: MaskController,
    demo: Boolean,
    modifier: Modifier = Modifier,
    authState: AuthState = AuthState.SignedOut,
    onSignOut: () -> Unit = {},
) {
    val masked = mask.masked
    Column(
        modifier = modifier
            .fillMaxSize()
            .testTag(ScreenTags.SETTINGS)
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        ScreenHeader(title = "Settings", mask = mask, demo = demo)
        if (authState !is AuthState.Authenticated) {
            SectionBody(state = SectionState.Expired, onRetry = {})
            return@Column
        }

        SectionCaption("Session")
        DetailRow(label = "Signed in as", value = authState.user.email)
        DetailRow(label = "Mode", value = if (authState.isDemo) "Demo · read-only" else "Read-only")
        DetailRow(
            label = "Masking",
            value = if (masked) "On (double tap a figure's toggle to show)" else "Off (double tap to mask)",
        )

        Text(
            "Masking hides figures on this device's screen only. Values always travel from the server as they are.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(
            "This app keeps no offline copy of your figures and never changes your data.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        Button(
            onClick = onSignOut,
            modifier = Modifier
                .fillMaxWidth()
                .testTag(SectionTags.SIGN_OUT)
                .semantics { contentDescription = "Sign out" },
        ) {
            Text("Sign out")
        }
    }
}
