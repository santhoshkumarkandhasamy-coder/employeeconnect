/// api-contract.md 19.9 and the notes in 19.16 (payroll phase 5, work package 5c): the TDS engine's
/// `TdsComputation`. In memory only. Money strings and reasons are never logged and never put in an
/// exception: no toString overrides, no print.
library;

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

// ---- flags -------------------------------------------------------------------------------------

/// Short chip text for the payslip warnings of the engine (also used in the run review).
const Map<String, String> tdsWarningLabels = {
  'TDS_EXCESS': 'TDS already more than the tax due',
  'TDS_CAPPED_NET': 'TDS reduced to fit the pay',
  'TDS_OVERRIDDEN': 'TDS set by hand',
  'TAX_RULES_NOT_VERIFIED': 'Tax rules not verified',
  'RULES_CHANGED': 'Newer tax rules: refresh the run',
  'TDS_SHORTFALL_FINAL': 'TDS cannot be collected: set a TDS override',
  'TDS_MANUAL_REPLACED': 'Manual TDS replaced by the computed figure',
  'PREV_EMPLOYER_UNVERIFIED': 'Previous employer details not verified',
  'RULES_SUPERSEDED': 'Tax rules superseded',
  'NO_PAN_206AA': 'No PAN: higher TDS rate',
  'PERQUISITE_REVIEW': 'Check loan perquisite',
  'LEAVE_ENCASH_REVIEW': 'Check leave encashment tax',
  'REGIME_DEFAULT': 'Default regime used',
  'NO_DECLARATION': 'No declaration submitted',
};

/// A sentence for each flag, for the TDS card.
const Map<String, String> tdsFlagExplanations = {
  'TDS_EXCESS': 'More TDS has already been deducted this year than the tax now works out to, so this month '
      'is nil. Nothing is refunded through payroll.',
  'TDS_CAPPED_NET': 'The computed TDS is more than the pay left this month, so only part is deducted. '
      'The rest is not lost: the next months pick it up.',
  'TDS_OVERRIDDEN': 'The TDS on this payslip was typed in by hand instead of using the computed figure.',
  'TAX_RULES_NOT_VERIFIED': 'The tax rules for this financial year are not verified, so nothing was computed. '
      'The manual TDS figure is used.',
  'RULES_CHANGED': 'Newer verified tax rules exist than the ones this run uses. This blocks processing: '
      'refresh the draft run to use them.',
  'TDS_SHORTFALL_FINAL': 'The TDS due cannot be fully collected from the pay left, and this is the last payslip. '
      'This blocks processing: set a TDS override with a reason.',
  'TDS_MANUAL_REPLACED': 'A TDS figure typed earlier was replaced by the computed one. Set an override or hand '
      'the line back to the engine to confirm.',
  'PREV_EMPLOYER_UNVERIFIED': 'Income and TDS from a previous employer are not counted until HR verifies the '
      'proof (Form 12B).',
  'RULES_SUPERSEDED': 'The rules used by this run have been superseded. This run cannot be refreshed, so it keeps '
      'the rules it was made with.',
  'NO_PAN_206AA': 'The employee has no valid PAN, so the higher no-PAN rate applies.',
  'PERQUISITE_REVIEW': 'The employee has an outstanding loan above the limit in the rules. The tax on the loan '
      'benefit is not computed: check it and use a manual figure if needed.',
  'LEAVE_ENCASH_REVIEW': 'This final payslip includes leave encashment. Its tax exemption is not computed: '
      'check it and use a manual figure if needed.',
  'REGIME_DEFAULT': 'The employee has not chosen a regime, so the default regime from the rules is used.',
  'NO_DECLARATION': 'No investment declaration has been submitted, so no deductions are counted.',
};

String tdsFlagLabel(String f) => tdsWarningLabels[f] ?? f;
String tdsFlagExplanation(String f) => tdsFlagExplanations[f] ?? tdsFlagLabel(f);

// ---- labels ------------------------------------------------------------------------------------

String regimeLabel(String? r) => switch (r) {
      'old' => 'Old regime',
      'new' => 'New regime',
      _ => r ?? '-',
    };

String tdsModeLabel(String m) => switch (m) {
      'apply' => 'Applied automatically',
      'suggest' => 'Suggestion only',
      _ => m,
    };

String proofBasisLabel(String b) => switch (b) {
      'verified' => 'Verified proofs',
      'declared' => 'Declared amounts',
      _ => b,
    };

// ---- computation ----------------------------------------------------------------------------------

/// The `annual` block, in the order the statement shows it.
class TdsAnnual {
  const TdsAnnual(this.values);
  final Map<String, String> values;

  static const List<(String key, String label)> rows = [
    ('salary_paid', 'Salary paid so far this year'),
    ('salary_this_month', 'Salary this month'),
    ('salary_projected', 'Salary expected for the rest of the year'),
    ('previous_employer_income', 'Income from a previous employer'),
    ('other_income', 'Other income'),
    ('exemptions', 'Exemptions (such as HRA)'),
    ('standard_deduction', 'Standard deduction'),
    ('pt', 'Professional tax'),
    ('chapter_via', 'Deductions for investments and expenses'),
    ('taxable_income', 'Taxable income'),
    ('tax_on_income', 'Tax on the taxable income'),
    ('rebate', 'Rebate'),
    ('surcharge', 'Surcharge'),
    ('cess', 'Cess'),
    ('total_tax', 'Total tax for the year'),
  ];

  String? operator [](String key) => values[key];

  String get totalTax => values['total_tax'] ?? '0.00';
  String get taxableIncome => values['taxable_income'] ?? '0.00';

  factory TdsAnnual.fromJson(Object? v) {
    final j = _m(v);
    return TdsAnnual({
      for (final e in j.entries)
        if (e.value != null) e.key: '${e.value}',
    });
  }
}

class TdsOverride {
  const TdsOverride({required this.amount, required this.reason});
  final String amount;
  final String reason;
}

class TdsSectionStep {
  const TdsSectionStep({required this.section, required this.counted, required this.allowed});
  final String section;
  final String counted;
  final String allowed;
}

/// `steps`: how the figure was reached. Never holds a PAN or landlord data.
class TdsSteps {
  const TdsSteps({
    this.sections = const [],
    this.hraExemption,
    this.averageRatePct,
    this.monthTaxable,
    this.recurringTotalTax,
  });
  final List<TdsSectionStep> sections;
  final String? hraExemption;
  final String? averageRatePct;
  final String? monthTaxable;
  final String? recurringTotalTax;

  factory TdsSteps.fromJson(Object? v) {
    final j = _m(v);
    return TdsSteps(
      sections: [
        for (final s in _list(j['sections']))
          TdsSectionStep(
            section: _s(s['section']),
            counted: _s(s['declared_or_verified']),
            allowed: _s(s['allowed']),
          ),
      ],
      hraExemption: _opt(j['hra_exemption']),
      averageRatePct: _opt(j['average_rate_pct']),
      monthTaxable: _opt(j['month_taxable']),
      recurringTotalTax: _opt(j['recurring_total_tax']),
    );
  }
}

class TdsComputation {
  const TdsComputation({
    required this.fy,
    required this.mode,
    required this.regime,
    required this.annual,
    required this.suggested,
    this.rulesVersion,
    this.regimeSource = 'default',
    this.declarationVersion,
    this.proofBasis = 'declared',
    this.remainingMonths = 0,
    this.tdsPaidSoFar = '0.00',
    this.previousEmployerTds = '0.00',
    this.remainingTax = '0.00',
    this.oneTimeTax = '0.00',
    this.applied,
    this.override,
    this.flags = const [],
    this.shortfall,
    this.steps,
    this.computedAt,
  });

  final String fy;

  /// `suggest` | `apply`
  final String mode;
  final int? rulesVersion;
  final String regime;

  /// `declared` | `default`
  final String regimeSource;
  final int? declarationVersion;

  /// `declared` | `verified`
  final String proofBasis;
  final int remainingMonths;
  final TdsAnnual annual;
  final String tdsPaidSoFar;
  final String previousEmployerTds;
  final String remainingTax;
  final String oneTimeTax;
  final String suggested;

  /// Money string in mode `apply`, else null.
  final String? applied;

  /// Admin only; the employee projection never carries it.
  final TdsOverride? override;
  final List<String> flags;
  final String? shortfall;
  final TdsSteps? steps;
  final String? computedAt;

  bool get isApply => mode == 'apply';
  bool get isOverridden => override != null;
  bool get regimeIsDefault => regimeSource == 'default';
  bool get hasShortfall => shortfall != null && shortfall != '0.00' && shortfall != '0';

  factory TdsComputation.fromJson(Map<String, dynamic> j) {
    final rules = _m(j['rules']);
    final ov = j['override'];
    return TdsComputation(
      fy: _s(j['fy']),
      mode: _opt(j['mode']) ?? 'suggest',
      rulesVersion: rules['version'] == null ? null : _i(rules['version']),
      regime: _s(j['regime']),
      regimeSource: _opt(j['regime_source']) ?? 'default',
      declarationVersion: j['declaration_version'] == null ? null : _i(j['declaration_version']),
      proofBasis: _opt(j['proof_basis']) ?? 'declared',
      remainingMonths: _i(j['remaining_months']),
      annual: TdsAnnual.fromJson(j['annual']),
      tdsPaidSoFar: _opt(j['tds_paid_so_far']) ?? '0.00',
      previousEmployerTds: _opt(j['previous_employer_tds']) ?? '0.00',
      remainingTax: _opt(j['remaining_tax']) ?? '0.00',
      oneTimeTax: _opt(j['one_time_tax']) ?? '0.00',
      suggested: _opt(j['suggested']) ?? '0.00',
      applied: _opt(j['applied']),
      override: ov is Map ? TdsOverride(amount: _s(ov['amount']), reason: _s(ov['reason'])) : null,
      flags: _strings(j['flags']),
      shortfall: _opt(j['shortfall']),
      steps: j['steps'] is Map ? TdsSteps.fromJson(j['steps']) : null,
      computedAt: _opt(j['computed_at']),
    );
  }

  static TdsComputation? parse(Object? v) => v is Map ? TdsComputation.fromJson(v.cast<String, dynamic>()) : null;
}

/// `other_regime`: the same computation with the other regime.
class OtherRegime {
  const OtherRegime({required this.regime, required this.totalTax});
  final String regime;
  final String totalTax;

  static OtherRegime? parse(Object? v) => v is Map && _opt(v['regime']) != null
      ? OtherRegime(regime: _s(v['regime']), totalTax: _opt(v['total_tax']) ?? '0.00')
      : null;
}

/// Answer of preview and of `/me/tax/projection`.
class TdsResult {
  const TdsResult({required this.computation, this.otherRegime, this.estimate = false});
  final TdsComputation computation;
  final OtherRegime? otherRegime;
  final bool estimate;

  factory TdsResult.fromJson(Map<String, dynamic> j) => TdsResult(
        computation: TdsComputation.fromJson(_m(j['computation'])),
        otherRegime: OtherRegime.parse(j['other_regime']),
        estimate: j['estimate'] == true,
      );
}
