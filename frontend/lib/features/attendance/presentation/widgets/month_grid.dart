import 'package:flutter/material.dart';

import '../../../../core/utils/dates.dart';
import '../../domain/attendance_models.dart';

/// Status colour, tuned for light and dark. Never the only signal: cells also
/// carry a semantic label and the legend lists the names.
Color statusColor(DayStatus s, Brightness b) {
  final dark = b == Brightness.dark;
  return switch (s) {
    DayStatus.present => dark ? const Color(0xFF66BB6A) : const Color(0xFF2E7D32),
    DayStatus.late => dark ? const Color(0xFFFFA726) : const Color(0xFFE65100),
    DayStatus.halfDay => dark ? const Color(0xFFFFEE58) : const Color(0xFFF9A825),
    DayStatus.absent => dark ? const Color(0xFFEF5350) : const Color(0xFFC62828),
    DayStatus.onLeave => dark ? const Color(0xFF64B5F6) : const Color(0xFF1565C0),
    DayStatus.holiday => dark ? const Color(0xFFBA68C8) : const Color(0xFF6A1B9A),
    DayStatus.weeklyOff => dark ? const Color(0xFF90A4AE) : const Color(0xFF607D8B),
    DayStatus.future || DayStatus.unknown => Colors.transparent,
  };
}

/// Statuses shown in the legend.
const List<DayStatus> legendStatuses = [
  DayStatus.present,
  DayStatus.late,
  DayStatus.halfDay,
  DayStatus.absent,
  DayStatus.onLeave,
  DayStatus.holiday,
  DayStatus.weeklyOff,
];

/// Simple Monday-first month grid.
class MonthGrid extends StatelessWidget {
  const MonthGrid({
    required this.month,
    required this.days,
    required this.onDayTap,
    super.key,
    this.today,
  });

  /// `YYYY-MM`.
  final String month;
  final List<CalendarDay> days;
  final ValueChanged<CalendarDay> onDayTap;

  /// Highlighted day; defaults to now.
  final DateTime? today;

  @override
  Widget build(BuildContext context) {
    final first = parseMonth(month);
    if (first == null) return const SizedBox.shrink();
    final byDay = {for (final d in days) d.date.day: d};
    final lead = first.weekday - 1; // Monday-first
    final total = daysInMonth(first.year, first.month);
    final cells = lead + total;
    final rows = (cells / 7).ceil();
    final now = today ?? DateTime.now();
    final theme = Theme.of(context);

    return Column(
      children: [
        Row(
          children: [
            for (final w in weekdayShort)
              Expanded(
                child: Center(
                  child: Text(w, style: theme.textTheme.labelSmall),
                ),
              ),
          ],
        ),
        const SizedBox(height: 4),
        for (var r = 0; r < rows; r++)
          Row(
            children: [
              for (var c = 0; c < 7; c++)
                Expanded(child: _cell(context, r * 7 + c - lead + 1, total, first, byDay, now)),
            ],
          ),
      ],
    );
  }

  Widget _cell(
    BuildContext context,
    int day,
    int total,
    DateTime first,
    Map<int, CalendarDay> byDay,
    DateTime now,
  ) {
    if (day < 1 || day > total) return const SizedBox(height: 48);
    final theme = Theme.of(context);
    final entry = byDay[day];
    final status = entry?.status ?? DayStatus.unknown;
    final color = statusColor(status, theme.brightness);
    final filled = color != Colors.transparent;
    final isToday = now.year == first.year && now.month == first.month && now.day == day;
    final date = DateTime(first.year, first.month, day);
    final label = '${formatDayLong(date)}, ${status == DayStatus.unknown ? 'no data' : status.label}'
        '${entry?.note != null ? ', ${entry!.note}' : ''}';
    final fg = filled
        ? (ThemeData.estimateBrightnessForColor(color) == Brightness.dark ? Colors.white : Colors.black)
        : theme.colorScheme.onSurface;

    return Padding(
      padding: const EdgeInsets.all(2),
      child: Semantics(
        button: entry != null,
        label: label,
        excludeSemantics: true,
        child: InkWell(
          borderRadius: BorderRadius.circular(10),
          onTap: entry == null ? null : () => onDayTap(entry),
          child: Container(
            height: 44,
            alignment: Alignment.center,
            decoration: BoxDecoration(
              color: color,
              borderRadius: BorderRadius.circular(10),
              border: isToday ? Border.all(color: theme.colorScheme.primary, width: 2) : null,
            ),
            child: Text('$day', style: TextStyle(color: fg, fontWeight: isToday ? FontWeight.bold : null)),
          ),
        ),
      ),
    );
  }
}

class StatusLegend extends StatelessWidget {
  const StatusLegend({super.key});

  @override
  Widget build(BuildContext context) {
    final b = Theme.of(context).brightness;
    return Wrap(
      spacing: 12,
      runSpacing: 4,
      children: [
        for (final s in legendStatuses)
          Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                width: 12,
                height: 12,
                decoration: BoxDecoration(color: statusColor(s, b), shape: BoxShape.circle),
              ),
              const SizedBox(width: 4),
              Text(s.label, style: Theme.of(context).textTheme.labelSmall),
            ],
          ),
      ],
    );
  }
}
