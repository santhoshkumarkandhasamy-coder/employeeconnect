import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:employee_connect/console/console_api.dart';
import 'package:employee_connect/console/console_app.dart';
import 'package:employee_connect/console/console_auth.dart';
import 'package:employee_connect/console/console_shell.dart';
import 'package:employee_connect/console/pages/dashboard_page.dart';
import 'package:employee_connect/core/storage/app_state_store.dart';
import 'package:employee_connect/features/admin/data/admin_repository.dart';
import 'package:employee_connect/features/admin/domain/ops_models.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

typedef Reply = ({int status, Object body});

class _Adapter implements HttpClientAdapter {
  _Adapter(this.handler);
  final Reply Function(RequestOptions) handler;
  final List<RequestOptions> requests = [];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    requests.add(options);
    final r = handler(options);
    return ResponseBody.fromString(
      jsonEncode(r.body),
      r.status,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}

Reply _err(int status, String code) =>
    (status: status, body: {'error': {'code': code, 'message': code}});

const _me = {
  'id': 'u1',
  'employee_id': 'ADM1',
  'full_name': 'Admin One',
  'role': 'admin',
  'permissions': ['console.access', 'employee.view', 'attendance.view_all'],
  'must_change_password': false,
  'company': {'name': 'Acme', 'logo_url': null, 'logo_version': 0, 'configured': true},
};

void main() {
  group('console Dio', () {
    test('sends X-EC-Client and the in-memory bearer', () async {
      final tokens = ConsoleTokens()..access = 'tok';
      final adapter = _Adapter((_) => (status: 200, body: {'ok': true}));
      final dio = createConsoleDio(
        tokens: tokens,
        onSessionEnded: (_) {},
        baseUrl: 'https://x.test/api',
        adapter: adapter,
      );
      await dio.get<dynamic>('/admin/dashboard');
      final h = adapter.requests.single.headers;
      expect(h['X-EC-Client'], 'console');
      expect(h['Authorization'], 'Bearer tok');
    });

    test('a 401 refreshes through the cookie endpoint once and replays', () async {
      final tokens = ConsoleTokens()..access = 'old';
      var calls = 0;
      final adapter = _Adapter((o) {
        if (o.path.endsWith('/auth/web/refresh')) {
          return (status: 200, body: {'access_token': 'new'});
        }
        calls++;
        return o.headers['Authorization'] == 'Bearer new'
            ? (status: 200, body: {'ok': true})
            : _err(401, 'TOKEN_EXPIRED');
      });
      final dio = createConsoleDio(
        tokens: tokens,
        onSessionEnded: (_) {},
        baseUrl: 'https://x.test/api',
        adapter: adapter,
      );
      final res = await dio.get<dynamic>('/admin/dashboard');
      expect(res.statusCode, 200);
      expect(tokens.access, 'new');
      expect(calls, 2);
      expect(adapter.requests.where((r) => r.path.endsWith('/auth/web/refresh')).length, 1);
      // the refresh carries no body: the token is only in the cookie
      expect(adapter.requests.firstWhere((r) => r.path.endsWith('/auth/web/refresh')).data, isNull);
    });

    test('a failed refresh ends the session and clears the token', () async {
      final tokens = ConsoleTokens()..access = 'old';
      String? ended;
      final adapter = _Adapter((o) =>
          o.path.endsWith('/auth/web/refresh') ? _err(401, 'UNAUTHORIZED') : _err(401, 'TOKEN_EXPIRED'),);
      final dio = createConsoleDio(
        tokens: tokens,
        onSessionEnded: (c) => ended = c,
        baseUrl: 'https://x.test/api',
        adapter: adapter,
      );
      await expectLater(dio.get<dynamic>('/admin/dashboard'), throwsA(isA<DioException>()));
      expect(ended, 'UNAUTHORIZED');
      expect(tokens.access, isNull);
    });

    test('403 ACCOUNT_INACTIVE ends the session', () async {
      final tokens = ConsoleTokens()..access = 'a';
      String? ended;
      final dio = createConsoleDio(
        tokens: tokens,
        onSessionEnded: (c) => ended = c,
        baseUrl: 'https://x.test/api',
        adapter: _Adapter((_) => _err(403, 'ACCOUNT_INACTIVE')),
      );
      await expectLater(dio.get<dynamic>('/admin/dashboard'), throwsA(isA<DioException>()));
      expect(ended, 'ACCOUNT_INACTIVE');
      expect(tokens.access, isNull);
    });

    test('inactivity helpers', () {
      final t = DateTime(2026, 1, 1, 10);
      expect(consoleIdle(t, t.add(const Duration(minutes: 14)), const Duration(minutes: 15)), isFalse);
      expect(consoleIdle(t, t.add(const Duration(minutes: 15)), const Duration(minutes: 15)), isTrue);
      expect(clampConsoleMinutes(1), 5);
      expect(clampConsoleMinutes(500), 60);
      expect(clampConsoleMinutes('20'), 20);
      expect(clampConsoleMinutes(null), 15);
    });
  });

  group('console auth', () {
    ProviderContainer make(_Adapter adapter) {
      final c = ProviderContainer(overrides: [
        ...consoleAdminOverrides(
          (ref) => createConsoleDio(
            tokens: ref.read(consoleTokensProvider),
            onSessionEnded: (code) => ref.read(consoleAuthProvider.notifier).sessionEnded(code),
            baseUrl: 'https://x.test/api',
            adapter: adapter,
          ),
        ),
        appStateStoreProvider.overrideWithValue(MemoryAppStateStore()),
      ],);
      addTearDown(c.dispose);
      return c;
    }

    Future<void> settle() async {
      for (var i = 0; i < 200; i++) {
        await Future<void>.delayed(const Duration(milliseconds: 5));
        // done once the controller left the initial refresh and any follow-up call
        if (i > 10) break;
      }
    }

    test('reload without a cookie lands on the login page state', () async {
      final c = make(_Adapter((_) => _err(401, 'UNAUTHORIZED')));
      c.read(consoleAuthProvider);
      await settle();
      expect(c.read(consoleAuthProvider), isA<ConsoleSignedOut>());
    });

    test('reload with a cookie signs in and keeps the token in memory only', () async {
      final adapter = _Adapter((o) {
        if (o.path.endsWith('/auth/web/refresh')) {
          return (status: 200, body: {'access_token': 'acc', 'must_change_password': false});
        }
        if (o.path.endsWith('/auth/me')) return (status: 200, body: _me);
        return _err(404, 'NOT_FOUND');
      });
      final c = make(adapter);
      c.read(consoleAuthProvider);
      await settle();
      final s = c.read(consoleAuthProvider);
      expect(s, isA<ConsoleSignedIn>());
      expect(c.read(consoleTokensProvider).access, 'acc');
      expect(c.read(consolePermissionsProvider), contains('employee.view'));
    });

    test('login posts employee id and password to /auth/web/login', () async {
      final adapter = _Adapter((o) {
        if (o.path.endsWith('/auth/web/refresh')) return _err(401, 'UNAUTHORIZED');
        if (o.path.endsWith('/auth/web/login')) {
          return (status: 200, body: {'access_token': 'acc', 'must_change_password': false});
        }
        if (o.path.endsWith('/auth/me')) return (status: 200, body: _me);
        return _err(404, 'NOT_FOUND');
      });
      final c = make(adapter);
      c.read(consoleAuthProvider);
      await settle();
      await c.read(consoleAuthProvider.notifier).login(' ADM1 ', 'Secret1!x');
      final login = adapter.requests.firstWhere((r) => r.path.endsWith('/auth/web/login'));
      expect(login.data, {'employee_id': 'ADM1', 'password': 'Secret1!x'});
      expect(c.read(consoleAuthProvider), isA<ConsoleSignedIn>());
    });

    test('a user without console.access is signed out again', () async {
      final adapter = _Adapter((o) {
        if (o.path.endsWith('/auth/web/refresh') || o.path.endsWith('/auth/web/login')) {
          return (status: 200, body: {'access_token': 'acc'});
        }
        if (o.path.endsWith('/auth/web/logout')) return (status: 204, body: <String, Object>{});
        if (o.path.endsWith('/auth/me')) {
          return (status: 200, body: {..._me, 'permissions': ['employee.view']});
        }
        return _err(404, 'NOT_FOUND');
      });
      final c = make(adapter);
      c.read(consoleAuthProvider);
      await settle();
      final s = c.read(consoleAuthProvider);
      expect(s, isA<ConsoleSignedOut>());
      expect((s as ConsoleSignedOut).message, contains('cannot use'));
      expect(c.read(consoleTokensProvider).access, isNull);
    });
  });

  group('console UI', () {
    test('navigation only lists permitted pages', () {
      final all = visibleDestinations(['employee.view']).map((d) => d.label).toList();
      expect(all, ['Dashboard', 'Employees']);
      expect(visibleDestinations(const []).map((d) => d.label), ['Dashboard']);
      expect(
        visibleDestinations(['license.manage', 'company.manage', 'session.manage', 'audit.view'])
            .map((d) => d.label)
            .toList(),
        ['Dashboard', 'Sessions', 'Audit log', 'License', 'Company'],
      );
    });

    testWidgets('dashboard shows allowed blocks and hides nulled ones', (tester) async {
      tester.view.physicalSize = const Size(1200, 1600);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);
      final repo = _DashRepo();
      await tester.pumpWidget(ProviderScope(
        overrides: [
          adminRepositoryProvider.overrideWithValue(repo),
          consolePermissionsProvider.overrideWithValue(const ['console.access']),
        ],
        child: const MaterialApp(home: Scaffold(body: DashboardPage())),
      ),);
      await tester.pumpAndSettle();
      expect(find.text('Employees'), findsOneWidget);
      expect(find.text('Seats'), findsOneWidget);
      expect(find.text('120 / 150'), findsOneWidget);
      expect(find.text('Pending requests'), findsOneWidget);
      expect(find.text("Today's attendance"), findsNothing);
      expect(find.text('Live sessions'), findsNothing);
      expect(find.text('System health'), findsNothing);
    });

    testWidgets('inactivity watcher signs the console out', (tester) async {
      final calls = <String?>[];
      var now = DateTime(2026, 1, 1, 10);
      await tester.pumpWidget(ProviderScope(
        overrides: [
          consoleAuthProvider.overrideWith(() => _SpyAuth(calls)),
          consoleInactivityMinutesProvider.overrideWith((ref) => 5),
        ],
        child: MaterialApp(
          home: ConsoleInactivityWatcher(
            checkEvery: const Duration(minutes: 1),
            clock: () => now,
            child: const Text('content'),
          ),
        ),
      ),);
      for (var i = 0; i < 4; i++) {
        now = now.add(const Duration(minutes: 1));
        await tester.pump(const Duration(minutes: 1));
      }
      expect(calls, isEmpty);
      // activity resets the idle clock
      await tester.tapAt(const Offset(5, 5));
      for (var i = 0; i < 4; i++) {
        now = now.add(const Duration(minutes: 1));
        await tester.pump(const Duration(minutes: 1));
      }
      expect(calls, isEmpty);
      now = now.add(const Duration(minutes: 2));
      await tester.pump(const Duration(minutes: 2));
      expect(calls, hasLength(1));
      expect(calls.single, contains('inactivity'));
    });
  });
}

class _SpyAuth extends ConsoleAuthController {
  _SpyAuth(this.calls);
  final List<String?> calls;
  @override
  ConsoleAuthState build() => const ConsoleBooting();
  @override
  Future<void> logout({String? message}) async => calls.add(message);
}

class _DashRepo extends AdminRepository {
  _DashRepo() : super(Dio());

  @override
  Future<Dashboard> dashboard() async => Dashboard.fromJson({
        'date': '2026-10-22',
        'employees': {'active': 120, 'inactive': 8},
        'attendance': null,
        'requests': {'leave_pending': 4, 'permission_pending': 2},
        'sessions': null,
        'password_reset_requests_open': null,
        'seats': {'used': 120, 'total': 150, 'status': 'valid'},
      });
}
