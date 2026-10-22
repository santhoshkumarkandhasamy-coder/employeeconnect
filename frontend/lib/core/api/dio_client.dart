import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../config/constants.dart';
import '../config/env.dart';
import '../server/server_providers.dart';
import '../storage/secure_storage.dart';
import 'api_exception.dart';
import 'cert_pinning.dart';

/// Request option marking public endpoints (no bearer, no refresh-on-401).
const String noAuthKey = 'noAuth';
const String _retriedKey = 'retried';

/// Auth endpoints whose 401/403 must never trigger refresh + replay. Their
/// 401 means "bad credentials / bad token", not "access token expired"
/// (review B1/M4). `/auth/me` is deliberately NOT listed: it is a normal
/// authenticated call that must refresh when the access token expires.
const Set<String> _noReplayPaths = {
  '/auth/login',
  '/auth/refresh',
  '/auth/logout',
  '/auth/logout-all',
  '/auth/change-password',
  '/auth/forgot-password',
  '/auth/reset-password',
};

bool isNoReplayPath(String path) {
  var p = Uri.tryParse(path)?.path ?? path;
  if (p.length > 1 && p.endsWith('/')) p = p.substring(0, p.length - 1);
  // The base URL may carry a prefix such as /api; match on the suffix.
  return _noReplayPaths.any((e) => p == e || p.endsWith(e));
}

/// Broadcasts "session is gone, show login" (refresh failed / reuse detected /
/// account inactive) and "password change required" (403
/// PASSWORD_CHANGE_REQUIRED, or `must_change_password` in a refresh reply).
class SessionEvents {
  final StreamController<String> _c = StreamController<String>.broadcast();
  final StreamController<void> _p = StreamController<void>.broadcast();
  Stream<String> get expired => _c.stream;
  Stream<void> get passwordChangeRequired => _p.stream;
  void expire(String code) => _c.add(code);

  /// Time of the last successful authenticated call (resume check).
  DateTime? lastSuccess;
  void requirePasswordChange() => _p.add(null);
  void dispose() {
    unawaited(_c.close());
    unawaited(_p.close());
  }
}

final sessionEventsProvider = Provider<SessionEvents>((ref) {
  final e = SessionEvents();
  ref.onDispose(e.dispose);
  return e;
});

final dioProvider = Provider<Dio>((ref) {
  return createDio(
    store: ref.watch(tokenStoreProvider),
    events: ref.watch(sessionEventsProvider),
    baseUrl: ref.watch(baseUrlProvider),
  );
});

/// Builds the app Dio. [adapterFactory] is for tests (skips pinning).
///
/// In release builds this throws [StateError] unless the base URL is https
/// and at least two certificate pins are configured (review H4). [releaseMode]
/// is overridable for tests.
Dio createDio({
  required TokenStore store,
  required SessionEvents events,
  String? baseUrl,
  List<String>? pins,
  HttpClientAdapter Function()? adapterFactory,
  bool? releaseMode,
}) {
  final url = baseUrl ?? Env.apiBaseUrl;
  final activePins = pins ?? Env.certPins;
  if (releaseMode == null) {
    Env.assertSecureTransport(baseUrl: url, pins: activePins);
  } else {
    Env.assertSecureTransport(
      baseUrl: url,
      pins: activePins,
      releaseMode: releaseMode,
    );
  }

  BaseOptions options() => BaseOptions(
        baseUrl: url,
        connectTimeout: AppConstants.connectTimeout,
        receiveTimeout: AppConstants.receiveTimeout,
        headers: const {'Accept': 'application/json'},
      );

  final dio = Dio(options());
  final bare = Dio(options()); // used for /auth/refresh only (no interceptors)
  for (final d in [dio, bare]) {
    if (adapterFactory != null) {
      d.httpClientAdapter = adapterFactory();
    } else {
      applyCertPinning(d, activePins, expectedHost: Uri.tryParse(url)?.host);
    }
  }
  dio.interceptors.add(AuthInterceptor(
    dio: dio,
    refreshDio: bare,
    store: store,
    events: events,
  ),);
  return dio;
}

/// Attaches the bearer token; on an access-token 401 performs ONE shared
/// refresh (single flight) and replays the request once.
///
/// - Never refreshes/replays 401s from auth endpoints (login, change-password,
///   logout, ...) or `INVALID_CREDENTIALS` (B1/M4).
/// - 403 `ACCOUNT_INACTIVE` ends the session; 403 `PASSWORD_CHANGE_REQUIRED`
///   routes to change-password (B2/B3).
/// - Only a definitive refresh failure ends the session; 429/5xx/network keep
///   it (B6).
class AuthInterceptor extends Interceptor {
  AuthInterceptor({
    required this.dio,
    required this.refreshDio,
    required this.store,
    required this.events,
  });

  final Dio dio;
  final Dio refreshDio;
  final TokenStore store;
  final SessionEvents events;
  Future<String?>? _refreshing;

  @override
  Future<void> onRequest(
    RequestOptions options,
    RequestInterceptorHandler handler,
  ) async {
    if (options.extra[noAuthKey] != true) {
      final token = await store.readAccess();
      if (token != null) options.headers['Authorization'] = 'Bearer $token';
    }
    handler.next(options);
  }

  DateTime? _lastPersisted;

  @override
  void onResponse(Response<dynamic> response, ResponseInterceptorHandler handler) {
    if (response.requestOptions.extra[noAuthKey] != true) {
      final now = DateTime.now();
      events.lastSuccess = now;
      final p = _lastPersisted;
      if (p == null || now.difference(p) > const Duration(minutes: 1)) {
        _lastPersisted = now;
        unawaited(store.writeLastOk(now).catchError((Object _) {}));
      }
    }
    handler.next(response);
  }

  @override
  Future<void> onError(
    DioException err,
    ErrorInterceptorHandler handler,
  ) async {
    final req = err.requestOptions;
    final status = err.response?.statusCode;
    final isPublic = req.extra[noAuthKey] == true;
    if (isPublic) return handler.next(err);

    if (status == 403) {
      final code = ApiException.fromDio(err).code;
      if (code == 'ACCOUNT_INACTIVE') {
        await _endSession(code);
      } else if (code == 'PASSWORD_CHANGE_REQUIRED') {
        events.requirePasswordChange();
      }
      return handler.next(err);
    }

    if (status != 401 ||
        req.extra[_retriedKey] == true ||
        isNoReplayPath(req.path)) {
      return handler.next(err);
    }

    final code = ApiException.fromDio(err).code;
    if (ApiException.sessionEndingCodes.contains(code)) {
      await _endSession(code);
      return handler.next(err);
    }
    // A wrong password is not an expired token; never refresh for it.
    if (code == 'INVALID_CREDENTIALS') return handler.next(err);

    final String? token;
    try {
      token = await (_refreshing ??= _refresh().whenComplete(() => _refreshing = null));
    } on DioException {
      // Transient trouble while refreshing (network, 429, 5xx): surface the
      // original failure, keep the session.
      return handler.next(err);
    }
    if (token == null) return handler.next(err);

    req.headers['Authorization'] = 'Bearer $token';
    req.extra[_retriedKey] = true;
    try {
      handler.resolve(await dio.fetch<dynamic>(req));
    } on DioException catch (e) {
      handler.next(e);
    }
  }

  /// A refresh failure that proves the session is unusable (as opposed to a
  /// transient problem such as 429, 5xx, a proxy error or no network).
  static bool _isDefinitiveRefreshFailure(ApiException api) {
    final s = api.status ?? 0;
    if (api.isNetwork || s >= 500 || s == 429 || s == 408) return false;
    if (s == 401) return true;
    if (s == 403) return api.code == 'ACCOUNT_INACTIVE';
    // 400/422: the stored token is malformed; retrying cannot help.
    if (s == 400 || s == 422) return true;
    return false;
  }

  /// Returns the new access token, or null when the session has ended.
  /// Throws [DioException] for transient failures (session kept).
  Future<String?> _refresh() async {
    final refresh = await store.readRefresh();
    if (refresh == null) {
      await _endSession('UNAUTHORIZED');
      return null;
    }
    try {
      final res = await refreshDio.post<Map<String, dynamic>>(
        '/auth/refresh',
        data: {'refresh_token': refresh},
        options: Options(extra: const {noAuthKey: true}),
      );
      final d = res.data!;
      await store.saveTokens(
        access: d['access_token'] as String,
        refresh: d['refresh_token'] as String,
      );
      if (d['must_change_password'] == true) events.requirePasswordChange();
      return d['access_token'] as String;
    } on DioException catch (e) {
      final api = ApiException.fromDio(e);
      if (!_isDefinitiveRefreshFailure(api)) rethrow;
      await _endSession(api.code);
      return null;
    }
  }

  Future<void> _endSession(String code) async {
    await store.clearSession();
    events.expire(code);
  }
}

/// Runs [call] and converts transport errors to [ApiException].
Future<T> guardApi<T>(Future<T> Function() call) async {
  try {
    return await call();
  } on DioException catch (e) {
    throw ApiException.fromDio(e);
  }
}
