import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../attendance/presentation/widgets/summary_card.dart' show StaleBanner;
import '../domain/leave_models.dart';
import 'leave_providers.dart';

class BalanceTab extends ConsumerWidget {
  const BalanceTab({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final bal = ref.watch(leaveBalanceProvider);

    Future<void> refresh() async {
      ref.invalidate(leaveBalanceProvider);
      await ref.read(leaveBalanceProvider.future).then((_) {}, onError: (Object _) {});
    }

    return bal.when(
      loading: () => const LoadingView(label: 'Loading leave balance'),
      error: (e, _) => ErrorView(
        message: e is ApiException ? e.message : 'Could not load your balance.',
        onRetry: refresh,
      ),
      data: (c) {
        final d = c.value;
        return RefreshIndicator(
          onRefresh: refresh,
          child: ListView(
            padding: const EdgeInsets.all(16),
            children: [
              if (c.stale) const StaleBanner(),
              Text('Leave balance ${d.year}', style: Theme.of(context).textTheme.titleMedium),
              const SizedBox(height: 8),
              if (d.leave.isEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 24),
                  child: EmptyView(message: 'No leave types are set up for you yet.'),
                )
              else
                for (final b in d.leave) LeaveBalanceCard(balance: b),
              const SizedBox(height: 8),
              PermissionBalanceCard(balance: d.permission),
            ],
          ),
        );
      },
    );
  }
}

class LeaveBalanceCard extends StatelessWidget {
  const LeaveBalanceCard({required this.balance, super.key});
  final LeaveBalanceItem balance;

  @override
  Widget build(BuildContext context) {
    final b = balance;
    final theme = Theme.of(context);
    // Unpaid/LOP types have an effectively unlimited quota (365).
    final unlimited = b.quota >= 365;
    final label = '${b.name}: ${daysLabel(b.remaining)} remaining'
        '${unlimited ? '' : ' of ${formatDays(b.quota)}'}, '
        '${formatDays(b.used)} used, ${formatDays(b.pending)} pending';
    final frac = b.quota <= 0 ? 0.0 : ((b.used + b.pending) / b.quota).clamp(0.0, 1.0);
    return Card(
      child: Semantics(
        label: label,
        excludeSemantics: true,
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(child: Text(b.name, style: theme.textTheme.titleSmall)),
                  Text(
                    unlimited ? 'No limit' : '${formatDays(b.remaining)} left',
                    style: theme.textTheme.titleMedium,
                  ),
                ],
              ),
              if (!unlimited) ...[
                const SizedBox(height: 8),
                LinearProgressIndicator(value: frac),
              ],
              const SizedBox(height: 8),
              Wrap(
                spacing: 16,
                children: [
                  if (!unlimited) Text('Quota ${formatDays(b.quota)}'),
                  Text('Used ${formatDays(b.used)}'),
                  Text('Pending ${formatDays(b.pending)}'),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class PermissionBalanceCard extends StatelessWidget {
  const PermissionBalanceCard({required this.balance, super.key});
  final PermissionBalance balance;

  @override
  Widget build(BuildContext context) {
    final p = balance;
    final theme = Theme.of(context);
    final title = p.month.isEmpty ? 'Permissions this month' : 'Permissions in ${monthTitle(p.month)}';
    return Card(
      child: Semantics(
        label: '$title: ${p.remainingCount} of ${p.limitCount} requests and '
            '${formatDuration(p.remainingMinutes)} of ${formatDuration(p.limitMinutes)} remaining, '
            '${p.pendingCount} requests and ${formatDuration(p.pendingMinutes)} pending',
        excludeSemantics: true,
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(title, style: theme.textTheme.titleSmall),
              const SizedBox(height: 8),
              Text('${p.remainingCount} of ${p.limitCount} requests left', style: theme.textTheme.titleMedium),
              Text(
                '${formatDuration(p.remainingMinutes)} of ${formatDuration(p.limitMinutes)} left',
                style: theme.textTheme.titleMedium,
              ),
              const SizedBox(height: 8),
              Text(
                'Includes pending: ${p.pendingCount} requests, ${formatDuration(p.pendingMinutes)}',
                style: theme.textTheme.bodySmall,
              ),
            ],
          ),
        ),
      ),
    );
  }
}
