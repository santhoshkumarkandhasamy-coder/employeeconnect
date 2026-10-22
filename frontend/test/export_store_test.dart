import 'dart:io';
import 'dart:typed_data';

import 'package:employee_connect/app/app.dart';
import 'package:employee_connect/core/security/biometric_gate.dart';
import 'package:employee_connect/core/storage/secure_storage.dart';
import 'package:employee_connect/features/auth/data/auth_repository.dart';
import 'package:employee_connect/features/auth/presentation/auth_controller.dart';
import 'package:employee_connect/features/auth/presentation/auth_state.dart';
import 'package:employee_connect/features/food/data/food_repository.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fakes.dart';

/// Auth stand-in whose state a test can flip.
class _CtlAuth extends AuthController {
  @override
  AuthState build() => const AuthAuthenticated(testUser);
  void setState(AuthState s) => state = s;
}

Directory _tmp() => Directory.systemTemp.createTempSync('ec_exports_test');

void main() {
  test('files left from an earlier session are deleted when the store is created', () {
    final dir = _tmp();
    File('${dir.path}${Platform.pathSeparator}old-food.xlsx').writeAsBytesSync([1, 2, 3]);
    File('${dir.path}${Platform.pathSeparator}announcement-x.pdf').writeAsBytesSync([4, 5]);
    final c = ProviderContainer(overrides: [
      exportDirProvider.overrideWithValue(dir),
      authControllerProvider.overrideWith(_CtlAuth.new),
    ],);
    addTearDown(c.dispose);

    c.read(exportStoreProvider);

    expect(dir.existsSync(), isFalse);
  });

  test('a missing folder is fine', () {
    final dir = Directory('${Directory.systemTemp.path}${Platform.pathSeparator}ec_never_created_x');
    final c = ProviderContainer(overrides: [
      exportDirProvider.overrideWithValue(dir),
      authControllerProvider.overrideWith(_CtlAuth.new),
    ],);
    addTearDown(c.dispose);
    expect(() => c.read(exportStoreProvider), returnsNormally);
  });

  test('wiped again on lock and on logout, kept while signed in', () async {
    final dir = _tmp();
    addTearDown(() {
      if (dir.existsSync()) dir.deleteSync(recursive: true);
    });
    final c = ProviderContainer(overrides: [
      exportDirProvider.overrideWithValue(dir),
      authControllerProvider.overrideWith(_CtlAuth.new),
    ],);
    addTearDown(c.dispose);
    final store = c.read(exportStoreProvider);
    final ctl = c.read(authControllerProvider.notifier) as _CtlAuth;

    await store.save(Uint8List.fromList([1]), 'a.xlsx');
    ctl.setState(const AuthAuthenticated(testUser, biometricEnabled: true));
    await Future<void>.delayed(Duration.zero);
    expect(dir.existsSync(), isTrue, reason: 'ordinary state changes keep the file');

    ctl.setState(const AuthLocked(testUser));
    await Future<void>.delayed(Duration.zero);
    expect(dir.existsSync(), isFalse, reason: 'lock wipes');

    await store.save(Uint8List.fromList([2]), 'b.pdf');
    expect(dir.existsSync(), isTrue);
    ctl.setState(const AuthUnauthenticated());
    await Future<void>.delayed(Duration.zero);
    expect(dir.existsSync(), isFalse, reason: 'logout wipes');
  });

  test('share confirmation and share_plus copies are reset on lock', () async {
    final root = Directory.systemTemp.createTempSync('ec_exports_root');
    addTearDown(() {
      if (root.existsSync()) root.deleteSync(recursive: true);
    });
    final dir = Directory('${root.path}${Platform.pathSeparator}ec_exports');
    final copies = Directory('${root.path}${Platform.pathSeparator}share_plus')..createSync();
    File('${copies.path}${Platform.pathSeparator}old.xlsx').writeAsBytesSync([1]);
    final c = ProviderContainer(overrides: [
      exportDirProvider.overrideWithValue(dir),
      authControllerProvider.overrideWith(_CtlAuth.new),
    ],);
    addTearDown(c.dispose);

    c.read(exportStoreProvider);
    expect(copies.existsSync(), isFalse, reason: 'swept at start');

    copies.createSync();
    File('${copies.path}${Platform.pathSeparator}new.xlsx').writeAsBytesSync([1]);
    c.read(shareConfirmedProvider.notifier).state = true;
    (c.read(authControllerProvider.notifier) as _CtlAuth).setState(const AuthLocked(testUser));
    await Future<void>.delayed(Duration.zero);
    expect(copies.existsSync(), isFalse, reason: 'lock wipes share copies');
    expect(c.read(shareConfirmedProvider), isFalse);
  });

  testWidgets('the app creates the store at start-up (sweep runs without any export)', (tester) async {
    final dir = _tmp();
    File('${dir.path}${Platform.pathSeparator}leftover.xlsx').writeAsBytesSync([1]);
    addTearDown(() {
      if (dir.existsSync()) dir.deleteSync(recursive: true);
    });
    await tester.pumpWidget(ProviderScope(
      overrides: [
        exportDirProvider.overrideWithValue(dir),
        authRepositoryProvider.overrideWithValue(FakeAuthRepository()),
        tokenStoreProvider.overrideWithValue(MemoryTokenStore()),
        biometricGateProvider.overrideWithValue(FakeBiometricGate()),
      ],
      child: const EmployeeConnectApp(),
    ),);
    await tester.pump();
    expect(dir.existsSync(), isFalse);
    expect(find.byType(MaterialApp), findsOneWidget);
  });
}
