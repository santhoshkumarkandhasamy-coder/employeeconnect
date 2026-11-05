import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../domain/payroll_models.dart' show PayrollRunItem, periodTitle;
import '../domain/payroll_money.dart';
import '../domain/phase5a_models.dart';
import 'pay_items_widgets.dart';
import 'payment_batch_screen.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// Shared pieces of the payment screens (contract 19.5). Money, accounts and
/// IFSC are shown exactly as the server sends them (masked) and never logged.

PayTone batchTone(String status) => switch (status) {
      'approved' || 'closed' => PayTone.good,
      'submitted' || 'exported' => PayTone.warn,
      'cancelled' => PayTone.bad,
      _ => PayTone.neutral,
    };

PayTone lineTone(String status) => switch (status) {
      'paid' => PayTone.good,
      'failed' => PayTone.bad,
      'exported' => PayTone.warn,
      _ => PayTone.neutral,
    };

class BatchStatusChip extends StatelessWidget {
  const BatchStatusChip(this.status, {super.key});
  final String status;

  @override
  Widget build(BuildContext context) => PayStatusChip(batchStatusLabel(status), tone: batchTone(status));
}

String valueDateLabel(String? ymdText) {
  final d = parseYmd(ymdText);
  return d == null ? (ymdText ?? '-') : formatDayLong(d);
}

/// One row of the batch list.
class PaymentBatchTile extends StatelessWidget {
  const PaymentBatchTile({required this.batch, required this.onTap, super.key, this.extra});
  final PaymentBatch batch;
  final VoidCallback onTap;
  final Widget? extra;

  @override
  Widget build(BuildContext context) {
    final b = batch;
    final theme = Theme.of(context);
    return Card(
      key: ValueKey('batch-${b.id}'),
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: InkWell(
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(14, 10, 14, 10),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(child: Text(b.reference, style: theme.textTheme.titleSmall)),
                  BatchStatusChip(b.status),
                ],
              ),
              Text(
                '${periodTitle(b.runPeriod)}  -  ${b.counts.lines} lines'
                '${b.counts.excluded > 0 ? ', ${b.counts.excluded} left out' : ''}',
                style: theme.textTheme.bodySmall,
              ),
              Text('Total ${formatRupees(b.total)}', style: theme.textTheme.titleMedium),
              Text(
                'Value date ${valueDateLabel(b.valueDate)}'
                '${b.debitAccount == null ? '' : '  -  from ${b.debitAccount!.label}'}',
                style: theme.textTheme.bodySmall,
              ),
              if (extra != null) extra!,
            ],
          ),
        ),
      ),
    );
  }
}

void invalidatePaymentState(WidgetRef ref, {String? id}) {
  ref
    ..invalidate(paymentBatchesProvider)
    ..invalidate(approvalQueueProvider)
    ..invalidate(payrollDashboardProvider);
  if (id != null) ref.invalidate(paymentBatchProvider(id));
}

Future<void> openPaymentBatch(BuildContext context, String id) => Navigator.of(context).push<void>(
      MaterialPageRoute<void>(builder: (_) => PaymentBatchScreen(batchId: id)),
    );

/// Batches with a status filter. [runId] limits them to one run.
class PaymentBatchesList extends ConsumerStatefulWidget {
  const PaymentBatchesList({super.key, this.runId});
  final String? runId;

  @override
  ConsumerState<PaymentBatchesList> createState() => _PaymentBatchesListState();
}

class _PaymentBatchesListState extends ConsumerState<PaymentBatchesList> {
  final _scroll = ScrollController();
  String? _status;

  PaymentBatchQuery get _query => PaymentBatchQuery(status: _status, runId: widget.runId);

  @override
  void initState() {
    super.initState();
    _scroll.addListener(() {
      if (_scroll.hasClients && _scroll.position.extentAfter < 300) {
        unawaited(ref.read(paymentBatchesProvider(_query).notifier).loadMore());
      }
    });
  }

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(paymentBatchesProvider(_query));
    return Column(
      children: [
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          padding: const EdgeInsets.symmetric(horizontal: 12),
          child: Row(
            children: [
              for (final f in <(String?, String)>[(null, 'All'), for (final s in batchStatuses) (s, batchStatusLabel(s))])
                Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: ChoiceChip(
                    key: ValueKey('batch-filter-${f.$1 ?? 'all'}'),
                    label: Text(f.$2),
                    selected: _status == f.$1,
                    onSelected: (_) => setState(() => _status = f.$1),
                  ),
                ),
            ],
          ),
        ),
        Expanded(
          child: async.when(
            skipLoadingOnReload: true,
            loading: () => const LoadingView(label: 'Loading payment batches'),
            error: (e, _) => ErrorView(message: p5Msg(e), onRetry: () => ref.invalidate(paymentBatchesProvider(_query))),
            data: (s) => RefreshIndicator(
              onRefresh: () async => ref.invalidate(paymentBatchesProvider(_query)),
              child: s.items.isEmpty
                  ? ListView(
                      physics: const AlwaysScrollableScrollPhysics(),
                      children: const [
                        SizedBox(height: 80),
                        EmptyView(
                          icon: Icons.account_balance_outlined,
                          message: 'No payment batches yet. Create one from a published run.',
                        ),
                      ],
                    )
                  : ListView(
                      controller: _scroll,
                      physics: const AlwaysScrollableScrollPhysics(),
                      padding: const EdgeInsets.fromLTRB(12, 8, 12, 96),
                      children: [
                        for (final b in s.items)
                          PaymentBatchTile(
                            batch: b,
                            onTap: () async {
                              await openPaymentBatch(context, b.id);
                              ref.invalidate(paymentBatchesProvider(_query));
                            },
                          ),
                        if (s.loadingMore)
                          const Padding(
                            padding: EdgeInsets.all(16),
                            child: Center(child: CircularProgressIndicator()),
                          ),
                        if (s.loadMoreError != null)
                          TextButton(
                            onPressed: () => ref.read(paymentBatchesProvider(_query).notifier).loadMore(),
                            child: Text('${s.loadMoreError} Try again'),
                          ),
                      ],
                    ),
            ),
          ),
        ),
      ],
    );
  }
}

/// Published runs of a year with a chevron year selector; [onPick] gets the run.
class PublishedRunsPicker extends ConsumerStatefulWidget {
  const PublishedRunsPicker({required this.onPick, super.key, this.trailingLabel});
  final void Function(PayrollRunItem run) onPick;
  final String? trailingLabel;

  @override
  ConsumerState<PublishedRunsPicker> createState() => _PublishedRunsPickerState();
}

class _PublishedRunsPickerState extends ConsumerState<PublishedRunsPicker> {
  late int _year = ref.read(payrollClockProvider)().year;

  @override
  Widget build(BuildContext context) {
    final maxYear = ref.read(payrollClockProvider)().year;
    final async = ref.watch(payrollRunsProvider(_year));
    return Column(
      children: [
        YearSelector(year: _year, maxYear: maxYear, onChanged: (y) => setState(() => _year = y)),
        Expanded(
          child: async.when(
            skipLoadingOnReload: true,
            loading: () => const LoadingView(label: 'Loading runs'),
            error: (e, _) => ErrorView(message: payrollMsg(e), onRetry: () => ref.invalidate(payrollRunsProvider(_year))),
            data: (runs) {
              final published = [for (final r in runs) if (r.status == 'published') r];
              if (published.isEmpty) {
                return const EmptyView(icon: Icons.event_busy_outlined, message: 'No published payroll runs this year.');
              }
              return ListView(
                padding: const EdgeInsets.fromLTRB(12, 4, 12, 48),
                children: [
                  for (final r in published)
                    Card(
                      key: ValueKey('picker-run-${r.id}'),
                      child: ListTile(
                        title: Text(periodTitle(r.period)),
                        subtitle: Text('${r.employees} employees'),
                        trailing: const Icon(Icons.chevron_right),
                        onTap: () => widget.onPick(r),
                      ),
                    ),
                ],
              );
            },
          ),
        ),
      ],
    );
  }
}
