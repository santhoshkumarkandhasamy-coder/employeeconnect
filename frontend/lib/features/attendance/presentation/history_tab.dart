import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../domain/attendance_models.dart';
import 'attendance_providers.dart';
import 'widgets/month_grid.dart';
import 'widgets/summary_card.dart';

class HistoryTab extends ConsumerWidget {
  const HistoryTab({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final hist = ref.watch(historyProvider);
    return hist.when(
      loading: () => const LoadingView(label: 'Loading history'),
      error: (e, _) => ErrorView(
        message: e is ApiException ? e.message : 'Could not load your history.',
        onRetry: () => ref.invalidate(historyProvider),
      ),
      data: (s) {
        if (s.items.isEmpty) {
          return const EmptyView(
            icon: Icons.history,
            message: 'No attendance records yet.',
          );
        }
        return RefreshIndicator(
          onRefresh: () async {
            ref.invalidate(historyProvider);
            await ref.read(historyProvider.future).then((_) {}, onError: (Object _) {});
          },
          child: ListView.builder(
            padding: const EdgeInsets.all(16),
            itemCount: s.items.length + 1 + (s.stale ? 1 : 0),
            itemBuilder: (context, i) {
              if (s.stale) {
                if (i == 0) return const StaleBanner();
                i -= 1;
              }
              if (i == s.items.length) return _Footer(state: s);
              return HistoryTile(record: s.items[i]);
            },
          ),
        );
      },
    );
  }
}

class _Footer extends ConsumerWidget {
  const _Footer({required this.state});
  final HistoryState state;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (state.loadingMore) {
      return const Padding(padding: EdgeInsets.all(16), child: LoadingView());
    }
    if (state.loadMoreError != null) {
      return Padding(
        padding: const EdgeInsets.all(8),
        child: Column(
          children: [
            Text(state.loadMoreError!),
            TextButton(
              onPressed: () => ref.read(historyProvider.notifier).loadMore(),
              child: const Text('Try again'),
            ),
          ],
        ),
      );
    }
    if (!state.hasMore) return const SizedBox(height: 8);
    return Padding(
      padding: const EdgeInsets.all(8),
      child: OutlinedButton(
        onPressed: () => ref.read(historyProvider.notifier).loadMore(),
        child: const Text('Load more'),
      ),
    );
  }
}

class HistoryTile extends StatelessWidget {
  const HistoryTile({required this.record, super.key});
  final AttendanceRecord record;

  @override
  Widget build(BuildContext context) {
    final r = record;
    final date = parseYmd(r.workDate);
    final title = date == null ? r.workDate : formatDayLong(date);
    final inT = r.checkInAt == null ? '--:--' : formatTime(r.checkInAt!);
    final outT = r.checkOutAt == null ? '--:--' : formatTime(r.checkOutAt!);
    final worked = r.workedMinutes == null ? '' : ' - ${formatDuration(r.workedMinutes!)}';
    final extra = [
      if (r.status == DayStatus.late) '${r.minutesLate} min late',
      if (r.autoClosed) 'No check-out recorded',
    ].join(', ');
    final color = statusColor(r.status, Theme.of(context).brightness);
    return Card(
      child: Semantics(
        label: '$title, ${r.status.label}, in $inT, out $outT$worked${extra.isEmpty ? '' : ', $extra'}',
        excludeSemantics: true,
        child: ListTile(
          leading: Icon(Icons.circle, size: 14, color: color),
          title: Text(title),
          subtitle: Text('In $inT  Out $outT$worked${extra.isEmpty ? '' : '\n$extra'}'),
          isThreeLine: extra.isNotEmpty,
          trailing: Text(r.status.label),
        ),
      ),
    );
  }
}
