import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/month_selector.dart';
import '../../../core/widgets/state_views.dart';
import '../../on_duty/presentation/on_duty_data_providers.dart' show dayVisitsProvider;
import '../../on_duty/presentation/on_duty_panel.dart' show VisitTimeline;
import '../domain/attendance_models.dart';
import 'attendance_providers.dart';
import 'widgets/month_grid.dart';
import 'widgets/summary_card.dart';

class CalendarTab extends ConsumerWidget {
  const CalendarTab({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final month = ref.watch(attendanceMonthProvider);
    final cal = ref.watch(calendarProvider(month));
    final sum = ref.watch(attendanceSummaryProvider(month));

    Future<void> refresh() async {
      ref
        ..invalidate(calendarProvider(month))
        ..invalidate(attendanceSummaryProvider(month));
      await ref.read(calendarProvider(month).future).then((_) {}, onError: (Object _) {});
    }

    return Column(
      children: [
        MonthSelector(
          month: month,
          onChanged: (m) => ref.read(attendanceMonthProvider.notifier).state = m,
        ),
        Expanded(
          child: cal.when(
            loading: () => const LoadingView(label: 'Loading calendar'),
            error: (e, _) => ErrorView(
              message: e is ApiException ? e.message : 'Could not load the calendar.',
              onRetry: refresh,
            ),
            data: (c) => RefreshIndicator(
              onRefresh: refresh,
              child: ListView(
                padding: const EdgeInsets.all(16),
                children: [
                  if (c.stale || (sum.valueOrNull?.stale ?? false)) const StaleBanner(),
                  if (sum.valueOrNull != null) AttendanceSummaryCard(summary: sum.value!.value),
                  if (sum.hasError && !sum.hasValue)
                    const Padding(
                      padding: EdgeInsets.only(bottom: 8),
                      child: Text('Summary unavailable.'),
                    ),
                  const SizedBox(height: 8),
                  MonthGrid(
                    month: month,
                    days: c.value.days,
                    onDayTap: (d) => _showDay(context, d),
                  ),
                  const SizedBox(height: 16),
                  const StatusLegend(),
                ],
              ),
            ),
          ),
        ),
      ],
    );
  }

  void _showDay(BuildContext context, CalendarDay d) {
    showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      isScrollControlled: true,
      builder: (ctx) => SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.fromLTRB(24, 0, 24, 24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(formatDayLong(d.date), style: Theme.of(ctx).textTheme.titleMedium),
              const SizedBox(height: 8),
              Row(
                children: [
                  Container(
                    width: 14,
                    height: 14,
                    decoration: BoxDecoration(
                      color: statusColor(d.status, Theme.of(ctx).brightness),
                      shape: BoxShape.circle,
                      border: Border.all(color: Theme.of(ctx).colorScheme.outline),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Text(d.status.label),
                ],
              ),
              if (d.note != null) ...[
                const SizedBox(height: 4),
                Text(d.note!),
              ],
              if (d.onDuty) ...[
                const SizedBox(height: 8),
                const Chip(label: Text('On Duty'), avatar: Icon(Icons.place, size: 16)),
                _DayVisits(date: ymd(d.date)),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

/// Visits of one day, loaded when the day detail opens.
class _DayVisits extends ConsumerWidget {
  const _DayVisits({required this.date});
  final String date;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(dayVisitsProvider(date));
    return async.when(
      loading: () => const Padding(padding: EdgeInsets.all(12), child: LinearProgressIndicator()),
      error: (e, _) => Padding(
        padding: const EdgeInsets.only(top: 8),
        child: Row(
          children: [
            Expanded(
              child: Text(e is ApiException ? e.message : 'Could not load the visits.'),
            ),
            TextButton(
              onPressed: () => ref.invalidate(dayVisitsProvider(date)),
              child: const Text('Try again'),
            ),
          ],
        ),
      ),
      data: (visits) => visits.isEmpty
          ? const Padding(padding: EdgeInsets.only(top: 8), child: Text('No visits to show.'))
          : VisitTimeline(visits: visits, title: 'Visits'),
    );
  }
}
