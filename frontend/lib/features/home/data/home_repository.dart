import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/dio_client.dart';
import '../../../core/cache/json_cache.dart';
import '../../attendance/data/attendance_repository.dart' show cacheScopeProvider;
import '../domain/home_models.dart';

/// `GET /home`. Cached for the offline dashboard (JSON cache, cleared on
/// logout / lock). Nothing sensitive is in the payload: counts, balances,
/// today's punch times, names of colleagues with a celebration today.
class HomeRepository {
  HomeRepository(this._dio, this._cache, {this.scope = 'anon'});
  final Dio _dio;
  final JsonCache _cache;
  final String scope;

  Future<Cached<HomeData>> home() => fetchWithCache<HomeData>(
        cache: _cache,
        key: '$scope.home',
        fetch: () => guardApi<Object>(() async {
          final res = await _dio.get<Map<String, dynamic>>('/home');
          return res.data ?? const <String, dynamic>{};
        }),
        parse: (j) => HomeData.fromJson(Map<String, dynamic>.from(j as Map)),
      );
}

final homeRepositoryProvider = Provider<HomeRepository>(
  (ref) => HomeRepository(
    ref.watch(dioProvider),
    ref.watch(jsonCacheProvider),
    scope: ref.watch(cacheScopeProvider),
  ),
);

final homeProvider = FutureProvider.autoDispose<Cached<HomeData>>(
  (ref) => ref.watch(homeRepositoryProvider).home(),
);
