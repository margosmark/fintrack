package com.margosmark.fintracker.screens

import android.graphics.Bitmap
import android.graphics.Canvas
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.junit4.ComposeContentTestRule
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.captureToImage
import java.io.File
import kotlin.math.roundToInt

/** Outcome of one screenshot attempt. [file] is null when BOTH capture paths failed. */
data class QaShotOutcome(
    val file: File?,
    val stats: String,
    val errors: List<String>,
)

/**
 * Pixel census used to prove a capture is a REAL raster, not a blank canvas:
 * distinct opaque colours + non-white pixel count. A uniform (1-colour) image
 * is reported as such so a test can refuse it.
 */
fun qaBitmapStats(bitmap: Bitmap): String {
    val w = bitmap.width
    val h = bitmap.height
    val colors = HashSet<Int>()
    var nonWhite = 0
    var opaque = 0
    val stepX = if (w > 200) 2 else 1
    val stepY = if (h > 200) 2 else 1
    var y = 0
    while (y < h) {
        var x = 0
        while (x < w) {
            val c = bitmap.getPixel(x, y)
            if (colors.size < 8192) colors.add(c)
            if (c ushr 24 != 0) opaque++
            if (c != 0xFFFFFFFF.toInt()) nonWhite++
            x += stepX
        }
        y += stepY
    }
    return "${w}x${h} sampledColors=${colors.size} opaque=$opaque nonWhite=$nonWhite"
}

/**
 * Robolectric has no window compositor, so two capture paths are attempted in
 * order:
 *
 *  1. software-draw the app's own ComposeView into an ARGB bitmap (works when
 *     graphics mode gives us a real Skia bitmap/canvas);
 *  2. compose's `captureToImage()` (needs a live compositor — usually times out).
 *
 * A failure of a path is RECORDED with its full stack on stdout, never thrown,
 * so the behavioural assertions of a test never depend on pixels. The caller
 * must check [QaShotOutcome.file] + [QaShotOutcome.stats] to accept the shot.
 */
fun ComposeContentTestRule.qaScreenshot(
    name: String,
    hostView: () -> android.view.View?,
    shotDir: String,
): QaShotOutcome {
    val dir = File(shotDir).apply { mkdirs() }
    val errors = mutableListOf<String>()

    try {
        val view = hostView() ?: error("no host view captured during composition")
        if (view.width == 0 || view.height == 0) {
            // Robolectric windows rarely get a real size; force the QA width
            // (320dp at the test's density) with a tall portrait height.
            val density = view.resources.displayMetrics.density
            val w = (320 * density).roundToInt()
            val h = (1280 * density).roundToInt()
            view.measure(
                android.view.View.MeasureSpec.makeMeasureSpec(w, android.view.View.MeasureSpec.EXACTLY),
                android.view.View.MeasureSpec.makeMeasureSpec(h, android.view.View.MeasureSpec.EXACTLY),
            )
            view.layout(0, 0, w, h)
        }
        check(view.width > 0 && view.height > 0) { "view size ${view.width}x${view.height}" }
        val bitmap = Bitmap.createBitmap(view.width, view.height, Bitmap.Config.ARGB_8888)
        view.draw(Canvas(bitmap))
        val stats = qaBitmapStats(bitmap)
        val out = File(dir, name)
        out.outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }
        println("QA-SHOT view.draw OK -> ${out.absolutePath} $stats")
        return QaShotOutcome(out, stats, errors)
    } catch (t: Throwable) {
        errors.add("view.draw: $t")
        println("QA-SHOT view.draw FAILED:")
        t.printStackTrace(System.out)
    }

    try {
        mainClock.advanceTimeByFrame()
        val bitmap = onRoot().captureToImage().asAndroidBitmap()
        val stats = qaBitmapStats(bitmap)
        val out = File(dir, name)
        out.outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }
        println("QA-SHOT captureToImage OK -> ${out.absolutePath} $stats")
        return QaShotOutcome(out, stats, errors)
    } catch (t: Throwable) {
        errors.add("captureToImage: $t")
        println("QA-SHOT captureToImage FAILED:")
        t.printStackTrace(System.out)
    }

    val errFile = File(dir, name.removeSuffix(".png") + "_ERROR.txt")
    errFile.writeText(errors.joinToString("\n"))
    println("QA-SHOT both paths failed -> ${errFile.absolutePath}")
    return QaShotOutcome(null, "", errors)
}
