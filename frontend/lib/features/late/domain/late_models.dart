import '../../../core/utils/dates.dart';

class LateRecord {
  const LateRecord({
    required this.id,
    required this.workDate,
    required this.minutesLate,
    this.checkInAt,
    this.shiftStart,
    this.graceMinutes,
  });
  final String id;
  final String workDate;
  final int minutesLate;
  final DateTime? checkInAt;
  final String? shiftStart;
  final int? graceMinutes;

  factory LateRecord.fromJson(Map<String, dynamic> j) => LateRecord(
        id: '${j['id'] ?? ''}',
        workDate: '${j['work_date'] ?? ''}',
        minutesLate: (j['minutes_late'] as num?)?.toInt() ?? 0,
        checkInAt: parseTimestamp(j['check_in_at']),
        shiftStart: j['shift_start'] as String?,
        graceMinutes: (j['grace_minutes'] as num?)?.toInt(),
      );
}

class LateSummary {
  const LateSummary({
    required this.month,
    this.lateCount = 0,
    this.totalMinutesLate = 0,
    this.averageMinutesLate = 0,
  });
  final String month;
  final int lateCount;
  final int totalMinutesLate;
  final double averageMinutesLate;

  factory LateSummary.fromJson(Map<String, dynamic> j) => LateSummary(
        month: '${j['month'] ?? ''}',
        lateCount: (j['late_count'] as num?)?.toInt() ?? 0,
        totalMinutesLate: (j['total_minutes_late'] as num?)?.toInt() ?? 0,
        averageMinutesLate: (j['average_minutes_late'] as num?)?.toDouble() ?? 0,
      );
}

/// `21.7` -> `21.7 min`, `20.0` -> `20 min`.
String formatAverageMinutes(double v) {
  final r = (v * 10).round() / 10;
  return r == r.roundToDouble() ? '${r.round()} min' : '$r min';
}
