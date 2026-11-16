package com.margosmark.fintracker

import com.margosmark.fintracker.ui.screens.AccountValueFormat
import java.util.Locale
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Account-detail number formatting (docs/52 §3): server decimal strings are
 * grouped and given a sensible precision, without ever being re-derived,
 * zeroed, or dropped. Every input here is a wire string exactly as the server
 * sends money/quantity/price (docs/02 Decimal columns).
 */
class AccountValueFormatTest {

    private val us: Locale = Locale.US

    // --- money ----------------------------------------------------------

    @Test
    fun moneyIsGroupedWithExactlyTwoDecimals() {
        assertEquals("110.00", AccountValueFormat.amount("110.00", us))
        assertEquals("1,234.50", AccountValueFormat.amount("1234.50", us))
        assertEquals("1,234,567.90", AccountValueFormat.amount("1234567.895", us))
        assertEquals("-4.50", AccountValueFormat.amount("-4.50", us))
        assertEquals("0.00", AccountValueFormat.amount("0", us))
    }

    @Test
    fun moneyRoundsHalfUpAtTheMoneyPrecision() {
        assertEquals("1,234.57", AccountValueFormat.amount("1234.565", us))
        assertEquals("-1,234.57", AccountValueFormat.amount("-1234.565", us))
    }

    // --- prices ---------------------------------------------------------

    @Test
    fun pricesKeepUpToFourDecimalsAndStillGroup() {
        assertEquals("0.05", AccountValueFormat.price("0.05", us))
        assertEquals("0.0512", AccountValueFormat.price("0.05123456", us))
        assertEquals("123.4568", AccountValueFormat.price("123.456789123456", us))
        assertEquals("1,000,000.50", AccountValueFormat.price("1000000.5", us))
    }

    // --- quantities -----------------------------------------------------

    @Test
    fun quantitiesGroupAndTrimTheServerPadding() {
        // The wire's leading zeros are presentation, not value.
        assertEquals("10", AccountValueFormat.quantity("000010.0000", us))
        assertEquals("2", AccountValueFormat.quantity("2.0000", us))
        assertEquals("2.5", AccountValueFormat.quantity("2.5000", us))
        assertEquals("0.000001", AccountValueFormat.quantity("0.000001", us))
        assertEquals("1,234.5", AccountValueFormat.quantity("1234.5000", us))
    }

    // --- honesty rules --------------------------------------------------

    @Test
    fun unparsableValuesPassThroughVerbatim() {
        // A value we cannot pretty-print is still shown, never replaced.
        assertEquals("unknown", AccountValueFormat.amount("unknown", us))
        assertEquals("", AccountValueFormat.price("", us))
        assertEquals("n/a", AccountValueFormat.quantity("n/a", us))
    }

    @Test
    fun aTinyNonZeroValueIsNeverRenderedAsZero() {
        // Rounding for readability must not invent a zero the server never sent.
        assertEquals("0.004", AccountValueFormat.amount("0.004", us))
        assertEquals("0.00001", AccountValueFormat.price("0.00001", us))
        assertEquals("0.0000001", AccountValueFormat.quantity("0.0000001", us))
    }

    // --- locale ---------------------------------------------------------

    @Test
    fun groupingFollowsTheDeviceLocale() {
        val german = Locale.GERMANY
        assertEquals("1.234,57", AccountValueFormat.amount("1234.567", german))
        assertEquals("1.234,5679", AccountValueFormat.price("1234.56789", german))
        assertEquals("1.234,5", AccountValueFormat.quantity("1234.5000", german))
    }
}
