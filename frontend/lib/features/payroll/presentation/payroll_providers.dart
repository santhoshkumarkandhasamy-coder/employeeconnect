import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../admin/presentation/admin_providers.dart' show AdminPagedNotifier;
import '../../announcements/presentation/announcement_providers.dart' show PagedList;
import '../../attendance/domain/attendance_models.dart' show Paged;
import '../data/payroll_repository.dart';
import '../domain/pay_items_models.dart';
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

final payrollDashboardProvider = FutureProvider.autoDispose.family<Map<String, dynamic>, String>(
  (ref, period) => ref.watch(payrollRepositoryProvider).dashboard(period),
);

final payrollVarianceProvider = FutureProvider.autoDispose.family<Map<String, dynamic>, String>(
  (ref, period) => ref.watch(payrollRepositoryProvider).variance(period),
);

/// Statutory dues for one month (contract section 16). In memory only.
final statutoryDuesProvider = FutureProvider.autoDispose.family<List<StatutoryMonth>, String>(
  (ref, period) => ref.watch(payrollRepositoryProvider).statutoryDues(period),
);

// ---- pay items (contract section 17) -----------------------------------------------

class OneTimeItemsNotifier extends AdminPagedNotifier<OneTimeItem, ItemQuery> {
  @override
  Future<Paged<OneTimeItem>> fetchPage(ItemQuery arg, int page) =>
      ref.read(payrollRepositoryProvider).oneTimeItems(arg, page: page);

  @override
  String idOf(OneTimeItem item) => item.id;
}

final oneTimeItemsProvider = AsyncNotifierProvider.autoDispose
    .family<OneTimeItemsNotifier, PagedList<OneTimeItem>, ItemQuery>(OneTimeItemsNotifier.new);

/// Import batches of one month.
final importBatchesProvider = FutureProvider.autoDispose.family<List<ImportBatch>, String>(
  (ref, period) => ref.watch(payrollRepositoryProvider).importBatches(period: period),
);

class LoansNotifier extends AdminPagedNotifier<Loan, LoanQuery> {
  @override
  Future<Paged<Loan>> fetchPage(LoanQuery arg, int page) =>
      ref.read(payrollRepositoryProvider).loans(arg, page: page);

  @override
  String idOf(Loan item) => item.id;
}

final loansProvider = AsyncNotifierProvider.autoDispose
    .family<LoansNotifier, PagedList<Loan>, LoanQuery>(LoansNotifier.new);

final loanDetailProvider = FutureProvider.autoDispose.family<Loan, String>(
  (ref, id) => ref.watch(payrollRepositoryProvider).loan(id),
);

class ArrearsNotifier extends AdminPagedNotifier<Arrears, ArrearsQuery> {
  @override
  Future<Paged<Arrears>> fetchPage(ArrearsQuery arg, int page) =>
      ref.read(payrollRepositoryProvider).arrears(arg, page: page);

  @override
  String idOf(Arrears item) => item.id;
}

final arrearsProvider = AsyncNotifierProvider.autoDispose
    .family<ArrearsNotifier, PagedList<Arrears>, ArrearsQuery>(ArrearsNotifier.new);

class EncashmentsNotifier extends AdminPagedNotifier<Encashment, EncashmentQuery> {
  @override
  Future<Paged<Encashment>> fetchPage(EncashmentQuery arg, int page) =>
      ref.read(payrollRepositoryProvider).encashments(arg, page: page);

  @override
  String idOf(Encashment item) => item.id;
}

final encashmentsProvider = AsyncNotifierProvider.autoDispose
    .family<EncashmentsNotifier, PagedList<Encashment>, EncashmentQuery>(EncashmentsNotifier.new);

final encashableLeaveTypesProvider = FutureProvider.autoDispose<List<LeaveTypeRef>>(
  (ref) => ref.watch(payrollRepositoryProvider).encashableLeaveTypes(),
);

/// Employee: own loans (read-only). In memory only.
final myLoansProvider = FutureProvider.autoDispose<List<MyLoan>>(
  (ref) => ref.watch(payrollRepositoryProvider).myLoans(),
);

final myLoanProvider = FutureProvider.autoDispose.family<MyLoan, String>(
  (ref, id) => ref.watch(payrollRepositoryProvider).myLoan(id),
);
