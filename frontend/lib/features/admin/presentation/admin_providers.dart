import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../announcements/presentation/announcement_providers.dart' show PagedList;
import '../../attendance/domain/attendance_models.dart' show Paged;
import '../data/admin_repository.dart';
import '../domain/admin_models.dart';
import '../domain/ops_models.dart';

String adminMsg(Object e) =>
    e is ApiException ? adminErrorMessage(e) : 'Something went wrong. Please try again.';

/// Shared paging behaviour for the admin lists. State lives only while a
/// screen watches it (autoDispose) and is never written to disk.
abstract class AdminPagedNotifier<T, A> extends AutoDisposeFamilyAsyncNotifier<PagedList<T>, A> {
  Future<Paged<T>> fetchPage(A arg, int page);
  String idOf(T item);

  @override
  Future<PagedList<T>> build(A arg) async {
    final p = await fetchPage(arg, 1);
    return PagedList<T>(items: p.items, page: p.page, hasMore: p.hasMore);
  }

  Future<void> loadMore() async {
    final s = state.valueOrNull;
    if (s == null || s.loadingMore || !s.hasMore) return;
    state = AsyncData(s.copyWith(loadingMore: true));
    try {
      final p = await fetchPage(arg, s.page + 1);
      final cur = state.valueOrNull ?? s;
      state = AsyncData(cur.copyWith(
        items: mergeById<T>(cur.items, p.items, idOf),
        page: p.page,
        hasMore: p.hasMore,
        loadingMore: false,
      ),);
    } on Object catch (e) {
      final cur = state.valueOrNull ?? s;
      state = AsyncData(cur.copyWith(loadingMore: false, loadMoreError: adminMsg(e)));
    }
  }

  Future<void> reload() async {
    ref.invalidateSelf();
    await future.then((_) {}, onError: (Object _) {});
  }
}

class EmployeesNotifier extends AdminPagedNotifier<Employee, EmployeeQuery> {
  @override
  Future<Paged<Employee>> fetchPage(EmployeeQuery arg, int page) =>
      ref.read(adminRepositoryProvider).employees(arg, page: page);
  @override
  String idOf(Employee item) => item.id;
}

final employeesProvider = AsyncNotifierProvider.autoDispose
    .family<EmployeesNotifier, PagedList<Employee>, EmployeeQuery>(EmployeesNotifier.new);

class AuditNotifier extends AdminPagedNotifier<AuditEntry, AuditQuery> {
  @override
  Future<Paged<AuditEntry>> fetchPage(AuditQuery arg, int page) =>
      ref.read(adminRepositoryProvider).auditLogs(arg, page: page);
  @override
  String idOf(AuditEntry item) => item.id;
}

final auditLogsProvider = AsyncNotifierProvider.autoDispose
    .family<AuditNotifier, PagedList<AuditEntry>, AuditQuery>(AuditNotifier.new);

/// Family argument: status `open` | `handled` | `expired`.
class ResetRequestsNotifier extends AdminPagedNotifier<ResetRequest, String> {
  @override
  Future<Paged<ResetRequest>> fetchPage(String arg, int page) =>
      ref.read(adminRepositoryProvider).resetRequests(arg, page: page);
  @override
  String idOf(ResetRequest item) => item.id;
}

final resetRequestsProvider = AsyncNotifierProvider.autoDispose
    .family<ResetRequestsNotifier, PagedList<ResetRequest>, String>(ResetRequestsNotifier.new);

final employeeDetailProvider = FutureProvider.autoDispose.family<Employee, String>(
  (ref, id) => ref.watch(adminRepositoryProvider).employee(id),
);

final rolesProvider = FutureProvider.autoDispose<List<RoleItem>>(
  (ref) => ref.watch(adminRepositoryProvider).roles(),
);

final permissionCatalogueProvider = FutureProvider.autoDispose<List<PermissionDef>>(
  (ref) => ref.watch(adminRepositoryProvider).permissionCatalogue(),
);

final departmentRefsProvider = FutureProvider.autoDispose<List<NamedRef>>(
  (ref) => ref.watch(adminRepositoryProvider).refs('/admin/departments'),
);

final shiftRefsProvider = FutureProvider.autoDispose<List<NamedRef>>(
  (ref) => ref.watch(adminRepositoryProvider).refs('/admin/shifts'),
);

/// Master list. Family argument: (spec path, year or null) as a record so it
/// has value equality; the spec is looked up by path.
typedef MasterKey = (String path, int? year);

MasterSpec masterSpecFor(String path) => switch (path) {
      '/admin/departments' => departmentSpec,
      '/admin/shifts' => shiftSpec,
      '/holidays' => holidaySpec,
      _ => leaveTypeSpec,
    };

final masterListProvider =
    FutureProvider.autoDispose.family<List<MasterRecord>, MasterKey>((ref, key) {
  return ref.watch(adminRepositoryProvider).masterList(masterSpecFor(key.$1), year: key.$2);
});

final settingsProvider = FutureProvider.autoDispose<Map<String, Object?>>(
  (ref) => ref.watch(adminRepositoryProvider).settings(),
);

typedef BalanceKey = (String employeeId, int year);

final leaveBalancesProvider =
    FutureProvider.autoDispose.family<List<LeaveBalanceRow>, BalanceKey>(
  (ref, key) => ref.watch(adminRepositoryProvider).leaveBalances(key.$1, key.$2),
);

// ---- company, license, sessions, console data -----------------------------------

final companyAdminProvider = FutureProvider.autoDispose<CompanyAdmin>(
  (ref) => ref.watch(adminRepositoryProvider).company(),
);

final licenseProvider = FutureProvider.autoDispose<LicenseInfo>(
  (ref) => ref.watch(adminRepositoryProvider).license(),
);

/// Family argument: (user id or null for all, employee id filter or null,
/// client type or null).
typedef SessionKey = (String? userId, String? employeeId, String? clientType);

class SessionsNotifier extends AdminPagedNotifier<SessionItem, SessionKey> {
  @override
  Future<Paged<SessionItem>> fetchPage(SessionKey arg, int page) {
    final repo = ref.read(adminRepositoryProvider);
    return arg.$1 != null
        ? repo.employeeSessions(arg.$1!, page: page)
        : repo.sessions(employeeId: arg.$2, clientType: arg.$3, page: page);
  }

  @override
  String idOf(SessionItem item) => item.id;
}

final sessionsProvider = AsyncNotifierProvider.autoDispose
    .family<SessionsNotifier, PagedList<SessionItem>, SessionKey>(SessionsNotifier.new);

final dashboardProvider = FutureProvider.autoDispose<Dashboard>(
  (ref) => ref.watch(adminRepositoryProvider).dashboard(),
);

final systemHealthProvider = FutureProvider.autoDispose<SystemHealth>(
  (ref) => ref.watch(adminRepositoryProvider).health(),
);

/// Family argument: (date `YYYY-MM-DD` or null = today, status, search).
typedef DayKey = (String? date, String status, String q);

class AttendanceDayNotifier extends AdminPagedNotifier<AttendanceDayRow, DayKey> {
  @override
  Future<Paged<AttendanceDayRow>> fetchPage(DayKey arg, int page) => ref
      .read(adminRepositoryProvider)
      .attendanceDay(date: arg.$1, status: arg.$2, q: arg.$3, page: page);

  @override
  String idOf(AttendanceDayRow item) => item.user?.id ?? '${item.hashCode}';
}

final attendanceDayProvider = AsyncNotifierProvider.autoDispose
    .family<AttendanceDayNotifier, PagedList<AttendanceDayRow>, DayKey>(
  AttendanceDayNotifier.new,
);
