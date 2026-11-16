package com.margosmark.fintracker

import com.margosmark.fintracker.finance.FinanceJson
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Test

/**
 * Decode contract (docs/52 §4.1, docs/53 AC1.1): money and quantity fields
 * are the SERVER's own strings, copied byte-for-byte — never parsed, rounded
 * or re-formatted — and null stays null, never 0.
 */
class FinanceJsonTest {

    @Test
    fun moneyStringsPassThroughVerbatim() {
        val page = FinanceJson.positionsPage(com.margosmark.fintracker.screens.Fixtures.POSITIONS_JSON)
        val vcn = page.items.first { it.symbol == "VCN" }
        // Leading zeros and 4-decimal quantity survive exactly as sent.
        assertEquals("000010.0000", vcn.quantity)
        assertEquals("0.11", vcn.marketValueCad)
        // The server's headline is taken as-is — no client-side sum of rows.
        assertEquals("777.77", page.totalValueCad)
    }

    @Test
    fun nullFieldsStayNullNeverZero() {
        val page = FinanceJson.positionsPage(com.margosmark.fintracker.screens.Fixtures.POSITIONS_JSON)
        val zag = page.items.first { it.symbol == "ZAG" }
        assertNull(zag.priceDate)
        assertNull(zag.price)
        assertNull(zag.priceFetchedAt)
        assertNull(zag.priceStale)
        assertNull(zag.dayChangePct)
        assertNull(page.totalDayChangeCad)
    }

    @Test
    fun unknownServerFieldsAreIgnored() {
        val page = FinanceJson.transactionsPage(
            """{"items":[{"id":1,"account_id":1,"date":"2026-10-01","raw_description":"X",""" +
                """"normalized_payee":null,"amount_native":"-1.00","currency":"CAD","brand_new_field":{"a":1}}],""" +
                """"total":1,"server_added":{"nested":true}}""",
        )
        assertEquals("-1.00", page.items.single().amountNative)
    }

    @Test
    fun withheldAndLimitDefaultsAreHonest() {
        val page = FinanceJson.transactionsPage(
            """{"items":[],"total":0}""",
        )
        assertEquals(0, page.withheld)
        assertEquals(0, page.limit)
    }

    @Test
    fun malformedSuccessBodyIsAParseFailureNotAnEmptyScreen() {
        assertThrows(IllegalStateException::class.java) {
            FinanceJson.accountsPage("""{"items":[{"id":1}],"total":1}""")
        }
    }

    @Test
    fun fxStatusDefaultsToCadOnlyWhenTheServerOmitsIt() {
        assertEquals("CAD", FinanceJson.fxStatus("""{}""").reportingCurrency)
        assertEquals("USD", FinanceJson.fxStatus("""{"reporting_currency":"USD"}""").reportingCurrency)
    }

    @Test
    fun unknownAccountTypeLabelPassesThroughUnmapped() {
        val row = FinanceJson.account(
            """{"id":3,"name":"New","type":"some_future_type","currency":"CAD",""" +
                """"investment_value":"0.00","starting_balance":"0.00"}""",
        )
        assertEquals("some_future_type", row.type)
    }
}
