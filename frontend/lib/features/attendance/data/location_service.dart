import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:geolocator/geolocator.dart';

/// [reducedAccuracy]: permission is granted but only "approximate" location
/// (Android 12+ approximate grant, iOS reduced accuracy). Check-in needs precise.
enum LocationAccess { granted, denied, deniedForever, serviceOff, reducedAccuracy }

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

class LocationException implements Exception {
  const LocationException(this.message);
  final String message;
  @override
  String toString() => 'LocationException($message)';
}

/// Foreground location only (no background tracking for attendance).
abstract class LocationService {
  /// Current state without prompting.
  Future<LocationAccess> access();

  /// Prompts when permission is `denied`; returns the resulting state.
  Future<LocationAccess> request();

  Future<LocationFix> current();

  /// Opens the OS screen where the user can fix [access].
  Future<void> openSettings(LocationAccess access);
}

class GeolocatorLocationService implements LocationService {
  const GeolocatorLocationService();

  LocationAccess _map(LocationPermission p) => switch (p) {
        LocationPermission.always || LocationPermission.whileInUse => LocationAccess.granted,
        LocationPermission.deniedForever => LocationAccess.deniedForever,
        LocationPermission.denied || LocationPermission.unableToDetermine => LocationAccess.denied,
      };

  @override
  Future<LocationAccess> access() async {
    if (!await Geolocator.isLocationServiceEnabled()) return LocationAccess.serviceOff;
    return _precise(_map(await Geolocator.checkPermission()));
  }

  /// A granted permission can still be approximate-only.
  Future<LocationAccess> _precise(LocationAccess a) async {
    if (a != LocationAccess.granted) return a;
    try {
      if (await Geolocator.getLocationAccuracy() == LocationAccuracyStatus.reduced) {
        return LocationAccess.reducedAccuracy;
      }
    } on Object {
      // Unsupported: the accuracy gate on the fix itself still applies.
    }
    return a;
  }

  @override
  Future<LocationAccess> request() async {
    if (!await Geolocator.isLocationServiceEnabled()) return LocationAccess.serviceOff;
    return _precise(_map(await Geolocator.requestPermission()));
  }

  @override
  Future<LocationFix> current() async {
    try {
      final p = await Geolocator.getCurrentPosition(
        locationSettings: const LocationSettings(
          accuracy: LocationAccuracy.best,
          timeLimit: Duration(seconds: 20),
        ),
      );
      return LocationFix(
        lat: p.latitude,
        lng: p.longitude,
        accuracyM: p.accuracy,
        isMock: p.isMocked,
        timestamp: p.timestamp,
      );
    } on Exception {
      throw const LocationException('Could not get your location. Move to an open area and try again.');
    }
  }

  @override
  Future<void> openSettings(LocationAccess access) async {
    if (access == LocationAccess.serviceOff) {
      await Geolocator.openLocationSettings();
    } else {
      await Geolocator.openAppSettings();
    }
  }
}

final locationServiceProvider = Provider<LocationService>((_) => const GeolocatorLocationService());
