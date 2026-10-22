import 'dart:convert';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/api/dio_client.dart';
import '../../../core/utils/dates.dart';
import '../../food/data/food_repository.dart' show ExportStore;
import '../domain/export_models.dart';

class DepartmentOption {
  const DepartmentOption(this.id, this.name);
  final String id;
  final String name;
}

/// `GET /admin/export` (api-contract.md section 10). Nothing is cached or
/// logged: report bytes only live in memory until the caller writes them to
/// the private export folder.
class ExportRepository {
  ExportRepository(this._dio);
  final Dio _dio;

  /// Active departments for the filter (GET is open to any authenticated user).
  Future<List<DepartmentOption>> departments() => guardApi(() async {
        final res = await _dio.get<Map<String, dynamic>>(
          '/admin/departments',
          queryParameters: {'page_size': 100},
        );
        final raw = res.data?['items'];
        return [
          if (raw is List)
            for (final e in raw)
              if (e is Map && e['is_active'] != false && e['id'] != null)
                DepartmentOption('${e['id']}', '${e['name'] ?? ''}'),
        ];
      });

  /// Streams the workbook straight into a file of [store] (never the whole
  /// workbook in memory). Aborts above [maxBytes] and deletes the partial file
  /// on every failure and on cancel via [cancelToken] (screen closed).
  /// Review 08 L8.
  ///
  /// [includeReasons] sends `include_reasons=true` (Leave and Permission
  /// reason/comment columns; needs `export.sensitive`, otherwise 403).
  /// [extraQuery] is a hook for further optional query parameters; callers
  /// must only pass names the contract documents. The fixed parameters below
  /// always win over it.
  Future<({File file, String filename})> download({
    required ExportType type,
    required DateTime from,
    required DateTime to,
    required ExportStore store,
    String? departmentId,
    String? employeeId,
    bool includeReasons = false,
    Map<String, String> extraQuery = const {},
    CancelToken? cancelToken,
    int maxBytes = exportMaxBytes,
    void Function(int received, int total)? onProgress,
  }) async {
    final f = ymd(from);
    final t = ymd(to);
    final emp = employeeId?.trim() ?? '';
    final token = cancelToken ?? CancelToken();
    File? file;
    IOSink? sink;
    try {
      final res = await _dio.get<ResponseBody>(
        '/admin/export',
        queryParameters: {
          ...extraQuery,
          'type': type.apiValue,
          'from': f,
          'to': t,
          if (departmentId != null && departmentId.isNotEmpty) 'department_id': departmentId,
          if (emp.isNotEmpty) 'employee_id': emp,
          // Opt-in (default false on the server); needs export.sensitive.
          if (includeReasons) 'include_reasons': 'true',
        },
        options: Options(responseType: ResponseType.stream),
        cancelToken: token,
      );
      final body = res.data;
      if (body == null) {
        throw const ApiException(code: ApiException.unknownCode, message: 'Empty response.');
      }
      final declared = int.tryParse(res.headers.value(Headers.contentLengthHeader) ?? '') ?? -1;
      if (declared > maxBytes) {
        token.cancel('too large');
        throw exportTooLarge;
      }
      final filename = safeExportName(
        res.headers.value('content-disposition'),
        'employee-connect-${type.apiValue}-${f}_$t.xlsx',
      );
      file = store.reserve(filename);
      sink = file.openWrite();
      var got = 0;
      await for (final chunk in body.stream) {
        got += chunk.length;
        if (got > maxBytes) {
          token.cancel('too large');
          throw exportTooLarge;
        }
        sink.add(chunk);
        onProgress?.call(got, declared);
      }
      await sink.flush();
      await sink.close();
      sink = null;
      return (file: file, filename: filename);
    } on DioException catch (e) {
      await _discard(sink, file);
      if (CancelToken.isCancel(e)) rethrow;
      throw ApiException.fromDio(await decodeStreamErrorBody(e));
    } on Object {
      await _discard(sink, file);
      rethrow;
    }
  }

  static Future<void> _discard(IOSink? sink, File? file) async {
    try {
      await sink?.close();
    } on Object {
      // ignore
    }
    try {
      if (file != null && file.existsSync()) file.deleteSync();
    } on Object {
      // The folder is wiped on logout, lock and next start anyway.
    }
  }
}

/// Client-side ceiling for one downloaded workbook.
const int exportMaxBytes = 100 * 1024 * 1024;

const ApiException exportTooLarge = ApiException(
  code: 'FILE_TOO_LARGE',
  message: 'The report is too large to download.',
);

/// Like [decodeErrorBody] for a request made with `ResponseType.stream`: reads
/// (at most 64 KB of) the error body first.
Future<DioException> decodeStreamErrorBody(DioException e) async {
  final res = e.response;
  final d = res?.data;
  if (res != null && d is ResponseBody) {
    final bytes = <int>[];
    try {
      await for (final c in d.stream) {
        bytes.addAll(c);
        if (bytes.length > 65536) break;
      }
    } on Object {
      // keep what we have
    }
    return decodeErrorBody(DioException(
      requestOptions: e.requestOptions,
      response: Response<dynamic>(
        requestOptions: res.requestOptions,
        statusCode: res.statusCode,
        headers: res.headers,
        data: bytes,
      ),
      type: e.type,
      error: e.error,
    ),);
  }
  return decodeErrorBody(e);
}

/// Error bodies of a bytes request arrive as raw bytes; decode the JSON
/// envelope so the normal error mapping applies.
DioException decodeErrorBody(DioException e) {
  final res = e.response;
  final d = res?.data;
  if (res != null && d is List<int>) {
    try {
      final json = jsonDecode(utf8.decode(d));
      return DioException(
        requestOptions: e.requestOptions,
        response: Response<dynamic>(
          requestOptions: res.requestOptions,
          statusCode: res.statusCode,
          headers: res.headers,
          data: json,
        ),
        type: e.type,
        error: e.error,
      );
    } on Object {
      // not JSON: keep the original
    }
  }
  return e;
}

/// File name from `Content-Disposition`, reduced to `[A-Za-z0-9_.-]` and
/// forced to end in `.xlsx` (never trust a server path).
String safeExportName(String? disposition, String fallback) {
  if (disposition == null) return fallback;
  final m = RegExp('filename="?([^";]+)"?').firstMatch(disposition);
  var n = m?[1] ?? '';
  n = n.split(RegExp(r'[\\/]')).last.replaceAll(RegExp(r'[^A-Za-z0-9_.-]'), '_');
  if (n.isEmpty || n.startsWith('.')) return fallback;
  return n.toLowerCase().endsWith('.xlsx') ? n : '$n.xlsx';
}

final exportRepositoryProvider =
    Provider<ExportRepository>((ref) => ExportRepository(ref.watch(dioProvider)));

final exportDepartmentsProvider = FutureProvider.autoDispose<List<DepartmentOption>>(
  (ref) => ref.watch(exportRepositoryProvider).departments(),
);
