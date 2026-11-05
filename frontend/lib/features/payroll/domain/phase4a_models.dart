import '../../../core/utils/dates.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import 'payroll_models.dart' show AllowanceLine, FixedDeductionLine, SalaryStructure, StructureInput;
import 'payroll_money.dart';

/// api-contract.md section 18 (payroll phase 4, work package 4a): CTC
/// calculator, gratuity, increments, bulk hike batches and structure import.
/// In memory only. Money and percents stay strings; nothing here is logged.

String _s(Object? v) => v == null ? '' : '$v';
String? _opt(Object? v) => v is String && v.isNotEmpty ? v : null;
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

EmployeeRef _user(Object? v) =>
    EmployeeRef.parse(v) ?? const EmployeeRef(id: '', employeeId: '', fullName: '');

// ---- money beyond the 10,000,000 form limit (annual and aggregate figures) ----------------

final RegExp _bigMoneyRe = RegExp(r'^(-?)(\d{1,12})(\.\d{1,2})?$');

/// Rupee text for any server money string, also above 10,000,000 and negative
/// (totals, annual figures, deltas). Returns the raw text when malformed.
String formatBigRupees(String? money) {
  if (money == null) return '-';
  final m = _bigMoneyRe.firstMatch(money.trim());
  if (m == null) return money;
  final frac = (m[3] ?? '.00').padRight(3, '0').substring(1);
  return '${m[1]}₹${groupIndian(m[2]!)}.$frac';
}

// ---- permission ---------------------------------------------------------------------------

const String payrollApprovePerm = 'payroll.approve';
bool canApprovePayroll(List<String> p) => p.contains(payrollApprovePerm);

// ---- labels -------------------------------------------------------------------------------

const Map<String, String> incrementKindLabels = {
  'percent': 'Percent increase',
  'amount': 'New gross',
  'structure': 'Full structure',
  'remove': 'Remove structure',
  'import': 'From import',
};

String incrementKindLabel(String k) => incrementKindLabels[k] ?? k;

const Map<String, String> incrementStatusLabels = {
  'draft': 'Draft',
  'submitted': 'Waiting for approval',
  'approved': 'Approved',
  'rejected': 'Rejected',
  'cancelled': 'Cancelled',
};

String incrementStatusLabel(String s) => incrementStatusLabels[s] ?? s;

const Map<String, String> incrementWarningLabels = {
  'BACKDATED': 'Backdated: arrears will be proposed',
  'TDS_REVIEW': 'Check the monthly TDS',
  'DECREASE': 'Salary decreases',
  'LARGE_CHANGE': 'Large change',
  'ESI_THRESHOLD_CROSSED': 'ESI threshold crossed',
  'PF_CEILING_CROSSED': 'PF ceiling crossed',
  'NO_CHANGE': 'No change from the current structure',
};

String incrementWarningLabel(String w) => incrementWarningLabels[w] ?? structureImportWarningLabels[w] ?? w;

const Map<String, String> structureSourceLabels = {
  'manual': 'Manual',
  'increment': 'Increment',
  'import': 'Import',
};

String structureSourceLabel(String s) => structureSourceLabels[s] ?? s;

/// Reasons in `skipped[]` / `failed[]` of bulk hike, batch submit and batch approve.
const Map<String, String> bulkReasonLabels = {
  'NO_STRUCTURE': 'No salary structure',
  'SELF': 'This is you',
  'INCREMENT_EXISTS': 'Already has a draft or submitted increment for that month',
  'STRUCTURE_EXISTS': 'A structure already starts that month',
  'LATER_STRUCTURE_EXISTS': 'A later structure exists',
  'NOT_ELIGIBLE': 'Not in payroll',
  'AFTER_EXIT': 'Leaves before that month',
  'NO_CHANGE': 'No change',
  'NOT_DRAFT': 'Not a draft',
  'INCREMENT_STALE': 'The salary structure changed after the draft was made',
  'SECOND_PERSON_REQUIRED': 'You made or submitted this row',
  'STRUCTURE_IN_USE': 'The structure is already used by a payslip',
  'FORBIDDEN': 'Not allowed (this is you)',
};

String bulkReasonLabel(String r) => bulkReasonLabels[r] ?? r;

const Map<String, String> ctcFlagLabels = {
  'ESI_APPLIES': 'ESI applies at this gross',
  'PF_CEILING_APPLIED': 'PF is limited by the wage ceiling',
  'PF_NOT_APPLICABLE': 'PF is switched off',
  'TARGET_TOO_LOW': 'The target is too low for any gross',
  'NO_PT': 'No professional tax slab applies',
};

String ctcFlagLabel(String f) => ctcFlagLabels[f] ?? f;

const Map<String, String> gratuityFlagLabels = {
  'NO_JOIN_DATE': 'No join date',
  'NO_STRUCTURE': 'No salary structure',
  'CAPPED': 'Capped at the legal limit',
};

String gratuityFlagLabel(String f) => gratuityFlagLabels[f] ?? f;

// ---- CTC calculator (18.2) ------------------------------------------------------------------

/// Largest annual amount the server accepts.
const int ctcMaxAnnualPaise = 12000000000;

final RegExp _ctcAmountRe = RegExp(r'^\d{1,9}(\.\d{1,2})?$');

/// Paise of a calculator amount (up to 120,000,000.00), or null.
int? parseCtcPaise(String? text) {
  final t = (text ?? '').trim().replaceAll(',', '');
  if (!_ctcAmountRe.hasMatch(t)) return null;
  final parts = t.split('.');
  final p = int.parse(parts[0]) * 100 + (parts.length == 1 ? 0 : int.parse(parts[1].padRight(2, '0')));
  return p > ctcMaxAnnualPaise ? null : p;
}

String? validateCtcAmount(String? v, {required bool annual}) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return 'Amount is required';
  final p = parseCtcPaise(t);
  if (p == null) return 'Enter an amount like 40000 or 40000.50';
  if (p < 100) return 'Enter at least 1';
  if (!annual && p > maxMoneyPaise) return 'A monthly amount can be at most 1,00,00,000';
  return null;
}

class CtcInput {
  const CtcInput({
    required this.mode,
    required this.amount,
    this.frequency = 'monthly',
    this.pfApplicable = true,
    this.pfOnFullBasic = false,
    this.esiApplicable = true,
    this.ptApplicable = true,
    this.basicPct,
    this.hraPctOfBasic,
    this.includeGratuity,
    this.employerShareFromEmployee,
  });

  /// `from_gross` | `from_ctc`.
  final String mode;
  final String amount;

  /// `monthly` | `annual`.
  final String frequency;
  final bool pfApplicable;
  final bool pfOnFullBasic;
  final bool esiApplicable;
  final bool ptApplicable;

  /// Both set = a custom split; both null = the company rule (server setting).
  final int? basicPct;
  final int? hraPctOfBasic;

  /// Null = the payroll setting.
  final bool? includeGratuity;
  final bool? employerShareFromEmployee;

  Map<String, Object?> toJson() => {
        'mode': mode,
        'amount': amount,
        'frequency': frequency,
        'pf_applicable': pfApplicable,
        'pf_on_full_basic': pfApplicable && pfOnFullBasic,
        'esi_applicable': esiApplicable,
        'pt_applicable': ptApplicable,
        'split': basicPct == null || hraPctOfBasic == null
            ? null
            : {'basic_pct': basicPct, 'hra_pct_of_basic': hraPctOfBasic},
        'include_gratuity': includeGratuity,
        'employer_share_from_employee': employerShareFromEmployee,
      };
}

class CtcResult {
  const CtcResult({
    required this.mode,
    required this.frequency,
    required this.basic,
    required this.hra,
    required this.allowances,
    required this.monthlyGross,
    required this.employerPf,
    required this.employerEsi,
    required this.employerRecovered,
    required this.gratuityAccrual,
    required this.monthlyCtc,
    required this.annualGross,
    required this.annualCtc,
    required this.employeePf,
    required this.employeeEsi,
    required this.ptAnnualEstimate,
    required this.netMonthlyEstimate,
    required this.unallocatedMonthly,
    required this.flags,
    required this.rules,
  });

  factory CtcResult.fromJson(Map<String, dynamic> j) {
    final split = _m(j['split']);
    final monthly = _m(j['monthly']);
    final annual = _m(j['annual']);
    final emp = _m(j['employee']);
    return CtcResult(
      mode: _s(j['mode']),
      frequency: _s(j['frequency']),
      basic: _opt(split['basic']) ?? '0.00',
      hra: _opt(split['hra']) ?? '0.00',
      allowances: [for (final a in _list(split['allowances'])) AllowanceLine.fromJson(a)],
      monthlyGross: _opt(monthly['gross']) ?? '0.00',
      employerPf: _opt(monthly['employer_pf']) ?? '0.00',
      employerEsi: _opt(monthly['employer_esi']) ?? '0.00',
      employerRecovered: _opt(monthly['employer_recovered']) ?? '0.00',
      gratuityAccrual: _opt(monthly['gratuity_accrual']) ?? '0.00',
      monthlyCtc: _opt(monthly['ctc']) ?? '0.00',
      annualGross: _opt(annual['gross']) ?? '0.00',
      annualCtc: _opt(annual['ctc']) ?? '0.00',
      employeePf: _opt(emp['pf']) ?? '0.00',
      employeeEsi: _opt(emp['esi']) ?? '0.00',
      ptAnnualEstimate: _opt(emp['pt_annual_estimate']) ?? '0.00',
      netMonthlyEstimate: _opt(emp['net_monthly_estimate']) ?? '0.00',
      unallocatedMonthly: _opt(j['unallocated_monthly']) ?? '0.00',
      flags: _strings(j['flags']),
      rules: _m(j['rules']),
    );
  }

  final String mode;
  final String frequency;
  final String basic;
  final String hra;
  final List<AllowanceLine> allowances;
  final String monthlyGross;
  final String employerPf;
  final String employerEsi;
  final String employerRecovered;
  final String gratuityAccrual;
  final String monthlyCtc;
  final String annualGross;
  final String annualCtc;
  final String employeePf;
  final String employeeEsi;
  final String ptAnnualEstimate;
  final String netMonthlyEstimate;
  final String unallocatedMonthly;
  final List<String> flags;
  final Map<String, dynamic> rules;

  /// The split as a structure (for "Use as structure"); flags come from [input].
  StructureInput toStructureInput(CtcInput input, String effectiveFrom) => StructureInput(
        effectiveFrom: effectiveFrom,
        basic: basic,
        hra: hra,
        allowances: allowances,
        fixedDeductions: const [],
        tdsMonthly: '0.00',
        pfApplicable: input.pfApplicable,
        pfOnFullBasic: input.pfApplicable && input.pfOnFullBasic,
        esiApplicable: input.esiApplicable,
        ptApplicable: input.ptApplicable,
      );
}

// ---- gratuity (18.3) --------------------------------------------------------------------------

class GratuityQuery {
  const GratuityQuery({this.asOf, this.departmentId, this.eligibleOnly = false, this.q = ''});

  /// `YYYY-MM-DD`; null = today.
  final String? asOf;
  final String? departmentId;
  final bool eligibleOnly;
  final String q;

  @override
  bool operator ==(Object other) =>
      other is GratuityQuery &&
      other.asOf == asOf &&
      other.departmentId == departmentId &&
      other.eligibleOnly == eligibleOnly &&
      other.q == q;

  @override
  int get hashCode => Object.hash(asOf, departmentId, eligibleOnly, q);
}

/// Provider key: a query and a 1-based page.
typedef GratuityKey = (GratuityQuery query, int page);

class GratuityItem {
  const GratuityItem({
    required this.user,
    required this.years,
    required this.months,
    required this.days,
    required this.countedYears,
    required this.eligible,
    required this.flags,
    this.joinDate,
    this.exitDate,
    this.wage,
    this.accrued,
    this.monthlyAccrual,
  });

  factory GratuityItem.fromJson(Map<String, dynamic> j) {
    final svc = _m(j['service']);
    return GratuityItem(
      user: _user(j['user']),
      joinDate: _opt(j['join_date']),
      exitDate: _opt(j['exit_date']),
      years: _i(svc['years']),
      months: _i(svc['months']),
      days: _i(svc['days']),
      countedYears: _i(j['counted_years']),
      eligible: j['eligible'] == true,
      wage: _opt(j['wage']),
      accrued: _opt(j['accrued']),
      monthlyAccrual: _opt(j['monthly_accrual']),
      flags: _strings(j['flags']),
    );
  }

  final EmployeeRef user;
  final String? joinDate;
  final String? exitDate;
  final int years;
  final int months;
  final int days;
  final int countedYears;
  final bool eligible;

  /// Null for `NO_STRUCTURE` / `NO_JOIN_DATE`.
  final String? wage;
  final String? accrued;
  final String? monthlyAccrual;
  final List<String> flags;

  String get serviceLabel => '${years}y ${months}m ${days}d';
}

class GratuityPage {
  const GratuityPage({
    required this.asOf,
    required this.basis,
    required this.cap,
    required this.minYears,
    required this.items,
    required this.accrued,
    required this.eligibleAccrued,
    required this.monthlyAccrual,
    required this.page,
    required this.pageSize,
    required this.total,
  });

  factory GratuityPage.fromJson(Map<String, dynamic> j) {
    final t = _m(j['totals']);
    return GratuityPage(
      asOf: _s(j['as_of']),
      basis: _s(j['basis']),
      cap: _opt(j['cap']) ?? '0.00',
      minYears: _i(j['min_years']),
      items: [for (final e in _list(j['items'])) GratuityItem.fromJson(e)],
      accrued: _opt(t['accrued']) ?? '0.00',
      eligibleAccrued: _opt(t['eligible_accrued']) ?? '0.00',
      monthlyAccrual: _opt(t['monthly_accrual']) ?? '0.00',
      page: _i(j['page']) == 0 ? 1 : _i(j['page']),
      pageSize: _i(j['page_size']) == 0 ? 50 : _i(j['page_size']),
      total: _i(j['total']),
    );
  }

  final String asOf;
  final String basis;
  final String cap;
  final int minYears;
  final List<GratuityItem> items;
  final String accrued;
  final String eligibleAccrued;
  final String monthlyAccrual;
  final int page;
  final int pageSize;
  final int total;

  bool get hasMore => page * pageSize < total;
}

// ---- increments (18.4) -------------------------------------------------------------------------

class IncrementBase {
  const IncrementBase({required this.id, required this.effectiveFrom, required this.monthlyGross});
  final String id;
  final String effectiveFrom;
  final String monthlyGross;

  static IncrementBase? parse(Object? v) {
    if (v is! Map) return null;
    final j = v.cast<String, dynamic>();
    return IncrementBase(
      id: _s(j['id']),
      effectiveFrom: _s(j['effective_from']),
      monthlyGross: _s(j['monthly_gross']),
    );
  }
}

class IncrementProposed {
  const IncrementProposed({
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
  });

  factory IncrementProposed.fromJson(Map<String, dynamic> j) => IncrementProposed(
        basic: _opt(j['basic']) ?? '0.00',
        hra: _opt(j['hra']) ?? '0.00',
        allowances: [for (final a in _list(j['allowances'])) AllowanceLine.fromJson(a)],
        fixedDeductions: [for (final a in _list(j['fixed_deductions'])) FixedDeductionLine.fromJson(a)],
        tdsMonthly: _opt(j['tds_monthly']) ?? '0.00',
        pfApplicable: j['pf_applicable'] != false,
        pfOnFullBasic: j['pf_on_full_basic'] == true,
        esiApplicable: j['esi_applicable'] != false,
        ptApplicable: j['pt_applicable'] != false,
        monthlyGross: _opt(j['monthly_gross']) ?? '0.00',
      );

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
}

class IncrementChange {
  const IncrementChange({this.oldGross, this.newGross, this.amount, this.percent});

  factory IncrementChange.fromJson(Map<String, dynamic> j) => IncrementChange(
        oldGross: _opt(j['old_gross']),
        newGross: _opt(j['new_gross']),
        amount: _opt(j['amount']),
        percent: _opt(j['percent']),
      );

  /// Everything is null for kind `remove`.
  final String? oldGross;
  final String? newGross;
  final String? amount;
  final String? percent;

  bool get isEmpty => newGross == null;
}

class DiffLine {
  const DiffLine({required this.code, this.oldValue, this.newValue});
  final String code;
  final String? oldValue;
  final String? newValue;
}

class Increment {
  const Increment({
    required this.id,
    required this.user,
    required this.kind,
    required this.status,
    required this.version,
    required this.change,
    required this.diff,
    required this.warnings,
    required this.reason,
    this.batchId,
    this.effectiveFrom,
    this.percent,
    this.method,
    this.base,
    this.replacesStructureId,
    this.proposed,
    this.runStatus = 'none',
    this.structureId,
    this.arrearsProposed,
    this.createdBy,
    this.createdAt,
    this.submittedBy,
    this.submittedAt,
    this.decidedBy,
    this.decidedAt,
    this.decisionComment,
  });

  factory Increment.fromJson(Map<String, dynamic> j) => Increment(
        id: _s(j['id']),
        user: _user(j['user']),
        batchId: _opt(j['batch_id']),
        kind: _s(j['kind']),
        effectiveFrom: _opt(j['effective_from']),
        percent: _opt(j['percent']),
        method: _opt(j['method']),
        base: IncrementBase.parse(j['base_structure']),
        replacesStructureId: _opt(j['replaces_structure_id']),
        proposed: j['proposed'] is Map ? IncrementProposed.fromJson(_m(j['proposed'])) : null,
        change: IncrementChange.fromJson(_m(j['change'])),
        diff: [
          for (final d in _list(j['diff']))
            DiffLine(code: _s(d['code']), oldValue: _opt(d['old']), newValue: _opt(d['new'])),
        ],
        warnings: _strings(j['warnings']),
        reason: _s(j['reason']),
        status: _s(j['status']),
        runStatus: _opt(j['run_status']) ?? 'none',
        structureId: _opt(j['structure_id']),
        arrearsProposed: j['arrears_proposed'] is bool ? j['arrears_proposed'] as bool : null,
        createdBy: EmployeeRef.parse(j['created_by']),
        createdAt: parseTimestamp(j['created_at']),
        submittedBy: EmployeeRef.parse(j['submitted_by']),
        submittedAt: parseTimestamp(j['submitted_at']),
        decidedBy: EmployeeRef.parse(j['decided_by']),
        decidedAt: parseTimestamp(j['decided_at']),
        decisionComment: _opt(j['decision_comment']),
        version: _i(j['version']) == 0 ? 1 : _i(j['version']),
      );

  final String id;
  final EmployeeRef user;
  final String? batchId;
  final String kind;
  final String? effectiveFrom;
  final String? percent;
  final String? method;
  final IncrementBase? base;
  final String? replacesStructureId;

  /// Not in list items; null for kind `remove`.
  final IncrementProposed? proposed;
  final IncrementChange change;
  final List<DiffLine> diff;
  final List<String> warnings;
  final String reason;
  final String status;
  final String runStatus;
  final String? structureId;
  final bool? arrearsProposed;
  final EmployeeRef? createdBy;
  final DateTime? createdAt;
  final EmployeeRef? submittedBy;
  final DateTime? submittedAt;
  final EmployeeRef? decidedBy;
  final DateTime? decidedAt;
  final String? decisionComment;
  final int version;

  bool get isDraft => status == 'draft';
  bool get isSubmitted => status == 'submitted';
  bool get isInBatch => batchId != null;

  /// The proposed structure as a [SalaryStructure] (to prefill the structure form).
  SalaryStructure? proposedAsStructure() {
    final p = proposed;
    if (p == null) return null;
    return SalaryStructure(
      id: '',
      userId: user.id,
      effectiveFrom: effectiveFrom ?? '',
      basic: p.basic,
      hra: p.hra,
      allowances: p.allowances,
      fixedDeductions: p.fixedDeductions,
      tdsMonthly: p.tdsMonthly,
      pfApplicable: p.pfApplicable,
      pfOnFullBasic: p.pfOnFullBasic,
      esiApplicable: p.esiApplicable,
      ptApplicable: p.ptApplicable,
      monthlyGross: p.monthlyGross,
      inUse: false,
    );
  }

  static List<Increment> parseList(Map<String, dynamic> j) =>
      [for (final e in _list(j['items'])) Increment.fromJson(e)];
}

class IncrementQuery {
  const IncrementQuery({this.status, this.userId, this.batchId, this.effectiveFrom});
  final String? status;
  final String? userId;
  final String? batchId;

  /// `YYYY-MM`.
  final String? effectiveFrom;

  IncrementQuery copyWith({String? Function()? status, String? Function()? effectiveFrom}) => IncrementQuery(
        status: status == null ? this.status : status(),
        userId: userId,
        batchId: batchId,
        effectiveFrom: effectiveFrom == null ? this.effectiveFrom : effectiveFrom(),
      );

  @override
  bool operator ==(Object other) =>
      other is IncrementQuery &&
      other.status == status &&
      other.userId == userId &&
      other.batchId == batchId &&
      other.effectiveFrom == effectiveFrom;

  @override
  int get hashCode => Object.hash(status, userId, batchId, effectiveFrom);
}

/// Request body of create (POST) and edit (PUT) of a single increment. One
/// shape per kind; fields of other kinds are never sent (the server refuses
/// them with 422).
class IncrementInput {
  const IncrementInput._({
    required this.kind,
    required this.reason,
    this.effectiveFrom,
    this.percent,
    this.monthlyGross,
    this.method,
    this.structure,
    this.replacesStructureId,
  });

  factory IncrementInput.percent({
    required String effectiveFrom,
    required String percent,
    required String method,
    required String reason,
  }) =>
      IncrementInput._(
        kind: 'percent',
        effectiveFrom: effectiveFrom,
        percent: percent,
        method: method,
        reason: reason,
      );

  factory IncrementInput.amount({
    required String effectiveFrom,
    required String monthlyGross,
    required String method,
    required String reason,
  }) =>
      IncrementInput._(
        kind: 'amount',
        effectiveFrom: effectiveFrom,
        monthlyGross: monthlyGross,
        method: method,
        reason: reason,
      );

  factory IncrementInput.structure({
    required StructureInput structure,
    required String reason,
    String? replacesStructureId,
  }) =>
      IncrementInput._(
        kind: 'structure',
        effectiveFrom: structure.effectiveFrom,
        structure: structure,
        replacesStructureId: replacesStructureId,
        reason: reason,
      );

  factory IncrementInput.remove({required String replacesStructureId, required String reason}) =>
      IncrementInput._(kind: 'remove', replacesStructureId: replacesStructureId, reason: reason);

  final String kind;
  final String reason;
  final String? effectiveFrom;
  final String? percent;
  final String? monthlyGross;
  final String? method;
  final StructureInput? structure;
  final String? replacesStructureId;

  /// [expectedVersion] is added for an edit (PUT).
  Map<String, Object?> toJson({int? expectedVersion}) {
    final out = <String, Object?>{
      if (expectedVersion != null) 'expected_version': expectedVersion,
      'kind': kind,
    };
    switch (kind) {
      case 'percent':
        out
          ..['effective_from'] = effectiveFrom
          ..['percent'] = percent
          ..['method'] = method;
      case 'amount':
        out
          ..['effective_from'] = effectiveFrom
          ..['monthly_gross'] = monthlyGross
          ..['method'] = method;
      case 'structure':
        final s = structure!.toJson()..remove('effective_from');
        out
          ..['effective_from'] = effectiveFrom
          ..['structure'] = s
          ..['replaces_structure_id'] = replacesStructureId;
      case 'remove':
        out['replaces_structure_id'] = replacesStructureId;
    }
    out['reason'] = reason.trim();
    return out;
  }
}

// ---- percent helpers -------------------------------------------------------------------------

final RegExp _percentRe = RegExp(r'^\d{1,3}(\.\d{1,2})?$');

/// Hundredths of a percent (`"10.5"` -> 1050), or null when malformed.
int? parsePercentHundredths(String? text) {
  final t = (text ?? '').trim();
  if (!_percentRe.hasMatch(t)) return null;
  final parts = t.split('.');
  return int.parse(parts[0]) * 100 + (parts.length == 1 ? 0 : int.parse(parts[1].padRight(2, '0')));
}

/// `"10"` -> `"10.00"` (the wire form), or null when malformed.
String? normalizePercent(String? text) {
  final h = parsePercentHundredths(text);
  return h == null ? null : '${h ~/ 100}.${(h % 100).toString().padLeft(2, '0')}';
}

/// Validates a percent between 0.01 and [maxPct] (the `payroll.increment_max_pct` setting).
String? validatePercent(String? v, {int maxPct = 50}) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return 'Percent is required';
  final h = parsePercentHundredths(t);
  if (h == null) return 'Enter a percent like 8 or 8.5 (up to 2 decimals)';
  if (h < 1) return 'The percent must be more than 0';
  if (h > maxPct * 100) return 'At most $maxPct%';
  return null;
}

/// Local estimate of the target gross of a percent increase: the old gross
/// raised by [percent], rounded half-up to the rupee (the server is
/// authoritative; this is only a preview). Null when an input is malformed.
String? previewPercentGross(String oldGross, String percent) {
  final old = parseMoneyPaise(oldGross);
  final h = parsePercentHundredths(percent);
  if (old == null || h == null) return null;
  final rupees = (old * (10000 + h) + 500000) ~/ 1000000;
  return paiseToMoney(rupees * 100);
}

// ---- salary history (18.4) ---------------------------------------------------------------------

class SalaryHistoryEntry {
  const SalaryHistoryEntry({
    required this.type,
    required this.effectiveFrom,
    this.structureId,
    this.incrementId,
    this.effectiveTo,
    this.monthlyGross,
    this.changePercent,
    this.source,
    this.approvedBy,
    this.status,
    this.kind,
  });

  factory SalaryHistoryEntry.fromJson(Map<String, dynamic> j) {
    final type = _s(j['type']);
    return SalaryHistoryEntry(
      type: type,
      structureId: _opt(j['structure_id']),
      incrementId: _opt(j['increment_id']),
      effectiveFrom: _s(j['effective_from']),
      effectiveTo: _opt(j['effective_to']),
      monthlyGross: _opt(type == 'increment' ? j['new_gross'] : j['monthly_gross']),
      changePercent: _opt(j['change_percent']),
      source: _opt(j['source']),
      approvedBy: EmployeeRef.parse(j['approved_by']),
      status: _opt(j['status']),
      kind: _opt(j['kind']),
    );
  }

  /// `structure` | `increment` (an open one: draft or submitted).
  final String type;
  final String effectiveFrom;
  final String? structureId;
  final String? incrementId;
  final String? effectiveTo;

  /// Structures: the monthly gross; increments: the proposed gross.
  final String? monthlyGross;
  final String? changePercent;
  final String? source;
  final EmployeeRef? approvedBy;
  final String? status;
  final String? kind;

  bool get isOpenIncrement => type == 'increment';

  static List<SalaryHistoryEntry> parseList(Map<String, dynamic> j) =>
      [for (final e in _list(j['items'])) SalaryHistoryEntry.fromJson(e)];
}

// ---- increment batches (18.5) -------------------------------------------------------------------

class BatchTotals {
  const BatchTotals({
    required this.oldGross,
    required this.newGross,
    required this.deltaMonthly,
    required this.deltaAnnual,
  });

  factory BatchTotals.fromJson(Map<String, dynamic> j) => BatchTotals(
        oldGross: _opt(j['old_gross']) ?? '0.00',
        newGross: _opt(j['new_gross']) ?? '0.00',
        deltaMonthly: _opt(j['delta_monthly']) ?? '0.00',
        deltaAnnual: _opt(j['delta_annual']) ?? '0.00',
      );

  final String oldGross;
  final String newGross;
  final String deltaMonthly;
  final String deltaAnnual;
}

class IncrementBatch {
  const IncrementBatch({
    required this.id,
    required this.kind,
    required this.rows,
    required this.reason,
    required this.counts,
    required this.totals,
    this.effectiveFrom,
    this.percent,
    this.method,
    this.fileSha256,
    this.createdBy,
    this.createdAt,
    this.cancelledAt,
  });

  factory IncrementBatch.fromJson(Map<String, dynamic> j) => IncrementBatch(
        id: _s(j['id']),
        kind: _s(j['kind']),
        effectiveFrom: _opt(j['effective_from']),
        percent: _opt(j['percent']),
        method: _opt(j['method']),
        rows: _i(j['rows']),
        reason: _s(j['reason']),
        counts: {for (final e in _m(j['counts']).entries) e.key: _i(e.value)},
        totals: BatchTotals.fromJson(_m(j['totals'])),
        fileSha256: _opt(j['file_sha256']),
        createdBy: EmployeeRef.parse(j['created_by']),
        createdAt: parseTimestamp(j['created_at']),
        cancelledAt: parseTimestamp(j['cancelled_at']),
      );

  final String id;

  /// `percent` | `import`.
  final String kind;
  final String? effectiveFrom;
  final String? percent;
  final String? method;
  final int rows;
  final String reason;
  final Map<String, int> counts;
  final BatchTotals totals;
  final String? fileSha256;
  final EmployeeRef? createdBy;
  final DateTime? createdAt;
  final DateTime? cancelledAt;

  int count(String status) => counts[status] ?? 0;
  int get draftCount => count('draft');
  int get submittedCount => count('submitted');
  bool get isCancelled => cancelledAt != null;

  static List<IncrementBatch> parseList(Map<String, dynamic> j) =>
      [for (final e in _list(j['items'])) IncrementBatch.fromJson(e)];
}

class BulkSkip {
  const BulkSkip({required this.user, required this.reason});
  final EmployeeRef user;
  final String reason;
}

class BulkHikeResult {
  const BulkHikeResult({required this.batch, required this.created, required this.skipped});

  factory BulkHikeResult.fromJson(Map<String, dynamic> j) => BulkHikeResult(
        batch: IncrementBatch.fromJson(_m(j['batch'])),
        created: _i(j['created']),
        skipped: [
          for (final s in _list(j['skipped'])) BulkSkip(user: _user(s['user']), reason: _s(s['reason'])),
        ],
      );

  final IncrementBatch batch;
  final int created;
  final List<BulkSkip> skipped;
}

class BulkHikeInput {
  const BulkHikeInput({
    required this.effectiveFrom,
    required this.percent,
    required this.method,
    required this.reason,
    this.departmentIds,
    this.userIds,
    this.excludeUserIds = const [],
    this.overrides = const {},
  });

  final String effectiveFrom;
  final String percent;
  final String method;
  final String reason;

  /// Null = every department. Empty lists are sent as null (the server reads
  /// null as "all").
  final List<String>? departmentIds;
  final List<String>? userIds;
  final List<String> excludeUserIds;

  /// user id -> percent
  final Map<String, String> overrides;

  Map<String, Object?> toJson() => {
        'effective_from': effectiveFrom,
        'percent': percent,
        'method': method,
        'reason': reason.trim(),
        'scope': {
          'department_ids': (departmentIds?.isEmpty ?? true) ? null : departmentIds,
          'user_ids': (userIds?.isEmpty ?? true) ? null : userIds,
          'exclude_user_ids': excludeUserIds,
        },
        'overrides': [
          for (final e in overrides.entries) {'user_id': e.key, 'percent': e.value},
        ],
      };
}

class IdReason {
  const IdReason({required this.id, required this.reason});
  final String id;
  final String reason;
}

class BatchSubmitResult {
  const BatchSubmitResult({required this.submitted, required this.skipped});

  factory BatchSubmitResult.fromJson(Map<String, dynamic> j) => BatchSubmitResult(
        submitted: _i(j['submitted']),
        skipped: [for (final s in _list(j['skipped'])) IdReason(id: _s(s['id']), reason: _s(s['reason']))],
      );

  final int submitted;
  final List<IdReason> skipped;
}

class BatchApproveResult {
  const BatchApproveResult({required this.approved, required this.failed, required this.arrearsProposed});

  factory BatchApproveResult.fromJson(Map<String, dynamic> j) => BatchApproveResult(
        approved: _i(j['approved']),
        failed: [for (final s in _list(j['failed'])) IdReason(id: _s(s['id']), reason: _s(s['reason']))],
        arrearsProposed: _i(j['arrears_proposed']),
      );

  final int approved;
  final List<IdReason> failed;
  final int arrearsProposed;
}

// ---- structure import (18.6) ----------------------------------------------------------------------

class StructureImportIssue {
  const StructureImportIssue({
    required this.row,
    required this.sheet,
    required this.field,
    required this.message,
  });

  factory StructureImportIssue.fromJson(Map<String, dynamic> j) => StructureImportIssue(
        row: _i(j['row']),
        sheet: _s(j['sheet']),
        field: _s(j['field']),
        message: _s(j['message']),
      );

  /// A commit failure carries `field: "Structures row 14: employee_id"` and
  /// the code in `message`.
  factory StructureImportIssue.fromCommitDetail(String field, String message) {
    final m = RegExp(r'^(\w+) row (\d+):\s*(.*)$').firstMatch(field);
    if (m == null) return StructureImportIssue(row: 0, sheet: '', field: field, message: message);
    return StructureImportIssue(row: int.parse(m[2]!), sheet: m[1]!, field: m[3]!, message: message);
  }

  final int row;
  final String sheet;
  final String field;
  final String message;
}

const Map<String, String> structureImportErrorLabels = {
  'UNKNOWN_EMPLOYEE': 'Employee ID not found',
  'DELETED': 'Employee was deleted',
  'SELF': 'You cannot change your own salary',
  'DUPLICATE_ROW': 'The employee appears twice',
  'INVALID_PERIOD': 'Effective From must be a month like 2027-04 within the allowed range',
  'INVALID_AMOUNT': 'Amount must be a number with at most 2 decimals',
  'INVALID_FLAG': 'Use Y or N',
  'INVALID_TYPE': 'Type must be allowance or deduction',
  'INVALID_CODE': 'Code must be 2-16 capitals, digits or _',
  'RESERVED_CODE': 'This code is reserved',
  'DUPLICATE_CODE': 'The code is used twice for this employee',
  'TOO_MANY_COMPONENTS': 'Too many allowances or deductions for this employee',
  'GROSS_AND_PARTS': 'Give either Gross or Basic with its parts, not both',
  'MISSING_BASIC': 'Basic is missing',
  'ORPHAN_COMPONENT': 'No matching row in the Structures sheet',
  'STRUCTURE_EXISTS': 'A structure already starts that month',
  'LATER_STRUCTURE_EXISTS': 'A later structure exists',
  'INCREMENT_EXISTS': 'A draft or submitted increment already exists for that month',
  'AFTER_EXIT': 'The employee leaves before that month',
  'FORMULA_NOT_ALLOWED': 'Formulas and text starting with = + - @ are not allowed',
};

String structureImportErrorLabel(String code) => structureImportErrorLabels[code] ?? code;

const Map<String, String> structureImportWarningLabels = {
  'BACKDATED': 'Backdated: arrears will be proposed',
  'NO_CHANGE': 'Same as the current structure',
  'NEEDS_APPROVAL': 'Becomes a submitted increment that needs approval',
  'ESI_THRESHOLD_CROSSED': 'ESI threshold crossed',
  'TDS_REVIEW': 'Check the monthly TDS',
};

String structureImportWarningLabel(String code) => structureImportWarningLabels[code] ?? code;

class StructureImportReport {
  const StructureImportReport({
    required this.fileSha256,
    required this.rows,
    required this.valid,
    required this.errors,
    required this.warnings,
    required this.createCount,
    required this.incrementCount,
    required this.totalGross,
  });

  factory StructureImportReport.fromJson(Map<String, dynamic> j) {
    final actions = _m(j['actions']);
    return StructureImportReport(
      fileSha256: _s(j['file_sha256']),
      rows: _i(j['rows']),
      valid: _i(j['valid']),
      errors: [for (final e in _list(j['errors'])) StructureImportIssue.fromJson(e)],
      warnings: [for (final e in _list(j['warnings'])) StructureImportIssue.fromJson(e)],
      createCount: _i(actions['create']),
      incrementCount: _i(actions['increment']),
      totalGross: _opt(_m(j['totals'])['monthly_gross']) ?? '0.00',
    );
  }

  final String fileSha256;
  final int rows;
  final int valid;
  final List<StructureImportIssue> errors;
  final List<StructureImportIssue> warnings;
  final int createCount;
  final int incrementCount;
  final String totalGross;

  /// The server only commits a file without any row error.
  bool get canCommit => errors.isEmpty && valid > 0 && fileSha256.length == 64;
}

class StructureImportCommit {
  const StructureImportCommit({
    required this.batchId,
    required this.created,
    required this.incrementsSubmitted,
    required this.arrearsProposed,
  });

  factory StructureImportCommit.fromJson(Map<String, dynamic> j) => StructureImportCommit(
        batchId: _s(j['batch_id']),
        created: _i(j['created']),
        incrementsSubmitted: _i(j['increments_submitted']),
        arrearsProposed: _i(j['arrears_proposed']),
      );

  final String batchId;
  final int created;
  final int incrementsSubmitted;
  final int arrearsProposed;
}

/// A form input as a [SalaryStructure], to prefill the structure form.
SalaryStructure salaryStructureFromInput(String userId, StructureInput i) => SalaryStructure(
      id: '',
      userId: userId,
      effectiveFrom: i.effectiveFrom,
      basic: i.basic,
      hra: i.hra,
      allowances: i.allowances,
      fixedDeductions: i.fixedDeductions,
      tdsMonthly: i.tdsMonthly,
      pfApplicable: i.pfApplicable,
      pfOnFullBasic: i.pfOnFullBasic,
      esiApplicable: i.esiApplicable,
      ptApplicable: i.ptApplicable,
      monthlyGross: i.monthlyGross,
      inUse: false,
    );
