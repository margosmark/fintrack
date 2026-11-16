package com.margosmark.fintracker

import androidx.compose.ui.semantics.SemanticsNode
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.getOrNull
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.margosmark.fintracker.state.MASK_BULLETS
import com.margosmark.fintracker.state.MaskedText
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/**
 * Masking contract (docs/48/49 hard boundary): masked values render the
 * stable bullet mask, and no digit of the underlying value leaks — not in
 * the visible text, not in the semantics tree (TalkBack reads this).
 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class MaskedTextTest {

    @get:Rule
    val composeRule = FlushesBeforeWaitRule(createComposeRule())

    /** Sample value with distinctive digits that must never appear masked. */
    private val rawValue = "1234567.89"

    @Test
    fun maskedRendersBullets() {
        composeRule.setContent {
            MaskedText(text = rawValue, masked = true)
        }
        composeRule.onNodeWithText(MASK_BULLETS).assertExists()
        composeRule.onNodeWithText(rawValue).assertDoesNotExist()
    }

    @Test
    fun maskedLeaksNoDigitsInSemanticsTree() {
        composeRule.setContent {
            MaskedText(text = rawValue, masked = true, testTagValue = "mask_probe")
        }
        composeRule.onNodeWithTag("mask_probe").assertExists()
        val dumped = dumpAllSemanticsText("mask_probe")
        assertTrue("mask bullets missing from semantics: $dumped", dumped.contains(MASK_BULLETS))
        for (ch in rawValue) {
            if (ch.isDigit()) {
                assertFalse(
                    "digit '$ch' leaked into semantics while masked: $dumped",
                    dumped.contains(ch),
                )
            }
        }
    }

    @Test
    fun unmaskedShowsTheValue() {
        composeRule.setContent {
            MaskedText(text = rawValue, masked = false)
        }
        composeRule.onNodeWithText(rawValue).assertExists()
        composeRule.onNodeWithText(MASK_BULLETS).assertDoesNotExist()
    }

    @Test
    fun maskCoversLabelsAsWellAsValues() {
        // Hard boundary: labels are masked by the same primitive.
        val label = "RRSP account 9876"
        composeRule.setContent {
            MaskedText(text = label, masked = true, testTagValue = "mask_label")
        }
        composeRule.onNodeWithTag("mask_label").assertExists()
        val dumped = dumpAllSemanticsText("mask_label")
        assertTrue(dumped.contains(MASK_BULLETS))
        assertFalse("label digits leaked while masked: $dumped", dumped.contains("9876"))
        assertFalse("label word leaked while masked: $dumped", dumped.contains("RRSP"))
    }

    /**
     * Collects every Text semantics property at and below the tagged node —
     * the exact strings a screen reader would announce.
     */
    private fun dumpAllSemanticsText(tag: String): String {
        val texts = mutableListOf<String>()
        fun walk(node: SemanticsNode) {
            node.config.getOrNull(SemanticsProperties.Text)?.forEach { texts.add(it.text) }
            node.children.forEach { walk(it) }
        }
        walk(composeRule.onNodeWithTag(tag).fetchSemanticsNode())
        return texts.joinToString("|")
    }
}
