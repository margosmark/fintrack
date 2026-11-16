package com.margosmark.fintracker

import com.margosmark.fintracker.ui.screens.HomeNumberFormat
import java.util.Locale
import org.junit.Assert.assertEquals
import org.junit.Test

class HomeNumberFormatTest {
    @Test fun groupsAndRoundsDecimalStringsWithoutBinaryFloatConversion() {
        assertEquals("1,234,567.90", HomeNumberFormat.amount("1234567.895", Locale.US))
        assertEquals("-1,234.57", HomeNumberFormat.amount("-1234.565", Locale.US))
        assertEquals("123.4568", HomeNumberFormat.price("123.456789123456", Locale.US))
        assertEquals("0.05", HomeNumberFormat.price("0.05", Locale.US))
        assertEquals("9,007,199,254,740,993.01", HomeNumberFormat.amount("9007199254740993.01", Locale.US))
    }

    @Test fun followsLocaleAndDoesNotInventInvalidValues() {
        assertEquals("1.234,57", HomeNumberFormat.amount("1234.567", Locale.GERMANY))
        assertEquals("1.234,5679", HomeNumberFormat.price("1234.56789", Locale.GERMANY))
        assertEquals("0.00000001", HomeNumberFormat.price("0.00000001", Locale.US))
        assertEquals("unknown", HomeNumberFormat.price("unknown", Locale.US))
    }
}
