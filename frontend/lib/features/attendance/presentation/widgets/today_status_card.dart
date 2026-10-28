import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../core/utils/dates.dart';
import '../../domain/attendance_models.dart';
import '../attendance_providers.dart';

/// One-line summary of today's attendance for the home card.
String todayStatusText(AttendanceRecord? r) {
  if (r == null || !r.checkedIn) return 'Not checked in yet';
  final inAt = formatTime(r.checkInAt!);
  final late = r.status == DayStatus.late ? ' (${r.minutesLate} min late)' : '';
  // Night shift: today's open record may belong to the previous work date.
  final today = DateTime.now();
  final todayKey = '${today.year.toString().padLeft(4, '0')}-'
      '${today.month.toString().padLeft(2, '0')}-${today.day.toString().padLeft(2, '0')}';
  final prev = r.workDate.isNotEmpty && r.workDate != todayKey ? ' (${r.workDate})' : '';
  if (!r.checkedOut) return 'Checked in at $inAt$prev$late';
  return 'In $inAt$late, out ${formatTime(r.checkOutAt!)}';
}

/// Home-screen card driven by `GET /attendance/today`. Drop it into the Home
/// screen; [onTap] should navigate to the Attendance tab.
class TodayStatusCard extends ConsumerWidget {
  const TodayStatusCard({super.key, this.onTap, this.fallbackSubtitle});
  final VoidCallback? onTap;

  /// Shown instead of the error text when `/attendance/today` cannot be
  /// loaded but the cached `/home` summary has today's status (offline).
  final String? fallbackSubtitle;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final today = ref.watch(todayAttendanceProvider);
    final theme = Theme.of(context);
    final (IconData icon, String title, String subtitle) = today.when(
      loading: () => (Icons.hourglass_empty, 'Today', 'Loading'),
      error: (_, __) => fallbackSubtitle != null
          ? (Icons.fingerprint, 'Today', fallbackSubtitle!)
          : (Icons.cloud_off_outlined, 'Today', 'Could not load. Tap to retry.'),
      data: (t) {
        final r = t.record;
        final site = t.onDuty;
        return (
          r != null && r.checkedIn ? Icons.check_circle_outline : Icons.fingerprint,
          r != null && r.checkedOut ? 'Day complete' : (r != null && r.checkedIn ? 'Checked in' : 'Check in'),
          site == null ? todayStatusText(r) : '${todayStatusText(r)}. On duty at ${site.name}',
        );
      },
    );
    return Card(
      child: Semantics(
        button: true,
        label: '$title. $subtitle',
        excludeSemantics: true,
        child: InkWell(
          borderRadius: BorderRadius.circular(12),
          onTap: today.hasError && fallbackSubtitle == null
              ? () => ref.invalidate(todayAttendanceProvider)
              : onTap,
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Row(
              children: [
                Icon(icon, size: 32, color: theme.colorScheme.primary),
                const SizedBox(width: 16),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(title, style: theme.textTheme.titleMedium),
                      Text(subtitle, style: theme.textTheme.bodyMedium),
                    ],
                  ),
                ),
                if (onTap != null) const Icon(Icons.chevron_right),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
