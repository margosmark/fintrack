package com.margosmark.fintracker.state

import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * The ONE timestamp formatter for server_checked_at values. Formats the
 * instant the server reported — never a local "now". All "checked"-style
 * provenance text must go through this so tests can pin the exact output.
 */
private val serverCheckedFormatter: DateTimeFormatter =
    DateTimeFormatter.ofPattern("MMM d, HH:mm").withZone(ZoneId.systemDefault())

fun formatServerCheckedAt(at: Instant): String = serverCheckedFormatter.format(at)

/** Rendered when a state carries a server-checked time; OMITTED when null. */
fun serverCheckedLabel(at: Instant?): String? =
    at?.let { "Checked ${formatServerCheckedAt(it)}" }
