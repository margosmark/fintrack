package com.margosmark.fintracker

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test
import org.w3c.dom.Element

/**
 * AC4.4: the words "live", "current", "real time" and "continuous" — and
 * anything that promises a next refresh — may not appear in v1 copy. This
 * scan covers every STRING RESOURCE and every quoted display literal in the
 * UI/navigation/state sources; a violation names itself in the failure.
 */
class ForbiddenCopyTest {

    private val forbidden = listOf(Regex("""\blive\b""", RegexOption.IGNORE_CASE),
        Regex("""\bcurrent\b""", RegexOption.IGNORE_CASE),
        Regex("""real[\s-]?time""", RegexOption.IGNORE_CASE),
        Regex("""\bcontinuous\b""", RegexOption.IGNORE_CASE),
        Regex("""refresh(es|ing)? (in|every|at)\b""", RegexOption.IGNORE_CASE),
        Regex("""updates? (in|every|at)\b""", RegexOption.IGNORE_CASE))

    private fun violations(text: String): List<String> =
        forbidden.flatMap { re -> re.findAll(text).map { it.value } }

    private fun sourceRoot(): File {
        // Test working dir is the android/ project dir (AGP default).
        val base = File("src/main")
        assertTrue("src/main not found from ${File(".").absolutePath}", base.isDirectory)
        return base
    }

    @Test
    fun stringResourcesCarryNoForbiddenFreshnessWords() {
        val strings = File(sourceRoot(), "res/values/strings.xml")
        val doc = javax.xml.parsers.DocumentBuilderFactory.newInstance()
            .newDocumentBuilder()
            .parse(strings)
        val texts = doc.getElementsByTagName("string").let { list ->
            (0 until list.length).map { (list.item(it) as Element).textContent }
        }
        val bad = texts.flatMap { violations(it) }
        assertTrue("forbidden copy in strings.xml: $bad", bad.isEmpty())
    }

    @Test
    fun uiDisplayLiteralsCarryNoForbiddenFreshnessWords() {
        val dirs = listOf("kotlin/com/margosmark/fintracker/ui", "kotlin/com/margosmark/fintracker/navigation", "kotlin/com/margosmark/fintracker/state")
        val literal = Regex("\"(?:[^\"\\\\]|\\\\.)*\"")
        val bad = mutableListOf<String>()
        dirs.forEach { dir ->
            File(sourceRoot(), "kotlin/com/margosmark/fintracker/${dir.substringAfter("com/margosmark/fintracker/")}")
                .walkTopDown()
                .filter { it.isFile && it.extension == "kt" }
                .forEach { file ->
                    file.readLines().forEachIndexed { index, line ->
                        val trimmed = line.trimStart()
                        if (trimmed.startsWith("//") || trimmed.startsWith("*")) return@forEachIndexed
                        literal.findAll(line).forEach { m ->
                            violations(m.value).forEach { word ->
                                bad += "${file.name}:${index + 1}: '$word' in ${m.value}"
                            }
                        }
                    }
                }
        }
        assertTrue("forbidden copy in UI sources:\n${bad.joinToString("\n")}", bad.isEmpty())
    }
}
