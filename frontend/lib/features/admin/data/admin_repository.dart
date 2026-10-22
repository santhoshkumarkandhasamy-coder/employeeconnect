import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/dio_client.dart';
import '../../attendance/domain/attendance_models.dart' show Paged;
import '../domain/admin_models.dart';
import '../domain/ops_models.dart';

/// api-contract.md section 9. Deliberately NO caching of any kind: admin data
/// (employees, audit rows, settings, temporary passwords) is sensitive and is
/// only ever held in memory by screens that are open.
class AdminRepository {
  AdminRepository(this._dio);
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
        // 204 / empty bodies come back as null or ''.
        return d is Map ? Map<String, dynamic>.from(d) : const <String, dynamic>{};
      });

  Future<void> _delete(String path) => guardApi(() async {
        await _dio.delete<void>(path);
      });

  // ---- employees ---------------------------------------------------------------

  Future<Paged<Employee>> employees(EmployeeQuery q, {int page = 1}) async => Paged.parse(
        await _get('/admin/employees', {...q.toQuery(), 'page': page, 'page_size': 20}),
        Employee.fromJson,
      );

  Future<Employee> employee(String id) async =>
      Employee.fromJson(await _get('/admin/employees/$id'));

  /// [currentPassword] is the actor's own password (step-up re-auth, needed
  /// when the chosen role is elevated). Never stored or logged.
  Future<TempPasswordResult> createEmployee(
    Map<String, Object?> body, {
    String? currentPassword,
  }) async {
    final j = await _send('POST', '/admin/employees', {
      ...body,
      if (currentPassword != null) 'current_password': currentPassword,
    });
    final emp = j['employee'];
    return TempPasswordResult(
      temporaryPassword: '${j['temporary_password'] ?? ''}',
      employee: emp is Map<String, dynamic> ? Employee.fromJson(emp) : null,
    );
  }

  Future<Employee> updateEmployee(
    String id,
    Map<String, Object?> body, {
    String? currentPassword,
  }) async {
    final j = await _send('PUT', '/admin/employees/$id', {
      ...body,
      if (currentPassword != null) 'current_password': currentPassword,
    });
    final emp = j['employee'];
    return Employee.fromJson(emp is Map<String, dynamic> ? emp : j);
  }

  Future<void> deleteEmployee(String id) => _delete('/admin/employees/$id');

  /// [reason] (offboarding) is sent with deactivate only.
  Future<void> setEmployeeActive(
    String id, {
    required bool active,
    DeactivationReason? reason,
  }) async {
    await _send(
      'POST',
      '/admin/employees/$id/${active ? 'activate' : 'deactivate'}',
      !active && reason != null ? {'reason': reason.code} : null,
    );
  }

  // ---- company -------------------------------------------------------------------

  Future<CompanyAdmin> company() async => CompanyAdmin.fromJson(await _get('/admin/company'));

  Future<CompanyAdmin> saveCompanyName(String name) async =>
      CompanyAdmin.fromJson(await _send('PUT', '/admin/company', {'name': name.trim()}));

  Future<CompanyAdmin> uploadCompanyLogo(String filename, List<int> bytes) =>
      guardApi(() async {
        final res = await _dio.post<Map<String, dynamic>>(
          '/admin/company/logo',
          data: FormData.fromMap({
            'file': MultipartFile.fromBytes(bytes, filename: filename),
          }),
        );
        return CompanyAdmin.fromJson(res.data ?? const <String, dynamic>{});
      });

  Future<void> deleteCompanyLogo() => _delete('/admin/company/logo');

  // ---- license -------------------------------------------------------------------

  Future<LicenseInfo> license() async => LicenseInfo.fromJson(await _get('/admin/license'));

  Future<LicenseInfo> installLicense(String key, {required String currentPassword}) async =>
      LicenseInfo.fromJson(await _send('PUT', '/admin/license', {
        'license_key': key.trim(),
        'current_password': currentPassword,
      }),);

  // ---- sessions ------------------------------------------------------------------

  Future<Paged<SessionItem>> sessions({
    String? employeeId,
    String? clientType,
    int page = 1,
  }) async =>
      Paged.parse(
        await _get('/admin/sessions', {
          if (employeeId != null && employeeId.isNotEmpty) 'employee_id': employeeId,
          if (clientType != null) 'client_type': clientType,
          'page': page,
          'page_size': 20,
        }),
        SessionItem.fromJson,
      );

  Future<Paged<SessionItem>> employeeSessions(String userId, {int page = 1}) async =>
      Paged.parse(
        await _get('/admin/employees/$userId/sessions', {'page': page, 'page_size': 20}),
        SessionItem.fromJson,
      );

  Future<void> revokeSession(String id) => _delete('/admin/sessions/$id');

  /// Signs the employee out of every device without deactivating.
  Future<int> revokeAllSessions(String userId) async {
    final j = await _send('POST', '/admin/employees/$userId/revoke-sessions');
    return (j['revoked'] as num?)?.toInt() ?? 0;
  }

  // ---- console data ----------------------------------------------------------------

  Future<Dashboard> dashboard() async => Dashboard.fromJson(await _get('/admin/dashboard'));

  Future<SystemHealth> health() async => SystemHealth.fromJson(await _get('/admin/system/health'));

  Future<Paged<AttendanceDayRow>> attendanceDay({
    String? date,
    String status = 'all',
    String? q,
    int page = 1,
  }) async =>
      Paged.parse(
        await _get('/admin/attendance/day', {
          if (date != null) 'date': date,
          'status': status,
          if (q != null && q.trim().isNotEmpty) 'q': q.trim(),
          'page': page,
          'page_size': 30,
        }),
        AttendanceDayRow.fromJson,
      );

  Future<void> unlockEmployee(String id) async {
    await _send('POST', '/admin/employees/$id/unlock');
  }

  /// The result holds the temporary password: show once, never store.
  ///
  /// [currentPassword] (the actor's own password) is only required by the
  /// server when the target holds any permission (api-contract.md section 9,
  /// step-up table); for plain employees the body is omitted.
  Future<String> resetPassword(String id, {String? currentPassword}) async {
    final j = await _send(
      'POST',
      '/admin/employees/$id/reset-password',
      currentPassword == null ? null : {'current_password': currentPassword},
    );
    return '${j['temporary_password'] ?? ''}';
  }

  Future<Paged<ResetRequest>> resetRequests(String status, {int page = 1}) async => Paged.parse(
        await _get('/admin/password-reset-requests', {
          'status': status,
          'page': page,
          'page_size': 20,
        }),
        ResetRequest.fromJson,
      );

  Future<List<LeaveBalanceRow>> leaveBalances(String employeeId, int year) async {
    final j = await _get('/admin/employees/$employeeId/leave-balances', {'year': year});
    final raw = j['items'] ?? j['balances'];
    return [
      if (raw is List)
        for (final e in raw)
          if (e is Map<String, dynamic>) LeaveBalanceRow.fromJson(e),
    ];
  }

  Future<void> saveLeaveBalances(String employeeId, int year, List<LeaveBalanceRow> rows) async {
    await _send('PUT', '/admin/employees/$employeeId/leave-balances', {
      'year': year,
      'items': [
        for (final r in rows) {'leave_type_id': r.leaveTypeId, 'quota': r.quota},
      ],
    });
  }

  // ---- roles -------------------------------------------------------------------

  Future<List<RoleItem>> roles() async {
    final j = await _get('/admin/roles', {'page_size': 100});
    final raw = j['items'];
    return [
      if (raw is List)
        for (final e in raw)
          if (e is Map<String, dynamic>) RoleItem.fromJson(e),
    ];
  }

  Future<List<PermissionDef>> permissionCatalogue() async {
    final j = await _get('/admin/permissions');
    final raw = j['items'];
    return [
      if (raw is List)
        for (final e in raw)
          if (e is Map<String, dynamic>) PermissionDef.fromJson(e),
    ];
  }

  Future<RoleItem> createRole(String name, String? description, List<String> codes) async =>
      RoleItem.fromJson(await _send('POST', '/admin/roles', {
        'name': name,
        'description': description,
        'permission_codes': codes,
      }),);

  Future<void> updateRole(String id, String name, String? description) async {
    await _send('PUT', '/admin/roles/$id', {'name': name, 'description': description});
  }

  Future<void> setRolePermissions(
    String id,
    List<String> codes, {
    required String currentPassword,
  }) async {
    await _send('PUT', '/admin/roles/$id/permissions', {
      'permission_codes': codes,
      'current_password': currentPassword,
    });
  }

  Future<void> deleteRole(String id) => _delete('/admin/roles/$id');

  // ---- masters -----------------------------------------------------------------

  Future<List<MasterRecord>> masterList(MasterSpec spec, {int? year}) async {
    final out = <MasterRecord>[];
    for (var page = 1; page <= 20; page++) {
      final j = await _get(spec.readPath, {
        if (year != null) 'year': year,
        'page': page,
        'page_size': 100,
      });
      final p = Paged.parse(j, MasterRecord.fromJson);
      out.addAll(p.items);
      // Un-paginated responses have no `total`, so they stop after one page.
      if (j['total'] == null || !p.hasMore) break;
    }
    return out;
  }

  Future<void> masterSave(MasterSpec spec, String? id, Map<String, Object?> body) async {
    await _send(
      id == null ? 'POST' : spec.updateMethod,
      id == null ? spec.path : '${spec.path}/$id',
      body,
    );
  }

  /// `POST /admin/leave-balances/apply-defaults` (idempotent).
  Future<({int employees, int created})> applyDefaultQuotas(int year) async {
    final j = await _send('POST', '/admin/leave-balances/apply-defaults', {'year': year});
    return (
      employees: (j['employees'] as num?)?.toInt() ?? 0,
      created: (j['created'] as num?)?.toInt() ?? 0,
    );
  }

  Future<void> masterDelete(MasterSpec spec, String id) => _delete('${spec.path}/$id');

  /// Active departments / shifts for the employee form (open to any user).
  Future<List<NamedRef>> refs(String path) async {
    final j = await _get(path, {'page_size': 100});
    final raw = j['items'];
    return [
      if (raw is List)
        for (final e in raw)
          if (e is Map<String, dynamic> && e['is_active'] != false)
            if (NamedRef.parse(e) case final r?) r,
    ];
  }

  // ---- settings ----------------------------------------------------------------

  Future<Map<String, Object?>> settings() async {
    final j = await _get('/admin/settings');
    final s = j['settings'];
    return s is Map ? Map<String, Object?>.from(s) : <String, Object?>{};
  }

  Future<Map<String, Object?>> saveSettings(Map<String, Object?> changes) async {
    final j = await _send('PUT', '/admin/settings', {'settings': changes});
    final s = j['settings'];
    return s is Map ? Map<String, Object?>.from(s) : <String, Object?>{};
  }

  // ---- audit -------------------------------------------------------------------

  Future<Paged<AuditEntry>> auditLogs(AuditQuery q, {int page = 1}) async => Paged.parse(
        await _get('/admin/audit-logs', {...q.toQuery(), 'page': page, 'page_size': 30}),
        AuditEntry.fromJson,
      );
}

final adminRepositoryProvider = Provider<AdminRepository>(
  (ref) => AdminRepository(ref.watch(dioProvider)),
);
