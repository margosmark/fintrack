// Root build file: pins the Android Gradle Plugin, Kotlin, and Compose
// compiler plugin versions (Compose compiler ships as a Kotlin plugin since
// Kotlin 2.0 — versions must match the Kotlin pin exactly).
// Toolchain requirements (JDK 17, Android SDK) are documented in toolchain.md
// and enforced by scripts/verify_toolchain.sh.
plugins {
    id("com.android.application") version "8.5.2" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.0.21" apply false
}
