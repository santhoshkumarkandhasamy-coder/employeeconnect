/// "Split from gross" helper for the salary structure form.
///
/// Company rule: basic is a percentage of gross, HRA a percentage of basic, and
/// the remainder goes into one allowance. The numbers come from the server
/// payroll settings (contract 18.11, plan 16 F9); the constants below are the
/// defaults used until they are loaded or when the settings do not carry them.
/// This is a local preview only: the server is authoritative.
library;

/// Basic as a percentage of the monthly gross.
const int grossBasicPercent = 50;

/// HRA as a percentage of basic (40% of 50% = 20% of gross).
const int basicHraPercent = 40;

/// Code and name of the allowance that receives the remainder (not a reserved
/// code, api-contract.md section 13.2).
const String splitAllowanceCode = 'SPECIAL';
const String splitAllowanceName = 'Special allowance';

/// The split rule of the company (`payroll.split_*` settings).
class SplitRule {
  const SplitRule({
    this.basicPct = grossBasicPercent,
    this.hraPctOfBasic = basicHraPercent,
    this.allowanceCode = splitAllowanceCode,
    this.allowanceName = splitAllowanceName,
  });

  /// Reads the rule from the settings payload; any missing or invalid value
  /// keeps its default.
  factory SplitRule.fromSettings(Map<String, Object?> s) {
    int pct(String key, int fallback, int min) {
      final v = s[key];
      final n = v is num && v == v.roundToDouble() ? v.toInt() : null;
      return n != null && n >= min && n <= 100 ? n : fallback;
    }

    String text(String key, String fallback) {
      final v = s[key];
      return v is String && v.trim().isNotEmpty ? v.trim() : fallback;
    }

    return SplitRule(
      basicPct: pct('payroll.split_basic_pct', grossBasicPercent, 1),
      hraPctOfBasic: pct('payroll.split_hra_pct_of_basic', basicHraPercent, 0),
      allowanceCode: text('payroll.split_allowance_code', splitAllowanceCode).toUpperCase(),
      allowanceName: text('payroll.split_allowance_name', splitAllowanceName),
    );
  }

  static const SplitRule defaults = SplitRule();

  final int basicPct;
  final int hraPctOfBasic;
  final String allowanceCode;
  final String allowanceName;

  @override
  bool operator ==(Object other) =>
      other is SplitRule &&
      other.basicPct == basicPct &&
      other.hraPctOfBasic == hraPctOfBasic &&
      other.allowanceCode == allowanceCode &&
      other.allowanceName == allowanceName;

  @override
  int get hashCode => Object.hash(basicPct, hraPctOfBasic, allowanceCode, allowanceName);
}

/// Whole-rupee split of a gross amount. [allowance] is the remainder, so
/// `basic + hra + allowance == gross` always.
class GrossSplit {
  const GrossSplit({required this.basic, required this.hra, required this.allowance});
  final int basic;
  final int hra;
  final int allowance;

  int get total => basic + hra + allowance;
}

int _pctHalfUp(int value, int pct) => (value * pct + 50) ~/ 100;

/// Splits [grossRupees] (whole rupees, >= 0) by [rule] (default: the company
/// defaults).
GrossSplit splitGross(int grossRupees, {SplitRule rule = SplitRule.defaults}) {
  final g = grossRupees < 0 ? 0 : grossRupees;
  final basic = _pctHalfUp(g, rule.basicPct);
  final hraRaw = _pctHalfUp(basic, rule.hraPctOfBasic);
  final hra = hraRaw > g - basic ? g - basic : hraRaw; // never negative remainder
  return GrossSplit(basic: basic, hra: hra, allowance: g - basic - hra);
}
