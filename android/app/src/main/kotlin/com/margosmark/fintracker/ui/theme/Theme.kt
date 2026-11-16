package com.margosmark.fintracker.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable

// ---------------------------------------------------------------------------
// Material 3 theme built from the committed tokens in Color.kt / Type.kt.
// Follows system light/dark by default (visual-system.md §Dark mode).
// ---------------------------------------------------------------------------

val LightColorScheme = lightColorScheme(
    primary = SignalBlue,
    onPrimary = WhiteSurface,
    secondary = SemanticGreen,
    onSecondary = WhiteSurface,
    tertiary = SemanticAmber,
    onTertiary = WhiteSurface,
    background = PaperBackground,
    onBackground = Ink,
    surface = WhiteSurface,
    onSurface = Ink,
    surfaceVariant = WarmTonalSurface,
    onSurfaceVariant = MutedInk,
    error = SemanticRed,
    onError = WhiteSurface,
    outline = LightOutline,
)

val DarkColorScheme = darkColorScheme(
    primary = NightSignalBlue,
    onPrimary = NightBackground,
    secondary = NightGreen,
    onSecondary = NightBackground,
    tertiary = NightAmber,
    onTertiary = NightBackground,
    background = NightBackground,
    onBackground = NightInk,
    surface = NightSurface,
    onSurface = NightInk,
    surfaceVariant = NightTonalSurface,
    onSurfaceVariant = NightMutedInk,
    error = NightRed,
    onError = NightBackground,
    outline = DarkOutline,
)

@Composable
fun FinTrackerTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    MaterialTheme(
        colorScheme = if (darkTheme) DarkColorScheme else LightColorScheme,
        typography = FinTrackerTypography,
        content = content,
    )
}
