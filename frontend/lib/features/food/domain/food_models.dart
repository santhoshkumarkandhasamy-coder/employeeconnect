import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../leave/domain/leave_models.dart' show PersonSummary;

/// One day of the signed-in user's food enrollment (api-contract section 6).
/// Only a Yes/No answer exists: `needFood == null` means "not answered yet"
/// (counted as No by the server).
class FoodDay {
  const FoodDay({
    required this.date,
    this.needFood,
    this.canChange = true,
    this.cutoffAt,
    this.isHoliday = false,
    this.isWeeklyOff = false,
  });

  /// `YYYY-MM-DD` (office calendar date, as sent by the server).
  final String date;
  final bool? needFood;
  final bool canChange;
  final DateTime? cutoffAt;
  final bool isHoliday;

  /// Weekly off of the user's shift (contract adds `is_weekly_off`).
  final bool isWeeklyOff;

  DateTime? get day => parseYmd(date);

  FoodDay copyWith({bool? needFood, bool? canChange, DateTime? cutoffAt}) => FoodDay(
        date: date,
        needFood: needFood ?? this.needFood,
        canChange: canChange ?? this.canChange,
        cutoffAt: cutoffAt ?? this.cutoffAt,
        isHoliday: isHoliday,
        isWeeklyOff: isWeeklyOff,
      );

  static FoodDay? tryParse(Object? v) {
    if (v is! Map) return null;
    final date = v['date'];
    if (date is! String || parseYmd(date) == null) return null;
    final need = v['need_food'];
    return FoodDay(
      date: date,
      needFood: need is bool ? need : null,
      canChange: v['can_change'] as bool? ?? true,
      cutoffAt: parseTimestamp(v['cutoff_at']),
      isHoliday: v['is_holiday'] as bool? ?? false,
      isWeeklyOff: v['is_weekly_off'] as bool? ?? false,
    );
  }

  /// Cache shape (nothing sensitive in here).
  Map<String, Object?> toJson() => {
        'date': date,
        'need_food': needFood,
        'can_change': canChange,
        'cutoff_at': cutoffAt?.toUtc().toIso8601String(),
        'is_holiday': isHoliday,
        'is_weekly_off': isWeeklyOff,
      };

  static List<FoodDay> parseList(Object json) {
    final raw = json is Map ? json['items'] : null;
    return [
      if (raw is List)
        for (final e in raw)
          if (tryParse(e) case final d?) d,
    ]..sort((a, b) => a.date.compareTo(b.date));
  }
}

enum CutoffPhase { holiday, open, closingSoon, locked }

/// How a day should be presented, computed against SERVER time.
class CutoffStatus {
  const CutoffStatus(this.phase, this.label, {this.remaining});
  final CutoffPhase phase;
  final String label;
  final Duration? remaining;

  bool get editable => phase == CutoffPhase.open || phase == CutoffPhase.closingSoon;
}

/// `6:00 PM`.
String formatClock12(DateTime t) {
  final h = t.hour % 12 == 0 ? 12 : t.hour % 12;
  return '$h:${two(t.minute)} ${t.hour < 12 ? 'AM' : 'PM'}';
}

/// `Wed 21 Oct`.
String formatDayShort(DateTime d) =>
    '${weekdayShort[d.weekday - 1]} ${d.day} ${monthNames[d.month - 1].substring(0, 3)}';

/// Estimated server time: device clock plus the offset learned from the
/// server's `Date` header. This keeps the countdown right when the phone
/// clock is wrong. The server remains the authority (`can_change`, 409).
DateTime serverNow(DateTime deviceNow, Duration offset) => deviceNow.add(offset);

const Duration closingSoonWindow = Duration(hours: 2);

/// Pure cut-off rule: change is allowed while `now < cutoff_at` AND the
/// server said `can_change`. A cached `can_change: true` is never trusted
/// past its `cutoff_at`.
CutoffStatus cutoffStatus(FoodDay d, DateTime now) {
  if (d.isHoliday || d.isWeeklyOff) {
    return const CutoffStatus(CutoffPhase.holiday, 'Holiday or day off - no food needed');
  }
  final cut = d.cutoffAt;
  final passed = cut != null && !now.isBefore(cut);
  if (!d.canChange || passed) {
    return CutoffStatus(
      CutoffPhase.locked,
      cut == null
          ? 'Locked - cut-off has passed'
          : 'Locked - cut-off was ${formatClock12(cut)}, ${formatDayShort(cut)}',
    );
  }
  if (cut == null) return const CutoffStatus(CutoffPhase.open, 'Open for changes');
  final left = cut.difference(now);
  if (left < const Duration(hours: 24)) {
    final soon = left <= closingSoonWindow;
    final mins = left.inMinutes < 1 ? 1 : left.inMinutes;
    return CutoffStatus(
      soon ? CutoffPhase.closingSoon : CutoffPhase.open,
      'Change within ${formatDuration(mins)} (by ${formatClock12(cut)})',
      remaining: left,
    );
  }
  return CutoffStatus(
    CutoffPhase.open,
    'Change before ${formatClock12(cut)}, ${formatDayShort(cut)}',
    remaining: left,
  );
}

/// Drops cached days that are already in the past (by estimated server date).
List<FoodDay> upcomingOnly(List<FoodDay> days, DateTime now) {
  final today = ymd(now);
  return [for (final d in days) if (d.date.compareTo(today) >= 0) d];
}

/// Days the "set for the week" action may touch: editable days (not holiday,
/// not locked) within [span] days of the first listed day.
List<FoodDay> weekTargets(List<FoodDay> days, DateTime now, {int span = 7}) {
  final up = upcomingOnly(days, now);
  if (up.isEmpty) return const [];
  final start = parseYmd(up.first.date)!;
  final end = DateTime(start.year, start.month, start.day + span);
  return [
    for (final d in up)
      if (d.day!.isBefore(end) && cutoffStatus(d, now).editable) d,
  ];
}

class FoodBulkFailure {
  const FoodBulkFailure(this.date, this.code);
  final String date;
  final String code;
}

class FoodBulkResult {
  const FoodBulkResult({this.items = const [], this.failed = const []});
  final List<FoodDay> items;
  final List<FoodBulkFailure> failed;

  static FoodBulkResult parse(Object json) {
    final m = json is Map ? json : const <String, dynamic>{};
    final f = m['failed'];
    return FoodBulkResult(
      items: FoodDay.parseList(m),
      failed: [
        if (f is List)
          for (final e in f)
            if (e is Map) FoodBulkFailure('${e['date'] ?? ''}', '${e['code'] ?? ''}'),
      ],
    );
  }
}

// ---- admin daily report ----------------------------------------------------

class FoodEmployee {
  const FoodEmployee({required this.person, required this.needFood, this.onLeave = false});
  final PersonSummary person;
  final bool needFood;
  final bool onLeave;
}

class FoodDailyReport {
  const FoodDailyReport({
    required this.date,
    this.cutoffAt,
    this.countYes = 0,
    this.countNo = 0,
    this.countUnanswered = 0,
    this.employees = const [],
  });
  final String date;
  final DateTime? cutoffAt;
  final int countYes;
  final int countNo;
  final int countUnanswered;
  final List<FoodEmployee> employees;

  factory FoodDailyReport.fromJson(Map<String, dynamic> j) => FoodDailyReport(
        date: '${j['date'] ?? ''}',
        cutoffAt: parseTimestamp(j['cutoff_at']),
        countYes: (j['count_yes'] as num?)?.toInt() ?? 0,
        countNo: (j['count_no'] as num?)?.toInt() ?? 0,
        countUnanswered: (j['count_unanswered'] as num?)?.toInt() ?? 0,
        employees: [
          if (j['employees'] is List)
            for (final e in j['employees'] as List)
              if (e is Map<String, dynamic>)
                if (PersonSummary.parse(e['user']) case final p?)
                  FoodEmployee(
                    person: p,
                    needFood: e['need_food'] == true,
                    onLeave: e['on_leave'] as bool? ?? false,
                  ),
        ],
      );
}

const String noDepartment = 'No department';

/// Employees who need food, grouped by department (A-Z, "No department"
/// last), people sorted by name inside each group.
Map<String, List<FoodEmployee>> groupByDepartment(List<FoodEmployee> list) {
  final groups = <String, List<FoodEmployee>>{};
  for (final e in list) {
    if (!e.needFood) continue;
    final raw = e.person.department?.trim() ?? '';
    groups.putIfAbsent(raw.isEmpty ? noDepartment : raw, () => []).add(e);
  }
  final keys = groups.keys.toList()
    ..sort((a, b) {
      if (a == noDepartment) return 1;
      if (b == noDepartment) return -1;
      return a.toLowerCase().compareTo(b.toLowerCase());
    });
  return {
    for (final k in keys)
      k: groups[k]!
        ..sort((a, b) => a.person.display.toLowerCase().compareTo(b.person.display.toLowerCase())),
  };
}

// ---- permissions + errors --------------------------------------------------

/// The report is shown only to `food.view_all` holders (server enforces too).
bool canViewFoodReport(List<String> permissions) => permissions.contains('food.view_all');

bool canExportFood(List<String> permissions) =>
    permissions.contains('export.all') || permissions.contains('export.team');

String foodErrorMessage(ApiException e) {
  switch (e.code) {
    case 'FOOD_CUTOFF_PASSED':
      return 'The cut-off time has passed, so this day can no longer be changed.';
    case 'INVALID_DATE':
      return 'Food cannot be set for that date (past, holiday, day off or too far ahead).';
    case 'VALIDATION_ERROR':
      return 'Please check the details and try again.';
    case 'FORBIDDEN':
      return 'You do not have access to this.';
    case 'INVALID_DATE_RANGE':
    case 'RANGE_TOO_LARGE':
      return 'Choose a shorter date range.';
    case 'RATE_LIMITED':
      return 'Too many attempts. Please wait a moment and try again.';
    default:
      return e.message;
  }
}
