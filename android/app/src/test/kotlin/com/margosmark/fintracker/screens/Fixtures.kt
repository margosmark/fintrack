package com.margosmark.fintracker.screens

/**
 * Canned wire fixtures for the read-screen tests. Every value that crosses
 * the UI is a SERVER STRING on purpose: odd leading zeros, more precision
 * than a float would keep, and a server total that deliberately does NOT
 * equal the sum of its rows (docs/53 AC1.1 — no client-side arithmetic).
 */
object Fixtures {

    const val ACCOUNTS_JSON =
        """{"items":[{"id":1,"name":"TFSA","type":"tfsa","currency":"CAD","cash_balance":"10.00",""" +
            """"investment_value":"100.00","total_value":"110.00","starting_balance":"5.00","last_sync_at":null,""" +
            """"provider_status":null,"pending_import_count":0}],"total":1}"""

    const val ACCOUNT_JSON =
        """{"id":1,"name":"TFSA","type":"tfsa","currency":"CAD","cash_balance":"10.00",""" +
            """"investment_value":"100.00","total_value":"110.00","starting_balance":"5.00","last_sync_at":null,""" +
            """"provider_status":null,"pending_import_count":0}"""

    /** Two rows summing to 0.33 while the server headline says 777.77. */
    const val POSITIONS_JSON =
        """{"items":[{"account_id":1,"account_name":"TFSA","security_id":9,"symbol":"VCN",""" +
            """"listing_currency":"CAD","quantity":"000010.0000","price_date":"2026-10-28","price":"0.05",""" +
            """"price_source":"manual","price_age_days":1,"price_fetched_at":null,"price_stale":false,""" +
            """"market_value_native":"0.11","market_value_cad":"0.11","book_cost_cad":"0.09",""" +
            """"day_change_native":null,"day_change_pct":null,"day_change_cad":null,"unrealized_gain_cad":"0.02"},""" +
            """{"account_id":1,"account_name":"TFSA","security_id":10,"symbol":"ZAG","listing_currency":"CAD",""" +
            """"quantity":"2.0000","price_date":null,"price":null,"price_source":null,"price_age_days":null,""" +
            """"price_fetched_at":null,"price_stale":null,"market_value_native":"0.22","market_value_cad":"0.22",""" +
            """"book_cost_cad":null,"day_change_native":null,"day_change_pct":null,"day_change_cad":null,""" +
            """"unrealized_gain_cad":null}],"total":2,"total_value_cad":"777.77","cash_cad":"10.00",""" +
            """"portfolio_value_cad":"787.77","total_day_change_cad":null,"total_day_change_pct":null,""" +
            """"day_change_as_of":"2026-10-28","day_change_mixed_baseline":false}"""

    const val FX_JSON = """{"reporting_currency":"CAD","as_of":"2026-10-29","age_days":0,"stale":false}"""

    const val TRANSACTIONS_JSON =
        """{"items":[{"id":7,"account_id":1,"date":"2026-10-01","raw_description":"COFFEE SHOP",""" +
            """"normalized_payee":"Coffee Shop","amount_native":"-4.50","currency":"CAD","cad_amount":"-4.50",""" +
            """"category_id":null,"reviewed_at":null,"is_recurring":false,"has_receipt":false,"is_split":false,""" +
            """"parent_split_id":null,"note":null}],"total":1,"limit":50,"offset":0,"withheld":0}"""

    const val TRANSACTIONS_EMPTY_JSON = """{"items":[],"total":0,"limit":50,"offset":0,"withheld":0}"""

    const val EVENTS_JSON =
        """{"items":[{"id":1,"tx_id":7,"event_kind":"imported","actor_label":"import",""" +
            """"at":"2026-10-02T00:00:00Z","summary":"Imported from CSV"}],"total":1}"""

    const val LEDGER_JSON =
        """{"items":[{"transaction_id":7,"date":"2026-10-01","description":"COFFEE","amount":"-4.50",""" +
            """"running_balance":"105.50"}],"starting_balance":"110.00","current_balance":"105.50","total":1}"""

    /** Canned routing for a full read walkthrough: any GET path → its body. */
    fun fullReadRouting(): Array<Pair<String, String>> = arrayOf(
        "/portfolio/positions" to POSITIONS_JSON,
        "/fx/status" to FX_JSON,
        "/transactions/7/events" to EVENTS_JSON,
        "/transactions" to TRANSACTIONS_JSON,
        "/accounts/ledger" to LEDGER_JSON,
        "/accounts/1" to ACCOUNT_JSON,
        "/accounts" to ACCOUNTS_JSON,
    )
}
