import '../../../core/utils/dates.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import 'payroll_money.dart';

/// api-contract.md section 17 (payroll pay components, work package 3a):
/// one-time items and bulk import, loans, arrears, leave encashment.
/// In memory only, like the rest of payroll. Money stays a string.

String _s(Object? v) => v == null ? '' : '$v';
String? _opt(Object? v) => v is String && v.isNotEmpty ? v : null;
int _i(Object? v) => v is num ? v.toInt() : (int.tryParse('$v') ?? 0);
double _d(Object? v) => v is num ? v.toDouble() : (double.tryParse('$v') ?? 0);
Map<String, dynamic> _m(Object? v) =>
    v is Map ? v.cast<String, dynamic>() : const <String, dynamic>{};
List<Map<String, dynamic>> _list(Object? v) => [
      if (v is List)
        for (final e in v)
          if (e is Map) e.cast<String, dynamic>(),
    ];
List<String> _strings(Object? v) => [
      if (v is List)
        for (final e in v) '$e',
    ];

EmployeeRef _user(Object? v) =>
    EmployeeRef.parse(v) ?? const EmployeeRef(id: '', employeeId: '', fullName: '');

// ---- common ---------------------------------------------------------------------

/// `none|draft|processed|published` -> a hint about where the item lands.
String? runStatusHint(String? runStatus) => switch (runStatus) {
      'processed' => 'Lands after the run is reopened',
      'published' => 'Lands in the open revision',
      _ => null,
    };

const Map<String, String> payItemStatusLabels = {
  'pending': 'Pending',
  'processing': 'In payslip',
  'paid': 'Paid',
  'cancelled': 'Cancelled',
  'skipped': 'Skipped',
  'proposed': 'Proposed',
  'dismissed': 'Dismissed',
  'active': 'Active',
  'closed': 'Closed',
};

String payItemStatusLabel(String s) => payItemStatusLabels[s] ?? s;

const Map<String, String> payItemFlagLabels = {
  'PERIOD_CLOSED': 'Month already published: will not be paid until moved',
};

String payItemFlagLabel(String f) => payItemFlagLabels[f] ?? f;

// ---- one-time items -----------------------------------------------------------------

/// Codes an admin can create (contract 17.2). A deduction is `OTHER_DEDUCTION`.
const List<String> oneTimeCodes = ['BONUS', 'INCENTIVE', 'OTHER_EARNING', 'OTHER_DEDUCTION'];

const Map<String, String> oneTimeCodeLabels = {
  'BONUS': 'Bonus',
  'INCENTIVE': 'Incentive',
  'OTHER_EARNING': 'Other earning',
  'OTHER_DEDUCTION': 'Other deduction',
};

String oneTimeCodeLabel(String c) => oneTimeCodeLabels[c] ?? c;

/// Largest number of one-time items per employee-month (`TOO_MANY_ITEMS`).
const int maxItemsPerEmployeeMonth = 20;

class OneTimeItem {
  const OneTimeItem({
    required this.id,
    required this.user,
    required this.period,
    required this.code,
    required this.label,
    required this.amount,
    required this.status,
    required this.version,
    this.runStatus,
    this.flags = const [],
    this.batchId,
    this.payslipId,
  });

  factory OneTimeItem.fromJson(Map<String, dynamic> j) => OneTimeItem(
        id: _s(j['id']),
        user: _user(j['user']),
        period: _s(j['period']),
        code: _s(j['code']),
        label: _s(j['label']),
        amount: _s(j['amount']),
        status: _opt(j['status']) ?? 'pending',
        version: _i(j['version']),
        runStatus: _opt(j['run_status']),
        flags: _strings(j['flags']),
        batchId: _opt(j['batch_id']),
        payslipId: _opt(j['payslip_id']),
      );

  final String id;
  final EmployeeRef user;
  final String period;
  final String code;
  final String label;
  final String amount;
  final String status;
  final int version;
  final String? runStatus;
  final List<String> flags;
  final String? batchId;
  final String? payslipId;

  bool get isPending => status == 'pending';
  bool get isDeduction => code == 'OTHER_DEDUCTION';
}

class ItemQuery {
  const ItemQuery({required this.period, this.code, this.status, this.userId, this.batchId});
  final String period;
  final String? code;
  final String? status;
  final String? userId;
  final String? batchId;

  ItemQuery copyWith({String? period, String? Function()? code, String? Function()? status}) => ItemQuery(
        period: period ?? this.period,
        code: code == null ? this.code : code(),
        status: status == null ? this.status : status(),
        userId: userId,
        batchId: batchId,
      );

  @override
  bool operator ==(Object other) =>
      other is ItemQuery &&
      other.period == period &&
      other.code == code &&
      other.status == status &&
      other.userId == userId &&
      other.batchId == batchId;

  @override
  int get hashCode => Object.hash(period, code, status, userId, batchId);
}

/// One row error of an import (dry run or `IMPORT_INVALID`).
class ImportRowError {
  const ImportRowError({required this.row, required this.field, required this.message});
  final int row;
  final String field;
  final String message;

  /// Commit failures carry `field: "row 14: employee_id"` and no `row`.
  factory ImportRowError.fromJson(Map<String, dynamic> j) {
    var row = _i(j['row']);
    var field = _s(j['field']);
    final m = RegExp(r'^row (\d+):\s*(.*)$').firstMatch(field);
    if (m != null) {
      row = int.parse(m[1]!);
      field = m[2]!;
    }
    return ImportRowError(row: row, field: field, message: _s(j['message']));
  }
}

const Map<String, String> importErrorLabels = {
  'UNKNOWN_EMPLOYEE': 'Employee ID not found',
  'NOT_ELIGIBLE': 'Employee is not eligible for payroll',
  'SELF': 'You cannot add items for yourself',
  'INVALID_CODE': 'Code must be BONUS, INCENTIVE, OTHER_EARNING or OTHER_DEDUCTION',
  'INVALID_LABEL': 'Label must be 1 to 40 plain characters',
  'INVALID_AMOUNT': 'Amount must be above 0 with at most 2 decimals',
  'FORMULA_NOT_ALLOWED': 'Formulas and text starting with = + - @ are not allowed',
  'TOO_MANY_ITEMS': 'Too many items for this employee and month',
  'DUPLICATE_ROW': 'Same employee, code, label and amount appears twice',
};

String importErrorLabel(String code) => importErrorLabels[code] ?? code;

class ImportByCode {
  const ImportByCode({required this.code, required this.count, required this.total});
  final String code;
  final int count;
  final String total;
}

class ImportReport {
  const ImportReport({
    required this.period,
    required this.fileSha256,
    required this.rows,
    required this.valid,
    required this.errors,
    required this.byCode,
    this.runStatus,
  });

  factory ImportReport.fromJson(Map<String, dynamic> j) => ImportReport(
        period: _s(j['period']),
        fileSha256: _s(j['file_sha256']),
        rows: _i(j['rows']),
        valid: _i(j['valid']),
        errors: [for (final e in _list(j['errors'])) ImportRowError.fromJson(e)],
        byCode: [
          for (final e in _list(j['by_code']))
            ImportByCode(code: _s(e['code']), count: _i(e['count']), total: _s(e['total'])),
        ],
        runStatus: _opt(j['run_status']),
      );

  final String period;
  final String fileSha256;
  final int rows;
  final int valid;
  final List<ImportRowError> errors;
  final List<ImportByCode> byCode;
  final String? runStatus;

  /// The server only commits a file without any row error.
  bool get canCommit => errors.isEmpty && valid > 0 && fileSha256.length == 64;
}

class ImportCommit {
  const ImportCommit({required this.batchId, required this.period, required this.created});
  factory ImportCommit.fromJson(Map<String, dynamic> j) =>
      ImportCommit(batchId: _s(j['batch_id']), period: _s(j['period']), created: _i(j['created']));
  final String batchId;
  final String period;
  final int created;
}

class ImportBatch {
  const ImportBatch({
    required this.id,
    required this.period,
    required this.rows,
    required this.counts,
    this.createdAt,
    this.createdBy,
    this.cancelledAt,
  });

  factory ImportBatch.fromJson(Map<String, dynamic> j) => ImportBatch(
        id: _s(j['id']),
        period: _s(j['period']),
        rows: _i(j['rows']),
        createdAt: parseTimestamp(j['created_at']),
        createdBy: EmployeeRef.parse(j['created_by']),
        cancelledAt: parseTimestamp(j['cancelled_at']),
        counts: {
          for (final e in _m(j['counts']).entries) e.key: _i(e.value),
        },
      );

  static List<ImportBatch> parseList(Map<String, dynamic> j) =>
      [for (final e in _list(j['items'])) ImportBatch.fromJson(e)];

  final String id;
  final String period;
  final int rows;
  final DateTime? createdAt;
  final EmployeeRef? createdBy;
  final DateTime? cancelledAt;
  final Map<String, int> counts;

  bool get isCancelled => cancelledAt != null;
  int get pendingCount => counts['pending'] ?? 0;
}

class BatchCancelResult {
  const BatchCancelResult({required this.cancelled, required this.notCancelled});
  factory BatchCancelResult.fromJson(Map<String, dynamic> j) =>
      BatchCancelResult(cancelled: _i(j['cancelled']), notCancelled: _i(j['not_cancelled']));
  final int cancelled;
  final int notCancelled;
}

// ---- loans ---------------------------------------------------------------------------

class LoanInstallment {
  const LoanInstallment({
    required this.id,
    required this.seq,
    required this.period,
    required this.amount,
    required this.kind,
    required this.status,
    this.payslipId,
    this.flags = const [],
  });

  factory LoanInstallment.fromJson(Map<String, dynamic> j) => LoanInstallment(
        id: _s(j['id']),
        seq: _i(j['seq']),
        period: _s(j['period']),
        amount: _s(j['amount']),
        kind: _opt(j['kind']) ?? 'emi',
        status: _opt(j['status']) ?? 'pending',
        payslipId: _opt(j['payslip_id']),
        flags: _strings(j['flags']),
      );

  final String id;
  final int seq;
  final String period;
  final String amount;
  final String kind;
  final String status;
  final String? payslipId;
  final List<String> flags;

  bool get isPendingEmi => status == 'pending' && kind == 'emi';
}

class NextInstallment {
  const NextInstallment({required this.period, required this.amount});
  final String period;
  final String amount;
  static NextInstallment? parse(Object? v) =>
      v is Map ? NextInstallment(period: _s(v['period']), amount: _s(v['amount'])) : null;
}

class Loan {
  const Loan({
    required this.id,
    required this.reference,
    required this.user,
    required this.kind,
    required this.principal,
    required this.emi,
    required this.startPeriod,
    required this.recovered,
    required this.inProcess,
    required this.outstanding,
    required this.status,
    required this.version,
    this.closeMode,
    this.reason,
    this.installments = const [],
    this.nextInstallment,
    this.createdAt,
    this.closedAt,
  });

  factory Loan.fromJson(Map<String, dynamic> j) => Loan(
        id: _s(j['id']),
        reference: _s(j['reference']),
        user: _user(j['user']),
        kind: _opt(j['kind']) ?? 'loan',
        principal: _s(j['principal']),
        emi: _s(j['emi']),
        startPeriod: _s(j['start_period']),
        recovered: _opt(j['recovered']) ?? '0.00',
        inProcess: _opt(j['in_process']) ?? '0.00',
        outstanding: _opt(j['outstanding']) ?? '0.00',
        status: _opt(j['status']) ?? 'active',
        version: _i(j['version']),
        closeMode: _opt(j['close_mode']),
        reason: _opt(j['reason']),
        installments: [for (final e in _list(j['installments'])) LoanInstallment.fromJson(e)],
        nextInstallment: NextInstallment.parse(j['next_installment']),
        createdAt: parseTimestamp(j['created_at']),
        closedAt: parseTimestamp(j['closed_at']),
      );

  final String id;
  final String reference;
  final EmployeeRef user;
  final String kind;
  final String principal;
  final String emi;
  final String startPeriod;
  final String recovered;
  final String inProcess;
  final String outstanding;
  final String status;
  final int version;
  final String? closeMode;
  final String? reason;
  final List<LoanInstallment> installments;
  final NextInstallment? nextInstallment;
  final DateTime? createdAt;
  final DateTime? closedAt;

  bool get isActive => status == 'active';
  String get kindLabel => kind == 'advance' ? 'Advance' : 'Loan';
}

class LoanQuery {
  const LoanQuery({this.status, this.kind});
  final String? status;
  final String? kind;

  @override
  bool operator ==(Object other) =>
      other is LoanQuery && other.status == status && other.kind == kind;

  @override
  int get hashCode => Object.hash(status, kind);
}

const Map<String, String> loanCloseModeLabels = {
  'recovered': 'Fully recovered',
  'foreclosed_payroll': 'Foreclosed through payroll',
  'foreclosed_outside': 'Foreclosed outside payroll',
  'cancelled': 'Cancelled',
};

/// Largest principal / smallest principal accepted (contract 17.3), paise.
const int loanMinPrincipalPaise = 100;
const int loanMaxPrincipalPaise = 100000000;

/// Default `payroll.loan_max_tenure_months`.
const int defaultLoanMaxTenure = 60;

/// `ceil(principal / emi)` in paise arithmetic; null when the inputs are not
/// valid money or emi is 0.
int? loanInstallmentCount(String principal, String emi) {
  final p = parseMoneyPaise(principal);
  final e = parseMoneyPaise(emi);
  if (p == null || e == null || e <= 0 || p <= 0) return null;
  return (p + e - 1) ~/ e;
}

/// Local validation of the create form; the server is authoritative.
/// Returns a map of field -> message (empty when valid).
Map<String, String> validateLoanInput({
  required String principal,
  required String emi,
  required String startPeriod,
  required String reason,
  int maxTenure = defaultLoanMaxTenure,
}) {
  final errors = <String, String>{};
  final p = parseMoneyPaise(principal);
  final e = parseMoneyPaise(emi);
  if (p == null || p < loanMinPrincipalPaise || p > loanMaxPrincipalPaise) {
    errors['principal'] = 'Enter an amount from 1.00 to 1000000.00';
  }
  if (e == null || e <= 0) {
    errors['emi'] = 'Enter the monthly instalment above 0';
  } else if (p != null && e > p) {
    errors['emi'] = 'The instalment cannot be more than the principal';
  }
  final n = loanInstallmentCount(principal, emi);
  if (errors['principal'] == null && errors['emi'] == null && n != null && n > maxTenure) {
    errors['emi'] = 'That needs $n instalments; the limit is $maxTenure months';
  }
  if (parseMonth(startPeriod) == null) errors['start_period'] = 'Choose the first deduction month';
  final r = reason.trim();
  if (r.length < 3 || r.length > 200) errors['reason'] = 'Reason must be 3 to 200 characters';
  return errors;
}

/// Local schedule preview (server is authoritative): equal instalments of
/// [emi] from [startPeriod], the last one takes the remainder.
List<({String period, String amount})> previewLoanSchedule(
  String principal,
  String emi,
  String startPeriod,
) {
  final p = parseMoneyPaise(principal);
  final e = parseMoneyPaise(emi);
  final n = loanInstallmentCount(principal, emi);
  if (p == null || e == null || n == null || parseMonth(startPeriod) == null || n > 120) {
    return const [];
  }
  var left = p;
  final out = <({String period, String amount})>[];
  for (var i = 0; i < n; i++) {
    final a = left < e ? left : e;
    out.add((period: shiftMonth(startPeriod, i), amount: paiseToMoney(a)));
    left -= a;
  }
  return out;
}

/// Reference for an outside payment: 1-60 of `[A-Za-z0-9 ._/-]`.
final RegExp loanReferenceRe = RegExp(r'^[A-Za-z0-9 ._/-]{1,60}$');

// ---- employee loans (read-only) ----------------------------------------------------------

class MyLoanInstallment {
  const MyLoanInstallment({
    required this.seq,
    required this.period,
    required this.amount,
    required this.status,
  });
  factory MyLoanInstallment.fromJson(Map<String, dynamic> j) => MyLoanInstallment(
        seq: _i(j['seq']),
        period: _s(j['period']),
        amount: _s(j['amount']),
        status: _s(j['status']),
      );
  final int seq;
  final String period;
  final String amount;

  /// `scheduled | deducted | deferred | cancelled`.
  final String status;
}

const Map<String, String> myInstallmentLabels = {
  'scheduled': 'Scheduled',
  'deducted': 'Deducted',
  'deferred': 'Deferred',
  'cancelled': 'Cancelled',
};

class MyLoan {
  const MyLoan({
    required this.id,
    required this.reference,
    required this.kind,
    required this.principal,
    required this.emi,
    required this.outstanding,
    required this.status,
    required this.startPeriod,
    this.nextInstallment,
    this.installments = const [],
  });

  factory MyLoan.fromJson(Map<String, dynamic> j) => MyLoan(
        id: _s(j['id']),
        reference: _s(j['reference']),
        kind: _opt(j['kind']) ?? 'loan',
        principal: _s(j['principal']),
        emi: _s(j['emi']),
        outstanding: _opt(j['outstanding']) ?? '0.00',
        status: _opt(j['status']) ?? 'active',
        startPeriod: _s(j['start_period']),
        nextInstallment: NextInstallment.parse(j['next_installment']),
        installments: [for (final e in _list(j['installments'])) MyLoanInstallment.fromJson(e)],
      );

  static List<MyLoan> parseList(Map<String, dynamic> j) =>
      [for (final e in _list(j['items'])) MyLoan.fromJson(e)];

  final String id;
  final String reference;
  final String kind;
  final String principal;
  final String emi;
  final String outstanding;
  final String status;
  final String startPeriod;
  final NextInstallment? nextInstallment;
  final List<MyLoanInstallment> installments;

  String get kindLabel => kind == 'advance' ? 'Advance' : 'Loan';
}

// ---- arrears --------------------------------------------------------------------------------

class ArrearsMonth {
  const ArrearsMonth({
    required this.period,
    required this.paidDays,
    required this.paidEarnings,
    required this.newEarnings,
    required this.alreadyPaid,
    required this.diff,
    required this.basicDiff,
  });

  factory ArrearsMonth.fromJson(Map<String, dynamic> j) => ArrearsMonth(
        period: _s(j['period']),
        paidDays: _d(j['paid_days']),
        paidEarnings: _s(j['paid_earnings']),
        newEarnings: _s(j['new_earnings']),
        alreadyPaid: _opt(j['already_paid_arrears']) ?? '0.00',
        diff: _s(j['diff']),
        basicDiff: _s(j['basic_diff']),
      );

  final String period;
  final double paidDays;
  final String paidEarnings;
  final String newEarnings;
  final String alreadyPaid;

  /// Signed for `OTHER_DEDUCTION` arrears.
  final String diff;
  final String basicDiff;
}

class Arrears {
  const Arrears({
    required this.id,
    required this.user,
    required this.code,
    required this.amount,
    required this.fromPeriod,
    required this.toPeriod,
    required this.status,
    required this.version,
    this.period,
    this.runStatus,
    this.triggerEffectiveFrom,
    this.breakdown = const [],
    this.payslipId,
    this.flags = const [],
  });

  factory Arrears.fromJson(Map<String, dynamic> j) => Arrears(
        id: _s(j['id']),
        user: _user(j['user']),
        code: _opt(j['code']) ?? 'ARREARS',
        amount: _s(j['amount']),
        fromPeriod: _s(j['from_period']),
        toPeriod: _s(j['to_period']),
        status: _opt(j['status']) ?? 'proposed',
        version: _i(j['version']),
        period: _opt(j['period']),
        runStatus: _opt(j['run_status']),
        triggerEffectiveFrom: _opt(_m(j['trigger_structure'])['effective_from']),
        breakdown: [for (final e in _list(j['breakdown'])) ArrearsMonth.fromJson(e)],
        payslipId: _opt(j['payslip_id']),
        flags: _strings(j['flags']),
      );

  final String id;
  final EmployeeRef user;

  /// `ARREARS` (earning) or `OTHER_DEDUCTION` (salary recovery).
  final String code;
  final String amount;
  final String fromPeriod;
  final String toPeriod;
  final String status;
  final int version;
  final String? period;
  final String? runStatus;
  final String? triggerEffectiveFrom;
  final List<ArrearsMonth> breakdown;
  final String? payslipId;
  final List<String> flags;

  bool get isRecovery => code == 'OTHER_DEDUCTION';
  bool get isProposed => status == 'proposed';
  bool get canDismiss => status == 'proposed' || status == 'pending';

  String get rangeLabel => fromPeriod == toPeriod
      ? monthTitle(fromPeriod)
      : '${monthTitle(fromPeriod)} - ${monthTitle(toPeriod)}';
}

class ArrearsQuery {
  const ArrearsQuery({this.status});
  final String? status;

  @override
  bool operator ==(Object other) => other is ArrearsQuery && other.status == status;

  @override
  int get hashCode => status.hashCode;
}

// ---- leave encashment -----------------------------------------------------------------------

class LeaveTypeRef {
  const LeaveTypeRef({required this.id, required this.code, required this.name});
  factory LeaveTypeRef.fromJson(Object? v) {
    final j = _m(v);
    return LeaveTypeRef(id: _s(j['id']), code: _s(j['code']), name: _s(j['name']));
  }
  final String id;
  final String code;
  final String name;
}

class EncashmentQuote {
  const EncashmentQuote({
    required this.leaveType,
    required this.year,
    required this.availableDays,
    required this.days,
    required this.dailyRate,
    required this.amount,
    required this.basis,
    required this.divisor,
    required this.period,
  });

  factory EncashmentQuote.fromJson(Map<String, dynamic> j) => EncashmentQuote(
        leaveType: LeaveTypeRef.fromJson(j['leave_type']),
        year: _i(j['year']),
        availableDays: _d(j['available_days']),
        days: _d(j['days']),
        dailyRate: _s(j['daily_rate']),
        amount: _s(j['amount']),
        basis: _s(j['basis']),
        divisor: _i(j['divisor']),
        period: _s(j['period']),
      );

  final LeaveTypeRef leaveType;
  final int year;
  final double availableDays;
  final double days;
  final String dailyRate;
  final String amount;
  final String basis;
  final int divisor;
  final String period;
}

class Encashment {
  const Encashment({
    required this.id,
    required this.user,
    required this.leaveType,
    required this.year,
    required this.days,
    required this.dailyRate,
    required this.amount,
    required this.period,
    required this.source,
    required this.status,
    required this.version,
    this.runStatus,
    this.payslipId,
  });

  factory Encashment.fromJson(Map<String, dynamic> j) => Encashment(
        id: _s(j['id']),
        user: _user(j['user']),
        leaveType: LeaveTypeRef.fromJson(j['leave_type']),
        year: _i(j['year']),
        days: _d(j['days']),
        dailyRate: _s(j['daily_rate']),
        amount: _s(j['amount']),
        period: _s(j['period']),
        source: _opt(j['source']) ?? 'request',
        status: _opt(j['status']) ?? 'pending',
        version: _i(j['version']),
        runStatus: _opt(j['run_status']),
        payslipId: _opt(j['payslip_id']),
      );

  final String id;
  final EmployeeRef user;
  final LeaveTypeRef leaveType;
  final int year;
  final double days;
  final String dailyRate;
  final String amount;
  final String period;
  final String source;
  final String status;
  final int version;
  final String? runStatus;
  final String? payslipId;

  bool get isPending => status == 'pending';
}

class EncashmentQuery {
  const EncashmentQuery({required this.year, this.status});
  final int year;
  final String? status;

  @override
  bool operator ==(Object other) =>
      other is EncashmentQuery && other.year == year && other.status == status;

  @override
  int get hashCode => Object.hash(year, status);
}

/// Days must be a positive multiple of 0.5 (contract 17.5).
String? validateEncashDays(String? raw, {double? max}) {
  final n = double.tryParse((raw ?? '').trim());
  if (n == null || n.isNaN || n.isInfinite) return 'Enter the number of days';
  if (n <= 0) return 'Days must be above 0';
  if ((n * 2 - (n * 2).roundToDouble()).abs() > 1e-9) return 'Use steps of 0.5 day';
  if (max != null && n > max + 1e-9) return 'Only ${_days(max)} days are available';
  return null;
}

String _days(double d) => d == d.roundToDouble() ? '${d.toInt()}' : d.toStringAsFixed(1);

// ---- payslip components (read-only lines pulled by the server) -----------------------------

class ComponentSource {
  const ComponentSource({required this.type, required this.id});
  final String type;
  final String id;
}

const Map<String, String> componentSourceLabels = {
  'one_time': 'One-time item',
  'loan_installment': 'Loan instalment',
  'arrears': 'Arrears',
  'encashment': 'Leave encashment',
  'reimbursement': 'Reimbursement',
  'overtime': 'Overtime',
};

String componentSourceLabel(String t) => componentSourceLabels[t] ?? t;

class PayslipComponent {
  const PayslipComponent({
    required this.code,
    required this.label,
    required this.amount,
    this.minutes,
    this.minutesTotal,
    this.sources = const [],
  });

  factory PayslipComponent.fromJson(Map<String, dynamic> j) => PayslipComponent(
        code: _s(j['code']),
        label: _s(j['label']),
        amount: _s(j['amount']),
        minutes: j['minutes'] is num ? (j['minutes'] as num).toInt() : null,
        minutesTotal: j['minutes_total'] is num ? (j['minutes_total'] as num).toInt() : null,
        sources: [
          for (final s in _list(j['sources'])) ComponentSource(type: _s(s['type']), id: _s(s['id'])),
        ],
      );

  final String code;
  final String label;
  final String amount;
  final int? minutes;

  /// Overtime: approved minutes before the monthly cap (`minutes` is after it).
  final int? minutesTotal;
  final List<ComponentSource> sources;

  /// `7h 30m paid` or `7h 30m paid of 9h approved (capped)`; null when the line has no minutes.
  String? get minutesCaption {
    final m = minutes;
    if (m == null) return null;
    final t = minutesTotal;
    final paid = formatMinutesShort(m);
    if (t == null || t == m) return '$paid paid';
    return '$paid paid of ${formatMinutesShort(t)} approved${t > m ? ' (capped)' : ''}';
  }
}

String formatMinutesShort(int minutes) {
  final m = minutes < 0 ? 0 : minutes;
  final h = m ~/ 60;
  final r = m % 60;
  if (h == 0) return '${r}m';
  return r == 0 ? '${h}h' : '${h}h ${r}m';
}

/// Short, readable form of a source id (first 8 characters).
String shortId(String id) => id.length <= 8 ? id : id.substring(0, 8);

/// Warning chips new in section 17.
const Map<String, String> payItemWarningLabels = {
  'ARREARS_PROPOSED': 'Arrears proposed: review in Pay items',
  'MANUAL_COMPONENT_OVERLAP': 'A manual adjustment may duplicate an automatic line',
  'OVERTIME_UNREVIEWED': 'Overtime not reviewed',
  'OVERTIME_CAPPED': 'Overtime capped',
};

/// Variance reason added in 17.1.
const String componentsVarianceReason = 'COMPONENTS';
