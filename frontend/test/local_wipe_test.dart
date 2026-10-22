import 'dart:io';

import 'package:employee_connect/core/api/cert_pinning.dart';
import 'package:employee_connect/core/security/biometric_gate.dart';
import 'package:employee_connect/core/security/device_integrity.dart';
import 'package:employee_connect/core/storage/app_state_store.dart';
import 'package:employee_connect/core/storage/secure_storage.dart';
import 'package:employee_connect/core/wipe/local_wipe.dart';
import 'package:employee_connect/features/auth/data/auth_repository.dart';
import 'package:employee_connect/features/auth/presentation/auth_controller.dart';
import 'package:employee_connect/features/auth/presentation/auth_state.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fakes.dart';

class _Clean implements DeviceIntegrity {
  @override
  Future<bool> isCompromised() async => false;
}

void main() {
  test('LocalWipe.all deletes picker cache copies in temp and app cache dirs', () async {
    final root = Directory.systemTemp.createTempSync('ec_wipe');
    addTearDown(() => root.deleteSync(recursive: true));
    final tmp = Directory('${root.path}/tmp')..createSync();
    final cache = Directory('${root.path}/cache')..createSync();
    File('${tmp.path}/picked.jpg').writeAsStringSync('x');
    Directory('${tmp.path}/file_picker').createSync();
    File('${tmp.path}/file_picker/a.pdf').writeAsStringSync('x');
    File('${cache.path}/logo.png').writeAsStringSync('x');

    final c = ProviderContainer(overrides: [
      tokenStoreProvider.overrideWithValue(MemoryTokenStore()),
      appStateStoreProvider.overrideWithValue(MemoryAppStateStore()),
      wipeCacheDirsProvider.overrideWithValue(() async => [tmp, cache]),
    ],);
    addTearDown(c.dispose);

    await c.read(localWipeProvider).all(keepServer: true);

    expect(tmp.listSync(), isEmpty);
    expect(cache.listSync(), isEmpty);
  });

  test('offline limit on resume locks the UI; null hours means 72', () async {
    final store = MemoryTokenStore()
      ..access = 'a'
      ..refresh = 'r';
    final appState = MemoryAppStateStore(); // offlineHours stays null
    final c = ProviderContainer(overrides: [
      authRepositoryProvider.overrideWithValue(FakeAuthRepository()),
      tokenStoreProvider.overrideWithValue(store),
      appStateStoreProvider.overrideWithValue(appState),
      biometricGateProvider.overrideWithValue(FakeBiometricGate()),
      deviceIntegrityProvider.overrideWithValue(_Clean()),
      wipeCacheDirsProvider.overrideWithValue(() async => []),
    ],);
    addTearDown(c.dispose);
    final ctl = c.read(authControllerProvider.notifier);
    await Future<void>.delayed(Duration.zero);
    await ctl.login('EMP001', 'pw');
    expect(c.read(authControllerProvider), isA<AuthAuthenticated>());

    await store.writeLastOk(DateTime.now().subtract(const Duration(hours: 10)));
    await ctl.onResume();
    expect(c.read(authControllerProvider), isA<AuthAuthenticated>());

    await store.writeLastOk(DateTime.now().subtract(const Duration(hours: 73)));
    await ctl.onResume();
    expect(c.read(authControllerProvider), isA<AuthLocked>());
  });

  test('pinning host check requires the saved server host', () {
    expect(hostMatches('HR.example.com', 'hr.example.com'), isTrue);
    expect(hostMatches('evil.example.com', 'hr.example.com'), isFalse);
    expect(hostMatches('anything', null), isTrue);
  });
}
