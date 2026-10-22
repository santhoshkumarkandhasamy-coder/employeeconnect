import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/cache/json_cache.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/month_selector.dart';
import '../../../core/widgets/state_views.dart';
import '../../attendance/presentation/widgets/summary_card.dart' show StaleBanner;
import '../data/late_repository.dart';
import '../domain/late_models.dart';

final lateMonthProvider = StateProvider.autoDispose<String>((ref) => monthKey(DateTime.now()));

final lateListProvider = FutureProvider.autoDispose.family<Cached<List<LateRecord>>, String>(
  (ref, month) => ref.watch(lateRepositoryProvider).list(month),
);

final lateSummaryProvider = FutureProvider.autoDispose.family<Cached<LateSummary>, String>(
  (ref, month) => ref.watch(lateRepositoryProvider).summary(month),
);

class LateScreen extends ConsumerWidget {
  const LateScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final month = ref.watch(lateMonthProvider);
    final list = ref.watch(lateListProvider(month));
    final sum = ref.watch(lateSummaryProvider(month));

    Future<void> refresh() async {
      ref
        ..invalidate(lateListProvider(month))
        ..invalidate(lateSummaryProvider(month));
      await ref.read(lateListProvider(month).future).then((_) {}, onError: (Object _) {});
    }

    return Scaffold(
      appBar: AppBar(title: const Text('Late records')),
      body: Column(
        children: [
          MonthSelector(
            month: month,
            onChanged: (m) => ref.read(lateMonthProvider.notifier).state = m,
          ),
          Expanded(
            child: list.when(
              loading: () => const LoadingView(label: 'Loading late records'),
              error: (e, _) => ErrorView(
                message: e is ApiException ? e.message : 'Could not load late records.',
                onRetry: refresh,
              ),
              data: (l) => RefreshIndicator(
                onRefresh: refresh,
                child: ListView(
                  padding: const EdgeInsets.all(16),
                  children: [
                    if (l.stale) const StaleBanner(),
                    LateSummaryCard(
                      summary: sum.valueOrNull?.value ?? LateSummary(month: month),
                      loading: sum.isLoading,
                    ),
                    const SizedBox(height: 8),
                    if (l.value.isEmpty)
                      Padding(
                        padding: const EdgeInsets.only(top: 32),
                        child: EmptyView(
                          icon: Icons.thumb_up_alt_outlined,
                          message: 'No late arrivals in ${monthTitle(month)}.',
                        ),
                      )
                    else
                      for (final r in l.value) LateTile(record: r),
                  ],
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class LateSummaryCard extends StatelessWidget {
  const LateSummaryCard({required this.summary, super.key, this.loading = false});
  final LateSummary summary;
  final bool loading;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    Widget stat(String label, String value) => Expanded(
          child: Semantics(
            label: '$label: $value',
            excludeSemantics: true,
            child: Column(
              children: [
                Text(value, style: theme.textTheme.titleLarge),
                Text(label, style: theme.textTheme.labelSmall),
              ],
            ),
          ),
        );
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: loading
            ? const SizedBox(height: 48, child: LoadingView(label: 'Loading summary'))
            : Row(
                children: [
                  stat('Late days', '${summary.lateCount}'),
                  stat('Total', formatDuration(summary.totalMinutesLate)),
                  stat('Average', formatAverageMinutes(summary.averageMinutesLate)),
                ],
              ),
      ),
    );
  }
}

class LateTile extends StatelessWidget {
  const LateTile({required this.record, super.key});
  final LateRecord record;

  @override
  Widget build(BuildContext context) {
    final r = record;
    final date = parseYmd(r.workDate);
    final title = date == null ? r.workDate : formatDayLong(date);
    final sub = [
      if (r.checkInAt != null) 'Checked in ${formatTime(r.checkInAt!)}',
      if (r.shiftStart != null) 'shift ${r.shiftStart}',
    ].join(', ');
    return Card(
      child: Semantics(
        label: '$title, ${r.minutesLate} minutes late${sub.isEmpty ? '' : ', $sub'}',
        excludeSemantics: true,
        child: ListTile(
          leading: const Icon(Icons.schedule),
          title: Text(title),
          subtitle: sub.isEmpty ? null : Text(sub),
          trailing: Text(
            '${r.minutesLate} min late',
            style: Theme.of(context).textTheme.labelLarge,
          ),
        ),
      ),
    );
  }
}
