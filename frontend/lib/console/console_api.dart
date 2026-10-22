import 'dart:async';

import 'package:dio/dio.dart';

import '../core/api/api_exception.dart';
import '../core/api/dio_client.dart' show noAuthKey;
import '../core/config/env.dart';
import '../core/server/server_url.dart';

/// Access token of the console: IN MEMORY ONLY (never localStorage,
/// sessionStorage or IndexedDB). The refresh token is an HttpOnly cookie the
/// page cannot read.
class ConsoleTokens {
  String? access;
}

const String consoleClientHeader = 'X-EC-Client';
const String consoleClientValue = 'console';

/// `/api` on the page's own origin (the console is served by the same nginx),
/// unless a real `API_BASE_URL` was compiled in (local development).
String consoleBaseUrl({String? origin}) => isPlaceholderUrl(Env.apiBaseUrl)
    ? '${origin ?? Uri.base.origin}/api'
    : Env.apiBaseUrl;

bool _isWebAuthPath(String path) => path.contains('/auth/web/');

/// Dio for the console: bearer from memory, `X-EC-Client: console`, one
/// shared refresh through the cookie (`POST /auth/web/refresh`) on a 401, and
/// sign-out when the session is gone or the account was deactivated.
Dio createConsoleDio({
  required ConsoleTokens tokens,
  required void Function(String code) onSessionEnded,
  void Function()? onPasswordChangeRequired,
  String? baseUrl,
  HttpClientAdapter? adapter,
}) {
  BaseOptions options() => BaseOptions(
        baseUrl: baseUrl ?? consoleBaseUrl(),
        connectTimeout: const Duration(seconds: 15),
        receiveTimeout: const Duration(seconds: 30),
        headers: const {'Accept': 'application/json', consoleClientHeader: consoleClientValue},
      );
  final dio = Dio(options());
  final bare = Dio(options());
  if (adapter != null) {
    dio.httpClientAdapter = adapter;
    bare.httpClientAdapter = adapter;
  }
  dio.interceptors.add(
    ConsoleInterceptor(
      dio: dio,
      refreshDio: bare,
      tokens: tokens,
      onSessionEnded: onSessionEnded,
      onPasswordChangeRequired: onPasswordChangeRequired,
    ),
  );
  return dio;
}

class ConsoleInterceptor extends Interceptor {
  ConsoleInterceptor({
    required this.dio,
    required this.refreshDio,
    required this.tokens,
    required this.onSessionEnded,
    this.onPasswordChangeRequired,
  });

  final Dio dio;
  final Dio refreshDio;
  final ConsoleTokens tokens;
  final void Function(String code) onSessionEnded;
  final void Function()? onPasswordChangeRequired;
  Future<String?>? _refreshing;
  static const _retried = 'retried';

  @override
  void onRequest(RequestOptions options, RequestInterceptorHandler handler) {
    final t = tokens.access;
    if (options.extra[noAuthKey] != true && t != null) {
      options.headers['Authorization'] = 'Bearer $t';
    }
    handler.next(options);
  }

  @override
  Future<void> onError(DioException err, ErrorInterceptorHandler handler) async {
    final req = err.requestOptions;
    final status = err.response?.statusCode;
    if (req.extra[noAuthKey] == true || _isWebAuthPath(req.path)) {
      return handler.next(err);
    }
    if (status == 403) {
      final code = ApiException.fromDio(err).code;
      if (code == 'ACCOUNT_INACTIVE') {
        tokens.access = null;
        onSessionEnded(code);
      } else if (code == 'PASSWORD_CHANGE_REQUIRED') {
        onPasswordChangeRequired?.call();
      }
      return handler.next(err);
    }
    if (status != 401 || req.extra[_retried] == true) return handler.next(err);

    final String? token;
    try {
      token = await (_refreshing ??= _refresh().whenComplete(() => _refreshing = null));
    } on DioException {
      return handler.next(err); // transient: keep the session
    }
    if (token == null) return handler.next(err);
    req.headers['Authorization'] = 'Bearer $token';
    req.extra[_retried] = true;
    try {
      handler.resolve(await dio.fetch<dynamic>(req));
    } on DioException catch (e) {
      handler.next(e);
    }
  }

  Future<String?> _refresh() async {
    try {
      final res = await refreshDio.post<Map<String, dynamic>>(
        '/auth/web/refresh',
        options: Options(extra: const {noAuthKey: true}),
      );
      final t = res.data?['access_token'] as String?;
      tokens.access = t;
      if (res.data?['must_change_password'] == true) onPasswordChangeRequired?.call();
      return t;
    } on DioException catch (e) {
      final api = ApiException.fromDio(e);
      final s = api.status ?? 0;
      if (api.isNetwork || s >= 500 || s == 429 || s == 408) rethrow;
      tokens.access = null;
      onSessionEnded(api.code);
      return null;
    }
  }
}

/// True when the console has been idle for [timeout] (inactivity sign-out).
bool consoleIdle(DateTime lastActivity, DateTime now, Duration timeout) =>
    now.difference(lastActivity) >= timeout;

/// `console.inactivity_timeout_minutes` is 5..60 on the server.
int clampConsoleMinutes(Object? v, {int fallback = 15}) {
  final n = v is num ? v.toInt() : int.tryParse('$v');
  if (n == null) return fallback;
  return n.clamp(5, 60);
}
