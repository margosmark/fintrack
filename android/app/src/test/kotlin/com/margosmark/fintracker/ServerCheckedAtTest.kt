package com.margosmark.fintracker

import com.margosmark.fintracker.state.formatServerCheckedAt
import com.margosmark.fintracker.state.serverCheckedLabel
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Instant

/**
 * Timestamp truthfulness: the only formatter for server_checked_at renders
 * the instant the server reported, and the label is omitted entirely when
 * there is no server time to show.
 */
class ServerCheckedAtTest {

    private val instant: Instant = Instant.parse("2026-10-29T18:05:00Z")

    @Test
    fun formatsTheServerInstantDeterministically() {
        // Formatting is pinned via the same formatter — the point is that the
        // OUTPUT is a pure function of the server_checked_at input.
        assertEquals(formatServerCheckedAt(instant), formatServerCheckedAt(instant))
        assertNotEqualsDifferentServerTimes()
    }

    private fun assertNotEqualsDifferentServerTimes() {
        val earlier = Instant.parse("2026-10-01T09:00:00Z")
        assertFalse(formatServerCheckedAt(instant) == formatServerCheckedAt(earlier))
    }

    @Test
    fun labelCarriesTheFormattedInstant() {
        val label = serverCheckedLabel(instant)!!
        assertTrue(label.startsWith("Checked "))
        assertTrue(label.contains(formatServerCheckedAt(instant)))
    }

    @Test
    fun nullServerTimeOmitsTheLabelEntirely() {
        // Omit rather than fabricate: no server_checked_at → no timestamp text.
        assertNull(serverCheckedLabel(null))
    }
}
