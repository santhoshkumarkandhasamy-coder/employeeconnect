/// Money handling for payroll (api-contract.md section 13.0).
///
/// Money is a JSON **string** with two decimals ("25000.00"), never a double.
/// All arithmetic here is on integer paise so there is no float rounding.
library;

/// Highest amount the contract accepts, in paise (10,000,000.00).
const int maxMoneyPaise = 1000000000;

final RegExp _inputRe = RegExp(r'^\d{1,8}(\.\d{1,2})?$');

/// Parses text typed by a user (0-2 decimals, no sign, no grouping) into
/// paise. Returns null when it is not a valid amount in 0..10,000,000.00.
int? parseMoneyPaise(String? text) {
  final t = (text ?? '').trim().replaceAll(',', '');
  if (!_inputRe.hasMatch(t)) {
    // "10000000" has 8 digits and matches; anything longer does not.
    return null;
  }
  final parts = t.split('.');
  final whole = int.parse(parts[0]);
  final frac = parts.length == 1 ? 0 : int.parse(parts[1].padRight(2, '0'));
  final paise = whole * 100 + frac;
  return paise > maxMoneyPaise ? null : paise;
}

/// `1234567` paise -> `"12345.67"`.
String paiseToMoney(int paise) {
  final p = paise < 0 ? 0 : paise;
  return '${p ~/ 100}.${(p % 100).toString().padLeft(2, '0')}';
}

/// Normalises user text into the wire format, or null when invalid.
String? normalizeMoney(String? text) {
  final p = parseMoneyPaise(text);
  return p == null ? null : paiseToMoney(p);
}

/// Paise of a server money string (tolerant: null/garbage = 0).
int moneyToPaise(String? money) => parseMoneyPaise(money) ?? 0;

/// Indian digit grouping of a non-negative integer string: `1234567` ->
/// `12,34,567`.
String groupIndian(String digits) {
  if (digits.length <= 3) return digits;
  final last3 = digits.substring(digits.length - 3);
  var rest = digits.substring(0, digits.length - 3);
  final groups = <String>[];
  while (rest.length > 2) {
    groups.insert(0, rest.substring(rest.length - 2));
    rest = rest.substring(0, rest.length - 2);
  }
  if (rest.isNotEmpty) groups.insert(0, rest);
  return '${groups.join(',')},$last3';
}

/// `"1234567.5"` / `"1234567.50"` -> `"12,34,567.50"` (no currency sign).
/// Returns `-` for null and the raw text when it is not a money string.
String formatInr(String? money) {
  if (money == null) return '-';
  final p = parseMoneyPaise(money);
  if (p == null) return money;
  return '${groupIndian('${p ~/ 100}')}.${(p % 100).toString().padLeft(2, '0')}';
}

/// With the rupee sign: `Rs 12,34,567.50` is avoided on purpose, the glyph
/// is part of Roboto and every Android system font.
String formatRupees(String? money) => money == null ? '-' : '₹${formatInr(money)}';

/// Sum of money strings, in paise.
int sumPaise(Iterable<String?> amounts) {
  var total = 0;
  for (final a in amounts) {
    total += moneyToPaise(a);
  }
  return total;
}
