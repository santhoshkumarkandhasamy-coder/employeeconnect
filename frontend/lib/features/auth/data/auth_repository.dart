import 'dart:io' show Platform;

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/dio_client.dart';
import '../../../core/config/env.dart';
import '../../../core/server/company.dart';
import '../../../core/push/push_token_source.dart';
import '../../../core/storage/secure_storage.dart';
import '../domain/auth_user.dart';

class AppConfig {
  const AppConfig({this.inactivityMinutes, this.offlineCacheMaxHours, this.company});
  final int? inactivityMinutes;
  final int? offlineCacheMaxHours;
  final CompanyInfo? company;
}

/// Talks to api-contract.md section 1. Persists tokens; throws ApiException.
class AuthRepository {
  AuthRepository(this._dio, this._store, [this._push]);

  final Dio _dio;
  final TokenStore _store;
  final PushTokenSource? _push;

  static final Options _public = Options(extra: const {noAuthKey: true});

  Future<AuthUser> login(String employeeId, String password) => guardApi(() async {
        final res = await _dio.post<Map<String, dynamic>>(
          '/auth/login',
          data: {
            'employee_id': employeeId.trim(),
            'password': password,
            'device_id': await _store.deviceId(),
            'device_name': Platform.operatingSystem,
            'app_version': Env.appVersion,
            'platform': Platform.isIOS ? 'ios' : 'android',
          },
          options: _public,
        );
        return _saveAndParse(res.data!);
      });

  Future<AuthUser> me() => guardApi(() async {
        final res = await _dio.get<Map<String, dynamic>>('/auth/me');
        return AuthUser.fromMe(res.data!);
      });

  /// Returns the user from the new token pair (login shape).
  Future<AuthUser> changePassword(String current, String next) => guardApi(() async {
        final res = await _dio.post<Map<String, dynamic>>(
          '/auth/change-password',
          data: {'current_password': current, 'new_password': next},
        );
        return _saveAndParse(res.data!);
      });

  /// Best effort server logout; local session is always wiped.
  Future<void> logout({bool all = false}) async {
    await _deletePushToken();
    try {
      if (all) {
        await _dio.post<void>('/auth/logout-all');
      } else {
        final refresh = await _store.readRefresh();
        if (refresh != null) {
          await _dio.post<void>('/auth/logout', data: {'refresh_token': refresh});
        }
      }
    } on DioException {
      // offline or already expired: nothing more to do
    } finally {
      await _store.clearSession();
    }
  }

  /// Unregisters this device's FCM token (B7). Guarded: any failure, or no
  /// token source yet, is ignored so logout always completes.
  Future<void> _deletePushToken() async {
    final source = _push;
    if (source == null) return;
    try {
      final token = await source.currentToken();
      if (token == null || token.isEmpty) return;
      await _dio.delete<void>('/me/device-tokens/${Uri.encodeComponent(token)}');
    } on Object {
      // best effort
    }
  }

  /// `GET /config`: inactivity timeout, offline cache limit and company.
  Future<AppConfig?> config() async {
    try {
      final res = await _dio.get<Map<String, dynamic>>('/config');
      final d = res.data;
      if (d == null) return null;
      final auth = d['auth'];
      return AppConfig(
        inactivityMinutes:
            auth is Map ? (auth['inactivity_timeout_minutes'] as num?)?.toInt() : null,
        offlineCacheMaxHours:
            auth is Map ? (auth['offline_cache_max_hours'] as num?)?.toInt() : null,
        company: CompanyInfo.parse(d['company']),
      );
    } on DioException {
      return null;
    }
  }

  /// Public company branding for the login screen (no auth).
  Future<CompanyInfo?> publicCompany() async {
    try {
      final res = await _dio.get<Map<String, dynamic>>('/public/company', options: _public);
      return CompanyInfo.parse(res.data);
    } on DioException {
      return null;
    }
  }

  Future<AuthUser> _saveAndParse(Map<String, dynamic> d) async {
    await _store.saveTokens(
      access: d['access_token'] as String,
      refresh: d['refresh_token'] as String,
    );
    return AuthUser.fromLogin(d);
  }
}

final authRepositoryProvider = Provider<AuthRepository>(
  (ref) => AuthRepository(
    ref.watch(dioProvider),
    ref.watch(tokenStoreProvider),
    ref.watch(pushTokenSourceProvider),
  ),
);
