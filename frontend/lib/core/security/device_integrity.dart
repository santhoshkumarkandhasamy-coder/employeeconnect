import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../config/env.dart';

/// Root / jailbreak detection (plan 01, open decision O3, review H2).
///
/// Policy: the Admin role is blocked on a compromised device; other roles get
/// a warning banner. The check is re-run on every app resume.
abstract class DeviceIntegrity {
  Future<bool> isCompromised();
}

/// Lightweight heuristic check implemented natively (no third party
/// dependency) behind channel `employeeconnect/integrity`, method
/// `isCompromised` -> bool. The Kotlin / Swift code (su binaries, test-keys
/// build tags, writable system paths, Cydia / jailbreak paths, sandbox write
/// test, debugger/hook indicators) is in `frontend/PLATFORM_SETUP.md` and is
/// applied by `frontend/tool/bootstrap.ps1`.
///
/// Heuristics are bypassable by a determined attacker (Magisk DenyList,
/// Frida). For strong assurance integrate Play Integrity / App Attest and
/// verify server side; until then this raises the bar and fails CLOSED:
///
/// - release build, native handler missing or erroring -> treated as
///   COMPROMISED (Admin blocked, others see the banner), unless the build was
///   made with `--dart-define=ALLOW_UNVERIFIED_DEVICE=true`;
/// - debug / test builds -> an unavailable check is treated as clean so
///   emulators and desktop keep working.
class PlatformDeviceIntegrity implements DeviceIntegrity {
  PlatformDeviceIntegrity({
    MethodChannel? channel,
    bool? releaseMode,
    bool? allowUnverified,
  })  : _channel = channel ?? const MethodChannel('employeeconnect/integrity'),
        _release = releaseMode ?? kReleaseMode,
        _allowUnverified = allowUnverified ?? Env.allowUnverifiedDevice;

  final MethodChannel _channel;
  final bool _release;
  final bool _allowUnverified;

  @override
  Future<bool> isCompromised() async {
    try {
      final r = await _channel.invokeMethod<bool>('isCompromised');
      if (r == null) return _failureResult();
      return r;
    } on MissingPluginException {
      return _failureResult();
    } on PlatformException {
      return _failureResult();
    }
  }

  bool _failureResult() => _release && !_allowUnverified;
}

final deviceIntegrityProvider =
    Provider<DeviceIntegrity>((ref) => PlatformDeviceIntegrity());
