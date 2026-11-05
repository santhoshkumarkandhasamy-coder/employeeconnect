import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../admin/presentation/admin_providers.dart' show AdminPagedNotifier;
import '../../announcements/presentation/announcement_providers.dart' show PagedList;
import '../../attendance/domain/attendance_models.dart' show Paged;
import '../data/payroll_repository.dart';
import '../domain/claims_models.dart';
import '../domain/gross_split.dart';
import '../domain/pay_items_models.dart';
import '../domain/phase4a_models.dart';
import '../domain/phase4b_models.dart';
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

// ---- reimbursement claims and overtime (contract 17.6, 17.8) -----------------------

/// Categories, caps and the maximum age of an expense (employee form).
final claimCategoriesProvider = FutureProvider.autoDispose<ClaimCategories>(
  (ref) => ref.watch(payrollRepositoryProvider).claimCategories(),
);

class MyClaimsNotifier extends AdminPagedNotifier<Claim, ClaimQuery> {
  @override
  Future<Paged<Claim>> fetchPage(ClaimQuery arg, int page) =>
      ref.read(payrollRepositoryProvider).myClaims(arg, page: page);

  @override
  String idOf(Claim item) => item.id;
}

final myClaimsProvider = AsyncNotifierProvider.autoDispose
    .family<MyClaimsNotifier, PagedList<Claim>, ClaimQuery>(MyClaimsNotifier.new);

final myClaimProvider = FutureProvider.autoDispose.family<Claim, String>(
  (ref, id) => ref.watch(payrollRepositoryProvider).myClaim(id),
);

class AdminClaimsNotifier extends AdminPagedNotifier<Claim, AdminClaimQuery> {
  @override
  Future<Paged<Claim>> fetchPage(AdminClaimQuery arg, int page) =>
      ref.read(payrollRepositoryProvider).adminClaims(arg, page: page);

  @override
  String idOf(Claim item) => item.id;
}

final adminClaimsProvider = AsyncNotifierProvider.autoDispose
    .family<AdminClaimsNotifier, PagedList<Claim>, AdminClaimQuery>(AdminClaimsNotifier.new);

final adminClaimProvider = FutureProvider.autoDispose.family<Claim, String>(
  (ref, id) => ref.watch(payrollRepositoryProvider).adminClaim(id),
);

class OvertimeNotifier extends AdminPagedNotifier<OvertimeDay, OvertimeQuery> {
  @override
  Future<Paged<OvertimeDay>> fetchPage(OvertimeQuery arg, int page) =>
      ref.read(payrollRepositoryProvider).overtimeDays(arg, page: page);

  @override
  String idOf(OvertimeDay item) => item.id;
}

final overtimeProvider = AsyncNotifierProvider.autoDispose
    .family<OvertimeNotifier, PagedList<OvertimeDay>, OvertimeQuery>(OvertimeNotifier.new);

final overtimeSummaryProvider = FutureProvider.autoDispose.family<List<OvertimeSummary>, OvertimeQuery>(
  (ref, q) => ref.watch(payrollRepositoryProvider).overtimeSummary(q),
);

/// Employee: own overtime of one month (`YYYY-MM`). `enabled == false` = show nothing.
final myOvertimeProvider = FutureProvider.autoDispose.family<MyOvertime, String>(
  (ref, period) => ref.watch(payrollRepositoryProvider).myOvertime(period),
);

/// Whether the overtime switch is on, from the payroll settings (HR/admin only;
/// a failed read counts as off so the segment stays hidden).
final overtimeEnabledProvider = FutureProvider.autoDispose<bool>((ref) async {
  final s = await ref.watch(payrollSettingsProvider.future);
  return s['payroll.overtime_enabled'] == true;
});

// ---- phase 4a (contract section 18) ---------------------------------------------------

/// The company split rule from the server settings (plan 16 F9). A failed read
/// falls back to the defaults: the split is only a preview, the server decides.
final splitRuleProvider = FutureProvider.autoDispose<SplitRule>((ref) async {
  try {
    return SplitRule.fromSettings(await ref.watch(payrollSettingsProvider.future));
  } on Object {
    return SplitRule.defaults;
  }
});

/// `payroll.increment_max_pct` (default 50), for the percent field limit.
final incrementMaxPctProvider = FutureProvider.autoDispose<int>((ref) async {
  try {
    final v = (await ref.watch(payrollSettingsProvider.future))['payroll.increment_max_pct'];
    return v is num && v >= 1 && v <= 100 ? v.toInt() : 50;
  } on Object {
    return 50;
  }
});

class IncrementsNotifier extends AdminPagedNotifier<Increment, IncrementQuery> {
  @override
  Future<Paged<Increment>> fetchPage(IncrementQuery arg, int page) =>
      ref.read(payrollRepositoryProvider).increments(arg, page: page);

  @override
  String idOf(Increment item) => item.id;
}

final incrementsProvider = AsyncNotifierProvider.autoDispose
    .family<IncrementsNotifier, PagedList<Increment>, IncrementQuery>(IncrementsNotifier.new);

final incrementProvider = FutureProvider.autoDispose.family<Increment, String>(
  (ref, id) => ref.watch(payrollRepositoryProvider).increment(id),
);

class IncrementBatchesNotifier extends AdminPagedNotifier<IncrementBatch, String?> {
  @override
  Future<Paged<IncrementBatch>> fetchPage(String? arg, int page) =>
      ref.read(payrollRepositoryProvider).incrementBatches(kind: arg, page: page);

  @override
  String idOf(IncrementBatch item) => item.id;
}

/// Argument: the batch kind filter (`percent` | `import`) or null.
final incrementBatchesProvider = AsyncNotifierProvider.autoDispose
    .family<IncrementBatchesNotifier, PagedList<IncrementBatch>, String?>(IncrementBatchesNotifier.new);

final incrementBatchProvider = FutureProvider.autoDispose.family<IncrementBatch, String>(
  (ref, id) => ref.watch(payrollRepositoryProvider).incrementBatch(id),
);

final salaryHistoryProvider = FutureProvider.autoDispose.family<List<SalaryHistoryEntry>, String>(
  (ref, userId) => ref.watch(payrollRepositoryProvider).salaryHistory(userId),
);

final gratuityProvider = FutureProvider.autoDispose.family<GratuityPage, GratuityKey>(
  (ref, key) => ref.watch(payrollRepositoryProvider).gratuity(key.$1, page: key.$2),
);

// ---- phase 4b: full and final settlement (contract 18.8, 18.9) -------------------------

class FnfListNotifier extends AdminPagedNotifier<FnfListItem, FnfQuery> {
  @override
  Future<Paged<FnfListItem>> fetchPage(FnfQuery arg, int page) =>
      ref.read(payrollRepositoryProvider).fnfList(arg, page: page);

  @override
  String idOf(FnfListItem item) => item.id;
}

final fnfListProvider = AsyncNotifierProvider.autoDispose
    .family<FnfListNotifier, PagedList<FnfListItem>, FnfQuery>(FnfListNotifier.new);

final fnfProvider = FutureProvider.autoDispose.family<Fnf, String>(
  (ref, id) => ref.watch(payrollRepositoryProvider).fnf(id),
);

/// Estimate of the final payslip; reloaded whenever the settlement is invalidated.
final fnfPreviewProvider = FutureProvider.autoDispose.family<FnfPreview, String>(
  (ref, id) => ref.watch(payrollRepositoryProvider).fnfPreview(id),
);

/// Employee: own settlement, or null. In memory only.
final myFnfProvider = FutureProvider.autoDispose<MyFnf?>(
  (ref) => ref.watch(payrollRepositoryProvider).myFnf(),
);

/// Submitted increments (outside bulk batches), batches and F&F settlements
/// waiting for a decision: the Approvals queue and its badge. The batch endpoint
/// has no status filter, so the first page of batches is filtered here.
class ApprovalQueue {
  const ApprovalQueue({required this.increments, required this.batches, this.fnfs = const []});
  final List<Increment> increments;
  final List<IncrementBatch> batches;
  final List<FnfListItem> fnfs;
  int get count => increments.length + batches.length + fnfs.length;
}

final approvalQueueProvider = FutureProvider.autoDispose<ApprovalQueue>((ref) async {
  final repo = ref.watch(payrollRepositoryProvider);
  final results = await Future.wait([
    repo.increments(const IncrementQuery(status: 'submitted')),
    repo.incrementBatches(),
    repo.fnfList(const FnfQuery(status: 'submitted')),
  ]);
  final incs = (results[0] as Paged<Increment>).items;
  final batches = (results[1] as Paged<IncrementBatch>).items;
  final fnfs = (results[2] as Paged<FnfListItem>).items;
  return ApprovalQueue(
    increments: [for (final i in incs) if (!i.isInBatch) i],
    batches: [for (final b in batches) if (!b.isCancelled && b.submittedCount > 0) b],
    fnfs: [for (final f in fnfs) if (f.status == 'submitted') f],
  );
});
