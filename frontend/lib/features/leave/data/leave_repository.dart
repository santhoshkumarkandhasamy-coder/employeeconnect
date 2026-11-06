import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/dio_client.dart';
import '../../../core/api/api_exception.dart';
import '../../../core/cache/json_cache.dart';
import '../../../core/files/file_service.dart';
import '../../attendance/data/attendance_repository.dart' show cacheScopeProvider;
import '../../attendance/domain/attendance_models.dart' show Paged;
import '../domain/leave_logic.dart' show isValidBalanceYear, maxBalanceYear, minBalanceYear;
import '../domain/leave_models.dart';

/// api-contract.md section 4 (+ POST /files, purpose `leave_attachment`).
/// Types, balance and the user's own requests are cached for offline reading.
/// Other people's requests (approvals) are never cached.
class LeaveRepository {
  LeaveRepository(this._dio, this._cache, {this.scope = 'anon', FileService? files})
      : _files = files ?? FileService(_dio);

  final Dio _dio;
  final JsonCache _cache;
  final String scope;
  final FileService _files;

  Future<Map<String, dynamic>> _get(String path, [Map<String, Object?>? query]) =>
      guardApi(() async {
        final res = await _dio.get<Map<String, dynamic>>(path, queryParameters: query);
        return res.data ?? const <String, dynamic>{};
      });

  Future<Map<String, dynamic>> _send(String method, String path, [Object? body]) =>
      guardApi(() async {
        final res = await _dio.request<Map<String, dynamic>>(
          path,
          data: body,
          options: Options(method: method),
        );
        return res.data ?? const <String, dynamic>{};
      });

  Future<Cached<List<LeaveType>>> types() => fetchWithCache(
        cache: _cache,
        key: '$scope.leave.types',
        fetch: () => _get('/leave/types'),
        parse: LeaveType.parseList,
      );

  Future<Cached<BalanceData>> balance(int year) {
    if (!isValidBalanceYear(year)) {
      throw const ApiException(
        code: 'VALIDATION_ERROR',
        message: 'Choose a year between $minBalanceYear and $maxBalanceYear.',
        status: 422,
      );
    }
    return _balance(year);
  }

  Future<Cached<BalanceData>> _balance(int year) => fetchWithCache(
        cache: _cache,
        key: '$scope.leave.bal.$year',
        fetch: () => _get('/leave/balance', {'year': year}),
        parse: (j) => BalanceData.fromJson(j as Map<String, dynamic>),
      );

  /// The latest 100 leave and 100 permission requests, newest first.
  Future<Cached<List<RequestItem>>> myRequests() => fetchWithCache(
        cache: _cache,
        key: '$scope.leave.mine',
        fetch: () async => <String, dynamic>{
          'leave': await _get('/leave/requests', {'page': 1, 'page_size': 100}),
          'permission': await _get('/permission/requests', {'page': 1, 'page_size': 100}),
        },
        parse: (j) => mergeRequests(j as Map<String, dynamic>),
      );

  Future<RequestItem> applyLeave({
    required String leaveTypeId,
    required String fromDate,
    required String toDate,
    required bool halfDay,
    String? halfDaySession,
    required String reason,
    String? attachmentFileId,
  }) async =>
      RequestItem.fromJson(
        await _send('POST', '/leave/requests', {
          'leave_type_id': leaveTypeId,
          'from_date': fromDate,
          'to_date': toDate,
          'half_day': halfDay,
          'half_day_session': halfDay ? halfDaySession : null,
          'reason': reason,
          'attachment_file_id': attachmentFileId,
        }),
        kind: RequestKind.leave,
      );

  Future<RequestItem> applyPermission({
    required String date,
    required String fromTime,
    required String toTime,
    required String reason,
  }) async =>
      RequestItem.fromJson(
        await _send('POST', '/permission/requests', {
          'date': date,
          'from_time': fromTime,
          'to_time': toTime,
          'reason': reason,
        }),
        kind: RequestKind.permission,
      );

  /// Uploads a supporting document through the shared [FileService]; returns
  /// the file id.
  Future<String> uploadAttachment(String path, {String? filename}) => _files.upload(
        path,
        purpose: FilePurpose.leaveAttachment,
        filename: filename ?? 'attachment.jpg',
      );

  Future<RequestItem> cancel(RequestKind kind, String id) async => RequestItem.fromJson(
        await _send('POST', '/${kind.wire}/requests/$id/cancel'),
        kind: kind,
      );

  // ---- approvals (never cached) ----------------------------------------------

  Future<Paged<RequestItem>> adminRequests({
    required RequestKind kind,
    RequestStatus? status,
    int page = 1,
  }) async {
    final j = await _get('/admin/requests', {
      'type': kind.wire,
      if (status != null) 'status': status.wire,
      'page': page,
      'page_size': 20,
    });
    return Paged.parse(j, (e) => RequestItem.fromJson(e, kind: kind));
  }

  Future<RequestItem> decide(
    RequestKind kind,
    String id, {
    required bool approve,
    String? comment,
  }) async =>
      RequestItem.fromJson(
        await _send('PUT', '/admin/requests/${kind.wire}/$id/${approve ? 'approve' : 'reject'}', {
          if (comment != null && comment.trim().isNotEmpty) 'comment': comment.trim(),
        }),
        kind: kind,
      );
}

/// Merges `{leave: {items}, permission: {items}}` into one list, newest first.
List<RequestItem> mergeRequests(Map<String, dynamic> j) {
  List<RequestItem> of(Object? page, RequestKind k) {
    final raw = page is Map ? page['items'] : null;
    return [
      if (raw is List)
        for (final e in raw)
          if (e is Map<String, dynamic>) RequestItem.fromJson(e, kind: k),
    ];
  }

  final all = [...of(j['leave'], RequestKind.leave), ...of(j['permission'], RequestKind.permission)];
  final epoch = DateTime.fromMillisecondsSinceEpoch(0);
  all.sort((a, b) => (b.createdAt ?? epoch).compareTo(a.createdAt ?? epoch));
  return all;
}

final leaveRepositoryProvider = Provider<LeaveRepository>(
  (ref) => LeaveRepository(
    ref.watch(dioProvider),
    ref.watch(jsonCacheProvider),
    scope: ref.watch(cacheScopeProvider),
    files: ref.watch(fileServiceProvider),
  ),
);
