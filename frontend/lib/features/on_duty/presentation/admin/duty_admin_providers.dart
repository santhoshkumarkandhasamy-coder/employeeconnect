import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../admin/domain/admin_models.dart' show EmployeeRef;
import '../../../admin/presentation/admin_providers.dart' show AdminPagedNotifier;
import '../../../announcements/presentation/announcement_providers.dart' show PagedList;
import '../../../attendance/domain/attendance_models.dart' show Paged;
import '../../data/on_duty_repository.dart';
import '../../domain/duty_admin_models.dart';

class DutyLocationsNotifier extends AdminPagedNotifier<DutyLocationAdmin, DutyLocationQuery> {
  @override
  Future<Paged<DutyLocationAdmin>> fetchPage(DutyLocationQuery arg, int page) =>
      ref.read(onDutyRepositoryProvider).adminLocations(arg, page: page);

  @override
  String idOf(DutyLocationAdmin item) => item.id;
}

final dutyLocationsAdminProvider = AsyncNotifierProvider.autoDispose
    .family<DutyLocationsNotifier, PagedList<DutyLocationAdmin>, DutyLocationQuery>(
  DutyLocationsNotifier.new,
);

class AdminVisitsNotifier extends AdminPagedNotifier<AdminVisit, VisitQuery> {
  @override
  Future<Paged<AdminVisit>> fetchPage(VisitQuery arg, int page) =>
      ref.read(onDutyRepositoryProvider).adminVisits(arg, page: page);

  @override
  String idOf(AdminVisit item) => item.id;
}

final adminVisitsProvider = AsyncNotifierProvider.autoDispose
    .family<AdminVisitsNotifier, PagedList<AdminVisit>, VisitQuery>(AdminVisitsNotifier.new);

final dutyAssignmentsProvider = FutureProvider.autoDispose.family<DutyAssignments, String>(
  (ref, id) => ref.watch(onDutyRepositoryProvider).assignments(id),
);

/// Employee search for the picker (`employee.view`). Family argument = text.
final dutyEmployeeSearchProvider = FutureProvider.autoDispose.family<List<EmployeeRef>, String>(
  (ref, q) => ref.watch(onDutyRepositoryProvider).searchEmployees(q),
);

final employeeDutyLocationsProvider =
    FutureProvider.autoDispose.family<List<EmployeeDutyLocation>, String>(
  (ref, userId) => ref.watch(onDutyRepositoryProvider).employeeLocations(userId),
);

/// All sites for the visits filter (first 100; needs `duty_location.manage`).
final dutyLocationOptionsProvider = FutureProvider.autoDispose<List<DutyLocationAdmin>>((ref) async {
  final p = await ref.watch(onDutyRepositoryProvider).adminLocations(const DutyLocationQuery());
  return p.items;
});

/// Reads the device position once for the location form. Mobile only: the
/// console passes no reader, so it needs no location plugin.
class PositionReadException implements Exception {
  const PositionReadException(this.message);
  final String message;
  @override
  String toString() => message;
}

typedef CurrentPositionReader = Future<({double lat, double lng})> Function();
