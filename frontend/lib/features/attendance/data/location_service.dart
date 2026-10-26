import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:geolocator/geolocator.dart';

import 'location_fix.dart';

export 'location_fix.dart';

/// [reducedAccuracy]: permission is granted but only "approximate" location
/// (Android 12+ approximate grant, iOS reduced accuracy). Check-in needs precise.
enum LocationAccess { granted, denied, deniedForever, serviceOff, reducedAccuracy }

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
