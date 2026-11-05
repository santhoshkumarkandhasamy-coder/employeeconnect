import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart'
    show AdminPagedBody, adminPermissionsProvider, adminSelfIdProvider, runWithReauth;
import '../data/payroll_repository.dart';
import '../domain/payroll_models.dart' show canManagePayroll, payrollErrorMessage;
import '../domain/phase4a_models.dart';
import 'increment_detail_screen.dart';
import 'increment_widgets.dart';
import 'pay_items_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

String batchTitle(IncrementBatch b) => b.kind == 'import'
    ? 'Structure import'
    : 'Bulk hike${b.percent == null ? '' : ' ${b.percent}%'}';

/// Compact status counts: "44 waiting, 2 cancelled".
String batchCountsText(IncrementBatch b) {
  final parts = <String>[];
  for (final s in const ['draft', 'submitted', 'approved', 'rejected', 'cancelled']) {
    final n = b.count(s);
    if (n > 0) parts.add('$n ${incrementStatusLabel(s).toLowerCase()}');
  }
  return parts.isEmpty ? 'No rows' : parts.join(', ');
}

/// Reloads everything that shows a batch.
void invalidateBatchState(WidgetRef ref, String batchId) {
  ref.invalidate(incrementBatchesProvider);
  ref.invalidate(incrementBatchProvider(batchId));
  ref.invalidate(incrementsProvider);
  ref.invalidate(approvalQueueProvider);
}

/// Approve a whole batch: optional comment, then the password. Shows what
/// failed (rows stay submitted). Returns true when the call went through.
Future<bool> approveBatchFlow(
  BuildContext context,
  WidgetRef ref,
  IncrementBatch batch, {
  Map<String, String> names = const {},
}) async {
  final comment = await showCommentDialog(
    context,
    title: 'Approve this batch?',
    label: 'Comment (optional)',
    confirmLabel: 'Continue',
  );
  if (comment == null || !context.mounted) return false;
  final repo = ref.read(payrollRepositoryProvider);
  BatchApproveResult? out;
  try {
    final done = await runWithReauth(
      context,
      reason: 'Approving ${batch.submittedCount} salary changes needs your password.',
      action: (pw) async {
        out = await repo.approveIncrementBatch(batch.id, currentPassword: pw, comment: comment);
      },
    );
    if (!done) return false;
  } on ApiException catch (e) {
    invalidateBatchState(ref, batch.id);
    if (context.mounted) payrollSnack(context, payrollErrorMessage(e));
    return false;
  }
  invalidateBatchState(ref, batch.id);
  final r = out;
  if (r != null && r.arrearsProposed > 0) ref.invalidate(arrearsProvider);
  if (r != null && context.mounted) await _showApproveResult(context, r, names);
  return true;
}

Future<void> _showApproveResult(BuildContext context, BatchApproveResult r, Map<String, String> names) =>
    showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        key: const ValueKey('batch-approve-result'),
        title: Text('${r.approved} approved'),
        content: SizedBox(
          width: double.maxFinite,
          child: ListView(
            shrinkWrap: true,
            children: [
              if (r.arrearsProposed > 0)
                Text('Arrears were proposed for ${r.arrearsProposed} employees. Review them in Pay items.'),
              if (r.failed.isEmpty)
                const Text('Every row was approved.')
              else ...[
                Text('${r.failed.length} rows could not be approved and stay waiting:'),
                for (final f in r.failed)
                  ListTile(
                    dense: true,
                    contentPadding: EdgeInsets.zero,
                    leading: const Icon(Icons.error_outline),
                    title: Text(names[f.id] ?? 'Row ${shortIdOf(f.id)}'),
                    subtitle: Text(bulkReasonLabel(f.reason)),
                  ),
              ],
            ],
          ),
        ),
        actions: [FilledButton(onPressed: () => Navigator.pop(ctx), child: const Text('OK'))],
      ),
    );

String shortIdOf(String id) => id.length <= 8 ? id : id.substring(0, 8);

/// Reject a whole batch (comment required).
Future<bool> rejectBatchFlow(BuildContext context, WidgetRef ref, IncrementBatch batch) async {
  final comment = await showCommentDialog(
    context,
    title: 'Reject this batch?',
    label: 'Reason for rejecting',
    confirmLabel: 'Reject',
    required: true,
  );
  if (comment == null || !context.mounted) return false;
  final repo = ref.read(payrollRepositoryProvider);
  try {
    final n = await repo.rejectIncrementBatch(batch.id, comment: comment);
    invalidateBatchState(ref, batch.id);
    if (context.mounted) payrollSnack(context, 'Rejected $n rows.');
    return true;
  } on ApiException catch (e) {
    invalidateBatchState(ref, batch.id);
    if (context.mounted) payrollSnack(context, payrollErrorMessage(e));
    return false;
  }
}

class BatchCard extends StatelessWidget {
  const BatchCard({required this.batch, required this.onTap, super.key});
  final IncrementBatch batch;
  final VoidCallback onTap;

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
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(child: Text(batchTitle(b), style: theme.textTheme.titleSmall)),
                  if (b.isCancelled) const PayStatusChip('Cancelled', tone: PayTone.bad),
                ],
              ),
              Text(
                '${b.rows} employees'
                '${b.effectiveFrom == null ? '' : ' from ${effectiveLabel(b.effectiveFrom)}'}',
                style: theme.textTheme.bodySmall,
              ),
              Text(batchCountsText(b)),
              Text('Extra per month ${formatBigRupees(b.totals.deltaMonthly)}'),
            ],
          ),
        ),
      ),
    );
  }
}

/// Bulk hike and import batches.
class IncrementBatchesScreen extends ConsumerWidget {
  const IncrementBatchesScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(incrementBatchesProvider(null));
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Increment batches')),
        body: AdminPagedBody<IncrementBatch>(
          async: async,
          emptyMessage: 'No batches yet. A bulk hike or a structure import creates one.',
          emptyIcon: Icons.layers_outlined,
          onRefresh: () async => ref.read(incrementBatchesProvider(null).notifier).reload(),
          onLoadMore: () => ref.read(incrementBatchesProvider(null).notifier).loadMore(),
          itemBuilder: (context, b) => BatchCard(
            batch: b,
            onTap: () => Navigator.of(context).push<void>(
              MaterialPageRoute<void>(builder: (_) => BatchDetailScreen(batchId: b.id)),
            ),
          ),
        ),
      ),
    );
  }
}

/// One batch: totals, counts, its rows, and submit / approve / reject / cancel.
class BatchDetailScreen extends ConsumerStatefulWidget {
  const BatchDetailScreen({required this.batchId, super.key});
  final String batchId;

  @override
  ConsumerState<BatchDetailScreen> createState() => _BatchDetailScreenState();
}

class _BatchDetailScreenState extends ConsumerState<BatchDetailScreen> {
  bool _busy = false;

  Future<void> _guard(Future<void> Function() body) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await body();
    } on ApiException catch (e) {
      invalidateBatchState(ref, widget.batchId);
      if (mounted) payrollSnack(context, payrollErrorMessage(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _submit() => _guard(() async {
        final r = await ref.read(payrollRepositoryProvider).submitIncrementBatch(widget.batchId);
        invalidateBatchState(ref, widget.batchId);
        if (!mounted) return;
        payrollSnack(
          context,
          r.skipped.isEmpty
              ? 'Submitted ${r.submitted} rows for approval.'
              : 'Submitted ${r.submitted} rows. ${r.skipped.length} were skipped: '
                  '${r.skipped.map((s) => bulkReasonLabel(s.reason)).toSet().join(', ')}.',
        );
      });

  Future<void> _cancel(IncrementBatch b) async {
    final reason = await showCommentDialog(
      context,
      title: 'Cancel this batch?',
      label: 'Reason (3 to 200 characters)',
      confirmLabel: 'Cancel batch',
      required: true,
    );
    if (reason == null || !mounted) return;
    await _guard(() async {
      final r = await ref.read(payrollRepositoryProvider).cancelIncrementBatch(b.id, reason);
      invalidateBatchState(ref, b.id);
      if (mounted) {
        payrollSnack(
          context,
          r.notCancelled == 0
              ? 'Cancelled ${r.cancelled} rows.'
              : 'Cancelled ${r.cancelled} rows; ${r.notCancelled} could not be cancelled.',
        );
      }
    });
  }

  Future<void> _approve(IncrementBatch b, Map<String, String> names) =>
      _guard(() => approveBatchFlow(context, ref, b, names: names));

  Future<void> _reject(IncrementBatch b) => _guard(() => rejectBatchFlow(context, ref, b));

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(incrementBatchProvider(widget.batchId));
    final rowsQuery = IncrementQuery(batchId: widget.batchId);
    final rows = ref.watch(incrementsProvider(rowsQuery));
    final perms = ref.watch(adminPermissionsProvider);
    final selfId = ref.watch(adminSelfIdProvider);
    final theme = Theme.of(context);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Batch')),
        body: async.when(
          skipLoadingOnReload: true,
          loading: () => const LoadingView(label: 'Loading batch'),
          error: (e, _) => ErrorView(
            message: payrollMsg(e),
            onRetry: () => ref.invalidate(incrementBatchProvider(widget.batchId)),
          ),
          data: (b) {
            final list = rows.valueOrNull?.items ?? const <Increment>[];
            final names = {for (final i in list) i.id: i.user.display};
            final canManage = canManagePayroll(perms) && !b.isCancelled;
            final canApprove = canApprovePayroll(perms) && !b.isCancelled && b.submittedCount > 0;
            final isMaker = selfId != null && selfId == b.createdBy?.id;
            return ListView(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 48),
              children: [
                Text(batchTitle(b), style: theme.textTheme.titleMedium),
                Text(
                  '${b.rows} employees'
                  '${b.effectiveFrom == null ? '' : ' from ${effectiveLabel(b.effectiveFrom)}'}'
                  '${b.method == null ? '' : b.method == 'proportional' ? ', components scaled' : ', split from gross'}',
                  style: theme.textTheme.bodySmall,
                ),
                if (b.createdBy != null) Text('Created by ${b.createdBy!.display}', style: theme.textTheme.bodySmall),
                const SizedBox(height: 4),
                Text(b.reason),
                SectionCard(
                  title: 'Totals (live rows)',
                  children: [
                    AmountRow(label: 'Monthly gross now', value: formatBigRupees(b.totals.oldGross)),
                    AmountRow(label: 'Monthly gross after', value: formatBigRupees(b.totals.newGross)),
                    AmountRow(label: 'Extra per month', value: formatBigRupees(b.totals.deltaMonthly), bold: true),
                    AmountRow(label: 'Extra per year', value: formatBigRupees(b.totals.deltaAnnual)),
                    const SizedBox(height: 4),
                    Text(batchCountsText(b), key: const ValueKey('batch-counts')),
                  ],
                ),
                if (isMaker && canApprove)
                  const Padding(
                    padding: EdgeInsets.only(bottom: 8),
                    child: Text(
                      'You created this batch, so a different person must approve or reject it.',
                      key: ValueKey('batch-maker-note'),
                    ),
                  ),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    if (canManage && b.draftCount > 0)
                      FilledButton(
                        key: const ValueKey('batch-submit'),
                        onPressed: _busy ? null : _submit,
                        child: Text('Submit ${b.draftCount} drafts'),
                      ),
                    if (canApprove && !isMaker) ...[
                      FilledButton.icon(
                        key: const ValueKey('batch-approve'),
                        onPressed: _busy ? null : () => _approve(b, names),
                        icon: const Icon(Icons.lock_outline),
                        label: Text('Approve ${b.submittedCount}'),
                      ),
                      OutlinedButton(
                        key: const ValueKey('batch-reject'),
                        onPressed: _busy ? null : () => _reject(b),
                        child: const Text('Reject'),
                      ),
                    ],
                    if (canManage && (b.draftCount > 0 || b.submittedCount > 0))
                      TextButton(
                        key: const ValueKey('batch-cancel'),
                        onPressed: _busy ? null : () => _cancel(b),
                        child: const Text('Cancel batch'),
                      ),
                  ],
                ),
                const SizedBox(height: 12),
                Text('Employees', style: theme.textTheme.titleSmall),
                rows.when(
                  skipLoadingOnReload: true,
                  loading: () => const Padding(padding: EdgeInsets.all(16), child: LoadingView()),
                  error: (e, _) => ErrorView(
                    message: payrollMsg(e),
                    onRetry: () => ref.invalidate(incrementsProvider(rowsQuery)),
                  ),
                  data: (page) => page.items.isEmpty
                      ? const EmptyView(message: 'No rows in this batch.')
                      : Column(
                          children: [
                            for (final i in page.items)
                              IncrementTile(
                                increment: i,
                                onTap: () async {
                                  await Navigator.of(context).push<void>(
                                    MaterialPageRoute<void>(
                                      builder: (_) => IncrementDetailScreen(incrementId: i.id),
                                    ),
                                  );
                                  invalidateBatchState(ref, widget.batchId);
                                },
                              ),
                            if (page.hasMore)
                              TextButton(
                                key: const ValueKey('batch-more'),
                                onPressed: () => ref.read(incrementsProvider(rowsQuery).notifier).loadMore(),
                                child: const Text('Load more'),
                              ),
                          ],
                        ),
                ),
              ],
            );
          },
        ),
      ),
    );
  }
}
