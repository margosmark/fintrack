plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "com.margosmark.fintracker"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.margosmark.fintracker"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
    }

    buildTypes {
        release {
            // Debug-only card: no release signing, no minification.
            isMinifyEnabled = false
        }
    }

    testOptions {
        unitTests {
            // Robolectric needs merged resources on the JVM; no emulator here.
            isIncludeAndroidResources = true
        }
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.09.00")
    implementation(composeBom)
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.navigation:navigation-compose:2.8.2")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.6")

    // Auth/session + finance client transport (docs/52 Option A). Coordinates
    // are the ones the auth card (t_6eddd208) documented for integration;
    // applied here because the merged tree does not compile without them and
    // this card gates on real ./gradlew runs.
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // JVM unit-test harness (Robolectric, no emulator/AVD).
    testImplementation(composeBom)
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.robolectric:robolectric:4.14")
    testImplementation("androidx.test.ext:junit:1.2.1")
    testImplementation("androidx.compose.ui:ui-test-junit4")
    testImplementation("androidx.navigation:navigation-testing")
    debugImplementation("androidx.compose.ui:ui-test-manifest")
    // Mock server for read-path tests; real org.json for JVM tests (the
    // android.jar stub throws "not mocked"); kotlin.test assertions.
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
    testImplementation("org.json:json:20240303")
    testImplementation(kotlin("test-junit"))
}

// ---------------------------------------------------------------------------
// Compose-host Robolectric tests run on the DEBUG variant only.
//
// createComposeRule launches androidx.activity.ComponentActivity through
// ActivityScenario; that activity enters the test manifest via
// `debugImplementation("androidx.compose.ui:ui-test-manifest")`. The RELEASE
// variant's Robolectric environment builds its manifest from the release
// local-test APK's binary manifest (Robolectric binary-resources mode), not
// from test-scoped manifest AARs or test_config.properties - verified by
// probing the JVM's resolved test_config.properties: pointing it at a
// patched manifest changed nothing (cmp= still came from the APK). Patching
// the release APK's binary manifest would mean editing a shipped artifact.
//
// So: every Compose-host test runs on testDebugUnitTest; every JVM/Robolectric
// test without an activity host (the auth/session docs/54 suite, JSON
// decoding, masking logic, state mapping...) runs on BOTH variants, so
// FLAG_DEBUGGABLE-dependent behaviour stays release-verified. New
// Compose-host tests belong in com.margosmark.fintracker.screens (matched
// below) so this list cannot go stale silently.
// ---------------------------------------------------------------------------
tasks.withType<Test>().configureEach {
    if (name == "testReleaseUnitTest") {
        filter {
            excludeTestsMatching("com.margosmark.fintracker.screens.*")
            excludeTestsMatching("com.margosmark.fintracker.NavGraphTest")
            excludeTestsMatching("com.margosmark.fintracker.StatePaneTest")
            excludeTestsMatching("com.margosmark.fintracker.MaskedTextTest")
            excludeTestsMatching("com.margosmark.fintracker.DeepLinkGateTest")
            excludeTestsMatching("com.margosmark.fintracker.DestinationRound3Test")
        }
    }
}
