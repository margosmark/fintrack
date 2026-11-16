package com.margosmark.fintracker.screens

import android.view.View
import androidx.compose.ui.semantics.SemanticsNode
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.getOrNull
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.navigation.compose.ComposeNavigator
import androidx.navigation.testing.TestNavHostController
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.margosmark.fintracker.FakeServer
import com.margosmark.fintracker.navigation.FinTrackerApp
import com.margosmark.fintracker.serverBackedScope
import com.margosmark.fintracker.state.MASK_BULLETS
import com.margosmark.fintracker.ui.screens.SectionTags
import java.io.File
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

/**
 * QA screenshots on a NARROW (320dp) config with synthetic fixture values —
 * no production data, no network beyond the local FakeServer.
 *
 * Rasterization context: Robolectric 4.14 defaults to LEGACY graphics mode,
 * where `Bitmap.createBitmap` bottoms out in a null native bitmap and compose's
 * vector DrawCache dies with an NPE (`bm is null`), while compose's
 * `captureToImage()` has no window compositor to force a redraw on. This class
 * opts into NATIVE graphics mode (real Skia via robolectric nativeruntime) so
 * the software `view.draw` path can produce a real PNG.
 *
 * Both capture paths are still attempted by [qaScreenshot]; a test only passes
 * when a REAL raster was written (file + multi-colour pixel census).
 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34], qualifiers = "w320dp")
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class QaScreenshotTest {

    @get:Rule
    val rule = createComposeRule()

    private val shotDir: String = System.getenv("QA_SHOT_DIR") ?: "/tmp/qa-shots"

    private var hostView: View? = null

    /** Synthetic mutations of the canned fixtures (server-string shapes). */
    private fun mutatedRouting(): Array<Pair<String, String>> {
        val positions = Fixtures.POSITIONS_JSON
            .replace("787.77", "12345678.91")
            .replace("\"price\":\"0.05\"", "\"price\":\"55.55550000\"")
            .replace("\"market_value_native\":\"0.11\"", "\"market_value_native\":\"1234567.895\"")
            .replace("\"market_value_cad\":\"0.11\"", "\"market_value_cad\":\"1234567.895\"")
        val accounts = Fixtures.ACCOUNTS_JSON
            .replace("\"total_value\":\"110.00\"", "\"total_value\":\"-98765.40\"")
        val account = Fixtures.ACCOUNT_JSON
            .replace("\"total_value\":\"110.00\"", "\"total_value\":\"-98765.40\"")
        return arrayOf(
            "/portfolio/positions" to positions,
            "/fx/status" to Fixtures.FX_JSON,
            "/transactions/7/events" to Fixtures.EVENTS_JSON,
            "/transactions" to Fixtures.TRANSACTIONS_JSON,
            "/accounts/ledger" to Fixtures.LEDGER_JSON,
            "/accounts/1" to account,
            "/accounts" to accounts,
        )
    }

    private fun launch(server: FakeServer, reveal: Boolean) {
        val navController = TestNavHostController(ApplicationProvider.getApplicationContext())
        navController.navigatorProvider.addNavigator(ComposeNavigator())
        val scope = serverBackedScope(server)
        if (reveal) scope.mask.reveal()
        rule.setContent {
            hostView = androidx.compose.ui.platform.LocalView.current
            FinTrackerApp(scope = scope, navController = navController)
        }
    }

    private fun dumpAllSemanticsText(tag: String): String {
        val texts = mutableListOf<String>()
        fun walk(node: SemanticsNode) {
            node.config.getOrNull(SemanticsProperties.Text)?.forEach { texts.add(it.text) }
            node.children.forEach { walk(it) }
        }
        rule.onAllNodesWithTag(tag, useUnmergedTree = true)
            .fetchSemanticsNodes()
            .forEach { walk(it) }
        return texts.joinToString("|")
    }

    /** A capture counts only with a real file AND a multi-colour raster. */
    private fun assertRealShot(outcome: QaShotOutcome, name: String) {
        val f = outcome.file ?: error("$name: both capture paths failed: ${outcome.errors}")
        assertTrue("$name written: ${f.isFile} (${f.length()} bytes)", f.isFile && f.length() > 1024)
        val colors = outcome.stats.substringAfter("sampledColors=", "").substringBefore(" ")
        assertTrue("$name is a blank raster: ${outcome.stats}", (colors.toIntOrNull() ?: 0) > 1)
        // PNG magic bytes — a real PNG, not an empty/placeholder file.
        val head = ByteArray(8)
        f.inputStream().use { s -> s.read(head) }
        assertTrue(
            "$name is not a PNG",
            head.contentEquals(byteArrayOf(0x89.toByte(), 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)),
        )
    }

    private fun reportGraphicsMode() {
        val mode = QaScreenshotTest::class.java.getAnnotation(GraphicsMode::class.java)?.value
        println("QA-SHOT graphicsMode=$mode qualifiers=w320dp shotDir=$shotDir")
    }

    @Test
    fun glanceUnmaskedScreenshot() {
        reportGraphicsMode()
        FakeServer().use { server ->
            server.serveByPath(*mutatedRouting())
            launch(server, reveal = true)
            rule.awaitText("TFSA")

            // headline: long server value grouped, raw string gone
            rule.awaitText("12,345,678.91")
            assertFalse(rule.textDisplayed("12345678.91"))
            // negative account total keeps its sign and groups
            assertTrue(rule.textDisplayed("-98,765.40"))
            assertFalse(rule.textDisplayed("-98765.40"))

            val out = rule.qaScreenshot("qa-glance-unmasked-320dp.png", { hostView }, shotDir)
            assertRealShot(out, "unmasked glance")
        }
    }

    @Test
    fun glanceMaskedScreenshot() {
        FakeServer().use { server ->
            server.serveByPath(*mutatedRouting())
            launch(server, reveal = false)
            rule.awaitText("TFSA")

            // labels remain visible; every figure is masked
            assertFalse(rule.textDisplayed("12,345,678.91"))
            assertFalse(rule.textDisplayed("-98,765.40"))

            // TalkBack reads the semantics tree — no digit may leak
            rule.awaitNodeWithTag("headline_value")
            val dumped = dumpAllSemanticsText("headline_value")
            assertTrue("bullets missing from semantics: $dumped", dumped.contains(MASK_BULLETS))
            for (ch in "1234567891") {
                assertFalse("digit '$ch' leaked in masked semantics: $dumped", dumped.contains(ch))
            }

            val out = rule.qaScreenshot("qa-glance-masked-320dp.png", { hostView }, shotDir)
            assertRealShot(out, "masked glance")
        }
    }

    @Test
    fun maskToggleRevealsAfterMaskedShot() {
        // Interaction sanity for the masked screenshot above: the toggle really
        // changes what is on screen (so the two PNGs differ for a reason).
        FakeServer().use { server ->
            server.serveByPath(*mutatedRouting())
            launch(server, reveal = false)
            rule.awaitText("TFSA")
            assertFalse(rule.textDisplayed("12,345,678.91"))
            rule.onNodeWithTag(SectionTags.MASK_TOGGLE).performClick()
            rule.awaitText("12,345,678.91")
            val out = rule.qaScreenshot("qa-glance-revealed-320dp.png", { hostView }, shotDir)
            assertRealShot(out, "revealed glance")
        }
    }
}
