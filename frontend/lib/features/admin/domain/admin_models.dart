import 'dart:convert';
import 'dart:math' as math;

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../attendance/domain/attendance_models.dart' show AttendanceRecord;
import '../../profile/domain/profile_models.dart'
    show EmergencyContact, normalizePhone;

/// api-contract.md section 9 (admin). Nothing here is cached or persisted.

// ---- permission gating ---------------------------------------------------------

abstract final class AdminPerm {
  static const employeeView = 'employee.view';
  static const employeeCreate = 'employee.create';
  static const employeeUpdate = 'employee.update';
  static const employeeDelete = 'employee.delete';
  static const employeeDeactivate = 'employee.deactivate';
  static const employeeUnlock = 'employee.unlock';
  static const employeeResetPassword = 'employee.reset_password';
  static const employeeViewSensitive = 'employee.view_sensitive';
  static const leaveBalanceManage = 'leave_balance.manage';
  static const departmentManage = 'department.manage';
  static const shiftManage = 'shift.manage';
  static const holidayManage = 'holiday.manage';
  static const leaveTypeManage = 'leave_type.manage';
  static const clientLocationManage = 'client_location.manage';
  static const attendanceManual = 'attendance.manual';
  static const announcementManage = 'announcement.manage';
  static const roleView = 'role.view';
  static const roleManage = 'role.manage';
  static const roleAssign = 'role.assign';
  static const settingsManage = 'settings.manage';
  static const auditView = 'audit.view';
  static const companyManage = 'company.manage';
  static const sessionManage = 'session.manage';
  static const systemView = 'system.view';
  static const licenseManage = 'license.manage';
  static const consoleAccess = 'console.access';
  static const attendanceViewAll = 'attendance.view_all';
  static const payrollViewAll = 'payroll.view_all';
}

/// One entry of the Admin home. Shown only when the user holds [permission];
/// the server enforces it again on every call.
enum AdminSection {
  employees('Employees', AdminPerm.employeeView),
  resetRequests('Password reset requests', AdminPerm.employeeResetPassword),
  roles('Roles and permissions', AdminPerm.roleView),
  departments('Departments', AdminPerm.departmentManage),
  shifts('Shifts', AdminPerm.shiftManage),
  holidays('Holidays', AdminPerm.holidayManage),
  leaveTypes('Leave types and quotas', AdminPerm.leaveTypeManage),
  clientLocations('Client locations (on duty)', AdminPerm.clientLocationManage),
  manualAttendance('Manual attendance', AdminPerm.attendanceManual),
  announcements('Announcements', AdminPerm.announcementManage),
  settings('Settings', AdminPerm.settingsManage),
  audit('Audit log', AdminPerm.auditView),
  sessions('Sessions and devices', AdminPerm.sessionManage),
  license('License', AdminPerm.licenseManage),
  company('Company', AdminPerm.companyManage),
  payroll('Payroll', AdminPerm.payrollViewAll);

  const AdminSection(this.label, this.permission);
  final String label;
  final String permission;
}

List<AdminSection> adminSections(List<String> permissions) => [
      for (final s in AdminSection.values)
        if (permissions.contains(s.permission)) s,
    ];

bool canOpenAdmin(List<String> permissions) => adminSections(permissions).isNotEmpty;

// ---- small value types ---------------------------------------------------------

String _s(Object? v) => v == null ? '' : '$v';
String? _opt(Object? v) => v is String && v.isNotEmpty ? v : null;

class NamedRef {
  const NamedRef({required this.id, required this.name});
  final String id;
  final String name;

  static NamedRef? parse(Object? v) {
    if (v is Map) return NamedRef(id: _s(v['id']), name: _s(v['name']));
    return null;
  }
}

class EmployeeRef {
  const EmployeeRef({required this.id, required this.employeeId, required this.fullName});
  final String id;
  final String employeeId;
  final String fullName;

  String get display => fullName.isEmpty ? employeeId : '$fullName ($employeeId)';

  static EmployeeRef? parse(Object? v) => v is Map
      ? EmployeeRef(
          id: _s(v['id']),
          employeeId: _s(v['employee_id']),
          fullName: _s(v['full_name']),
        )
      : null;
}

/// The seven personal fields of `GET/PUT /admin/employees/{id}` `profile`
/// (present only for holders of `employee.view_sensitive`).
class EmployeeProfile {
  const EmployeeProfile({
    this.dob,
    this.weddingAnniversary,
    this.personalEmail,
    this.phone,
    this.emergency,
    this.bloodGroup,
    this.address,
  });
  final String? dob;
  final String? weddingAnniversary;
  final String? personalEmail;
  final String? phone;
  final EmergencyContact? emergency;
  final String? bloodGroup;
  final String? address;

  static EmployeeProfile? parse(Object? v) {
    if (v is! Map) return null;
    return EmployeeProfile(
      dob: _opt(v['dob']),
      weddingAnniversary: _opt(v['wedding_anniversary']),
      personalEmail: _opt(v['personal_email']),
      phone: _opt(v['phone']),
      emergency: EmergencyContact.parse(v['emergency_contact']),
      bloodGroup: _opt(v['blood_group']),
      address: _opt(v['address']),
    );
  }
}

const List<String> bloodGroups = ['', 'A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', 'O+', 'O-'];

/// Raw text of the personal-field form.
class ProfileInput {
  const ProfileInput({
    this.dob = '',
    this.weddingAnniversary = '',
    this.personalEmail = '',
    this.phone = '',
    this.emergencyName = '',
    this.emergencyRelationship = '',
    this.emergencyPhone = '',
    this.bloodGroup = '',
    this.address = '',
  });
  final String dob;
  final String weddingAnniversary;
  final String personalEmail;
  final String phone;
  final String emergencyName;
  final String emergencyRelationship;
  final String emergencyPhone;
  final String bloodGroup;
  final String address;
}

/// Partial update for the personal fields: only keys whose value differs from
/// [original] are included; an emptied field is sent as null (clears it).
Map<String, Object?> profilePatch(EmployeeProfile original, ProfileInput i) {
  final out = <String, Object?>{};
  void text(String key, String? orig, String now) {
    final n = now.trim();
    if (n != (orig ?? '')) out[key] = n.isEmpty ? null : n;
  }

  text('dob', original.dob, i.dob);
  text('wedding_anniversary', original.weddingAnniversary, i.weddingAnniversary);
  text('personal_email', original.personalEmail, i.personalEmail);
  text('blood_group', original.bloodGroup, i.bloodGroup);
  text('address', original.address, i.address);

  final phone = normalizePhone(i.phone.trim());
  if (phone != normalizePhone(original.phone ?? '')) out['phone'] = phone.isEmpty ? null : phone;

  final o = original.emergency;
  final name = i.emergencyName.trim();
  final rel = i.emergencyRelationship.trim();
  final ephone = normalizePhone(i.emergencyPhone.trim());
  final hadEc = o != null;
  final hasEc = name.isNotEmpty || ephone.isNotEmpty;
  if (hasEc) {
    if (o == null ||
        o.name != name ||
        o.relationship != rel ||
        normalizePhone(o.phone) != ephone) {
      out['emergency_contact'] = {
        'name': name,
        if (rel.isNotEmpty) 'relationship': rel,
        'phone': ephone,
      };
    }
  } else if (hadEc) {
    out['emergency_contact'] = null;
  }

  return out;
}

class Employee {
  const Employee({
    required this.id,
    required this.employeeId,
    required this.fullName,
    this.officialEmail = '',
    this.profile,
    this.department,
    this.shift,
    this.role,
    this.manager,
    this.status = 'active',
    this.isLocked = false,
    this.lockedUntil,
    this.joinDate,
    this.lastLoginAt,
    this.hasAccountInfo = true,
    this.deactivatedAt,
    this.deactivationReason,
  });

  final String id;
  final String employeeId;
  final String fullName;
  final String officialEmail;

  /// False when the server sent the reduced shape (managers see reports
  /// without `last_login_at`, `is_locked` and `locked_until`, review L5).
  final bool hasAccountInfo;

  /// Personal fields; null unless the caller holds `employee.view_sensitive`
  /// and the response is a detail (list and create have no `profile`).
  final EmployeeProfile? profile;
  final NamedRef? department;
  final NamedRef? shift;
  final NamedRef? role;
  final EmployeeRef? manager;
  final String status;
  final bool isLocked;
  final DateTime? lockedUntil;
  final String? joinDate;
  final DateTime? lastLoginAt;
  final DateTime? deactivatedAt;
  final String? deactivationReason;

  bool get isActive => status == 'active';

  factory Employee.fromJson(Map<String, dynamic> j) => Employee(
        id: _s(j['id']),
        employeeId: _s(j['employee_id']),
        fullName: _s(j['full_name']),
        officialEmail: _s(j['official_email']),
        profile: EmployeeProfile.parse(j['profile']),
        department: NamedRef.parse(j['department']),
        shift: NamedRef.parse(j['shift']),
        role: NamedRef.parse(j['role']),
        manager: EmployeeRef.parse(j['manager']),
        status: _opt(j['status']) ?? 'active',
        isLocked: j['is_locked'] == true,
        lockedUntil: parseTimestamp(j['locked_until']),
        joinDate: _opt(j['join_date']),
        lastLoginAt: parseTimestamp(j['last_login_at']),
        deactivatedAt: parseTimestamp(j['deactivated_at']),
        deactivationReason: _opt(j['deactivation_reason']),
        hasAccountInfo: j.containsKey('is_locked') ||
            j.containsKey('last_login_at') ||
            j.containsKey('locked_until'),
      );
}

/// `POST /admin/employees` and `POST .../reset-password` results. The
/// temporary password is shown once and never stored or logged.
class TempPasswordResult {
  const TempPasswordResult({required this.temporaryPassword, this.employee});
  final String temporaryPassword;
  final Employee? employee;
}

class RoleItem {
  const RoleItem({
    required this.id,
    required this.name,
    this.description = '',
    this.isSystem = false,
    this.userCount,
    this.permissionCodes,
  });
  final String id;
  final String name;
  final String description;
  final bool isSystem;
  final int? userCount;

  /// Sorted codes; always present in `Role` responses.
  final List<String>? permissionCodes;

  bool get isAdminRole => name.toLowerCase() == 'admin';

  factory RoleItem.fromJson(Map<String, dynamic> j) {
    final codes = j['permission_codes'] ?? j['permissions'];
    return RoleItem(
      id: _s(j['id']),
      name: _s(j['name']),
      description: _s(j['description']),
      isSystem: j['is_system'] == true,
      userCount: (j['user_count'] as num?)?.toInt(),
      permissionCodes: codes is List
          ? [
              for (final c in codes)
                if (c is String) c else if (c is Map) _s(c['code']),
            ]
          : null,
    );
  }
}

class PermissionDef {
  const PermissionDef({required this.code, required this.module, this.description = ''});
  final String code;
  final String module;
  final String description;

  factory PermissionDef.fromJson(Map<String, dynamic> j) => PermissionDef(
        code: _s(j['code']),
        module: _s(j['module']).isEmpty ? 'other' : _s(j['module']),
        description: _s(j['description']),
      );
}

/// Groups the catalogue by module, modules and codes sorted.
Map<String, List<PermissionDef>> groupByModule(List<PermissionDef> defs) {
  final out = <String, List<PermissionDef>>{};
  for (final d in defs) {
    out.putIfAbsent(d.module, () => []).add(d);
  }
  final keys = out.keys.toList()..sort();
  return {
    for (final k in keys) k: (out[k]!..sort((a, b) => a.code.compareTo(b.code))),
  };
}

/// A master record (department, shift, holiday, leave type) kept as the raw
/// JSON map; the screens are driven by a [MasterSpec].
class MasterRecord {
  const MasterRecord(this.data);
  final Map<String, dynamic> data;
  String get id => _s(data['id']);
  bool get isActive => data['is_active'] as bool? ?? true;
  factory MasterRecord.fromJson(Map<String, dynamic> j) => MasterRecord(j);
}

class AuditEntry {
  const AuditEntry({
    required this.id,
    required this.action,
    this.ts,
    this.actor,
    this.entityType = '',
    this.entityId,
    this.before,
    this.after,
    this.ip,
    this.outcome = '',
  });
  final String id;
  final DateTime? ts;
  final EmployeeRef? actor;
  final String action;
  final String entityType;
  final String? entityId;
  final Object? before;
  final Object? after;
  final String? ip;
  final String outcome;

  factory AuditEntry.fromJson(Map<String, dynamic> j) => AuditEntry(
        id: _s(j['id']),
        ts: parseTimestamp(j['ts']),
        actor: EmployeeRef.parse(j['actor']),
        action: _s(j['action']),
        entityType: _s(j['entity_type']),
        entityId: _opt(j['entity_id']),
        before: j['before'],
        after: j['after'],
        ip: _opt(j['ip']),
        outcome: _s(j['outcome']),
      );
}

String prettyJson(Object? v) {
  if (v == null) return 'none';
  try {
    return const JsonEncoder.withIndent('  ').convert(v);
  } on Object {
    return '$v';
  }
}

class ResetRequest {
  const ResetRequest({
    required this.id,
    required this.status,
    this.requestedAt,
    this.handledAt,
    this.user,
  });
  final String id;
  final String status;
  final DateTime? requestedAt;
  final DateTime? handledAt;
  final EmployeeRef? user;

  factory ResetRequest.fromJson(Map<String, dynamic> j) => ResetRequest(
        id: _s(j['id']),
        status: _s(j['status']),
        requestedAt: parseTimestamp(j['requested_at']),
        handledAt: parseTimestamp(j['handled_at']),
        user: EmployeeRef.parse(j['user']),
      );
}

class LeaveBalanceRow {
  const LeaveBalanceRow({
    required this.leaveTypeId,
    required this.label,
    required this.quota,
    this.used = 0,
    this.pending = 0,
  });
  final String leaveTypeId;
  final String label;
  final double quota;
  final double used;
  final double pending;

  double get minQuota => used + pending;

  LeaveBalanceRow withQuota(double q) => LeaveBalanceRow(
        leaveTypeId: leaveTypeId,
        label: label,
        quota: q,
        used: used,
        pending: pending,
      );

  /// Tolerant of both `leave_type:{id,code,name}` and flat ids (the contract
  /// does not define the GET shape).
  factory LeaveBalanceRow.fromJson(Map<String, dynamic> j) {
    final t = j['leave_type'];
    final id = t is Map ? _s(t['id']) : _s(j['leave_type_id']);
    final name = t is Map
        ? (_opt(t['name']) ?? _s(t['code']))
        : (_opt(j['leave_type_name']) ?? _opt(j['name']) ?? _s(j['leave_type_code']));
    double n(Object? v) => (v as num?)?.toDouble() ?? 0;
    return LeaveBalanceRow(
      leaveTypeId: id,
      label: name.isEmpty ? id : name,
      quota: n(j['quota'] ?? j['total']),
      used: n(j['used']),
      pending: n(j['pending']),
    );
  }
}

// ---- master specs --------------------------------------------------------------

enum FieldKind { text, integer, decimal, boolean, time, date, weekdays }

class FieldSpec {
  const FieldSpec(
    this.key,
    this.label,
    this.kind, {
    this.required = true,
    this.min,
    this.max,
    this.maxLength = 100,
    this.nullable = false,
    this.createOnly = false,
    this.defaultValue,
    this.code = false,
    this.decimals = 1,
  });

  /// Allowed decimal places for [FieldKind.decimal] (coordinates need 7).
  final int decimals;

  /// Short code: letters, digits, dash, underscore (the server upper-cases it).
  final bool code;
  final String key;
  final String label;
  final FieldKind kind;
  final bool required;
  final num? min;
  final num? max;
  final int maxLength;

  /// Empty input is sent as JSON null.
  final bool nullable;
  final bool createOnly;
  final Object? defaultValue;
}

class MasterSpec {
  const MasterSpec({
    required this.title,
    required this.singular,
    required this.path,
    required this.permission,
    required this.fields,
    required this.titleKey,
    required this.subtitle,
    this.listPath,
    this.hasYearFilter = false,
    this.updateMethod = 'PUT',
    this.hasApplyDefaults = false,
    this.hasGeoPicker = false,
  });

  /// Offer "Use my current location" to fill the `lat` / `lng` fields.
  final bool hasGeoPicker;

  /// HTTP method for edits (holidays use PATCH, the masters PUT).
  final String updateMethod;

  /// Offer "Apply default quotas" (leave types, needs `leave_balance.manage`).
  final bool hasApplyDefaults;
  final String title;
  final String singular;

  /// Mutation path (`/admin/departments`).
  final String path;

  /// Read path when it differs (`/holidays`).
  final String? listPath;
  final String permission;
  final List<FieldSpec> fields;
  final String titleKey;
  final String Function(Map<String, dynamic>) subtitle;
  final bool hasYearFilter;

  String get readPath => listPath ?? path;
  bool get hasActiveFlag => fields.any((f) => f.key == 'is_active');
}

String _weekdays(Object? v) {
  const names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  if (v is! List) return '';
  final days = [for (final d in v) if (d is num && d >= 1 && d <= 7) d.toInt()]..sort();
  return days.map((d) => names[d - 1]).join(', ');
}

String _trimTime(Object? v) {
  final s = _s(v);
  return s.length >= 5 ? s.substring(0, 5) : s;
}

const departmentSpec = MasterSpec(
  title: 'Departments',
  singular: 'department',
  path: '/admin/departments',
  permission: AdminPerm.departmentManage,
  titleKey: 'name',
  fields: [
    FieldSpec('name', 'Name', FieldKind.text),
    FieldSpec('code', 'Code', FieldKind.text, maxLength: 20, code: true),
    FieldSpec('is_active', 'Active', FieldKind.boolean, defaultValue: true),
  ],
  subtitle: _deptSub,
);

String _deptSub(Map<String, dynamic> d) =>
    '${_s(d['code'])}${d['is_active'] == false ? '  (inactive)' : ''}';

final shiftSpec = MasterSpec(
  title: 'Shifts',
  singular: 'shift',
  path: '/admin/shifts',
  permission: AdminPerm.shiftManage,
  titleKey: 'name',
  fields: const [
    FieldSpec('name', 'Name', FieldKind.text),
    FieldSpec('start_time', 'Start time', FieldKind.time),
    FieldSpec('end_time', 'End time', FieldKind.time),
    FieldSpec('grace_minutes', 'Grace minutes', FieldKind.integer, min: 0, max: 120, defaultValue: 10),
    FieldSpec('working_days', 'Working days', FieldKind.weekdays),
    FieldSpec('is_active', 'Active', FieldKind.boolean, defaultValue: true),
  ],
  subtitle: (d) => '${_trimTime(d['start_time'])} - ${_trimTime(d['end_time'])}, '
      '${_weekdays(d['working_days'])}${d['is_active'] == false ? '  (inactive)' : ''}',
);

final holidaySpec = MasterSpec(
  title: 'Holidays',
  singular: 'holiday',
  path: '/holidays',
  permission: AdminPerm.holidayManage,
  titleKey: 'name',
  hasYearFilter: true,
  updateMethod: 'PATCH',
  fields: const [
    FieldSpec('date', 'Date', FieldKind.date),
    FieldSpec('name', 'Name', FieldKind.text, maxLength: 120),
  ],
  subtitle: (d) => _s(d['date']),
);

final leaveTypeSpec = MasterSpec(
  title: 'Leave types and quotas',
  singular: 'leave type',
  path: '/admin/leave-types',
  permission: AdminPerm.leaveTypeManage,
  hasApplyDefaults: true,
  titleKey: 'name',
  fields: const [
    FieldSpec('code', 'Code', FieldKind.text, maxLength: 20, code: true),
    FieldSpec('name', 'Name', FieldKind.text),
    FieldSpec('annual_quota', 'Annual quota (days)', FieldKind.decimal, min: 0, max: 999.9, defaultValue: 0),
    FieldSpec('is_paid', 'Paid', FieldKind.boolean, defaultValue: true),
    FieldSpec('allow_half_day', 'Allow half day', FieldKind.boolean, defaultValue: true),
    FieldSpec(
      'requires_attachment_after_days',
      'Attachment required after (days)',
      FieldKind.integer,
      required: false,
      nullable: true,
      min: 0,
      max: 366,
    ),
    FieldSpec(
      'max_consecutive_days',
      'Max consecutive days',
      FieldKind.integer,
      required: false,
      nullable: true,
      min: 1,
      max: 366,
    ),
    FieldSpec('is_active', 'Active', FieldKind.boolean, defaultValue: true),
  ],
  subtitle: (d) => '${_s(d['code'])}, quota ${_s(d['annual_quota'])} days'
      '${d['is_active'] == false ? '  (inactive)' : ''}',
);

/// Client sites for on duty. On an approved on-duty day an employee can mark
/// attendance only within [radius_m] metres of these coordinates.
final clientLocationSpec = MasterSpec(
  title: 'Client locations',
  singular: 'client location',
  path: '/admin/client-locations',
  permission: AdminPerm.clientLocationManage,
  titleKey: 'name',
  hasGeoPicker: true,
  fields: const [
    FieldSpec('name', 'Name', FieldKind.text, maxLength: 120),
    FieldSpec('address', 'Address', FieldKind.text, required: false, nullable: true, maxLength: 300),
    FieldSpec('lat', 'Latitude', FieldKind.decimal, min: -90, max: 90, decimals: 7),
    FieldSpec('lng', 'Longitude', FieldKind.decimal, min: -180, max: 180, decimals: 7),
    FieldSpec('radius_m', 'Allowed radius (metres)', FieldKind.integer, min: 20, max: 5000, defaultValue: 200),
    FieldSpec('is_active', 'Active', FieldKind.boolean, defaultValue: true),
  ],
  subtitle: (d) => '${_opt(d['address']) ?? '${_s(d['lat'])}, ${_s(d['lng'])}'}, '
      'radius ${_s(d['radius_m'])} m${d['is_active'] == false ? '  (inactive)' : ''}',
);

/// Validates the raw text of one master field; null when valid.
String? validateField(FieldSpec f, String? raw) {
  final s = raw?.trim() ?? '';
  switch (f.kind) {
    case FieldKind.boolean:
    case FieldKind.weekdays:
      return null;
    case FieldKind.text:
      if (s.isEmpty) return f.required ? 'Enter ${f.label.toLowerCase()}' : null;
      if (s.length > f.maxLength) return '${f.label} can be at most ${f.maxLength} characters';
      if (f.code && !RegExp(r'^[A-Za-z0-9_-]+$').hasMatch(s)) {
        return 'Use letters, digits, dash or underscore';
      }
      return null;
    case FieldKind.integer:
      if (s.isEmpty) return f.required ? 'Enter ${f.label.toLowerCase()}' : null;
      final n = int.tryParse(s);
      if (n == null) return 'Enter a whole number';
      if (f.min != null && n < f.min!) return 'Must be at least ${f.min}';
      if (f.max != null && n > f.max!) return 'Must be at most ${f.max}';
      return null;
    case FieldKind.decimal:
      if (s.isEmpty) return f.required ? 'Enter ${f.label.toLowerCase()}' : null;
      final n = double.tryParse(s);
      if (n == null || n.isNaN || n.isInfinite) return 'Enter a number';
      if (f.min != null && n < f.min!) return 'Must be at least ${f.min}';
      if (f.max != null && n > f.max!) return 'Must be at most ${f.max}';
      final scale = math.pow(10, f.decimals);
      if ((n * scale - (n * scale).roundToDouble()).abs() > 1e-6) {
        return f.decimals == 1 ? 'At most one decimal place' : 'At most ${f.decimals} decimal places';
      }
      return null;
    case FieldKind.time:
      return isValidTime(s) ? null : 'Use HH:MM (24 hour)';
    case FieldKind.date:
      return parseYmd(s) == null ? 'Pick a date' : null;
  }
}

bool isValidTime(String s) {
  final m = RegExp(r'^(\d{2}):(\d{2})$').firstMatch(s);
  if (m == null) return false;
  return int.parse(m[1]!) <= 23 && int.parse(m[2]!) <= 59;
}

/// Cross-field checks (null when valid): shift end must differ from start and
/// at least one working day is required.
String? validateMasterCross(MasterSpec spec, Map<String, Object?> values) {
  if (spec.fields.any((f) => f.kind == FieldKind.weekdays)) {
    final days = values['working_days'];
    if (days is! List || days.isEmpty) return 'Choose at least one working day';
  }
  if (values.containsKey('lat') && values['lat'] == 0 && values['lng'] == 0) {
    return 'Latitude and longitude 0, 0 is not a real location';
  }
  if (values.containsKey('start_time') &&
      values['start_time'] != null &&
      values['start_time'] == values['end_time']) {
    return 'Start and end time must differ';
  }
  return null;
}

/// Builds the request body from raw form [text] values and [others] (bools,
/// weekday lists). Empty nullable fields become null.
Map<String, Object?> buildMasterBody(
  MasterSpec spec,
  Map<String, String> text,
  Map<String, Object?> others,
) {
  final body = <String, Object?>{};
  for (final f in spec.fields) {
    switch (f.kind) {
      case FieldKind.boolean:
      case FieldKind.weekdays:
        body[f.key] = others[f.key];
      case FieldKind.integer:
        final s = (text[f.key] ?? '').trim();
        body[f.key] = s.isEmpty ? null : int.parse(s);
      case FieldKind.decimal:
        final s = (text[f.key] ?? '').trim();
        body[f.key] = s.isEmpty ? null : double.parse(s);
      case FieldKind.text:
      case FieldKind.time:
      case FieldKind.date:
        final s = (text[f.key] ?? '').trim();
        body[f.key] = s.isEmpty && f.nullable ? null : s;
    }
  }
  return body;
}

// ---- settings ------------------------------------------------------------------

enum SettingKind { time, integer, decimal, boolean }

class SettingSpec {
  const SettingSpec(this.key, this.label, this.kind, {this.min, this.max, this.hint});
  final String key;
  final String label;
  final SettingKind kind;
  final num? min;
  final num? max;
  final String? hint;

  String get group {
    final p = key.split('.').first;
    return switch (p) {
      'office' => 'Office location',
      'attendance' => 'Attendance',
      'food' => 'Food',
      'auth' => 'Security',
      'permission' => 'Permission requests',
      'leave' => 'Leave',
      _ => 'Other',
    };
  }
}

/// Mirrors the ranges in api-contract.md section 9 (server is authoritative).
const List<SettingSpec> settingSpecs = [
  SettingSpec('office.lat', 'Office latitude', SettingKind.decimal, min: -90, max: 90),
  SettingSpec('office.lng', 'Office longitude', SettingKind.decimal, min: -180, max: 180),
  SettingSpec('office.radius_m', 'Office radius (m)', SettingKind.integer, min: 10, max: 2000),
  SettingSpec('attendance.max_accuracy_m', 'Max GPS accuracy (m)', SettingKind.integer, min: 5, max: 500),
  SettingSpec('attendance.half_day_minutes', 'Half day minutes', SettingKind.integer, min: 30, max: 720),
  SettingSpec('attendance.max_fix_age_s', 'Max location age (s)', SettingKind.integer, min: 5, max: 300),
  SettingSpec('attendance.max_fix_future_s', 'Max location clock skew (s)', SettingKind.integer, min: 0, max: 120),
  SettingSpec('attendance.selfie_max_age_s', 'Max selfie age (s)', SettingKind.integer, min: 30, max: 3600),
  SettingSpec('attendance.location_retention_days', 'Location retention (days)', SettingKind.integer, min: 1, max: 3650),
  SettingSpec('attendance.selfie_retention_days', 'Selfie retention (days)', SettingKind.integer, min: 1, max: 3650),
  SettingSpec('attendance.selfie_required', 'Selfie required', SettingKind.boolean),
  SettingSpec('attendance.checkout_geofence', 'Geofence on check-out', SettingKind.boolean),
  SettingSpec('attendance.reject_mock_location', 'Reject mock locations', SettingKind.boolean),
  SettingSpec('food.cutoff_time', 'Food cut-off time', SettingKind.time, hint: 'HH:MM, office time'),
  SettingSpec('food.max_advance_days', 'Food: days ahead', SettingKind.integer, min: 1, max: 60),
  SettingSpec('auth.inactivity_timeout_minutes', 'Inactivity timeout (minutes)', SettingKind.integer, min: 1, max: 30),
  SettingSpec('permission.monthly_count_limit', 'Permissions per month', SettingKind.integer, min: 0, max: 100),
  SettingSpec('permission.monthly_minutes_limit', 'Permission minutes per month', SettingKind.integer, min: 0, max: 10080),
  SettingSpec('permission.max_minutes_per_request', 'Max minutes per request', SettingKind.integer, min: 1, max: 1440),
  SettingSpec('permission.past_grace_minutes', 'Past grace (minutes)', SettingKind.integer, min: 0, max: 1440),
  SettingSpec('leave.backdate_days', 'Leave back-date days', SettingKind.integer, min: 0, max: 365),
  SettingSpec('leave.max_advance_days', 'Leave advance days (0 = no limit)', SettingKind.integer, min: 0, max: 3650),
  SettingSpec('leave.reason_retention_days', 'Reason retention days (0 = keep)', SettingKind.integer, min: 0, max: 3650),
  SettingSpec('leave.allow_cancel_approved', 'Allow cancelling approved leave', SettingKind.boolean),
];

String? validateSetting(SettingSpec s, String? raw) {
  final v = raw?.trim() ?? '';
  switch (s.kind) {
    case SettingKind.boolean:
      return null;
    case SettingKind.time:
      return isValidTime(v) ? null : 'Use HH:MM (24 hour)';
    case SettingKind.integer:
      final n = int.tryParse(v);
      if (n == null) return 'Enter a whole number';
      if (n < s.min! || n > s.max!) return 'Must be between ${s.min} and ${s.max}';
      return null;
    case SettingKind.decimal:
      final n = double.tryParse(v);
      if (n == null || n.isNaN || n.isInfinite) return 'Enter a number';
      if (n < s.min! || n > s.max!) return 'Must be between ${s.min} and ${s.max}';
      return null;
  }
}

/// Parses a validated raw string into the JSON value to send.
Object parseSettingValue(SettingSpec s, String raw) {
  final v = raw.trim();
  return switch (s.kind) {
    SettingKind.integer => int.parse(v),
    SettingKind.decimal => double.parse(v),
    SettingKind.time => v,
    SettingKind.boolean => v == 'true',
  };
}

/// Text shown for a stored value in the form.
String settingText(Object? v) {
  if (v == null) return '';
  if (v is double && v == v.truncateToDouble() && v.abs() < 1e12) {
    return v.toInt().toString();
  }
  return '$v';
}

/// Keys whose value differs from the loaded one (only these are sent: PUT merges).
Map<String, Object?> changedSettings(
  Map<String, Object?> original,
  Map<String, Object?> edited,
) {
  final out = <String, Object?>{};
  edited.forEach((k, v) {
    final o = original[k];
    final same = o == v || (o is num && v is num && o.toDouble() == v.toDouble());
    if (!same) out[k] = v;
  });
  return out;
}

// ---- employee form validation --------------------------------------------------

String? validateNewEmployeeId(String? v) {
  final s = v?.trim() ?? '';
  if (s.isEmpty) return 'Enter an employee ID';
  if (s.length > 32) return 'Employee ID is too long';
  if (!RegExp(r'^[A-Za-z0-9._-]+$').hasMatch(s)) {
    return 'Use letters, digits, dot, dash or underscore';
  }
  return null;
}

String? validateFullName(String? v) {
  final s = v?.trim() ?? '';
  if (s.isEmpty) return 'Enter the full name';
  if (s.length > 100) return 'Name can be at most 100 characters';
  return null;
}

String? validateOfficialEmail(String? v) {
  final s = v?.trim() ?? '';
  if (s.isEmpty) return 'Enter the official email';
  if (s.length > 254 || !RegExp(r'^[^@\s]+@[^@\s]+\.[^@\s]+$').hasMatch(s)) {
    return 'Enter a valid email address';
  }
  return null;
}

String? validateJoinDate(String? v) => parseYmd(v?.trim()) == null ? 'Pick the join date' : null;

/// Request body for create (when [create]) or update. On update the org
/// fields are a full replace; personal fields go in [personal] (a
/// [profilePatch]): omitted keys keep their value, null clears (so `phone` is
/// never sent on update unless it is in [personal]).
Map<String, Object?> employeeBody({
  required bool create,
  String? employeeId,
  required String fullName,
  required String email,
  String phone = '',
  required String departmentId,
  required String shiftId,
  required String roleId,
  String? managerId,
  required String joinDate,
  Map<String, Object?> personal = const {},
}) {
  final p = normalizePhone(phone.trim());
  return {
    if (create) 'employee_id': employeeId?.trim(),
    'full_name': fullName.trim(),
    'official_email': email.trim(),
    if (create) 'phone': p.isEmpty ? null : p,
    if (!create) ...personal,
    'department_id': departmentId,
    'shift_id': shiftId,
    'role_id': roleId,
    'manager_id': managerId,
    'join_date': joinDate,
  };
}

// ---- privilege helpers (client side hints; the server enforces) -------------------

/// A role is "elevated" when it holds `role.assign` or `role.manage`: creating
/// or assigning it needs the actor's current password (review M1). A role whose
/// permissions are unknown is treated as elevated when it is not `employee`.
bool isElevatedRole(RoleItem? role) {
  if (role == null) return false;
  final codes = role.permissionCodes;
  if (codes == null) return role.name.toLowerCase() != 'employee';
  return codes.contains(AdminPerm.roleAssign) || codes.contains(AdminPerm.roleManage);
}

/// Saving an employee needs the actor's password when the NEW role is elevated
/// or the account is ALREADY elevated (editing, demoting or re-saving it).
bool employeeSaveNeedsReauth({RoleItem? selected, RoleItem? existing}) =>
    isElevatedRole(selected) || isElevatedRole(existing);

/// Whether [role] holds any permission at all (anything above self-service).
bool roleHoldsPermissions(RoleItem role) {
  final codes = role.permissionCodes;
  if (codes == null) return role.name.toLowerCase() != 'employee';
  return codes.isNotEmpty;
}

/// Roles the actor may offer in the employee form: without `role.assign` only
/// roles with no permissions; always only roles whose permissions the actor
/// holds. Unknown permission lists are offered only with `role.assign`.
List<RoleItem> assignableRoles(List<RoleItem> roles, List<String> actorPermissions) {
  final canAssign = actorPermissions.contains(AdminPerm.roleAssign);
  return [
    for (final r in roles)
      if (canAssign
          ? (r.permissionCodes == null || actorPermissions.toSet().containsAll(r.permissionCodes!))
          : !roleHoldsPermissions(r))
        r,
  ];
}

/// Whether the target employee's role holds permissions (so activate,
/// deactivate and unlock need `role.assign`, review H1). [roles] may be null
/// when the actor cannot list roles; then the role name decides.
bool targetHoldsPermissions(Employee target, List<RoleItem>? roles) {
  final ref = target.role;
  if (ref == null) return false;
  if (roles != null) {
    for (final r in roles) {
      if (r.id == ref.id) return roleHoldsPermissions(r);
    }
  }
  return ref.name.toLowerCase() != 'employee';
}

// ---- audit ---------------------------------------------------------------------

class AuditQuery {
  const AuditQuery({
    this.actor = '',
    this.action = '',
    this.entityType = '',
    this.entityId = '',
    this.from,
    this.to,
  });
  final String actor;
  final String action;
  final String entityType;
  final String entityId;
  final String? from;
  final String? to;

  bool get isEmpty =>
      actor.isEmpty &&
      action.isEmpty &&
      entityType.isEmpty &&
      entityId.isEmpty &&
      from == null &&
      to == null;

  Map<String, Object?> toQuery() => {
        if (actor.isNotEmpty) 'actor': actor,
        if (action.isNotEmpty) 'action': action,
        if (entityType.isNotEmpty) 'entity_type': entityType,
        if (entityId.isNotEmpty) 'entity_id': entityId,
        if (from != null) 'from': from,
        if (to != null) 'to': to,
      };

  @override
  bool operator ==(Object other) =>
      other is AuditQuery &&
      other.actor == actor &&
      other.action == action &&
      other.entityType == entityType &&
      other.entityId == entityId &&
      other.from == from &&
      other.to == to;

  @override
  int get hashCode => Object.hash(actor, action, entityType, entityId, from, to);
}

/// `from` must not be after `to`.
String? validateAuditRange(String? from, String? to) {
  final f = parseYmd(from);
  final t = parseYmd(to);
  if (f != null && t != null && f.isAfter(t)) return 'From date must not be after To date';
  return null;
}

class EmployeeQuery {
  const EmployeeQuery({this.q = '', this.departmentId, this.status});
  final String q;
  final String? departmentId;

  /// `active` | `inactive` | null (all).
  final String? status;

  Map<String, Object?> toQuery() => {
        if (q.trim().isNotEmpty) 'q': q.trim(),
        if (departmentId != null) 'department_id': departmentId,
        if (status != null) 'status': status,
      };

  @override
  bool operator ==(Object other) =>
      other is EmployeeQuery &&
      other.q == q &&
      other.departmentId == departmentId &&
      other.status == status;

  @override
  int get hashCode => Object.hash(q, departmentId, status);
}

/// Appends [next] to [current] without duplicate ids.
List<T> mergeById<T>(List<T> current, List<T> next, String Function(T) idOf) {
  final seen = {for (final c in current) idOf(c)};
  return [
    ...current,
    for (final n in next)
      if (seen.add(idOf(n))) n,
  ];
}

// ---- errors --------------------------------------------------------------------

String adminErrorMessage(ApiException e) {
  switch (e.code) {
    case 'FORBIDDEN':
      // The server names the exact rule (admin role, system role rename or
      // delete, permission you do not hold, your own role, role.assign).
      return e.message.isNotEmpty && e.message != 'Something went wrong.'
          ? e.message
          : 'You are not allowed to do this. The Admin role cannot be changed, system '
              'roles cannot be renamed or deleted, and you can only grant permissions '
              'you hold yourself.';
    case 'NOT_FOUND':
      return 'This record no longer exists.';
    case 'REAUTH_FAILED':
      return 'Your password was not correct.';
    case 'TEMP_PASSWORD_EXPIRED':
      return 'This temporary password has expired. Ask HR or Admin for a new reset.';
    case 'IN_USE':
      return 'It is still in use. Deactivate it instead of deleting.';
    case 'LAST_ADMIN':
      return 'This is the last active admin, so it cannot be changed this way.';
    case 'EMPLOYEE_ID_EXISTS':
      return 'That employee ID is already taken.';
    case 'EMAIL_EXISTS':
      return 'That email is already used by another employee.';
    case 'QUOTA_BELOW_USED':
      return 'A quota cannot be lower than the days already used or pending.';
    case 'USER_LIMIT_REACHED':
      return 'The license seat limit (${e.detail('max_users') ?? '-'}) is reached. '
          'Deactivate someone or install a larger license.';
    case 'LICENSE_EXPIRED':
      return 'The license has expired. Install a new license.';
    case 'LICENSE_INVALID':
      return 'This license key is not valid for this installation.';
    case 'LICENSE_OUTDATED':
      return 'A newer license is already installed.';
    case 'FILE_TYPE_NOT_ALLOWED':
      return 'Only PNG or JPEG images are allowed.';
    case 'FILE_TOO_LARGE':
      return 'The image is too large (2 MB at most).';
    case 'CONFLICT':
      return e.message.isEmpty ? 'This record changed or already exists.' : e.message;
    case 'RATE_LIMITED':
      final s = e.retryAfter?.inSeconds;
      return s == null
          ? 'Too many attempts. Please wait a moment and try again.'
          : 'Too many attempts. Please try again in $s seconds.';
    case 'VALIDATION_ERROR':
      final msgs = e.details.map((d) => d.field.isEmpty ? d.message : '${d.field}: ${d.message}')
          .where((m) => m.isNotEmpty);
      return msgs.isEmpty ? 'Please check the details and try again.' : msgs.join('\n');
    default:
      return e.message;
  }
}


// ---- manual attendance ---------------------------------------------------------

/// `GET/PUT /admin/attendance/{user_id}/{work_date}`: the employee and that
/// day's record (null when there is none yet).
class ManualAttendanceDay {
  const ManualAttendanceDay({required this.workDate, this.user, this.record});
  final String workDate;
  final EmployeeRef? user;
  final AttendanceRecord? record;

  factory ManualAttendanceDay.fromJson(Map<String, dynamic> j) {
    final r = j['record'];
    return ManualAttendanceDay(
      workDate: _s(j['work_date']),
      user: EmployeeRef.parse(j['user']),
      record: r is Map<String, dynamic> ? AttendanceRecord.fromJson(r) : null,
    );
  }
}

/// First problem with the manual attendance form, or null.
String? validateManualAttendance({
  required DateTime? date,
  required String? checkIn,
  required String? checkOut,
  required String reason,
  required DateTime now,
}) {
  if (date == null) return 'Choose the date';
  final today = DateTime(now.year, now.month, now.day);
  if (DateTime(date.year, date.month, date.day).isAfter(today)) return 'Attendance cannot be entered for a future date';
  if (checkIn == null || !isValidTime(checkIn)) return 'Choose the check-in time';
  if (checkOut != null && !isValidTime(checkOut)) return 'Choose a valid check-out time';
  if (reason.trim().length < 3) return 'Give a reason (at least 3 characters)';
  if (reason.trim().length > 300) return 'Keep the reason under 300 characters';
  return null;
}
