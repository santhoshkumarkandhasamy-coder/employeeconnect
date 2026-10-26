import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/admin/domain/admin_models.dart' show EmployeeRef;
import 'package:employee_connect/features/attendance/data/location_service.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart';
import 'package:employee_connect/features/leave/domain/leave_models.dart';
import 'package:employee_connect/features/on_duty/data/maps_launcher.dart';
import 'package:employee_connect/features/on_duty/data/on_duty_repository.dart';
import 'package:employee_connect/features/on_duty/domain/duty_admin_models.dart';
import 'package:employee_connect/features/on_duty/domain/on_duty_config.dart';
import 'package:employee_connect/features/on_duty/domain/on_duty_models.dart';

import 'attendance_fakes.dart';

const onDutyOn = OnDutyConfig(enabled: true, hasLocations: true);

const acme = DutyLocation(id: 'l1', name: 'Acme Client - Guindy', lat: 12.9716, lng: 77.5946);
const branch = DutyLocation(id: 'l2', name: 'Branch - Tambaram', lat: 13.0500, lng: 77.7000);

/// Counts one-shot reads; a stream API simply does not exist on [LocationService].
class CountingLocation extends FakeLocation {
  CountingLocation({super.state, super.afterRequest, super.fix});
  int reads = 0;

  @override
  Future<LocationFix> current() async {
    reads++;
    return super.current();
  }
}

class FakeMaps implements MapsLauncher {
  final List<DutyLocation> opened = [];
  @override
  Future<bool> open(DutyLocation site) async {
    opened.add(site);
    return true;
  }
}

class FakeOnDutyRepo extends OnDutyRepository {
  FakeOnDutyRepo() : super(Dio());

  List<DutyLocation> sites = [acme, branch];
  ApiException? sitesError;
  OnDutyToday todayData = const OnDutyToday(workDate: '2026-10-26', canCheckIn: true, visitsRemaining: 10);
  ApiException? todayError;
  ApiException? checkInError;
  ApiException? checkOutError;
  ApiException? applyError;

  final List<({String locationId, LocationFix fix, String? note, String? selfie})> checkIns = [];
  final List<({String? visitId, String? note})> checkOuts = [];
  final List<({DateTime from, DateTime to, String? locationId, String purpose})> applied = [];
  int todayCalls = 0;

  @override
  Future<List<DutyLocation>> locations() async {
    final e = sitesError;
    if (e != null) throw e;
    return sites;
  }

  @override
  Future<OnDutyToday> today() async {
    todayCalls++;
    final e = todayError;
    if (e != null) throw e;
    return todayData;
  }

  DutyVisit visit(String id, {bool open = true, DateTime? out}) => DutyVisit(
        id: id,
        workDate: '2026-10-26',
        locationId: 'l1',
        locationName: acme.name,
        checkInAt: DateTime(2026, 10, 26, 10, 12),
        checkOutAt: out,
        open: open,
        visitMinutes: out == null ? null : 95,
      );

  @override
  Future<VisitResult> checkIn({
    required String locationId,
    required LocationFix fix,
    String? selfieFileId,
    String? note,
  }) async {
    checkIns.add((locationId: locationId, fix: fix, note: note, selfie: selfieFileId));
    final e = checkInError;
    if (e != null) throw e;
    final v = visit('v1');
    todayData = OnDutyToday(
      workDate: '2026-10-26',
      openVisit: v,
      visits: [v],
      visitsRemaining: 9,
    );
    return VisitResult(visit: v);
  }

  @override
  Future<VisitResult> checkOut({
    required LocationFix fix,
    String? visitId,
    String? selfieFileId,
    String? note,
  }) async {
    checkOuts.add((visitId: visitId, note: note));
    final e = checkOutError;
    if (e != null) throw e;
    final v = visit('v1', open: false, out: DateTime(2026, 10, 26, 11, 47));
    todayData = OnDutyToday(
      workDate: '2026-10-26',
      visits: [v],
      visitsRemaining: 9,
      canCheckIn: true,
    );
    return VisitResult(visit: v);
  }

  @override
  Future<String> uploadSelfie(String path) async => 'file-1';

  @override
  Future<RequestItem> applyRequest({
    required DateTime from,
    required DateTime to,
    required String? locationId,
    required String purpose,
  }) async {
    applied.add((from: from, to: to, locationId: locationId, purpose: purpose));
    final e = applyError;
    if (e != null) throw e;
    return const RequestItem(kind: RequestKind.onDuty, id: 'r1', status: RequestStatus.pending);
  }

  // ---- admin ----
  List<DutyLocationAdmin> adminSites = [];
  ApiException? adminSitesError;
  final List<(String?, Map<String, Object?>)> saved = [];
  ApiException? saveError;
  final List<String> deleted = [];
  ApiException? deleteError;
  DutyAssignments assignmentData = const DutyAssignments(locationId: 'a1', assignMode: 'selected', users: []);
  final List<AssignmentSelection> savedAssignments = [];
  ApiException? assignError;
  List<EmployeeRef> employeesFound = const [];
  List<AdminVisit> visitData = [];
  ApiException? visitsError;
  final List<VisitQuery> visitQueries = [];
  List<EmployeeDutyLocation> employeeSites = const [];

  @override
  Future<Paged<DutyLocationAdmin>> adminLocations(DutyLocationQuery q, {int page = 1}) async {
    final e = adminSitesError;
    if (e != null) throw e;
    return Paged(items: adminSites, page: 1, total: adminSites.length, pageSize: 50);
  }

  @override
  Future<DutyLocationAdmin> saveLocation(String? id, Map<String, Object?> body) async {
    saved.add((id, body));
    final e = saveError;
    if (e != null) throw e;
    return DutyLocationAdmin.fromJson({'id': id ?? 'new', ...body});
  }

  @override
  Future<void> deleteLocation(String id) async {
    final e = deleteError;
    if (e != null) throw e;
    deleted.add(id);
  }

  @override
  Future<DutyAssignments> assignments(String id) async => assignmentData;

  @override
  Future<DutyAssignments> saveAssignments(String id, AssignmentSelection sel) async {
    savedAssignments.add(sel);
    final e = assignError;
    if (e != null) throw e;
    return assignmentData;
  }

  @override
  Future<List<EmployeeRef>> searchEmployees(String q) async => employeesFound;

  @override
  Future<Paged<AdminVisit>> adminVisits(VisitQuery q, {int page = 1}) async {
    visitQueries.add(q);
    final e = visitsError;
    if (e != null) throw e;
    return Paged(items: visitData, page: 1, total: visitData.length, pageSize: 30);
  }

  @override
  Future<List<EmployeeDutyLocation>> employeeLocations(String userId) async => employeeSites;
}

AttendanceConfig configWith(OnDutyConfig c) => AttendanceConfig(
      officeLat: 12.97,
      officeLng: 77.59,
      radiusM: 150,
      onDuty: c,
    );
