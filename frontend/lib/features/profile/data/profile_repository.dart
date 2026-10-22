import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/dio_client.dart';
import '../../../core/cache/json_cache.dart';
import '../../attendance/data/attendance_repository.dart' show cacheScopeProvider;
import '../domain/profile_models.dart';

/// api-contract.md section 2. The cached copy holds only non-personal fields
/// (the JSON cache scrubs dob, phone, address, emergency contact, ...).
class ProfileRepository {
  ProfileRepository(this._dio, this._cache, {this.scope = 'anon'});

  final Dio _dio;
  final JsonCache _cache;
  final String scope;

  static Profile _parse(Object json) => Profile.fromJson(Map<String, dynamic>.from(json as Map));

  Future<Cached<Profile>> profile() async {
    final r = await fetchWithCache<Profile>(
      cache: _cache,
      key: '$scope.profile',
      fetch: () => guardApi<Object>(() async {
        final res = await _dio.get<Map<String, dynamic>>('/me/profile');
        return res.data ?? const <String, dynamic>{};
      }),
      parse: _parse,
    );
    return r.stale
        ? Cached(r.value.copyWith(detailsHidden: true), stale: true)
        : r;
  }

  /// `PUT /me/profile` with only the changed keys; returns the saved profile.
  Future<Profile> update(Map<String, Object?> body) => guardApi(() async {
        final res = await _dio.put<Map<String, dynamic>>('/me/profile', data: body);
        return _parse(res.data ?? const <String, dynamic>{});
      });

  /// `POST /me/profile/photo` (multipart `file`, jpeg/png up to 5 MB). The
  /// response is expected to be the profile; if it is not, the caller reloads.
  Future<Profile?> uploadPhoto(String path, {String? filename}) => guardApi(() async {
        final form = FormData.fromMap({
          'file': await MultipartFile.fromFile(path, filename: filename ?? 'photo.jpg'),
        });
        final res = await _dio.post<Map<String, dynamic>>('/me/profile/photo', data: form);
        final d = res.data;
        return d != null && d.containsKey('employee_id') ? _parse(d) : null;
      });
}

final profileRepositoryProvider = Provider<ProfileRepository>(
  (ref) => ProfileRepository(
    ref.watch(dioProvider),
    ref.watch(jsonCacheProvider),
    scope: ref.watch(cacheScopeProvider),
  ),
);
