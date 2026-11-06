import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import 'payroll_models.dart' show Payslip;
import 'phase5a_models.dart' show phase5ErrorMessage;
import 'phase5b_models.dart' show isValidFy, fyBounds, fyTitle, phase5bErrorMessage;
import 'tds_computation.dart';

/// api-contract.md 19.9, 19.10 and the notes in 19.16 (payroll phase 5, work package 5c): TDS engine
/// summary, exports, settings and challan details. In memory only. Money, PANs, bank codes and reasons
/// are never logged and never put in an exception: no toString overrides, no print.

String _s(Object? v) => v == null ? '' : '$v';
String? _opt(Object? v) => v is String && v.isNotEmpty ? v : null;
int _i(Object? v) => v is num ? v.toInt() : (int.tryParse('$v') ?? 0);
Map<String, dynamic> _m(Object? v) => v is Map ? v.cast<String, dynamic>() : <String, dynamic>{};
List<Map<String, dynamic>> _list(Object? v) => [
      if (v is List)
        for (final e in v)
          if (e is Map) e.cast<String, dynamic>(),
    ];
List<String> _strings(Object? v) => [
      if (v is List)
        for (final e in v) '$e',
    ];

// ---- settings (19.10) --------------------------------------------------------------------------

/// Needs `role.assign` + the caller's password (like the other controls).
const String tdsEngineEnabledKey = 'payroll.tds_engine_enabled';
const String tdsEngineModeKey = 'payroll.tds_engine_mode';
const String tdsOneTimeKey = 'payroll.tds_one_time_in_month';
const String tdsProofsFromMonthKey = 'payroll.tds_proofs_from_month';
const String tdsIncludeOtherIncomeKey = 'payroll.tds_include_other_income';

const List<String> tdsSettingKeys = [
  tdsEngineEnabledKey,
  tdsEngineModeKey,
  tdsOneTimeKey,
  tdsProofsFromMonthKey,
  tdsIncludeOtherIncomeKey,
];

/// Explanations shown with the TDS engine settings.
const String tdsSettingsIntro =
    'The TDS engine works out each employee\'s monthly income tax from their salary, the verified tax rules '
    'and their declaration. It only runs when the tax rules for the year are verified. Changing any of these '
    'settings needs the administrator role and your password.';
const String tdsModeHelp =
    'Suggestion only: the payslip keeps the TDS figure that HR types, and the computed figure is shown beside '
    'it. Applied automatically: the computed figure is put on the payslip, and changing it needs a reason.';

// ---- quarters --------------------------------------------------------------------------------------

/// Quarter 1 = Apr-Jun ... 4 = Jan-Mar, for the month of [d].
int quarterOf(DateTime d) => switch (d.month) {
      4 || 5 || 6 => 1,
      7 || 8 || 9 => 2,
      10 || 11 || 12 => 3,
      _ => 4,
    };

String quarterLabel(int q) => switch (q) {
      1 => 'Quarter 1 (Apr to Jun)',
      2 => 'Quarter 2 (Jul to Sep)',
      3 => 'Quarter 3 (Oct to Dec)',
      4 => 'Quarter 4 (Jan to Mar)',
      _ => 'Quarter $q',
    };

// ---- validation ---------------------------------------------------------------------------------------

/// Reason for changing the TDS by hand: 3..200 characters (19.16).
String? validateTdsOverrideReason(String? v, {bool required = true}) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return required ? 'Say why the TDS is changed by hand (3 to 200 characters)' : null;
  if (t.length < 3) return 'At least 3 characters';
  if (t.length > 200) return 'At most 200 characters';
  return null;
}

/// The reason is needed when the run applies the engine (a computation exists and its mode is `apply`).
bool tdsReasonRequired(Payslip p) => p.tdsComputation?.isApply ?? false;

final RegExp _bsrRe = RegExp(r'^\d{7}$');
final RegExp _serialRe = RegExp(r'^\d{1,5}$');

/// BSR code of the bank branch: exactly 7 digits.
String? validateBsrCode(String? v) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return 'Enter the BSR code';
  if (!_bsrRe.hasMatch(t)) return 'The BSR code has exactly 7 digits';
  return null;
}

/// Challan serial number: 1 to 5 digits.
String? validateChallanSerial(String? v) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return 'Enter the challan serial number';
  if (!_serialRe.hasMatch(t)) return 'The serial number has 1 to 5 digits';
  return null;
}

/// Deposit date: valid, not in the future, not before the month of the challan starts.
String? validateDepositedOn(String? v, DateTime today, DateTime periodStart) {
  final d = parseYmd((v ?? '').trim());
  if (d == null) return 'Enter a date like 2026-11-07';
  final t = DateTime(today.year, today.month, today.day);
  if (d.isAfter(t)) return 'Not in the future';
  if (d.isBefore(periodStart)) return 'Not before ${ymd(periodStart)}';
  return null;
}

/// Month `YYYY-MM` that lies inside [fy]; empty = the server picks one.
String? validateTaxPeriod(String? v, String fy) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return null;
  final d = parseMonth(t);
  if (d == null) return 'Use YYYY-MM, like 2026-11';
  final b = fyBounds(fy);
  if (b == null) return 'Choose a valid financial year';
  if (d.isBefore(b.$1) || d.isAfter(b.$2)) return 'The month must be inside ${fyTitle(fy)}';
  return null;
}

// ---- regime comparison --------------------------------------------------------------------------------

class RegimeComparison {
  const RegimeComparison({
    required this.chosen,
    required this.chosenTax,
    required this.other,
    required this.otherTax,
    required this.cheaper,
    required this.differencePaise,
  });
  final String chosen;
  final String chosenTax;
  final String other;
  final String otherTax;

  /// `chosen`, `other`, or null when both come to the same tax.
  final String? cheaper;
  final int differencePaise;
}

int _paise(String? money) {
  final t = (money ?? '').trim().replaceAll(',', '');
  final m = RegExp(r'^(\d+)(?:\.(\d{1,2}))?$').firstMatch(t);
  if (m == null) return 0;
  return int.parse(m[1]!) * 100 + int.parse((m[2] ?? '').padRight(2, '0'));
}

String paiseText(int paise) {
  final p = paise < 0 ? -paise : paise;
  return '${p ~/ 100}.${(p % 100).toString().padLeft(2, '0')}';
}

/// Total tax of the regime in use against the other regime (`other_regime`, 19.9).
RegimeComparison? compareRegimes(TdsComputation c, OtherRegime? o) {
  if (o == null) return null;
  final a = _paise(c.annual.totalTax);
  final b = _paise(o.totalTax);
  return RegimeComparison(
    chosen: c.regime,
    chosenTax: c.annual.totalTax,
    other: o.regime,
    otherTax: o.totalTax,
    cheaper: a == b ? null : (a < b ? 'chosen' : 'other'),
    differencePaise: (a - b).abs(),
  );
}

// ---- summary (19.9) ------------------------------------------------------------------------------------------

const Map<String, String> tdsSummaryFlagLabels = {
  'TDS_EXCESS': 'More TDS than due',
  'TDS_CAPPED_NET': 'Reduced to fit the pay',
  'TDS_OVERRIDDEN': 'Set by hand',
  'NO_PAN_206AA': 'No PAN',
  'RULES_CHANGED': 'Newer rules',
  'RULES_SUPERSEDED': 'Rules superseded',
  'TDS_SHORTFALL_FINAL': 'TDS shortfall on last payslip',
  'TDS_MANUAL_REPLACED': 'Manual TDS replaced',
  'PREV_EMPLOYER_UNVERIFIED': 'Previous employer not verified',
  'NO_DECLARATION': 'No declaration',
  'REGIME_DEFAULT': 'Default regime',
  'PERQUISITE_REVIEW': 'Loan perquisite',
  'LEAVE_ENCASH_REVIEW': 'Leave encashment',
};

class TdsSummaryQuery {
  const TdsSummaryQuery({this.fy, this.q = '', this.flag});
  final String? fy;
  final String q;
  final String? flag;

  @override
  bool operator ==(Object other) => other is TdsSummaryQuery && other.fy == fy && other.q == q && other.flag == flag;

  @override
  int get hashCode => Object.hash(fy, q, flag);
}

typedef TdsSummaryKey = (TdsSummaryQuery query, int page);

class TdsSummaryItem {
  const TdsSummaryItem({
    required this.user,
    required this.regime,
    required this.totalTax,
    required this.tdsPaidSoFar,
    required this.remainingTax,
    required this.suggestedThisMonth,
    required this.manualThisMonth,
    this.flags = const [],
  });
  final EmployeeRef user;
  final String regime;
  final String totalTax;
  final String tdsPaidSoFar;
  final String remainingTax;
  final String suggestedThisMonth;
  final String manualThisMonth;
  final List<String> flags;

  /// Suggested and the payslip's figure differ (worth a look).
  bool get differs => _paise(suggestedThisMonth) != _paise(manualThisMonth);

  factory TdsSummaryItem.fromJson(Map<String, dynamic> j) => TdsSummaryItem(
        user: EmployeeRef.parse(j['user']) ?? const EmployeeRef(id: '', employeeId: '', fullName: ''),
        regime: _s(j['regime']),
        totalTax: _opt(j['total_tax']) ?? '0.00',
        tdsPaidSoFar: _opt(j['tds_paid_so_far']) ?? '0.00',
        remainingTax: _opt(j['remaining_tax']) ?? '0.00',
        suggestedThisMonth: _opt(j['suggested_this_month']) ?? '0.00',
        manualThisMonth: _opt(j['manual_this_month']) ?? '0.00',
        flags: _strings(j['flags']),
      );
}

class TdsSummaryPage {
  const TdsSummaryPage({
    required this.items,
    required this.page,
    required this.pageSize,
    required this.total,
    this.totalTax = '0.00',
    this.tdsPaidSoFar = '0.00',
    this.remainingTax = '0.00',
  });
  final List<TdsSummaryItem> items;
  final int page;
  final int pageSize;
  final int total;

  /// Totals cover the whole filtered set, not just this page.
  final String totalTax;
  final String tdsPaidSoFar;
  final String remainingTax;

  bool get hasMore => page * pageSize < total;

  factory TdsSummaryPage.fromJson(Map<String, dynamic> j) {
    final t = _m(j['totals']);
    final items = [for (final e in _list(j['items'])) TdsSummaryItem.fromJson(e)];
    return TdsSummaryPage(
      items: items,
      page: _i(j['page']) == 0 ? 1 : _i(j['page']),
      pageSize: _i(j['page_size']) == 0 ? 20 : _i(j['page_size']),
      total: j['total'] == null ? items.length : _i(j['total']),
      totalTax: _opt(t['total_tax']) ?? '0.00',
      tdsPaidSoFar: _opt(t['tds_paid_so_far']) ?? '0.00',
      remainingTax: _opt(t['remaining_tax']) ?? '0.00',
    );
  }
}

/// Family argument of the preview: (user id, fy, period, regime).
typedef TdsPreviewKey = (String userId, String? fy, String? period, String? regime);

// ---- errors -------------------------------------------------------------------------------------------------

/// Plain text for the 5c codes, or null when the code is not one of them. Never includes request values.
String? tdsErrorMessage(ApiException e) {
  switch (e.code) {
    case 'TDS_ENGINE_OFF':
      return 'The TDS engine is switched off. An administrator can switch it on in Payroll, Settings.';
    case 'TAX_RULES_NOT_VERIFIED':
      return 'The tax rules for this financial year are not verified yet, so tax cannot be worked out. '
          'Ask HR to verify them under Tax, Rules.';
    case 'INVALID_PERIOD':
      return 'Choose a month inside the financial year.';
    case 'VALIDATION_ERROR':
      final f = e.details.isEmpty ? '' : e.details.first.field;
      switch (f) {
        case 'tds_override_reason':
          return 'Say why the TDS is changed by hand (3 to 200 characters). The payroll run is applying the '
              'computed TDS, so a change needs a reason.';
        case 'period':
          return 'Choose a month inside the financial year.';
        case 'bsr_code':
          return 'The BSR code has exactly 7 digits.';
        case 'challan_serial':
          return 'The challan serial number has 1 to 5 digits.';
        case 'deposited_on':
          return 'Enter the date the tax was deposited.';
        case 'fy':
        case 'quarter':
          return 'Choose a valid financial year and quarter.';
      }
      return null;
    default:
      return null;
  }
}

/// 24Q: a quarter with no published month cannot be exported at all.
String tax24qErrorMessage(ApiException e) {
  if (e.code == 'RUN_NOT_PUBLISHED') {
    return 'No month of this quarter has a published payroll run yet, so there is nothing to export. '
        'Publish the run first.';
  }
  return phase5cErrorMessage(e);
}

/// Everything the TDS screens say about an error; falls back to the 5b texts and then the common ones.
String phase5cErrorMessage(ApiException e) => tdsErrorMessage(e) ?? phase5bErrorMessage(e);

/// For the payslip editor, where the 5a texts (publish requests) also apply.
String payslipEditorErrorMessage(ApiException e) => tdsErrorMessage(e) ?? phase5ErrorMessage(e);

String p5cMsg(Object e) => e is ApiException ? phase5cErrorMessage(e) : 'Something went wrong. Please try again.';

/// Codes meaning the engine cannot be used at all right now (shown as an empty state, not an error).
bool tdsUnavailable(Object e) =>
    e is ApiException && (e.code == 'TDS_ENGINE_OFF' || e.code == 'TAX_RULES_NOT_VERIFIED');

/// For the employee's own estimate: no hints about admin screens.
String myProjectionMessage(Object e) {
  if (e is ApiException) {
    switch (e.code) {
      case 'TDS_ENGINE_OFF':
        return 'Tax estimates are not switched on in your company yet.';
      case 'TAX_RULES_NOT_VERIFIED':
        return 'The tax rules for this financial year are not verified yet, so an estimate is not available.';
    }
  }
  return p5cMsg(e);
}

/// Flags an employee sees on their own estimate.
const List<String> projectionFlags = ['REGIME_DEFAULT', 'NO_DECLARATION', 'NO_PAN_206AA', 'TDS_EXCESS'];

/// Validates the FY text for the exports.
String? validateExportFy(String? fy) => isValidFy(fy) ? null : 'Choose a valid financial year';
