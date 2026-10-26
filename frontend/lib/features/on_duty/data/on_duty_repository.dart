import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/dio_client.dart';
import '../../attendance/data/location_fix.dart' show LocationFix;
import '../../attendance/domain/attendance_models.dart' show Paged;
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import '../../leave/domain/leave_models.dart' show RequestItem, RequestKind;
import '../domain/duty_admin_models.dart';
import '../domain/on_duty_models.dart';
import '../domain/on_duty_request_logic.dart';

/// docs/plans/13-on-duty-contract.md sections 14.3, 14.5, 14.6.
///
/// No offline cache: a visit punch needs the server, and the site list holds
/// coordinates (which the JSON cache scrubs anyway).
///
/// Employee-side methods only take a [LocationFix] for the ONE-SHOT reading at
/// the moment of the punch; nothing here starts or listens to a location stream.
class OnDutyRepository {
  OnDutyRepository(this._dio);
  final Dio _dio;

  Future<Map<String, dynamic>> _get(String path, [Map<String, Object?>? query]) =>
      guardApi(() async {
        final res = await _dio.get<Map<String, dynamic>>(path, queryParameters: query);
        return res.data ?? const <String, dynamic>{};
      });

  Future<Map<String, dynamic>> _send(String method, String path, [Object? body]) =>
      guardApi(() async {
        final res = await _dio.request<Object?>(
          path,
          data: body,
          options: Options(method: method),
        );
        final d = res.data;
        return d is Map ? Map<String, dynamic>.from(d) : const <String, dynamic>{};
      });

  // ---- employee ------------------------------------------------------------------

  Future<List<DutyLocation>> locations() async =>
      DutyLocation.parseList(await _get('/on-duty/locations'));

  Future<OnDutyToday> today() async => OnDutyToday.fromJson(await _get('/on-duty/today'));

  Map<String, Object?> _fixBody(LocationFix fix, String? selfieId, String? note) => {
        'lat': fix.lat,
        'lng': fix.lng,
        'accuracy_m': fix.accuracyM,
        'fix_time': fix.timestamp?.toUtc().toIso8601String(),
        'selfie_file_id': selfieId,
        'client_time': DateTime.now().toUtc().toIso8601String(),
        'is_mock_location': fix.isMock,
        'note': note,
      };

  Future<VisitResult> checkIn({
    required String locationId,
    required LocationFix fix,
    String? selfieFileId,
    String? note,
  }) async =>
      VisitResult.fromJson(await _send('POST', '/on-duty/check-in', {
        'location_id': locationId,
        ..._fixBody(fix, selfieFileId, cleanNote(note)),
      }),);

  /// [note] non-null replaces the visit note.
  Future<VisitResult> checkOut({
    required LocationFix fix,
    String? visitId,
    String? selfieFileId,
    String? note,
  }) async =>
      VisitResult.fromJson(await _send('POST', '/on-duty/check-out', {
        'visit_id': visitId,
        ..._fixBody(fix, selfieFileId, cleanNote(note)),
      }),);

  /// Uploads a selfie via `POST /files` (purpose `selfie`); returns the file id.
  Future<String> uploadSelfie(String path) => guardApi(() async {
        final form = FormData.fromMap({
          'purpose': 'selfie',
          'file': await MultipartFile.fromFile(path, filename: 'selfie.jpg'),
        });
        final res = await _dio.post<Map<String, dynamic>>('/files', data: form);
        return res.data!['id'] as String;
      });

  /// Visits of one work date (calendar day detail).
  Future<List<DutyVisit>> visitsOn(String date) async {
    final p = Paged.parse(
      await _get('/on-duty/visits', {'from': date, 'to': date, 'page_size': 50}),
      DutyVisit.fromJson,
    );
    return [...p.items]..sort((a, b) => (a.checkInAt ?? DateTime(0)).compareTo(b.checkInAt ?? DateTime(0)));
  }

  Future<RequestItem> applyRequest({
    required DateTime from,
    required DateTime to,
    required String? locationId,
    required String purpose,
  }) async =>
      RequestItem.fromJson(
        await _send(
          'POST',
          '/on-duty/requests',
          onDutyRequestBody(from: from, to: to, locationId: locationId, purpose: purpose),
        ),
        kind: RequestKind.onDuty,
      );

  // ---- admin: locations ------------------------------------------------------------

  Future<Paged<DutyLocationAdmin>> adminLocations(DutyLocationQuery q, {int page = 1}) async =>
      Paged.parse(
        await _get('/admin/duty-locations', {...q.toQuery(), 'page': page, 'page_size': 50}),
        DutyLocationAdmin.fromJson,
      );

  Future<DutyLocationAdmin> adminLocation(String id) async =>
      DutyLocationAdmin.fromJson(await _get('/admin/duty-locations/$id'));

  Future<DutyLocationAdmin> saveLocation(String? id, Map<String, Object?> body) async =>
      DutyLocationAdmin.fromJson(
        await _send(id == null ? 'POST' : 'PUT', id == null ? '/admin/duty-locations' : '/admin/duty-locations/$id', body),
      );

  Future<void> deleteLocation(String id) => guardApi(() async {
        await _dio.delete<void>('/admin/duty-locations/$id');
      });

  Future<DutyAssignments> assignments(String id) async =>
      DutyAssignments.fromJson(await _get('/admin/duty-locations/$id/assignments'));

  Future<DutyAssignments> saveAssignments(String id, AssignmentSelection sel) async =>
      DutyAssignments.fromJson(
        await _send('PUT', '/admin/duty-locations/$id/assignments', sel.toBody()),
      );

  Future<List<EmployeeDutyLocation>> employeeLocations(String userId) async {
    final j = await _get('/admin/employees/$userId/duty-locations');
    final raw = j['items'];
    return [
      if (raw is List)
        for (final e in raw)
          if (e is Map<String, dynamic>) EmployeeDutyLocation.fromJson(e),
    ];
  }

  // ---- admin: visits ---------------------------------------------------------------

  Future<Paged<AdminVisit>> adminVisits(VisitQuery q, {int page = 1}) async => Paged.parse(
        await _get('/admin/on-duty/visits', {...q.toQuery(), 'page': page, 'page_size': 30}),
        AdminVisit.fromJson,
      );

  /// Employee search for the assignment picker (needs `employee.view`).
  Future<List<EmployeeRef>> searchEmployees(String q) async {
    final j = await _get('/admin/employees', {
      if (q.trim().isNotEmpty) 'q': q.trim(),
      'status': 'active',
      'page': 1,
      'page_size': 30,
    });
    final raw = j['items'];
    return [
      if (raw is List)
        for (final e in raw)
          if (e is Map && EmployeeRef.parse(e) != null) EmployeeRef.parse(e)!,
    ];
  }
}

final onDutyRepositoryProvider = Provider<OnDutyRepository>(
  (ref) => OnDutyRepository(ref.watch(dioProvider)),
);
