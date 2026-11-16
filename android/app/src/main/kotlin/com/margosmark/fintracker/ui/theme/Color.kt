package com.margosmark.fintracker.ui.theme

import androidx.compose.ui.graphics.Color

// ---------------------------------------------------------------------------
// Design tokens — derived from design/android-material/visual-system.md.
// Signal blue is an INTERACTIVE accent only; it never encodes financial
// meaning (gains/losses use semantic green/red + signed text, never hue
// alone). Commit changes to tokens here, not ad hoc inside screens.
// ---------------------------------------------------------------------------

// Light (Ledger Daylight)
val PaperBackground = Color(0xFFF6F4EF)
val WhiteSurface = Color(0xFFFFFFFF)
val WarmTonalSurface = Color(0xFFECE8DF)
val Ink = Color(0xFF171A20)
val MutedInk = Color(0xFF5D626C)
val SignalBlue = Color(0xFF245DB4)
val SemanticGreen = Color(0xFF146B4A)
val SemanticRed = Color(0xFFA8322A)
val SemanticAmber = Color(0xFF765100)
val LightOutline = Color(0xFFC9C4BA)

// Dark (Ledger Night)
val NightBackground = Color(0xFF111318)
val NightSurface = Color(0xFF1B1E25)
val NightTonalSurface = Color(0xFF262A33)
val NightInk = Color(0xFFEDF0F5)
val NightMutedInk = Color(0xFFA6ACB6)
val NightSignalBlue = Color(0xFFA5C4FF)
val NightGreen = Color(0xFF4CBE8F)
val NightRed = Color(0xFFE5766C)
val NightAmber = Color(0xFFD9A648)
val DarkOutline = Color(0xFF3A3F49)

// State-pane accents (derived tones, both themes). Every truthful-state pane
// gets its own accent so the seven states are distinguishable at a glance;
// the headline text and icon carry the same distinction without color.
val SkeletonLight = Color(0xFFB9B5AC)
val SkeletonDark = Color(0xFF3A3E46)
val PermissionTintLight = Color(0xFFF6D9D6)
val PermissionTintDark = Color(0xFF4A2A26)
