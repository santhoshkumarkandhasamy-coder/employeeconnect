import 'package:flutter/material.dart';

import '../../../../core/utils/dates.dart';
import '../../domain/attendance_models.dart';
import 'month_grid.dart';

class AttendanceSummaryCard extends StatelessWidget {
  const AttendanceSummaryCard({required this.summary, super.key});
  final AttendanceSummary summary;

  @override
  Widget build(BuildContext context) {
    final b = Theme.of(context).brightness;
    Widget stat(String label, int value, DayStatus s) => Expanded(
          child: Semantics(
            label: '$label: $value',
            excludeSemantics: true,
            child: Column(
              children: [
                Text(
                  '$value',
                  style: Theme.of(context).textTheme.titleLarge?.copyWith(color: statusColor(s, b)),
                ),
                Text(label, style: Theme.of(context).textTheme.labelSmall),
              ],
            ),
          ),
        );
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Month summary', style: Theme.of(context).textTheme.titleSmall),
            const SizedBox(height: 12),
            Row(
              children: [
                stat('Present', summary.present, DayStatus.present),
                stat('Late', summary.late, DayStatus.late),
                stat('Half day', summary.halfDay, DayStatus.halfDay),
                stat('Absent', summary.absent, DayStatus.absent),
                stat('Leave', summary.onLeave, DayStatus.onLeave),
              ],
            ),
            const SizedBox(height: 12),
            Text('Total worked: ${formatDuration(summary.totalWorkedMinutes)}'),
          ],
        ),
      ),
    );
  }
}

/// Banner shown when data came from the offline cache.
class StaleBanner extends StatelessWidget {
  const StaleBanner({super.key});

  @override
  Widget build(BuildContext context) => Semantics(
        liveRegion: true,
        child: Container(
          width: double.infinity,
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
          margin: const EdgeInsets.only(bottom: 8),
          decoration: BoxDecoration(
            color: Theme.of(context).colorScheme.secondaryContainer,
            borderRadius: BorderRadius.circular(8),
          ),
          child: const Row(
            children: [
              Icon(Icons.cloud_off_outlined, size: 16),
              SizedBox(width: 8),
              Expanded(child: Text('Offline. Showing saved data.')),
            ],
          ),
        ),
      );
}
