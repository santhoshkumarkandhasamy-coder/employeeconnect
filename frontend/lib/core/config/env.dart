import 'package:flutter/foundation.dart' show kReleaseMode;

/// Build-time configuration, supplied with `--dart-define`.
///
/// flutter build apk --dart-define=API_BASE_URL=https://hr.example.com/api \
///   --dart-define=CERT_PINS=<b64-spki-sha256>,<b64-backup-spki-sha256>
abstract final class Env {
  static const String apiBaseUrl = String.fromEnvironment(
    'API_BASE_URL',
    defaultValue: 'https://localhost/api',
  );

  /// Sent to the server at login (`app_version`); keep in step with pubspec.
  static const String appVersion =
      String.fromEnvironment('APP_VERSION', defaultValue: '0.1.0');

  /// Comma separated base64 SHA-256 hashes of the server certificate SPKI
  /// (at least two in release: leaf/intermediate + backup).
  /// Empty is allowed in debug/profile builds only.
  static const String _certPins = String.fromEnvironment('CERT_PINS');

  /// Release builds fail closed when the device integrity check cannot run
  /// (missing native handler, platform error). Set
  /// `--dart-define=ALLOW_UNVERIFIED_DEVICE=true` ONLY for internal builds.
  static const bool allowUnverifiedDevice =
      bool.fromEnvironment('ALLOW_UNVERIFIED_DEVICE');

  static List<String> get certPins => parsePins(_certPins);

  static List<String> parsePins(String raw) => raw
      .split(',')
      .map((e) => e.trim())
      .where((e) => e.isNotEmpty)
      .toList(growable: false);

  /// Minimum pins required in a release build.
  static const int minReleasePins = 2;

  /// Throws [StateError] when the transport configuration is unsafe for a
  /// release build (plain http, or fewer than [minReleasePins] pins).
  /// [releaseMode] defaults to [kReleaseMode]; overridable for tests.
  static void assertSecureTransport({
    required String baseUrl,
    required List<String> pins,
    bool releaseMode = kReleaseMode,
  }) {
    if (!releaseMode) return;
    final uri = Uri.tryParse(baseUrl);
    if (uri == null || uri.scheme != 'https' || uri.host.isEmpty) {
      throw StateError(
        'Release build refused: API_BASE_URL must be an https:// URL '
        '(got "$baseUrl").',
      );
    }
    if (pins.length < minReleasePins) {
      throw StateError(
        'Release build refused: CERT_PINS must contain at least '
        '$minReleasePins SPKI pins (got ${pins.length}).',
      );
    }
  }
}
