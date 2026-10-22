import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/api/dio_client.dart';
import 'package:employee_connect/core/security/biometric_gate.dart';
import 'package:employee_connect/core/security/device_integrity.dart';
import 'package:employee_connect/core/server/server_providers.dart';
import 'package:employee_connect/core/storage/app_state_store.dart';
import 'package:employee_connect/core/storage/secure_storage.dart';
import 'package:employee_connect/features/auth/data/auth_repository.dart';
import 'package:employee_connect/features/auth/presentation/auth_controller.dart';
import 'package:employee_connect/features/auth/presentation/auth_state.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fakes.dart';

class _CleanDevice implements DeviceIntegrity {
  @override
  Future<bool> isCompromised() async => false;
}

void main() {
  test('first run without a server URL shows server setup', () async {
    final c = ProviderContainer(overrides: [
      authRepositoryProvider.overrideWithValue(FakeAuthRepository()),
      tokenStoreProvider.overrideWithValue(MemoryTokenStore()),
      appStateStoreProvider.overrideWithValue(MemoryAppStateStore()),
      serverSetupRequiredProvider.overrideWith((ref) => true),
    ],);
    addTearDown(c.dispose);
    c.read(authControllerProvider);
    await Future<void>.delayed(Duration.zero);
    expect(c.read(authControllerProvider), isA<AuthNeedsServer>());
  });

  late FakeAuthRepository repo;
  late MemoryTokenStore store;
  late MemoryAppStateStore appState;
  late ProviderContainer container;

  ProviderContainer make({bool withSession = false}) {
    repo = FakeAuthRepository();
    store = MemoryTokenStore();
    appState = MemoryAppStateStore();
    if (withSession) {
      store
        ..access = 'a'
        ..refresh = 'r';
    }
    final c = ProviderContainer(overrides: [
      authRepositoryProvider.overrideWithValue(repo),
      tokenStoreProvider.overrideWithValue(store),
      appStateStoreProvider.overrideWithValue(appState),
      biometricGateProvider.overrideWithValue(FakeBiometricGate()),
      deviceIntegrityProvider.overrideWithValue(_CleanDevice()),
    ],);
    addTearDown(c.dispose);
    return c;
  }

  Future<void> settle() => Future<void>.delayed(Duration.zero);

  Future<void> started(ProviderContainer c) async {
    c.read(authControllerProvider);
    await settle();
    await settle();
  }

  group('mid-session transitions (B2/B3)', () {
    test('ACCOUNT_INACTIVE event wipes local data and shows access removed', () async {
      container = make();
      await started(container);
      await container.read(authControllerProvider.notifier).login('EMP001', 'pw');
      expect(container.read(authControllerProvider), isA<AuthAuthenticated>());
      store
        ..access = 'a'
        ..refresh = 'r';
      appState
        ..url = 'https://hr.example.com/api'
        ..offlineHours = 72;

      container.read(sessionEventsProvider).expire('ACCOUNT_INACTIVE');
      await settle();
      await settle();

      expect(container.read(authControllerProvider), isA<AuthAccessRemoved>());
      expect(store.refresh, isNull);
      expect(appState.offlineHours, isNull);
      expect(appState.url, 'https://hr.example.com/api'); // server URL is kept
    });

    test('a push only verifies via /auth/me and never wipes by itself', () async {
      container = make();
      await started(container);
      await container.read(authControllerProvider.notifier).login('EMP001', 'pw');
      store.refresh = 'r';

      await container.read(authControllerProvider.notifier).verifyAccount();

      expect(container.read(authControllerProvider), isA<AuthAuthenticated>());
      expect(store.refresh, 'r');
    });

    test('verifyAccount with ACCOUNT_INACTIVE from /auth/me removes access', () async {
      container = make();
      await started(container);
      await container.read(authControllerProvider.notifier).login('EMP001', 'pw');
      repo.meError =
          const ApiException(code: 'ACCOUNT_INACTIVE', message: 'x', status: 403);

      await container.read(authControllerProvider.notifier).verifyAccount();

      expect(container.read(authControllerProvider), isA<AuthAccessRemoved>());
    });

    test('PASSWORD_CHANGE_REQUIRED event moves to the change-password state', () async {
      container = make();
      await started(container);
      await container.read(authControllerProvider.notifier).login('EMP001', 'pw');

      container.read(sessionEventsProvider).requirePasswordChange();
      await settle();

      final s = container.read(authControllerProvider);
      expect(s, isA<AuthNeedsPasswordChange>());
      expect((s as AuthNeedsPasswordChange).user.mustChangePassword, isTrue);
    });

    test('SESSION_REVOKED and TOKEN_REUSED give distinct messages', () async {
      container = make();
      await started(container);
      await container.read(authControllerProvider.notifier).login('EMP001', 'pw');
      container.read(sessionEventsProvider).expire('SESSION_REVOKED');
      await settle();
      final revoked =
          (container.read(authControllerProvider) as AuthUnauthenticated).message;
      expect(revoked, contains('elsewhere'));

      await container.read(authControllerProvider.notifier).login('EMP001', 'pw');
      container.read(sessionEventsProvider).expire('TOKEN_REUSED');
      await settle();
      final reused =
          (container.read(authControllerProvider) as AuthUnauthenticated).message;
      expect(reused, contains('security'));
      expect(reused, isNot(revoked));
    });
  });

  group('bootstrap failures', () {
    test('ACCOUNT_INACTIVE wipes and shows access removed', () async {
      container = make(withSession: true);
      repo.meError =
          const ApiException(code: 'ACCOUNT_INACTIVE', message: 'x', status: 403);
      await started(container);
      await settle();

      expect(container.read(authControllerProvider), isA<AuthAccessRemoved>());
      expect(store.refresh, isNull);
    });

    for (final c in [
      (401, 'SESSION_REVOKED', 'elsewhere'),
    ]) {
      test('${c.$2} clears the stored session', () async {
        container = make(withSession: true);
        repo.meError = ApiException(code: c.$2, message: 'x', status: c.$1);
        await started(container);

        final s = container.read(authControllerProvider);
        expect(s, isA<AuthUnauthenticated>());
        expect((s as AuthUnauthenticated).message, contains(c.$3));
        expect(store.refresh, isNull);
      });
    }

    test('PASSWORD_CHANGE_REQUIRED on /auth/me keeps the tokens', () async {
      container = make(withSession: true);
      repo.meError = const ApiException(
        code: 'PASSWORD_CHANGE_REQUIRED',
        message: 'x',
        status: 403,
      );
      await started(container);
      expect(container.read(authControllerProvider), isNot(isA<AuthAuthenticated>()));
      expect(store.refresh, 'r');
    });
  });
}
