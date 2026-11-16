package com.margosmark.fintracker

import androidx.compose.runtime.Composable
import androidx.compose.runtime.snapshots.Snapshot
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.SemanticsNodeInteraction
import androidx.compose.ui.test.SemanticsNodeInteractionCollection
import androidx.compose.ui.test.junit4.ComposeContentTestRule

/**
 * Applies any orphaned global-snapshot writes. MUST be called from the JUnit main thread
 * only: on main it is serialized with composition and frames (the composition itself advances
 * the global snapshot the same way at frame boundaries), and apply-observers that touch views
 * run where view access is legal. The AppNotIdleException flake family (t_2e96b89e) happened
 * because background-pool writers (`SectionPresenter.submit` on `fintracker-section-N`) leave
 * `Snapshot.hasPendingChanges()` true while nothing advances the global snapshot, and the next
 * wait's `isIdleNow` then burns its 60 s budget (see SnapshotFlushApp for the full chain).
 */
internal fun flushOrphanedSnapshotWrites() {
    try {
        Snapshot.sendApplyNotifications()
    } catch (_: Throwable) {
        // best-effort: a failure surfaces as the original AppNotIdle, never worse
    }
}

/**
 * `createComposeRule()` wrapper that flushes orphaned snapshot writes before anything that
 * can enter compose/espresso idleness checks (t_2e96b89e). Every flush runs on the JUnit main
 * thread, so it cannot race a frame or touch views from the wrong thread — the failure modes
 * that made background-thread flushes break StatePaneTest/MaskingTest/DistinctStatesTest.
 *
 * Coverage: `onNode*`/`onAllNodes*` (all await/assert extensions dispatch through these),
 * `waitUntil` (also flushed on every poll iteration, since fetches can complete mid-wait),
 * `waitForIdle`, `runOnIdle`, `runOnUiThread`, `setContent`.
 */
class FlushesBeforeWaitRule(
    private val delegate: ComposeContentTestRule,
) : ComposeContentTestRule by delegate {

    override fun setContent(content: @Composable () -> Unit) {
        flushOrphanedSnapshotWrites()
        delegate.setContent(content)
    }

    override fun waitForIdle() {
        flushOrphanedSnapshotWrites()
        delegate.waitForIdle()
    }

    override fun waitUntil(timeoutMillis: Long, condition: () -> Boolean) {
        flushOrphanedSnapshotWrites()
        delegate.waitUntil(timeoutMillis) {
            flushOrphanedSnapshotWrites()
            condition()
        }
    }

    override fun <T> runOnIdle(action: () -> T): T {
        flushOrphanedSnapshotWrites()
        return delegate.runOnIdle(action)
    }

    override fun <T> runOnUiThread(action: () -> T): T {
        flushOrphanedSnapshotWrites()
        return delegate.runOnUiThread(action)
    }

    override fun onNode(matcher: SemanticsMatcher, useUnmergedTree: Boolean): SemanticsNodeInteraction {
        flushOrphanedSnapshotWrites()
        return delegate.onNode(matcher, useUnmergedTree)
    }

    override fun onAllNodes(matcher: SemanticsMatcher, useUnmergedTree: Boolean): SemanticsNodeInteractionCollection {
        flushOrphanedSnapshotWrites()
        return delegate.onAllNodes(matcher, useUnmergedTree)
    }
}
