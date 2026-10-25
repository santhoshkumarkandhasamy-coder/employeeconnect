import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import 'payroll_money.dart';

/// api-contract.md section 13 (payroll and payslips). Nothing here is cached or
/// persisted: payroll data lives only in autoDispose Riverpod state.

// ---- permissions -------------------------------------------------------------

abstract final class PayrollPerm {
  static const viewOwn = 'payroll.view_own';
  static const viewAll = 'payroll.view_all';
  static const manage = 'payroll.manage';
  static const publish = 'payroll.publish';
}

bool canViewOwnPayslips(List<String> p) => p.contains(PayrollPerm.viewOwn);
bool canOpenPayroll(List<String> p) => p.contains(PayrollPerm.viewAll);
bool canManagePayroll(List<String> p) => p.contains(PayrollPerm.manage);
bool canPublishPayroll(List<String> p) => p.contains(PayrollPerm.publish);

// ---- parsing helpers -----------------------------------------------------------

String _s(Object? v) => v == null ? '' : '$v';
String? _opt(Object? v) => v is String && v.isNotEmpty ? v : null;
double _d(Object? v) => v is num ? v.toDouble() : (double.tryParse('$v') ?? 0);
int _i(Object? v) => v is num ? v.toInt() : (int.tryParse('$v') ?? 0);
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

/// `29.5` -> `29.5`, `30.0` -> `30`.
String formatDays(double d) => d == d.roundToDouble() ? '${d.toInt()}' : d.toStringAsFixed(1);

/// `2026-10` -> `October 2026` (or the input when malformed).
String periodTitle(String period) => monthTitle(period);

// ---- employee: payslip list ------------------------------------------------------

/// One row of `GET /payslips`. Carries no amounts by contract (D10).
class PayslipListItem {
  const PayslipListItem({
    required this.id,
    required this.period,
    required this.revision,
    required this.revised,
    this.publishedAt,
    this.payDate,
  });
  final String id;
  final String period;
  final int revision;
  final bool revised;
  final DateTime? publishedAt;
  final String? payDate;

  factory PayslipListItem.fromJson(Map<String, dynamic> j) => PayslipListItem(
        id: _s(j['id']),
        period: _s(j['period']),
        revision: _i(j['revision']),
        revised: j['revised'] == true,
        publishedAt: parseTimestamp(j['published_at']),
        payDate: _opt(j['pay_date']),
      );

  static List<PayslipListItem> parseList(Map<String, dynamic> j) =>
      [for (final e in _list(j['items'])) PayslipListItem.fromJson(e)];
}

// ---- payslip (employee and admin) ---------------------------------------------------

class PayslipLine {
  const PayslipLine({required this.code, required this.label, required this.amount, this.monthly});
  final String code;
  final String label;
  final String amount;

  /// Structure amount before proration; null for adjustments.
  final String? monthly;

  factory PayslipLine.fromJson(Map<String, dynamic> j) => PayslipLine(
        code: _s(j['code']),
        label: _s(j['label']),
        amount: _s(j['amount']),
        monthly: _opt(j['monthly']),
      );
}

class PayslipEmployee {
  const PayslipEmployee({
    required this.employeeId,
    required this.fullName,
    required this.paymentMode,
    this.department,
    this.designation,
    this.joinDate,
    this.pan,
    this.uan,
    this.esiIpNumber,
    this.bankName,
    this.bankAccount,
  });
  final String employeeId;
  final String fullName;
  final String paymentMode;
  final String? department;
  final String? designation;
  final String? joinDate;
  final String? pan;
  final String? uan;
  final String? esiIpNumber;
  final String? bankName;
  final String? bankAccount;

  factory PayslipEmployee.fromJson(Map<String, dynamic> j) => PayslipEmployee(
        employeeId: _s(j['employee_id']),
        fullName: _s(j['full_name']),
        paymentMode: _opt(j['payment_mode']) ?? 'bank_transfer',
        department: _opt(j['department']),
        designation: _opt(j['designation']),
        joinDate: _opt(j['join_date']),
        pan: _opt(j['pan']),
        uan: _opt(j['uan']),
        esiIpNumber: _opt(j['esi_ip_number']),
        bankName: _opt(j['bank_name']),
        bankAccount: _opt(j['bank_account']),
      );
}

class PayslipDays {
  const PayslipDays({
    required this.basisDays,
    required this.employedDays,
    required this.lopDays,
    required this.paidDays,
  });
  final double basisDays;
  final double employedDays;
  final double lopDays;
  final double paidDays;

  factory PayslipDays.fromJson(Map<String, dynamic> j) => PayslipDays(
        basisDays: _d(j['basis_days']),
        employedDays: _d(j['employed_days']),
        lopDays: _d(j['lop_days']),
        paidDays: _d(j['paid_days']),
      );
}

class PayslipTotals {
  const PayslipTotals({
    required this.grossEarnings,
    required this.totalDeductions,
    required this.netPay,
    this.netPayWords,
  });
  final String grossEarnings;
  final String totalDeductions;
  final String netPay;
  final String? netPayWords;

  static PayslipTotals? parse(Object? v) {
    if (v is! Map) return null;
    final j = v.cast<String, dynamic>();
    return PayslipTotals(
      grossEarnings: _s(j['gross_earnings']),
      totalDeductions: _s(j['total_deductions']),
      netPay: _s(j['net_pay']),
      netPayWords: _opt(j['net_pay_words']),
    );
  }
}

class RevisionInfo {
  const RevisionInfo({this.reason, this.supersedesId, this.supersededById});
  final String? reason;
  final String? supersedesId;
  final String? supersededById;

  static RevisionInfo? parse(Object? v) {
    if (v is! Map) return null;
    final j = v.cast<String, dynamic>();
    return RevisionInfo(
      reason: _opt(j['reason']),
      supersedesId: _opt(j['supersedes_id']),
      supersededById: _opt(j['superseded_by_id']),
    );
  }
}

class PayslipLop {
  const PayslipLop({
    required this.auto,
    this.override,
    this.overrideReason,
    this.unpaidLeave = 0,
    this.absent = 0,
    this.halfDayAttendance = 0,
    this.notEmployed = 0,
    this.pendingLeaveDays = 0,
  });
  final double auto;
  final double? override;
  final String? overrideReason;
  final double unpaidLeave;
  final double absent;
  final double halfDayAttendance;
  final double notEmployed;
  final double pendingLeaveDays;

  factory PayslipLop.fromJson(Map<String, dynamic> j) {
    final b = _m(j['breakdown']);
    final o = j['override'];
    return PayslipLop(
      auto: _d(j['auto']),
      override: o is num ? o.toDouble() : null,
      overrideReason: _opt(j['override_reason']),
      unpaidLeave: _d(b['unpaid_leave']),
      absent: _d(b['absent']),
      halfDayAttendance: _d(b['half_day_attendance']),
      notEmployed: _d(b['not_employed']),
      pendingLeaveDays: _d(b['pending_leave_days']),
    );
  }
}

class Adjustment {
  const Adjustment({required this.code, required this.label, required this.amount});
  final String code;
  final String label;
  final String amount;

  factory Adjustment.fromJson(Map<String, dynamic> j) =>
      Adjustment(code: _s(j['code']), label: _s(j['label']), amount: _s(j['amount']));

  Map<String, Object?> toJson() => {'code': code, 'label': label, 'amount': amount};

  @override
  bool operator ==(Object other) =>
      other is Adjustment && other.code == code && other.label == label && other.amount == amount;

  @override
  int get hashCode => Object.hash(code, label, amount);
}

class PayslipInputs {
  const PayslipInputs({this.tds, this.ptOverride, this.adjustments = const [], this.note});
  final String? tds;
  final String? ptOverride;
  final List<Adjustment> adjustments;
  final String? note;

  factory PayslipInputs.fromJson(Map<String, dynamic> j) => PayslipInputs(
        tds: _opt(j['tds']),
        ptOverride: _opt(j['pt_override']),
        adjustments: [for (final a in _list(j['adjustments'])) Adjustment.fromJson(a)],
        note: _opt(j['note']),
      );
}

/// `Payslip` (13.2) and `AdminPayslip` (13.6). The admin-only members are
/// null / empty for the employee shape.
class Payslip {
  const Payslip({
    required this.id,
    required this.period,
    required this.revision,
    required this.status,
    required this.companyName,
    required this.employee,
    required this.days,
    required this.earnings,
    required this.deductions,
    this.publishedAt,
    this.payDate,
    this.companyAddress,
    this.employerContributions,
    this.totals,
    this.revisionInfo,
    this.runId,
    this.user,
    this.hold = false,
    this.version = 0,
    this.lop,
    this.inputs,
    this.errors = const [],
    this.warnings = const [],
  });

  final String id;
  final String period;
  final int revision;
  final String status;
  final DateTime? publishedAt;
  final String? payDate;
  final String companyName;
  final String? companyAddress;
  final PayslipEmployee employee;
  final PayslipDays days;
  final List<PayslipLine> earnings;
  final List<PayslipLine> deductions;
  final List<PayslipLine>? employerContributions;
  final PayslipTotals? totals;
  final RevisionInfo? revisionInfo;

  // admin only
  final String? runId;
  final EmployeeRef? user;
  final bool hold;
  final int version;
  final PayslipLop? lop;
  final PayslipInputs? inputs;
  final List<String> errors;
  final List<String> warnings;

  bool get isAdminShape => runId != null;
  bool get isDraft => status == 'draft';
  bool get isProcessed => status == 'processed';
  bool get isPublished => status == 'published';
  bool get isSuperseded => status == 'superseded';

  factory Payslip.fromJson(Map<String, dynamic> j) {
    final company = _m(j['company']);
    final emp = _m(j['employee']);
    final employer = j['employer_contributions'];
    final admin = j['run_id'] != null;
    return Payslip(
      id: _s(j['id']),
      period: _s(j['period']),
      revision: _i(j['revision']) == 0 ? 1 : _i(j['revision']),
      status: _opt(j['status']) ?? 'published',
      publishedAt: parseTimestamp(j['published_at']),
      payDate: _opt(j['pay_date']),
      companyName: _s(company['name']),
      companyAddress: _opt(company['address']),
      employee: PayslipEmployee.fromJson(emp),
      days: PayslipDays.fromJson(_m(j['days'])),
      earnings: [for (final e in _list(j['earnings'])) PayslipLine.fromJson(e)],
      deductions: [for (final e in _list(j['deductions'])) PayslipLine.fromJson(e)],
      employerContributions:
          employer is List ? [for (final e in _list(employer)) PayslipLine.fromJson(e)] : null,
      totals: PayslipTotals.parse(j['totals']),
      revisionInfo: RevisionInfo.parse(j['revision_info']),
      runId: admin ? _s(j['run_id']) : null,
      user: admin ? EmployeeRef.parse(j['user']) : null,
      hold: j['hold'] == true,
      version: _i(j['version']),
      lop: j['lop'] is Map ? PayslipLop.fromJson(_m(j['lop'])) : null,
      inputs: j['inputs'] is Map ? PayslipInputs.fromJson(_m(j['inputs'])) : null,
      errors: _strings(j['errors']),
      warnings: _strings(j['warnings']),
    );
  }
}

// ---- admin: runs ----------------------------------------------------------------------

class PayrollRunItem {
  const PayrollRunItem({
    required this.id,
    required this.period,
    required this.status,
    required this.employees,
    required this.netPayTotal,
    this.publishedAt,
  });
  final String id;
  final String period;
  final String status;
  final int employees;
  final String netPayTotal;
  final DateTime? publishedAt;

  factory PayrollRunItem.fromJson(Map<String, dynamic> j) => PayrollRunItem(
        id: _s(j['id']),
        period: _s(j['period']),
        status: _opt(j['status']) ?? 'draft',
        employees: _i(j['employees']),
        netPayTotal: _s(j['net_pay_total']),
        publishedAt: parseTimestamp(j['published_at']),
      );

  static List<PayrollRunItem> parseList(Map<String, dynamic> j) =>
      [for (final e in _list(j['items'])) PayrollRunItem.fromJson(e)];
}

class RunCounts {
  const RunCounts({
    this.employees = 0,
    this.draft = 0,
    this.processed = 0,
    this.published = 0,
    this.held = 0,
    this.errors = 0,
    this.warnings = 0,
    this.openRevisions = 0,
  });
  final int employees;
  final int draft;
  final int processed;
  final int published;
  final int held;
  final int errors;
  final int warnings;
  final int openRevisions;

  factory RunCounts.fromJson(Map<String, dynamic> j) => RunCounts(
        employees: _i(j['employees']),
        draft: _i(j['draft']),
        processed: _i(j['processed']),
        published: _i(j['published']),
        held: _i(j['held']),
        errors: _i(j['errors']),
        warnings: _i(j['warnings']),
        openRevisions: _i(j['open_revisions']),
      );
}

class RunTotals {
  const RunTotals({
    this.grossEarnings = '0.00',
    this.totalDeductions = '0.00',
    this.netPay = '0.00',
    this.employerPf = '0.00',
    this.employerEsi = '0.00',
  });
  final String grossEarnings;
  final String totalDeductions;
  final String netPay;
  final String employerPf;
  final String employerEsi;

  factory RunTotals.fromJson(Map<String, dynamic> j) => RunTotals(
        grossEarnings: _opt(j['gross_earnings']) ?? '0.00',
        totalDeductions: _opt(j['total_deductions']) ?? '0.00',
        netPay: _opt(j['net_pay']) ?? '0.00',
        employerPf: _opt(j['employer_pf']) ?? '0.00',
        employerEsi: _opt(j['employer_esi']) ?? '0.00',
      );
}

class PayrollRun {
  const PayrollRun({
    required this.id,
    required this.period,
    required this.status,
    required this.counts,
    required this.totals,
    this.payDate,
    this.attendanceCutoff,
    this.publishedAt,
    this.processedAt,
  });
  final String id;
  final String period;
  final String status;
  final String? payDate;
  final String? attendanceCutoff;
  final RunCounts counts;
  final RunTotals totals;
  final DateTime? publishedAt;
  final DateTime? processedAt;

  bool get isDraft => status == 'draft';
  bool get isProcessed => status == 'processed';
  bool get isPublished => status == 'published';

  factory PayrollRun.fromJson(Map<String, dynamic> j) => PayrollRun(
        id: _s(j['id']),
        period: _s(j['period']),
        status: _opt(j['status']) ?? 'draft',
        payDate: _opt(j['pay_date']),
        attendanceCutoff: _opt(j['attendance_cutoff']),
        counts: RunCounts.fromJson(_m(j['counts'])),
        totals: RunTotals.fromJson(_m(j['totals'])),
        publishedAt: parseTimestamp(j['published_at']),
        processedAt: parseTimestamp(j['processed_at']),
      );
}

/// A row of `GET /admin/payroll/runs/{id}/payslips`. Draft rows with an error
/// have null money fields.
class RunPayslipRow {
  const RunPayslipRow({
    required this.id,
    required this.revision,
    required this.status,
    required this.hold,
    required this.paidDays,
    required this.lopDays,
    this.user,
    this.grossEarnings,
    this.totalDeductions,
    this.netPay,
    this.errors = const [],
    this.warnings = const [],
  });
  final String id;
  final EmployeeRef? user;
  final int revision;
  final String status;
  final bool hold;
  final double paidDays;
  final double lopDays;
  final String? grossEarnings;
  final String? totalDeductions;
  final String? netPay;
  final List<String> errors;
  final List<String> warnings;

  factory RunPayslipRow.fromJson(Map<String, dynamic> j) => RunPayslipRow(
        id: _s(j['id']),
        user: EmployeeRef.parse(j['user']),
        revision: _i(j['revision']) == 0 ? 1 : _i(j['revision']),
        status: _opt(j['status']) ?? 'draft',
        hold: j['hold'] == true,
        paidDays: _d(j['paid_days']),
        lopDays: _d(j['lop_days']),
        grossEarnings: _opt(j['gross_earnings']),
        totalDeductions: _opt(j['total_deductions']),
        netPay: _opt(j['net_pay']),
        errors: _strings(j['errors']),
        warnings: _strings(j['warnings']),
      );
}

/// Filter of the run payslip list. Value equality so it can key a provider.
class RunPayslipQuery {
  const RunPayslipQuery(this.runId, {this.q = '', this.flag});
  final String runId;
  final String q;

  /// `error`, `warning`, `hold` or null.
  final String? flag;

  @override
  bool operator ==(Object other) =>
      other is RunPayslipQuery && other.runId == runId && other.q == q && other.flag == flag;

  @override
  int get hashCode => Object.hash(runId, q, flag);
}

// ---- admin: employees, structures, details ---------------------------------------------

class CurrentStructure {
  const CurrentStructure({required this.id, required this.effectiveFrom, required this.monthlyGross});
  final String id;
  final String effectiveFrom;
  final String monthlyGross;

  static CurrentStructure? parse(Object? v) {
    if (v is! Map) return null;
    final j = v.cast<String, dynamic>();
    return CurrentStructure(
      id: _s(j['id']),
      effectiveFrom: _s(j['effective_from']),
      monthlyGross: _s(j['monthly_gross']),
    );
  }
}

class PayrollEmployee {
  const PayrollEmployee({
    required this.user,
    required this.status,
    required this.includeInPayroll,
    this.currentStructure,
    this.flags = const [],
    this.department,
  });
  final EmployeeRef user;
  final String? department;
  final String status;
  final bool includeInPayroll;
  final CurrentStructure? currentStructure;
  final List<String> flags;

  bool get isActive => status == 'active';

  factory PayrollEmployee.fromJson(Map<String, dynamic> j) {
    final u = _m(j['user']);
    return PayrollEmployee(
      user: EmployeeRef.parse(u) ?? const EmployeeRef(id: '', employeeId: '', fullName: ''),
      department: _opt(u['department']),
      status: _opt(j['status']) ?? 'active',
      includeInPayroll: j['include_in_payroll'] != false,
      currentStructure: CurrentStructure.parse(j['current_structure']),
      flags: _strings(j['flags']),
    );
  }
}

class PayrollEmployeeQuery {
  const PayrollEmployeeQuery({this.q = '', this.hasStructure});
  final String q;
  final bool? hasStructure;

  @override
  bool operator ==(Object other) =>
      other is PayrollEmployeeQuery && other.q == q && other.hasStructure == hasStructure;

  @override
  int get hashCode => Object.hash(q, hasStructure);
}

class AllowanceLine {
  const AllowanceLine({
    required this.code,
    required this.name,
    required this.amount,
    this.prorate = true,
    this.esiWage = true,
  });
  final String code;
  final String name;
  final String amount;
  final bool prorate;
  final bool esiWage;

  factory AllowanceLine.fromJson(Map<String, dynamic> j) => AllowanceLine(
        code: _s(j['code']),
        name: _s(j['name']),
        amount: _s(j['amount']),
        prorate: j['prorate'] != false,
        esiWage: j['esi_wage'] != false,
      );

  Map<String, Object?> toJson() =>
      {'code': code, 'name': name, 'amount': amount, 'prorate': prorate, 'esi_wage': esiWage};
}

class FixedDeductionLine {
  const FixedDeductionLine({required this.code, required this.name, required this.amount});
  final String code;
  final String name;
  final String amount;

  factory FixedDeductionLine.fromJson(Map<String, dynamic> j) =>
      FixedDeductionLine(code: _s(j['code']), name: _s(j['name']), amount: _s(j['amount']));

  Map<String, Object?> toJson() => {'code': code, 'name': name, 'amount': amount};
}

class SalaryStructure {
  const SalaryStructure({
    required this.id,
    required this.userId,
    required this.effectiveFrom,
    required this.basic,
    required this.hra,
    required this.allowances,
    required this.fixedDeductions,
    required this.tdsMonthly,
    required this.pfApplicable,
    required this.pfOnFullBasic,
    required this.esiApplicable,
    required this.ptApplicable,
    required this.monthlyGross,
    required this.inUse,
    this.effectiveTo,
  });
  final String id;
  final String userId;
  final String effectiveFrom;
  final String? effectiveTo;
  final String basic;
  final String hra;
  final List<AllowanceLine> allowances;
  final List<FixedDeductionLine> fixedDeductions;
  final String tdsMonthly;
  final bool pfApplicable;
  final bool pfOnFullBasic;
  final bool esiApplicable;
  final bool ptApplicable;
  final String monthlyGross;
  final bool inUse;

  factory SalaryStructure.fromJson(Map<String, dynamic> j) => SalaryStructure(
        id: _s(j['id']),
        userId: _s(j['user_id']),
        effectiveFrom: _s(j['effective_from']),
        effectiveTo: _opt(j['effective_to']),
        basic: _opt(j['basic']) ?? '0.00',
        hra: _opt(j['hra']) ?? '0.00',
        allowances: [for (final a in _list(j['allowances'])) AllowanceLine.fromJson(a)],
        fixedDeductions: [
          for (final a in _list(j['fixed_deductions'])) FixedDeductionLine.fromJson(a),
        ],
        tdsMonthly: _opt(j['tds_monthly']) ?? '0.00',
        pfApplicable: j['pf_applicable'] != false,
        pfOnFullBasic: j['pf_on_full_basic'] == true,
        esiApplicable: j['esi_applicable'] != false,
        ptApplicable: j['pt_applicable'] != false,
        monthlyGross: _opt(j['monthly_gross']) ?? '0.00',
        inUse: j['in_use'] == true,
      );

  static List<SalaryStructure> parseList(Map<String, dynamic> j) =>
      [for (final e in _list(j['items'])) SalaryStructure.fromJson(e)];
}

/// Text of the structure form, validated and turned into the request body.
class StructureInput {
  const StructureInput({
    required this.effectiveFrom,
    required this.basic,
    required this.hra,
    required this.allowances,
    required this.fixedDeductions,
    required this.tdsMonthly,
    required this.pfApplicable,
    required this.pfOnFullBasic,
    required this.esiApplicable,
    required this.ptApplicable,
  });
  final String effectiveFrom;
  final String basic;
  final String hra;
  final List<AllowanceLine> allowances;
  final List<FixedDeductionLine> fixedDeductions;
  final String tdsMonthly;
  final bool pfApplicable;
  final bool pfOnFullBasic;
  final bool esiApplicable;
  final bool ptApplicable;

  /// Same value the server reports as `monthly_gross` (basic + hra + allowances).
  String get monthlyGross =>
      paiseToMoney(moneyToPaise(basic) + moneyToPaise(hra) + sumPaise(allowances.map((a) => a.amount)));

  Map<String, Object?> toJson() => {
        'effective_from': effectiveFrom,
        'basic': basic,
        'hra': hra,
        'allowances': [for (final a in allowances) a.toJson()],
        'fixed_deductions': [for (final d in fixedDeductions) d.toJson()],
        'tds_monthly': tdsMonthly,
        'pf_applicable': pfApplicable,
        'pf_on_full_basic': pfOnFullBasic,
        'esi_applicable': esiApplicable,
        'pt_applicable': ptApplicable,
      };
}

/// `GET /admin/payroll/employees/{id}/details` (masked unless revealed).
class PayrollDetails {
  const PayrollDetails({
    required this.userId,
    required this.paymentMode,
    required this.includeInPayroll,
    required this.revealed,
    this.designation,
    this.bankName,
    this.bankAccount,
    this.bankIfsc,
    this.pan,
    this.uan,
    this.esiIpNumber,
    this.updatedAt,
  });
  final String userId;
  final String? designation;
  final String paymentMode;
  final bool includeInPayroll;
  final String? bankName;
  final String? bankAccount;
  final String? bankIfsc;
  final String? pan;
  final String? uan;
  final String? esiIpNumber;
  final bool revealed;
  final DateTime? updatedAt;

  factory PayrollDetails.fromJson(Map<String, dynamic> j) => PayrollDetails(
        userId: _s(j['user_id']),
        designation: _opt(j['designation']),
        paymentMode: _opt(j['payment_mode']) ?? 'bank_transfer',
        includeInPayroll: j['include_in_payroll'] != false,
        bankName: _opt(j['bank_name']),
        bankAccount: _opt(j['bank_account']),
        bankIfsc: _opt(j['bank_ifsc']),
        pan: _opt(j['pan']),
        uan: _opt(j['uan']),
        esiIpNumber: _opt(j['esi_ip_number']),
        revealed: j['revealed'] == true,
        updatedAt: parseTimestamp(j['updated_at']),
      );
}

// ---- settings ---------------------------------------------------------------------------

/// One Professional Tax slab (`payroll.pt_slabs`). `months` null = every month.
class PtSlab {
  const PtSlab({required this.minGross, required this.amount, this.maxGross, this.months});
  final String minGross;
  final String? maxGross;
  final String amount;
  final List<int>? months;

  factory PtSlab.fromJson(Map<String, dynamic> j) {
    final m = j['months'];
    return PtSlab(
      minGross: _s(j['min_gross']),
      maxGross: _opt(j['max_gross']),
      amount: _s(j['amount']),
      months: m is List ? [for (final e in m) _i(e)] : null,
    );
  }

  Map<String, Object?> toJson() => {
        'min_gross': minGross,
        'max_gross': maxGross,
        'amount': amount,
        'months': months,
      };

  static List<PtSlab> parseList(Object? v) => [for (final e in _list(v)) PtSlab.fromJson(e)];
}

// ---- errors -------------------------------------------------------------------------------

const Map<String, String> payrollWarningLabels = {
  'NO_SHIFT': 'No shift assigned',
  'PENDING_LEAVE': 'Pending leave request',
  'LOP_OVERRIDDEN': 'LOP overridden',
  'ZERO_PAID_DAYS': 'Zero paid days',
  'NO_BANK_DETAILS': 'No bank details',
  'NO_PAN': 'No PAN',
  'STRUCTURE_CHANGED': 'Structure changed',
  'NOT_ELIGIBLE': 'No longer eligible',
};

const Map<String, String> payrollErrorLabels = {
  'STRUCTURE_MISSING': 'No salary structure',
  'NEGATIVE_NET_PAY': 'Net pay is negative',
};

String payrollFlagLabel(String code) =>
    payrollErrorLabels[code] ?? payrollWarningLabels[code] ?? code;

const Map<String, String> payrollStatusLabels = {
  'draft': 'Draft',
  'processed': 'Processed',
  'published': 'Published',
  'superseded': 'Superseded',
};

String payrollStatusLabel(String s) => payrollStatusLabels[s] ?? s;

/// User-facing text for payroll errors (contract 13.7 and section 0).
String payrollErrorMessage(ApiException e) {
  switch (e.code) {
    case 'PAYROLL_RUN_EXISTS':
      return 'A payroll run for this month already exists.';
    case 'PAYROLL_LOCKED':
      return 'This payroll is locked (already published, or not ready for this step). '
          'Reload to see its current state.';
    case 'PAYROLL_BLOCKED':
      final lines = e.details.map((d) => '${d.field}: ${payrollFlagLabel(d.message)}');
      return lines.isEmpty
          ? 'Some payslips have errors, so payroll cannot be processed.'
          : 'Payroll cannot be processed yet:\n${lines.join('\n')}';
    case 'NOTHING_TO_PUBLISH':
      return 'There is nothing to publish. Process the payroll first.';
    case 'STRUCTURE_EXISTS':
      return 'A salary structure already exists from that month.';
    case 'STRUCTURE_IN_USE':
      return 'This structure is already used by a processed or published payslip. '
          'Add a new structure with a later start month instead.';
    case 'VERSION_CONFLICT':
      return 'Someone else changed this payslip. Reload it and try again.';
    case 'INVALID_PERIOD':
      return 'Choose a valid month (not in the future) that starts on the 1st.';
    case 'SECOND_PERSON_REQUIRED':
      return 'A different person must publish this: you processed the run or last edited a payslip in it.';
    case 'FORBIDDEN':
      return e.message.isNotEmpty && e.message != 'Something went wrong.'
          ? e.message
          : 'You are not allowed to do this. Nobody can change their own payroll data.';
    case 'REAUTH_FAILED':
      return 'Your password was not correct.';
    case 'NOT_FOUND':
      return 'This payslip or record is not available.';
    case 'DATA_UNREADABLE':
      return 'This payslip cannot be read right now. Please contact your administrator.';
    case 'TEMPORARILY_UNAVAILABLE':
    case 'SERVICE_UNAVAILABLE':
      final s = e.retryAfter?.inSeconds;
      return s == null
          ? 'The server is busy. Please try again in a moment.'
          : 'The server is busy. Please try again in $s seconds.';
    case 'RATE_LIMITED':
      final s = e.retryAfter?.inSeconds;
      return s == null
          ? 'Too many requests. Please wait a moment and try again.'
          : 'Too many requests. Please try again in $s seconds.';
    case 'VALIDATION_ERROR':
      final msgs = e.details
          .map((d) => d.field.isEmpty ? d.message : '${d.field}: ${d.message}')
          .where((m) => m.isNotEmpty);
      return msgs.isEmpty ? 'Please check the details and try again.' : msgs.join('\n');
    case 'CONFLICT':
      return e.message.isEmpty ? 'This record changed or already exists.' : e.message;
    default:
      return e.message.isEmpty ? 'Something went wrong. Please try again.' : e.message;
  }
}
