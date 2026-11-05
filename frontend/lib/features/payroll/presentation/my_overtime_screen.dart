import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart' show NoPermissionScreen, adminPermissionsProvider;
import '../domain/claims_models.dart';
import '../domain/payroll_models.dart';
import 'pay_items_widgets.dart';
import 'payroll_providers.dart';

/// "My overtime" (read-only): the day rows of one month with minutes and
/// status, no money. Shows nothing when overtime is switched off or the
/// employee is not eligible. Screenshot-protected; in memory only.
class MyOvertimeScreen extends ConsumerStatefulWidget {
  const MyOvertimeScreen({super.key});

  @override
  ConsumerState<MyOvertimeScreen> createState() => _MyOvertimeScreenState();
}

class _MyOvertimeScreenState extends ConsumerState<MyOvertimeScreen> {
  late final String _thisMonth = monthKey(ref.read(payrollClockProvider)());
  late String _period = _thisMonth;

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!canViewOwnPayslips(perms)) return const NoPermissionScreen(title: 'My overtime');
    final async = ref.watch(myOvertimeProvider(_period));
    final theme = Theme.of(context);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('My overtime')),
        body: Column(
          children: [
            PayMonthBar(
              keyPrefix: 'my-ot',
              period: _period,
              max: _thisMonth,
              onChanged: (p) => setState(() => _period = p),
            ),
            Expanded(
              child: payAsync<MyOvertime>(
                context,
                async,
                loadingLabel: 'Loading overtime',
                onRetry: () => ref.invalidate(myOvertimeProvider(_period)),
                emptyMessage: 'Overtime is not available for you.',
                emptyIcon: Icons.more_time_outlined,
                isEmpty: (o) => !o.visible,
                builder: (o) => RefreshIndicator(
                  onRefresh: () async {
                    ref.invalidate(myOvertimeProvider(_period));
                    await ref.read(myOvertimeProvider(_period).future).then((_) {}, onError: (Object _) {});
                  },
                  child: o.items.isEmpty
                      ? ListView(
                          physics: const AlwaysScrollableScrollPhysics(),
                          children: [
                            const SizedBox(height: 80),
                            EmptyView(
                              icon: Icons.more_time_outlined,
                              message: 'No overtime for ${monthTitle(_period)}.',
                            ),
                          ],
                        )
                      : ListView(
                          physics: const AlwaysScrollableScrollPhysics(),
                          padding: const EdgeInsets.fromLTRB(12, 4, 12, 24),
                          children: [
                            Padding(
                              padding: const EdgeInsets.only(bottom: 8),
                              child: Text(
                                'Approved this month: ${minutesText(o.totalApprovedMinutes)}',
                                style: theme.textTheme.titleSmall,
                              ),
                            ),
                            for (final d in o.items)
                              Card(
                                key: ValueKey('my-ot-${d.workDate}'),
                                child: ListTile(
                                  title: Text(_day(d.workDate)),
                                  subtitle: Text(
                                    'Worked ${minutesText(d.computedMinutes)} extra'
                                    '${d.approvedMinutes == null ? '' : ', approved ${minutesText(d.approvedMinutes!)}'}',
                                  ),
                                  trailing: PayStatusChip(
                                    overtimeStatusLabel(d.status),
                                    tone: switch (d.status) {
                                      'approved' || 'paid' => PayTone.good,
                                      'computed' || 'processing' => PayTone.warn,
                                      'rejected' => PayTone.bad,
                                      _ => PayTone.neutral,
                                    },
                                  ),
                                ),
                              ),
                          ],
                        ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  String _day(String ymdText) {
    final d = parseYmd(ymdText);
    return d == null ? ymdText : formatDayLong(d);
  }
}
