import '../../attendance/data/location_service.dart';
import 'admin/duty_admin_providers.dart' show CurrentPositionReader, PositionReadException;

/// One-shot device position for the admin location form (phone app only).
/// Asks for permission when needed, reads once, never streams.
CurrentPositionReader devicePositionReader(LocationService loc) => () async {
      var access = await loc.access();
      if (access == LocationAccess.denied) access = await loc.request();
      if (access != LocationAccess.granted) {
        throw PositionReadException(switch (access) {
          LocationAccess.serviceOff => 'Turn on location services to use your position.',
          LocationAccess.reducedAccuracy => preciseLocationMessage,
          LocationAccess.deniedForever =>
            'Location permission is blocked. Enable it in Settings, or type the coordinates.',
          _ => 'Location permission was not granted. You can type the coordinates instead.',
        },);
      }
      final LocationFix fix;
      try {
        fix = await loc.current();
      } on LocationException catch (e) {
        throw PositionReadException(e.message);
      }
      final rejection = fixRejection(fix);
      if (rejection != null) throw PositionReadException(rejection);
      return (lat: fix.lat, lng: fix.lng);
    };

const String preciseLocationMessage =
    'Precise location is required. Allow "Precise location" for Employee Connect in Settings.';
