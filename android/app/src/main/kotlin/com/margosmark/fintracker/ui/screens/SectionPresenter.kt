package com.margosmark.fintracker.ui.screens

import androidx.compose.runtime.MutableState
import androidx.compose.runtime.State
import androidx.compose.runtime.mutableStateOf
import com.margosmark.fintracker.http.ApiException
import java.io.IOException
import java.time.Instant
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger

/**
 * Drives ONE section's [SectionState] from a blocking fetch on a background
 * thread (the parent HTTP client is blocking by design — no coroutine
 * dependency was added there, and this class keeps it that way).
 *
 * Error mapping (docs/53 AC1.3):
 *  - `IOException`            → first load: `Offline`; over data: `RefreshFailed(offline=true)`
 *  - `ApiException(404)`      → `NotFound` (record gone; generic)
 *  - `ApiException(401)`      → `Expired` (the session manager flips auth state too)
 *  - `ApiException(403)`      → `Permission` (explained in place)
 *  - any other failure        → first load: `Unavailable`; over data: `RefreshFailed(offline=false)`
 *
 * [receivedAt] is the CLIENT-OBSERVED time the response arrived. It is the
 * only clock this class reads, and the UI always labels it "device time"
 * (docs/52 §3 sanctions exactly this wording; it is never shown as a server
 * check time).
 *
 * Superseded loads are discarded by generation counter, so an older slow
 * response can never overwrite a newer state (including a logout race —
 * the caller simply drops the presenter).
 */
class SectionPresenter<T>(
    private val fetch: () -> T,
    private val staleWhen: (T) -> String? = { null },
    private val emptyWhen: (T) -> Boolean = { false },
    private val executor: ExecutorService = sharedExecutor,
    private val receivedClock: () -> Instant = { Instant.now() },
) {
    private val _state: MutableState<SectionState<T>> = mutableStateOf(SectionState.Loading)
    val state: State<SectionState<T>> get() = _state

    /** Current value for non-Compose callers (tests, wiring). */
    val current: SectionState<T> get() = _state.value

    private var generation = 0

    fun load() = submit()

    fun retry() = submit()

    private fun submit() {
        val myGeneration: Int
        val previous: T?
        val previousReceived: Instant?
        synchronized(this) {
            myGeneration = ++generation
            val cur = _state.value
            previous = cur.lastData()
            previousReceived = cur.receivedAtOrNull()
        }
        // First load → Loading (NO figures). Refresh over data → keep figures visible.
        _state.value = if (previous == null) {
            SectionState.Loading
        } else {
            SectionState.Refreshing(previous, requireNotNull(previousReceived))
        }
        executor.execute {
            val next: SectionState<T> = try {
                val data = fetch()
                mapSuccess(data)
            } catch (e: Throwable) {
                mapFailure(e, previous, previousReceived)
            }
            synchronized(this) {
                if (myGeneration != generation) return@execute // superseded — discard
            }
            _state.value = next
        }
    }

    private fun mapSuccess(data: T): SectionState<T> {
        val at = receivedClock()
        return when {
            emptyWhen(data) -> SectionState.NoData(at)
            else -> {
                val note = staleWhen(data)
                if (note != null) SectionState.Stale(data, at, note)
                else SectionState.Ready(data, at)
            }
        }
    }

    private fun mapFailure(e: Throwable, previous: T?, previousReceived: Instant?): SectionState<T> =
        when {
            // HTTP verdicts come first: they say something about the RECORD or
            // the SESSION, which outranks "we still have figures on screen".
            e is ApiException && e.status == 401 -> SectionState.Expired
            e is ApiException && e.status == 404 -> SectionState.NotFound
            e is ApiException && e.status == 403 -> SectionState.Permission(e.message)
            // Otherwise: refresh failure over last-known figures stays visible
            // with its ORIGINAL receipt time (AC1.3); first load has nothing.
            previous != null && previousReceived != null ->
                SectionState.RefreshFailed(previous, previousReceived, offline = e is IOException)
            e is IOException -> SectionState.Offline
            else -> SectionState.Unavailable
        }

    fun dispose() {
        synchronized(this) { generation++ } // in-flight result can never land
        if (executor === sharedExecutor) return
        executor.shutdownNow()
    }

    companion object {
        /** Daemon pool shared by all presenters; per-presenter executors are also allowed. */
        val sharedExecutor: ExecutorService = Executors.newCachedThreadPool { runnable ->
            Thread(runnable, "fintracker-section-${threadIndex.getAndIncrement()}").apply { isDaemon = true }
        }
        private val threadIndex = AtomicInteger()
    }
}

/** Last-known figures inside a state, if any (refresh continuity). */
private fun <T> SectionState<T>.lastData(): T? = when (this) {
    is SectionState.Ready -> data
    is SectionState.Refreshing -> data
    is SectionState.RefreshFailed -> data
    is SectionState.Stale -> data
    else -> null
}

/**
 * True when this state carries data the server actually returned for the
 * CURRENT identity (security S1). A route-derived id (the `Account 5`
 * subtitle) may render only under this verdict — a bare route id is never
 * server truth, and an unconfirmed one can belong to another tenant's
 * route (docs/53 AC1.3/AC3.1).
 */
fun <T> SectionState<T>.hasServerData(): Boolean = lastData() != null

/** The ORIGINAL receipt time of the figures currently on screen. */
private fun <T> SectionState<T>.receivedAtOrNull(): Instant? = when (this) {
    is SectionState.Ready -> receivedAt
    is SectionState.Refreshing -> receivedAt
    is SectionState.RefreshFailed -> receivedAt
    is SectionState.Stale -> receivedAt
    is SectionState.NoData -> receivedAt
    else -> null
}
