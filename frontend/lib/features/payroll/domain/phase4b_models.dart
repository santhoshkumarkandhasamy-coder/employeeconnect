import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import 'pay_items_models.dart' show LeaveTypeRef;
import 'payroll_models.dart' show PayslipLine, payrollErrorMessage;

/// api-contract.md section 18 (payroll phase 4, work package 4b): full and
/// final settlement (F&F). In memory only. Money stays a string; amounts,
/// reasons, comments and remarks are never logged (no toString overrides, no
/// print) and nothing here is persisted.

String _s(Object? v) => v == null ? '' : '$v';
String? _opt(Object? v) => v is String && v.isNotEmpty ? v : null;
int _i(Object? v) => v is num ? v.toInt() : (int.tryParse('$v') ?? 0);
double? _dbl(Object? v) => v is num ? v.toDouble() : double.tryParse('${v ?? ''}');
Map<String, dynamic> _m(Object? v) => v is Map ? v.cast<String, dynamic>() : const <String, dynamic>{};
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

// ---- labels ------------------------------------------------------------------------------------

const Map<String, String> fnfStatusLabels = {
  'draft': 'Draft',
  'submitted': 'Waiting for approval',
  'approved': 'Approved',
  'settled': 'Settled',
  'cancelled': 'Cancelled',
};

String fnfStatusLabel(String s) => fnfStatusLabels[s] ?? s;

const List<String> exitReasons = [
  'resignation',
  'termination',
  'retirement',
  'death',
  'contract_end',
  'absconding',
  'other',
];

const Map<String, String> exitReasonLabels = {
  'resignation': 'Resignation',
  'termination': 'Termination',
  'retirement': 'Retirement',
  'death': 'Death',
  'contract_end': 'End of contract',
  'absconding': 'Absconding',
  'other': 'Other',
};

String exitReasonLabel(String r) => exitReasonLabels[r] ?? r;

/// Checklist and estimate warnings of an F&F (contract 18.8, plan 16 section 3).
const Map<String, String> fnfWarningLabels = {
  'ARREARS_PROPOSED': 'Arrears are proposed and not yet decided',
  'CLAIMS_OPEN': 'Reimbursement claims are still open',
  'OVERTIME_UNREVIEWED': 'Overtime is not reviewed yet',
  'INCREMENT_PENDING': 'A salary increment is pending',
  'PAID_AFTER_EXIT': 'Salary was paid for days after the last working day: revise the payslip or add a deduction',
  'ACCOUNT_STILL_ACTIVE': 'The account is still active after the last working day',
  'NEGATIVE_NET_ESTIMATE': 'The employee would owe the company: record a loan as paid outside or reduce the items',
  'TDS_REVIEW': 'Check the income tax (TDS) on this settlement',
  'NO_JOIN_DATE': 'No joining date: gratuity cannot be worked out',
  'STRUCTURE_MISSING': 'No salary structure for the settlement month',
  'ENCASHMENT_INVALID': 'A leave encashment line is not valid any more',
  'FNF_SETTLEMENT_ONLY': 'Settlement-only payslip: no salary for the month, only the settlement lines',
  'EXIT_IN_MONTH': 'The employee leaves during this month',
};

String fnfWarningLabel(String w) => fnfWarningLabels[w] ?? w;

const Map<String, String> fnfItemCodeLabels = {
  'OTHER_EARNING': 'Other earning',
  'OTHER_DEDUCTION': 'Other deduction',
  'BONUS': 'Bonus',
  'INCENTIVE': 'Incentive',
};

const List<String> fnfItemCodes = ['OTHER_EARNING', 'OTHER_DEDUCTION', 'BONUS', 'INCENTIVE'];

String fnfItemCodeLabel(String c) => fnfItemCodeLabels[c] ?? c;

const Map<String, String> fnfOpenItemLabels = {
  'one_time': 'One-time pay item',
  'encashment': 'Leave encashment',
  'arrears': 'Arrears',
  'reimbursement': 'Reimbursement claim',
};

String fnfOpenItemLabel(String t) => fnfOpenItemLabels[t] ?? t;

String fnfRunStatusLabel(String s) => switch (s) {
      'draft' => 'run in draft',
      'processed' => 'run processed',
      'published' => 'run published',
      _ => 'no run yet',
    };

// ---- permission and rules ----------------------------------------------------------------------

/// Whether [selfId] may approve, reject or revoke [f]: not the employee, the
/// creator, the last editor, the submitter or anybody who ever edited or
/// submitted it (contract 18.0). Cosmetic only: the server enforces it.
bool canDecideFnf(Fnf f, String? selfId) {
  if (selfId == null || selfId.isEmpty) return false;
  return selfId != f.user.id &&
      selfId != f.createdBy?.id &&
      selfId != f.submittedBy?.id &&
      selfId != f.lastEditedById &&
      !f.contributorIds.contains(selfId);
}

String? cannotDecideFnfReason(Fnf f, String? selfId) {
  if (selfId == null || canDecideFnf(f, selfId)) return null;
  return selfId == f.user.id
      ? 'This is your own settlement. You cannot decide on it.'
      : 'You made, edited or submitted this settlement. A different person must approve, reject or revoke it.';
}

/// A negative money string such as "-120.00" (zero is not negative).
bool isNegativeMoney(String? money) {
  final t = (money ?? '').trim();
  if (!t.startsWith('-')) return false;
  return RegExp(r'[1-9]').hasMatch(t);
}

/// Q5: a settlement where the employee owes the company is not supported on a
/// payslip. True when the server's warnings, the preview or the approval
/// snapshot say the net is below zero.
bool fnfNetNegative(Fnf f, [FnfPreview? preview]) =>
    f.warnings.contains('NEGATIVE_NET_ESTIMATE') ||
    (preview?.warnings.contains('NEGATIVE_NET_ESTIMATE') ?? false) ||
    isNegativeMoney(preview?.netPay) ||
    isNegativeMoney(f.summary?.netEstimate);

// ---- the settlement ----------------------------------------------------------------------------

class FnfNotice {
  const FnfNotice({
    required this.requiredDays,
    this.servedDays,
    required this.shortfallDays,
    required this.computedShortfallDays,
    required this.waived,
    required this.basis,
    required this.divisor,
    required this.dailyRate,
    required this.amount,
    this.overrideDays,
    this.overrideReason,
    this.itemId,
    this.status,
  });

  factory FnfNotice.fromJson(Map<String, dynamic> j) {
    final o = j['override'] is Map ? _m(j['override']) : null;
    return FnfNotice(
      requiredDays: _i(j['required_days']),
      servedDays: j['served_days'] is num ? (j['served_days'] as num).toInt() : null,
      shortfallDays: _i(j['shortfall_days']),
      computedShortfallDays: _i(j['computed_shortfall_days']),
      waived: j['waived'] == true,
      overrideDays: o == null ? null : _i(o['days']),
      overrideReason: o == null ? null : _opt(o['reason']),
      basis: _opt(j['basis']) ?? 'gross',
      divisor: _i(j['divisor']) == 0 ? 30 : _i(j['divisor']),
      dailyRate: _opt(j['daily_rate']) ?? '0.00',
      amount: _opt(j['amount']) ?? '0.00',
      itemId: _opt(j['item_id']),
      status: _opt(j['status']),
    );
  }

  static const empty = FnfNotice(
    requiredDays: 0,
    shortfallDays: 0,
    computedShortfallDays: 0,
    waived: false,
    basis: 'gross',
    divisor: 30,
    dailyRate: '0.00',
    amount: '0.00',
  );

  final int requiredDays;
  /// Null without a resignation date (the shortfall is 0 then).
  final int? servedDays;
  final int shortfallDays;
  final int computedShortfallDays;
  final bool waived;
  final int? overrideDays;
  final String? overrideReason;
  final String basis;
  final int divisor;
  final String dailyRate;
  final String amount;
  final String? itemId;
  final String? status;

  bool get hasOverride => overrideDays != null;
}

class FnfEncashment {
  const FnfEncashment({
    required this.leaveType,
    required this.year,
    required this.days,
    required this.dailyRate,
    required this.amount,
    this.availableDays,
    this.encashmentId,
    this.status,
  });

  factory FnfEncashment.fromJson(Map<String, dynamic> j) => FnfEncashment(
        leaveType: LeaveTypeRef.fromJson(j['leave_type']),
        year: _i(j['year']),
        days: _dbl(j['days']) ?? 0,
        availableDays: _dbl(j['available_days']),
        dailyRate: _opt(j['daily_rate']) ?? '0.00',
        amount: _opt(j['amount']) ?? '0.00',
        encashmentId: _opt(j['encashment_id']),
        status: _opt(j['status']),
      );

  final LeaveTypeRef leaveType;
  final int year;
  final double days;
  final double? availableDays;
  final String dailyRate;
  final String amount;
  final String? encashmentId;
  final String? status;
}

class FnfLoan {
  const FnfLoan({
    required this.loanId,
    required this.reference,
    required this.kind,
    required this.outstanding,
    this.mode,
    this.paidOn,
    this.paymentReference,
    this.installmentId,
    this.status,
  });

  factory FnfLoan.fromJson(Map<String, dynamic> j) {
    final l = _m(j['loan']);
    return FnfLoan(
      loanId: _s(l['id']),
      reference: _s(l['reference']),
      kind: _opt(l['kind']) ?? 'loan',
      outstanding: _opt(j['outstanding']) ?? '0.00',
      mode: _opt(j['mode']),
      paidOn: _opt(j['paid_on']),
      paymentReference: _opt(j['reference']),
      installmentId: _opt(j['installment_id']),
      status: _opt(j['status']),
    );
  }

  final String loanId;
  final String reference;
  final String kind;
  final String outstanding;

  /// `payroll` | `outside` | null (not chosen yet).
  final String? mode;
  final String? paidOn;
  final String? paymentReference;
  final String? installmentId;
  final String? status;

  String get kindLabel => kind == 'advance' ? 'Advance' : 'Loan';
}

class FnfItem {
  const FnfItem({required this.code, required this.label, required this.amount, this.itemId, this.status});

  factory FnfItem.fromJson(Map<String, dynamic> j) => FnfItem(
        code: _s(j['code']),
        label: _s(j['label']),
        amount: _opt(j['amount']) ?? '0.00',
        itemId: _opt(j['item_id']),
        status: _opt(j['status']),
      );

  final String code;
  final String label;
  final String amount;
  final String? itemId;
  final String? status;

  bool get isDeduction => code == 'OTHER_DEDUCTION';
}

class FnfOpenItem {
  const FnfOpenItem({
    required this.type,
    required this.id,
    required this.status,
    required this.flags,
    required this.action,
    this.period,
    this.code,
  });

  factory FnfOpenItem.fromJson(Map<String, dynamic> j) => FnfOpenItem(
        type: _s(j['type']),
        id: _s(j['id']),
        period: _opt(j['period']),
        code: _opt(j['code']),
        status: _s(j['status']),
        flags: _strings(j['flags']),
        action: _opt(j['action']) ?? 'keep',
      );

  final String type;
  final String id;
  final String? period;
  final String? code;
  final String status;
  final List<String> flags;

  /// `move` | `keep`.
  final String action;
}

class FnfGratuity {
  const FnfGratuity({
    required this.eligible,
    required this.years,
    required this.months,
    required this.days,
    required this.countedYears,
    required this.wage,
    required this.amount,
    required this.capped,
    required this.basis,
    this.note,
  });

  factory FnfGratuity.fromJson(Map<String, dynamic> j) {
    final svc = _m(j['service']);
    return FnfGratuity(
      eligible: j['eligible'] == true,
      years: _i(svc['years']),
      months: _i(svc['months']),
      days: _i(svc['days']),
      countedYears: _i(j['counted_years']),
      wage: _opt(j['wage']) ?? '0.00',
      amount: _opt(j['amount']) ?? '0.00',
      capped: j['capped'] == true,
      basis: _opt(j['basis']) ?? 'basic',
      note: _opt(j['note']),
    );
  }

  final bool eligible;
  final int years;
  final int months;
  final int days;
  final int countedYears;
  final String wage;
  final String amount;
  final bool capped;
  final String basis;
  final String? note;

  String get serviceLabel => '${years}y ${months}m ${days}d';
}

class FnfSummary {
  const FnfSummary({required this.earnings, required this.deductions, required this.netEstimate});

  factory FnfSummary.fromJson(Map<String, dynamic> j) => FnfSummary(
        earnings: _opt(j['earnings']) ?? '0.00',
        deductions: _opt(j['deductions']) ?? '0.00',
        netEstimate: _opt(j['net_estimate']) ?? '0.00',
      );

  final String earnings;
  final String deductions;
  final String netEstimate;
}

class FnfFinalPayslip {
  const FnfFinalPayslip({required this.id, required this.revision, required this.status});
  final String id;
  final int revision;
  final String status;
}

class Fnf {
  const Fnf({
    required this.id,
    required this.user,
    required this.status,
    required this.lastWorkingDay,
    required this.exitReason,
    required this.settlementPeriod,
    required this.runStatus,
    required this.notice,
    required this.encashments,
    required this.loans,
    required this.items,
    required this.moveOpenItems,
    required this.openItems,
    required this.warnings,
    required this.version,
    this.resignationDate,
    this.gratuity,
    this.summary,
    this.finalPayslip,
    this.remarks,
    this.createdBy,
    this.createdAt,
    this.submittedBy,
    this.submittedAt,
    this.decidedBy,
    this.decidedAt,
    this.decisionComment,
    this.lastEditedById,
    this.contributorIds = const [],
  });

  factory Fnf.fromJson(Map<String, dynamic> j) {
    final contributors = j['contributors'];
    return Fnf(
      id: _s(j['id']),
      user: _user(j['user']),
      status: _s(j['status']),
      resignationDate: _opt(j['resignation_date']),
      lastWorkingDay: _s(j['last_working_day']),
      exitReason: _opt(j['exit_reason']) ?? 'other',
      settlementPeriod: _s(j['settlement_period']),
      runStatus: _opt(j['run_status']) ?? 'none',
      notice: j['notice'] is Map ? FnfNotice.fromJson(_m(j['notice'])) : FnfNotice.empty,
      encashments: [for (final e in _list(j['encashments'])) FnfEncashment.fromJson(e)],
      loans: [for (final e in _list(j['loans'])) FnfLoan.fromJson(e)],
      items: [for (final e in _list(j['items'])) FnfItem.fromJson(e)],
      moveOpenItems: j['move_open_items'] != false,
      openItems: [for (final e in _list(j['open_items'])) FnfOpenItem.fromJson(e)],
      gratuity: j['gratuity'] is Map ? FnfGratuity.fromJson(_m(j['gratuity'])) : null,
      warnings: _strings(j['warnings']),
      summary: j['summary'] is Map ? FnfSummary.fromJson(_m(j['summary'])) : null,
      finalPayslip: j['final_payslip'] is Map
          ? FnfFinalPayslip(
              id: _s(_m(j['final_payslip'])['id']),
              revision: _i(_m(j['final_payslip'])['revision']),
              status: _s(_m(j['final_payslip'])['status']),
            )
          : null,
      remarks: _opt(j['remarks']),
      createdBy: EmployeeRef.parse(j['created_by']),
      createdAt: parseTimestamp(j['created_at']),
      submittedBy: EmployeeRef.parse(j['submitted_by']),
      submittedAt: parseTimestamp(j['submitted_at']),
      decidedBy: EmployeeRef.parse(j['decided_by']),
      decidedAt: parseTimestamp(j['decided_at']),
      decisionComment: _opt(j['decision_comment']),
      // Not in the contract today; read when the server adds them (maker-checker history).
      lastEditedById: j['last_edited_by'] is Map ? _opt(_m(j['last_edited_by'])['id']) : _opt(j['last_edited_by']),
      contributorIds: [
        if (contributors is List)
          for (final c in contributors)
            if (c is Map) _s(c['id']) else '$c',
      ],
      version: _i(j['version']) == 0 ? 1 : _i(j['version']),
    );
  }

  final String id;
  final EmployeeRef user;
  final String status;
  final String? resignationDate;
  final String lastWorkingDay;
  final String exitReason;
  final String settlementPeriod;
  final String runStatus;
  final FnfNotice notice;
  final List<FnfEncashment> encashments;
  final List<FnfLoan> loans;
  final List<FnfItem> items;
  final bool moveOpenItems;
  final List<FnfOpenItem> openItems;
  final FnfGratuity? gratuity;
  final List<String> warnings;
  final FnfSummary? summary;
  final FnfFinalPayslip? finalPayslip;
  final String? remarks;
  final EmployeeRef? createdBy;
  final DateTime? createdAt;
  final EmployeeRef? submittedBy;
  final DateTime? submittedAt;
  final EmployeeRef? decidedBy;
  final DateTime? decidedAt;
  final String? decisionComment;
  final String? lastEditedById;
  final List<String> contributorIds;
  final int version;

  bool get isDraft => status == 'draft';
  bool get isSubmitted => status == 'submitted';
  bool get isApproved => status == 'approved';
  bool get isSettled => status == 'settled';
  bool get isCancelled => status == 'cancelled';

  /// Only a draft can be edited; approved and settled settlements are read-only.
  bool get isEditable => isDraft;

  /// Approved / settled / cancelled: nothing can be changed any more except
  /// revoke (approved only).
  bool get isReadOnly => !isEditable;

  bool get isLive => isDraft || isSubmitted || isApproved;

  /// Amounts shown in the editor are quotes until approval.
  bool get amountsAreQuotes => !isApproved && !isSettled;

  bool get hasUnresolvedLoan => loans.any((l) => l.mode == null);
}

/// One row of the F&F list (no money). `version`, `createdBy` and `submittedBy`
/// are read when the server sends them.
class FnfListItem {
  const FnfListItem({
    required this.id,
    required this.user,
    required this.status,
    required this.lastWorkingDay,
    required this.settlementPeriod,
    required this.exitReason,
    required this.runStatus,
    required this.warnings,
    this.createdAt,
  });

  factory FnfListItem.fromJson(Map<String, dynamic> j) => FnfListItem(
        id: _s(j['id']),
        user: _user(j['user']),
        status: _s(j['status']),
        lastWorkingDay: _s(j['last_working_day']),
        settlementPeriod: _s(j['settlement_period']),
        exitReason: _opt(j['exit_reason']) ?? 'other',
        runStatus: _opt(j['run_status']) ?? 'none',
        warnings: _strings(j['warnings']),
        createdAt: parseTimestamp(j['created_at']),
      );

  final String id;
  final EmployeeRef user;
  final String status;
  final String lastWorkingDay;
  final String settlementPeriod;
  final String exitReason;
  final String runStatus;
  final List<String> warnings;
  final DateTime? createdAt;
}

class FnfQuery {
  const FnfQuery({this.status, this.q = ''});
  final String? status;
  final String q;

  FnfQuery copyWith({String? Function()? status, String? q}) =>
      FnfQuery(status: status == null ? this.status : status(), q: q ?? this.q);

  @override
  bool operator ==(Object other) => other is FnfQuery && other.status == status && other.q == q;

  @override
  int get hashCode => Object.hash(status, q);
}

/// `GET /admin/payroll/fnf/{id}/preview`: an estimate of the final payslip.
class FnfPreview {
  const FnfPreview({
    required this.estimate,
    required this.basisDays,
    required this.employedDays,
    required this.lopDays,
    required this.paidDays,
    required this.earnings,
    required this.deductions,
    required this.grossEarnings,
    required this.totalDeductions,
    required this.netPay,
    required this.warnings,
    required this.source,
  });

  factory FnfPreview.fromJson(Map<String, dynamic> j) {
    final d = _m(j['days']);
    final t = _m(j['totals']);
    return FnfPreview(
      estimate: j['estimate'] != false,
      basisDays: _dbl(d['basis_days']) ?? 0,
      employedDays: _dbl(d['employed_days']) ?? 0,
      lopDays: _dbl(d['lop_days']) ?? 0,
      paidDays: _dbl(d['paid_days']) ?? 0,
      earnings: [for (final e in _list(j['earnings'])) PayslipLine.fromJson(e)],
      deductions: [for (final e in _list(j['deductions'])) PayslipLine.fromJson(e)],
      grossEarnings: _opt(t['gross_earnings']) ?? '0.00',
      totalDeductions: _opt(t['total_deductions']) ?? '0.00',
      netPay: _opt(t['net_pay']) ?? '0.00',
      warnings: _strings(j['warnings']),
      source: _opt(j['source']) ?? 'structure',
    );
  }

  final bool estimate;
  final double basisDays;
  final double employedDays;
  final double lopDays;
  final double paidDays;
  final List<PayslipLine> earnings;
  final List<PayslipLine> deductions;
  final String grossEarnings;
  final String totalDeductions;
  final String netPay;
  final List<String> warnings;

  /// `structure` | `payslip`.
  final String source;

  bool get isNegative => isNegativeMoney(netPay);
}

/// `GET /me/fnf`: the employee's own settlement, no money.
class MyFnf {
  const MyFnf({
    required this.id,
    required this.status,
    required this.lastWorkingDay,
    required this.settlementPeriod,
    this.finalPayslipId,
  });

  factory MyFnf.fromJson(Map<String, dynamic> j) => MyFnf(
        id: _s(j['id']),
        status: _s(j['status']),
        lastWorkingDay: _s(j['last_working_day']),
        settlementPeriod: _s(j['settlement_period']),
        finalPayslipId: _opt(j['final_payslip_id']),
      );

  /// `{ "fnf": null }` -> null.
  static MyFnf? parse(Map<String, dynamic> j) => j['fnf'] is Map ? MyFnf.fromJson(_m(j['fnf'])) : null;

  final String id;

  /// `in_progress` | `settled`.
  final String status;
  final String lastWorkingDay;
  final String settlementPeriod;
  final String? finalPayslipId;

  bool get isSettled => status == 'settled';
}

// ---- inputs ------------------------------------------------------------------------------------

/// Largest number of days after today that a last working day may be set.
const int fnfMaxDaysAhead = 180;

class ExitValues {
  const ExitValues({
    required this.lastWorkingDay,
    required this.exitReason,
    required this.settlementPeriod,
    this.resignationDate,
    this.noticePeriodDays,
  });

  final DateTime? lastWorkingDay;
  final DateTime? resignationDate;
  final String exitReason;
  final String? settlementPeriod;

  /// Null = the payroll details value, else the company default.
  final int? noticePeriodDays;
}

/// Client-side check of the exit dates (the server decides). Returns field ->
/// message; empty = fine.
Map<String, String> validateExit(ExitValues v, DateTime today) {
  final errors = <String, String>{};
  final lwd = v.lastWorkingDay;
  if (lwd == null) {
    errors['last_working_day'] = 'Choose the last working day';
  } else if (lwd.isAfter(DateTime(today.year, today.month, today.day + fnfMaxDaysAhead))) {
    errors['last_working_day'] = 'The last working day can be at most $fnfMaxDaysAhead days from today';
  }
  final res = v.resignationDate;
  if (res != null && lwd != null && res.isAfter(lwd)) {
    errors['resignation_date'] = 'The resignation date cannot be after the last working day';
  }
  final p = v.settlementPeriod;
  if (p == null || p.isEmpty) {
    errors['settlement_period'] = 'Choose the settlement month';
  } else if (lwd != null) {
    final first = monthKey(lwd);
    final last = shiftMonth(first, 3);
    if (p.compareTo(first) < 0 || p.compareTo(last) > 0) {
      errors['settlement_period'] = 'Choose the month of the last working day or one of the next 3 months';
    }
  }
  final n = v.noticePeriodDays;
  if (n != null && (n < 0 || n > 365)) errors['notice_period_days'] = 'Notice days must be 0 to 365';
  return errors;
}

/// Body of `POST /admin/payroll/employees/{id}/fnf`.
Map<String, Object?> fnfCreateBody(ExitValues v, {String? remarks}) => {
      'resignation_date': v.resignationDate == null ? null : ymd(v.resignationDate!),
      'last_working_day': ymd(v.lastWorkingDay!),
      'exit_reason': v.exitReason,
      'notice_period_days': v.noticePeriodDays,
      'settlement_period': v.settlementPeriod,
      'remarks': (remarks == null || remarks.trim().isEmpty) ? null : remarks.trim(),
    };

class FnfEncashmentInput {
  const FnfEncashmentInput({required this.leaveTypeId, required this.year, required this.days});
  final String leaveTypeId;
  final int year;
  final double days;

  Map<String, Object?> toJson() => {'leave_type_id': leaveTypeId, 'year': year, 'days': days};
}

class FnfLoanInput {
  const FnfLoanInput({required this.loanId, required this.mode, this.paidOn, this.reference});
  final String loanId;
  final String mode;
  final String? paidOn;
  final String? reference;

  Map<String, Object?> toJson() => mode == 'outside'
      ? {'loan_id': loanId, 'mode': 'outside', 'paid_on': paidOn, 'reference': reference}
      : {'loan_id': loanId, 'mode': 'payroll'};
}

class FnfItemInput {
  const FnfItemInput({required this.code, required this.label, required this.amount});
  final String code;
  final String label;
  final String amount;

  Map<String, Object?> toJson() => {'code': code, 'label': label.trim(), 'amount': amount};
}

/// Body of `PUT /admin/payroll/fnf/{id}`. Lists replace the stored ones as a
/// whole, so the editor sends every section it manages.
class FnfEdit {
  const FnfEdit({
    this.exit,
    this.waived,
    this.overrideDays,
    this.overrideReason,
    this.clearOverride = false,
    this.encashments,
    this.loans,
    this.items,
    this.moveOpenItems,
    this.remarks,
  });

  final ExitValues? exit;
  final bool? waived;
  final int? overrideDays;
  final String? overrideReason;
  final bool clearOverride;
  final List<FnfEncashmentInput>? encashments;
  final List<FnfLoanInput>? loans;
  final List<FnfItemInput>? items;
  final bool? moveOpenItems;
  final String? remarks;

  Map<String, Object?> toJson(int expectedVersion) {
    final e = exit;
    return {
      'expected_version': expectedVersion,
      if (e != null) ...{
        'resignation_date': e.resignationDate == null ? null : ymd(e.resignationDate!),
        if (e.lastWorkingDay != null) 'last_working_day': ymd(e.lastWorkingDay!),
        'exit_reason': e.exitReason,
        if (e.noticePeriodDays != null) 'notice_period_days': e.noticePeriodDays,
        if (e.settlementPeriod != null) 'settlement_period': e.settlementPeriod,
      },
      if (waived != null || overrideDays != null || clearOverride)
        'notice': {
          'waived': waived ?? false,
          'override': overrideDays == null || clearOverride
              ? null
              : {'days': overrideDays, 'reason': (overrideReason ?? '').trim()},
        },
      if (encashments != null) 'encashments': [for (final x in encashments!) x.toJson()],
      if (loans != null) 'loans': [for (final x in loans!) x.toJson()],
      if (items != null) 'items': [for (final x in items!) x.toJson()],
      if (moveOpenItems != null) 'move_open_items': moveOpenItems,
      if (remarks != null) 'remarks': remarks!.trim().isEmpty ? null : remarks!.trim(),
    };
  }
}

// ---- validation helpers ------------------------------------------------------------------------

/// Reason / comment: 3..200 characters.
String? validateFnfReason(String? v, {String label = 'A reason'}) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return '$label is required (3 to 200 characters)';
  if (t.length < 3) return 'At least 3 characters';
  if (t.length > 200) return 'At most 200 characters';
  return null;
}

/// Leave days to encash: multiples of 0.5, 0.5..999.5.
double? parseEncashDays(String? text) {
  final t = (text ?? '').trim();
  if (!RegExp(r'^\d{1,3}(\.\d)?$').hasMatch(t)) return null;
  final d = double.parse(t);
  if (d <= 0 || (d * 2) % 1 != 0) return null;
  return d;
}

String? validateEncashDays(String? v, {double? available}) {
  final d = parseEncashDays(v);
  if (d == null) return 'Enter days in steps of 0.5, for example 2 or 2.5';
  if (available != null && d > available) return 'Only ${formatDays1(available)} days are available';
  return null;
}

String formatDays1(double d) => d == d.roundToDouble() ? d.toStringAsFixed(0) : d.toStringAsFixed(1);

final RegExp _loanRefRe = RegExp(r'^[A-Za-z0-9 ._/-]{1,60}$');

String? validatePaymentReference(String? v) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return 'The payment reference is required';
  if (!_loanRefRe.hasMatch(t)) return 'Use letters, digits, space and . _ / - only (60 at most)';
  return null;
}

/// Overall check before saving: lists, limits and overrides. Returns a message
/// or null.
String? validateFnfPlan({
  required int encashments,
  required int loans,
  required int items,
}) {
  if (encashments > 10) return 'At most 10 leave encashment lines';
  if (loans > 20) return 'At most 20 loans';
  if (items > 20) return 'At most 20 other items';
  return null;
}

// ---- error messages ----------------------------------------------------------------------------

/// Plain-language name of the part named by `details[0].field` of an approve
/// failure (`notice`, `encashments[0]`, `loans[1]`, `items[2]`).
String? fnfPartLabel(String? field) {
  if (field == null || field.isEmpty) return null;
  if (field == 'notice') return 'the notice shortfall';
  final m = RegExp(r'^(encashments|loans|items)\[(\d+)\]$').firstMatch(field);
  if (m == null) return null;
  final n = int.parse(m[2]!) + 1;
  return switch (m[1]) {
    'encashments' => 'leave encashment line $n',
    'loans' => 'loan $n',
    _ => 'other item $n',
  };
}

/// Message for an error of an F&F call. Falls back to [payrollErrorMessage].
String fnfErrorMessage(ApiException e) {
  final part = e.details.isEmpty ? null : fnfPartLabel(e.details.first.field);
  final where = part == null ? '' : ' Check $part.';
  switch (e.code) {
    case 'NOT_DRAFT':
      return 'Only a draft settlement can be changed. Reload to see its state.';
    case 'NOT_SUBMITTED':
      return 'This settlement is no longer waiting for approval. Reload to see its state.';
    case 'INVALID_DATE':
      return 'Check the dates: the last working day cannot be before the joining date or more than '
          '$fnfMaxDaysAhead days ahead, and the resignation date cannot be after it.';
    case 'INVALID_PERIOD':
      return 'Choose a valid settlement month: the month of the last working day or one of the next 3. '
          'It must also be a month that can still take pay items.';
    case 'PERIOD_LOCKED':
    case 'ITEM_LOCKED':
    case 'LOAN_NOT_ACTIVE':
    case 'INSUFFICIENT_BALANCE':
    case 'NOT_ENCASHABLE':
    case 'TOO_MANY_ITEMS':
      return '${payrollErrorMessage(e)}$where';
    case 'STRUCTURE_MISSING':
      return 'This employee has no salary structure for the month of the last working day.';
    case 'FNF_NOT_REVOCABLE':
      return 'This settlement cannot be revoked: it is not approved, or a pay item of it is already being paid. '
          'Reopen the payroll run first if needed.';
    case 'VALIDATION_ERROR':
      final msgs = e.details
          .map((d) => d.message.isEmpty ? '' : (fnfPartLabel(d.field) == null ? d.message : '${fnfPartLabel(d.field)}: ${d.message}'))
          .where((m) => m.isNotEmpty);
      return msgs.isEmpty ? 'Please check the details and try again.' : msgs.join('\n');
    default:
      return payrollErrorMessage(e);
  }
}

/// Codes after which the shown settlement is out of date and must be reloaded.
bool isStaleFnfError(ApiException e) => const {
      'VERSION_CONFLICT',
      'NOT_DRAFT',
      'NOT_SUBMITTED',
      'FNF_NOT_REVOCABLE',
      'FNF_NEGATIVE',
      'LOAN_NOT_ACTIVE',
      'ITEM_LOCKED',
      'PERIOD_LOCKED',
    }.contains(e.code);
