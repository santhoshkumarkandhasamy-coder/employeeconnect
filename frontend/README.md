# Employee Connect - Flutter app

Lightweight by design: Riverpod, go_router, Dio, flutter_secure_storage, local_auth, crypto. Material 3, system fonts, no image assets.

## First-time setup
Platform folders are not committed yet. Generate them in place (keeps `lib/`, `pubspec.yaml`):
```
flutter create --org com.employeeconnect --project-name employee_connect --platforms android,ios .
flutter pub get
```
Preferred: run `tool/bootstrap.ps1`, which does the above plus the security hardening (allowBackup=false,
no cleartext, FLAG_SECURE and root-check channels, R8, iOS ATS / cover view). See `PLATFORM_SETUP.md`.
Release builds fail closed without those native parts, an https `API_BASE_URL` and two `CERT_PINS`.

## Run
```
flutter run --dart-define=API_BASE_URL=https://<server>/api
```

## Release (small + obfuscated)
```
flutter build apk --release --split-per-abi --obfuscate --split-debug-info=symbols/ \
  --dart-define=API_BASE_URL=https://<server>/api \
  --dart-define=CERT_PINS=<b64-spki-sha256>,<backup-pin>
flutter build appbundle --release --obfuscate --split-debug-info=symbols/ ...
flutter build ipa --release --obfuscate --split-debug-info=symbols/ ...
```
Android: enable `minifyEnabled true` and `shrinkResources true` in the release build type (R8).
Keep `symbols/` privately to symbolicate crashes. Pin value:
`openssl x509 -in cert.pem -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64`.

## Checks
```
flutter analyze
flutter test
```
