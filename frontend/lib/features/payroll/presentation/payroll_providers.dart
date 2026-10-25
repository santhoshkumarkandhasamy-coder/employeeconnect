import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../admin/presentation/admin_providers.dart' show AdminPagedNotifier;
import '../../announcements/presentation/announcement_providers.dart' show PagedList;
import '../../attendance/domain/attendance_models.dart' show Paged;
import '../data/payroll_repository.dart';
import '../domain/payroll_models.dart';

/// All payroll state is autoDispose and in memory only: it is never written to
/// the JSON cache or to disk (plan 12, D10). Screens close on lock / logout,
/// which disposes it.

final myPayslipsProvider = FutureProvider.autoDispose.family<List<PayslipListItem>, int>(
  (ref, year) => ref.watch(payrollRepositoryProvider).myPayslips(year),
);

final myPayslipProvider = FutureProvider.autoDispose.family<Payslip, String>(
  (ref, id) => ref.watch(payrollRepositoryProvider).myPayslip(id),
);

// ---- admin -----------------------------------------------------------------------

final payrollRunsProvider = FutureProvider.autoDispose.family<List<PayrollRunItem>, int>(
  (ref, year) => ref.watch(payrollRepositoryProvider).runs(year),
);

final payrollRunProvider = FutureProvider.autoDispose.family<PayrollRun, String>(
  (ref, id) => ref.watch(payrollRepositoryProvider).run(id),
);

class RunPayslipsNotifier extends AdminPagedNotifier<RunPayslipRow, RunPayslipQuery> {
  @override
  Future<Paged<RunPayslipRow>> fetchPage(RunPayslipQuery arg, int page) =>
      ref.read(payrollRepositoryProvider).runPayslips(arg, page: page);

  @override
  String idOf(RunPayslipRow item) => item.id;
}

final runPayslipsProvider = AsyncNotifierProvider.autoDispose
    .family<RunPayslipsNotifier, PagedList<RunPayslipRow>, RunPayslipQuery>(
  RunPayslipsNotifier.new,
);

class PayrollEmployeesNotifier extends AdminPagedNotifier<PayrollEmployee, PayrollEmployeeQuery> {
  @override
  Future<Paged<PayrollEmployee>> fetchPage(PayrollEmployeeQuery arg, int page) =>
      ref.read(payrollRepositoryProvider).employees(arg, page: page);

  @override
  String idOf(PayrollEmployee item) => item.user.id;
}

final payrollEmployeesProvider = AsyncNotifierProvider.autoDispose
    .family<PayrollEmployeesNotifier, PagedList<PayrollEmployee>, PayrollEmployeeQuery>(
  PayrollEmployeesNotifier.new,
);

final adminPayslipProvider = FutureProvider.autoDispose.family<Payslip, String>(
  (ref, id) => ref.watch(payrollRepositoryProvider).payslip(id),
);

final payrollStructuresProvider =
    FutureProvider.autoDispose.family<List<SalaryStructure>, String>(
  (ref, userId) => ref.watch(payrollRepositoryProvider).structures(userId),
);

/// Family argument: (user id, reveal). Revealed copies are never kept: the
/// provider is autoDispose and the screen drops the revealed state on exit.
typedef DetailsKey = (String userId, bool reveal);

final payrollDetailsProvider = FutureProvider.autoDispose.family<PayrollDetails, DetailsKey>(
  (ref, key) => ref.watch(payrollRepositoryProvider).details(key.$1, reveal: key.$2),
);

final payrollSettingsProvider = FutureProvider.autoDispose<Map<String, Object?>>(
  (ref) => ref.watch(payrollRepositoryProvider).settings(),
);

/// Device clock; overridable in tests.
final payrollClockProvider = Provider<DateTime Function()>((ref) => DateTime.now);
