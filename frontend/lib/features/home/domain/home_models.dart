import '../../../core/utils/dates.dart';
import '../../leave/domain/leave_models.dart' show PersonSummary, formatDays;

/// `GET /home` (api-contract section 2). The server returns `null` for a part
/// that failed, so every part here is nullable and the screen shows what it
/// has.

class HomeAttendance {
  const HomeAttendance({this.checkInAt, this.checkOutAt, this.status});
  final DateTime? checkInAt;
  final DateTime? checkOutAt;
  final String? status;

  static HomeAttendance? parse(Object? v) => v is Map
      ? HomeAttendance(
          checkInAt: parseTimestamp(v['check_in_at']),
          checkOutAt: parseTimestamp(v['check_out_at']),
          status: v['status'] as String?,
        )
      : null;

  /// Same wording as the attendance card (`todayStatusText`).
  String get text {
    final inAt = checkInAt;
    if (inAt == null) return 'Not checked in yet';
    final late = status == 'late' ? ' (late)' : '';
    final out = checkOutAt;
    return out == null
        ? 'Checked in at ${formatTime(inAt)}$late'
        : 'In ${formatTime(inAt)}$late, out ${formatTime(out)}';
  }
}

class HomeLeaveBalance {
  const HomeLeaveBalance({required this.type, required this.remaining});
  final String type;
  final double remaining;

  String get label => '$type ${formatDays(remaining)}';
}

class HomePermissionRemaining {
  const HomePermissionRemaining({required this.count, required this.minutes});
  final int count;
  final int minutes;

  String get label => '$count left, ${formatDuration(minutes)}';
}

class HomeFood {
  const HomeFood({required this.date, this.needFood, this.canChange = false});
  final String date;
  final bool? needFood;
  final bool canChange;

  HomeFood copyWith({bool? needFood}) =>
      HomeFood(date: date, needFood: needFood ?? this.needFood, canChange: canChange);
}

enum CelebrationType { birthday, anniversary }

class Celebration {
  const Celebration({required this.user, required this.type, required this.date, this.years});
  final PersonSummary user;
  final CelebrationType type;

  /// `YYYY-MM-DD` of the next occurrence (month/day matter, never the birth year).
  final String date;
  final int? years;

  /// `Happy birthday, Asha!` style message the user can copy.
  String wish() {
    final name = user.display.split(' ').first;
    return type == CelebrationType.birthday
        ? 'Happy birthday, $name! Wishing you a wonderful year ahead.'
        : 'Happy anniversary, $name! Wishing you many more happy years together.';
  }

  bool isToday(DateTime now) => date == ymd(now);

  String get typeLabel => type == CelebrationType.birthday ? 'Birthday' : 'Anniversary';

  /// `Today`, `Tomorrow`, `Thu 22 Oct`.
  String whenLabel(DateTime now) {
    final d = parseYmd(date);
    if (d == null) return '';
    final diff = DateTime.utc(d.year, d.month, d.day)
        .difference(DateTime.utc(now.year, now.month, now.day))
        .inDays;
    if (diff == 0) return 'Today';
    if (diff == 1) return 'Tomorrow';
    return '${weekdayShort[d.weekday - 1]} ${d.day} ${monthNames[d.month - 1].substring(0, 3)}';
  }

  static Celebration? tryParse(Object? v) {
    if (v is! Map) return null;
    final user = PersonSummary.parse(v['user']);
    final date = v['date'];
    if (user == null || date is! String || parseYmd(date) == null) return null;
    return Celebration(
      user: user,
      type: v['type'] == 'anniversary' ? CelebrationType.anniversary : CelebrationType.birthday,
      date: date,
      years: (v['years'] as num?)?.toInt(),
    );
  }
}

/// Today's celebrations first, then by date, then name.
List<Celebration> sortCelebrations(List<Celebration> list) => [...list]..sort((a, b) {
    final c = a.date.compareTo(b.date);
    return c != 0 ? c : a.user.display.toLowerCase().compareTo(b.user.display.toLowerCase());
  });

class HomeData {
  const HomeData({
    this.attendance,
    this.leaveBalances,
    this.permission,
    this.unreadAnnouncements,
    this.pendingRequests,
    this.food,
    this.celebrations = const [],
  });

  final HomeAttendance? attendance;
  final List<HomeLeaveBalance>? leaveBalances;
  final HomePermissionRemaining? permission;
  final int? unreadAnnouncements;
  final int? pendingRequests;
  final HomeFood? food;
  final List<Celebration> celebrations;

  factory HomeData.fromJson(Map<String, dynamic> j) {
    final bal = j['leave_balances'];
    final perm = j['permission_remaining'];
    final food = j['food_today'];
    final cel = j['celebrations'];
    return HomeData(
      attendance: HomeAttendance.parse(j['today_attendance']),
      leaveBalances: bal is List
          ? [
              for (final e in bal)
                if (e is Map)
                  HomeLeaveBalance(
                    type: '${e['leave_type'] ?? ''}',
                    remaining: (e['remaining'] as num?)?.toDouble() ?? 0,
                  ),
            ]
          : null,
      permission: perm is Map
          ? HomePermissionRemaining(
              count: (perm['count'] as num?)?.toInt() ?? 0,
              minutes: (perm['minutes'] as num?)?.toInt() ?? 0,
            )
          : null,
      unreadAnnouncements: (j['unread_announcements'] as num?)?.toInt(),
      pendingRequests: (j['pending_requests'] as num?)?.toInt(),
      food: food is Map && food['date'] is String
          ? HomeFood(
              date: food['date'] as String,
              needFood: food['need_food'] is bool ? food['need_food'] as bool : null,
              canChange: food['can_change'] == true,
            )
          : null,
      celebrations: sortCelebrations([
        if (cel is List)
          for (final e in cel)
            if (Celebration.tryParse(e) case final c?) c,
      ]),
    );
  }

  /// True when the server returned nothing usable (every part failed).
  bool get isEmpty =>
      attendance == null &&
      leaveBalances == null &&
      permission == null &&
      unreadAnnouncements == null &&
      pendingRequests == null &&
      food == null &&
      celebrations.isEmpty;
}

String greeting(DateTime now) {
  if (now.hour < 12) return 'Good morning';
  if (now.hour < 17) return 'Good afternoon';
  return 'Good evening';
}
