import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart' show adminSelfIdProvider;
import 'batch_screens.dart';
import 'fnf_detail_screen.dart';
import 'fnf_widgets.dart';
import 'increment_detail_screen.dart';
import 'increment_widgets.dart';
import '../domain/phase5a_models.dart';
import 'payment_widgets.dart';
import 'payroll_providers.dart';
import 'publish_request_widgets.dart';
import 'payroll_widgets.dart';

/// Approvals queue (`payroll.approve` and / or `payroll.publish`): increments, bulk
/// batches, final settlements, payment batches (approve) and publish requests (publish)
/// waiting for a decision. The maker (creator or submitter) and the employee
/// cannot decide on their own item; the server enforces it too.
class ApprovalsTab extends ConsumerWidget {
  const ApprovalsTab({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(approvalQueueProvider);
    final selfId = ref.watch(adminSelfIdProvider);
    return async.when(
      skipLoadingOnReload: true,
      loading: () => const LoadingView(label: 'Loading approvals'),
      error: (e, _) => ErrorView(message: payrollMsg(e), onRetry: () => ref.invalidate(approvalQueueProvider)),
      data: (q) => RefreshIndicator(
        onRefresh: () async => ref.invalidate(approvalQueueProvider),
        child: q.count == 0
            ? ListView(
                physics: const AlwaysScrollableScrollPhysics(),
                children: const [
                  SizedBox(height: 80),
                  EmptyView(icon: Icons.task_alt_outlined, message: 'Nothing is waiting for your approval.'),
                ],
              )
            : ListView(
                padding: const EdgeInsets.fromLTRB(12, 8, 12, 48),
                children: [
                  if (q.publishRequests.isNotEmpty) ...[
                    Padding(
                      padding: const EdgeInsets.symmetric(vertical: 6),
                      child: Text('Publish requests', style: Theme.of(context).textTheme.titleSmall),
                    ),
                    for (final r in q.publishRequests)
                      PublishRequestTile(
                        request: r,
                        onTap: () async {
                          await openPublishRequest(context, r.id);
                          ref.invalidate(approvalQueueProvider);
                        },
                        extra: canDecidePublishRequest(r, selfId)
                            ? Padding(
                                padding: const EdgeInsets.only(top: 6),
                                child: Wrap(
                                  spacing: 8,
                                  children: [
                                    FilledButton.icon(
                                      key: ValueKey('queue-publish-approve-${r.id}'),
                                      onPressed: () => approvePublishFlow(context, ref, r),
                                      icon: const Icon(Icons.lock_outline, size: 18),
                                      label: const Text('Approve'),
                                    ),
                                    OutlinedButton(
                                      key: ValueKey('queue-publish-reject-${r.id}'),
                                      onPressed: () => rejectPublishFlow(context, ref, r),
                                      child: const Text('Reject'),
                                    ),
                                  ],
                                ),
                              )
                            : Padding(
                                padding: const EdgeInsets.only(top: 4),
                                child: Text(
                                  cannotDecidePublishReason(r, selfId) ?? '',
                                  key: ValueKey('queue-publish-why-${r.id}'),
                                  style: Theme.of(context).textTheme.bodySmall,
                                ),
                              ),
                      ),
                  ],
                  if (q.paymentBatches.isNotEmpty) ...[
                    Padding(
                      padding: const EdgeInsets.symmetric(vertical: 6),
                      child: Text('Payment batches', style: Theme.of(context).textTheme.titleSmall),
                    ),
                    for (final b in q.paymentBatches)
                      PaymentBatchTile(
                        batch: b,
                        onTap: () async {
                          await openPaymentBatch(context, b.id);
                          ref.invalidate(approvalQueueProvider);
                        },
                        extra: canDecideBatch(b, selfId)
                            ? null
                            : Padding(
                                padding: const EdgeInsets.only(top: 4),
                                child: Text(
                                  cannotDecideBatchReason(b, selfId) ?? '',
                                  key: ValueKey('queue-batch-why-${b.id}'),
                                  style: Theme.of(context).textTheme.bodySmall,
                                ),
                              ),
                      ),
                  ],
                  if (q.batches.isNotEmpty) ...[
                    Padding(
                      padding: const EdgeInsets.symmetric(vertical: 6),
                      child: Text('Bulk batches', style: Theme.of(context).textTheme.titleSmall),
                    ),
                    for (final b in q.batches)
                      BatchCard(
                        batch: b,
                        onTap: () async {
                          await Navigator.of(context).push<void>(
                            MaterialPageRoute<void>(builder: (_) => BatchDetailScreen(batchId: b.id)),
                          );
                          ref.invalidate(approvalQueueProvider);
                        },
                      ),
                  ],
                  if (q.fnfs.isNotEmpty) ...[
                    Padding(
                      padding: const EdgeInsets.symmetric(vertical: 6),
                      child: Text('Final settlements', style: Theme.of(context).textTheme.titleSmall),
                    ),
                    for (final f in q.fnfs)
                      FnfTile(
                        item: f,
                        onTap: () async {
                          await Navigator.of(context).push<void>(
                            MaterialPageRoute<void>(builder: (_) => FnfDetailScreen(fnfId: f.id)),
                          );
                          ref.invalidate(approvalQueueProvider);
                        },
                      ),
                  ],
                  if (q.increments.isNotEmpty) ...[
                    Padding(
                      padding: const EdgeInsets.symmetric(vertical: 6),
                      child: Text('Increments', style: Theme.of(context).textTheme.titleSmall),
                    ),
                    for (final i in q.increments)
                      IncrementTile(
                        increment: i,
                        onTap: () async {
                          await Navigator.of(context).push<void>(
                            MaterialPageRoute<void>(builder: (_) => IncrementDetailScreen(incrementId: i.id)),
                          );
                          ref.invalidate(approvalQueueProvider);
                        },
                        trailingExtra: canDecideIncrement(i, selfId)
                            ? Padding(
                                padding: const EdgeInsets.only(top: 6),
                                child: Wrap(
                                  spacing: 8,
                                  children: [
                                    FilledButton.icon(
                                      key: ValueKey('queue-approve-${i.id}'),
                                      onPressed: () => approveIncrementFlow(context, ref, i),
                                      icon: const Icon(Icons.lock_outline, size: 18),
                                      label: const Text('Approve'),
                                    ),
                                    OutlinedButton(
                                      key: ValueKey('queue-reject-${i.id}'),
                                      onPressed: () => rejectIncrementFlow(context, ref, i),
                                      child: const Text('Reject'),
                                    ),
                                  ],
                                ),
                              )
                            : Padding(
                                padding: const EdgeInsets.only(top: 4),
                                child: Text(
                                  cannotDecideReason(i, selfId) ?? '',
                                  style: Theme.of(context).textTheme.bodySmall,
                                ),
                              ),
                      ),
                  ],
                ],
              ),
      ),
    );
  }
}
