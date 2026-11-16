// Visual seed only; not wired into an Android application.
package ca.fintracker.design

import androidx.compose.material3.ColorScheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.ui.graphics.Color

private fun c(hex: Long) = Color(0xFF000000 or hex)

object FinTrackTokens {
    val Light: ColorScheme = lightColorScheme(
        primary = c(0x245DB4), onPrimary = c(0xFFFFFF),
        primaryContainer = c(0xD8E5FF), onPrimaryContainer = c(0x102D55),
        secondary = c(0x4F5F77), onSecondary = c(0xFFFFFF),
        secondaryContainer = c(0xDCE4F1), onSecondaryContainer = c(0x18283D),
        tertiary = c(0x146B4A), onTertiary = c(0xFFFFFF),
        tertiaryContainer = c(0xC5F0D9), onTertiaryContainer = c(0x002114),
        error = c(0xA8322A), onError = c(0xFFFFFF),
        errorContainer = c(0xFFDAD5), onErrorContainer = c(0x410006),
        background = c(0xF6F4EF), onBackground = c(0x171A20),
        surface = c(0xFFFFFF), onSurface = c(0x171A20),
        surfaceVariant = c(0xEEEEE9), onSurfaceVariant = c(0x4B515A),
        outline = c(0x6F757E), outlineVariant = c(0xC5C7C7),
        inverseSurface = c(0x2C3038), inverseOnSurface = c(0xF1F0F5),
        inversePrimary = c(0xB2CAFF), scrim = c(0x000000),
    )

    val Dark: ColorScheme = darkColorScheme(
        primary = c(0xA5C4FF), onPrimary = c(0x142D50),
        primaryContainer = c(0x29364C), onPrimaryContainer = c(0xD8E5FF),
        secondary = c(0xB9C7DF), onSecondary = c(0x263347),
        secondaryContainer = c(0x3A465A), onSecondaryContainer = c(0xDCE4F1),
        tertiary = c(0x73D6A9), onTertiary = c(0x12382A),
        tertiaryContainer = c(0x184D38), onTertiaryContainer = c(0xC5F0D9),
        error = c(0xFFB4AB), onError = c(0x5A100D),
        errorContainer = c(0x7C2D28), onErrorContainer = c(0xFFDAD5),
        background = c(0x111318), onBackground = c(0xEDF0F5),
        surface = c(0x1B1E25), onSurface = c(0xEDF0F5),
        surfaceVariant = c(0x252932), onSurfaceVariant = c(0xC1C6D0),
        outline = c(0x8B919C), outlineVariant = c(0x454A54),
        inverseSurface = c(0xE2E5EC), inverseOnSurface = c(0x292D35),
        inversePrimary = c(0x315FAD), scrim = c(0x000000),
    )

    // High-contrast scheme uses stronger neutral ink/outlines and dark controls.
    val HighContrast: ColorScheme = lightColorScheme(
        primary = c(0x003A7A), onPrimary = c(0xFFFFFF),
        primaryContainer = c(0xD8E5FF), onPrimaryContainer = c(0x001B3D),
        secondary = c(0x303F55), onSecondary = c(0xFFFFFF),
        secondaryContainer = c(0xE1E8F2), onSecondaryContainer = c(0x111F33),
        tertiary = c(0x005333), onTertiary = c(0xFFFFFF),
        tertiaryContainer = c(0xC5F0D9), onTertiaryContainer = c(0x00190F),
        error = c(0x8C1715), onError = c(0xFFFFFF),
        errorContainer = c(0xFFDAD5), onErrorContainer = c(0x410006),
        background = c(0xFFFFFF), onBackground = c(0x000000),
        surface = c(0xFFFFFF), onSurface = c(0x000000),
        surfaceVariant = c(0xF0F0F0), onSurfaceVariant = c(0x202124),
        outline = c(0x3F444B), outlineVariant = c(0x767A80),
        inverseSurface = c(0x202124), inverseOnSurface = c(0xFFFFFF),
        inversePrimary = c(0xA9C5FF), scrim = c(0x000000),
    )
}

// Keep financial-state semantics separate from Material's primary role.
data class FinTrackSemanticColors(
    val positive: Color,
    val negative: Color,
    val warning: Color,
    val onWarning: Color,
    val offlineContainer: Color,
    val onOfflineContainer: Color,
)

val FinTrackLightSemantics = FinTrackSemanticColors(
    positive = Color(0xFF146B4A), negative = Color(0xFFA8322A),
    warning = Color(0xFF765100), onWarning = Color(0xFFFFFFFF),
    offlineContainer = Color(0xFFE8E7E3), onOfflineContainer = Color(0xFF25282D),
)
val FinTrackDarkSemantics = FinTrackSemanticColors(
    positive = Color(0xFF73D6A9), negative = Color(0xFFFFB4AB),
    warning = Color(0xFFF2C66D), onWarning = Color(0xFF342600),
    offlineContainer = Color(0xFF34363A), onOfflineContainer = Color(0xFFF2F2F2),
)
val FinTrackHighContrastSemantics = FinTrackSemanticColors(
    positive = Color(0xFF005333), negative = Color(0xFF8C1715),
    warning = Color(0xFF624100), onWarning = Color(0xFFFFFFFF),
    offlineContainer = Color(0xFFF0F0F0), onOfflineContainer = Color(0xFF000000),
)
