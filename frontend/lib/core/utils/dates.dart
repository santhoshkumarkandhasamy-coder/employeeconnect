/// Tiny hand-rolled date helpers (no intl dependency).
library;

const List<String> monthNames = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
];

const List<String> weekdayShort = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

String two(int n) => n.toString().padLeft(2, '0');

/// `2026-10-21`.
String ymd(DateTime d) => '${d.year.toString().padLeft(4, '0')}-${two(d.month)}-${two(d.day)}';

/// `2026-10`.
String monthKey(DateTime d) => '${d.year.toString().padLeft(4, '0')}-${two(d.month)}';

/// Parses `YYYY-MM-DD` into a local date, or null.
DateTime? parseYmd(String? s) {
  if (s == null) return null;
  final m = RegExp(r'^(\d{4})-(\d{2})-(\d{2})$').firstMatch(s);
  if (m == null) return null;
  final mo = int.parse(m[2]!);
  final d = DateTime(int.parse(m[1]!), mo, int.parse(m[3]!));
  return d.month == mo ? d : null;
}

/// Parses `YYYY-MM` into the first day of that month, or null.
DateTime? parseMonth(String key) {
  final m = RegExp(r'^(\d{4})-(\d{2})$').firstMatch(key);
  if (m == null) return null;
  final mo = int.parse(m[2]!);
  if (mo < 1 || mo > 12) return null;
  return DateTime(int.parse(m[1]!), mo);
}

/// `2026-12` + 1 = `2027-01`.
String shiftMonth(String key, int delta) {
  final base = parseMonth(key) ?? DateTime.now();
  return monthKey(DateTime(base.year, base.month + delta));
}

/// `October 2026`.
String monthTitle(String key) {
  final d = parseMonth(key);
  return d == null ? key : '${monthNames[d.month - 1]} ${d.year}';
}

/// First and last day of [key] as `YYYY-MM-DD`.
({String from, String to}) monthRange(String key) {
  final d = parseMonth(key) ?? DateTime.now();
  return (from: ymd(DateTime(d.year, d.month)), to: ymd(DateTime(d.year, d.month + 1, 0)));
}

int daysInMonth(int year, int month) => DateTime(year, month + 1, 0).day;

/// Parses an ISO-8601 timestamp into device-local time, or null.
DateTime? parseTimestamp(Object? v) => v is String ? DateTime.tryParse(v)?.toLocal() : null;

/// `09:05` (24h, device-local).
String formatTime(DateTime t) => '${two(t.hour)}:${two(t.minute)}';

/// `Wed, 21 Oct 2026`.
String formatDayLong(DateTime d) =>
    '${weekdayShort[d.weekday - 1]}, ${d.day} ${monthNames[d.month - 1].substring(0, 3)} ${d.year}';

/// `7h 30m`, `45m`, `2h`.
String formatDuration(int minutes) {
  final m = minutes < 0 ? 0 : minutes;
  final h = m ~/ 60;
  final r = m % 60;
  if (h == 0) return '${r}m';
  return r == 0 ? '${h}h' : '${h}h ${r}m';
}
