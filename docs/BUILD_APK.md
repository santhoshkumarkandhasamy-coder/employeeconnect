# Build the Android app (APK)

**Easiest: GitHub builds it.** Repository -> Actions -> "Android APK" -> Run workflow, then download
`employee-connect-apk` from the run's Artifacts. Add the secrets listed at the top of
`.github/workflows/android-apk.yml` (server URL, certificate pins, signing key) to get the release APK for employees;
without them you get a test APK that asks for the server address on first start.

To build it on your own computer instead, follow the steps below.

The app is built on a computer with Flutter and the Android SDK, then the `.apk` is installed on the
employees' Android phones. The server must be running first (see `SETUP.md` / `DEPLOY.md`).

## 1. One-time setup on the build computer
1. Install **Flutter (stable channel)**: https://docs.flutter.dev/get-started/install
2. Install **Android Studio** (it installs the Android SDK). In Android Studio: SDK Manager -> install the latest
   *Android SDK Platform* and *Android SDK Command-line Tools*.
3. Install **Java 17** (Android Studio ships one; `flutter doctor` finds it).
4. Accept licences and check everything is green:
   ```bash
   flutter doctor --android-licenses
   flutter doctor
   ```

## 2. Create the company signing key (once, keep it safe)
Every update must be signed with the SAME key, otherwise phones refuse to install it over the old app.
```bash
cd frontend/android
keytool -genkey -v -keystore employee-connect.jks -keyalg RSA -keysize 2048 -validity 10000 -alias employeeconnect
```
Create `frontend/android/key.properties` (it is git-ignored, never commit it):
```properties
storeFile=../employee-connect.jks
storePassword=<the keystore password>
keyAlias=employeeconnect
keyPassword=<the key password>
```
`storeFile` is relative to `frontend/android/app`. Back up the `.jks` file and the passwords offline.
Without `key.properties` the APK is signed with the debug key; use that only for testing.

## 3. Get the server certificate pins
A release build only talks to an `https://` server and needs at least TWO certificate pins (the live certificate
and a spare/backup key), so a fake Wi-Fi certificate cannot read employee data.
```bash
deploy/scripts/spki-pin.sh --host hr.yourcompany.com          # pin of the live certificate
deploy/scripts/spki-pin.sh --gen-backup backup-key.pem        # spare key (store it OFFLINE) and its pin
```

## 4. Build
```bash
cd frontend
flutter pub get
flutter build apk --release \
  --dart-define=API_BASE_URL=https://hr.yourcompany.com/api \
  --dart-define=CERT_PINS=<live-pin>,<backup-pin> \
  --obfuscate --split-debug-info=build/debug-info
```
The APK is `frontend/build/app/outputs/flutter-apk/app-release.apk`.
Smaller downloads per phone type: add `--split-per-abi` (then give most phones `app-arm64-v8a-release.apk`).
For Google Play use `flutter build appbundle --release ...` (same `--dart-define` values) instead.

Test build against a local server (debug, http allowed, no pins):
```bash
flutter run --dart-define=API_BASE_URL=http://<pc-ip>:8000/api
```

## 5. Install on phones
- Share the `.apk` (company file share / MDM / USB). On the phone allow "Install unknown apps" for the app used to
  open it, then install.
- An update is installed the same way; data is kept as long as the same signing key is used.
- First start asks for **location** (attendance) and, on Android 13+, **notifications** (task reminders).
  If notifications are refused, task reminders cannot ring; turn them on in Android Settings -> Apps ->
  Employee Connect -> Notifications.

## 6. Version numbers
Increase `version:` in `frontend/pubspec.yaml` for every release (e.g. `0.2.0+2`). The number after `+` must go up,
otherwise Android will not install the update.
