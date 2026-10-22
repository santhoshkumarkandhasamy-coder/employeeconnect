/// Client-side mirror of the server password policy (plan 01 section 3).
/// The server stays authoritative; this gives instant feedback.
class PasswordRuleResult {
  const PasswordRuleResult(this.label, this.passed);
  final String label;
  final bool passed;
}

List<PasswordRuleResult> evaluatePassword(
  String pw, {
  String? employeeId,
  String? fullName,
  String? current,
}) {
  final lower = pw.toLowerCase();
  final id = employeeId?.trim().toLowerCase() ?? '';
  final nameTokens = (fullName ?? '')
      .toLowerCase()
      .split(RegExp(r'\s+'))
      .where((t) => t.length >= 3);
  return [
    PasswordRuleResult(
      '8 to 128 characters',
      pw.runes.length >= 8 && pw.runes.length <= 128,
    ),
    PasswordRuleResult('An uppercase letter', RegExp('[A-Z]').hasMatch(pw)),
    PasswordRuleResult('A lowercase letter', RegExp('[a-z]').hasMatch(pw)),
    PasswordRuleResult('A number', RegExp('[0-9]').hasMatch(pw)),
    PasswordRuleResult('A symbol', RegExp('[^A-Za-z0-9]').hasMatch(pw)),
    PasswordRuleResult(
      'Does not contain your ID or name',
      !(id.isNotEmpty && lower.contains(id)) &&
          !nameTokens.any(lower.contains),
    ),
    if (current != null && current.isNotEmpty)
      PasswordRuleResult('Different from current password', pw != current),
  ];
}

bool passwordIsValid(
  String pw, {
  String? employeeId,
  String? fullName,
  String? current,
}) =>
    evaluatePassword(pw, employeeId: employeeId, fullName: fullName, current: current)
        .every((r) => r.passed);

String? validateEmployeeId(String? v) {
  final s = v?.trim() ?? '';
  if (s.isEmpty) return 'Enter your Employee ID';
  if (s.length > 32) return 'Employee ID is too long';
  return null;
}

String? validateRequired(String? v, String message) =>
    (v == null || v.isEmpty) ? message : null;
