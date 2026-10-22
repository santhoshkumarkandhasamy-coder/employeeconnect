# Platform setup and hardening (Android / iOS)

Status: NOT executed or compiled by the author (Flutter was not installed). Everything below and every file in
`tool/templates/` is unverified until you run it. The Dart side fails closed in release builds when the native
parts are missing, so a skipped step is caught at runtime, not silently ignored.

Fast path (Windows, Flutter on PATH):

```powershell
pwsh -File frontend/tool/bootstrap.ps1        # or: powershell -File frontend\tool\bootstrap.ps1
```

It runs `flutter create`, applies everything in this file, then `flutter pub get`. It prints a WARNING for any
patch it could not match. The manual steps below are the same content for review or for template drift.

Channel contract used by Dart:

| Channel | Method | Argument | Result | Dart file |
|---|---|---|---|---|
| `employeeconnect/screen` | `secure` | `bool` | null | `lib/core/security/screen_protect.dart` |
| `employeeconnect/integrity` | `isCompromised` | none | `bool` | `lib/core/security/device_integrity.dart` |

Release-mode behaviour (fail closed):

- No `employeeconnect/screen` handler: `SecureScreen` shows an "unavailable" message instead of the login / change-password / lock screens.
- No `employeeconnect/integrity` handler (or an error): device is treated as compromised (Admin blocked, others see the banner). Opt out for internal builds only with `--dart-define=ALLOW_UNVERIFIED_DEVICE=true`.
- `API_BASE_URL` not `https://`, or fewer than two `CERT_PINS`: `createDio` throws `StateError` at startup.

## Android

### AndroidManifest.xml (`android/app/src/main/AndroidManifest.xml`)

```xml
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.USE_BIOMETRIC" />
<!-- attendance geofence (foreground only; no ACCESS_BACKGROUND_LOCATION) -->
<uses-permission android:name="android.permission.ACCESS_FINE_LOCATION" />
<uses-permission android:name="android.permission.ACCESS_COARSE_LOCATION" />
<!-- Keep BOTH: Android 12+ ignores a FINE-only request. The app asks for fine
     location and refuses to check in when the user grants only "Approximate"
     (shows "Precise location is required" + Open settings). -->

<application
    android:label="Employee Connect"
    android:name="${applicationName}"
    android:icon="@mipmap/ic_launcher"
    android:allowBackup="false"
    android:fullBackupContent="false"
    android:dataExtractionRules="@xml/data_extraction_rules"
    android:usesCleartextTraffic="false"
    android:networkSecurityConfig="@xml/network_security_config">
    ...
</application>
```

`res/xml/network_security_config.xml` and `res/xml/data_extraction_rules.xml`: copy from `tool/templates/`.
The network config disables cleartext and trusts system CAs only (not user-installed ones). Pinning itself is in Dart.

Debug-only cleartext (so a debug APK can reach an `http://` LAN/emulator backend, e.g. `http://10.0.2.2:8000/api`):
`android/app/src/debug/AndroidManifest.xml` sets `usesCleartextTraffic="true"` with `tools:replace`, and
`android/app/src/debug/res/xml/network_security_config.xml` overlays the main config with `cleartextTrafficPermitted="true"`.
Templates: `tool/templates/AndroidManifest.debug.xml` and `network_security_config.debug.xml` (copied by `tool/bootstrap.ps1`).
Release and profile builds use only `src/main` and stay cleartext-off; check the merged release manifest
(`build/app/intermediates/merged_manifest/release/`) after changes.

Camera (selfie): `image_picker` launches the system camera app, so Android needs no `CAMERA` permission in the manifest.
Do NOT add `<uses-permission android:name="android.permission.CAMERA" />` (it would force a runtime prompt for no gain).
Selfies are moved into the app-private cache (`<cache>/ec_selfie`) and deleted right after upload (success or failure),
on logout/lock and when the screen closes. Attendance writes send `accuracy_m` and `fix_time`; fixes with missing accuracy or worse than 100 m are refused on the device.
The offline JSON cache is app-private, strips distance/fence/selfie/coordinate fields and is wiped on logout and inactivity lock.
Location is requested at runtime with an in-app rationale dialog first; denial and "denied forever" show an Open settings action.

### MainActivity.kt

Copy `tool/templates/MainActivity.kt` to `android/app/src/main/kotlin/<package path>/MainActivity.kt` and replace
`__PACKAGE__` with the generated package (default `com.employeeconnect.employee_connect`). Core of it:

```kotlin
class MainActivity : FlutterFragmentActivity() {   // FlutterFragmentActivity: required by local_auth
    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        val messenger = flutterEngine.dartExecutor.binaryMessenger
        MethodChannel(messenger, "employeeconnect/screen").setMethodCallHandler { call, result ->
            if (call.method == "secure") {
                val on = call.arguments as? Boolean ?: true
                runOnUiThread {
                    if (on) window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
                    else window.clearFlags(WindowManager.LayoutParams.FLAG_SECURE)
                    result.success(null)
                }
            } else result.notImplemented()
        }
        MethodChannel(messenger, "employeeconnect/integrity").setMethodCallHandler { call, result ->
            if (call.method == "isCompromised") result.success(isRooted()) else result.notImplemented()
        }
    }
}
```

`isRooted()` (in the template) checks `Build.TAGS` for `test-keys`, well known `su` / Superuser paths and `which su`.
These are heuristics and are bypassable (Magisk DenyList, Frida). For real assurance add Play Integrity and verify the
verdict server side; that needs a backend endpoint and is not part of this change.

### R8 / minify / minSdk (`android/app/build.gradle` or `build.gradle.kts`)

Kotlin DSL:

```kotlin
defaultConfig { minSdk = 23 }          // flutter_secure_storage 9.x and local_auth
buildTypes {
    release {
        isMinifyEnabled = true
        isShrinkResources = true
        proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        signingConfig = signingConfigs.getByName("release")   // NOT the debug key
    }
}
```

Groovy: `minifyEnabled true`, `shrinkResources true`, `proguardFiles getDefaultProguardFile('proguard-android-optimize.txt'), 'proguard-rules.pro'`.

`android/app/proguard-rules.pro`: copy `tool/templates/proguard-rules.pro` (keeps flutter_secure_storage / Tink,
androidx.biometric / local_auth, `MainActivity`; strips `Log.v/d/i`).

### Release build command

```
flutter build apk --release --split-per-abi --obfuscate --split-debug-info=symbols/ \
  --dart-define=API_BASE_URL=https://<server>/api \
  --dart-define=CERT_PINS=<pin1>,<backup-pin>
```

Add a CI check that the release command contains `CERT_PINS=` with two values (the app also refuses to start without them).

### Verify on a device

1. Login screen: screenshot is blocked / blank, recents thumbnail is blank.
2. `adb shell run-as com.employeeconnect.employee_connect ls` shows no restorable data; `adb backup` yields an empty archive.
3. Rooted test device or emulator with `su`: banner appears (Admin gets the blocked screen).
4. Install a user CA and run a MITM proxy: requests fail (pinning + network security config).

## iOS

Requires macOS and Xcode to build; the file edits can be done anywhere.

### AppDelegate.swift

Copy `tool/templates/AppDelegate.swift` over `ios/Runner/AppDelegate.swift` (classic template). If Flutter generated a
SceneDelegate / implicit-engine template, merge instead: register the two channels where the engine is available and keep
the overrides `applicationWillResignActive` (show an opaque cover view), `applicationDidBecomeActive` (remove it unless
`UIScreen.main.isCaptured` while secure), plus the `UIScreen.capturedDidChangeNotification` observer. iOS has no
FLAG_SECURE equivalent, so the app-switcher snapshot is handled with the cover view and screen recording / mirroring with
`isCaptured`. Screenshots themselves cannot be blocked on iOS.

### Info.plist (`ios/Runner/Info.plist`)

- ATS: leave App Transport Security at its default (secure). Do NOT add `NSAppTransportSecurity`,
  `NSAllowsArbitraryLoads` or `NSExceptionDomains`. `bootstrap.ps1` removes any such block.
- Add `NSFaceIDUsageDescription` (required by local_auth):

```xml
<key>NSFaceIDUsageDescription</key>
<string>Unlock Employee Connect with Face ID.</string>
```

- Location (attendance check-in/out, foreground only) and camera (selfie) usage strings:

```xml
<key>NSLocationWhenInUseUsageDescription</key>
<string>Employee Connect uses your location when you check in or out to confirm you are at the office.</string>
<key>NSCameraUsageDescription</key>
<string>Employee Connect uses the camera to take an optional selfie when you check in.</string>
```

  iOS "Precise Location" off (reduced accuracy) is detected and shows "Precise location is required" with an Open settings action.
  No `UIBackgroundModes` location entry is needed for attendance. `bootstrap.ps1` adds both keys.
- Profile photo and announcement image pickers (gallery) (module 06/07):

```xml
<key>NSPhotoLibraryUsageDescription</key>
<string>Employee Connect lets you choose a profile photo or an announcement image.</string>
```

  `NSCameraUsageDescription` is also used for the profile photo (update its text to mention it). The announcement PDF
  picker (`file_picker`) uses the system document picker and needs no permission on Android or iOS. NOT yet in `bootstrap.ps1`: add the
  photo-library key by hand.
- Keychain: `flutter_secure_storage` is configured with `first_unlock_this_device` (no backup migration) in Dart.
- Optional: set the "Data Protection" capability to "Complete" in Xcode for the Runner target.
- Release: `flutter build ipa --release --obfuscate --split-debug-info=symbols/ --dart-define=...` (same defines as Android).
- Jailbreak heuristics in the template (Cydia paths, sandbox write test) are bypassable; use App Attest for real assurance.

## FCM (later)

When `firebase_messaging` is added, override `pushTokenSourceProvider` (`lib/core/push/push_token_source.dart`) so logout
calls `DELETE /me/device-tokens/{fcm_token}`. The repository already does this when a token source returns a value.
