package com.margosmark.fintracker.state

import androidx.compose.material3.LocalTextStyle
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.buildAnnotatedString

/**
 * Stable bullet mask used everywhere a value or label is hidden. The same
 * string masks values AND labels (hard boundary from docs/48/49: masking
 * covers labels as well as values).
 */
const val MASK_BULLETS = "\u2022\u2022\u2022\u2022"

/**
 * Renders [text] as bullets when [masked] is true. The masked branch builds
 * its [AnnotatedString] from [MASK_BULLETS] only — the raw [text] never
 * enters the composition's semantics, so no digit can leak into TalkBack,
 * the semantics tree, or test assertions.
 *
 * A fixed-length bullet run (not variable dots) keeps row layout from
 * revealing digit count.
 */
@Composable
fun MaskedText(
    text: String,
    masked: Boolean,
    modifier: Modifier = Modifier,
    testTagValue: String = "masked_text",
) {
    val display: AnnotatedString = if (masked) {
        buildAnnotatedString { append(MASK_BULLETS) }
    } else {
        buildAnnotatedString { append(text) }
    }
    Text(
        text = display,
        modifier = modifier.testTag(testTagValue),
        style = LocalTextStyle.current,
    )
}
