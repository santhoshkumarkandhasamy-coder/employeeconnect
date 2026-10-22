import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/dio_client.dart';
import '../../../core/cache/json_cache.dart';
import '../../../core/files/file_service.dart';
import '../../attendance/data/attendance_repository.dart' show cacheScopeProvider;
import '../../attendance/domain/attendance_models.dart' show Paged;
import '../domain/announcement_models.dart';

/// api-contract.md section 7. The first page of the employee list is cached
/// for offline reading (JSON cache: cleared on logout / lock, scoped per
/// user). Admin lists, read stats and details are never cached.
class AnnouncementRepository {
  AnnouncementRepository(this._dio, this._cache, this._files, {this.scope = 'anon'});

  final Dio _dio;
  final JsonCache _cache;
  final FileService _files;
  final String scope;

  Future<Map<String, dynamic>> _get(String path, [Map<String, Object?>? query]) =>
      guardApi(() async {
        final res = await _dio.get<Map<String, dynamic>>(path, queryParameters: query);
        return res.data ?? const <String, dynamic>{};
      });

  static Paged<Announcement> _parsePage(Object json) => Paged.parse(
        Map<String, dynamic>.from(json as Map),
        Announcement.fromJson,
      );

  /// Page [page] of visible announcements. Page 1 falls back to the cached
  /// copy when the network fails; later pages never do.
  Future<Cached<Paged<Announcement>>> list({
    int page = 1,
    int pageSize = 20,
    bool unreadOnly = false,
  }) {
    Future<Object> fetch() => _get('/announcements', {
          'page': page,
          'page_size': pageSize,
          if (unreadOnly) 'unread': true,
        });
    if (page != 1) return fetch().then((j) => Cached(_parsePage(j)));
    return fetchWithCache(
      cache: _cache,
      key: '$scope.announcements.p1.s$pageSize.${unreadOnly ? 'unread' : 'all'}',
      fetch: fetch,
      parse: _parsePage,
    );
  }

  Future<int> unreadCount() async {
    final j = await _get('/announcements/unread-count');
    for (final k in const ['count', 'unread_count', 'unread', 'unread_announcements']) {
      final v = j[k];
      if (v is num) return v.toInt();
    }
    return 0;
  }

  Future<Announcement> detail(String id) async =>
      Announcement.fromJson(await _get('/announcements/$id'));

  /// Idempotent.
  Future<void> markRead(String id) => guardApi(() async {
        await _dio.post<void>('/announcements/$id/read');
      });

  // ---- admin (never cached) ----------------------------------------------------

  Future<Paged<Announcement>> adminList({int page = 1}) async =>
      _parsePage(await _get('/admin/announcements', {'page': page, 'page_size': 20}));

  Future<Announcement> create(Map<String, Object?> body) => guardApi(() async {
        final res = await _dio.post<Map<String, dynamic>>('/admin/announcements', data: body);
        return Announcement.fromJson(res.data ?? const <String, dynamic>{});
      });

  /// [version] (admin view `version`) is sent as `If-Match: "<version>"`; a
  /// newer version on the server answers 409 `CONFLICT`.
  Future<Announcement> update(String id, Map<String, Object?> body, {int? version}) =>
      guardApi(() async {
        final res = await _dio.put<Map<String, dynamic>>(
          '/admin/announcements/$id',
          data: body,
          options: version == null ? null : Options(headers: {'If-Match': '"$version"'}),
        );
        return Announcement.fromJson(res.data ?? const <String, dynamic>{});
      });

  Future<void> delete(String id) => guardApi(() async {
        await _dio.delete<void>('/admin/announcements/$id');
      });

  Future<ReadStats> reads(String id, {int page = 1}) async =>
      ReadStats.parse(await _get('/admin/announcements/$id/reads', {'page': page, 'page_size': 50}));

  /// Active departments for the target picker (`GET /admin/departments` is
  /// open to any signed-in user).
  Future<List<Department>> departments() async =>
      Department.parseList(await _get('/admin/departments', {'page_size': 100}))
          .where((d) => d.isActive)
          .toList();

  /// Uploads an announcement image or PDF (needs `announcement.manage`).
  Future<String> uploadAttachment(String path, {required bool pdf, String? filename}) =>
      _files.upload(
        path,
        purpose: pdf ? FilePurpose.announcementPdf : FilePurpose.announcementImage,
        filename: filename,
      );
}

final announcementRepositoryProvider = Provider<AnnouncementRepository>(
  (ref) => AnnouncementRepository(
    ref.watch(dioProvider),
    ref.watch(jsonCacheProvider),
    ref.watch(fileServiceProvider),
    scope: ref.watch(cacheScopeProvider),
  ),
);
