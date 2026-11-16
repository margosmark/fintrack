package com.margosmark.fintracker.screens

import androidx.compose.material3.Text
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.margosmark.fintracker.FlushesBeforeWaitRule
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/**
 * Regression for the t_91666519 attribution fix on `boundedObserve`.
 *
 * A transient exception thrown *by* the condition of a bounded data-wait must not abort
 * the wait. Before the fix it escaped `waitUntil`, `observe(...)` returned false on the
 * spot, and the test failed at its render assertion with the exact signature of a 15 s
 * timeout it had never waited for — which is what made the reported
 * `ac1_3_expiryOnAMultiArgRoute` residual undiagnosable.
 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class BoundedObserveTest {

    @get:Rule
    val composeRule = FlushesBeforeWaitRule(createComposeRule())

    @Test
    fun aTransientConditionFaultDoesNotAbortTheWait() {
        composeRule.setContent { Text("ready") }
        var faultsLeft = 1
        val held = composeRule.boundedObserve("probe_transient_fault", 10_000) {
            if (faultsLeft-- > 0) error("qa: synthetic condition fault")
            true
        }
        assertTrue("the wait must retry past a transient condition fault", held)
    }

    @Test
    fun aConditionThatNeverHoldsFailsAtTheDeadline() {
        composeRule.setContent { Text("ready") }
        val held = composeRule.boundedObserve("probe_never_holds", 300) { false }
        assertFalse("a condition that never holds must fail at its deadline", held)
    }
}
