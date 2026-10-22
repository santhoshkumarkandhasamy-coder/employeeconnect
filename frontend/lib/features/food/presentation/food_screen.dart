import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../attendance/presentation/widgets/summary_card.dart' show StaleBanner;
import '../domain/food_models.dart';
import 'food_providers.dart';

/// Food tab: one "Need food?" Yes/No switch per date. Cut-off times come from
/// the server (`cutoff_at`, `can_change`); the countdown uses the server clock
/// offset so a wrong phone clock does not unlock or lock days by mistake.
class FoodScreen extends ConsumerStatefulWidget {
  const FoodScreen({super.key});

  @override
  ConsumerState<FoodScreen> createState() => _FoodScreenState();
}

class _FoodScreenState extends ConsumerState<FoodScreen> {
  Timer? _tick;

  @override
  void initState() {
    super.initState();
    // Refresh countdown labels and lock rows as cut-offs pass.
    _tick = Timer.periodic(const Duration(seconds: 30), (_) {
      if (mounted) setState(() {});
    });
  }

  @override
  void dispose() {
    _tick?.cancel();
    super.dispose();
  }

  void _snack(String m) => ScaffoldMessenger.of(context)
    ..hideCurrentSnackBar()
    ..showSnackBar(SnackBar(content: Text(m)));

  Future<void> _toggle(String date, bool v) async {
    final err = await ref.read(foodProvider.notifier).toggle(date, needFood: v);
    if (err != null && mounted) _snack(err);
  }

  Future<void> _bulk(FoodState s, DateTime now) async {
    final targets = weekTargets(s.days, now);
    if (targets.isEmpty) {
      _snack('No days left to change this week.');
      return;
    }
    final choice = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Set for the week'),
        content: Text('Need food on all ${targets.length} open day(s) this week?'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
          OutlinedButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('No')),
          FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Yes')),
        ],
      ),
    );
    if (choice == null || !mounted) return;
    final err = await ref
        .read(foodProvider.notifier)
        .setMany([for (final d in targets) d.date], needFood: choice);
    if (!mounted) return;
    _snack(err ?? 'Saved for ${targets.length} day(s).');
  }

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(foodProvider);
    final clock = ref.watch(foodClockProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Food'),
      ),
      body: async.when(
        skipLoadingOnReload: true,
        loading: () => const LoadingView(label: 'Loading food choices'),
        error: (e, _) => ErrorView(
          message: e is ApiException ? foodErrorMessage(e) : 'Could not load food choices.',
          onRetry: () => ref.read(foodProvider.notifier).reload(),
        ),
        data: (s) {
          final now = serverNow(clock(), s.offset);
          return RefreshIndicator(
            onRefresh: () => ref.read(foodProvider.notifier).reload(),
            child: s.days.isEmpty
                ? ListView(children: const [
                    SizedBox(height: 120),
                    EmptyView(
                      message: 'No days are open for food choices right now.',
                      icon: Icons.restaurant_outlined,
                    ),
                  ],)
                : ListView(
                    padding: const EdgeInsets.fromLTRB(12, 8, 12, 24),
                    children: [
                      if (s.stale) const StaleBanner(),
                      Padding(
                        padding: const EdgeInsets.fromLTRB(4, 4, 4, 8),
                        child: Row(
                          children: [
                            Expanded(
                              child: Text(
                                'Need food?',
                                style: Theme.of(context).textTheme.titleMedium,
                              ),
                            ),
                            TextButton.icon(
                              onPressed: s.stale || s.pending.isNotEmpty ? null : () => _bulk(s, now),
                              icon: const Icon(Icons.date_range),
                              label: const Text('Set for the week'),
                            ),
                          ],
                        ),
                      ),
                      for (final d in s.days)
                        FoodDayTile(
                          day: d,
                          now: now,
                          saving: s.pending.contains(d.date),
                          offline: s.stale,
                          onChanged: (v) => _toggle(d.date, v),
                        ),
                    ],
                  ),
          );
        },
      ),
    );
  }
}

/// One date row: weekday/date, cut-off text and a Yes/No switch.
class FoodDayTile extends StatelessWidget {
  const FoodDayTile({
    required this.day,
    required this.now,
    required this.onChanged,
    super.key,
    this.saving = false,
    this.offline = false,
  });

  final FoodDay day;
  final DateTime now;
  final bool saving;

  /// Showing cached data: changes need the network, so they are disabled.
  final bool offline;
  final ValueChanged<bool> onChanged;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final status = cutoffStatus(day, now);
    final date = day.day ?? now;
    final isToday = day.date == ymd(now);
    final canEdit = status.editable && !saving && !offline;
    final answer = day.needFood == true
        ? 'Yes'
        : day.needFood == false
            ? 'No'
            : 'Not answered';
    final Color labelColor = switch (status.phase) {
      CutoffPhase.closingSoon => cs.error,
      CutoffPhase.locked || CutoffPhase.holiday => cs.outline,
      CutoffPhase.open => cs.onSurfaceVariant,
    };
    final icon = switch (status.phase) {
      CutoffPhase.locked => Icons.lock_outline,
      CutoffPhase.holiday => Icons.beach_access_outlined,
      CutoffPhase.closingSoon => Icons.timer_outlined,
      CutoffPhase.open => Icons.schedule,
    };

    return Semantics(
      container: true,
      label: 'Need food on ${formatDayLong(date)}${isToday ? ', today' : ''}: $answer. ${status.label}',
      child: Card(
        margin: const EdgeInsets.symmetric(vertical: 4),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          child: Row(
            children: [
              Expanded(
                child: ExcludeSemantics(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        isToday ? 'Today, ${formatDayShort(date)}' : formatDayShort(date),
                        style: Theme.of(context).textTheme.titleSmall,
                      ),
                      const SizedBox(height: 2),
                      Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Icon(icon, size: 16, color: labelColor),
                          const SizedBox(width: 4),
                          Flexible(
                            child: Text(
                              status.label,
                              style: Theme.of(context)
                                  .textTheme
                                  .bodySmall
                                  ?.copyWith(color: labelColor),
                            ),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
              if (status.phase != CutoffPhase.holiday) ...[
                const SizedBox(width: 8),
                ExcludeSemantics(
                  child: SizedBox(
                    width: 84,
                    child: Text(
                      answer,
                      textAlign: TextAlign.end,
                      style: Theme.of(context).textTheme.labelLarge,
                    ),
                  ),
                ),
                Switch(
                  key: ValueKey('food-switch-${day.date}'),
                  value: day.needFood ?? false,
                  onChanged: canEdit ? onChanged : null,
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
