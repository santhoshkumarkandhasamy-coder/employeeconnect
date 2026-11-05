import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import '../../admin/presentation/admin_widgets.dart' show adminPermissionsProvider, adminSelfIdProvider;
import '../data/payroll_repository.dart';
import '../domain/payroll_models.dart' show canManagePayroll, payrollErrorMessage;
import '../domain/phase4a_models.dart';
import 'increment_form_screen.dart';
import 'increment_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// One increment: what changes (diff), warnings, who did what, and the
/// actions that fit its state. Maker actions need `payroll.manage`, checker
/// actions `payroll.approve` (the server enforces both and the maker-checker
/// rule again).
class IncrementDetailScreen extends ConsumerWidget {
  const IncrementDetailScreen({required this.incrementId, super.key});
  final String incrementId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(incrementProvider(incrementId));
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Increment')),
        body: async.when(
          skipLoadingOnReload: true,
          loading: () => const LoadingView(label: 'Loading increment'),
          error: (e, _) => ErrorView(
            message: payrollMsg(e),
            onRetry: () => ref.invalidate(incrementProvider(incrementId)),
          ),
          data: (inc) => IncrementDetailBody(increment: inc),
        ),
      ),
    );
  }
}

class IncrementDetailBody extends ConsumerStatefulWidget {
  const IncrementDetailBody({required this.increment, super.key});
  final Increment increment;

  @override
  ConsumerState<IncrementDetailBody> createState() => _IncrementDetailBodyState();
}

class _IncrementDetailBodyState extends ConsumerState<IncrementDetailBody> {
  bool _busy = false;

  Increment get _inc => widget.increment;

  Future<void> _run(Future<void> Function(PayrollRepository repo) call) async {
    if (_busy) return;
    setState(() => _busy = true);
    final repo = ref.read(payrollRepositoryProvider);
    try {
      await call(repo);
    } on ApiException catch (e) {
      if (isStaleIncrementError(e)) invalidateIncrementState(ref, incrementId: _inc.id, userId: _inc.user.id);
      if (mounted) payrollSnack(context, payrollErrorMessage(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _submit() => _run((repo) async {
        await repo.submitIncrement(_inc.id, expectedVersion: _inc.version);
        invalidateIncrementState(ref, incrementId: _inc.id, userId: _inc.user.id);
        if (mounted) payrollSnack(context, 'Submitted. Someone with approval rights will review it.');
      });

  Future<void> _withdraw() => _run((repo) async {
        await repo.withdrawIncrement(_inc.id, expectedVersion: _inc.version);
        invalidateIncrementState(ref, incrementId: _inc.id, userId: _inc.user.id);
        if (mounted) payrollSnack(context, 'Withdrawn. It is a draft again.');
      });

  Future<void> _cancel() async {
    final reason = await showCommentDialog(
      context,
      title: 'Cancel this increment?',
      label: 'Reason (3 to 200 characters)',
      confirmLabel: 'Cancel increment',
      required: true,
    );
    if (reason == null || !mounted) return;
    await _run((repo) async {
      await repo.cancelIncrement(_inc.id, reason);
      invalidateIncrementState(ref, incrementId: _inc.id, userId: _inc.user.id);
      if (mounted) payrollSnack(context, 'Cancelled.');
    });
  }

  Future<void> _edit() async {
    final saved = await Navigator.of(context).push<bool>(
      MaterialPageRoute<bool>(
        builder: (_) => IncrementFormScreen(userId: _inc.user.id, userName: _inc.user.fullName, existing: _inc),
      ),
    );
    if (saved == true) invalidateIncrementState(ref, incrementId: _inc.id, userId: _inc.user.id);
  }

  Future<void> _approve() async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await approveIncrementFlow(context, ref, _inc);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _reject() async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await rejectIncrementFlow(context, ref, _inc);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i = _inc;
    final theme = Theme.of(context);
    final perms = ref.watch(adminPermissionsProvider);
    final selfId = ref.watch(adminSelfIdProvider);
    final canManage = canManagePayroll(perms) && selfId != i.user.id;
    final canApprove = canApprovePayroll(perms);
    final blocked = cannotDecideReason(i, selfId);
    final hint = i.runStatus == 'processed'
        ? 'The payroll run of this month is processed. After approval, reopen and process it again to use the new salary.'
        : i.runStatus == 'published'
            ? 'That month is already published. Approval proposes arrears instead of changing the payslips.'
            : null;

    return RefreshIndicator(
      onRefresh: () async => ref.invalidate(incrementProvider(i.id)),
      child: ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 48),
        children: [
          Row(
            children: [
              Expanded(child: Text(i.user.display, style: theme.textTheme.titleMedium)),
              IncrementStatusChip(i.status),
            ],
          ),
          Text(
            '${incrementKindLabel(i.kind)}'
            '${i.effectiveFrom == null ? '' : ' from ${effectiveLabel(i.effectiveFrom)}'}',
            style: theme.textTheme.bodySmall,
          ),
          const SizedBox(height: 8),
          Text(changeSummary(i.change), key: const ValueKey('increment-change')),
          if (i.percent != null) Text('Requested: ${i.percent}%'),
          if (i.method != null)
            Text(i.method == 'proportional' ? 'Method: scale every component' : 'Method: split from the new gross'),
          const SizedBox(height: 8),
          IncrementWarnings(i.warnings),
          if (hint != null)
            Padding(padding: const EdgeInsets.only(top: 6), child: Text(hint, style: theme.textTheme.bodySmall)),
          SectionCard(
            title: i.kind == 'remove' ? 'Structure to remove' : 'What changes',
            children: [
              if (i.kind == 'remove')
                Text('The salary structure of ${i.base == null ? 'this employee' : effectiveLabel(i.base!.effectiveFrom)} '
                    'will be deleted.',)
              else
                IncrementDiffTable(i.diff),
            ],
          ),
          SectionCard(
            title: 'Reason',
            children: [Text(i.reason.isEmpty ? '-' : i.reason)],
          ),
          SectionCard(
            title: 'History',
            children: [
              _who('Created by', i.createdBy, i.createdAt),
              _who('Submitted by', i.submittedBy, i.submittedAt),
              _who(i.status == 'rejected' ? 'Rejected by' : 'Decided by', i.decidedBy, i.decidedAt),
              if (i.decisionComment != null) Text('Comment: ${i.decisionComment}'),
              if (i.isInBatch) const Text('Part of a bulk hike batch.'),
            ],
          ),
          if (i.status == 'approved' && i.arrearsProposed == true)
            const Text('Arrears were proposed. Review them in Pay items.'),
          const SizedBox(height: 12),
          if (blocked != null && i.isSubmitted && canApprove)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Text(blocked, key: const ValueKey('cannot-decide')),
            ),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              if (i.isDraft && canManage) ...[
                FilledButton(key: const ValueKey('inc-submit'), onPressed: _busy ? null : _submit, child: const Text('Submit')),
                OutlinedButton(key: const ValueKey('inc-edit'), onPressed: _busy ? null : _edit, child: const Text('Edit')),
              ],
              if (i.isSubmitted && canManage && !i.isInBatch)
                OutlinedButton(key: const ValueKey('inc-withdraw'), onPressed: _busy ? null : _withdraw, child: const Text('Withdraw')),
              if ((i.isDraft || i.isSubmitted) && canManage)
                TextButton(key: const ValueKey('inc-cancel'), onPressed: _busy ? null : _cancel, child: const Text('Cancel increment')),
              if (i.isSubmitted && canApprove && blocked == null) ...[
                FilledButton.icon(
                  key: const ValueKey('inc-approve'),
                  onPressed: _busy ? null : _approve,
                  icon: const Icon(Icons.lock_outline),
                  label: const Text('Approve'),
                ),
                OutlinedButton(key: const ValueKey('inc-reject'), onPressed: _busy ? null : _reject, child: const Text('Reject')),
              ],
            ],
          ),
          if (i.isInBatch && i.isSubmitted && canApprove)
            const Padding(
              padding: EdgeInsets.only(top: 8),
              child: Text('This row belongs to a bulk hike. It can also be approved with the whole batch.'),
            ),
        ],
      ),
    );
  }

  Widget _who(String label, EmployeeRef? who, DateTime? at) {
    if (who == null) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Text('$label ${who.display}${at == null ? '' : ' on ${ymd(at)}'}'),
    );
  }
}

