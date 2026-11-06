import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/domain/admin_models.dart' show EmployeeRef;
import '../../admin/presentation/admin_widgets.dart' show adminPermissionsProvider, adminSelfIdProvider;
import '../data/payroll_repository.dart';
import '../domain/payroll_models.dart' show canManagePayroll, periodTitle;
import '../domain/payroll_money.dart';
import '../domain/phase4a_models.dart' show canApprovePayroll;
import '../domain/phase4b_models.dart';
import '../domain/tds_computation.dart' show tdsModeLabel;
import 'fnf_editor_screen.dart';
import 'fnf_widgets.dart';
import 'increment_widgets.dart' show showCommentDialog;
import 'payroll_providers.dart';
import 'payroll_widgets.dart';
import 'payslip_edit_screen.dart';

/// One full and final settlement: dates, notice, encashment, loans, other
/// items, checklist, estimate, who did what and the actions that fit its state.
/// Maker actions need `payroll.manage`, checker actions `payroll.approve`
/// (the server enforces both and the maker-checker rule again).
class FnfDetailScreen extends ConsumerWidget {
  const FnfDetailScreen({required this.fnfId, super.key});
  final String fnfId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(fnfProvider(fnfId));
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Final settlement')),
        body: async.when(
          skipLoadingOnReload: true,
          loading: () => const LoadingView(label: 'Loading settlement'),
          error: (e, _) => ErrorView(message: payrollMsg(e), onRetry: () => ref.invalidate(fnfProvider(fnfId))),
          data: (f) => FnfDetailBody(fnf: f),
        ),
      ),
    );
  }
}

class FnfDetailBody extends ConsumerStatefulWidget {
  const FnfDetailBody({required this.fnf, super.key});
  final Fnf fnf;

  @override
  ConsumerState<FnfDetailBody> createState() => _FnfDetailBodyState();
}

class _FnfDetailBodyState extends ConsumerState<FnfDetailBody> {
  bool _busy = false;

  Fnf get _f => widget.fnf;

  Future<void> _run(Future<void> Function(PayrollRepository repo) call) async {
    if (_busy) return;
    setState(() => _busy = true);
    final repo = ref.read(payrollRepositoryProvider);
    try {
      await call(repo);
    } on ApiException catch (e) {
      if (isStaleFnfError(e)) invalidateFnfState(ref, id: _f.id);
      if (mounted) payrollSnack(context, fnfErrorMessage(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _submit() => _run((repo) async {
        await repo.submitFnf(_f.id, expectedVersion: _f.version);
        invalidateFnfState(ref, id: _f.id);
        if (mounted) payrollSnack(context, 'Submitted. Someone with approval rights will review it.');
      });

  Future<void> _withdraw() => _run((repo) async {
        await repo.withdrawFnf(_f.id, expectedVersion: _f.version);
        invalidateFnfState(ref, id: _f.id);
        if (mounted) payrollSnack(context, 'Withdrawn. It is a draft again.');
      });

  Future<void> _cancel() async {
    final reason = await showCommentDialog(
      context,
      title: 'Cancel this settlement?',
      label: 'Reason (3 to 200 characters)',
      confirmLabel: 'Cancel settlement',
      required: true,
    );
    if (reason == null || !mounted) return;
    await _run((repo) async {
      await repo.cancelFnf(_f.id, reason);
      invalidateFnfState(ref, id: _f.id);
      if (mounted) payrollSnack(context, 'Cancelled. The last working day was cleared.');
    });
  }

  Future<void> _edit() async {
    final saved = await Navigator.of(context).push<bool>(
      MaterialPageRoute<bool>(builder: (_) => FnfEditorScreen(fnf: _f)),
    );
    if (saved == true) invalidateFnfState(ref, id: _f.id);
  }

  Future<void> _decide(Future<Fnf?> Function() flow) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await flow();
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _statement() async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await downloadPayrollFile(context, ref, (repo) => repo.fnfStatement(_f.id));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final f = _f;
    final theme = Theme.of(context);
    final perms = ref.watch(adminPermissionsProvider);
    final selfId = ref.watch(adminSelfIdProvider);
    final canManage = canManagePayroll(perms) && selfId != f.user.id;
    final canApprove = canApprovePayroll(perms);
    final blocked = cannotDecideFnfReason(f, selfId);
    final preview = f.isCancelled || f.isSettled ? null : ref.watch(fnfPreviewProvider(f.id)).valueOrNull;
    final negative = f.isCancelled || f.isSettled ? false : fnfNetNegative(f, preview);
    final cs = theme.colorScheme;

    return RefreshIndicator(
      onRefresh: () async {
        ref.invalidate(fnfProvider(f.id));
        ref.invalidate(fnfPreviewProvider(f.id));
      },
      child: ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 48),
        children: [
          Row(
            children: [
              Expanded(child: Text(f.user.display, style: theme.textTheme.titleMedium)),
              FnfStatusChip(f.status),
            ],
          ),
          Text(
            '${exitReasonLabel(f.exitReason)}  -  last working day ${lastDayLabel(f.lastWorkingDay)}',
            style: theme.textTheme.bodySmall,
          ),
          Text(
            'Settled in the payroll of ${periodTitle(f.settlementPeriod)} (${fnfRunStatusLabel(f.runStatus)})',
            style: theme.textTheme.bodySmall,
          ),
          if (f.isReadOnly && !f.isCancelled)
            const Padding(
              padding: EdgeInsets.only(top: 6),
              child: Text('This settlement is locked and cannot be edited.', key: ValueKey('fnf-readonly')),
            ),
          const SizedBox(height: 8),
          if (negative)
            Card(
              key: const ValueKey('fnf-negative'),
              color: cs.errorContainer,
              child: Padding(
                padding: const EdgeInsets.all(12),
                child: Text(
                  'The employee would owe the company money. This cannot be settled on a payslip. '
                  'Record a loan as paid outside payroll or reduce the items, then continue.',
                  style: TextStyle(color: cs.onErrorContainer),
                ),
              ),
            ),
          FnfWarnings([for (final w in f.warnings) if (w != 'NEGATIVE_NET_ESTIMATE' || !negative) w]),
          FnfSections(fnf: f),
          if (!f.isCancelled) FnfPreviewCard(fnf: f),
          SectionCard(
            title: 'History',
            children: [
              _who('Created by', f.createdBy, f.createdAt),
              _who('Submitted by', f.submittedBy, f.submittedAt),
              _who('Decided by', f.decidedBy, f.decidedAt),
              if (f.decisionComment != null) Text('Comment: ${f.decisionComment}'),
              if (f.remarks != null) Text('Remarks: ${f.remarks}'),
            ],
          ),
          if (f.finalPayslip != null)
            SectionCard(
              title: 'Final payslip',
              children: [
                Text(
                  'Revision ${f.finalPayslip!.revision}, ${f.finalPayslip!.status}',
                  key: const ValueKey('fnf-final-payslip'),
                ),
                Align(
                  alignment: Alignment.centerLeft,
                  child: TextButton(
                    key: const ValueKey('fnf-open-payslip'),
                    onPressed: () => Navigator.of(context).push<void>(
                      MaterialPageRoute<void>(builder: (_) => PayslipEditScreen(payslipId: f.finalPayslip!.id)),
                    ),
                    child: const Text('Open the payslip'),
                  ),
                ),
              ],
            ),
          if (f.isApproved)
            const Padding(
              padding: EdgeInsets.only(bottom: 8),
              child: Text(
                'Approved. Process the payroll run of the settlement month and publish it: '
                'publishing settles this record.',
              ),
            ),
          if (blocked != null && f.isSubmitted && canApprove)
            Padding(padding: const EdgeInsets.only(bottom: 8), child: Text(blocked, key: const ValueKey('cannot-decide'))),
          if (blocked != null && f.isApproved && canApprove)
            Padding(padding: const EdgeInsets.only(bottom: 8), child: Text(blocked, key: const ValueKey('cannot-decide'))),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              if (f.isDraft && canManage) ...[
                FilledButton(
                  key: const ValueKey('fnf-submit'),
                  onPressed: _busy || negative ? null : _submit,
                  child: const Text('Submit'),
                ),
                OutlinedButton(key: const ValueKey('fnf-edit'), onPressed: _busy ? null : _edit, child: const Text('Edit')),
              ],
              if (f.isSubmitted && canManage)
                OutlinedButton(key: const ValueKey('fnf-withdraw'), onPressed: _busy ? null : _withdraw, child: const Text('Withdraw')),
              if ((f.isDraft || f.isSubmitted) && canManage)
                TextButton(key: const ValueKey('fnf-cancel'), onPressed: _busy ? null : _cancel, child: const Text('Cancel settlement')),
              if (f.isSubmitted && canApprove && blocked == null) ...[
                FilledButton.icon(
                  key: const ValueKey('fnf-approve'),
                  onPressed: _busy || negative ? null : () => _decide(() => approveFnfFlow(context, ref, f)),
                  icon: const Icon(Icons.lock_outline),
                  label: const Text('Approve'),
                ),
                OutlinedButton(
                  key: const ValueKey('fnf-reject'),
                  onPressed: _busy ? null : () => _decide(() => rejectFnfFlow(context, ref, f)),
                  child: const Text('Reject'),
                ),
              ],
              if (f.isApproved && canApprove && blocked == null)
                OutlinedButton.icon(
                  key: const ValueKey('fnf-revoke'),
                  onPressed: _busy ? null : () => _decide(() => revokeFnfFlow(context, ref, f)),
                  icon: const Icon(Icons.undo),
                  label: const Text('Revoke approval'),
                ),
              OutlinedButton.icon(
                key: const ValueKey('fnf-statement'),
                onPressed: _busy ? null : _statement,
                icon: const Icon(Icons.picture_as_pdf_outlined),
                label: Text(f.isSettled ? 'Statement (PDF)' : 'Draft statement (PDF)'),
              ),
            ],
          ),
          if (!f.isSettled)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(
                'The statement is marked "DRAFT - NOT FINAL" until the settlement is settled.',
                style: theme.textTheme.bodySmall,
              ),
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

/// Read-only sections: notice, leave encashment, loans, other items, open
/// items and gratuity. Amounts are quotes until the settlement is approved.
class FnfSections extends StatelessWidget {
  const FnfSections({required this.fnf, super.key});
  final Fnf fnf;

  @override
  Widget build(BuildContext context) {
    final f = fnf;
    final n = f.notice;
    final theme = Theme.of(context);
    final quote = f.amountsAreQuotes ? ' (estimate)' : '';
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SectionCard(
          title: 'Notice period',
          children: [
            Text('Required ${n.requiredDays} days, served ${n.servedDays ?? '-'} days, '
                'shortfall ${n.shortfallDays} days.',),
            if (n.waived) const Text('Waived: nothing is recovered.', key: ValueKey('notice-waived')),
            if (n.hasOverride)
              Text('Shortfall set by HR to ${n.overrideDays} days (computed ${n.computedShortfallDays}).',
                  key: const ValueKey('notice-override'),),
            if (n.overrideReason != null) Text('Reason: ${n.overrideReason}', style: theme.textTheme.bodySmall),
            if (n.shortfallDays > 0 && !n.waived)
              AmountRow(label: 'Notice recovery$quote', value: formatRupees(n.amount), caption: 'Daily rate ${formatRupees(n.dailyRate)}'),
          ],
        ),
        SectionCard(
          title: 'Leave encashment',
          children: [
            if (f.encashments.isEmpty) const Text('None.'),
            for (final e in f.encashments)
              AmountRow(
                label: '${e.leaveType.name} ${e.year}: ${formatDays1(e.days)} days',
                value: formatRupees(e.amount),
                caption: e.availableDays == null
                    ? 'Balance could not be checked'
                    : 'Available ${formatDays1(e.availableDays!)} days, rate ${formatRupees(e.dailyRate)}',
              ),
          ],
        ),
        SectionCard(
          title: 'Loans and advances',
          children: [
            if (f.loans.isEmpty) const Text('No active loans.'),
            for (final l in f.loans)
              AmountRow(
                label: '${l.kindLabel} ${l.reference}',
                value: formatRupees(l.outstanding),
                caption: switch (l.mode) {
                  'payroll' => 'Recovered in the final payslip',
                  'outside' => 'Paid outside payroll${l.paidOn == null ? '' : ' on ${l.paidOn}'}',
                  _ => 'Not decided yet',
                },
              ),
          ],
        ),
        SectionCard(
          title: 'Other items',
          children: [
            if (f.items.isEmpty) const Text('None.'),
            for (final i in f.items)
              AmountRow(
                label: '${fnfItemCodeLabel(i.code)}: ${i.label}',
                value: formatRupees(i.amount),
              ),
          ],
        ),
        if (f.openItems.isNotEmpty)
          SectionCard(
            title: f.isApproved || f.isSettled ? 'Open items moved' : 'Open items',
            children: [
              Text(
                f.moveOpenItems
                    ? 'These unpaid items are moved to the settlement month on approval.'
                    : 'These items are left where they are.',
                style: theme.textTheme.bodySmall,
              ),
              for (final o in f.openItems)
                Text(
                  '${fnfOpenItemLabel(o.type)}${o.code == null ? '' : ' ${o.code}'}'
                  '${o.period == null ? '' : ' - ${periodTitle(o.period!)}'}'
                  '${o.flags.contains('PERIOD_CLOSED') ? ' (month closed)' : ''}',
                ),
            ],
          ),
        if (f.gratuity != null)
          SectionCard(
            title: 'Gratuity (for information)',
            children: [
              Text('Service ${f.gratuity!.serviceLabel}. '
                  '${f.gratuity!.eligible ? 'Eligible' : 'Not eligible'}.'
                  '${f.gratuity!.capped ? ' Capped at the legal limit.' : ''}',),
              if (f.gratuity!.eligible) AmountRow(label: 'Gratuity', value: formatRupees(f.gratuity!.amount)),
              Text(f.gratuity!.note ?? 'Gratuity is paid separately, outside payroll.', style: theme.textTheme.bodySmall),
            ],
          ),
      ],
    );
  }
}

/// Estimate of the final payslip (or the stored figures once approved).
class FnfPreviewCard extends ConsumerWidget {
  const FnfPreviewCard({required this.fnf, super.key});
  final Fnf fnf;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final f = fnf;
    if (f.isSettled && f.summary != null) {
      return SectionCard(title: 'Net settlement', children: [_totals(f.summary!.earnings, f.summary!.deductions, f.summary!.netEstimate)]);
    }
    final async = ref.watch(fnfPreviewProvider(f.id));
    return SectionCard(
      title: 'Net settlement',
      children: [
        async.when(
          skipLoadingOnReload: true,
          loading: () => const Padding(padding: EdgeInsets.all(16), child: LoadingView(label: 'Working out the estimate')),
          error: (e, _) => ErrorView(message: payrollMsg(e), onRetry: () => ref.invalidate(fnfPreviewProvider(f.id))),
          data: (p) => Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                p.source == 'payslip'
                    ? 'From the payslip of ${periodTitle(f.settlementPeriod)}. The published payslip is the final word.'
                    : 'An estimate from the salary structure. The published payslip is the final word.',
                key: const ValueKey('preview-source'),
                style: Theme.of(context).textTheme.bodySmall,
              ),
              Text(
                'Paid days ${formatDays1(p.paidDays)} of ${formatDays1(p.basisDays)}'
                '${p.lopDays > 0 ? ', loss of pay ${formatDays1(p.lopDays)}' : ''}',
                style: Theme.of(context).textTheme.bodySmall,
              ),
              ...payslipLineRows(p.earnings),
              ...payslipLineRows(p.deductions),
              const Divider(),
              _totals(p.grossEarnings, p.totalDeductions, p.netPay),
              if (p.tds != null) ...[
                const SizedBox(height: 8),
                Text('Income tax (TDS) on this settlement', style: Theme.of(context).textTheme.titleSmall),
                AmountRow(
                  key: const ValueKey('fnf-tds-suggested'),
                  label: 'Computed by the engine',
                  value: formatRupees(p.tds!.suggested),
                  caption: tdsModeLabel(p.tds!.mode),
                ),
                if (p.tds!.applied != null)
                  AmountRow(
                    key: const ValueKey('fnf-tds-applied'),
                    label: 'Deducted',
                    value: formatRupees(p.tds!.applied!),
                  ),
                if (p.tds!.hasShortfall)
                  AmountRow(
                    key: const ValueKey('fnf-tds-shortfall'),
                    label: 'Shortfall (cannot be collected)',
                    value: formatRupees(p.tds!.shortfall),
                    caption: 'Set a TDS override with a reason on the final payslip.',
                  ),
              ],
              if (p.warnings.isNotEmpty) ...[
                const SizedBox(height: 6),
                FnfWarnings(p.warnings),
              ],
            ],
          ),
        ),
      ],
    );
  }

  Widget _totals(String earnings, String deductions, String net) => Column(
        children: [
          AmountRow(label: 'Earnings', value: formatRupees(earnings)),
          AmountRow(label: 'Deductions', value: formatRupees(deductions)),
          AmountRow(label: 'Net payable', value: formatSignedRupees(net), bold: true),
        ],
      );
}
