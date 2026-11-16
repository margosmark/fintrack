package com.margosmark.fintracker

import com.margosmark.fintracker.http.ApiException
import com.margosmark.fintracker.ui.screens.SectionPresenter
import com.margosmark.fintracker.ui.screens.SectionState
import java.io.IOException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * AC1.3 state mapping: every failure kind renders as its OWN state — never
 * collapsed to zero, never a generic error — and a refresh failure keeps the
 * last-known figures with their ORIGINAL receipt time.
 */
class SectionPresenterStatesTest {

    private fun waitUntil(timeoutMs: Long = 5_000, condition: () -> Boolean) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (!condition()) {
            if (System.currentTimeMillis() > deadline) throw AssertionError("condition not met in ${timeoutMs}ms")
            Thread.sleep(10)
        }
    }

    @Test
    fun successIsReadyWithTheServerData() {
        val p = SectionPresenter<String>(fetch = { "server-value" })
        p.load()
        waitUntil { p.current is SectionState.Ready }
        val ready = p.current as SectionState.Ready
        assertEquals("server-value", ready.data)
    }

    @Test
    fun ioFailureIsOfflineAndNeverZero() {
        val p = SectionPresenter<String>(fetch = { throw IOException("no route") })
        p.load()
        waitUntil { p.current !is SectionState.Loading }
        assertEquals(SectionState.Offline, p.current)
    }

    @Test
    fun httpVerdictsMapToTheirOwnStates() {
        val cases = mapOf(
            401 to SectionState.Expired,
            404 to SectionState.NotFound,
        )
        for ((status, expected) in cases) {
            val p = SectionPresenter<String>(fetch = { throw ApiException(status, "code", "detail") })
            p.load()
            waitUntil { p.current !is SectionState.Loading }
            assertEquals("status $status", expected, p.current)
        }
        val forbidden = SectionPresenter<String>(fetch = { throw ApiException(403, "code", "kept detail") })
        forbidden.load()
        waitUntil { forbidden.current !is SectionState.Loading }
        assertTrue("403 must be Permission", forbidden.current is SectionState.Permission)

        val broken = SectionPresenter<String>(fetch = { throw IllegalStateException("boom") })
        broken.load()
        waitUntil { broken.current !is SectionState.Loading }
        assertEquals(SectionState.Unavailable, broken.current)
    }

    @Test
    fun refreshFailureOverDataKeepsFiguresAndOriginalReceiptTime() {
        var fail = false
        val p = SectionPresenter<String>(fetch = {
            if (fail) throw IOException("dropped") else "figures"
        })
        p.load()
        waitUntil { p.current is SectionState.Ready }
        val originalReceived = (p.current as SectionState.Ready).receivedAt
        fail = true
        p.retry()
        waitUntil { p.current is SectionState.RefreshFailed }
        val failed = p.current as SectionState.RefreshFailed
        assertEquals("figures", failed.data)
        assertEquals(originalReceived, failed.receivedAt)
        assertTrue("offline flag must name the connection", failed.offline)
    }

    @Test
    fun emptyAnswerIsNoDataNotAFabricatedZero() {
        val p = SectionPresenter<List<Int>>(fetch = { emptyList() }, emptyWhen = { it.isEmpty() })
        p.load()
        waitUntil { p.current is SectionState.NoData }
    }

    @Test
    fun serverStalenessSignalBecomesStaleWithServerBuiltNote() {
        val p = SectionPresenter<String>(fetch = { "figures" }, staleWhen = { "price is 3 days old" })
        p.load()
        waitUntil { p.current is SectionState.Stale }
        val stale = p.current as SectionState.Stale
        assertEquals("price is 3 days old", stale.staleNote)
    }
}
