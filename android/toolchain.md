# FinTracker Android build toolchain

Spec for the Android build host (this LXC, 2026-10-29, card `t_a86ada99`).
Any shell — including a fresh login shell with nothing pre-set — can build the
app after these pieces are installed. Verify at any time:

```bash
android/scripts/verify_toolchain.sh   # exit 0 = ready, exit 1 = loud FAIL lines
```

## Pinned versions

| Piece | Version | Install path |
|---|---|---|
| JDK | Temurin **17.0.21+9** (sha256 `8b9b611d917e3dfd8419d3566b5949226f69b052fed7e109c1368add1dfe8711`) | `/opt/temurin-17` |
| Android cmdline-tools | **11076708** (`commandlinetools-linux-11076708_latest.zip`) | `/opt/android-sdk/cmdline-tools/latest` |
| platform-tools | current (adb 37.0.1-15733141) | `/opt/android-sdk/platform-tools` |
| platform | `platforms;android-34` | `/opt/android-sdk/platforms/android-34` |
| build-tools | `build-tools;34.0.0` | `/opt/android-sdk/build-tools/34.0.0` |
| Gradle | **8.9** — wrapper (committed) + bootstrap at `/opt/gradle-8.9` | wrapper downloads on demand |
| Android Gradle Plugin | **8.5.2** (pinned in `build.gradle.kts`) | fetched from google() |
| Kotlin | **2.0.21** (pinned in `build.gradle.kts`) | fetched from mavenCentral() |

Debian trixie only ships OpenJDK 21, which is why the JDK is Temurin rather
than a distro package.

## Install (fresh host)

```bash
# 1. JDK 17 (Temurin)
curl -sL -o /tmp/jdk17.tar.gz 'https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.21%2B9/OpenJDK17U-jdk_x64_linux_hotspot_17.0.21_9.tar.gz'
echo '8b9b611d917e3dfd8419d3566b5949226f69b052fed7e109c1368add1dfe8711  /tmp/jdk17.tar.gz' | sha256sum -c -
sudo mkdir -p /opt/temurin-17 && sudo tar -xzf /tmp/jdk17.tar.gz -C /opt/temurin-17 --strip-components=1

# 2. Android SDK (licenses accepted non-interactively)
curl -sL -o /tmp/cmdline-tools.zip 'https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip'
sudo mkdir -p /opt/android-sdk/cmdline-tools
sudo unzip -q -o /tmp/cmdline-tools.zip -d /opt/android-sdk/cmdline-tools
sudo mv /opt/android-sdk/cmdline-tools/cmdline-tools /opt/android-sdk/cmdline-tools/latest
export JAVA_HOME=/opt/temurin-17
yes | sudo -E /opt/android-sdk/cmdline-tools/latest/bin/sdkmanager --sdk_root=/opt/android-sdk --licenses
sudo -E env JAVA_HOME=/opt/temurin-17 /opt/android-sdk/cmdline-tools/latest/bin/sdkmanager \
    --sdk_root=/opt/android-sdk "platform-tools" "platforms;android-34" "build-tools;34.0.0"
sudo chmod -R a+rX /opt/android-sdk   # Gradle (non-root) only reads the SDK

# 3. Bootstrap Gradle (only to regenerate the wrapper; builds use ./gradlew)
curl -sL -o /tmp/gradle.zip 'https://services.gradle.org/distributions/gradle-8.9-bin.zip'
echo 'd725d707bfabd4dfdc958c624003b3c80accc03f7037b5122c4b1d0ef15cecab  /tmp/gradle.zip' | sha256sum -c -
sudo unzip -q -o /tmp/gradle.zip -d /opt
```

## Environment wiring

`/etc/profile.d/fintracker-android.sh` (installed on the build host) exports
for every login shell:

```bash
export JAVA_HOME=/opt/temurin-17
export ANDROID_HOME=/opt/android-sdk
export ANDROID_SDK_ROOT=/opt/android-sdk
export PATH="$JAVA_HOME/bin:$ANDROID_HOME/platform-tools:$PATH"
```

Notes:

- `local.properties` (`sdk.dir=...`) is **gitignored** — machine-local only.
  AGP finds the SDK via `ANDROID_HOME` (set by profile.d / the verify script);
  do not commit it.
- `scripts/verify_toolchain.sh` resolves everything itself from well-known
  paths, so it (and `./gradlew`) works even on a non-login shell with nothing
  exported.
- Builds must run with a JDK 17 on `JAVA_HOME`; `verify_toolchain.sh` enforces
  this. AGP 8.5.2 requires JDK 17+.
- System Gradle at `/opt/gradle-8.9` is bootstrap-only (wrapper regeneration):
  always build with `./gradlew`, never the system install, so the committed
  wrapper pins the real build version.

## Build

```bash
cd android
./gradlew assembleDebug
# APK: android/app/build/outputs/apk/debug/app-debug.apk
```

Debug-signed only — this project ships no release signing config and commits
no keystores, keys, or credentials.

## Adding SDK components later

```bash
sudo -E env JAVA_HOME=/opt/temurin-17 /opt/android-sdk/cmdline-tools/latest/bin/sdkmanager \
    --sdk_root=/opt/android-sdk "platforms;android-35"   # example
```

(The SDK lives under root-owned `/opt`, so new components need `sudo`;
Gradle itself never installs SDK pieces here.)
