package com.margosmark.fintracker.state

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CloudOff
import androidx.compose.material.icons.filled.ErrorOutline
import androidx.compose.material.icons.filled.HourglassEmpty
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.NoAccounts
import androidx.compose.material.icons.filled.NoMeetingRoom
import androidx.compose.material.icons.filled.Schedule
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp

/**
 * Shared rendering for the seven truthful states. Each state is visually
 * distinct by icon + heading + accent tint, never by color alone (icon and
 * text carry the signal; the tint reinforces it). Screens wrap their real
 * content in [StatePane]; when the state is Ready/Stale the pane renders
 * [content] alongside the state chrome.
 *
 * Test tags are stable API: `state_pane_<name>` — tests assert distinctness
 * through these, not through colors.
 */
object StateTags {
    const val LOADING = "state_pane_loading"
    const val NO_DATA = "state_pane_no_data"
    const val UNAVAILABLE = "state_pane_unavailable"
    const val STALE = "state_pane_stale"
    const val LOCKED = "state_pane_locked"
    const val SESSION_EXPIRED = "state_pane_session_expired"
    const val PERMISSION = "state_pane_permission"
    const val READY = "state_pane_ready"
    const val CHECKED_AT = "state_checked_at"
}

@Composable
fun <T> StatePane(
    state: ScreenState<T>,
    modifier: Modifier = Modifier,
    content: @Composable () -> Unit = {},
) {
    when (state) {
        is ScreenState.Ready -> ReadyPane(state, modifier, content)
        is ScreenState.Stale -> StalePane(state, modifier, content)
        is ScreenState.Loading -> LoadingPane(state, modifier)
        is ScreenState.NoData -> NoDataPane(state, modifier)
        is ScreenState.Unavailable -> UnavailablePane(state, modifier)
        is ScreenState.Locked -> LockedPane(modifier)
        is ScreenState.SessionExpired -> SessionExpiredPane(modifier)
        is ScreenState.Permission -> PermissionPane(modifier)
    }
}

// The accent tints: each state owns a distinct tone. These are structural
// accents (state signaling), NOT financial semantics — signal blue and the
// gain/loss colors never appear here.

@Composable
private fun StateScaffold(
    tag: String,
    icon: ImageVector,
    heading: String,
    body: String,
    accent: Color,
    modifier: Modifier = Modifier,
    serverCheckedAt: java.time.Instant? = null,
) {
    Surface(
        modifier = modifier
            .fillMaxWidth()
            .testTag(tag),
        color = accent.copy(alpha = 0.08f),
        shape = MaterialTheme.shapes.medium,
    ) {
        Column(
            modifier = Modifier.padding(16.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Icon(icon, contentDescription = null, tint = accent)
            Text(heading, style = MaterialTheme.typography.titleMedium)
            Text(
                body,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            // Truthfulness: the timestamp is rendered ONLY when the state
            // carries a real server_checked_at; otherwise the line is absent.
            val checked = serverCheckedLabel(serverCheckedAt)
            if (checked != null) {
                Text(
                    checked,
                    modifier = Modifier.testTag(StateTags.CHECKED_AT),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable
private fun LoadingPane(state: ScreenState.Loading<*>, modifier: Modifier) = StateScaffold(
    tag = StateTags.LOADING,
    icon = Icons.Default.HourglassEmpty,
    heading = "Loading",
    body = "Fetching your data…",
    accent = MaterialTheme.colorScheme.primary,
    modifier = modifier,
    serverCheckedAt = state.serverCheckedAt,
)

@Composable
private fun NoDataPane(state: ScreenState.NoData<*>, modifier: Modifier) = StateScaffold(
    tag = StateTags.NO_DATA,
    icon = Icons.Default.NoAccounts,
    heading = "Nothing here yet",
    body = "Data appears here once it exists in FinTracker.",
    accent = MaterialTheme.colorScheme.onSurfaceVariant,
    modifier = modifier,
    serverCheckedAt = state.serverCheckedAt,
)

@Composable
private fun UnavailablePane(state: ScreenState.Unavailable<*>, modifier: Modifier) = StateScaffold(
    tag = StateTags.UNAVAILABLE,
    icon = Icons.Default.CloudOff,
    heading = "Unavailable",
    body = "Couldn't load right now. Pull to retry when you're ready.",
    accent = MaterialTheme.colorScheme.error,
    modifier = modifier,
    serverCheckedAt = state.lastServerCheckedAt,
)

@Composable
private fun LockedPane(modifier: Modifier) = StateScaffold(
    tag = StateTags.LOCKED,
    icon = Icons.Default.Lock,
    heading = "Vault locked",
    body = "Unlock the vault in Settings to see this data.",
    accent = MaterialTheme.colorScheme.tertiary,
    modifier = modifier,
)

@Composable
private fun SessionExpiredPane(modifier: Modifier) = StateScaffold(
    tag = StateTags.SESSION_EXPIRED,
    icon = Icons.Default.NoMeetingRoom,
    heading = "Session expired",
    body = "Sign in again to continue.",
    accent = MaterialTheme.colorScheme.tertiary,
    modifier = modifier,
)

@Composable
private fun PermissionPane(modifier: Modifier) = StateScaffold(
    tag = StateTags.PERMISSION,
    icon = Icons.Default.NoAccounts,
    heading = "No access",
    body = "This item isn't available for your account.",
    accent = MaterialTheme.colorScheme.error,
    modifier = modifier,
)

/**
 * Fresh-data wrapper: renders [content] plus a subtle "checked" provenance
 * line from the response's server_checked_at. No timestamp is invented —
 * [ScreenState.Ready] requires a real one at construction.
 */
@Composable
private fun <T> ReadyPane(
    state: ScreenState.Ready<T>,
    modifier: Modifier,
    content: @Composable () -> Unit,
) {
    Column(modifier = modifier.testTag(StateTags.READY)) {
        content()
        val checked = serverCheckedLabel(state.serverCheckedAt)
        if (checked != null) {
            Text(
                checked,
                modifier = Modifier
                    .padding(top = 4.dp)
                    .testTag(StateTags.CHECKED_AT),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/**
 * Stale wrapper: renders [content] AS IT WAS plus a distinct banner carrying
 * the age and a schedule icon. Amber + icon + text — never color alone.
 */
@Composable
private fun <T> StalePane(
    state: ScreenState.Stale<T>,
    modifier: Modifier,
    content: @Composable () -> Unit,
) {
    Column(modifier = modifier.testTag(StateTags.STALE)) {
        content()
        Surface(
            color = MaterialTheme.colorScheme.tertiary.copy(alpha = 0.12f),
            shape = MaterialTheme.shapes.small,
            modifier = Modifier
                .fillMaxWidth()
                .padding(top = 8.dp),
        ) {
            Column(Modifier.padding(12.dp)) {
                androidx.compose.foundation.layout.Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Icon(
                        Icons.Default.Schedule,
                        contentDescription = null,
                        tint = MaterialTheme.colorScheme.tertiary,
                    )
                    Text(
                        "Showing saved data",
                        style = MaterialTheme.typography.titleMedium,
                    )
                }
                Text(
                    "Last updated " + (serverCheckedLabel(state.serverCheckedAt) ?: ""),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.testTag(StateTags.CHECKED_AT),
                )
            }
        }
    }
}

// Referenced-but-unused-in-v1 icons kept for parity with the concept frames;
// suppressed to keep the build warning-free.
@Suppress("unused")
private val v1UnusedIcons = listOf(Icons.Default.ErrorOutline, Icons.Default.Info)
