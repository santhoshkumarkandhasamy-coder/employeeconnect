import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart'
    show adminPermissionsProvider, adminSelfIdProvider, confirmAction, runWithReauth;
import '../data/payroll_delivery.dart';
import '../data/payroll_repository.dart';
import '../domain/payroll_models.dart' show canManagePayroll, periodTitle;
import '../domain/payroll_money.dart';
import '../domain/phase4a_models.dart' show canApprovePayroll;
import '../domain/phase5a_models.dart';
import 'confirm_payment_screen.dart';
import 'increment_widgets.dart' show showCommentDialog;
import 'pay_items_widgets.dart';
import 'payment_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// One payment batch: header, blockers, lines with the masked snapshot and
/// current account, and the actions that fit its state. Maker actions need
/// `payroll.manage`, checker actions `payroll.approve`; the server enforces
/// both and the second-person rule again.
class PaymentBatchScreen extends ConsumerWidget {
  const PaymentBatchScreen({required this.batchId, super.key});
  final String batchId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(paymentBatchProvider(batchId));
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Payment batch')),
        body: async.when(
          skipLoadingOnReload: true,
          loading: () => const LoadingView(label: 'Loading payment batch'),
          error: (e, _) => ErrorView(message: p5Msg(e), onRetry: () => ref.invalidate(paymentBatchProvider(batchId))),
          data: (b) => PaymentBatchBody(batch: b),
        ),
      ),
    );
  }
}

class PaymentBatchBody extends ConsumerStatefulWidget {
  const PaymentBatchBody({required this.batch, super.key});
  final PaymentBatch batch;

  @override
  ConsumerState<PaymentBatchBody> createState() => _PaymentBatchBodyState();
}

class _PaymentBatchBodyState extends ConsumerState<PaymentBatchBody> {
  bool _busy = false;

  /// Lines whose "pay to the current account" switch is on and not saved yet.
  final Set<String> _useCurrent = {};

  PaymentBatch get _b => widget.batch;

  @override
  void didUpdateWidget(covariant PaymentBatchBody old) {
    super.didUpdateWidget(old);
    if (old.batch.version != widget.batch.version) _useCurrent.clear();
  }

  Future<void> _run(Future<void> Function(PayrollRepository repo) call) async {
    if (_busy) return;
    setState(() => _busy = true);
    final repo = ref.read(payrollRepositoryProvider);
    try {
      await call(repo);
    } on ApiException catch (e) {
      if (isStalePhase5Error(e)) invalidatePaymentState(ref, id: _b.id);
      if (mounted) payrollSnack(context, phase5ErrorMessage(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _done(String message) {
    invalidatePaymentState(ref, id: _b.id);
    if (mounted) payrollSnack(context, message);
  }

  Future<void> _edit(PaymentBatchEdit edit, String message) => _run((repo) async {
        await repo.updatePaymentBatch(_b.id, edit, expectedVersion: _b.version);
        _done(message);
      });

  Future<void> _refreshLines() => _edit(const PaymentBatchEdit(), 'Lines refreshed from the latest payslips and bank details.');

  Future<void> _saveSources() => _edit(
        PaymentBatchEdit(currentAccountLineIds: _useCurrent.toList()),
        'Saved. The file will use the current account for those lines.',
      );

  Future<void> _pickDate() async {
    final today = ref.read(payrollClockProvider)();
    final t = DateTime(today.year, today.month, today.day);
    final current = parseYmd(_b.valueDate) ?? t;
    final d = await showDatePicker(
      context: context,
      initialDate: current.isBefore(t) ? t : current,
      firstDate: t,
      lastDate: t.add(const Duration(days: 30)),
    );
    if (d == null || !mounted) return;
    await _edit(PaymentBatchEdit(valueDate: ymd(d)), 'Value date changed.');
  }

  Future<void> _submit() => _run((repo) async {
        await repo.submitPaymentBatch(_b.id, expectedVersion: _b.version);
        _done('Submitted. Someone with approval rights will review it.');
      });

  Future<void> _withdraw() => _run((repo) async {
        await repo.withdrawPaymentBatch(_b.id, expectedVersion: _b.version);
        _done('Withdrawn. It is a draft again.');
      });

  Future<void> _approve() async {
    final comment = await showCommentDialog(
      context,
      title: 'Approve this payment batch?',
      label: 'Comment (optional)',
      confirmLabel: 'Continue',
    );
    if (comment == null || !mounted) return;
    await _run((repo) async {
      final done = await runWithReauth(
        context,
        reason: 'Approving payment batch ${_b.reference} needs your password.',
        action: (pw) async {
          await repo.approvePaymentBatch(_b.id, expectedVersion: _b.version, currentPassword: pw, comment: comment);
        },
      );
      if (done) _done('Approved. The bank file can now be downloaded.');
    });
  }

  Future<void> _reject() async {
    final comment = await showCommentDialog(
      context,
      title: 'Reject this payment batch?',
      label: 'Reason for rejecting',
      confirmLabel: 'Reject',
      required: true,
    );
    if (comment == null || !mounted) return;
    await _run((repo) async {
      await repo.rejectPaymentBatch(_b.id, expectedVersion: _b.version, comment: comment);
      _done('Rejected. It is a draft again.');
    });
  }

  Future<void> _cancel() async {
    final reason = await showCommentDialog(
      context,
      title: 'Cancel this payment batch?',
      label: 'Reason (3 to 200 characters)',
      confirmLabel: 'Cancel batch',
      required: true,
    );
    if (reason == null || !mounted) return;
    await _run((repo) async {
      await repo.cancelPaymentBatch(_b.id, reason);
      _done('Cancelled. Its payslips can go into a new batch.');
    });
  }

  /// Downloads the approved CSV (the first download marks the batch as exported)
  /// and hands it to the share sheet. The file holds full account numbers.
  Future<void> _download() async {
    final ok = await confirmAction(
      context,
      title: 'Download the bank file?',
      message: _b.isApproved
          ? 'The file has the full account numbers. The first download marks this batch as exported. '
              'Upload it to your bank only and delete it afterwards.'
          : 'The file has the full account numbers. Upload it to your bank only and delete it afterwards.',
      confirmLabel: 'Download',
    );
    if (!ok || !mounted || _busy) return;
    setState(() => _busy = true);
    final repo = ref.read(payrollRepositoryProvider);
    final delivery = ref.read(payrollDeliveryProvider);
    final messenger = ScaffoldMessenger.of(context);
    void say(String m) => messenger
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(m), duration: const Duration(seconds: 6)));
    try {
      PayrollFile? file;
      final done = await runWithReauth(
        context,
        reason: 'Downloading the bank file needs your password.',
        action: (pw) async {
          file = await repo.paymentFile(_b.id, currentPassword: pw);
        },
      );
      // The first call exports the batch on the server: reload either way.
      invalidatePaymentState(ref, id: _b.id);
      if (!done || file == null || !mounted) return;
      final r = await delivery.deliver(context, file!);
      switch (r) {
        case DeliveryOutcome.done:
          break;
        case DeliveryOutcome.cancelled:
          say('Cancelled. The file was not kept. You can download it again.');
        case DeliveryOutcome.unavailable:
          say('Sharing is not available on this device. The file was deleted.');
      }
    } on ApiException catch (e) {
      if (isStalePhase5Error(e)) invalidatePaymentState(ref, id: _b.id);
      say(phase5ErrorMessage(e));
    } on Object {
      say('Could not save the file.');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _confirmPaid() async {
    final changed = await Navigator.of(context).push<bool>(
      MaterialPageRoute<bool>(builder: (_) => ConfirmPaymentScreen(batch: _b)),
    );
    if (changed == true) invalidatePaymentState(ref, id: _b.id);
  }

  @override
  Widget build(BuildContext context) {
    final b = _b;
    final theme = Theme.of(context);
    final perms = ref.watch(adminPermissionsProvider);
    final selfId = ref.watch(adminSelfIdProvider);
    final manage = canManagePayroll(perms);
    final approver = canApprovePayroll(perms);
    final editable = manage && canEditBatch(b, selfId);
    final canDecide = approver && canDecideBatch(b, selfId);
    final why = approver ? cannotDecideBatchReason(b, selfId) : null;
    final hasStale = b.lines.any((l) => l.bankChanged);
    return RefreshIndicator(
      onRefresh: () async => ref.invalidate(paymentBatchProvider(b.id)),
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
                      Expanded(child: Text(b.reference, style: theme.textTheme.titleMedium)),
                      BatchStatusChip(b.status),
                    ],
                  ),
                  Text(periodTitle(b.runPeriod), style: theme.textTheme.bodySmall),
                  const SizedBox(height: 6),
                  AmountRow(label: 'Total to pay', value: formatRupees(b.total), bold: true),
                  Text('${b.counts.lines} lines'
                      '${b.counts.excluded > 0 ? ', ${b.counts.excluded} left out' : ''}'
                      '${b.counts.paid > 0 ? ', ${b.counts.paid} paid' : ''}'
                      '${b.counts.failed > 0 ? ', ${b.counts.failed} failed' : ''}'
                      '${b.counts.exported > 0 ? ', ${b.counts.exported} waiting for the bank' : ''}',),
                  const SizedBox(height: 4),
                  Text('Value date ${valueDateLabel(b.valueDate)}'),
                  if (b.debitAccount != null)
                    Text('Debit account ${b.debitAccount!.label} (${b.debitAccount!.account})'),
                  if (b.createdBy != null) Text('Made by ${b.createdBy!.display}'),
                  if (b.submittedBy != null) Text('Submitted by ${b.submittedBy!.display}'),
                  if (b.decidedBy != null)
                    Text('${b.isDraft ? 'Sent back' : 'Decided'} by ${b.decidedBy!.display}'
                        '${b.decisionComment == null ? '' : ': ${b.decisionComment}'}',),
                  if (b.exportedAt != null)
                    Text('File downloaded ${stampOf(b.exportedAt)}'
                        '${b.exportedBy == null ? '' : ' by ${b.exportedBy!.display}'}',),
                  if (b.fileSha256 != null)
                    Text('File fingerprint ${b.fileSha256!.substring(0, b.fileSha256!.length < 16 ? b.fileSha256!.length : 16)}',
                        style: theme.textTheme.bodySmall,),
                ],
              ),
            ),
          ),
          if (b.isDraft && b.blockers.isNotEmpty)
            SectionCard(
              title: 'Not in this batch (${b.blockers.length})',
              children: [
                for (final k in b.blockers)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 2),
                    child: Text('${k.user.display}: ${blockerLabel(k.reason)}'),
                  ),
              ],
            ),
          if (why != null)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 8),
              child: Text(why, key: const ValueKey('batch-cannot-decide')),
            ),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              if (editable)
                OutlinedButton.icon(
                  key: const ValueKey('batch-refresh'),
                  onPressed: _busy ? null : _refreshLines,
                  icon: const Icon(Icons.refresh),
                  label: const Text('Refresh lines'),
                ),
              if (editable)
                OutlinedButton.icon(
                  key: const ValueKey('batch-date'),
                  onPressed: _busy ? null : _pickDate,
                  icon: const Icon(Icons.event_outlined),
                  label: const Text('Value date'),
                ),
              if (editable && _useCurrent.isNotEmpty)
                FilledButton.tonal(
                  key: const ValueKey('batch-save-sources'),
                  onPressed: _busy ? null : _saveSources,
                  child: Text('Save account choice (${_useCurrent.length})'),
                ),
              if (editable)
                FilledButton.icon(
                  key: const ValueKey('batch-submit'),
                  onPressed: _busy || b.counts.lines == 0 || _useCurrent.isNotEmpty ? null : _submit,
                  icon: const Icon(Icons.send_outlined, size: 18),
                  label: const Text('Submit for approval'),
                ),
              if (manage && b.isSubmitted)
                OutlinedButton(
                  key: const ValueKey('batch-withdraw'),
                  onPressed: _busy ? null : _withdraw,
                  child: const Text('Withdraw'),
                ),
              if (canDecide)
                FilledButton.icon(
                  key: const ValueKey('batch-approve'),
                  onPressed: _busy ? null : _approve,
                  icon: const Icon(Icons.lock_outline, size: 18),
                  label: const Text('Approve'),
                ),
              if (canDecide)
                OutlinedButton(
                  key: const ValueKey('batch-reject'),
                  onPressed: _busy ? null : _reject,
                  child: const Text('Reject'),
                ),
              if (manage && b.hasFile)
                FilledButton.icon(
                  key: const ValueKey('batch-download'),
                  onPressed: _busy ? null : _download,
                  icon: const Icon(Icons.download_outlined, size: 18),
                  label: Text(b.isApproved ? 'Download bank file' : 'Download file again'),
                ),
              if (manage && b.isExported && b.counts.exported > 0)
                FilledButton.tonalIcon(
                  key: const ValueKey('batch-confirm'),
                  onPressed: _busy ? null : _confirmPaid,
                  icon: const Icon(Icons.fact_check_outlined, size: 18),
                  label: const Text('Confirm payments'),
                ),
              if (manage && b.canCancel)
                TextButton(
                  key: const ValueKey('batch-cancel'),
                  onPressed: _busy ? null : _cancel,
                  child: const Text('Cancel batch'),
                ),
            ],
          ),
          if (editable && hasStale)
            const Padding(
              padding: EdgeInsets.only(top: 8),
              child: Text(
                'Some employees changed their bank details after the payslip was published. '
                'Choose the account for each of them below.',
                key: ValueKey('batch-bank-changed-note'),
              ),
            ),
          const SizedBox(height: 8),
          Text('Lines', style: theme.textTheme.titleSmall),
          if (b.lines.isEmpty) const EmptyView(icon: Icons.list_alt_outlined, message: 'No lines.'),
          for (final l in b.lines)
            PaymentLineTile(
              line: l,
              batchDraft: editable,
              busy: _busy,
              useCurrent: _useCurrent.contains(l.id),
              onUseCurrent: (v) => setState(() => v ? _useCurrent.add(l.id) : _useCurrent.remove(l.id)),
              onExclude: () => _edit(PaymentBatchEdit(excludeLineIds: [l.id]), 'Left out of the file.'),
              onInclude: () => _edit(PaymentBatchEdit(includeLineIds: [l.id]), 'Included again.'),
            ),
        ],
      ),
    );
  }
}

String stampOf(DateTime? t) => t == null ? '-' : '${formatDayLong(t)} ${formatTime(t)}';

/// One employee line of a batch.
class PaymentLineTile extends StatelessWidget {
  const PaymentLineTile({
    required this.line,
    required this.batchDraft,
    required this.busy,
    required this.useCurrent,
    required this.onUseCurrent,
    required this.onExclude,
    required this.onInclude,
    super.key,
  });
  final PaymentLine line;
  final bool batchDraft;
  final bool busy;
  final bool useCurrent;
  final ValueChanged<bool> onUseCurrent;
  final VoidCallback onExclude;
  final VoidCallback onInclude;

  @override
  Widget build(BuildContext context) {
    final l = line;
    final theme = Theme.of(context);
    final cs = theme.colorScheme;
    final otherFlags = [for (final f in l.flags) if (f != 'BANK_DETAILS_CHANGED') f];
    return Card(
      key: ValueKey('line-${l.id}'),
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(14, 10, 14, 10),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(child: Text('${l.seq}. ${l.user.display}', style: theme.textTheme.titleSmall, maxLines: 2)),
                PayStatusChip(lineStatusLabel(l.status), tone: lineTone(l.status)),
              ],
            ),
            Text(formatRupees(l.amount), style: theme.textTheme.titleMedium),
            Text('${l.beneficiaryName}  -  ${l.account}  -  ${l.ifsc}', style: theme.textTheme.bodySmall),
            if (l.bankChanged)
              Container(
                key: ValueKey('line-changed-${l.id}'),
                margin: const EdgeInsets.only(top: 6),
                padding: const EdgeInsets.all(8),
                decoration: BoxDecoration(color: cs.errorContainer, borderRadius: BorderRadius.circular(8)),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Bank details changed after the payslip was published.',
                      style: TextStyle(color: cs.onErrorContainer, fontWeight: FontWeight.w600),
                    ),
                    Text(
                      'Reviewed account: ${l.account} / ${l.ifsc}',
                      style: TextStyle(color: cs.onErrorContainer),
                    ),
                    if (l.currentAccount != null)
                      Text(
                        'Current account: ${l.currentAccount} / ${l.currentIfsc ?? '-'}',
                        style: TextStyle(color: cs.onErrorContainer),
                      ),
                    if (l.usesCurrent)
                      Text(
                        'The file pays the current account.',
                        key: ValueKey('line-source-${l.id}'),
                        style: TextStyle(color: cs.onErrorContainer),
                      )
                    else if (batchDraft && l.isPending)
                      Material(
                        type: MaterialType.transparency,
                        child: SwitchListTile(
                          key: ValueKey('line-use-current-${l.id}'),
                          contentPadding: EdgeInsets.zero,
                          dense: true,
                          title: Text('Pay the current account instead', style: TextStyle(color: cs.onErrorContainer)),
                          subtitle: Text(
                            'Off: the file pays the reviewed account.',
                            style: TextStyle(color: cs.onErrorContainer),
                          ),
                          value: useCurrent,
                          onChanged: busy ? null : onUseCurrent,
                        ),
                      )
                    else
                      Text(
                        'The file pays the reviewed account.',
                        key: ValueKey('line-source-${l.id}'),
                        style: TextStyle(color: cs.onErrorContainer),
                      ),
                  ],
                ),
              ),
            if (otherFlags.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Wrap(
                  spacing: 6,
                  runSpacing: 4,
                  children: [
                    for (final f in otherFlags)
                      Chip(
                        avatar: const Icon(Icons.warning_amber_outlined, size: 16),
                        label: Text(lineFlagLabel(f)),
                        visualDensity: VisualDensity.compact,
                      ),
                  ],
                ),
              ),
            if (l.paidOn != null || l.reference != null)
              Text(
                [
                  if (l.paidOn != null) 'Paid on ${valueDateLabel(l.paidOn)}',
                  if (l.reference != null) 'Reference ${l.reference}',
                ].join('  -  '),
                style: theme.textTheme.bodySmall,
              ),
            if (batchDraft && l.isPending)
              Align(
                alignment: Alignment.centerRight,
                child: TextButton(
                  key: ValueKey('line-exclude-${l.id}'),
                  onPressed: busy ? null : onExclude,
                  child: const Text('Leave out'),
                ),
              ),
            if (batchDraft && l.isExcluded)
              Align(
                alignment: Alignment.centerRight,
                child: TextButton(
                  key: ValueKey('line-include-${l.id}'),
                  onPressed: busy ? null : onInclude,
                  child: const Text('Include again'),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
