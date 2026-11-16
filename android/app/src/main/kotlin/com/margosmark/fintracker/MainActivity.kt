package com.margosmark.fintracker

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import com.margosmark.fintracker.navigation.FinTrackerApp
import com.margosmark.fintracker.ui.productionScope
import com.margosmark.fintracker.ui.theme.FinTrackerTheme

/**
 * Single-activity shell. The wiring seam is [productionScope]: it builds the
 * parent card's auth/session stack (docs/52 Option A), feeds the real
 * session into the graph, and starts the boot restore (purge + refresh) in
 * the background. This activity never parses tokens itself.
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Launch intent data is resolved into the graph's start destination
        // inside FinTrackerApp (gate: authenticated + shape; existence and
        // tenant scoping for id routes are enforced server-side on load).
        val deepLinkUri: String? = intent?.data?.toString()
        val scope = productionScope(this)
        setContent {
            FinTrackerTheme {
                FinTrackerApp(
                    scope = scope,
                    deepLinkUri = deepLinkUri,
                )
            }
        }
    }
}
