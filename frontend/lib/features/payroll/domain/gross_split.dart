/// "Split from gross" helper for the salary structure form.
///
/// Company rule: basic is [grossBasicPercent] of gross, HRA is
/// [basicHraPercent] of basic, and the remainder goes into one allowance.
library;

/// Basic as a percentage of the monthly gross.
const int grossBasicPercent = 50;

/// HRA as a percentage of basic (40% of 50% = 20% of gross).
const int basicHraPercent = 40;

/// Code and name of the allowance that receives the remainder (not a reserved
/// code, api-contract.md section 13.2).
const String splitAllowanceCode = 'SPECIAL';
const String splitAllowanceName = 'Special allowance';

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

/// Splits [grossRupees] (whole rupees, >= 0) by the company rule.
GrossSplit splitGross(int grossRupees) {
  final g = grossRupees < 0 ? 0 : grossRupees;
  final basic = _pctHalfUp(g, grossBasicPercent);
  final hra = _pctHalfUp(basic, basicHraPercent);
  return GrossSplit(basic: basic, hra: hra, allowance: g - basic - hra);
}
