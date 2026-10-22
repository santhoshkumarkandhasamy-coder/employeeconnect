
import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/core/server/company.dart';
import 'package:employee_connect/core/server/server_providers.dart';
import 'package:employee_connect/core/storage/app_state_store.dart';
import 'package:employee_connect/core/storage/secure_storage.dart';
import 'package:employee_connect/features/admin/data/admin_repository.dart';
import 'package:employee_connect/features/admin/domain/ops_models.dart';
import 'package:employee_connect/features/admin/presentation/admin_widgets.dart';
import 'package:employee_connect/features/admin/presentation/company_screen.dart';
import 'package:employee_connect/features/admin/presentation/license_screen.dart';
import 'package:employee_connect/features/admin/presentation/sessions_screen.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart' show Paged;
import 'package:employee_connect/features/auth/presentation/access_removed_screen.dart';
import 'package:employee_connect/features/auth/presentation/auth_controller.dart';
import 'package:employee_connect/features/auth/presentation/auth_state.dart';
import 'package:employee_connect/features/auth/presentation/server_setup_screen.dart';
import 'package:employee_connect/features/auth/data/auth_repository.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fakes.dart';
import 'helpers/module_fakes.dart';

class _Repo extends AdminRepository {
  _Repo() : super(Dio());

  final List<String> savedNames = [];
  final List<String> installed = [];
  final List<String> revoked = [];
  int revokeAllCalls = 0;
  LicenseInfo lic = LicenseInfo.fromJson(const {
    'status': 'valid',
    'max_users': 50,
    'active_users': 47,
    'seats_available': 3,
    'on_expiry': 'block_new_users',
    'deployment_id': 'dep-123',
    'licensed_to': 'Acme',
  });
  ApiException? installError;
  List<SessionItem> sessionItems = [];

  @override
  Future<CompanyAdmin> saveCompanyName(String name) async {
    savedNames.add(name);
    return CompanyAdmin(name: name, configured: true);
  }

  @override
  Future<CompanyAdmin> uploadCompanyLogo(String filename, List<int> bytes) async =>
      const CompanyAdmin(name: 'Acme', logoVersion: 1, configured: true);

  @override
  Future<LicenseInfo> license() async => lic;

  @override
  Future<LicenseInfo> installLicense(String key, {required String currentPassword}) async {
    final e = installError;
    if (e != null) throw e;
    installed.add('$key|$currentPassword');
    return lic;
  }

  @override
  Future<Paged<SessionItem>> sessions({String? employeeId, String? clientType, int page = 1}) async =>
      Paged(items: sessionItems, page: 1, total: sessionItems.length, pageSize: 20);

  @override
  Future<Paged<SessionItem>> employeeSessions(String userId, {int page = 1}) async =>
      Paged(items: sessionItems, page: 1, total: sessionItems.length, pageSize: 20);

  @override
  Future<void> revokeSession(String id) async => revoked.add(id);

  @override
  Future<int> revokeAllSessions(String userId) async {
    revokeAllCalls++;
    return 2;
  }
}

class _Picker implements LogoPicker {
  _Picker(this.image);
  final PickedImage? image;
  @override
  Future<PickedImage?> pick() async => image;
}

void _tall(WidgetTester t) {
  t.view.physicalSize = const Size(800, 2000);
  t.view.devicePixelRatio = 1;
  addTearDown(t.view.reset);
}

List<Override> _ov(_Repo repo, List<String> perms, {LogoPicker? picker}) => [
      adminRepositoryProvider.overrideWithValue(repo),
      authControllerProvider.overrideWith(() => FakeAuth(userWith(perms))),
      if (picker != null) logoPickerProvider.overrideWithValue(picker),
    ];

void main() {
  group('server setup', () {
    ServerProbeResult found() => const ServerProbeResult(
          CompanyInfo(name: 'Acme Pvt Ltd', configured: true, product: 'employee-connect'),
        );

    Widget host(ServerProbe probe, {bool release = true}) => ProviderScope(
          overrides: [
            serverProbeProvider.overrideWithValue(probe),
            tokenStoreProvider.overrideWithValue(MemoryTokenStore()),
            appStateStoreProvider.overrideWithValue(MemoryAppStateStore()),
            authRepositoryProvider.overrideWithValue(FakeAuthRepository()),
            serverSetupRequiredProvider.overrideWith((ref) => true),
          ],
          child: MaterialApp(home: ServerSetupScreen(release: release)),
        );

    testWidgets('rejects an invalid or http address without calling the server', (tester) async {
      var calls = 0;
      await tester.pumpWidget(host((_) async {
        calls++;
        return found();
      }),);
      await tester.enterText(find.byType(TextFormField), 'http://hr.company.com');
      await tester.tap(find.text('Continue'));
      await tester.pump();
      expect(find.textContaining('valid https address'), findsOneWidget);
      expect(calls, 0);
    });

    testWidgets('shows the company name for confirmation, then saves the server', (tester) async {
      String? probed;
      await tester.pumpWidget(host((url) async {
        probed = url;
        return found();
      }),);
      await tester.enterText(find.byType(TextFormField), 'hr.company.com');
      await tester.tap(find.text('Continue'));
      await tester.pumpAndSettle();
      expect(probed, 'https://hr.company.com/api');
      expect(find.text('Acme Pvt Ltd'), findsOneWidget);
      expect(find.text('Yes, use this server'), findsOneWidget);

      await tester.tap(find.text('Yes, use this server'));
      await tester.pumpAndSettle();
      final c = ProviderScope.containerOf(tester.element(find.byType(ServerSetupScreen)));
      expect(c.read(baseUrlProvider), 'https://hr.company.com/api');
      expect(c.read(serverSetupRequiredProvider), isFalse);
      expect(await c.read(appStateStoreProvider).readServerUrl(), 'https://hr.company.com/api');
      expect(c.read(authControllerProvider), isA<AuthUnauthenticated>());
    });

    testWidgets('a server that is not Employee Connect shows an error', (tester) async {
      await tester.pumpWidget(host((_) async => throw const ApiException(
            code: 'NOT_EMPLOYEE_CONNECT',
            message: 'That address is not an Employee Connect server.',
          ),),);
      await tester.enterText(find.byType(TextFormField), 'example.com');
      await tester.tap(find.text('Continue'));
      await tester.pumpAndSettle();
      expect(find.textContaining('not an Employee Connect server'), findsOneWidget);
      expect(find.text('Yes, use this server'), findsNothing);
    });
  });

  testWidgets('access removed screen explains and offers another sign-in', (tester) async {
    await tester.pumpWidget(ProviderScope(
      overrides: [
        authRepositoryProvider.overrideWithValue(FakeAuthRepository()),
        tokenStoreProvider.overrideWithValue(MemoryTokenStore()),
        appStateStoreProvider.overrideWithValue(MemoryAppStateStore()),
      ],
      child: const MaterialApp(home: AccessRemovedScreen()),
    ),);
    expect(find.text('Your access has been removed.'), findsOneWidget);
    expect(find.textContaining('Contact HR'), findsOneWidget);
    await tester.tap(find.text('Sign in with another account'));
    await tester.pumpAndSettle();
    final c = ProviderScope.containerOf(tester.element(find.byType(AccessRemovedScreen)));
    expect(c.read(authControllerProvider), isA<AuthUnauthenticated>());
  });

  group('company setup wizard', () {
    testWidgets('validates the name and saves it with an optional logo', (tester) async {
      _tall(tester);
      final repo = _Repo();
      CompanyInfo? done;
      await tester.pumpWidget(app(
        Scaffold(body: CompanySetupView(onConfigured: (c) => done = c)),
        _ov(repo, ['company.manage'], picker: _Picker(PickedImage('logo.png', tinyPng))),
      ),);
      await tester.tap(find.text('Finish setup'));
      await tester.pump();
      expect(find.text('Enter at least 2 characters'), findsOneWidget);
      expect(repo.savedNames, isEmpty);

      await tester.enterText(find.byType(TextFormField), 'Acme Pvt Ltd');
      await tester.tap(find.text('Choose logo (optional)'));
      await tester.pump();
      await tester.tap(find.text('Finish setup'));
      await tester.pumpAndSettle();
      expect(repo.savedNames, ['Acme Pvt Ltd']);
      expect(done?.configured, isTrue);
      expect(done?.logoVersion, 1);
    });

    testWidgets('a wrong logo type is refused before upload', (tester) async {
      _tall(tester);
      final repo = _Repo();
      await tester.pumpWidget(app(
        Scaffold(body: CompanySetupView(onConfigured: (_) {})),
        _ov(repo, ['company.manage'], picker: _Picker(PickedImage('logo.svg', tinyPng))),
      ),);
      await tester.tap(find.text('Choose logo (optional)'));
      await tester.pump();
      expect(find.text('Choose a PNG or JPEG image'), findsOneWidget);
    });
  });

  group('license screen', () {
    testWidgets('shows seats, status and deployment id', (tester) async {
      _tall(tester);
      await tester.pumpWidget(app(const LicenseScreen(), _ov(_Repo(), ['license.manage'])));
      await tester.pumpAndSettle();
      expect(find.text('Seats 47 / 50'), findsOneWidget);
      expect(find.text('Valid'), findsOneWidget);
      expect(find.text('dep-123'), findsOneWidget);
    });

    testWidgets('a malformed key never reaches the server', (tester) async {
      _tall(tester);
      final repo = _Repo();
      await tester.pumpWidget(app(const LicenseScreen(), _ov(repo, ['license.manage'])));
      await tester.pumpAndSettle();
      await tester.enterText(find.widgetWithText(TextFormField, 'License key'), 'nonsense');
      await tester.tap(find.text('Install license'));
      await tester.pump();
      expect(find.textContaining('not a license key'), findsOneWidget);
      expect(repo.installed, isEmpty);
    });

    testWidgets('install asks for the password (step-up) and sends both', (tester) async {
      _tall(tester);
      final repo = _Repo();
      await tester.pumpWidget(app(const LicenseScreen(), _ov(repo, ['license.manage'])));
      await tester.pumpAndSettle();
      await tester.enterText(find.widgetWithText(TextFormField, 'License key'), 'ECL1.aaa.bbb');
      await tester.tap(find.text('Install license'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byKey(const ValueKey('reauth-password')), 'MyPass1!');
      await tester.tap(find.text('Confirm'));
      await tester.pumpAndSettle();
      expect(repo.installed, ['ECL1.aaa.bbb|MyPass1!']);
    });

    testWidgets('without license.manage nothing is requested', (tester) async {
      await tester.pumpWidget(app(const LicenseScreen(), _ov(_Repo(), const [])));
      await tester.pumpAndSettle();
      expect(find.textContaining('do not have permission'), findsOneWidget);
    });

    testWidgets('an expired license shows the banner', (tester) async {
      _tall(tester);
      final repo = _Repo()
        ..lic = LicenseInfo.fromJson(const {
          'status': 'expired',
          'max_users': 50,
          'active_users': 10,
          'seats_available': 40,
          'on_expiry': 'read_only',
          'deployment_id': 'd',
        });
      await tester.pumpWidget(app(const LicenseScreen(), _ov(repo, ['license.manage'])));
      await tester.pumpAndSettle();
      expect(find.textContaining('read-only'), findsOneWidget);
    });
  });

  group('sessions', () {
    final items = [
      SessionItem.fromJson(const {
        'id': 'f1',
        'user': {'id': 'u', 'employee_id': 'E1', 'full_name': 'Asha'},
        'client_type': 'mobile',
        'platform': 'android',
        'device_name': 'Pixel 8',
        'ip': '203.0.113.7',
      }),
    ];

    testWidgets('lists sessions and revokes one after confirmation', (tester) async {
      _tall(tester);
      final repo = _Repo()..sessionItems = items;
      await tester.pumpWidget(app(const SessionsScreen(), _ov(repo, ['session.manage'])));
      await tester.pumpAndSettle();
      expect(find.textContaining('Asha'), findsOneWidget);
      await tester.tap(find.byTooltip('Sign out this device'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Sign out'));
      await tester.pumpAndSettle();
      expect(repo.revoked, ['f1']);
    });

    testWidgets('empty state and per-employee sign out of all devices', (tester) async {
      _tall(tester);
      final repo = _Repo();
      await tester.pumpWidget(app(
        const SessionsScreen(userId: 'u1', title: 'Asha'),
        _ov(repo, ['session.manage']),
      ),);
      await tester.pumpAndSettle();
      expect(find.text('No active sessions.'), findsOneWidget);
      await tester.tap(find.byTooltip('Sign out all devices'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Sign out all'));
      await tester.pumpAndSettle();
      expect(repo.revokeAllCalls, 1);
    });

    testWidgets('requires session.manage', (tester) async {
      await tester.pumpWidget(app(const SessionsScreen(), _ov(_Repo(), const [])));
      await tester.pumpAndSettle();
      expect(find.textContaining('do not have permission'), findsOneWidget);
    });
  });

  testWidgets('offboarding asks for a reason and returns it', (tester) async {
    DeactivationReason? picked;
    await tester.pumpWidget(MaterialApp(
      home: Builder(
        builder: (context) => TextButton(
          onPressed: () async => picked = await pickDeactivationReason(context, 'Asha'),
          child: const Text('go'),
        ),
      ),
    ),);
    await tester.tap(find.text('go'));
    await tester.pumpAndSettle();
    expect(find.text('Deactivate Asha?'), findsOneWidget);
    await tester.tap(find.text('Terminated'));
    await tester.pump();
    await tester.tap(find.text('Deactivate'));
    await tester.pumpAndSettle();
    expect(picked, DeactivationReason.terminated);
  });
}
