package com.margosmark.fintracker

import android.content.Context
import android.content.pm.PackageManager
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.margosmark.fintracker.session.AuthSessionManager
import com.margosmark.fintracker.session.AuthState
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.test.assertEquals
import kotlin.test.assertTrue
import okhttp3.mockwebserver.MockResponse
import org.junit.After
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/**
 * Boot smoke for the launch path (t_6781e4c5): the FIRST thing the app does
 * on start is `AppScope → AuthSessionManager.restoreSession →
 * ApiClient.refreshTokens` on a pool thread, before any UI renders — so a
 * failure there is a BOOT CRASH (the shipped APK died 2.1 s after launch).
 * Two halves are pinned here:
 *
 *  1. the manifest declares `android.permission.INTERNET` — without it every
 *     DNS attempt throws `SecurityException` at the connect seam;
 *  2. a failed background refresh returns `RestoreOutcome.Unreachable` with
 *     the session still signed out and nothing escaping the pool thread —
 *     the process survives a refresh that cannot succeed.
 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class BootSmokeTest {
    @get:Rule
    val folder = TemporaryFolder()

    private lateinit var stack: TestStack
    private val escaped = CopyOnWriteArrayList<Throwable>()

    @After
    fun tearDown() {
        if (this::stack.isInitialized) stack.close()
    }

    private fun recordingExecutor(): ExecutorService =
        Executors.newSingleThreadExecutor { runnable ->
            Thread(runnable, "fintracker-api-boot").apply {
                setUncaughtExceptionHandler { _, t -> escaped += t }
            }
        }

    /** Bounded wait: an never-completed refresh future would hang the test forever. */
    private fun restoreBounded(): AuthSessionManager.RestoreOutcome {
        val pool = Executors.newSingleThreadExecutor()
        try {
            return pool.submit<AuthSessionManager.RestoreOutcome> { stack.session.restoreSession() }
                .get(10, TimeUnit.SECONDS)
        } finally {
            pool.shutdown()
        }
    }

    @Test
    fun `manifest declares the INTERNET permission`() {
        val context = ApplicationProvider.getApplicationContext<Context>()

        // The DECLARATION is the fix (t_6781e4c5): read it off the merged
        // manifest itself. Robolectric's runtime permission state defaults to
        // DENIED for checkPermission() no matter what the manifest says, so
        // requestedPermissions is the only seam here that observes the
        // declaration (proven by mutation: drop the uses-permission line and
        // this assertion fails).
        val info = context.packageManager.getPackageInfo(context.packageName, PackageManager.GET_PERMISSIONS)
        val declared = info.requestedPermissions?.toList().orEmpty()
        assertTrue(
            android.Manifest.permission.INTERNET in declared,
            "AndroidManifest.xml must declare <uses-permission android:name=\"android.permission.INTERNET\"/>; declared=$declared",
        )
    }

    @Test
    fun `boot restore - a failed background refresh returns Unreachable, stays signed out, kills nothing`() {
        stack = startStack(folder, executor = recordingExecutor())
        // Cold boot: no session cookie yet (Mark's crash was a fresh install).
        // The server answers 200 with an access_token but no `user` object —
        // tokenResponse() throws a NON-IO exception on the pool thread, the
        // same code shape as the boot crash's SecurityException.
        stack.server.enqueue(MockResponse().setResponseCode(200).setBody("""{"access_token":"x"}"""))

        val outcome = restoreBounded()

        assertEquals(AuthSessionManager.RestoreOutcome.Unreachable, outcome, "failed refresh = no answer")
        assertEquals(AuthState.SignedOut, stack.session.state, "a failed refresh is not a session")
        assertTrue(escaped.isEmpty(), "nothing may escape the refresh pool thread: $escaped")
    }
}
