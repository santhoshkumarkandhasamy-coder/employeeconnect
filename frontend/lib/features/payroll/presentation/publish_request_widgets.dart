import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart'
    show adminPermissionsProvider, adminSelfIdProvider, runWithReauth;
import '../data/payroll_repository.dart';
import '../domain/payroll_models.dart' show PublishRequestRef, canManagePayroll, canPublishPayroll;
import '../domain/payroll_money.dart';
import '../domain/phase5a_models.dart';
import 'increment_widgets.dart' show showCommentDialog;
import 'pay_items_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// Publish requests (contract 19.2): status chips, the "Request publish" flow,
/// approve / reject / withdraw and the detail screen. Passwords only go through
/// [runWithReauth]; nothing here keeps money or comments beyond the screen.

PayTone publishTone(String status) => switch (status) {
      'approved' => PayTone.good,
      'submitted' => PayTone.warn,
      'rejected' || 'stale' => PayTone.bad,
      _ => PayTone.neutral,
    };

class PublishStatusChip extends StatelessWidget {
  const PublishStatusChip(this.status, {super.key});
  final String status;

  @override
  Widget build(BuildContext context) => PayStatusChip(publishStatusLabel(status), tone: publishTone(status));
}

String stampLabel(DateTime? t) => t == null ? '-' : '${formatDayLong(t)} ${formatTime(t)}';

/// The newest request of a run or payslip (contract 19.1) on the review screens.
class PublishRefCard extends StatelessWidget {
  const PublishRefCard({required this.request, super.key, this.onTap});
  final PublishRequestRef request;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) => Card(
        key: const ValueKey('publish-ref-card'),
        margin: const EdgeInsets.only(top: 6),
        child: InkWell(
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Row(
              children: [
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text('Publish request', style: Theme.of(context).textTheme.titleSmall),
                      const SizedBox(height: 2),
                      Text(publishRefLine(request), style: Theme.of(context).textTheme.bodySmall),
                    ],
                  ),
                ),
                PublishStatusChip(request.status),
              ],
            ),
          ),
        ),
      );
}

/// One row of a publish request list.
class PublishRequestTile extends StatelessWidget {
  const PublishRequestTile({required this.request, required this.onTap, super.key, this.extra});
  final PublishRequest request;
  final VoidCallback onTap;
  final Widget? extra;

  @override
  Widget build(BuildContext context) {
    final r = request;
    final theme = Theme.of(context);
    return Card(
      key: ValueKey('publish-request-${r.id}'),
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
                  Expanded(
                    child: Text(publishRequestScopeLabel(r), style: theme.textTheme.titleSmall, maxLines: 2),
                  ),
                  PublishStatusChip(r.status),
                ],
              ),
              Text(
                'Asked by ${r.requestedBy.display}  -  ${r.snapshot.payslips} payslips'
                '${r.snapshot.held > 0 ? ', ${r.snapshot.held} on hold' : ''}',
                style: theme.textTheme.bodySmall,
              ),
              if (r.isSubmitted && r.expiresAt != null)
                Text('Expires ${stampLabel(r.expiresAt)}', style: theme.textTheme.bodySmall),
              if (extra != null) extra!,
            ],
          ),
        ),
      ),
    );
  }
}

void invalidatePublishState(WidgetRef ref, {String? requestId, String? runId}) {
  ref
    ..invalidate(publishRequestsProvider)
    ..invalidate(approvalQueueProvider)
    ..invalidate(payrollRunProvider)
    ..invalidate(runPayslipsProvider)
    ..invalidate(adminPayslipProvider)
    ..invalidate(payrollRunsProvider)
    ..invalidate(payrollDashboardProvider)
    ..invalidate(publishCheckRunProvider)
    ..invalidate(publishCheckPayslipProvider);
  if (requestId != null) ref.invalidate(publishRequestProvider(requestId));
  if (runId != null) ref.invalidate(payrollRunProvider(runId));
}

/// "Request publish": checks first (second person on? already a request?),
/// then asks for an optional note and sends the request. Returns the request
/// or null when nothing was sent (a message says why).
Future<PublishRequest?> requestPublishFlow(
  BuildContext context,
  WidgetRef ref, {
  required String id,
  required bool payslip,
}) async {
  final repo = ref.read(payrollRepositoryProvider);
  PublishCheck check;
  try {
    check = payslip ? await repo.publishCheckPayslip(id) : await repo.publishCheckRun(id);
  } on ApiException catch (e) {
    if (context.mounted) payrollSnack(context, phase5ErrorMessage(e));
    return null;
  }
  if (!context.mounted) return null;
  if (check.hasOpenRequest) {
    invalidatePublishState(ref);
    payrollSnack(context, 'A publish request is already waiting for approval. Open it to follow it.');
    return null;
  }
  if (!check.makerChecker) {
    payrollSnack(context, phase5ErrorMessage(const ApiException(code: 'MAKER_CHECKER_OFF', message: '')));
    return null;
  }
  if (!check.canRequest) {
    payrollSnack(context, 'A publish request cannot be sent right now. Reload and try again.');
    return null;
  }
  final comment = await showCommentDialog(
    context,
    title: payslip
        ? 'Ask for approval to publish this payslip?'
        : 'Ask for approval to publish ${check.publishablePayslips} payslips?',
    label: 'Note for the approver (optional)',
    confirmLabel: 'Send request',
  );
  if (comment == null || !context.mounted) return null;
  try {
    final created = payslip
        ? await repo.createPayslipPublishRequest(id, comment: comment)
        : await repo.createRunPublishRequest(id, comment: comment);
    invalidatePublishState(ref, requestId: created.id, runId: created.runId);
    if (context.mounted) {
      payrollSnack(context, 'Publish request sent. A different person with publish rights must approve it.');
    }
    return created;
  } on ApiException catch (e) {
    if (isStalePhase5Error(e) || e.code == 'PUBLISH_REQUEST_EXISTS' || e.code == 'MAKER_CHECKER_OFF') {
      invalidatePublishState(ref);
    }
    if (context.mounted) payrollSnack(context, phase5ErrorMessage(e));
    return null;
  }
}

/// Approve: optional comment, then the password (step-up). No `expected_version`
/// guess: the version of the request as loaded is sent.
Future<PublishApproveResult?> approvePublishFlow(BuildContext context, WidgetRef ref, PublishRequest r) async {
  final comment = await showCommentDialog(
    context,
    title: 'Approve and publish?',
    label: 'Comment (optional)',
    confirmLabel: 'Continue',
  );
  if (comment == null || !context.mounted) return null;
  final repo = ref.read(payrollRepositoryProvider);
  PublishApproveResult? out;
  try {
    final done = await runWithReauth(
      context,
      reason: 'Approving this publish needs your password.',
      action: (pw) async {
        out = await repo.approvePublishRequest(r.id, expectedVersion: r.version, currentPassword: pw, comment: comment);
      },
    );
    if (!done) return null;
  } on ApiException catch (e) {
    if (isStalePhase5Error(e)) invalidatePublishState(ref, requestId: r.id);
    if (context.mounted) payrollSnack(context, phase5ErrorMessage(e));
    return null;
  }
  invalidatePublishState(ref, requestId: r.id, runId: r.runId);
  if (context.mounted) {
    payrollSnack(context, r.isPayslip ? 'Approved. The payslip was published.' : 'Approved. The payslips were published.');
  }
  return out;
}

/// Reject: a comment is required.
Future<PublishRequest?> rejectPublishFlow(BuildContext context, WidgetRef ref, PublishRequest r) async {
  final comment = await showCommentDialog(
    context,
    title: 'Reject this publish request?',
    label: 'Reason for rejecting',
    confirmLabel: 'Reject',
    required: true,
  );
  if (comment == null || !context.mounted) return null;
  final repo = ref.read(payrollRepositoryProvider);
  try {
    final res = await repo.rejectPublishRequest(r.id, expectedVersion: r.version, comment: comment);
    invalidatePublishState(ref, requestId: r.id);
    if (context.mounted) payrollSnack(context, 'Rejected. Nothing was published.');
    return res;
  } on ApiException catch (e) {
    if (isStalePhase5Error(e)) invalidatePublishState(ref, requestId: r.id);
    if (context.mounted) payrollSnack(context, phase5ErrorMessage(e));
    return null;
  }
}

Future<PublishRequest?> withdrawPublishFlow(BuildContext context, WidgetRef ref, PublishRequest r) async {
  final repo = ref.read(payrollRepositoryProvider);
  try {
    final res = await repo.withdrawPublishRequest(r.id, expectedVersion: r.version);
    invalidatePublishState(ref, requestId: r.id);
    if (context.mounted) payrollSnack(context, 'Withdrawn. You can send a new request.');
    return res;
  } on ApiException catch (e) {
    if (isStalePhase5Error(e)) invalidatePublishState(ref, requestId: r.id);
    if (context.mounted) payrollSnack(context, phase5ErrorMessage(e));
    return null;
  }
}

/// Opens the request [id] in the detail screen.
Future<void> openPublishRequest(BuildContext context, String id) => Navigator.of(context).push<void>(
      MaterialPageRoute<void>(builder: (_) => PublishRequestDetailScreen(requestId: id)),
    );

// ---- detail screen -------------------------------------------------------------------------

class PublishRequestDetailScreen extends ConsumerWidget {
  const PublishRequestDetailScreen({required this.requestId, super.key});
  final String requestId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(publishRequestProvider(requestId));
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Publish request')),
        body: async.when(
          skipLoadingOnReload: true,
          loading: () => const LoadingView(label: 'Loading request'),
          error: (e, _) => ErrorView(
            message: p5Msg(e),
            onRetry: () => ref.invalidate(publishRequestProvider(requestId)),
          ),
          data: (r) => PublishRequestBody(request: r),
        ),
      ),
    );
  }
}

class PublishRequestBody extends ConsumerStatefulWidget {
  const PublishRequestBody({required this.request, super.key});
  final PublishRequest request;

  @override
  ConsumerState<PublishRequestBody> createState() => _PublishRequestBodyState();
}

class _PublishRequestBodyState extends ConsumerState<PublishRequestBody> {
  bool _busy = false;

  PublishRequest get _r => widget.request;

  Future<void> _run(Future<Object?> Function() flow) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await flow();
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final r = _r;
    final theme = Theme.of(context);
    final perms = ref.watch(adminPermissionsProvider);
    final selfId = ref.watch(adminSelfIdProvider);
    final manage = canManagePayroll(perms);
    final publisher = canPublishPayroll(perms);
    // The server's publish-check says whether the caller is a maker; without it
    // (still loading or failed) only the requester / employee rule applies.
    final check = r.isSubmitted && publisher && r.checkId.isNotEmpty
        ? (r.isPayslip ? ref.watch(publishCheckPayslipProvider(r.checkId)) : ref.watch(publishCheckRunProvider(r.checkId)))
            .valueOrNull
        : null;
    final canDecide = publisher && canDecidePublishRequest(r, selfId, check: check);
    final why = publisher ? cannotDecidePublishReason(r, selfId, check: check) : null;
    final t = r.snapshot.totals;
    return RefreshIndicator(
      onRefresh: () async => ref.invalidate(publishRequestProvider(r.id)),
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 48),
        children: [
          Card(
            child: Padding(
              padding: const EdgeInsets.all(14),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Expanded(child: Text(publishRequestScopeLabel(r), style: theme.textTheme.titleMedium)),
                      PublishStatusChip(r.status),
                    ],
                  ),
                  if (r.status == 'stale') ...[
                    const SizedBox(height: 6),
                    Text(
                      '${publishStaleLabel(r.staleReason)} Nothing was published. Ask for a new request.',
                      key: const ValueKey('publish-stale-note'),
                      style: TextStyle(color: theme.colorScheme.error),
                    ),
                  ],
                  const SizedBox(height: 8),
                  Text('Asked by ${r.requestedBy.display} on ${stampLabel(r.requestedAt)}'),
                  if (r.isSubmitted) Text('Expires ${stampLabel(r.expiresAt)}'),
                  if (r.comment != null) Text('Note: ${r.comment}'),
                ],
              ),
            ),
          ),
          SectionCard(
            title: 'What will be published',
            children: [
              Text(
                '${r.snapshot.payslips} payslips'
                '${r.snapshot.held > 0 ? ', ${r.snapshot.held} on hold (skipped)' : ''}'
                '${r.snapshot.warnings > 0 ? ', ${r.snapshot.warnings} with warnings' : ''}',
              ),
              if (r.snapshot.processedAt != null) Text('Processed ${stampLabel(r.snapshot.processedAt)}'),
              if (t != null) ...[
                const Divider(),
                AmountRow(label: 'Gross earnings', value: formatRupees(t.grossEarnings)),
                AmountRow(label: 'Deductions', value: formatRupees(t.totalDeductions)),
                AmountRow(label: 'Net pay', value: formatRupees(t.netPay), bold: true),
              ],
            ],
          ),
          if (r.decidedBy != null)
            SectionCard(
              title: 'Decision',
              children: [
                Text('${publishStatusLabel(r.status)} by ${r.decidedBy!.display} on ${stampLabel(r.decidedAt)}'),
                if (r.decisionComment != null) Text('Comment: ${r.decisionComment}'),
              ],
            ),
          if (r.makers != null && r.makers!.isNotEmpty)
            SectionCard(
              title: 'People who made this payroll',
              children: [Text(r.makers!.map((m) => m.display).join(', '))],
            ),
          if (r.isSubmitted) ...[
            if (why != null)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 8),
                child: Text(why, key: const ValueKey('publish-cannot-decide'), style: theme.textTheme.bodyMedium),
              ),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                if (canDecide)
                  FilledButton.icon(
                    key: const ValueKey('publish-approve'),
                    onPressed: _busy ? null : () => _run(() => approvePublishFlow(context, ref, r)),
                    icon: const Icon(Icons.lock_outline, size: 18),
                    label: const Text('Approve and publish'),
                  ),
                if (canDecide)
                  OutlinedButton(
                    key: const ValueKey('publish-reject'),
                    onPressed: _busy ? null : () => _run(() => rejectPublishFlow(context, ref, r)),
                    child: const Text('Reject'),
                  ),
                if (manage)
                  TextButton(
                    key: const ValueKey('publish-withdraw'),
                    onPressed: _busy ? null : () => _run(() => withdrawPublishFlow(context, ref, r)),
                    child: const Text('Withdraw request'),
                  ),
              ],
            ),
          ],
        ],
      ),
    );
  }
}
