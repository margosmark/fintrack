package com.margosmark.fintracker

import android.app.Application
import androidx.compose.runtime.snapshots.Snapshot

/**
 * Test-only Application (wired via src/test/resources/robolectric.properties `application=`).
 * The app has no custom Application class in main, so this subclass is behaviourally identical
 * to the default one except that it repairs an orphaned global-snapshot write at test start.
 *
 * WHY THIS EXISTS (t_2e96b89e, AppNotIdleException flake family): `SectionPresenter.submit`
 * completes on the `fintracker-section-N` pool and writes compose state into the GLOBAL
 * snapshot from a background thread. While nothing advances the global snapshot,
 * `hasPendingChanges()` stays true; a wait that starts with it true (`RobolectricIdlingStrategy`
 * / Espresso idleness checks, ComposeUiTest.android.kt:326 pre-body) burns its 60 s budget and
 * throws `AppNotIdleException`, cascading to every later compose test in the JVM. Evidence:
 * probe-full5 ring dumps show the last global write before each freeze is always
 * `thread=fintracker-section-N ... SectionPresenter.submit$lambda$4` with apply counters
 * frozen; [IDLE-DIAG] at burn time shows A=0/R=0/no roots, only the snapshot flag stuck.
 * The apply itself needs a composition frame or an explicit `sendApplyNotifications()`;
 * `Recomposer.awaitWorkAvailable()` parks when nothing invalidates, so an orphan that lands
 * while the composition sleeps never self-clears (the apply deadlock).
 *
 * Robolectric creates a fresh Application per test, so onCreate runs on the main thread BEFORE
 * the test body — the one safe global repair point outside waits. All OTHER repair points are
 * the FlushesBeforeWaitRule hooks (also main thread). Background-thread flushes were tried and
 * are unsafe: apply-observers touch view hierarchies (ViewRootImpl$CalledFromWrongThread) and
 * race in-flight frames ("component is not displayed", "expiry must re-mask" assertion
 * breakage). Handler.post-to-main does not work either: under Robolectric's paused looper a
 * background post never executes inside Espresso.onIdle()'s drain (probe-dg: zero executions).
 */
class SnapshotFlushApp : Application() {
    override fun onCreate() {
        super.onCreate()
        // Main thread, no composition yet -> always safe.
        flushOrphanedSnapshotWrites()
    }
}
