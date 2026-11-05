import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart' show adminPermissionsProvider, confirmAction;
import '../data/payroll_repository.dart';
import '../domain/payroll_models.dart' show canManagePayroll, periodTitle;
import '../domain/phase5a_models.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';
import 'publish_request_widgets.dart' show stampLabel;

/// Who has opened their payslip (contract 19.3): counts, a filter and a reminder
/// for the ones who have not (at most 3 per payslip, one per 24 hours). The
/// push carries no amounts.
class DeliveryReportScreen extends ConsumerStatefulWidget {
  const DeliveryReportScreen({required this.runId, super.key, this.period});
  final String runId;
  final String? period;

  @override
  ConsumerState<DeliveryReportScreen> createState() => _DeliveryReportScreenState();
}

class _DeliveryReportScreenState extends ConsumerState<DeliveryReportScreen> {
  String? _state;
  int _page = 1;
  bool _busy = false;

  DeliveryKey get _key => (DeliveryQuery(widget.runId, state: _state), _page);

  Future<void> _remind({List<String>? userIds}) async {
    if (_busy) return;
    if (userIds == null) {
      final ok = await confirmAction(
        context,
        title: 'Remind everyone who has not opened it?',
        message: 'A notification is sent to every employee with a registered phone who has not opened the payslip. '
            'Each payslip is reminded at most $deliveryMaxReminders times and once in 24 hours.',
        confirmLabel: 'Send reminders',
      );
      if (!ok || !mounted) return;
    }
    setState(() => _busy = true);
    final repo = ref.read(payrollRepositoryProvider);
    try {
      final r = await repo.remindDelivery(widget.runId, userIds: userIds);
      ref.invalidate(deliveryProvider);
      if (mounted) payrollSnack(context, r.summary());
    } on ApiException catch (e) {
      if (mounted) payrollSnack(context, phase5ErrorMessage(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(deliveryProvider(_key));
    final manage = canManagePayroll(ref.watch(adminPermissionsProvider));
    final theme = Theme.of(context);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(
          title: Text(widget.period == null ? 'Payslip delivery' : 'Delivery: ${periodTitle(widget.period!)}'),
        ),
        body: async.when(
          skipLoadingOnReload: true,
          loading: () => const LoadingView(label: 'Loading delivery report'),
          error: (e, _) => ErrorView(message: p5Msg(e), onRetry: () => ref.invalidate(deliveryProvider(_key))),
          data: (d) {
            final c = d.counts;
            final items = d.items;
            return RefreshIndicator(
              onRefresh: () async => ref.invalidate(deliveryProvider(_key)),
              child: ListView(
                physics: const AlwaysScrollableScrollPhysics(),
                padding: const EdgeInsets.fromLTRB(12, 8, 12, 48),
                children: [
                  Card(
                    child: Padding(
                      padding: const EdgeInsets.all(14),
                      child: Wrap(
                        key: const ValueKey('delivery-counts'),
                        spacing: 16,
                        runSpacing: 4,
                        children: [
                          Text('Published ${c.published}'),
                          Text('Notified ${c.pushed}'),
                          Text('Opened ${c.viewed}'),
                          Text('Downloaded ${c.downloaded}'),
                          Text('No phone ${c.noDevice}'),
                        ],
                      ),
                    ),
                  ),
                  if (manage)
                    Padding(
                      padding: const EdgeInsets.symmetric(vertical: 6),
                      child: Align(
                        alignment: Alignment.centerLeft,
                        child: FilledButton.icon(
                          key: const ValueKey('remind-all'),
                          onPressed: _busy ? null : () => _remind(),
                          icon: const Icon(Icons.notifications_active_outlined, size: 18),
                          label: const Text('Remind everyone who has not opened it'),
                        ),
                      ),
                    ),
                  SingleChildScrollView(
                    scrollDirection: Axis.horizontal,
                    child: Row(
                      children: [
                        for (final f in const <(String?, String)>[
                          (null, 'All'),
                          ('not_viewed', 'Not opened'),
                          ('viewed', 'Opened'),
                          ('no_device', 'No phone'),
                        ])
                          Padding(
                            padding: const EdgeInsets.only(right: 8),
                            child: ChoiceChip(
                              key: ValueKey('delivery-filter-${f.$1 ?? 'all'}'),
                              label: Text(f.$2),
                              selected: _state == f.$1,
                              onSelected: (_) => setState(() {
                                _state = f.$1;
                                _page = 1;
                              }),
                            ),
                          ),
                      ],
                    ),
                  ),
                  if (items.items.isEmpty)
                    const EmptyView(icon: Icons.mark_email_read_outlined, message: 'Nobody matches this filter.'),
                  for (final i in items.items)
                    Card(
                      key: ValueKey('delivery-${i.payslipId}'),
                      margin: const EdgeInsets.symmetric(vertical: 4),
                      child: Padding(
                        padding: const EdgeInsets.fromLTRB(14, 8, 6, 8),
                        child: Row(
                          children: [
                            Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text(i.user.display, style: theme.textTheme.titleSmall),
                                  Text(
                                    [
                                      if (!i.hasDevice) 'No registered phone',
                                      if (i.pushedAt != null) 'Notified ${stampLabel(i.pushedAt)}',
                                      if (i.firstViewedAt != null)
                                        'Opened ${stampLabel(i.firstViewedAt)}'
                                      else
                                        'Not opened',
                                      if (i.firstDownloadedAt != null) 'Downloaded',
                                      if (i.reminders > 0) 'Reminded ${i.reminders} of $deliveryMaxReminders',
                                    ].join('  -  '),
                                    style: theme.textTheme.bodySmall,
                                  ),
                                ],
                              ),
                            ),
                            if (manage && i.canRemind)
                              IconButton(
                                key: ValueKey('remind-${i.user.id}'),
                                tooltip: 'Remind',
                                icon: const Icon(Icons.notifications_active_outlined),
                                onPressed: _busy ? null : () => _remind(userIds: [i.user.id]),
                              ),
                          ],
                        ),
                      ),
                    ),
                  if (_page > 1 || items.hasMore)
                    Row(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        TextButton(
                          key: const ValueKey('delivery-prev'),
                          onPressed: _page > 1 ? () => setState(() => _page--) : null,
                          child: const Text('Previous'),
                        ),
                        Text('Page $_page'),
                        TextButton(
                          key: const ValueKey('delivery-next'),
                          onPressed: items.hasMore ? () => setState(() => _page++) : null,
                          child: const Text('Next'),
                        ),
                      ],
                    ),
                ],
              ),
            );
          },
        ),
      ),
    );
  }
}
