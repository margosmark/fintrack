package com.margosmark.fintracker.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CloudOff
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.NewReleases
import androidx.compose.material.icons.filled.Flag
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Schedule
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material.icons.filled.VisibilityOff
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import com.margosmark.fintracker.state.MaskedText
import com.margosmark.fintracker.state.ScreenState
import com.margosmark.fintracker.state.StatePane
import com.margosmark.fintracker.state.formatServerCheckedAt
import com.margosmark.fintracker.ui.masking.MaskController
import java.time.Instant
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale

/**
 * Formatting helpers. All formatting is presentation only; every input is a
 * server string (or the client-observed receipt instant) and nothing is
 * derived from the wall clock at render time (docs/53 AC4.1).
 */
object ScreenFormat {

    private val dayFmt: DateTimeFormatter =
        DateTimeFormatter.ofPattern("MMM d, yyyy", Locale.US)

    /** ISO date (`2026-10-24`) → `Oct 24, 2026`; unparsable input passes through VERBATIM. */
    fun serverDate(raw: String?): String? {
        if (raw.isNullOrBlank()) return null
        return try {
            dayFmt.format(LocalDate.parse(raw))
        } catch (_: Exception) {
            raw
        }
    }

    /**
     * ISO datetime (`2026-10-29T18:22:31Z`) → `Oct 29, 18:22` in the device
     * zone via the shell's ONE timestamp formatter; unparsable input passes
     * through VERBATIM (never dropped — a value we cannot pretty-print is
     * still shown, hiding it would be the dishonest option).
     */
    fun serverInstant(raw: String?): String? {
        if (raw.isNullOrBlank()) return null
        val instant = try {
            Instant.parse(raw)
        } catch (_: Exception) {
            try {
                OffsetDateTime.parse(raw).toInstant()
            } catch (_: Exception) {
                return raw
            }
        }
        return formatServerCheckedAt(instant)
    }

    /**
     * The client-observed receipt label. ALWAYS carries the words
     * "device time" so a reader can never mistake it for a server check
     * (docs/52 §3: client observation must be labelled as one).
     */
    fun received(at: Instant): String = "Received ${formatServerCheckedAt(at)} · device time"

    /** AC1.3's exact fragment for refresh failure: `⟨time⟩ (device time)`. */
    fun receivedShort(at: Instant): String = "${formatServerCheckedAt(at)} (device time)"
}

// ---------------------------------------------------------------------------
// Shared screen chrome
// ---------------------------------------------------------------------------

/** Persistent "Demo · read-only" marker (docs/53 AC5.1). Label + icon. */
@Composable
fun DemoBadge(modifier: Modifier = Modifier) {
    Row(
        modifier = modifier
            .testTag(SectionTags.DEMO_BADGE)
            .semantics { contentDescription = "Demo read-only" },
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        Icon(
            Icons.Default.Lock,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(end = 0.dp),
        )
        Text(
            "Demo · read-only",
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

/**
 * The mask control: label + icon, state obvious without relying on color
 * (docs/48 §P0 mask; docs/53 AC2.1 "label + icon, not color alone").
 * The masked branch never composes the raw figure — [MaskedText] guarantees
 * that at the semantics level.
 */
@Composable
fun MaskToggleButton(
    mask: MaskController,
    modifier: Modifier = Modifier,
) {
    val masked = mask.masked
    val label = if (masked) "Masked" else "Values shown"
    val description = maskActionDescription(masked)
    TextButton(
        onClick = { mask.toggle() },
        modifier = modifier
            .testTag(SectionTags.MASK_TOGGLE)
            .semantics { contentDescription = description },
    ) {
        Icon(
            if (masked) Icons.Default.VisibilityOff else Icons.Default.Visibility,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.primary,
        )
        Text(
            " $label",
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.primary,
        )
    }
}

/** Single-sourced TalkBack wording shared by both mask toggles. */
private fun maskActionDescription(masked: Boolean): String =
    if (masked) {
        "Values are masked. Double tap to show values."
    } else {
        "Values are shown. Double tap to mask values."
    }

/**
 * The app-bar privacy action: an icon-only, EXPLICITLY 48dp toggle
 * (docs/56 §2.6 — the tap floor for back/eye actions is 44px, and these
 * ship at 48dp so no shared small-button token can undersize them). Same
 * tag and wording as [MaskToggleButton]; a screen composes exactly one of
 * the two, never both.
 */
@Composable
fun AppBarMaskToggle(
    mask: MaskController,
    modifier: Modifier = Modifier,
) {
    val masked = mask.masked
    IconButton(
        onClick = { mask.toggle() },
        modifier = modifier
            .size(48.dp)
            .testTag(SectionTags.MASK_TOGGLE)
            .semantics { contentDescription = maskActionDescription(masked) },
    ) {
        Icon(
            if (masked) Icons.Default.VisibilityOff else Icons.Default.Visibility,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.primary,
        )
    }
}

/** Section/sheet header: title (+ optional subtitle) and the mask control. */
@Composable
fun ScreenHeader(
    title: String,
    mask: MaskController,
    modifier: Modifier = Modifier,
    subtitle: String? = null,
    demo: Boolean = false,
    showMask: Boolean = true,
    trailing: @Composable () -> Unit = {},
) {
    Column(modifier = modifier.fillMaxWidth()) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.fillMaxWidth(),
        ) {
            Column(Modifier.weight(1f)) {
                Text(title, style = MaterialTheme.typography.titleLarge)
                if (subtitle != null) {
                    Text(
                        subtitle,
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            trailing()
            // A screen that owns its privacy action higher up (the
            // account-detail app bar, docs/56 §2.6) passes showMask=false so
            // exactly ONE mask toggle exists on the screen — this one scrolls
            // away with the header, the app-bar one never does.
            if (showMask) {
                MaskToggleButton(mask)
            }
        }
        if (demo) {
            DemoBadge(Modifier.padding(top = 4.dp))
        }
    }
}

/**
 * A monetary amount: the server's own string, bullet-masked when masked,
 * with the server's currency code shown beside it (currency is metadata,
 * not a value — matches docs/48's `••••• CAD` rows).
 */
@Composable
fun AmountText(
    amount: String,
    currency: String?,
    masked: Boolean,
    modifier: Modifier = Modifier,
    testTagAmount: String = "amount",
) {
    Row(modifier = modifier) {
        MaskedText(
            text = amount,
            masked = masked,
            testTagValue = testTagAmount,
        )
        if (currency != null) {
            Text(
                " $currency",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/** Client-observed provenance line — the ONLY timestamp on data-bearing sections. */
@Composable
fun ReceivedLine(at: java.time.Instant, modifier: Modifier = Modifier) {
    Text(
        ScreenFormat.received(at),
        modifier = modifier.testTag(SectionTags.RECEIVED_AT),
        style = MaterialTheme.typography.labelSmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
    )
}

@Composable
fun RetryButton(onRetry: () -> Unit, modifier: Modifier = Modifier) {
    Button(
        onClick = onRetry,
        modifier = modifier
            .testTag(SectionTags.RETRY)
            .semantics { contentDescription = "Retry" },
    ) {
        Icon(Icons.Default.Refresh, contentDescription = null)
        Text(" Retry")
    }
}

// ---------------------------------------------------------------------------
// Distinct state renders (docs/53 AC1.3) — every state has its own tag
// ---------------------------------------------------------------------------

@Composable
private fun RefreshFailedBanner(state: SectionState.RefreshFailed<*>, onRetry: () -> Unit) {
    Surface(
        color = MaterialTheme.colorScheme.error.copy(alpha = 0.10f),
        shape = MaterialTheme.shapes.small,
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp)
            .testTag(SectionTags.REFRESH_FAILED),
    ) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Icon(Icons.Default.NewReleases, contentDescription = null, tint = MaterialTheme.colorScheme.error)
                Text(
                    if (state.offline) {
                        "Couldn't update · no connection · showing data from ${ScreenFormat.receivedShort(state.receivedAt)}"
                    } else {
                        "Couldn't update · showing data from ${ScreenFormat.receivedShort(state.receivedAt)}"
                    },
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
            RetryButton(onRetry)
        }
    }
}

@Composable
private fun RefreshingBanner() {
    Surface(
        color = MaterialTheme.colorScheme.surfaceVariant,
        shape = MaterialTheme.shapes.small,
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp)
            .testTag(SectionTags.REFRESHING),
    ) {
        Row(
            Modifier.padding(12.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Icon(Icons.Default.Refresh, contentDescription = null, tint = MaterialTheme.colorScheme.primary)
            Text("Updating…", style = MaterialTheme.typography.bodyMedium)
        }
    }
}

/**
 * Stale: the SERVER's staleness signal (staleNote is built from
 * `price_age_days`/`price_date` server fields only — no clock math), amber +
 * icon + text, never styled as fresh (AC1.3, AC4.1).
 */
@Composable
private fun StaleBanner(note: String) {
    Surface(
        color = MaterialTheme.colorScheme.tertiary.copy(alpha = 0.12f),
        shape = MaterialTheme.shapes.small,
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp)
            .testTag(SectionTags.STALE),
    ) {
        Row(
            Modifier.padding(12.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Icon(Icons.Default.Schedule, contentDescription = null, tint = MaterialTheme.colorScheme.tertiary)
            Column {
                Text("Showing saved prices", style = MaterialTheme.typography.titleSmall)
                Text(
                    note,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable
private fun OfflinePane(modifier: Modifier, onRetry: () -> Unit) {
    Surface(
        modifier = modifier
            .fillMaxWidth()
            .testTag(SectionTags.OFFLINE),
        color = MaterialTheme.colorScheme.surfaceVariant,
        shape = MaterialTheme.shapes.medium,
    ) {
        Column(
            Modifier.padding(16.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Icon(Icons.Default.CloudOff, contentDescription = null, tint = MaterialTheme.colorScheme.error)
            Text("You're offline", style = MaterialTheme.typography.titleMedium)
            Text(
                "FinTracker needs a connection to show your figures. This app keeps no offline copy of them.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            RetryButton(onRetry)
        }
    }
}

@Composable
private fun NotFoundPane(modifier: Modifier) {
    Surface(
        modifier = modifier
            .fillMaxWidth()
            .testTag(SectionTags.NOT_FOUND),
        color = MaterialTheme.colorScheme.surfaceVariant,
        shape = MaterialTheme.shapes.medium,
    ) {
        Column(
            Modifier.padding(16.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Icon(Icons.Default.Flag, contentDescription = null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
            Text("This item is no longer available", style = MaterialTheme.typography.titleMedium)
            Text(
                "It may have been removed, or it isn't part of your data.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/**
 * Honest empty: names the situation and the real next step. Never a
 * fabricated zero portfolio (docs/53 AC1.3 "No data").
 */
@Composable
private fun NoDataPane(heading: String, body: String, receivedAt: java.time.Instant?, modifier: Modifier) {
    Surface(
        modifier = modifier
            .fillMaxWidth()
            .testTag(SectionTags.NO_DATA),
        color = MaterialTheme.colorScheme.surfaceVariant,
        shape = MaterialTheme.shapes.medium,
    ) {
        Column(
            Modifier.padding(16.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(heading, style = MaterialTheme.typography.titleMedium)
            Text(
                body,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            if (receivedAt != null) ReceivedLine(receivedAt)
        }
    }
}

/**
 * The ONE place a screen maps its [SectionState] to pixels.
 *
 * Rules encoded here:
 *  - Loading renders the shell's loading pane with NO figures (nothing else
 *    is even composed while Loading).
 *  - Every data-bearing state shows the client-observed receipt line, and
 *    refresh failure keeps the figures + ORIGINAL receipt time + Retry.
 *  - Unavailable / Permission / SessionExpired / Loading route through the
 *    shell's [StatePane] so the shared truthful states stay single-sourced;
 *    my states (offline/not-found/no-data/refresh/stale) are additive.
 */
@Composable
fun <T> SectionBody(
    state: SectionState<T>,
    onRetry: () -> Unit,
    modifier: Modifier = Modifier,
    emptyHeading: String = "Nothing here yet",
    emptyBody: String = "Data appears here once it exists in FinTracker.",
    staleNote: (T) -> String? = { null },
    content: @Composable (T) -> Unit = {},
) {
    when (state) {
        SectionState.Loading -> Box(modifier.testTag(SectionTags.LOADING)) {
            StatePane<Nothing>(ScreenState.Loading())
        }
        SectionState.Offline -> OfflinePane(modifier, onRetry)
        SectionState.Unavailable -> Box(modifier.testTag(SectionTags.UNAVAILABLE)) {
            StatePane<Nothing>(ScreenState.Unavailable())
        }
        SectionState.NotFound -> NotFoundPane(modifier)
        SectionState.Expired -> Box(modifier.testTag(SectionTags.EXPIRED)) {
            StatePane(ScreenState.SessionExpired)
        }
        is SectionState.Permission -> Column(modifier.testTag(SectionTags.PERMISSION)) {
            StatePane(ScreenState.Permission)
            if (state.detail != null) {
                Text(
                    state.detail,
                    modifier = Modifier.padding(top = 4.dp),
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        is SectionState.NoData -> NoDataPane(
            heading = emptyHeading,
            body = emptyBody,
            receivedAt = state.receivedAt,
            modifier = modifier,
        )
        is SectionState.Ready -> Column(modifier.testTag(SectionTags.READY)) {
            ReceivedLine(state.receivedAt)
            content(state.data)
        }
        is SectionState.Refreshing -> Column(modifier) {
            RefreshingBanner()
            ReceivedLine(state.receivedAt)
            content(state.data)
        }
        is SectionState.RefreshFailed -> Column(modifier) {
            RefreshFailedBanner(state, onRetry)
            ReceivedLine(state.receivedAt)
            content(state.data)
        }
        is SectionState.Stale -> Column(modifier) {
            StaleBanner(state.staleNote)
            ReceivedLine(state.receivedAt)
            content(state.data)
        }
    }
}

/** Small helper for detail rows: label left, value right (masked when masked). */
@Composable
fun DetailRow(
    label: String,
    value: String,
    modifier: Modifier = Modifier,
    masked: Boolean = false,
) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            label,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.weight(1f),
        )
        MaskedText(text = value, masked = masked, testTagValue = "detail_value")
    }
}

/** Chip-style label (source, status, review state) — text carries meaning, never color alone. */
@Composable
fun StatusChip(text: String, modifier: Modifier = Modifier, emphasize: Boolean = false) {
    Surface(
        modifier = modifier,
        color = if (emphasize) {
            MaterialTheme.colorScheme.tertiary.copy(alpha = 0.15f)
        } else {
            MaterialTheme.colorScheme.surfaceVariant
        },
        shape = MaterialTheme.shapes.small,
    ) {
        Text(
            text,
            modifier = Modifier.padding(horizontal = 8.dp, vertical = 4.dp),
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

/** Staleness note built ONLY from server fields — no clock math (AC4.1). */
fun positionStaleNote(rows: List<com.margosmark.fintracker.finance.PositionRow>): String? {
    val priced = rows.filter { it.priceStale == true && it.priceDate != null }
    if (priced.isEmpty()) return null
    val ages = priced.mapNotNull { it.priceAgeDays }
    val asOf = ScreenFormat.serverDate(priced.maxOf { it.priceDate ?: "" })
    val ageText = ages.maxOrNull()?.let { "$it days old" }
    return listOfNotNull(ageText, asOf?.let { "as of $it" }).joinToString(" · ")
}
