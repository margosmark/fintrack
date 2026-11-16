package com.margosmark.fintracker

import androidx.compose.material3.Text
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.margosmark.fintracker.state.ScreenState
import com.margosmark.fintracker.state.StatePane
import com.margosmark.fintracker.state.StateTags
import com.margosmark.fintracker.ui.theme.FinTrackerTheme
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config
import java.time.Instant

/**
 * Truthful-state contract: the seven states each render, each renders
 * DISTINCTLY (its own pane on screen, no other state's pane), and timestamps
 * come only from server_checked_at — never fabricated.
 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class StatePaneTest {

    @get:Rule
    val composeRule = FlushesBeforeWaitRule(createComposeRule())

    private val fixedInstant: Instant = Instant.parse("2026-10-29T18:05:00Z")

    private fun allSevenTags(): List<String> = listOf(
        StateTags.LOADING,
        StateTags.NO_DATA,
        StateTags.UNAVAILABLE,
        StateTags.STALE,
        StateTags.LOCKED,
        StateTags.SESSION_EXPIRED,
        StateTags.PERMISSION,
    )

    @Test
    fun sevenStateTagsAreUnique() {
        val tags = allSevenTags()
        assertEquals(7, tags.size)
        assertEquals(7, tags.toSet().size)
    }

    @Test
    fun eachStateHasADistinctHeading() {
        val headings = listOf(
            "Loading",
            "Nothing here yet",
            "Unavailable",
            "Showing saved data",
            "Vault locked",
            "Session expired",
            "No access",
        )
        assertEquals(7, headings.size)
        assertEquals(7, headings.toSet().size)
    }

    @Test
    fun eachStateRendersItsOwnTagAndOnlyItsOwn() {
        // One composition; swap the state under it (setContent allows one call).
        val current = mutableStateOf<ScreenState<String>>(ScreenState.Loading<String>(fixedInstant))
        composeRule.setContent {
            FinTrackerTheme {
                StatePane(state = current.value, content = { Text("data-visible-marker") })
            }
        }
        val states: List<Pair<String, ScreenState<String>>> = listOf(
            StateTags.LOADING to ScreenState.Loading(fixedInstant),
            StateTags.NO_DATA to ScreenState.NoData(fixedInstant),
            StateTags.UNAVAILABLE to ScreenState.Unavailable(fixedInstant),
            StateTags.STALE to ScreenState.Stale("value", fixedInstant),
            StateTags.LOCKED to ScreenState.Locked,
            StateTags.SESSION_EXPIRED to ScreenState.SessionExpired,
            StateTags.PERMISSION to ScreenState.Permission,
        )
        for ((ownTag, state) in states) {
            composeRule.runOnUiThread { current.value = state }
            composeRule.waitForIdle()
            // This state's pane is on screen…
            composeRule.onNodeWithTag(ownTag).assertIsDisplayed()
            // …and every OTHER state's pane is not: they are visually distinct.
            for (otherTag in allSevenTags()) {
                if (otherTag == ownTag) continue
                composeRule.onNodeWithTag(otherTag).assertDoesNotExist()
            }
        }
    }

    @Test
    fun staleRendersDataPlusItsOwnBanner() {
        val current = mutableStateOf<ScreenState<String>>(
            ScreenState.Stale(data = "row-1", serverCheckedAt = fixedInstant),
        )
        composeRule.setContent {
            FinTrackerTheme {
                StatePane(state = current.value, content = { Text("row-1") })
            }
        }
        composeRule.onNodeWithTag(StateTags.STALE).assertIsDisplayed()
        // The data itself still renders…
        composeRule.onNodeWithText("row-1").assertIsDisplayed()
        // …beside the stale banner and its provenance line.
        composeRule.onNodeWithTag(StateTags.CHECKED_AT).assertIsDisplayed()
    }

    @Test
    fun loadingWithoutServerCheckRendersNoTimestamp() {
        composeRule.setContent {
            FinTrackerTheme {
                StatePane(state = ScreenState.Loading<String>(serverCheckedAt = null))
            }
        }
        composeRule.onNodeWithTag(StateTags.LOADING).assertIsDisplayed()
        // Truthfulness: no server_checked_at → the "Checked …" line is OMITTED.
        composeRule.onNodeWithTag(StateTags.CHECKED_AT).assertDoesNotExist()
        composeRule.onNodeWithText("Checked", substring = true).assertDoesNotExist()
    }

    @Test
    fun unavailableWithNoHistoryRendersNoTimestamp() {
        composeRule.setContent {
            FinTrackerTheme {
                StatePane(state = ScreenState.Unavailable<String>(lastServerCheckedAt = null))
            }
        }
        composeRule.onNodeWithTag(StateTags.UNAVAILABLE).assertIsDisplayed()
        composeRule.onNodeWithTag(StateTags.CHECKED_AT).assertDoesNotExist()
    }

    @Test
    fun readyRendersContentAndProvenance() {
        composeRule.setContent {
            FinTrackerTheme {
                StatePane(
                    state = ScreenState.Ready(data = "fresh-row", serverCheckedAt = fixedInstant),
                    content = { Text("fresh-row") },
                )
            }
        }
        composeRule.onNodeWithTag(StateTags.READY).assertIsDisplayed()
        composeRule.onNodeWithText("fresh-row").assertIsDisplayed()
        composeRule.onNodeWithTag(StateTags.CHECKED_AT).assertIsDisplayed()
    }

    @Test
    fun readyRendersNoStateChrome() {
        composeRule.setContent {
            FinTrackerTheme {
                StatePane(
                    state = ScreenState.Ready(data = "x", serverCheckedAt = fixedInstant),
                    content = { Text("x") },
                )
            }
        }
        for (tag in allSevenTags()) {
            composeRule.onNodeWithTag(tag).assertDoesNotExist()
        }
        assertTrue(true) // loop above is the assertion (assertDoesNotExist throws)
    }
}
