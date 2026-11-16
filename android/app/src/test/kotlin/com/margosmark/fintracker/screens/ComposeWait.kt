package com.margosmark.fintracker.screens

import androidx.compose.ui.test.junit4.ComposeContentTestRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText

/** Polling helpers: section fetches run on background threads the compose
 *  idleness tracker cannot see, so assertions must wait for them. */
fun ComposeContentTestRule.awaitNodeWithTag(tag: String, timeoutMs: Long = 10_000) {
    waitUntil(timeoutMs) {
        onAllNodesWithTag(tag, useUnmergedTree = true).fetchSemanticsNodes().isNotEmpty()
    }
}

fun ComposeContentTestRule.awaitText(text: String, timeoutMs: Long = 10_000) {
    waitUntil(timeoutMs) {
        onAllNodesWithText(text, useUnmergedTree = true).fetchSemanticsNodes().isNotEmpty()
    }
}

fun ComposeContentTestRule.textDisplayed(text: String): Boolean =
    onAllNodesWithText(text, useUnmergedTree = true).fetchSemanticsNodes().isNotEmpty()

fun ComposeContentTestRule.tagDisplayed(tag: String): Boolean =
    onAllNodesWithTag(tag, useUnmergedTree = true).fetchSemanticsNodes().isNotEmpty()

/**
 * Bounded data-wait behind every QA round-3 `observe(...)`.
 *
 * Two properties matter for attribution:
 *
 *  1. A transient exception thrown *by the condition* (a semantics fetch racing a
 *     recomposition) must not be read as "condition false". Before t_91666519 it escaped
 *     `waitUntil`, the wait returned on the spot, and the test failed at its render
 *     assertion with the signature of a timeout it never waited for. Here the fault is
 *     swallowed and polling continues; the last one is reported — as a recovery note when
 *     the wait still succeeds, as the cause when it does not.
 *  2. A failure line carries the elapsed time, so a console-only red says whether the
 *     condition was never true (deadline) or blew up (named exception).
 *
 * Returns true when the condition held before the deadline. The caller's assertions are
 * unchanged: this only decides *when* the condition is sampled.
 */
fun ComposeContentTestRule.boundedObserve(name: String, timeoutMs: Long, cond: () -> Boolean): Boolean {
    var conditionThrew: Throwable? = null
    val startedAt = System.currentTimeMillis()
    return try {
        waitUntil(timeoutMs) {
            try {
                cond()
            } catch (t: Throwable) {
                conditionThrew = t
                false
            }
        }
        conditionThrew?.let {
            println("QA-R3 OBSERVE $name recovered after a transient ${it::class.simpleName}: ${it.message}")
        }
        true
    } catch (t: Throwable) {
        val elapsed = System.currentTimeMillis() - startedAt
        println(
            "QA-R3 OBSERVE $name not satisfied after ${elapsed}ms: ${t::class.simpleName}" +
                (conditionThrew?.let { " (condition threw ${it::class.simpleName}: ${it.message})" } ?: "")
        )
        false
    }
}
