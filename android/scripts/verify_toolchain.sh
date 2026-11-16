#!/usr/bin/env bash
# verify_toolchain.sh — non-interactive Android toolchain check for fin_tracker.
#
# Designed to pass on a FRESH shell with nothing pre-set: it resolves JDK 17
# and the Android SDK itself (env var, then well-known install paths) and
# exports them before running the Gradle wrapper. Any missing piece prints a
# loud FAIL line to stderr; the script exits non-zero if ANY check failed.
#
# Usage:  android/scripts/verify_toolchain.sh        (exit 0 = toolchain ready)
set -u

FAIL=0
pass() { printf 'ok   %s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*" >&2; FAIL=1; }

# ---------------------------------------------------------------- JDK 17 ----
# Resolution order: $JAVA_HOME, then the pinned Temurin install, then PATH.
if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/java" ]; then
    JDK="$JAVA_HOME"
elif [ -x /opt/temurin-17/bin/java ]; then
    JDK=/opt/temurin-17
elif command -v java >/dev/null 2>&1; then
    JDK="$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")"
else
    JDK=""
fi

if [ -z "$JDK" ]; then
    fail "no JDK found (install per android/toolchain.md)"
else
    JAVA_VER="$("$JDK/bin/java" -version 2>&1 | head -1)"
    case "$JAVA_VER" in
        *'"17.'*) pass "java 17: $JAVA_VER" ;;
        *)        fail "java is not 17 at $JDK/bin/java — got: $JAVA_VER" ;;
    esac
    if "$JDK/bin/javac" -version >/dev/null 2>&1; then
        pass "javac: $("$JDK/bin/javac" -version 2>&1)"
    else
        fail "javac missing at $JDK/bin/javac"
    fi
    export JAVA_HOME="$JDK"
fi

# ------------------------------------------------------- Android SDK -------
# Resolution order: $ANDROID_HOME, $ANDROID_SDK_ROOT, /opt/android-sdk,
# $HOME/Android/Sdk, /usr/lib/android-sdk.
SDK=""
for d in "${ANDROID_HOME:-}" "${ANDROID_SDK_ROOT:-}" /opt/android-sdk \
         "$HOME/Android/Sdk" /usr/lib/android-sdk; do
    if [ -n "$d" ] && [ -d "$d" ]; then SDK="$d"; break; fi
done

if [ -z "$SDK" ]; then
    fail "Android SDK not found (install per android/toolchain.md)"
else
    export ANDROID_HOME="$SDK"
    export ANDROID_SDK_ROOT="$SDK"

    [ -d "$SDK/platforms/android-34" ] \
        && pass "platforms;android-34 present" \
        || fail "platforms;android-34 missing under $SDK/platforms"

    if [ -x "$SDK/build-tools/34.0.0/aapt2" ]; then
        pass "build-tools;34.0.0 present ($("$SDK/build-tools/34.0.0/aapt2" version 2>&1))"
    else
        fail "build-tools/34.0.0/aapt2 missing under $SDK/build-tools"
    fi

    if [ -x "$SDK/platform-tools/adb" ]; then
        pass "platform-tools present ($("$SDK/platform-tools/adb" --version 2>&1 | head -1))"
    else
        fail "platform-tools/adb missing under $SDK/platform-tools"
    fi

    if [ -n "$(ls -A "$SDK/licenses" 2>/dev/null)" ]; then
        pass "SDK licenses accepted ($(ls "$SDK/licenses" | wc -l) license files)"
    else
        fail "no accepted SDK licenses in $SDK/licenses — run: yes | sdkmanager --licenses"
    fi
fi

# ------------------------------------------------------- Gradle wrapper -----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

for f in gradlew gradle/wrapper/gradle-wrapper.jar gradle/wrapper/gradle-wrapper.properties; do
    if [ -f "$PROJECT_DIR/$f" ]; then
        pass "wrapper file present: $f"
    else
        fail "wrapper file missing: android/$f"
    fi
done

if [ -x "$PROJECT_DIR/gradlew" ]; then
    WRAP_VER="$("$PROJECT_DIR/gradlew" --version 2>/dev/null | grep '^Gradle ' || true)"
    if [ "$WRAP_VER" = "Gradle 8.9" ]; then
        pass "gradlew runs: $WRAP_VER"
    elif [ -n "$WRAP_VER" ]; then
        fail "gradlew reports '$WRAP_VER', expected 'Gradle 8.9'"
    else
        fail "gradlew --version produced no output (JDK/SDK broken?)"
    fi
else
    fail "gradlew is not executable"
fi

# --------------------------------------------------------------- verdict ----
if [ "$FAIL" -eq 0 ]; then
    echo "TOOLCHAIN OK (JDK 17 + Android SDK 34 + Gradle 8.9 wrapper)"
    exit 0
else
    echo "TOOLCHAIN BROKEN — see FAIL lines above. See android/toolchain.md." >&2
    exit 1
fi
