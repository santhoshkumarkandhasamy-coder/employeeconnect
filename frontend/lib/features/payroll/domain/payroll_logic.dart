import '../../../core/utils/dates.dart';
import 'payroll_models.dart';
import 'payroll_money.dart';

/// Pure form logic for the payroll screens (validation and request bodies).

// ---- shared field validators -----------------------------------------------------------

String? validateMoney(String? v, {bool required = true, bool positive = false, String? label}) {
  final t = (v ?? '').trim();
  final name = label ?? 'Amount';
  if (t.isEmpty) return required ? '$name is required' : null;
  final p = parseMoneyPaise(t);
  if (p == null) return 'Enter an amount like 25000 or 25000.50 (up to 10,000,000)';
  if (positive && p <= 0) return '$name must be more than 0';
  return null;
}

/// Reserved codes (contract 13.0): not usable for allowances / deductions.
const Set<String> reservedPayrollCodes = {
  'BASIC', 'HRA', 'PF', 'ESI', 'PT', 'TDS', 'LOP', //
  'BONUS', 'ARREARS', 'INCENTIVE', 'REIMBURSEMENT', 'OTHER_EARNING',
  'ADVANCE_RECOVERY', 'LOAN_EMI', 'OTHER_DEDUCTION',
};

final RegExp _codeRe = RegExp(r'^[A-Z][A-Z0-9_]{1,15}$');

String? validatePayrollCode(String? v, {bool allowReserved = false}) {
  final t = (v ?? '').trim().toUpperCase();
  if (t.isEmpty) return 'Code is required';
  if (!_codeRe.hasMatch(t)) return 'Use 2-16 capitals, digits or _ (start with a letter)';
  if (!allowReserved && reservedPayrollCodes.contains(t)) return '$t is reserved';
  return null;
}

String? validatePayrollLabel(String? v, {String label = 'Name'}) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return '$label is required';
  if (t.length > 40) return '$label can have at most 40 characters';
  return null;
}

/// Unique codes across allowances and fixed deductions (contract 13.4), plus
/// the count limits. Returns a message or null.
String? validateStructureLists(
  List<AllowanceLine> allowances,
  List<FixedDeductionLine> deductions,
) {
  if (allowances.length > 15) return 'At most 15 allowances';
  if (deductions.length > 10) return 'At most 10 fixed deductions';
  final seen = <String>{};
  for (final c in [
    ...allowances.map((a) => a.code.trim().toUpperCase()),
    ...deductions.map((d) => d.code.trim().toUpperCase()),
  ]) {
    if (!seen.add(c)) return 'Code $c is used twice';
  }
  return null;
}

/// The first day of a month as sent for `effective_from`.
String structureEffectiveFrom(int year, int month) => ymd(DateTime(year, month));

// ---- LOP override -------------------------------------------------------------------------

/// Validates a typed LOP override. Empty = no override.
String? validateLopOverride(String? v, double employedDays) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return null;
  final n = double.tryParse(t);
  if (n == null || n < 0) return 'Enter days like 1 or 1.5';
  if ((n * 2) != (n * 2).roundToDouble()) return 'Use half-day steps (0.5)';
  if (n > employedDays) return 'At most ${formatDays(employedDays)} days';
  return null;
}

String? validateLopReason(String? v, {required bool overrideSet}) {
  final t = (v ?? '').trim();
  if (!overrideSet) return null;
  if (t.length < 3) return 'Give a reason (3 to 200 characters)';
  if (t.length > 200) return 'At most 200 characters';
  return null;
}

/// Parsed LOP text -> double, or null when empty/invalid.
double? parseLop(String? v) {
  final t = (v ?? '').trim();
  return t.isEmpty ? null : double.tryParse(t);
}

// ---- payslip edit patch ---------------------------------------------------------------------

class PayslipEditInput {
  const PayslipEditInput({
    this.lopOverride,
    this.lopReason = '',
    this.tds = '',
    this.ptOverride = '',
    this.adjustments = const [],
    this.hold = false,
    this.note = '',
  });
  final double? lopOverride;
  final String lopReason;
  final String tds;
  final String ptOverride;
  final List<Adjustment> adjustments;
  final bool hold;
  final String note;
}

const List<String> earningAdjustmentCodes = [
  'BONUS',
  'ARREARS',
  'INCENTIVE',
  'REIMBURSEMENT',
  'OTHER_EARNING',
];
const List<String> deductionAdjustmentCodes = [
  'ADVANCE_RECOVERY',
  'LOAN_EMI',
  'OTHER_DEDUCTION',
];
const List<String> adjustmentCodes = [...earningAdjustmentCodes, ...deductionAdjustmentCodes];

String adjustmentCodeLabel(String code) => switch (code) {
      'BONUS' => 'Bonus',
      'ARREARS' => 'Arrears',
      'INCENTIVE' => 'Incentive',
      'REIMBURSEMENT' => 'Reimbursement',
      'OTHER_EARNING' => 'Other earning',
      'ADVANCE_RECOVERY' => 'Advance recovery',
      'LOAN_EMI' => 'Loan EMI',
      'OTHER_DEDUCTION' => 'Other deduction',
      _ => code,
    };

/// Body for `PUT /admin/payroll/payslips/{id}`: `expected_version` plus only the
/// keys that differ from [original] (contract 13.6: "only sent keys change").
Map<String, Object?> buildPayslipPatch(Payslip original, PayslipEditInput input) {
  final out = <String, Object?>{'expected_version': original.version};
  final o = original.inputs;

  final origOverride = original.lop?.override;
  final reason = input.lopReason.trim();
  final overrideChanged = input.lopOverride != origOverride;
  final reasonChanged = input.lopOverride != null && reason != (original.lop?.overrideReason ?? '');
  if (overrideChanged || reasonChanged) {
    out['lop_override'] = input.lopOverride;
    if (input.lopOverride != null) out['lop_override_reason'] = reason;
  }

  final tds = normalizeMoney(input.tds);
  if (tds != null && moneyToPaise(tds) != moneyToPaise(o?.tds)) out['tds'] = tds;

  final ptText = input.ptOverride.trim();
  final pt = ptText.isEmpty ? null : normalizeMoney(ptText);
  final origPt = o?.ptOverride == null ? null : moneyToPaise(o!.ptOverride);
  final newPt = pt == null ? null : moneyToPaise(pt);
  if (newPt != origPt) out['pt_override'] = pt;

  final adj = [
    for (final a in input.adjustments)
      Adjustment(
        code: a.code,
        label: a.label.trim(),
        amount: normalizeMoney(a.amount) ?? a.amount,
      ),
  ];
  final origAdj = [
    for (final a in o?.adjustments ?? const <Adjustment>[])
      Adjustment(code: a.code, label: a.label, amount: normalizeMoney(a.amount) ?? a.amount),
  ];
  if (!_sameList(adj, origAdj)) out['adjustments'] = [for (final a in adj) a.toJson()];

  if (input.hold != original.hold) out['hold'] = input.hold;

  final note = input.note.trim();
  if (note != (o?.note ?? '')) out['note'] = note.isEmpty ? null : note;
  return out;
}

bool _sameList(List<Adjustment> a, List<Adjustment> b) {
  if (a.length != b.length) return false;
  for (var i = 0; i < a.length; i++) {
    if (a[i] != b[i]) return false;
  }
  return true;
}

/// True when [patch] changes something besides `expected_version`.
bool patchHasChanges(Map<String, Object?> patch) => patch.length > 1;

// ---- payroll details ---------------------------------------------------------------------------

final RegExp _panRe = RegExp(r'^[A-Z]{5}[0-9]{4}[A-Z]$');
final RegExp _ifscRe = RegExp(r'^[A-Z]{4}0[A-Z0-9]{6}$');
final RegExp _accountRe = RegExp(r'^\d{9,18}$');
final RegExp _uanRe = RegExp(r'^\d{12}$');
final RegExp _esiRe = RegExp(r'^\d{10}$');

/// Upper-case and remove spaces, as the server does.
String cleanIdentifier(String v) => v.replaceAll(RegExp(r'\s+'), '').toUpperCase();

String? _optionalFormat(String? v, RegExp re, String message) {
  final t = cleanIdentifier(v ?? '');
  if (t.isEmpty) return null;
  return re.hasMatch(t) ? null : message;
}

String? validatePan(String? v) => _optionalFormat(v, _panRe, 'PAN looks like ABCDE1234F');
String? validateIfsc(String? v) => _optionalFormat(v, _ifscRe, 'IFSC looks like HDFC0001234');
String? validateBankAccount(String? v) =>
    _optionalFormat(v, _accountRe, 'Account number has 9 to 18 digits');
String? validateUan(String? v) => _optionalFormat(v, _uanRe, 'UAN has 12 digits');
String? validateEsiIp(String? v) => _optionalFormat(v, _esiRe, 'ESI number has 10 digits');

String? validateDesignation(String? v) {
  final t = (v ?? '').trim();
  return t.length > 60 ? 'At most 60 characters' : null;
}

const List<String> paymentModes = ['bank_transfer', 'cheque', 'cash'];

String paymentModeLabel(String m) => switch (m) {
      'bank_transfer' => 'Bank transfer',
      'cheque' => 'Cheque',
      'cash' => 'Cash',
      _ => m,
    };

/// Partial body for the details PUT. [original] holds what the form was
/// loaded with: unrevealed (masked) fields are never prefilled, so a blank
/// field with `revealed == false` means "leave as it is".
Map<String, Object?> buildDetailsPatch({
  required PayrollDetails original,
  required String designation,
  required String paymentMode,
  required bool include,
  required String bankName,
  required String bankAccount,
  required String bankIfsc,
  required String pan,
  required String uan,
  required String esiIp,
}) {
  final out = <String, Object?>{};
  void plain(String key, String? orig, String now) {
    final n = now.trim();
    if (n != (orig ?? '')) out[key] = n.isEmpty ? null : n;
  }

  void sensitive(String key, String? orig, String now) {
    final n = cleanIdentifier(now);
    if (!original.revealed) {
      // Masked values are not in the field; only typed input changes anything.
      if (n.isNotEmpty) out[key] = n;
      return;
    }
    if (n != (orig ?? '')) out[key] = n.isEmpty ? null : n;
  }

  plain('designation', original.designation, designation);
  if (paymentMode != original.paymentMode) out['payment_mode'] = paymentMode;
  if (include != original.includeInPayroll) out['include_in_payroll'] = include;
  plain('bank_name', original.bankName, bankName);
  sensitive('bank_account', original.bankAccount, bankAccount);
  sensitive('bank_ifsc', original.bankIfsc, bankIfsc);
  sensitive('pan', original.pan, pan);
  sensitive('uan', original.uan, uan);
  sensitive('esi_ip_number', original.esiIpNumber, esiIp);
  return out;
}

// ---- settings / PT slabs ---------------------------------------------------------------------------

enum PayrollSettingKind { choice, boolean, number, text }

class PayrollSettingSpec {
  const PayrollSettingSpec(
    this.key,
    this.label,
    this.kind,
    this.group, {
    this.options = const [],
    this.min,
    this.max,
    this.maxLength,
    this.suffix,
  });
  final String key;
  final String label;
  final PayrollSettingKind kind;
  final String group;
  final List<(String value, String label)> options;
  final double? min;
  final double? max;
  final int? maxLength;
  final String? suffix;
}

const List<PayrollSettingSpec> payrollSettingSpecs = [
  PayrollSettingSpec(
    'payroll.pay_days_basis',
    'Pay days basis',
    PayrollSettingKind.choice,
    'Days and LOP',
    options: [
      ('calendar', 'Calendar days in the month'),
      ('fixed_30', 'Fixed 30 days'),
      ('working_days', 'Working days'),
    ],
  ),
  PayrollSettingSpec('payroll.lop_absent_days', 'Absent working days are LOP',
      PayrollSettingKind.boolean, 'Days and LOP',),
  PayrollSettingSpec('payroll.lop_half_day_attendance', 'Half-day attendance is 0.5 LOP',
      PayrollSettingKind.boolean, 'Days and LOP',),
  PayrollSettingSpec('payroll.pf_employee_rate_pct', 'PF employee rate', PayrollSettingKind.number,
      'Provident fund', min: 0, max: 20, suffix: '%',),
  PayrollSettingSpec('payroll.pf_employer_rate_pct', 'PF employer rate', PayrollSettingKind.number,
      'Provident fund', min: 0, max: 20, suffix: '%',),
  PayrollSettingSpec('payroll.pf_wage_ceiling', 'PF wage ceiling (0 = none)',
      PayrollSettingKind.number, 'Provident fund', min: 0, max: 1000000,),
  PayrollSettingSpec('payroll.esi_wage_threshold', 'ESI wage threshold', PayrollSettingKind.number,
      'ESI', min: 0, max: 1000000,),
  PayrollSettingSpec('payroll.esi_employee_rate_pct', 'ESI employee rate', PayrollSettingKind.number,
      'ESI', min: 0, max: 10, suffix: '%',),
  PayrollSettingSpec('payroll.esi_employer_rate_pct', 'ESI employer rate', PayrollSettingKind.number,
      'ESI', min: 0, max: 10, suffix: '%',),
  PayrollSettingSpec('payroll.employer_share_from_employee',
      'Also deduct the employer PF and ESI share from the employee', PayrollSettingKind.boolean,
      'Employer share',),
  PayrollSettingSpec(
    'payroll.pt_frequency',
    'Professional tax frequency',
    PayrollSettingKind.choice,
    'Professional tax',
    options: [('monthly', 'Monthly'), ('half_yearly', 'Half-yearly (Tamil Nadu)')],
  ),
  PayrollSettingSpec(
    'payroll.rounding',
    'Rounding',
    PayrollSettingKind.choice,
    'Payslip',
    options: [('rupee', 'To the rupee'), ('paise', 'To the paisa')],
  ),
  PayrollSettingSpec('payroll.show_employer_contributions', 'Show employer contributions on payslip',
      PayrollSettingKind.boolean, 'Payslip',),
  PayrollSettingSpec(
      'payroll.mask_pan', 'Mask PAN on payslip', PayrollSettingKind.boolean, 'Payslip',),
  PayrollSettingSpec('payroll.publish_requires_second_person',
      'Publisher must differ from the person who processed', PayrollSettingKind.boolean, 'Payslip',),
  PayrollSettingSpec('payroll.company_address', 'Company address', PayrollSettingKind.text, 'Payslip',
      maxLength: 300,),
  PayrollSettingSpec('payroll.payslip_footer', 'Payslip footer', PayrollSettingKind.text, 'Payslip',
      maxLength: 200,),
];

String? validatePayrollNumber(String? v, PayrollSettingSpec s) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return 'Required';
  final n = double.tryParse(t);
  if (n == null) return 'Enter a number';
  if (s.min != null && n < s.min!) return 'At least ${_fmt(s.min!)}';
  if (s.max != null && n > s.max!) return 'At most ${_fmt(s.max!)}';
  final dot = t.indexOf('.');
  if (dot >= 0 && t.length - dot - 1 > 2) return 'At most 2 decimals';
  return null;
}

String _fmt(double n) => n == n.roundToDouble() ? '${n.toInt()}' : '$n';

/// Number for the request: whole values as int (`12`), others as double (`0.75`).
num parsePayrollNumber(String v) {
  final n = double.parse(v.trim());
  return n == n.roundToDouble() ? n.toInt() : n;
}

/// Display text for a numeric setting value.
String payrollNumberText(Object? v) => v is num ? _fmt(v.toDouble()) : '${v ?? ''}';

/// Keys of [edited] whose value differs from [initial] (numbers compared by
/// value, so `12` equals `12.0`).
Map<String, Object?> changedPayrollSettings(
  Map<String, Object?> initial,
  Map<String, Object?> edited,
) {
  final out = <String, Object?>{};
  for (final e in edited.entries) {
    final a = initial[e.key];
    final b = e.value;
    final same = (a is num && b is num) ? a.toDouble() == b.toDouble() : a == b;
    if (!same) out[e.key] = b;
  }
  return out;
}

/// Client-side check of the slab table; the server stays the authority.
/// Returns a message or null.
String? validatePtSlabs(List<PtSlab> slabs) {
  if (slabs.length > 20) return 'At most 20 slabs';
  final ranges = <({int min, int max, Set<int>? months, int row})>[];
  for (var i = 0; i < slabs.length; i++) {
    final s = slabs[i];
    final row = 'Slab ${i + 1}';
    final min = parseMoneyPaise(s.minGross);
    if (min == null) return '$row: enter a valid minimum';
    var max = maxMoneyPaise * 10;
    if (s.maxGross != null && s.maxGross!.isNotEmpty) {
      final m = parseMoneyPaise(s.maxGross);
      if (m == null) return '$row: enter a valid maximum';
      if (m < min) return '$row: maximum must not be below the minimum';
      max = m;
    }
    if (parseMoneyPaise(s.amount) == null) return '$row: enter a valid tax amount';
    final months = s.months;
    if (months != null && months.any((m) => m < 1 || m > 12)) return '$row: months must be 1 to 12';
    ranges.add((min: min, max: max, months: months?.toSet(), row: i + 1));
  }
  // Slabs of the same kind (all months / explicit months) that share a month
  // must not overlap. A month-specific slab may sit on top of an all-months one.
  for (var a = 0; a < ranges.length; a++) {
    for (var b = a + 1; b < ranges.length; b++) {
      final x = ranges[a];
      final y = ranges[b];
      if ((x.months == null) != (y.months == null)) continue;
      final shareMonth = x.months == null || x.months!.intersection(y.months!).isNotEmpty;
      if (shareMonth && x.min <= y.max && y.min <= x.max) {
        return 'Slab ${x.row} and slab ${y.row} overlap';
      }
    }
  }
  return null;
}

const List<String> shortMonthNames = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec', //
];
