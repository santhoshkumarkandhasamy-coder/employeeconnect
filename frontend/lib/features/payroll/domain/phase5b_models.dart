import 'dart:convert';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import 'payroll_models.dart' show payrollErrorMessage;

/// api-contract.md section 19.6 to 19.8 and the notes in 19.15 (payroll phase 5,
/// work package 5b): tax year rules, investment declarations and proofs, Form 16.
/// In memory only. Money strings, PANs and file names are never logged, never
/// put in an exception and never persisted: no toString overrides, no print.

String _s(Object? v) => v == null ? '' : '$v';
String? _opt(Object? v) => v is String && v.isNotEmpty ? v : null;
int _i(Object? v) => v is num ? v.toInt() : (int.tryParse('$v') ?? 0);
bool _b(Object? v) => v == true;
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
EmployeeRef? _userOpt(Object? v) => EmployeeRef.parse(v);
EmployeeRef _user(Object? v) =>
    EmployeeRef.parse(v) ?? const EmployeeRef(id: '', employeeId: '', fullName: '');

/// Deep copy of decoded JSON (the editors work on a copy, never on server state).
Map<String, dynamic> deepCopyMap(Map<String, dynamic> m) =>
    (jsonDecode(jsonEncode(m)) as Map).cast<String, dynamic>();

// ---- keys, limits -------------------------------------------------------------------------

/// Needs role.assign + the caller's password (19.10).
const String form16SecondPersonKey = 'payroll.form16_requires_second_person';

const int maxDeclarationItems = 40;
const int maxProofFileBytes = 5 * 1024 * 1024;
const int defaultProofMaxFiles = 5;
const int form16MaxFiles = 20;
const int form16MaxFileBytes = 2 * 1024 * 1024;
const int form16MaxTotalBytes = 40 * 1024 * 1024;
const int annualCapPaise = 12000000000; // 120,000,000.00

// ---- financial years -----------------------------------------------------------------------

final RegExp _fyRe = RegExp(r'^(\d{4})-(\d{2})$');

/// `"2026-27"` is valid when the second part is the next year's last two digits.
bool isValidFy(String? fy) {
  final m = _fyRe.firstMatch(fy ?? '');
  if (m == null) return false;
  return (int.parse(m[1]!) + 1) % 100 == int.parse(m[2]!);
}

/// 1 April .. 31 March of [fy], or null.
(DateTime, DateTime)? fyBounds(String fy) {
  if (!isValidFy(fy)) return null;
  final y = int.parse(fy.substring(0, 4));
  return (DateTime(y, 4, 1), DateTime(y + 1, 3, 31));
}

/// Financial year that contains [d].
String fyOf(DateTime d) {
  final y = d.month >= 4 ? d.year : d.year - 1;
  return '$y-${two((y + 1) % 100)}';
}

String fyTitle(String fy) => 'FY $fy';

// ---- windows --------------------------------------------------------------------------------

class TaxWindows {
  const TaxWindows({
    this.declarationOpensOn,
    this.declarationClosesOn,
    this.proofOpensOn,
    this.proofClosesOn,
  });
  final String? declarationOpensOn;
  final String? declarationClosesOn;
  final String? proofOpensOn;
  final String? proofClosesOn;

  factory TaxWindows.fromJson(Object? v) {
    final j = _m(v);
    return TaxWindows(
      declarationOpensOn: _opt(j['declaration_opens_on']),
      declarationClosesOn: _opt(j['declaration_closes_on']),
      proofOpensOn: _opt(j['proof_opens_on']),
      proofClosesOn: _opt(j['proof_closes_on']),
    );
  }

  Map<String, Object?> toJson() => {
        'declaration_opens_on': declarationOpensOn,
        'declaration_closes_on': declarationClosesOn,
        'proof_opens_on': proofOpensOn,
        'proof_closes_on': proofClosesOn,
      };

  static bool _within(String? a, String? b, DateTime today) {
    final from = parseYmd(a);
    final to = parseYmd(b);
    if (from == null || to == null) return false;
    final t = DateTime(today.year, today.month, today.day);
    return !t.isBefore(from) && !t.isAfter(to);
  }

  bool declarationOpen(DateTime today) => _within(declarationOpensOn, declarationClosesOn, today);
  bool proofOpen(DateTime today) => _within(proofOpensOn, proofClosesOn, today);

  String get declarationText => _rangeText(declarationOpensOn, declarationClosesOn);
  String get proofText => _rangeText(proofOpensOn, proofClosesOn);

  static String _rangeText(String? a, String? b) {
    final from = parseYmd(a);
    final to = parseYmd(b);
    if (from == null || to == null) return 'Closed';
    return '${formatDayLong(from)} to ${formatDayLong(to)}';
  }
}

/// Both dates of a window or neither; opens <= closes; inside the FY (19.6). Null = fine.
String? validateWindows(TaxWindows w, String fy) {
  final b = fyBounds(fy);
  if (b == null) return 'Choose a valid financial year';
  String? one(String name, String? a, String? c) {
    if ((a == null) != (c == null)) return 'Give both dates of the $name window, or neither to close it.';
    if (a == null) return null;
    final from = parseYmd(a);
    final to = parseYmd(c);
    if (from == null || to == null) return 'Enter the $name dates as valid dates.';
    if (from.isAfter(to)) return 'The $name window cannot close before it opens.';
    if (from.isBefore(b.$1) || to.isAfter(b.$2)) return 'The $name window must be inside ${fyTitle(fy)}.';
    return null;
  }

  return one('declaration', w.declarationOpensOn, w.declarationClosesOn) ??
      one('proof', w.proofOpensOn, w.proofClosesOn);
}

// ---- tax years -------------------------------------------------------------------------------

class TaxOpenVersion {
  const TaxOpenVersion({required this.id, required this.version, required this.status});
  final String id;
  final int version;
  final String status;
}

class TaxYear {
  const TaxYear({
    required this.fy,
    required this.windows,
    this.verifiedVersion,
    this.openVersion,
    this.current = false,
  });
  final String fy;
  final int? verifiedVersion;
  final TaxOpenVersion? openVersion;
  final TaxWindows windows;
  final bool current;

  bool get verified => verifiedVersion != null;

  factory TaxYear.fromJson(Map<String, dynamic> j) {
    final o = j['open_version'];
    return TaxYear(
      fy: _s(j['fy']),
      verifiedVersion: j['verified_version'] == null ? null : _i(j['verified_version']),
      openVersion: o is Map
          ? TaxOpenVersion(id: _s(o['id']), version: _i(o['version']), status: _s(o['status']))
          : null,
      windows: TaxWindows.fromJson(j['windows']),
      current: _b(j['current']),
    );
  }

  static List<TaxYear> parseList(Map<String, dynamic> j) => [for (final e in _list(j['items'])) TaxYear.fromJson(e)];
}

class MyTaxYear {
  const MyTaxYear({required this.fy, required this.windows, required this.verified, required this.current});
  final String fy;
  final TaxWindows windows;
  final bool verified;
  final bool current;

  factory MyTaxYear.fromJson(Map<String, dynamic> j) => MyTaxYear(
        fy: _s(j['fy']),
        windows: TaxWindows.fromJson(j['windows']),
        verified: _b(j['verified']),
        current: _b(j['current']),
      );

  static List<MyTaxYear> parseList(Map<String, dynamic> j) =>
      [for (final e in _list(j['items'])) MyTaxYear.fromJson(e)];
}

// ---- tax rules (19.6) ---------------------------------------------------------------------------

const Map<String, String> rulesStatusLabels = {
  'draft': 'Draft',
  'submitted': 'Waiting for verification',
  'verified': 'Verified',
  'superseded': 'Replaced by a newer version',
};

String rulesStatusLabel(String s) => rulesStatusLabels[s] ?? s;

class TaxRules {
  const TaxRules({
    required this.id,
    required this.fy,
    required this.version,
    required this.status,
    required this.rulesSha256,
    required this.rules,
    required this.versionRow,
    this.note,
    this.attestation,
    this.createdBy,
    this.lastEditedBy,
    this.submittedBy,
    this.submittedAt,
    this.verifiedBy,
    this.verifiedAt,
    this.windows = const TaxWindows(),
  });

  final String id;
  final String fy;
  final int version;
  final String status;
  final String rulesSha256;
  final Map<String, dynamic> rules;
  final int versionRow;
  final String? note;
  final String? attestation;
  final EmployeeRef? createdBy;
  final EmployeeRef? lastEditedBy;
  final EmployeeRef? submittedBy;
  final DateTime? submittedAt;
  final EmployeeRef? verifiedBy;
  final DateTime? verifiedAt;
  final TaxWindows windows;

  bool get isDraft => status == 'draft';
  bool get isSubmitted => status == 'submitted';
  bool get isVerified => status == 'verified';

  /// The seed copy starts with `UNVERIFIED`: it cannot be submitted until the note is edited (19.15).
  bool get noteIsUnverified => (note ?? '').trimLeft().startsWith('UNVERIFIED');

  List<SectionDef> get sections => SectionDef.parseList(rules['sections']);

  factory TaxRules.fromJson(Map<String, dynamic> j) {
    final rules = _m(j['rules']);
    final inner = _opt(rules['note']);
    return TaxRules(
      id: _s(j['id']),
      fy: _s(j['fy']),
      version: _i(j['version']),
      status: _s(j['status']),
      rulesSha256: _s(j['rules_sha256']),
      rules: rules,
      versionRow: _i(j['version_row']),
      note: _opt(j['note']) ?? inner,
      attestation: _opt(j['attestation']),
      createdBy: _userOpt(j['created_by']),
      lastEditedBy: _userOpt(j['last_edited_by']),
      submittedBy: _userOpt(j['submitted_by']),
      submittedAt: parseTimestamp(j['submitted_at']),
      verifiedBy: _userOpt(j['verified_by']),
      verifiedAt: parseTimestamp(j['verified_at']),
      windows: TaxWindows.fromJson(j['windows']),
    );
  }

  static List<TaxRules> parseList(Map<String, dynamic> j) => [for (final e in _list(j['items'])) TaxRules.fromJson(e)];
}

/// Cosmetic maker-checker for rules: the creator, last editor and submitter cannot verify or reject.
/// The server also knows the append-only `contributors` list and answers 403 SECOND_PERSON_REQUIRED.
bool canDecideRules(TaxRules r, String? selfId) {
  if (selfId == null || selfId.isEmpty || !r.isSubmitted) return false;
  return selfId != r.createdBy?.id && selfId != r.lastEditedBy?.id && selfId != r.submittedBy?.id;
}

String? cannotDecideRulesReason(TaxRules r, String? selfId) {
  if (!r.isSubmitted || selfId == null || canDecideRules(r, selfId)) return null;
  return 'You made, edited or submitted these rules. A different person must verify or reject them.';
}

// ---- rules validation, diff ---------------------------------------------------------------------

class RulesIssue {
  const RulesIssue(this.path, this.message);
  final String path;
  final String message;
}

final RegExp _rateRe = RegExp(r'^\d{1,2}(\.\d{1,2})?$');
final RegExp _moneyRe = RegExp(r'^\d{1,9}(\.\d{1,2})?$');
final RegExp _sectionCodeRe = RegExp(r'^[A-Z0-9_]{2,16}$');

/// Money in a rules document: 0.00 .. 120,000,000.00, up to two decimals.
bool isRulesMoney(Object? v) {
  if (v is! String || !_moneyRe.hasMatch(v)) return false;
  final p = _paise(v);
  return p != null && p <= annualCapPaise;
}

int? _paise(String? text) {
  final t = (text ?? '').trim().replaceAll(',', '');
  if (!_moneyRe.hasMatch(t)) return null;
  final parts = t.split('.');
  final frac = parts.length == 1 ? 0 : int.parse(parts[1].padRight(2, '0'));
  return int.parse(parts[0]) * 100 + frac;
}

/// Two-decimal text for a typed money value, or null when invalid.
String? normalizeRulesMoney(String? text) {
  final p = _paise(text);
  if (p == null || p > annualCapPaise) return null;
  return '${p ~/ 100}.${(p % 100).toString().padLeft(2, '0')}';
}

bool isRulesRate(Object? v) {
  if (v is! String || !_rateRe.hasMatch(v)) return false;
  final n = double.tryParse(v);
  return n != null && n >= 0 && n <= 50;
}

/// Slabs contiguous from 0.00, ascending, exactly one open-ended last slab, rates 0..50 (19.6).
List<RulesIssue> validateSlabs(String path, Object? slabs) {
  final out = <RulesIssue>[];
  if (slabs is! List || slabs.isEmpty) return [RulesIssue(path, 'Add at least one slab.')];
  int? prevTo = 0;
  for (var i = 0; i < slabs.length; i++) {
    final s = slabs[i];
    final p = '$path[$i]';
    if (s is! Map) {
      out.add(RulesIssue(p, 'Not a slab.'));
      continue;
    }
    final from = _paise('${s['from']}');
    final toRaw = s['to'];
    final last = i == slabs.length - 1;
    if (from == null) {
      out.add(RulesIssue('$p.from', 'Enter the start amount.'));
    } else if (prevTo != null && from != prevTo) {
      out.add(RulesIssue('$p.from', i == 0 ? 'The first slab starts at 0.00.' : 'Each slab starts where the one before ends.'));
    }
    if (!isRulesRate(s['rate_pct'])) out.add(RulesIssue('$p.rate_pct', 'Rate must be 0 to 50 with up to 2 decimals.'));
    if (last) {
      if (toRaw != null) out.add(RulesIssue('$p.to', 'The last slab has no upper limit.'));
      prevTo = null;
    } else {
      final to = toRaw == null ? null : _paise('$toRaw');
      if (to == null) {
        out.add(RulesIssue('$p.to', 'Enter the end amount (only the last slab is open-ended).'));
        prevTo = from;
      } else {
        if (from != null && to <= from) out.add(RulesIssue('$p.to', 'The end must be above the start.'));
        prevTo = to;
      }
    }
  }
  return out;
}

/// Checks the parts the editor changes. The server validates everything again (422 with a JSON path).
List<RulesIssue> validateRulesLocally(Map<String, dynamic> rules) {
  final out = <RulesIssue>[];
  final regimes = _m(rules['regimes']);
  final secs = _list(rules['sections']);
  final codes = <String>{};
  for (var i = 0; i < secs.length; i++) {
    final c = secs[i]['code'];
    final p = 'rules.sections[$i]';
    if (c is! String || !_sectionCodeRe.hasMatch(c)) {
      out.add(RulesIssue('$p.code', 'Use 2-16 capitals, digits or _.'));
    } else if (!codes.add(c)) {
      out.add(RulesIssue('$p.code', 'Duplicate section code.'));
    }
    if (_s(secs[i]['name']).trim().isEmpty) out.add(RulesIssue('$p.name', 'Give the section a name.'));
    for (final k in ['limit', 'senior_limit']) {
      final v = secs[i][k];
      if (v != null && !isRulesMoney(v)) out.add(RulesIssue('$p.$k', 'Enter an amount, or leave it empty for no limit.'));
    }
  }
  if (secs.length > 40) out.add(const RulesIssue('rules.sections', 'At most 40 sections.'));
  for (final e in regimes.entries) {
    final r = _m(e.value);
    final p = 'rules.regimes.${e.key}';
    out.addAll(validateSlabs('$p.slabs', r['slabs']));
    if (!isRulesMoney(r['standard_deduction'])) out.add(RulesIssue('$p.standard_deduction', 'Enter an amount.'));
    final rb = _m(r['rebate']);
    if (!isRulesMoney(rb['income_limit'])) out.add(RulesIssue('$p.rebate.income_limit', 'Enter an amount.'));
    if (!isRulesMoney(rb['max'])) out.add(RulesIssue('$p.rebate.max', 'Enter an amount.'));
    final sur = _list(r['surcharge']);
    int prev = -1;
    for (var i = 0; i < sur.length; i++) {
      final above = _paise('${sur[i]['above']}');
      if (above == null) {
        out.add(RulesIssue('$p.surcharge[$i].above', 'Enter an amount.'));
      } else {
        if (above <= prev) out.add(RulesIssue('$p.surcharge[$i].above', 'Amounts must go up from row to row.'));
        prev = above;
      }
      if (!isRulesRate(sur[i]['rate_pct'])) out.add(RulesIssue('$p.surcharge[$i].rate_pct', 'Rate must be 0 to 50.'));
    }
    final allowed = _strings(r['allowed_sections']);
    for (var i = 0; i < allowed.length; i++) {
      if (!codes.contains(allowed[i])) {
        out.add(RulesIssue('$p.allowed_sections[$i]', '${allowed[i]} is not a section in this document: add it or remove it.'));
      }
    }
  }
  final cess = rules['cess_pct'];
  if (cess != null && !isRulesRate(cess)) out.add(const RulesIssue('rules.cess_pct', 'Rate must be 0 to 50.'));
  for (final g in _list(rules['group_limits'])) {
    if (!isRulesMoney(g['limit'])) out.add(RulesIssue('rules.group_limits.${g['group']}', 'Enter an amount.'));
  }
  return out;
}

class RuleChange {
  const RuleChange(this.path, this.before, this.after);
  final String path;
  final String? before;
  final String? after;
}

void _flatten(Object? v, String path, Map<String, String> out) {
  if (v is Map) {
    for (final e in v.entries) {
      _flatten(e.value, path.isEmpty ? '${e.key}' : '$path.${e.key}', out);
    }
    if (v.isEmpty) out[path] = '{}';
  } else if (v is List) {
    for (var i = 0; i < v.length; i++) {
      _flatten(v[i], '$path[$i]', out);
    }
    if (v.isEmpty) out[path] = '[]';
  } else {
    out[path] = v == null ? 'none' : '$v';
  }
}

/// Differences between two rules documents, by path ([before] = the older version).
List<RuleChange> diffRules(Map<String, dynamic> before, Map<String, dynamic> after) {
  final a = <String, String>{};
  final b = <String, String>{};
  _flatten(before, '', a);
  _flatten(after, '', b);
  final keys = {...a.keys, ...b.keys}.toList()..sort();
  return [
    for (final k in keys)
      if (a[k] != b[k]) RuleChange(k, a[k], b[k]),
  ];
}

/// Attestation text for verify: 20..500 characters (19.6).
String? validateAttestation(String? v) {
  final t = (v ?? '').trim();
  if (t.length < 20) return 'Say what you checked the rules against and when (at least 20 characters).';
  if (t.length > 500) return 'At most 500 characters.';
  return null;
}

// ---- sections and declarations (19.7) -------------------------------------------------------------

class SectionDef {
  const SectionDef({
    required this.code,
    required this.name,
    this.limit,
    this.seniorLimit,
    this.group,
    this.proofRequired = true,
    this.fields = const ['description'],
    this.landlordPanAbove,
  });
  final String code;
  final String name;
  final String? limit;
  final String? seniorLimit;
  final String? group;
  final bool proofRequired;
  final List<String> fields;
  final String? landlordPanAbove;

  bool get isHra => fields.contains('monthly_rent');

  factory SectionDef.fromJson(Map<String, dynamic> j) => SectionDef(
        code: _s(j['code']),
        name: _s(j['name']),
        limit: _opt(j['limit']),
        seniorLimit: _opt(j['senior_limit']),
        group: _opt(j['group']),
        proofRequired: j['proof_required'] != false,
        fields: _strings(j['fields']),
        landlordPanAbove: _opt(j['landlord_pan_above']),
      );

  static List<SectionDef> parseList(Object? v) => [for (final e in _list(v)) SectionDef.fromJson(e)];
}

/// Used when the verified rules cannot be read (an employee has no rules endpoint): the sections of the
/// seed in plan 17 Q-L1. The server validates every section against the verified rules (422 INVALID_SECTION).
const List<SectionDef> fallbackSections = [
  SectionDef(code: '80C', name: 'Section 80C (PF, PPF, ELSS, life insurance, tuition fees, home loan principal)'),
  SectionDef(code: '80CCD1B', name: 'NPS (additional)', limit: '50000.00'),
  SectionDef(
    code: '80D_SELF',
    name: 'Health insurance - self and family',
    limit: '25000.00',
    seniorLimit: '50000.00',
    fields: ['description', 'senior'],
  ),
  SectionDef(
    code: '24B',
    name: 'Home loan interest (self-occupied)',
    limit: '200000.00',
    fields: ['lender_name', 'lender_pan'],
  ),
  SectionDef(
    code: 'HRA',
    name: 'House rent',
    fields: ['monthly_rent', 'from_month', 'to_month', 'landlord_name', 'landlord_pan', 'city', 'metro'],
    landlordPanAbove: '100000.00',
  ),
  SectionDef(code: 'PREV_EMPLOYER', name: 'Previous employer this year (Form 12B)', fields: ['income', 'tds', 'pt', 'pf']),
  SectionDef(code: 'OTHER_INCOME', name: 'Other income', proofRequired: false),
];

enum FieldKind { text, flag, pan, month, monthlyMoney, annualMoney }

FieldKind fieldKindOf(String key) => switch (key) {
      'senior' || 'metro' => FieldKind.flag,
      'lender_pan' || 'landlord_pan' => FieldKind.pan,
      'from_month' || 'to_month' => FieldKind.month,
      'monthly_rent' => FieldKind.monthlyMoney,
      'income' || 'tds' || 'pt' || 'pf' => FieldKind.annualMoney,
      _ => FieldKind.text,
    };

const Map<String, String> fieldLabels = {
  'description': 'Description',
  'senior': 'Senior citizen',
  'lender_name': 'Lender name',
  'lender_pan': 'Lender PAN',
  'monthly_rent': 'Monthly rent',
  'from_month': 'From month (YYYY-MM)',
  'to_month': 'To month (YYYY-MM)',
  'landlord_name': 'Landlord name',
  'landlord_pan': 'Landlord PAN',
  'city': 'City',
  'metro': 'Metro city',
  'income': 'Income from previous employer',
  'tds': 'Tax deducted (TDS)',
  'pt': 'Professional tax',
  'pf': 'Provident fund',
};

String fieldLabel(String key) => fieldLabels[key] ?? key;

final RegExp _panRe = RegExp(r'^[A-Z]{5}[0-9]{4}[A-Z]$');
final RegExp _maskedPanRe = RegExp(r'^XXXXX[0-9]{4}X$');
final RegExp _monthRe = RegExp(r'^\d{4}-(0[1-9]|1[0-2])$');

bool isMaskedPan(String? v) => _maskedPanRe.hasMatch((v ?? '').trim().toUpperCase());

/// PAN like ABCDE1234F. A masked value (shown by the server) is refused: the server would store it as typed.
String? validatePan(String? v, {bool required = false}) {
  final t = (v ?? '').trim().toUpperCase();
  if (t.isEmpty) return required ? 'Enter the PAN' : null;
  if (isMaskedPan(t)) return 'This PAN is hidden for safety. Enter the full PAN again.';
  if (!_panRe.hasMatch(t)) return 'Enter a PAN like ABCDE1234F';
  return null;
}

/// Annual amount: 0.01 .. 120,000,000.00, up to two decimals.
String? validateAnnualAmount(String? v, {String label = 'Amount', bool allowZero = false}) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return '$label is required';
  final p = _paise(t);
  if (p == null) return 'Enter an amount like 150000 or 150000.50';
  if (p > annualCapPaise) return '$label can be at most 12,00,00,000';
  if (p == 0 && !allowZero) return '$label must be more than 0';
  return null;
}

String? validateMonthlyRent(String? v) {
  final err = validateAnnualAmount(v, label: 'Monthly rent');
  if (err != null) return err;
  if ((_paise(v) ?? 0) > 1000000000) return 'Monthly rent can be at most 1,00,00,000';
  return null;
}

String? validateMonthInFy(String? v, String fy) {
  final t = (v ?? '').trim();
  if (!_monthRe.hasMatch(t)) return 'Use YYYY-MM, like 2026-04';
  final b = fyBounds(fy);
  if (b == null) return null;
  final first = DateTime(int.parse(t.substring(0, 4)), int.parse(t.substring(5, 7)));
  if (first.isBefore(b.$1) || first.isAfter(b.$2)) return 'The month must be inside ${fyTitle(fy)}';
  return null;
}

/// Months from [from] to [to] inclusive (`YYYY-MM`), or null when either is invalid or reversed.
int? monthsBetween(String? from, String? to) {
  if (from == null || to == null || !_monthRe.hasMatch(from) || !_monthRe.hasMatch(to)) return null;
  final n = (int.parse(to.substring(0, 4)) - int.parse(from.substring(0, 4))) * 12 +
      int.parse(to.substring(5, 7)) -
      int.parse(from.substring(5, 7)) +
      1;
  return n < 1 ? null : n;
}

/// Annual rent in paise (monthly rent x months), or null while the inputs are incomplete.
int? hraAnnualRentPaise(Map<String, Object?> details) {
  final rent = _paise('${details['monthly_rent'] ?? ''}');
  final months = monthsBetween(_opt(details['from_month']), _opt(details['to_month']));
  if (rent == null || months == null) return null;
  return rent * months;
}

String? validateItemField(String key, String? value, String fy) => switch (fieldKindOf(key)) {
      FieldKind.flag => null,
      FieldKind.pan => validatePan(value),
      FieldKind.month => validateMonthInFy(value, fy),
      FieldKind.monthlyMoney => validateMonthlyRent(value),
      FieldKind.annualMoney => validateAnnualAmount(value, label: fieldLabel(key), allowZero: true),
      FieldKind.text => _validateText(value, fieldLabel(key)),
    };

String? _validateText(String? v, String label) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return '$label is required';
  if (t.length > 100) return 'At most 100 characters';
  return null;
}

class DeclItem {
  const DeclItem({
    required this.id,
    required this.section,
    required this.amount,
    this.details = const {},
    this.flags = const [],
  });
  final String id;
  final String section;
  final String amount;
  final Map<String, dynamic> details;
  final List<String> flags;

  factory DeclItem.fromJson(Map<String, dynamic> j) => DeclItem(
        id: _s(j['id']),
        section: _s(j['section']),
        amount: _s(j['amount']),
        details: _m(j['details']),
        flags: _strings(j['flags']),
      );
}

class DeclBlob {
  const DeclBlob({this.regime, this.items = const [], this.version, this.submittedAt});
  final String? regime;
  final List<DeclItem> items;
  final int? version;
  final DateTime? submittedAt;

  factory DeclBlob.fromJson(Object? v) {
    final j = _m(v);
    return DeclBlob(
      regime: _opt(j['regime']),
      items: [for (final e in _list(j['items'])) DeclItem.fromJson(e)],
      version: j['version'] == null ? null : _i(j['version']),
      submittedAt: parseTimestamp(j['submitted_at']),
    );
  }
}

class ProofCounts {
  const ProofCounts({this.submitted = 0, this.verified = 0, this.rejected = 0});
  final int submitted;
  final int verified;
  final int rejected;

  factory ProofCounts.fromJson(Object? v) {
    final j = _m(v);
    return ProofCounts(submitted: _i(j['submitted']), verified: _i(j['verified']), rejected: _i(j['rejected']));
  }

  bool get any => submitted + verified + rejected > 0;
}

const Map<String, String> editReasonLabels = {
  'DECLARATION_WINDOW': 'The declaration window is open.',
  'PROOF_WINDOW': 'The proof window is open: only items that need a proof can change, and the regime is fixed.',
  'UNLOCKED': 'HR unlocked this declaration for you.',
  'CLOSED': 'The declaration window is closed. Ask HR to unlock it if something must change.',
};

class TaxDeclaration {
  const TaxDeclaration({
    required this.fy,
    required this.windows,
    required this.canEdit,
    required this.editReason,
    required this.draft,
    required this.version,
    this.id,
    this.rulesVersion,
    this.submitted,
    this.dirty = false,
    this.proofs = const ProofCounts(),
    this.user,
    this.enteredBy,
    this.unlockedUntil,
  });

  final String? id;
  final String fy;
  final int? rulesVersion;
  final TaxWindows windows;
  final bool canEdit;
  final String editReason;
  final DeclBlob draft;
  final DeclBlob? submitted;
  final bool dirty;
  final ProofCounts proofs;
  final int version;
  final EmployeeRef? user;
  final EmployeeRef? enteredBy;
  final String? unlockedUntil;

  bool get isSubmitted => submitted != null;
  bool get regimeLocked => editReason == 'PROOF_WINDOW' || editReason == 'CLOSED';

  /// Proofs can be added in the proof window or while HR has unlocked the declaration.
  bool get canAddProofs => canEdit && (editReason == 'PROOF_WINDOW' || editReason == 'UNLOCKED');

  factory TaxDeclaration.fromJson(Map<String, dynamic> j) => TaxDeclaration(
        id: _opt(j['id']),
        fy: _s(j['fy']),
        rulesVersion: j['rules_version'] == null ? null : _i(j['rules_version']),
        windows: TaxWindows.fromJson(j['windows']),
        canEdit: _b(j['can_edit']),
        editReason: _s(j['edit_reason']),
        draft: DeclBlob.fromJson(j['draft']),
        submitted: j['submitted'] is Map ? DeclBlob.fromJson(j['submitted']) : null,
        dirty: _b(j['dirty']),
        proofs: ProofCounts.fromJson(j['proofs']),
        version: _i(j['version']),
        user: _userOpt(j['user']),
        enteredBy: _userOpt(j['entered_by']),
        unlockedUntil: _opt(j['unlocked_until']),
      );
}

/// One item being edited. [id] is null for a new item (the server assigns it).
class ItemDraft {
  ItemDraft({required this.section, this.id, this.amount = '', Map<String, Object?>? details, List<String>? flags})
      : details = details ?? <String, Object?>{},
        flags = flags ?? <String>[];

  String? id;
  String section;
  String amount;
  final Map<String, Object?> details;
  final List<String> flags;

  factory ItemDraft.fromItem(DeclItem i) => ItemDraft(
        id: i.id,
        section: i.section,
        amount: i.amount,
        details: Map<String, Object?>.from(i.details),
        flags: List<String>.of(i.flags),
      );

  ItemDraft copy() => ItemDraft(id: id, section: section, amount: amount, details: Map.of(details), flags: List.of(flags));

  /// The wire form. Only the fields of the section are sent; the HRA amount is computed by the server.
  Map<String, Object?> toJson(SectionDef? def) {
    final fields = def?.fields ?? const ['description'];
    final d = <String, Object?>{};
    for (final f in fields) {
      final v = details[f];
      if (v == null) continue;
      if (v is String) {
        final t = fieldKindOf(f) == FieldKind.pan ? v.trim().toUpperCase() : v.trim();
        if (t.isEmpty) continue;
        d[f] = fieldKindOf(f) == FieldKind.monthlyMoney || fieldKindOf(f) == FieldKind.annualMoney
            ? (normalizeRulesMoney(t) ?? t)
            : t;
      } else {
        d[f] = v;
      }
    }
    return {
      'id': id,
      'section': section,
      if (!(def?.isHra ?? false)) 'amount': normalizeRulesMoney(amount) ?? amount.trim(),
      'details': d,
    };
  }
}

/// Null when the item can be saved. [fy] is used for month checks.
String? validateItemDraft(ItemDraft item, SectionDef? def, String fy) {
  final d = def ?? SectionDef(code: item.section, name: item.section);
  if (!d.isHra) {
    final a = validateAnnualAmount(item.amount);
    if (a != null) return a;
  }
  for (final f in d.fields) {
    final v = item.details[f];
    final text = v is String ? v : null;
    final kind = fieldKindOf(f);
    if (kind == FieldKind.flag) continue;
    if (kind == FieldKind.pan) {
      final required = f == 'landlord_pan' && _landlordPanRequired(d, item);
      final e = validatePan(text, required: required);
      if (e != null) return '${fieldLabel(f)}: $e';
      continue;
    }
    // Only the rent and its months are required (19.15); every other field may stay empty.
    final needed = d.isHra && const {'monthly_rent', 'from_month', 'to_month'}.contains(f);
    if (!needed && (text ?? '').trim().isEmpty) continue;
    final e = validateItemField(f, text, fy);
    if (e != null) return '${fieldLabel(f)}: $e';
  }
  if (d.isHra) {
    final months = monthsBetween(_opt(item.details['from_month']), _opt(item.details['to_month']));
    if (months == null) return 'The rent period must end in the same month or later than it starts.';
  }
  return null;
}

bool _landlordPanRequired(SectionDef d, ItemDraft item) {
  final threshold = _paise(d.landlordPanAbove);
  final rent = hraAnnualRentPaise(item.details);
  return threshold != null && rent != null && rent > threshold;
}

/// Body of a draft save (employee, or HR on behalf with the password).
class DeclarationSave {
  const DeclarationSave({
    required this.fy,
    required this.expectedVersion,
    required this.regime,
    required this.items,
  });
  final String fy;
  final int expectedVersion;
  final String? regime;
  final List<Map<String, Object?>> items;

  Map<String, Object?> toJson(String? currentPassword) => {
        'fy': fy,
        'expected_version': expectedVersion,
        'regime': regime,
        'items': items,
        if (currentPassword != null) 'current_password': currentPassword,
      };
}

/// Plain text for an item flag.
const Map<String, String> itemFlagLabels = {
  'NOT_IN_REGIME': 'Not used with the chosen regime',
  'ABOVE_LIMIT': 'Above the limit: only the limit counts',
};

String itemFlagLabel(String f) => itemFlagLabels[f] ?? f;

String? validateRegimeChoice(String? regime) =>
    regime == null || regime == 'old' || regime == 'new' ? null : 'Choose the old or the new regime';

// ---- admin declaration list, proofs ---------------------------------------------------------------------

class TaxDeclarationQuery {
  const TaxDeclarationQuery({this.fy, this.status, this.regime, this.q = ''});
  final String? fy;
  final String? status;
  final String? regime;
  final String q;

  @override
  bool operator ==(Object other) =>
      other is TaxDeclarationQuery && other.fy == fy && other.status == status && other.regime == regime && other.q == q;

  @override
  int get hashCode => Object.hash(fy, status, regime, q);
}

class DeclarationListItem {
  const DeclarationListItem({
    required this.user,
    required this.fy,
    required this.status,
    this.regime,
    this.submittedAt,
    this.dirty = false,
    this.items = 0,
    this.proofs = const ProofCounts(),
    this.flags = const [],
  });
  final EmployeeRef user;
  final String fy;
  final String status;
  final String? regime;
  final DateTime? submittedAt;
  final bool dirty;
  final int items;
  final ProofCounts proofs;
  final List<String> flags;

  factory DeclarationListItem.fromJson(Map<String, dynamic> j) => DeclarationListItem(
        user: _user(j['user']),
        fy: _s(j['fy']),
        status: _s(j['status']),
        regime: _opt(j['regime']),
        submittedAt: parseTimestamp(j['submitted_at']),
        dirty: _b(j['dirty']),
        items: _i(j['items']),
        proofs: ProofCounts.fromJson(j['proofs']),
        flags: _strings(j['flags']),
      );
}

const Map<String, String> declarationStatusLabels = {
  'none': 'Nothing entered',
  'draft': 'Draft only',
  'submitted': 'Submitted',
};

String declarationStatusLabel(String s) => declarationStatusLabels[s] ?? s;

class ProofFile {
  const ProofFile({required this.id, required this.url, required this.contentType});
  final String id;
  final String url;
  final String contentType;

  bool get isPdf => contentType == 'application/pdf';
  String get extension => switch (contentType) {
        'application/pdf' => '.pdf',
        'image/png' => '.png',
        _ => '.jpg',
      };
}

const Map<String, String> proofStatusLabels = {
  'submitted': 'Waiting for HR',
  'verified': 'Verified',
  'rejected': 'Rejected',
  'cancelled': 'Cancelled',
};

String proofStatusLabel(String s) => proofStatusLabels[s] ?? s;

class TaxProof {
  const TaxProof({
    required this.id,
    required this.fy,
    required this.itemId,
    required this.section,
    required this.claimed,
    required this.status,
    required this.version,
    this.files = const [],
    this.verified,
    this.comment,
    this.verifiedAt,
    this.user,
  });
  final String id;
  final String fy;
  final String itemId;
  final String section;
  final String claimed;
  final String status;
  final int version;
  final List<ProofFile> files;
  final String? verified;
  final String? comment;
  final DateTime? verifiedAt;
  final EmployeeRef? user;

  bool get isSubmitted => status == 'submitted';
  bool get isLive => status == 'submitted' || status == 'verified';

  factory TaxProof.fromJson(Map<String, dynamic> j) => TaxProof(
        id: _s(j['id']),
        fy: _s(j['fy']),
        itemId: _s(j['item_id']),
        section: _s(j['section']),
        claimed: _s(j['claimed']),
        status: _s(j['status']),
        version: _i(j['version']),
        files: [
          for (final f in _list(j['files']))
            ProofFile(id: _s(f['id']), url: _s(f['url']), contentType: _s(f['content_type'])),
        ],
        verified: _opt(j['verified']),
        comment: _opt(j['comment']),
        verifiedAt: parseTimestamp(j['verified_at']),
        user: _userOpt(j['user']),
      );

  static List<TaxProof> parseList(Map<String, dynamic> j) => [for (final e in _list(j['items'])) TaxProof.fromJson(e)];
}

class TaxProofQuery {
  const TaxProofQuery({this.fy, this.status = 'submitted', this.userId});
  final String? fy;
  final String? status;
  final String? userId;

  @override
  bool operator ==(Object other) =>
      other is TaxProofQuery && other.fy == fy && other.status == status && other.userId == userId;

  @override
  int get hashCode => Object.hash(fy, status, userId);
}

/// The verifier is not the employee. Cosmetic: the declaration's `entered_by` is checked by the server.
bool canDecideProof(TaxProof p, String? selfId) =>
    p.isSubmitted && selfId != null && selfId.isNotEmpty && p.user?.id != selfId;

/// Verified amount: 0.00 .. claimed; 0.00 needs a comment (19.7).
String? validateVerifiedAmount(String? v, String claimed, {String? comment}) {
  final e = validateAnnualAmount(v, label: 'Verified amount', allowZero: true);
  if (e != null) return e;
  final p = _paise(v)!;
  final c = _paise(claimed);
  if (c != null && p > c) return 'The verified amount cannot be more than the amount claimed';
  if (p == 0 && (comment ?? '').trim().length < 3) return 'Say why nothing is accepted (comment of at least 3 characters)';
  return null;
}

/// Claimed amount of a new proof: 0.01 .. the submitted item amount.
String? validateClaimedAmount(String? v, String itemAmount) {
  final e = validateAnnualAmount(v, label: 'Amount claimed');
  if (e != null) return e;
  final c = _paise(itemAmount);
  if (c != null && c > 0 && _paise(v)! > c) return 'The amount claimed cannot be more than the declared amount';
  return null;
}

String? validateComment(String? v, {bool required = false}) {
  final t = (v ?? '').trim();
  if (t.isEmpty) return required ? 'Write a comment' : null;
  if (t.length < 3) return 'At least 3 characters';
  if (t.length > 200) return 'At most 200 characters';
  return null;
}

// ---- Form 16 (19.8) ----------------------------------------------------------------------------------------

const List<String> form16Kinds = ['form16', 'part_a', 'part_b', 'form12ba'];

const Map<String, String> form16KindLabels = {
  'form16': 'Form 16',
  'part_a': 'Form 16 Part A',
  'part_b': 'Form 16 Part B',
  'form12ba': 'Form 12BA',
};

String form16KindLabel(String k) => form16KindLabels[k] ?? k;

const Map<String, String> form16StatusLabels = {
  'uploaded': 'Ready to publish',
  'published': 'Published',
  'withdrawn': 'Withdrawn',
};

String form16StatusLabel(String s) => form16StatusLabels[s] ?? s;

class Form16Doc {
  const Form16Doc({
    required this.id,
    required this.user,
    required this.fy,
    required this.kind,
    required this.status,
    required this.sha256,
    this.uploadedBy,
    this.uploadedAt,
    this.publishedBy,
    this.publishedAt,
    this.withdrawnAt,
  });
  final String id;
  final EmployeeRef user;
  final String fy;
  final String kind;
  final String status;
  final String sha256;
  final EmployeeRef? uploadedBy;
  final DateTime? uploadedAt;
  final EmployeeRef? publishedBy;
  final DateTime? publishedAt;
  final DateTime? withdrawnAt;

  bool get isUploaded => status == 'uploaded';
  bool get isPublished => status == 'published';

  factory Form16Doc.fromJson(Map<String, dynamic> j) => Form16Doc(
        id: _s(j['id']),
        user: _user(j['user']),
        fy: _s(j['fy']),
        kind: _s(j['kind']),
        status: _s(j['status']),
        sha256: _s(j['sha256']),
        uploadedBy: _userOpt(j['uploaded_by']),
        uploadedAt: parseTimestamp(j['uploaded_at']),
        publishedBy: _userOpt(j['published_by']),
        publishedAt: parseTimestamp(j['published_at']),
        withdrawnAt: parseTimestamp(j['withdrawn_at']),
      );
}

class Form16Query {
  const Form16Query({this.fy, this.kind, this.status, this.q = ''});
  final String? fy;
  final String? kind;
  final String? status;
  final String q;

  @override
  bool operator ==(Object other) =>
      other is Form16Query && other.fy == fy && other.kind == kind && other.status == status && other.q == q;

  @override
  int get hashCode => Object.hash(fy, kind, status, q);
}

class Form16Match {
  const Form16Match({required this.index, required this.user, required this.sha256, this.replacesId, this.warnings = const []});
  final int index;
  final EmployeeRef user;
  final String sha256;
  final String? replacesId;
  final List<String> warnings;

  bool get replaces => replacesId != null || warnings.contains('REPLACES');
}

class Form16FileError {
  const Form16FileError({required this.index, required this.message});
  final int index;
  final String message;
}

const Map<String, String> form16ErrorLabels = {
  'UNMATCHED': 'No employee ID or PAN in the file name matches an employee.',
  'AMBIGUOUS': 'The file name names more than one employee.',
  'DUPLICATE_EMPLOYEE': 'More than one file in this upload is for the same employee.',
  'NOT_PDF': 'This is not a valid PDF.',
  'FILE_TOO_LARGE': 'The file is larger than 2 MB.',
  'DELETED_EMPLOYEE': 'The employee was removed.',
};

String form16ErrorLabel(String code) => form16ErrorLabels[code] ?? code;

class Form16DryRun {
  const Form16DryRun({
    required this.fy,
    required this.kind,
    required this.manifestSha256,
    required this.files,
    required this.matched,
    required this.errors,
  });
  final String fy;
  final String kind;
  final String manifestSha256;
  final int files;
  final List<Form16Match> matched;
  final List<Form16FileError> errors;

  /// The commit is only allowed when every file matched exactly one employee.
  bool get canCommit => errors.isEmpty && matched.isNotEmpty && matched.length == files;

  factory Form16DryRun.fromJson(Map<String, dynamic> j) => Form16DryRun(
        fy: _s(j['fy']),
        kind: _s(j['kind']),
        manifestSha256: _s(j['manifest_sha256']),
        files: _i(j['files']),
        matched: [
          for (final m in _list(j['matched']))
            Form16Match(
              index: _i(m['index']),
              user: _user(m['user']),
              sha256: _s(m['sha256']),
              replacesId: _opt(m['replaces_id']),
              warnings: _strings(m['warnings']),
            ),
        ],
        errors: [
          for (final e in _list(j['errors'])) Form16FileError(index: _i(e['index']), message: _s(e['message'])),
        ],
      );
}

class Form16CommitResult {
  const Form16CommitResult({required this.created, required this.replaced, this.batchId});
  final int created;
  final int replaced;
  final String? batchId;

  factory Form16CommitResult.fromJson(Map<String, dynamic> j) =>
      Form16CommitResult(created: _i(j['created']), replaced: _i(j['replaced']), batchId: _opt(j['upload_batch_id']));
}

const Map<String, String> form16PublishFailLabels = {
  'NOT_UPLOADED': 'No longer waiting to be published',
  'SHA_MISMATCH': 'The document changed after you reviewed it',
  'SECOND_PERSON_REQUIRED': 'You uploaded it: a different person must publish it',
  'FORBIDDEN': 'It is your own document',
};

String form16PublishFailLabel(String code) => form16PublishFailLabels[code] ?? code;

class Form16PublishResult {
  const Form16PublishResult({required this.published, this.failed = const []});
  final int published;
  final List<({String id, String reason})> failed;

  factory Form16PublishResult.fromJson(Map<String, dynamic> j) => Form16PublishResult(
        published: _i(j['published']),
        failed: [for (final f in _list(j['failed'])) (id: _s(f['id']), reason: _s(f['reason']))],
      );
}

class MyForm16 {
  const MyForm16({required this.id, required this.fy, required this.kind, this.publishedAt});
  final String id;
  final String fy;
  final String kind;
  final DateTime? publishedAt;

  factory MyForm16.fromJson(Map<String, dynamic> j) => MyForm16(
        id: _s(j['id']),
        fy: _s(j['fy']),
        kind: _s(j['kind']),
        publishedAt: parseTimestamp(j['published_at']),
      );

  static List<MyForm16> parseList(Map<String, dynamic> j) => [for (final e in _list(j['items'])) MyForm16.fromJson(e)];
}

/// One document the signed-in checker can still publish: uploaded, and not their own upload while the
/// second-person setting is on (the server decides; this only orders the choices).
bool canPublishDoc(Form16Doc d, String? selfId, {bool secondPerson = true}) {
  if (!d.isUploaded || selfId == null || selfId.isEmpty) return false;
  if (d.user.id == selfId) return false;
  if (secondPerson && d.uploadedBy?.id == selfId) return false;
  return true;
}

/// Checks the picked PDFs before upload: count, each size, total size (19.8).
String? validateForm16Selection(List<({String name, int size})> files) {
  if (files.isEmpty) return 'Choose at least one PDF.';
  if (files.length > form16MaxFiles) return 'Choose at most $form16MaxFiles files at a time.';
  var total = 0;
  for (final f in files) {
    if (!f.name.toLowerCase().endsWith('.pdf')) return 'Only PDF files can be uploaded.';
    if (f.size <= 0) return 'A file is empty.';
    if (f.size > form16MaxFileBytes) return 'Each file must be 2 MB or smaller.';
    total += f.size;
  }
  if (total > form16MaxTotalBytes) return 'The files together must be 40 MB or smaller.';
  return null;
}

// ---- errors ----------------------------------------------------------------------------------------------------

String _firstField(ApiException e) => e.details.isEmpty ? '' : e.details.first.field;

/// Plain-language text for the 5b codes; everything else falls through to [payrollErrorMessage]. Never
/// includes values from the request.
String phase5bErrorMessage(ApiException e) {
  switch (e.code) {
    case 'SECOND_PERSON_REQUIRED':
      return 'A different person must do this. You created, edited, submitted or entered it '
          '(or it is part of what you uploaded), so you cannot verify, reject or publish it.';
    case 'FORBIDDEN':
      return 'You cannot do this to your own tax data or documents. A different person must do it.';
    case 'CONFLICT':
      return 'This changed or already exists. A proof can only be added once per item: cancel the earlier one '
          'first, or wait for it to be rejected. Reload to see the current state.';
    case 'WINDOW_CLOSED':
      return 'This window is closed. Declarations and proofs can only change while their window is open '
          'or after HR unlocks them.';
    case 'REGIME_LOCKED':
      return 'The regime is fixed once the declaration window has closed. Ask HR to unlock the declaration to change it.';
    case 'TAX_RULES_NOT_VERIFIED':
      return 'The tax rules for this financial year are not verified yet, so declarations are not open.';
    case 'DRAFT_EXISTS':
      return 'A draft or a version waiting for verification already exists. Open it instead of making another.';
    case 'NOT_DRAFT':
      return 'Only a draft can be changed or submitted. Reload to see its state.';
    case 'NOT_SUBMITTED':
      return 'These rules are no longer waiting for verification. Reload to see their state.';
    case 'VERSION_CONFLICT':
      return 'Someone else changed this. Reload it and try again.';
    case 'RULES_CHANGED':
      return 'The rules changed after you opened them, so nothing was verified. Reload and review them again.';
    case 'INVALID_SECTION':
      return 'That section is not in the verified tax rules for this year. Choose another section.';
    case 'LANDLORD_PAN_REQUIRED':
      return 'The landlord PAN is required when the yearly rent is above the limit in the rules.';
    case 'ITEM_NOT_SUBMITTED':
      return 'Submit the declaration first. A proof can only be added to an item of the submitted declaration.';
    case 'AMOUNT_ABOVE_LIMIT':
      return 'The amount is above the declared amount.';
    case 'NO_CHANGE':
      return 'Nothing changed since the last submitted version.';
    case 'NOT_PENDING':
      return 'This proof was already decided or cancelled. Reload to see its state.';
    case 'INVALID_DATE':
      return 'Check the dates: the window must be inside the financial year, and an unlock date must be today or '
          'later and not after the end of the year.';
    case 'IMPORT_INVALID':
      return 'Some files cannot be matched to one employee. Fix the files listed and check them again.';
    case 'IMPORT_NOT_CHECKED':
      return 'These files were not checked in the last 15 minutes, or the check was already used. Check them again.';
    case 'IMPORT_FILE_CHANGED':
      return 'The files are not the ones that were checked. Choose them again and run the check again.';
    case 'FILE_TOO_LARGE':
      return 'A file is too large (2 MB at most for Form 16, 5 MB for a proof).';
    case 'VALIDATION_ERROR':
      final f = _firstField(e);
      final base = payrollErrorMessage(e);
      return f.startsWith('rules.') ? 'Rules: $base' : base;
    default:
      return payrollErrorMessage(e);
  }
}

String p5bMsg(Object e) => e is ApiException ? phase5bErrorMessage(e) : 'Something went wrong. Please try again.';

/// After these codes the shown record is out of date and is reloaded.
bool isStalePhase5bError(ApiException e) => const {
      'VERSION_CONFLICT',
      'NOT_DRAFT',
      'NOT_SUBMITTED',
      'NOT_PENDING',
      'RULES_CHANGED',
      'CONFLICT',
    }.contains(e.code);

/// Every value of a JSON document as `path -> text`, sorted by path (read-only review of rules).
Map<String, String> flattenJson(Object? v) {
  final out = <String, String>{};
  _flatten(v, '', out);
  return Map.fromEntries(out.entries.toList()..sort((a, b) => a.key.compareTo(b.key)));
}
