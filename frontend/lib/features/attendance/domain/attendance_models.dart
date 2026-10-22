import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';

enum DayStatus {
  present('Present'),
  late('Late'),
  halfDay('Half day'),
  absent('Absent'),
  onLeave('On leave'),
  holiday('Holiday'),
  weeklyOff('Weekly off'),
  future('Upcoming'),
  unknown('Unknown');

  const DayStatus(this.label);
  final String label;

  static DayStatus parse(Object? v) => switch (v) {
        'present' => present,
        'late' => late,
        'half_day' => halfDay,
        'absent' => absent,
        'on_leave' => onLeave,
        'holiday' => holiday,
        'weekly_off' => weeklyOff,
        'future' => future,
        _ => unknown,
      };
}

class AttendanceRecord {
  const AttendanceRecord({
    required this.id,
    required this.workDate,
    required this.status,
    this.checkInAt,
    this.checkOutAt,
    this.minutesLate = 0,
    this.workedMinutes,
    this.withinFence = true,
    this.distanceM,
    this.selfieUrl,
    this.autoClosed = false,
    this.shiftStart,
    this.shiftEnd,
  });

  final String id;
  final String workDate;
  final DayStatus status;
  final DateTime? checkInAt;
  final DateTime? checkOutAt;
  final int minutesLate;
  final int? workedMinutes;
  final bool withinFence;
  final num? distanceM;
  final String? selfieUrl;
  final bool autoClosed;
  final String? shiftStart;
  final String? shiftEnd;

  bool get checkedIn => checkInAt != null;
  bool get checkedOut => checkOutAt != null;

  factory AttendanceRecord.fromJson(Map<String, dynamic> j) {
    final shift = j['shift'];
    return AttendanceRecord(
      id: '${j['id'] ?? ''}',
      workDate: '${j['work_date'] ?? ''}',
      status: DayStatus.parse(j['status']),
      checkInAt: parseTimestamp(j['check_in_at']),
      checkOutAt: parseTimestamp(j['check_out_at']),
      minutesLate: (j['minutes_late'] as num?)?.toInt() ?? 0,
      workedMinutes: (j['worked_minutes'] as num?)?.toInt(),
      withinFence: j['within_fence'] as bool? ?? true,
      distanceM: j['distance_m'] as num?,
      selfieUrl: j['selfie_url'] as String?,
      autoClosed: j['auto_closed'] as bool? ?? false,
      shiftStart: shift is Map ? shift['start'] as String? : null,
      shiftEnd: shift is Map ? shift['end'] as String? : null,
    );
  }

  /// `GET /attendance/today` returns a record or `{ "record": null }`.
  static AttendanceRecord? fromToday(Map<String, dynamic> j) {
    if (j.containsKey('record')) {
      final r = j['record'];
      return r is Map<String, dynamic> ? AttendanceRecord.fromJson(r) : null;
    }
    return j['id'] == null ? null : AttendanceRecord.fromJson(j);
  }
}

class Paged<T> {
  const Paged({required this.items, required this.page, required this.total, required this.pageSize});
  final List<T> items;
  final int page;
  final int total;
  final int pageSize;

  bool get hasMore => page * pageSize < total;

  static Paged<T> parse<T>(Map<String, dynamic> j, T Function(Map<String, dynamic>) item) {
    final raw = j['items'];
    final items = <T>[
      if (raw is List)
        for (final e in raw)
          if (e is Map<String, dynamic>) item(e),
    ];
    return Paged<T>(
      items: items,
      page: (j['page'] as num?)?.toInt() ?? 1,
      pageSize: (j['page_size'] as num?)?.toInt() ?? (items.isEmpty ? 20 : items.length),
      total: (j['total'] as num?)?.toInt() ?? items.length,
    );
  }
}

class CalendarDay {
  const CalendarDay({required this.date, required this.status, this.holidayName, this.leaveType});
  final DateTime date;
  final DayStatus status;
  final String? holidayName;
  final String? leaveType;

  /// Extra text for a day detail: holiday name or leave type.
  String? get note => holidayName ?? leaveType;
}

class CalendarMonth {
  const CalendarMonth({required this.month, required this.days});
  final String month;
  final List<CalendarDay> days;

  factory CalendarMonth.fromJson(Map<String, dynamic> j) {
    final raw = j['days'];
    final days = <CalendarDay>[
      if (raw is List)
        for (final e in raw)
          if (e is Map<String, dynamic> && parseYmd(e['date'] as String?) != null)
            CalendarDay(
              date: parseYmd(e['date'] as String?)!,
              status: DayStatus.parse(e['status']),
              holidayName: e['holiday_name'] as String?,
              leaveType: e['leave_type'] as String?,
            ),
    ];
    return CalendarMonth(month: '${j['month'] ?? ''}', days: days);
  }
}

class AttendanceSummary {
  const AttendanceSummary({
    required this.month,
    this.present = 0,
    this.late = 0,
    this.absent = 0,
    this.onLeave = 0,
    this.halfDay = 0,
    this.totalWorkedMinutes = 0,
  });
  final String month;
  final int present;
  final int late;
  final int absent;
  final int onLeave;
  final int halfDay;
  final int totalWorkedMinutes;

  factory AttendanceSummary.fromJson(Map<String, dynamic> j) {
    int n(String k) => (j[k] as num?)?.toInt() ?? 0;
    return AttendanceSummary(
      month: '${j['month'] ?? ''}',
      present: n('present'),
      late: n('late'),
      absent: n('absent'),
      onLeave: n('on_leave'),
      halfDay: n('half_day'),
      totalWorkedMinutes: n('total_worked_minutes'),
    );
  }
}

/// Subset of `GET /config` used by attendance.
class AttendanceConfig {
  const AttendanceConfig({
    this.selfieRequired = false,
    this.officeLat,
    this.officeLng,
    this.radiusM,
  });
  final bool selfieRequired;
  final double? officeLat;
  final double? officeLng;
  final int? radiusM;

  static const fallback = AttendanceConfig();

  bool get hasOffice => officeLat != null && officeLng != null;

  factory AttendanceConfig.fromJson(Map<String, dynamic> j) {
    final att = j['attendance'];
    final office = j['office'];
    return AttendanceConfig(
      selfieRequired: att is Map && att['selfie_required'] == true,
      officeLat: office is Map ? (office['lat'] as num?)?.toDouble() : null,
      officeLng: office is Map ? (office['lng'] as num?)?.toDouble() : null,
      radiusM: office is Map ? (office['radius_m'] as num?)?.toInt() : null,
    );
  }
}

/// True for errors where the user must retake a fresh selfie for the punch.
bool isSelfieRetakeError(ApiException e) =>
    const {'SELFIE_REQUIRED', 'SELFIE_STALE', 'SELFIE_REUSED'}.contains(e.code);

/// User-facing text for attendance domain errors. The server no longer sends
/// distances (OUTSIDE_GEOFENCE details are only `within_fence=false`).
String attendanceErrorMessage(ApiException e) {
  switch (e.code) {
    case 'OUTSIDE_GEOFENCE':
      return 'You are outside the office area. Move closer and try again.';
    case 'LOCATION_ACCURACY_LOW':
      return 'Your GPS signal is weak. Move to an open area and try again.';
    case 'LOCATION_FIX_STALE':
      return 'Your location reading is out of date. Refresh your location, wait a moment '
          'for a fresh reading and try again.';
    case 'MOCK_LOCATION_DETECTED':
      return 'Check-in is blocked because a mock or fake location was detected. '
          'Turn off any mock location app and try again.';
    case 'SELFIE_STALE':
      return 'Your selfie has expired. Retake a fresh selfie and try again.';
    case 'SELFIE_REUSED':
      return 'That selfie was already used for another punch. Retake a new selfie and try again.';
    case 'ALREADY_CHECKED_IN':
      return 'You have already checked in today.';
    case 'ALREADY_CHECKED_OUT':
      return 'You have already checked out today.';
    case 'NOT_CHECKED_IN':
      return 'You have not checked in today.';
    case 'ON_LEAVE':
      return 'You are on approved leave today, so you cannot check in.';
    case 'SELFIE_REQUIRED':
      return 'A selfie is required. Take one and try again.';
    case 'NO_SHIFT_ASSIGNED':
      return 'No shift is assigned to you yet. Please contact HR.';
    case 'RATE_LIMITED':
      final s = e.retryAfter?.inSeconds;
      if (s == null || s <= 0) {
        return 'Too many attempts. Please wait a few minutes before trying again.';
      }
      final wait = s >= 90 ? '${(s / 60).ceil()} minutes' : '$s seconds';
      return 'Too many attempts. Please wait about $wait before trying again.';
    case 'DATA_UNREADABLE':
    case 'FILE_UNAVAILABLE':
    case 'TEMPORARILY_UNAVAILABLE':
      return 'The service is temporarily unavailable. Please try again in a few minutes.';
    default:
      return e.message;
  }
}
