import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/dio_client.dart';
import '../../../core/cache/json_cache.dart';
import '../../auth/presentation/auth_controller.dart';
import '../../auth/presentation/auth_state.dart';
import '../domain/attendance_models.dart';
import 'location_service.dart';

/// api-contract.md section 3 (+ POST /files for the selfie, GET /config).
/// History page 1, calendar and summary are cached for offline reading;
/// [scope] (the user id) keeps accounts apart.
class AttendanceRepository {
  AttendanceRepository(this._dio, this._cache, {this.scope = 'anon'});

  final Dio _dio;
  final JsonCache _cache;
  final String scope;

  Future<AttendanceConfig> config() => guardApi(() async {
        final res = await _dio.get<Map<String, dynamic>>('/config');
        return AttendanceConfig.fromJson(res.data ?? const {});
      });

  Future<TodayAttendance> today() => guardApi(() async {
        final res = await _dio.get<Map<String, dynamic>>('/attendance/today');
        return TodayAttendance.fromJson(res.data ?? const {});
      });

  Map<String, Object?> _body(LocationFix fix, String? selfieId) => {
        'lat': fix.lat,
        'lng': fix.lng,
        'accuracy_m': fix.accuracyM,
        // Platform timestamp of the fix (UTC), so the server can reject stale fixes.
        'fix_time': fix.timestamp?.toUtc().toIso8601String(),
        'selfie_file_id': selfieId,
        'client_time': DateTime.now().toUtc().toIso8601String(),
        'is_mock_location': fix.isMock,
      };

  Future<AttendanceRecord> checkIn(LocationFix fix, {String? selfieFileId}) => guardApi(() async {
        final res = await _dio.post<Map<String, dynamic>>(
          '/attendance/check-in',
          data: _body(fix, selfieFileId),
        );
        return AttendanceRecord.fromJson(res.data!);
      });

  Future<AttendanceRecord> checkOut(LocationFix fix) => guardApi(() async {
        final res = await _dio.post<Map<String, dynamic>>(
          '/attendance/check-out',
          data: _body(fix, null),
        );
        return AttendanceRecord.fromJson(res.data!);
      });

  /// Uploads a selfie via `POST /files` (purpose `selfie`); returns the file id.
  Future<String> uploadSelfie(String path) => guardApi(() async {
        final form = FormData.fromMap({
          'purpose': 'selfie',
          'file': await MultipartFile.fromFile(path, filename: 'selfie.jpg'),
        });
        final res = await _dio.post<Map<String, dynamic>>('/files', data: form);
        return res.data!['id'] as String;
      });

  Future<Cached<Paged<AttendanceRecord>>> history({int page = 1}) {
    Future<Object> fetch() => guardApi<Object>(() async {
          final res = await _dio.get<Map<String, dynamic>>(
            '/attendance',
            queryParameters: {'page': page},
          );
          return res.data ?? const <String, dynamic>{};
        });
    Paged<AttendanceRecord> parse(Object j) =>
        Paged.parse(j as Map<String, dynamic>, AttendanceRecord.fromJson);
    // Only the first page is cached offline.
    if (page > 1) return fetch().then((j) => Cached(parse(j)));
    return fetchWithCache(cache: _cache, key: '$scope.att.history', fetch: fetch, parse: parse);
  }

  Future<Cached<CalendarMonth>> calendar(String month) => fetchWithCache(
        cache: _cache,
        key: '$scope.att.cal.$month',
        fetch: () => guardApi<Object>(() async {
          final res = await _dio.get<Map<String, dynamic>>(
            '/attendance/calendar',
            queryParameters: {'month': month},
          );
          return res.data ?? const <String, dynamic>{};
        }),
        parse: (j) => CalendarMonth.fromJson(j as Map<String, dynamic>),
      );

  Future<Cached<AttendanceSummary>> summary(String month) => fetchWithCache(
        cache: _cache,
        key: '$scope.att.sum.$month',
        fetch: () => guardApi<Object>(() async {
          final res = await _dio.get<Map<String, dynamic>>(
            '/attendance/summary',
            queryParameters: {'month': month},
          );
          return res.data ?? const <String, dynamic>{};
        }),
        parse: (j) => AttendanceSummary.fromJson(j as Map<String, dynamic>),
      );
}

/// The signed-in user's id, used to scope cache keys.
final cacheScopeProvider = Provider<String>(
  (ref) => ref.watch(
    authControllerProvider.select((s) => s is AuthAuthenticated ? s.user.id : 'anon'),
  ),
);

final attendanceRepositoryProvider = Provider<AttendanceRepository>(
  (ref) => AttendanceRepository(
    ref.watch(dioProvider),
    ref.watch(jsonCacheProvider),
    scope: ref.watch(cacheScopeProvider),
  ),
);
