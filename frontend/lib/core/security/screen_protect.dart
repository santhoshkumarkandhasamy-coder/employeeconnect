import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

/// Blocks screenshots / recents thumbnails on sensitive screens.
///
/// Needs a tiny native handler (no plugin) for channel
/// `employeeconnect/screen`, method `secure` (bool). The exact Kotlin and
/// Swift code is in `frontend/PLATFORM_SETUP.md` and is applied by
/// `frontend/tool/bootstrap.ps1`.
///
/// Fail-closed in release: if the native handler is missing
/// ([MissingPluginException]) the call throws [ScreenProtectUnavailable] and
/// [SecureScreen] refuses to show its content. In debug/test builds a missing
/// handler is tolerated (desktop, widget tests).
///
/// Calls are reference counted: the native flag is changed only on 0 -> 1 and
/// 1 -> 0 transitions, so a replacement screen whose `initState` runs before
/// the old screen's `dispose` stays protected (review M10).
class ScreenProtectUnavailable implements Exception {
  const ScreenProtectUnavailable(this.reason);
  final String reason;
  @override
  String toString() => 'ScreenProtectUnavailable($reason)';
}

abstract final class ScreenProtect {
  static MethodChannel _channel = const MethodChannel('employeeconnect/screen');
  static int _count = 0;

  /// Overrides [kReleaseMode] (tests only).
  @visibleForTesting
  static bool? releaseModeOverride;

  @visibleForTesting
  static void debugReset({MethodChannel? channel}) {
    _count = 0;
    releaseModeOverride = null;
    _channel = channel ?? const MethodChannel('employeeconnect/screen');
  }

  static bool get _release => releaseModeOverride ?? kReleaseMode;

  @visibleForTesting
  static int get holders => _count;

  /// Registers a protected screen. Throws [ScreenProtectUnavailable] in
  /// release when the native side cannot be reached (the holder is not
  /// counted in that case).
  static Future<void> acquire() async {
    _count++;
    if (_count == 1) {
      try {
        await _invoke(secure: true);
      } on ScreenProtectUnavailable {
        _count--;
        rethrow;
      }
    }
  }

  static Future<void> release() async {
    if (_count == 0) return;
    _count--;
    if (_count == 0) {
      try {
        await _invoke(secure: false);
      } on ScreenProtectUnavailable {
        // clearing the flag failing is harmless
      }
    }
  }

  /// Direct, non-counted call kept for app-wide use (for example enabling the
  /// flag for the whole authenticated session).
  static Future<void> setSecure({required bool secure}) => _invoke(secure: secure);

  static Future<void> _invoke({required bool secure}) async {
    // Browser (admin console): there is no native flag; the console relies on
    // short sessions and its CSP instead.
    if (kIsWeb) return;
    try {
      await _channel.invokeMethod<void>('secure', secure);
    } on MissingPluginException {
      if (_release) {
        throw const ScreenProtectUnavailable(
          'native FLAG_SECURE handler is not installed',
        );
      }
      // debug / tests / desktop: no native handler
    } on PlatformException catch (e) {
      if (_release && secure) {
        throw ScreenProtectUnavailable('platform error ${e.code}');
      }
    }
  }
}

/// Wraps a screen so FLAG_SECURE is on while it is visible. When protection
/// cannot be enabled in a release build the content is NOT shown.
class SecureScreen extends StatefulWidget {
  const SecureScreen({required this.child, super.key});
  final Widget child;

  @override
  State<SecureScreen> createState() => _SecureScreenState();
}

class _SecureScreenState extends State<SecureScreen> {
  bool _failed = false;
  bool _held = false;

  /// True once protection is known to be on. Content is not built before
  /// that (review 07 L8), so it can never be captured in the gap.
  bool _ready = false;

  @override
  void initState() {
    super.initState();
    // Another holder already switched the flag on: nothing to wait for, so
    // replacement screens do not flash a placeholder.
    final alreadyProtected = ScreenProtect.holders > 0;
    ScreenProtect.acquire().then((_) {
      _held = true;
      if (!mounted) {
        ScreenProtect.release();
        return;
      }
      if (!_ready) setState(() => _ready = true);
    }, onError: (Object e) {
      if (e is! ScreenProtectUnavailable) throw e;
      if (mounted) setState(() => _failed = true);
    },);
    _ready = alreadyProtected;
  }

  @override
  void dispose() {
    if (_held) ScreenProtect.release();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (_failed) {
      return const Scaffold(
        body: Center(
          child: Padding(
            padding: EdgeInsets.all(24),
            child: Text(
              'Screen protection is unavailable on this build, so this screen '
              'cannot be shown. Please reinstall the app from the official source.',
              textAlign: TextAlign.center,
            ),
          ),
        ),
      );
    }
    if (!_ready) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }
    return widget.child;
  }
}
