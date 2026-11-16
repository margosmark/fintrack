package com.margosmark.fintracker.ui.theme

import androidx.compose.material3.Typography
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

// ---------------------------------------------------------------------------
// Type scale — design/android-material/visual-system.md:
//   12sp metadata, 14sp body, 16sp section title, 20sp app title,
//   30–32sp key balances. Roboto/system sans (platform default).
// Currency and quantity styles carry tabular lining numerals ("tnum") so
// digits align in columns; masking never depends on font metrics.
// ---------------------------------------------------------------------------

val FinTrackerTypography = Typography(
    // Key balances (30–32sp). Tabular numerals for currency/quantities.
    displaySmall = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.Medium,
        fontSize = 32.sp,
        lineHeight = 40.sp,
        fontFeatureSettings = "tnum",
    ),
    // App title (20sp).
    titleLarge = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.SemiBold,
        fontSize = 20.sp,
        lineHeight = 28.sp,
    ),
    // Section title (16sp).
    titleMedium = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.SemiBold,
        fontSize = 16.sp,
        lineHeight = 24.sp,
    ),
    // Body (14sp).
    bodyMedium = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontSize = 14.sp,
        lineHeight = 20.sp,
    ),
    // Metadata (12sp): timestamps, provenance lines.
    bodySmall = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontSize = 12.sp,
        lineHeight = 16.sp,
    ),
    labelSmall = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontSize = 12.sp,
        lineHeight = 16.sp,
    ),
)
