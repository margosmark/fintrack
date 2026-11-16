package com.margosmark.fintracker.ui.screens

import java.math.RoundingMode
import java.text.NumberFormat
import java.util.Locale

/** Display-only formatting; never changes the server model or performs currency conversion. */
object HomeNumberFormat {
    fun amount(raw: String, locale: Locale = Locale.getDefault()): String = format(raw, locale, 2)
    fun price(raw: String, locale: Locale = Locale.getDefault()): String = format(raw, locale, 4)

    private fun format(raw: String, locale: Locale, maxDecimals: Int): String {
        val value = raw.toBigDecimalOrNull() ?: return raw
        val formatter = NumberFormat.getNumberInstance(locale).apply {
            isGroupingUsed = true
            minimumFractionDigits = 2
            maximumFractionDigits = maxDecimals
            roundingMode = RoundingMode.HALF_UP
        }
        // Do not turn a non-zero tiny quote into a displayed zero.
        if (value.signum() != 0 && value.setScale(maxDecimals, RoundingMode.HALF_UP).signum() == 0) {
            return raw
        }
        return formatter.format(value)
    }
}
