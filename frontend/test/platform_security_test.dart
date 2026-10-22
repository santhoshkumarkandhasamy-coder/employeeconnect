import 'package:employee_connect/core/security/device_integrity.dart';
import 'package:employee_connect/core/security/screen_protect.dart';
import 'package:employee_connect/features/auth/presentation/auth_controller.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;

  group('ScreenProtect', () {
    const channel = MethodChannel('employeeconnect/screen');
    late List<bool> calls;

    setUp(() {
      // flutter_test_config.dart installs a no-op handler; these tests need none.
      messenger.setMockMethodCallHandler(channel, null);
      ScreenProtect.debugReset();
      calls = [];
    });
    tearDown(() {
      messenger.setMockMethodCallHandler(channel, null);
      ScreenProtect.debugReset();
    });

    void install() => messenger.setMockMethodCallHandler(channel, (c) async {
          calls.add(c.arguments as bool);
          return null;
        });

    test('release without native handler throws (fail closed)', () async {
      ScreenProtect.releaseModeOverride = true;
      await expectLater(
        ScreenProtect.acquire(),
        throwsA(isA<ScreenProtectUnavailable>()),
      );
      expect(ScreenProtect.holders, 0);
    });

    test('debug without native handler is tolerated', () async {
      ScreenProtect.releaseModeOverride = false;
      await ScreenProtect.acquire();
      expect(ScreenProtect.holders, 1);
    });

    test('reference counted: native flag only changes on 0<->1', () async {
      install();
      await ScreenProtect.acquire(); // login
      await ScreenProtect.acquire(); // change-password initState before login dispose
      await ScreenProtect.release(); // login dispose
      expect(calls, [true]);
      await ScreenProtect.release();
      expect(calls, [true, false]);
    });
  });

  group('PlatformDeviceIntegrity', () {
    const channel = MethodChannel('employeeconnect/integrity');
    tearDown(() => messenger.setMockMethodCallHandler(channel, null));

    test('reports the native result', () async {
      messenger.setMockMethodCallHandler(channel, (_) async => true);
      expect(await PlatformDeviceIntegrity(releaseMode: true).isCompromised(), isTrue);
      messenger.setMockMethodCallHandler(channel, (_) async => false);
      expect(await PlatformDeviceIntegrity(releaseMode: true).isCompromised(), isFalse);
    });

    test('release: missing handler is treated as compromised', () async {
      expect(await PlatformDeviceIntegrity(releaseMode: true).isCompromised(), isTrue);
    });

    test('release + ALLOW_UNVERIFIED_DEVICE: missing handler tolerated', () async {
      final i = PlatformDeviceIntegrity(releaseMode: true, allowUnverified: true);
      expect(await i.isCompromised(), isFalse);
    });

    test('release: platform error fails closed', () async {
      messenger.setMockMethodCallHandler(channel, (_) async {
        throw PlatformException(code: 'boom');
      });
      expect(await PlatformDeviceIntegrity(releaseMode: true).isCompromised(), isTrue);
    });

    test('debug: unavailable check is clean', () async {
      expect(await PlatformDeviceIntegrity(releaseMode: false).isCompromised(), isFalse);
    });
  });

  group('session messages (B4)', () {
    test('revoked and reused tokens differ', () {
      final reused = AuthController.sessionMessage('TOKEN_REUSED');
      final revoked = AuthController.sessionMessage('SESSION_REVOKED');
      expect(reused, isNot(revoked));
      expect(revoked, contains('elsewhere'));
      expect(reused, contains('security'));
      expect(revoked, contains('password'));
      expect(AuthController.sessionMessage('ACCOUNT_INACTIVE'), contains('inactive'));
    });
  });
}
