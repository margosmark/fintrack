package com.margosmark.fintracker.ui.screens

import java.math.RoundingMode
import java.text.NumberFormat
import java.util.Locale

/**
 * Display-only number formatting for the ACCOUNT DETAIL screen (docs/52 §3,
 * "account detail"): server decimal strings get thousands grouping and a
 * sensible fixed precision so a balance reads as `1,234,567.90` instead of
 * `1234567.895`.
 *
 * Rules this object exists to keep:
 *  - the DATA layer stays verbatim: [com.margosmark.fintracker.finance.FinanceModels]
 *    still carries every money/quantity/price string exactly as the server
 *    sent it; formatting happens here, at the render seam, and only for the
 *    account-detail screen;
 *  - no arithmetic: a value is parsed and re-rendered, never summed,
 *    converted, or re-derived — a mutated server string still shows up as
 *    that mutation (docs/53 AC1.1);
 *  - no invented zeros: a non-zero value that would round to zero at the
 *    target precision is returned UNFORMATTED rather than displayed as `0.00`
 *    (docs/53 AC1.3 "unavailable is not $0", applied to rounding too);
 *  - unparsable input passes through VERBATIM — a value we cannot pretty
 *    print is still shown, never dropped or replaced;
 *  - money/quantity fields are the SERVER's domain: this is rounding for
 *    readability (HALF_UP, the money convention used across the repo), not a
 *    change of value.
 *
 * Sibling note: candidate `8822083` adds a home-glance `HomeNumberFormat`
 * with the same amount/price shape for the glance screen. This file is
 * deliberately separate (that candidate owns `HomeNumberFormat.kt` and must
 * not be edited from this card); when both land, an integrator can fold the
 * two into one formatter in a follow-up.
 */
object AccountValueFormat {

    /** Money: grouped, exactly 2 decimals (`1,234.50`, `-4.50`). */
    fun amount(raw: String, locale: Locale = Locale.getDefault()): String =
        format(raw, locale, minDecimals = 2, maxDecimals = 2)

    /** Price: grouped, 2–4 decimals (`0.05`, `123.4568`). */
    fun price(raw: String, locale: Locale = Locale.getDefault()): String =
        format(raw, locale, minDecimals = 2, maxDecimals = 4)

    /**
     * Quantity (units/shares): grouped, 0–6 decimals with trailing zeros
     * trimmed (`10`, `2.5`, `0.000001`) — a share count is not money, so it
     * does not carry a forced 2-decimal tail.
     */
    fun quantity(raw: String, locale: Locale = Locale.getDefault()): String =
        format(raw, locale, minDecimals = 0, maxDecimals = 6)

    private fun format(raw: String, locale: Locale, minDecimals: Int, maxDecimals: Int): String {
        val value = raw.toBigDecimalOrNull() ?: return raw
        // Never turn a non-zero tiny value into a displayed zero.
        if (value.signum() != 0 && value.setScale(maxDecimals, RoundingMode.HALF_UP).signum() == 0) {
            return raw
        }
        val formatter = NumberFormat.getNumberInstance(locale).apply {
            isGroupingUsed = true
            minimumFractionDigits = minDecimals
            maximumFractionDigits = maxDecimals
            roundingMode = RoundingMode.HALF_UP
        }
        return formatter.format(value)
    }
}
