import '../../../core/utils/dates.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import 'on_duty_models.dart';

/// Admin side of On Duty (contract 14.5 / 14.6). Nothing here is cached.

String? _opt(Object? v) => v is String && v.isNotEmpty ? v : null;

abstract final class DutyPerm {
  static const manage = 'duty_location.manage';
  static const attendanceViewAll = 'attendance.view_all';
  static const attendanceViewTeam = 'attendance.view_team';
}

bool canManageDutyLocations(List<String> permissions) => permissions.contains(DutyPerm.manage);

/// Visits list: all employees or direct reports.
bool canViewDutyVisits(List<String> permissions) =>
    permissions.contains(DutyPerm.attendanceViewAll) ||
    permissions.contains(DutyPerm.attendanceViewTeam);

/// Distances and the suspicious flag are for `attendance.view_all` only.
bool canSeeVisitDetail(List<String> permissions) =>
    permissions.contains(DutyPerm.attendanceViewAll);

class DutyLocationAdmin {
  const DutyLocationAdmin({
    required this.id,
    required this.name,
    required this.lat,
    required this.lng,
    required this.radiusM,
    this.address,
    this.assignMode = 'selected',
    this.isActive = true,
    this.assignedCount = 0,
    this.inUse = false,
  });
  final String id;
  final String name;
  final String? address;
  final double lat;
  final double lng;
  final int radiusM;

  /// `all` | `selected`.
  final String assignMode;
  final bool isActive;
  final int assignedCount;
  final bool inUse;

  bool get assignsAll => assignMode == 'all';

  String get assignText =>
      assignsAll ? 'All employees ($assignedCount)' : 'Selected employees ($assignedCount)';

  factory DutyLocationAdmin.fromJson(Map<String, dynamic> j) => DutyLocationAdmin(
        id: '${j['id'] ?? ''}',
        name: '${j['name'] ?? ''}',
        address: _opt(j['address']),
        lat: (j['lat'] as num?)?.toDouble() ?? 0,
        lng: (j['lng'] as num?)?.toDouble() ?? 0,
        radiusM: (j['radius_m'] as num?)?.toInt() ?? 0,
        assignMode: j['assign_mode'] == 'all' ? 'all' : 'selected',
        isActive: j['is_active'] != false,
        assignedCount: (j['assigned_count'] as num?)?.toInt() ?? 0,
        inUse: j['in_use'] == true,
      );
}

/// Query of the locations list.
class DutyLocationQuery {
  const DutyLocationQuery({this.q = '', this.isActive});
  final String q;

  /// null = all.
  final bool? isActive;

  Map<String, Object?> toQuery() => {
        if (q.trim().isNotEmpty) 'q': q.trim(),
        if (isActive != null) 'is_active': isActive,
      };

  @override
  bool operator ==(Object other) =>
      other is DutyLocationQuery && other.q == q && other.isActive == isActive;

  @override
  int get hashCode => Object.hash(q, isActive);
}

// ---- form validation ------------------------------------------------------------

String? validateDutyName(String? v) {
  final t = (v ?? '').trim();
  if (t.length < 2) return 'Enter at least 2 characters';
  if (t.length > 80) return 'At most 80 characters';
  return null;
}

String? validateDutyAddress(String? v) =>
    (v ?? '').trim().length > 300 ? 'At most 300 characters' : null;

String? validateLatitude(String? v) {
  final n = double.tryParse((v ?? '').trim());
  if (n == null || !n.isFinite) return 'Enter a number';
  if (n < -90 || n > 90) return 'Must be between -90 and 90';
  return null;
}

String? validateLongitude(String? v) {
  final n = double.tryParse((v ?? '').trim());
  if (n == null || !n.isFinite) return 'Enter a number';
  if (n < -180 || n > 180) return 'Must be between -180 and 180';
  return null;
}

String? validateDutyRadius(String? v) {
  final n = int.tryParse((v ?? '').trim());
  if (n == null) return 'Enter a whole number';
  if (n < 10 || n > 2000) return 'Must be between 10 and 2000';
  return null;
}

/// Request body of POST / PUT `/admin/duty-locations` (call after validating).
Map<String, Object?> dutyLocationBody({
  required String name,
  required String address,
  required String lat,
  required String lng,
  required String radius,
  required String assignMode,
  required bool isActive,
}) {
  final a = address.trim();
  return {
    'name': name.trim(),
    'address': a.isEmpty ? null : a,
    'lat': roundCoord(double.parse(lat.trim())),
    'lng': roundCoord(double.parse(lng.trim())),
    'radius_m': int.parse(radius.trim()),
    'assign_mode': assignMode == 'all' ? 'all' : 'selected',
    'is_active': isActive,
  };
}

// ---- assignments ----------------------------------------------------------------

const int maxAssignedUsers = 5000;

class DutyAssignments {
  const DutyAssignments({
    required this.locationId,
    required this.assignMode,
    required this.users,
  });
  final String locationId;
  final String assignMode;
  final List<EmployeeRef> users;

  factory DutyAssignments.fromJson(Map<String, dynamic> j) {
    final raw = j['items'];
    return DutyAssignments(
      locationId: '${j['location_id'] ?? ''}',
      assignMode: j['assign_mode'] == 'all' ? 'all' : 'selected',
      users: [
        if (raw is List)
          for (final e in raw)
            if (e is Map && EmployeeRef.parse(e['user']) != null) EmployeeRef.parse(e['user'])!,
      ],
    );
  }
}

/// Editable state of the assignment screen: the mode, the chosen employees and
/// the departments whose CURRENT members the server adds at save time.
class AssignmentSelection {
  const AssignmentSelection({
    this.mode = 'selected',
    this.users = const [],
    this.departmentIds = const [],
  });
  final String mode;
  final List<EmployeeRef> users;
  final List<String> departmentIds;

  bool get isAll => mode == 'all';
  int get count => users.length;
  bool get overLimit => users.length > maxAssignedUsers;

  bool contains(String userId) => users.any((u) => u.id == userId);

  AssignmentSelection withMode(String m) =>
      AssignmentSelection(mode: m == 'all' ? 'all' : 'selected', users: users, departmentIds: departmentIds);

  AssignmentSelection toggle(EmployeeRef u) => contains(u.id)
      ? remove(u.id)
      : AssignmentSelection(mode: mode, users: [...users, u], departmentIds: departmentIds);

  AssignmentSelection remove(String userId) => AssignmentSelection(
        mode: mode,
        users: [for (final u in users) if (u.id != userId) u],
        departmentIds: departmentIds,
      );

  AssignmentSelection addAll(Iterable<EmployeeRef> more) {
    final seen = {for (final u in users) u.id};
    return AssignmentSelection(
      mode: mode,
      users: [...users, for (final u in more) if (seen.add(u.id)) u],
      departmentIds: departmentIds,
    );
  }

  AssignmentSelection clear() => AssignmentSelection(mode: mode, departmentIds: departmentIds);

  AssignmentSelection toggleDepartment(String id) => AssignmentSelection(
        mode: mode,
        users: users,
        departmentIds: departmentIds.contains(id)
            ? [for (final d in departmentIds) if (d != id) d]
            : [...departmentIds, id],
      );

  /// Body of `PUT /admin/duty-locations/{id}/assignments`. `user_ids` is always
  /// sent (the server keeps the list while the mode is `all`).
  Map<String, Object?> toBody() => {
        'assign_mode': mode,
        'user_ids': [for (final u in users) u.id],
        if (departmentIds.isNotEmpty) 'add_department_ids': departmentIds,
      };

  /// Why the selection cannot be saved, or null.
  String? get problem {
    if (overLimit) return 'At most $maxAssignedUsers employees can be assigned.';
    if (departmentIds.length > 50) return 'Pick at most 50 departments.';
    return null;
  }

  /// Local search over the selected employees.
  List<EmployeeRef> filtered(String q) {
    final t = q.trim().toLowerCase();
    if (t.isEmpty) return users;
    return [
      for (final u in users)
        if (u.fullName.toLowerCase().contains(t) || u.employeeId.toLowerCase().contains(t)) u,
    ];
  }
}

// ---- visits ---------------------------------------------------------------------

class AdminVisit {
  const AdminVisit({
    required this.visit,
    this.user,
    this.checkInDistanceM,
    this.checkOutDistanceM,
    this.checkOutWithinFence,
    this.suspicious = false,
    this.hasDetail = false,
  });
  final DutyVisit visit;
  final EmployeeRef? user;
  final int? checkInDistanceM;
  final int? checkOutDistanceM;

  /// null for managers and when no check-out location was measured.
  final bool? checkOutWithinFence;
  final bool suspicious;

  /// The view_all-only fields were present (they are null for managers).
  final bool hasDetail;

  String get id => visit.id;

  factory AdminVisit.fromJson(Map<String, dynamic> j) {
    final fence = j['check_out_within_fence'];
    final hasDetail = j.containsKey('check_in_distance_m') &&
        (j['check_in_distance_m'] != null || j['check_out_distance_m'] != null || fence != null);
    return AdminVisit(
      visit: DutyVisit.fromJson(j),
      user: EmployeeRef.parse(j['user']),
      checkInDistanceM: (j['check_in_distance_m'] as num?)?.toInt(),
      checkOutDistanceM: (j['check_out_distance_m'] as num?)?.toInt(),
      checkOutWithinFence: fence is bool ? fence : null,
      suspicious: j['suspicious'] == true,
      hasDetail: hasDetail,
    );
  }
}

/// Query of the admin visits list.
class VisitQuery {
  const VisitQuery({this.date, this.locationId, this.employeeId = '', this.openOnly = false});

  /// `YYYY-MM-DD`; null = today on the server.
  final String? date;
  final String? locationId;
  final String employeeId;
  final bool openOnly;

  Map<String, Object?> toQuery() => {
        if (date != null) 'date': date,
        if (locationId != null) 'location_id': locationId,
        if (employeeId.trim().isNotEmpty) 'employee_id': employeeId.trim(),
        if (openOnly) 'open': true,
      };

  @override
  bool operator ==(Object other) =>
      other is VisitQuery &&
      other.date == date &&
      other.locationId == locationId &&
      other.employeeId == employeeId &&
      other.openOnly == openOnly;

  @override
  int get hashCode => Object.hash(date, locationId, employeeId, openOnly);
}

/// One line of an admin visit card.
String adminVisitSubtitle(AdminVisit v) {
  final d = parseYmd(v.visit.workDate);
  final day = d == null ? v.visit.workDate : formatDayLong(d);
  return '$day  ${v.visit.timeText}';
}

/// `GET /admin/employees/{id}/duty-locations` item.
class EmployeeDutyLocation {
  const EmployeeDutyLocation({
    required this.id,
    required this.name,
    this.isActive = true,
    this.assignMode = 'selected',
  });
  final String id;
  final String name;
  final bool isActive;
  final String assignMode;

  factory EmployeeDutyLocation.fromJson(Map<String, dynamic> j) => EmployeeDutyLocation(
        id: '${j['id'] ?? ''}',
        name: '${j['name'] ?? ''}',
        isActive: j['is_active'] != false,
        assignMode: j['assign_mode'] == 'all' ? 'all' : 'selected',
      );
}
