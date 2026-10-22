import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/dio_client.dart';
import '../../../core/cache/json_cache.dart';
import '../../../core/utils/dates.dart';
import '../../attendance/data/attendance_repository.dart' show cacheScopeProvider;
import '../domain/late_models.dart';

/// api-contract.md section 5 (self endpoints). Reads are cached offline.
class LateRepository {
  LateRepository(this._dio, this._cache, {this.scope = 'anon'});

  final Dio _dio;
  final JsonCache _cache;
  final String scope;

  /// Late days of [month] (`YYYY-MM`). A month holds at most 31 records, so
  /// one page of 100 is enough.
  Future<Cached<List<LateRecord>>> list(String month) {
    final r = monthRange(month);
    return fetchWithCache(
      cache: _cache,
      key: '$scope.late.list.$month',
      fetch: () => guardApi<Object>(() async {
        final res = await _dio.get<Map<String, dynamic>>(
          '/late',
          queryParameters: {'from': r.from, 'to': r.to, 'page': 1, 'page_size': 100},
        );
        return res.data ?? const <String, dynamic>{};
      }),
      parse: (j) {
        final raw = (j as Map<String, dynamic>)['items'];
        return [
          if (raw is List)
            for (final e in raw)
              if (e is Map<String, dynamic>) LateRecord.fromJson(e),
        ];
      },
    );
  }

  Future<Cached<LateSummary>> summary(String month) => fetchWithCache(
        cache: _cache,
        key: '$scope.late.sum.$month',
        fetch: () => guardApi<Object>(() async {
          final res = await _dio.get<Map<String, dynamic>>(
            '/late/summary',
            queryParameters: {'month': month},
          );
          return res.data ?? const <String, dynamic>{};
        }),
        parse: (j) => LateSummary.fromJson(j as Map<String, dynamic>),
      );
}

final lateRepositoryProvider = Provider<LateRepository>(
  (ref) => LateRepository(
    ref.watch(dioProvider),
    ref.watch(jsonCacheProvider),
    scope: ref.watch(cacheScopeProvider),
  ),
);
