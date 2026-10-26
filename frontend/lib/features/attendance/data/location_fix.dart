// One-shot position reading used for punches. Kept free of any platform
// plugin so the web console can share the repositories that carry it.

class LocationFix {
  const LocationFix({
    required this.lat,
    required this.lng,
    required this.accuracyM,
    this.isMock = false,
    this.timestamp,
  });
  final double lat;
  final double lng;

  /// Horizontal accuracy in metres. Zero, negative or NaN means "unknown".
  final double accuracyM;

  /// Platform timestamp of the fix itself (not of the request), sent as `fix_time`.
  final DateTime? timestamp;

  /// Platform "mock location" flag. Logged by the server, never trusted.
  final bool isMock;
}

/// Worst horizontal accuracy accepted for a punch. Coarse/approximate fixes
/// are kilometres wide, so they are always above this.
const double maxAccuracyM = 100;

extension LocationFixChecks on LocationFix {
  bool get hasAccuracy => accuracyM.isFinite && accuracyM > 0;
}

/// User-facing reason the fix must not be submitted, or null when it is usable.
String? fixRejection(LocationFix fix) {
  if (!fix.hasAccuracy || fix.timestamp == null) {
    return 'Your phone did not report a precise location. Turn on Precise location '
        'for Employee Connect and try again.';
  }
  if (fix.accuracyM > maxAccuracyM) {
    return 'Your location is not accurate enough (about ${fix.accuracyM.round()} m). '
        'Move to an open area and try again.';
  }
  return null;
}
