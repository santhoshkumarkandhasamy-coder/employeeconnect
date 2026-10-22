
import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/dio_client.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/fakes.dart';

void main() {
  late MemoryTokenStore store;
  late SessionEvents events;
  late ScriptedAdapter adapter;
  late Dio dio;
  var refreshCalls = 0;
  var refreshStatus = 200;
  var refreshCode = 'TOKEN_REUSED';
  var refreshMustChange = false;
  // path -> (status, code) for requests carrying a stale token.
  late Map<String, ({int status, String code})> stale;

  Map<String, Object> err(String code) => {
        'error': {'code': code, 'message': code},
      };

  setUp(() {
    refreshCalls = 0;
    refreshStatus = 200;
    refreshCode = 'TOKEN_REUSED';
    refreshMustChange = false;
    stale = {};
    store = MemoryTokenStore()
      ..access = 'old'
      ..refresh = 'r1';
    events = SessionEvents();
    adapter = ScriptedAdapter((o) {
      if (o.path == '/auth/refresh') {
        refreshCalls++;
        if (refreshStatus != 200) {
          return (status: refreshStatus, body: err(refreshCode));
        }
        return (
          status: 200,
          body: {
            'access_token': 'new',
            'refresh_token': 'r2',
            if (refreshMustChange) 'must_change_password': true,
          },
        );
      }
      final s = stale[o.path];
      if (s != null) return (status: s.status, body: err(s.code));
      if (o.headers['Authorization'] == 'Bearer new') {
        return (status: 200, body: {'ok': true});
      }
      return (status: 401, body: err('TOKEN_EXPIRED'));
    });
    dio = createDio(
      store: store,
      events: events,
      baseUrl: 'https://test.local/api',
      adapterFactory: () => adapter,
    );
  });

  test('concurrent 401s trigger a single refresh and all replay', () async {
    final results = await Future.wait([
      dio.get<Map<String, dynamic>>('/a'),
      dio.get<Map<String, dynamic>>('/b'),
      dio.get<Map<String, dynamic>>('/c'),
    ]);
    expect(results.every((r) => r.statusCode == 200), isTrue);
    expect(refreshCalls, 1);
    expect(store.access, 'new');
    expect(store.refresh, 'r2');
  });

  test('failed refresh (401) clears the session and emits an expiry event', () async {
    refreshStatus = 401;
    final expired = events.expired.first;
    await expectLater(
      dio.get<Map<String, dynamic>>('/a'),
      throwsA(isA<DioException>()),
    );
    expect(await expired, 'TOKEN_REUSED');
    expect(store.refresh, isNull);
  });

  test('refresh 429 keeps the session (B6)', () async {
    refreshStatus = 429;
    refreshCode = 'RATE_LIMITED';
    var expiredEvents = 0;
    events.expired.listen((_) => expiredEvents++);
    await expectLater(
      dio.get<Map<String, dynamic>>('/a'),
      throwsA(isA<DioException>()),
    );
    await Future<void>.delayed(Duration.zero);
    expect(expiredEvents, 0);
    expect(store.refresh, 'r1');
  });

  test('refresh 503 keeps the session (B6)', () async {
    refreshStatus = 503;
    refreshCode = 'INTERNAL_ERROR';
    await expectLater(
      dio.get<Map<String, dynamic>>('/a'),
      throwsA(isA<DioException>()),
    );
    expect(store.refresh, 'r1');
  });

  test('refresh reply with must_change_password emits an event (B5)', () async {
    refreshMustChange = true;
    final fired = events.passwordChangeRequired.first;
    final res = await dio.get<Map<String, dynamic>>('/a');
    expect(res.statusCode, 200);
    await fired;
  });

  test('public endpoints never refresh', () async {
    await expectLater(
      dio.get<Map<String, dynamic>>(
        '/auth/login',
        options: Options(extra: const {noAuthKey: true}),
      ),
      throwsA(isA<DioException>()),
    );
    expect(refreshCalls, 0);
  });

  test('wrong current password on change-password is not refreshed or replayed (B1/M4)',
      () async {
    stale['/auth/change-password'] = (status: 400, code: 'CURRENT_PASSWORD_INVALID');
    await expectLater(
      dio.post<Map<String, dynamic>>('/auth/change-password', data: {'x': 1}),
      throwsA(isA<DioException>()),
    );
    expect(refreshCalls, 0);
    expect(
      adapter.requests.where((r) => r.path == '/auth/change-password').length,
      1,
    );
    expect(store.refresh, 'r1');
  });

  test('401 TOKEN_EXPIRED from an auth endpoint is never replayed', () async {
    // /auth/change-password with an expired access token: no refresh+replay.
    stale['/auth/change-password'] = (status: 401, code: 'TOKEN_EXPIRED');
    await expectLater(
      dio.post<Map<String, dynamic>>('/auth/change-password', data: {'x': 1}),
      throwsA(isA<DioException>()),
    );
    expect(refreshCalls, 0);
    expect(
      adapter.requests.where((r) => r.path == '/auth/change-password').length,
      1,
    );
  });

  test('INVALID_CREDENTIALS on a normal endpoint does not refresh', () async {
    stale['/x'] = (status: 401, code: 'INVALID_CREDENTIALS');
    await expectLater(
      dio.get<Map<String, dynamic>>('/x'),
      throwsA(isA<DioException>()),
    );
    expect(refreshCalls, 0);
  });

  test('/auth/me still refreshes on an expired access token', () async {
    final res = await dio.get<Map<String, dynamic>>('/auth/me');
    expect(res.statusCode, 200);
    expect(refreshCalls, 1);
  });

  test('401 SESSION_REVOKED ends the session with that code (B4)', () async {
    stale['/a'] = (status: 401, code: 'SESSION_REVOKED');
    final expired = events.expired.first;
    await expectLater(
      dio.get<Map<String, dynamic>>('/a'),
      throwsA(isA<DioException>()),
    );
    expect(await expired, 'SESSION_REVOKED');
    expect(refreshCalls, 0);
    expect(store.refresh, isNull);
  });

  test('revoked access token: refresh+replay ends cleanly via SESSION_REVOKED', () async {
    stale['/a'] = (status: 401, code: 'UNAUTHORIZED');
    refreshStatus = 401;
    refreshCode = 'SESSION_REVOKED';
    final expired = events.expired.first;
    await expectLater(
      dio.get<Map<String, dynamic>>('/a'),
      throwsA(isA<DioException>()),
    );
    expect(await expired, 'SESSION_REVOKED');
    expect(refreshCalls, 1);
    expect(store.refresh, isNull);
  });

  test('400 CURRENT_PASSWORD_INVALID on a normal endpoint never refreshes', () async {
    stale['/x'] = (status: 400, code: 'CURRENT_PASSWORD_INVALID');
    await expectLater(
      dio.get<Map<String, dynamic>>('/x'),
      throwsA(isA<DioException>()),
    );
    expect(refreshCalls, 0);
    expect(store.refresh, 'r1');
  });

  test('403 ACCOUNT_INACTIVE ends the session (B2)', () async {
    stale['/a'] = (status: 403, code: 'ACCOUNT_INACTIVE');
    final expired = events.expired.first;
    await expectLater(
      dio.get<Map<String, dynamic>>('/a'),
      throwsA(isA<DioException>()),
    );
    expect(await expired, 'ACCOUNT_INACTIVE');
    expect(store.refresh, isNull);
  });

  test('403 PASSWORD_CHANGE_REQUIRED routes to change-password (B3)', () async {
    stale['/a'] = (status: 403, code: 'PASSWORD_CHANGE_REQUIRED');
    final fired = events.passwordChangeRequired.first;
    await expectLater(
      dio.get<Map<String, dynamic>>('/a'),
      throwsA(isA<DioException>()),
    );
    await fired;
    expect(store.refresh, 'r1'); // session kept
  });

  group('release transport guard (H4)', () {
    Dio build({String url = 'https://x.test/api', List<String> pins = const [], bool? release}) =>
        createDio(
          store: store,
          events: events,
          baseUrl: url,
          pins: pins,
          adapterFactory: () => adapter,
          releaseMode: release,
        );

    test('release rejects http base URL', () {
      expect(
        () => build(url: 'http://x.test/api', pins: ['a', 'b'], release: true),
        throwsStateError,
      );
    });

    test('release requires at least two pins', () {
      expect(() => build(release: true), throwsStateError);
      expect(() => build(pins: ['a'], release: true), throwsStateError);
    });

    test('release accepts https plus two pins', () {
      expect(() => build(pins: ['a', 'b'], release: true), returnsNormally);
    });

    test('debug allows http and no pins', () {
      expect(() => build(url: 'http://10.0.2.2:8000', release: false), returnsNormally);
    });
  });
}
