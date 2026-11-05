import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/utils/dates.dart';
import '../../admin/presentation/admin_widgets.dart'
    show AdminPagedBody, adminSelfIdProvider, runWithReauth;
import '../data/payroll_repository.dart';
import '../domain/pay_items_models.dart';
import '../domain/payroll_money.dart';
import 'pay_items_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// Arrears proposals made by the server after a salary structure change that
/// reaches a published month (contract 17.4). A proposal does nothing until it
/// is accepted for a month (password) and is then pulled into that payslip.
class ArrearsSection extends ConsumerStatefulWidget {
  const ArrearsSection({required this.canManage, super.key});
  final bool canManage;

  @override
  ConsumerState<ArrearsSection> createState() => _ArrearsSectionState();
}

class _ArrearsSectionState extends ConsumerState<ArrearsSection> {
  String? _status = 'proposed';

  ArrearsQuery get _query => ArrearsQuery(status: _status);

  void _reload() => ref.invalidate(arrearsProvider);

  Future<void> _accept(Arrears a) async {
    final thisMonth = monthKey(ref.read(payrollClockProvider)());
    final period = await showDialog<String>(
      context: context,
      builder: (_) => _AcceptDialog(arrears: a, options: monthOptions(thisMonth, back: 1)),
    );
    if (period == null || !mounted) return;
    final repo = ref.read(payrollRepositoryProvider);
    await payGuard(context, () async {
      final done = await runWithReauth(
        context,
        reason: 'Accepting arrears for ${a.user.fullName} needs your password.',
        action: (pw) => repo.acceptArrears(
          a.id,
          period: period,
          expectedVersion: a.version,
          currentPassword: pw,
        ),
      );
      if (done && mounted) payrollSnack(context, 'Accepted. It will be added to ${monthTitle(period)}.');
      return done;
    });
    // ARREARS_STALE: the proposal was recalculated, show the new numbers.
    _reload();
  }

  Future<void> _dismiss(Arrears a) async {
    final reason = await showReasonDialog(
      context,
      title: 'Dismiss these arrears?',
      hint: 'Reason (3 to 200 characters)',
      confirmLabel: 'Dismiss',
    );
    if (reason == null || !mounted) return;
    final repo = ref.read(payrollRepositoryProvider);
    await payGuard(context, () async {
      await repo.dismissArrears(a.id, reason);
      if (mounted) payrollSnack(context, 'Dismissed.');
      return true;
    });
    _reload();
  }

  Future<void> _recalculate(Arrears a) async {
    final repo = ref.read(payrollRepositoryProvider);
    await payGuard(context, () async {
      final r = await repo.recalculateArrears(a.user.id);
      if (mounted) {
        payrollSnack(
          context,
          r == null
              ? 'No difference found. Any open proposal was dismissed.'
              : 'Recalculated for ${a.user.fullName}.',
        );
      }
      return true;
    });
    _reload();
  }

  @override
  Widget build(BuildContext context) {
    final q = _query;
    final async = ref.watch(arrearsProvider(q));
    final self = ref.watch(adminSelfIdProvider);
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 12, 0),
          child: Align(
            alignment: Alignment.centerLeft,
            child: SizedBox(
              width: 180,
              child: DropdownButtonFormField<String?>(
                key: const ValueKey('arrears-status'),
                initialValue: _status,
                isExpanded: true,
                decoration: const InputDecoration(
                  labelText: 'Status',
                  isDense: true,
                  border: OutlineInputBorder(),
                ),
                items: const [
                  DropdownMenuItem<String?>(child: Text('All')),
                  DropdownMenuItem<String?>(value: 'proposed', child: Text('Proposed')),
                  DropdownMenuItem<String?>(value: 'pending', child: Text('Accepted (pending)')),
                  DropdownMenuItem<String?>(value: 'processing', child: Text('In payslip')),
                  DropdownMenuItem<String?>(value: 'paid', child: Text('Paid')),
                  DropdownMenuItem<String?>(value: 'dismissed', child: Text('Dismissed')),
                ],
                onChanged: (v) => setState(() => _status = v),
              ),
            ),
          ),
        ),
        Expanded(
          child: AdminPagedBody<Arrears>(
            async: async,
            emptyMessage: 'No arrears here. Proposals appear after a salary structure change '
                'that reaches a published month.',
            emptyIcon: Icons.history_toggle_off_outlined,
            onRefresh: () async => ref.read(arrearsProvider(q).notifier).reload(),
            onLoadMore: () => ref.read(arrearsProvider(q).notifier).loadMore(),
            itemBuilder: (context, a) => ArrearsCard(
              arrears: a,
              canManage: widget.canManage && a.user.id != self,
              onAccept: () => _accept(a),
              onDismiss: () => _dismiss(a),
              onRecalculate: () => _recalculate(a),
            ),
          ),
        ),
      ],
    );
  }
}

class ArrearsCard extends StatelessWidget {
  const ArrearsCard({
    required this.arrears,
    required this.canManage,
    required this.onAccept,
    required this.onDismiss,
    required this.onRecalculate,
    super.key,
  });
  final Arrears arrears;
  final bool canManage;
  final VoidCallback onAccept;
  final VoidCallback onDismiss;
  final VoidCallback onRecalculate;

  @override
  Widget build(BuildContext context) {
    final a = arrears;
    final theme = Theme.of(context);
    final hint = a.period == null ? null : runStatusHint(a.runStatus);
    return Card(
      key: ValueKey('arrears-${a.id}'),
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(14, 10, 14, 6),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(child: Text(a.user.display, style: theme.textTheme.titleSmall)),
                PayStatusChip(payItemStatusLabel(a.status), tone: payToneFor(a.status)),
              ],
            ),
            Text(a.isRecovery ? 'Salary recovery for ${a.rangeLabel}' : 'Arrears for ${a.rangeLabel}'),
            Text(
              '${a.isRecovery ? '-' : '+'} ${formatRupees(a.amount)}',
              style: theme.textTheme.titleMedium,
            ),
            if (a.period != null) Text('To be paid in ${monthTitle(a.period!)}'),
            payNotes(context, [
              if (hint != null) hint,
              for (final f in a.flags) payItemFlagLabel(f),
            ]),
            ExpansionTile(
              key: ValueKey('arrears-breakdown-${a.id}'),
              tilePadding: EdgeInsets.zero,
              title: const Text('Month by month'),
              children: [
                for (final m in a.breakdown)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 8),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          '${monthTitle(m.period)}  (${m.paidDays == m.paidDays.roundToDouble() ? m.paidDays.toInt() : m.paidDays} paid days)',
                          style: theme.textTheme.titleSmall,
                        ),
                        AmountRow(label: 'Paid earnings', value: formatRupees(m.paidEarnings)),
                        AmountRow(label: 'Earnings on new structure', value: formatRupees(m.newEarnings)),
                        if (moneyToPaise(m.alreadyPaid) != 0)
                          AmountRow(label: 'Arrears already paid', value: formatSignedRupees(m.alreadyPaid)),
                        AmountRow(label: 'Difference', value: formatSignedRupees(m.diff), bold: true),
                        AmountRow(label: 'of which Basic', value: formatSignedRupees(m.basicDiff)),
                      ],
                    ),
                  ),
              ],
            ),
            if (canManage)
              Wrap(
                spacing: 4,
                children: [
                  if (a.isProposed)
                    FilledButton.tonal(
                      key: ValueKey('arrears-accept-${a.id}'),
                      onPressed: onAccept,
                      child: const Text('Accept'),
                    ),
                  if (a.canDismiss)
                    TextButton(
                      key: ValueKey('arrears-dismiss-${a.id}'),
                      onPressed: onDismiss,
                      child: const Text('Dismiss'),
                    ),
                  if (a.isProposed || a.status == 'pending')
                    TextButton(
                      key: ValueKey('arrears-recalc-${a.id}'),
                      onPressed: onRecalculate,
                      child: const Text('Recalculate'),
                    ),
                ],
              ),
          ],
        ),
      ),
    );
  }
}

class _AcceptDialog extends StatefulWidget {
  const _AcceptDialog({required this.arrears, required this.options});
  final Arrears arrears;
  final List<String> options;

  @override
  State<_AcceptDialog> createState() => _AcceptDialogState();
}

class _AcceptDialogState extends State<_AcceptDialog> {
  late String _period = widget.options.length > 1 ? widget.options[1] : widget.options.first;

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: const Text('Accept arrears'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              '${widget.arrears.isRecovery ? 'Recover' : 'Pay'} ${formatRupees(widget.arrears.amount)} '
              'for ${widget.arrears.rangeLabel}.',
            ),
            const SizedBox(height: 12),
            MonthDropdown(
              label: 'Add to the payslip of',
              value: _period,
              options: widget.options,
              onChanged: (v) => setState(() => _period = v ?? _period),
            ),
          ],
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            key: const ValueKey('arrears-accept-continue'),
            onPressed: () => Navigator.pop(context, _period),
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: const Text('Continue'),
          ),
        ],
      );
}
